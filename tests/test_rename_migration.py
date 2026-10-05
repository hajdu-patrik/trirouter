"""The rename from jev-router to trirouter: migration of the per-user state folder, recognition of the old
hook / MCP / task / marker names, and the compatibility package that keeps an un-migrated install routing.

Everything runs in tmp dirs with monkeypatching: nothing here touches the real home folder, registry, PATH,
scheduled tasks or the tools' configs (tests/conftest.py isolates the shim folders and the PATH as well).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from trirouter import cli, core, doctor, hub, integrations as I, legacy, platforms as P, remote, skillscan

REPO = Path(__file__).resolve().parents[1]
POSIX_ONLY = pytest.mark.skipif(sys.platform.startswith("win"), reason="needs POSIX permissions / symlinks")


def make_old_state(home, with_config=True):
    old = home / legacy.STATE_NAME
    (old / "logs").mkdir(parents=True)
    (old / "state").mkdir()
    (old / "quarantine" / "evil").mkdir(parents=True)
    (old / "bin").mkdir()
    (old / "tmp").mkdir()
    (old / "logs" / "routing.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (old / "state" / "skillscan.json").write_text("{}", encoding="utf-8")
    (old / "quarantine" / "evil" / "SKILL.md").write_text("x", encoding="utf-8")
    (old / "quarantine" / "evil.json").write_text('{"name": "evil"}', encoding="utf-8")
    (old / "models.local.json").write_text('{"codex": {}}', encoding="utf-8")
    (old / "tmp" / "cli_prompt.txt").write_text("p", encoding="utf-8")
    (old / "bin" / "run_hook.py").write_text("# old shim", encoding="utf-8")
    if with_config:
        fd = os.open(old / "config.json", os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"typesafe_api_key": "tok", "remote_name": "box"}))
    return old


def quiet():
    lines = []
    return lines, lines.append


# ---- state folder resolution (what hooks and the MCP server do: read only) ----

def test_state_dir_prefers_new_and_falls_back_to_old_until_migrated(tmp_path):
    home = tmp_path
    assert legacy.state_dir(home, {}) == home / ".trirouter"          # nothing yet
    make_old_state(home)
    assert legacy.state_dir(home, {}) == home / legacy.STATE_NAME      # un-migrated: keeps working
    (home / ".trirouter").mkdir()
    assert legacy.state_dir(home, {}) == home / legacy.STATE_NAME      # new folder has no config.json: old holds it
    (home / ".trirouter" / "config.json").write_text("{}", encoding="utf-8")
    assert legacy.state_dir(home, {}) == home / ".trirouter"


def test_state_dir_environment_override_new_and_old_name(tmp_path):
    assert legacy.state_dir(tmp_path, {"TRIROUTER_HOME": str(tmp_path / "x")}) == tmp_path / "x"
    assert legacy.state_dir(tmp_path, {legacy.ENV_HOME: str(tmp_path / "y")}) == tmp_path / "y"
    both = {"TRIROUTER_HOME": str(tmp_path / "x"), legacy.ENV_HOME: str(tmp_path / "y")}
    assert legacy.state_dir(tmp_path, both) == tmp_path / "x"


def test_core_reads_the_old_folder_without_creating_the_new_one(tmp_path):
    make_old_state(tmp_path)
    code = ("import sys; sys.path.insert(0, %r); from trirouter import core; print(core.STATE_DIR); print(core.user_config())"
            % str(REPO))
    # a throw-away home: HOME / USERPROFILE decide Path.home()
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    for name in ("TRIROUTER_HOME", legacy.ENV_HOME):
        env.pop(name, None)
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True).stdout.splitlines()
    assert Path(out[0]) == tmp_path / legacy.STATE_NAME
    assert not (tmp_path / ".trirouter").exists()


# ---- migrate_state ----

def test_dry_run_reports_and_changes_nothing(tmp_path):
    old = make_old_state(tmp_path)
    lines, say = quiet()
    assert legacy.migrate_state(tmp_path, apply=False, say=say) == "would-move"
    assert old.is_dir() and not (tmp_path / ".trirouter").exists()
    assert "[DRY]" in lines[0] and "move" in lines[0]


def test_nothing_to_do_without_an_old_folder(tmp_path):
    lines, say = quiet()
    assert legacy.migrate_state(tmp_path, apply=True, say=say) == "none" and not lines


def test_move_brings_everything_along(tmp_path):
    old = make_old_state(tmp_path)
    lines, say = quiet()
    assert legacy.migrate_state(tmp_path, apply=True, say=say) == "moved"
    new = tmp_path / ".trirouter"
    assert not old.exists()
    for rel in ("config.json", "models.local.json", "logs/routing.jsonl", "state/skillscan.json", "tmp/cli_prompt.txt",
                "quarantine/evil/SKILL.md", "quarantine/evil.json", "bin/run_hook.py"):
        assert (new / rel).is_file(), rel
    assert json.loads((new / "config.json").read_text())["typesafe_api_key"] == "tok"
    if not sys.platform.startswith("win"):
        assert (new / "config.json").stat().st_mode & 0o777 == 0o600  # still owner-only
    assert "[DO]" in lines[0]
    # idempotent: a second run finds nothing
    assert legacy.migrate_state(tmp_path, apply=True, say=say) == "none"


def test_move_that_fails_is_reported_and_leaves_the_old_folder(tmp_path, monkeypatch):
    old = make_old_state(tmp_path)

    def locked(*_a, **_k):
        raise PermissionError("in use")
    monkeypatch.setattr(os, "rename", locked)
    lines, say = quiet()
    assert legacy.migrate_state(tmp_path, apply=True, say=say) == "failed"
    assert old.is_dir() and any("could not move" in l for l in lines)


def test_merge_keeps_newer_files_and_merges_config(tmp_path):
    old = make_old_state(tmp_path)
    new = tmp_path / ".trirouter"
    (new / "logs").mkdir(parents=True)
    (new / "bin").mkdir()
    (new / "bin" / "trirouter.py").write_text("# launcher", encoding="utf-8")
    # newer in the new folder: must survive
    (new / "models.local.json").write_text('{"codex": {"new": true}}', encoding="utf-8")
    os.utime(old / "models.local.json", (1_000_000, 1_000_000))
    # older in the new folder: replaced by the newer old copy
    (new / "logs" / "routing.jsonl").write_text("stale\n", encoding="utf-8")
    os.utime(new / "logs" / "routing.jsonl", (1_000_000, 1_000_000))
    # config.json in both: keys merged, the new folder's value wins
    (new / "config.json").write_text(json.dumps({"remote_name": "newbox"}), encoding="utf-8")

    lines, say = quiet()
    assert legacy.migrate_state(tmp_path, apply=False, say=say) == "would-merge"
    assert (old / "config.json").exists() and json.loads((new / "config.json").read_text()) == {"remote_name": "newbox"}

    assert legacy.migrate_state(tmp_path, apply=True, say=say) == "merged"
    assert json.loads((new / "models.local.json").read_text()) == {"codex": {"new": True}}
    assert (new / "logs" / "routing.jsonl").read_text() == '{"a": 1}\n'
    assert json.loads((new / "config.json").read_text()) == {"typesafe_api_key": "tok", "remote_name": "newbox"}
    assert (new / "quarantine" / "evil.json").is_file() and (new / "state" / "skillscan.json").is_file()
    assert (new / "bin" / "trirouter.py").read_text() == "# launcher"          # the old shims are not carried over
    assert not (new / "bin" / "run_hook.py").exists()
    assert not (old / "config.json").exists()                                  # merged, so removed from the old folder
    assert any("stay in" in l for l in lines)                                  # leftovers are reported


def test_merge_leaves_no_old_folder_when_everything_moved(tmp_path):
    old = make_old_state(tmp_path)
    (old / "bin" / "run_hook.py").unlink()
    (old / "bin").rmdir()
    (tmp_path / ".trirouter").mkdir()
    lines, say = quiet()
    assert legacy.migrate_state(tmp_path, apply=True, say=say) == "merged"
    assert not old.exists()


# ---- prepare_state in the CLI ----

@pytest.fixture
def rebasable(monkeypatch):
    """prepare_state rewrites module-level paths: register each so monkeypatch restores it."""
    for mod, names in ((cli, ("STATE", "CONFIG", "MODELS_LOCAL")), (core, ("STATE_DIR",)), (doctor, ("STATE",)),
                       (remote, ("STATE", "BIN", "CONFIG", "LOG")),
                       (skillscan, ("STATE", "CONFIG", "QUARANTINE", "CACHE"))):
        for name in names:
            monkeypatch.setattr(mod, name, getattr(mod, name))


def test_prepare_state_moves_then_re_points_for_a_writing_command(tmp_path, monkeypatch, rebasable, capsys):
    make_old_state(tmp_path)
    monkeypatch.setattr(P, "HOME", tmp_path)
    monkeypatch.setattr(P, "STATE", tmp_path / ".trirouter")
    calls = []
    monkeypatch.setattr(I, "configured_providers", lambda: (["claude"], True))
    monkeypatch.setattr(I, "install", lambda providers, apply=False, **k: calls.append(("install", providers, apply)))
    monkeypatch.setattr(remote, "migrate", lambda name, workdir, apply=True: calls.append(("remote", name, apply)) or [])
    monkeypatch.setattr(cli, "DRY", False)
    monkeypatch.setattr(cli, "FLAGS", {})
    assert cli.prepare_state("setup") is None
    assert (tmp_path / ".trirouter" / "config.json").is_file() and not (tmp_path / legacy.STATE_NAME).exists()
    assert cli.STATE == tmp_path / ".trirouter" and remote.BIN == tmp_path / ".trirouter" / "bin"
    assert skillscan.QUARANTINE == tmp_path / ".trirouter" / "quarantine" and core.STATE_DIR == cli.STATE
    assert calls == [("install", ["claude"], True), ("remote", "box", True)]


def test_prepare_state_dry_run_and_read_only_commands_do_not_move(tmp_path, monkeypatch, rebasable, capsys):
    old = make_old_state(tmp_path)
    monkeypatch.setattr(P, "HOME", tmp_path)
    monkeypatch.setattr(cli, "FLAGS", {})
    monkeypatch.setattr(cli, "DRY", True)
    assert cli.prepare_state("setup") is None
    assert "[DRY]" in capsys.readouterr().out and old.is_dir()
    monkeypatch.setattr(cli, "DRY", False)
    assert cli.prepare_state("doctor") is None and cli.prepare_state("quarantine list") is None
    assert cli.prepare_state("skills") is None          # a preview (no --apply) only reports
    assert old.is_dir() and not (tmp_path / ".trirouter").exists()


def test_prepare_state_stops_when_the_move_fails(tmp_path, monkeypatch, rebasable, capsys):
    make_old_state(tmp_path)
    monkeypatch.setattr(P, "HOME", tmp_path)
    monkeypatch.setattr(cli, "FLAGS", {})
    monkeypatch.setattr(cli, "DRY", False)
    monkeypatch.setattr(os, "rename", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
    assert cli.prepare_state("setup") == 1
    assert "Nothing else was changed" in capsys.readouterr().out


# ---- old hook entries are recognized as ours ----

OLD_HOOK = "python C:/Users/x/.jev-router/bin/run_hook.py claude UserPromptSubmit"
OLDEST_HOOK = "python ~/claude-workspace/router/run_hook.py claude UserPromptSubmit"
OLD_MODULE_HOOK = 'cd "$D" && python -m jev_router.hooks claude UserPromptSubmit --cloud-only'
NEW_HOOK = "python C:/Users/x/.trirouter/bin/run_hook.py claude UserPromptSubmit"
NEW_MODULE_HOOK = 'cd "$D" && python -m trirouter.hooks claude UserPromptSubmit --cloud-only'


@pytest.mark.parametrize("cmd", [OLD_HOOK, OLDEST_HOOK, OLD_MODULE_HOOK, NEW_HOOK, NEW_MODULE_HOOK])
def test_every_generation_of_our_hook_command_is_recognized(cmd):
    assert I.is_router_cmd(cmd)


def test_foreign_hook_is_not_ours():
    assert not I.is_router_cmd("python /opt/other/hook.py") and not I.is_router_cmd("")
    assert I.is_legacy_cmd(OLD_HOOK) and I.is_legacy_cmd(OLD_MODULE_HOOK) and not I.is_legacy_cmd(NEW_HOOK)


def paths_in(tmp_path, monkeypatch):
    paths = {k: tmp_path / Path(v).relative_to(P.HOME) for k, v in P.PATHS.items()}
    monkeypatch.setattr(P, "PATHS", paths)
    monkeypatch.setattr(P, "claude_desktop_config", lambda: tmp_path / "claude_desktop_config.json")
    return paths


def test_old_hook_entries_are_replaced_never_duplicated(tmp_path, monkeypatch):
    paths = paths_in(tmp_path, monkeypatch)
    paths["claude_settings"].parent.mkdir(parents=True)
    old = {"hooks": {ev: [{"hooks": [{"type": "command", "command": OLD_HOOK}]},
                          {"hooks": [{"type": "command", "command": "other"}]}] for ev in ("UserPromptSubmit", "Stop")}}
    paths["claude_settings"].write_text(json.dumps(old), encoding="utf-8")
    paths["agy_hooks"].parent.mkdir(parents=True)
    paths["agy_hooks"].write_text(json.dumps({"router": {"PreInvocation": [{"type": "command", "command": OLD_HOOK}]}}),
                                  encoding="utf-8")
    I.install(("claude", "antigravity"), apply=True, launcher=False)
    cfg = json.loads(paths["claude_settings"].read_text())
    for event in ("UserPromptSubmit", "Stop"):
        cmds = [h["command"] for g in cfg["hooks"][event] for h in g["hooks"]]
        assert cmds.count("other") == 1 and sum(I.is_router_cmd(c) for c in cmds) == 1
        assert not any(I.is_legacy_cmd(c) for c in cmds)
    agy = json.loads(paths["agy_hooks"].read_text())["router"]
    assert not any(I.is_legacy_cmd(h["command"]) for hooks in agy.values() for h in hooks)
    assert I.configured_providers() == (["claude", "antigravity"], False)  # nothing old is left


def test_configured_providers_sees_old_and_new_entries(tmp_path, monkeypatch):
    paths = paths_in(tmp_path, monkeypatch)
    assert I.configured_providers() == ([], False)
    paths["codex_config"].parent.mkdir(parents=True)
    paths["codex_config"].write_text(f'[mcp_servers.{legacy.MCP_NAME}]\ncommand = "py"\nargs = []\n', encoding="utf-8")
    assert I.configured_providers() == (["codex"], True)
    (tmp_path / "claude_desktop_config.json").write_text(json.dumps({"mcpServers": {"trirouter": {}}}), encoding="utf-8")
    assert I.configured_providers() == (["claude", "codex"], True)


# ---- MCP server name ----

def test_json_mcp_replaces_the_old_named_entry(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {legacy.MCP_NAME: {"command": "old"}, "other": {"command": "o"}}}),
                    encoding="utf-8")
    I.json_mcp(path, "x", I.Writer(apply=True))
    servers = json.loads(path.read_text())["mcpServers"]
    assert set(servers) == {"trirouter", "other"} and servers["trirouter"]["command"] != "old"
    I.json_mcp(path, "x", I.Writer(apply=True), uninstall=True)
    assert set(json.loads(path.read_text())["mcpServers"]) == {"other"}
    path.write_text(json.dumps({"mcpServers": {legacy.MCP_NAME: {}}}), encoding="utf-8")
    I.json_mcp(path, "x", I.Writer(apply=True), uninstall=True)  # uninstall removes the old name too
    assert json.loads(path.read_text())["mcpServers"] == {}


def test_codex_mcp_replaces_the_old_section_in_place(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'model = "x"\n\n[mcp_servers.{legacy.MCP_NAME}]\ncommand = "old"\nargs = ["a"]\n\n'
                   f'# >>> agents block\n[agents.a]\n', encoding="utf-8")
    I.codex_mcp(cfg, I.Writer(apply=True))
    text = cfg.read_text()
    assert text.count("[mcp_servers.") == 1 and "[mcp_servers.trirouter]" in text and legacy.MCP_NAME not in text
    assert text.index("[mcp_servers.trirouter]") < text.index("# >>> agents block")
    I.codex_mcp(cfg, I.Writer(apply=True))
    assert cfg.read_text() == text  # idempotent
    cfg.write_text(f'[mcp_servers.{legacy.MCP_NAME}]\ncommand = "o"\n\n[mcp_servers.trirouter]\ncommand = "n"\n', encoding="utf-8")
    I.codex_mcp(cfg, I.Writer(apply=True))
    assert cfg.read_text().count("[mcp_servers.") == 1  # both present: one remains
    I.codex_mcp(cfg, I.Writer(apply=True), uninstall=True)
    assert "mcp_servers" not in cfg.read_text()


def test_doctor_finds_the_old_mcp_name_and_interpreter(tmp_path, monkeypatch):
    path = tmp_path / "cd.json"
    path.write_text(json.dumps({"mcpServers": {legacy.MCP_NAME: {"command": "/py/old"}}}), encoding="utf-8")
    assert doctor.mcp_entry(doctor.jload(path))["command"] == "/py/old"
    paths_in(tmp_path, monkeypatch)
    toml = f'[mcp_servers.{legacy.MCP_NAME}]\ncommand = "/py/codex"\n'
    found = dict(doctor.interpreters({"hooks": {"UserPromptSubmit": [{"hooks": [{"command": OLD_HOOK}]}]}}, toml))
    assert found["Codex MCP"] == "/py/codex" and found["Claude hook"] == "python"
    assert doctor.has_hook({"hooks": {"Stop": [{"hooks": [{"command": OLD_HOOK}]}]}}, "Stop")


# ---- generated agents: old markers are ours ----

def test_old_generated_marker_and_toml_block_are_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "APPLY", True)
    folder = tmp_path / "agents"
    folder.mkdir()
    (folder / "gone.md").write_text(f"---\n# {legacy.GEN_MARK}\n---\n", encoding="utf-8")
    (folder / "mine.md").write_text("hand-written", encoding="utf-8")
    hub._remove_stale(folder, "*.md", set(), "agent")
    assert not (folder / "gone.md").exists() and (folder / "mine.md").exists()
    old_block = (f'x = 1\n\n{legacy_begin()} (old hint)\n[agents.a]\ndescription = "d"\n\n{legacy.TOML_END}\n'
                 f'[hooks.state]\nkeep = true\n')
    new_block = f"{hub.TOML_BEGIN}\n[agents.b]\n\n{hub.TOML_END}\n"
    out = hub._with_agents_block(old_block, new_block)
    assert "[agents.a]" not in out and "[agents.b]" in out and legacy.NAME not in out and "keep = true" in out


def legacy_begin():
    return f"# >>> {legacy.NAME} agents"


def test_links_into_the_old_package_folder_count_as_ours(tmp_path):
    old_skill = tmp_path / "checkout" / legacy.PACKAGE / "skills" / "cli-bridge"
    assert hub._is_checkout_skills_dir(old_skill.parent)
    assert hub._is_checkout_skills_dir(tmp_path / "checkout" / "trirouter" / "skills")
    assert not hub._is_checkout_skills_dir(tmp_path / "checkout" / "other" / "skills")
    assert hub._bundled_in_other_checkout(old_skill)  # cli-bridge is bundled in this checkout


# ---- the launcher and the PATH ----

def test_windows_path_old_entry_out_new_entry_in_with_one_write(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "IS_WINDOWS", True)
    monkeypatch.setattr(P, "python_exe", lambda: "C:/Python/python.exe")
    old_bin, new_bin = tmp_path / "old-bin", tmp_path / "bin"
    monkeypatch.setattr(I, "BIN", new_bin)
    monkeypatch.setattr(I, "LEGACY_BIN", old_bin)
    writes = []
    monkeypatch.setattr(I, "user_path_read", lambda: (rf"C:\Windows;{old_bin};C:\Tools", 2))
    monkeypatch.setattr(I, "user_path_write", lambda value, kind: writes.append((value, kind)))
    monkeypatch.setattr(I, "broadcast_env_change", lambda: None)
    I.install_launcher(I.Writer(apply=True))
    assert len(writes) == 1
    value, kind = writes[0]
    assert str(old_bin) not in value and value.endswith(str(new_bin)) and "C:\\Tools" in value and kind == 2
    # uninstall removes whichever of the two entries is there
    writes.clear()
    monkeypatch.setattr(I, "user_path_read", lambda: (rf"C:\Windows;{old_bin};{new_bin}", 2))
    I.uninstall_launcher(I.Writer(apply=True))
    assert writes == [(r"C:\Windows", 2)]


def test_uninstall_removes_the_old_launcher_files_too(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "IS_WINDOWS", True)
    monkeypatch.setattr(I, "BIN", tmp_path / "bin")
    monkeypatch.setattr(I, "LEGACY_BIN", tmp_path / "old-bin")
    (tmp_path / "old-bin").mkdir()
    for name in ("trirouter.py", "trirouter.cmd"):
        (tmp_path / "old-bin" / name).write_text("x", encoding="utf-8")
    I.uninstall_launcher(I.Writer(apply=True))
    assert not list((tmp_path / "old-bin").iterdir())


@POSIX_ONLY
def test_posix_link_into_the_old_bin_folder_is_re_pointed(monkeypatch, tmp_path):
    monkeypatch.setattr(I, "BIN", tmp_path / "bin")
    monkeypatch.setattr(I, "LEGACY_BIN", tmp_path / "old-bin")
    monkeypatch.setattr(I, "LOCAL_BIN", tmp_path / "local-bin")
    (tmp_path / "local-bin").mkdir()
    link = tmp_path / "local-bin" / "trirouter"
    link.symlink_to(tmp_path / "old-bin" / "trirouter")  # dangling: the old folder moved away
    assert I._posix_link_state() == "legacy"
    I.install_launcher(I.Writer(apply=True))
    assert I._posix_link_state() == "ok" and os.path.realpath(link) == os.path.realpath(tmp_path / "bin" / "trirouter")
    # a foreign link is never touched
    link.unlink()
    link.symlink_to(tmp_path / "elsewhere")
    assert I._posix_link_state() == "foreign"


# ---- remote-access services ----

class FakePs:
    def __init__(self, listing=""):
        self.listing, self.scripts = listing, []

    def __call__(self, script, *a, **k):
        self.scripts.append(script)
        return 0, self.listing if "Get-ScheduledTask" in script and "Unregister" not in script else ""


def test_old_task_names_are_known_and_new_ones_differ():
    assert remote.TASK_CLAUDE == "Trirouter-ClaudeRemote" and remote.TASK_CODEX == "Trirouter-CodexRemote"
    assert remote.TASK_WATCHDOG == "Trirouter-Watchdog"
    assert set(remote.OLD_TASKS.values()) == {"JevRouter-ClaudeRemote", "JevRouter-CodexRemote", "JevRouter-Watchdog"}
    assert remote.LAUNCHD_LABEL == "com.trirouter.claude-remote" and remote.SYSTEMD.name == "trirouter-claude-remote.service"
    assert remote.OLD_SYSTEMD.name == "jev-router-claude-remote.service"


def test_legacy_services_on_windows(monkeypatch):
    monkeypatch.setattr(P, "IS_WINDOWS", True)
    fake = FakePs("JevRouter-ClaudeRemote\nJevRouter-Watchdog\nTrirouter-CodexRemote\n")
    monkeypatch.setattr(remote, "_ps", fake)
    assert remote.legacy_services() == {"claude", "watchdog"}
    monkeypatch.setattr(remote, "_ps", FakePs(""))
    assert remote.legacy_services() == set()


def test_remote_remove_unregisters_old_and_new_task_names(monkeypatch):
    monkeypatch.setattr(P, "IS_WINDOWS", True)
    fake = FakePs()
    monkeypatch.setattr(remote, "_ps", fake)
    monkeypatch.setattr(P, "find_exe", lambda name: None)
    remote.remove()
    text = "\n".join(fake.scripts)
    for task in ("Trirouter-ClaudeRemote", "Trirouter-CodexRemote", "Trirouter-Watchdog",
                 "JevRouter-ClaudeRemote", "JevRouter-CodexRemote", "JevRouter-Watchdog"):
        assert f'Unregister-ScheduledTask -TaskName "{task}"' in text


def test_setting_up_claude_remote_drops_the_old_task_first(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "IS_WINDOWS", True)
    fake = FakePs()
    monkeypatch.setattr(remote, "_ps", fake)
    monkeypatch.setattr(remote, "BIN", tmp_path)
    remote._setup_claude("box", "claude.exe", tmp_path)
    old = next(i for i, s in enumerate(fake.scripts) if "JevRouter-ClaudeRemote" in s and "Unregister" in s)
    new = next(i for i, s in enumerate(fake.scripts) if "Register-ScheduledTask -TaskName \"Trirouter-ClaudeRemote\"" in s)
    assert old < new


def test_migrate_recreates_only_what_existed(monkeypatch):
    monkeypatch.setattr(P, "IS_WINDOWS", True)
    done = []
    monkeypatch.setattr(remote, "legacy_services", lambda: {"claude", "watchdog"})
    monkeypatch.setattr(remote, "_remote_claude", lambda name, workdir: done.append("claude") or ("claude", True, "ok"))
    monkeypatch.setattr(remote, "_remote_codex", lambda workdir: done.append("codex") or ("codex", True, "ok"))
    monkeypatch.setattr(remote, "_setup_watchdog", lambda: done.append("watchdog") or (0, ""))
    report = remote.migrate("box", "/w")
    assert done == ["claude", "watchdog"] and all(ok for _, ok, _ in report)
    done.clear()
    assert remote.migrate("box", "/w", apply=False)[0][2].startswith("would re-point") and not done
    monkeypatch.setattr(remote, "legacy_services", lambda: set())
    assert remote.migrate("box", "/w") == []


def test_remote_status_warns_about_old_tasks(monkeypatch):
    monkeypatch.setattr(P, "IS_WINDOWS", True)
    monkeypatch.setattr(P, "find_exe", lambda name: None)
    monkeypatch.setattr(remote, "_ps", FakePs("JevRouter-Watchdog=Ready\nTrirouter-Watchdog=Ready\n"))
    lines = remote.status()
    assert any("pre-rename task JevRouter-Watchdog" in msg for _, ok, msg in lines)
    assert any(tool == "watchdog" and ok for tool, ok, _ in lines)


# ---- the compatibility package: an install that has not been migrated keeps routing after `git pull` ----

OLD_SHIM = '''# generated by jev-router's installer - runs {target} from the repository
import runpy, sys
sys.path.insert(0, r"{repo}")
{argv}runpy.run_module("{module}", run_name="__main__", alter_sys=True)
'''


def old_shim(tmp_path, module, command=None):
    argv = f'sys.argv[1:1] = ["{command}"]\n' if command else ""
    path = tmp_path / f"{module}.py"
    path.write_text(OLD_SHIM.format(target=module, repo=REPO, module=module, argv=argv), encoding="utf-8")
    return path


def run_shim(shim, args, stdin, tmp_path):
    env = {**os.environ, "ROUTER_BACKEND": "local", "TRIROUTER_HOME": str(tmp_path / "state"),
           "JEV_SKILLS_HUB": str(tmp_path / "skills")}
    return subprocess.run([sys.executable, str(shim), *args], input=stdin, env=env, capture_output=True,
                          text=True, timeout=60)


def test_old_hook_shim_still_routes(tmp_path):
    shim = old_shim(tmp_path, "jev_router.hooks")
    payload = json.dumps({"prompt": "Fix the failing unit test in parser.py", "session_id": "s1", "cwd": str(tmp_path)})
    r = run_shim(shim, ["claude", "UserPromptSubmit"], payload, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "[router]" in r.stdout
    assert (tmp_path / "state" / "logs" / "routing.jsonl").is_file()


def test_old_hook_shim_never_crashes_the_host(tmp_path):
    shim = old_shim(tmp_path, "jev_router.hooks")
    for args, stdin in ((["claude", "UserPromptSubmit"], "not json"), ([], ""), (["claude", "Stop"], "{}")):
        assert run_shim(shim, args, stdin, tmp_path).returncode == 0


def test_old_mcp_shim_still_answers_and_advertises_the_new_name(tmp_path):
    shim = old_shim(tmp_path, "jev_router.mcp_server")
    msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    r = run_shim(shim, [], msg + "\n", tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.splitlines()[0])["result"]["serverInfo"]["name"] == "trirouter"


def test_old_route_and_launcher_shims_still_run(tmp_path):
    shim = old_shim(tmp_path, "jev_router", "route")
    r = run_shim(shim, ["--json", "What is the derivative of x squared?"], "", tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["provider"] == "claude"
