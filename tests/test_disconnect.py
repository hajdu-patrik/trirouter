"""`uninstall --providers`: only the named tools are taken off and remembered; setup, remote, doctor and the model
check leave them alone until setup names them again. Every system call is faked."""
import json
import types

import pytest

from trirouter import cli, cliparse, core, discovery as D, doctor, integrations, platforms as P, remote


@pytest.fixture
def config(monkeypatch):
    path = core.STATE_DIR / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "CONFIG", path)
    monkeypatch.setattr(remote, "CONFIG", path)
    return path


def saved(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def invoke(argv):
    cli.configure(cliparse.resolve(argv, "trirouter", False))


@pytest.fixture
def calls(monkeypatch):
    seen = {"install": [], "remove": []}

    def install(providers, apply=False, uninstall=False, launcher=True, **_):
        seen["install"].append((list(providers), apply, uninstall, launcher))
        return types.SimpleNamespace(failed=0, changes=1)

    monkeypatch.setattr(integrations, "install", install)
    monkeypatch.setattr(remote, "remove", lambda providers=None: seen["remove"].append(providers) or [])
    return seen


def test_uninstall_providers_takes_off_only_those_tools_and_remembers_them(config, calls):
    config.write_text(json.dumps({"remote_name": "Box"}), encoding="utf-8")
    invoke(["uninstall", "--providers=codex,antigravity"])
    assert cli.run_uninstall() == 0
    assert calls["install"] == [(["codex", "antigravity"], True, True, False)]  # the launcher stays
    assert calls["remove"] == [["codex", "antigravity"]]
    assert saved(config) == {"remote_name": "Box", "disconnected": ["codex", "antigravity"]}
    invoke(["uninstall", "--providers=codex"])  # again: no duplicate
    cli.run_uninstall()
    assert saved(config)["disconnected"] == ["codex", "antigravity"]


def test_uninstall_providers_dry_run_changes_nothing(config, calls):
    invoke(["uninstall", "--providers=codex", "--dry-run"])
    assert cli.run_uninstall() == 0
    assert calls["install"] == [(["codex"], False, True, False)] and calls["remove"] == []
    assert not config.exists()


def test_a_full_uninstall_forgets_the_disconnected_tools(config, calls, monkeypatch):
    monkeypatch.setattr(cli.completion, "remove", lambda apply, say: None)
    config.write_text(json.dumps({"disconnected": ["codex"]}), encoding="utf-8")
    invoke(["uninstall"])
    cli.run_uninstall()
    assert calls["remove"] == [None] and "disconnected" not in saved(config)


def tools(monkeypatch):
    found = {p: {"provider": p, "label": p, "installed": True, "version": "1", "logged_in": True}
             for p in P.PROVIDER_NAMES}
    monkeypatch.setattr(P, "detect", lambda deep=True: {p: dict(i) for p, i in found.items()})


def test_setup_leaves_a_disconnected_tool_alone_until_named_again(config, monkeypatch):
    tools(monkeypatch)
    connected = []
    for name in ("welcome", "configure_jev", "next_steps"):
        monkeypatch.setattr(cli, name, lambda *a, **k: None)
    monkeypatch.setattr(cli.secret, "scrub_backups", lambda path, apply: [])
    monkeypatch.setattr(cli, "connect", lambda providers, cfg: connected.append(list(providers)) or (0, 0))
    monkeypatch.setattr(cli, "extras", lambda providers, cfg, changed: 0)
    config.write_text(json.dumps({"disconnected": ["codex", "antigravity"]}), encoding="utf-8")
    invoke(["setup", "--yes"])
    cli.run_setup()
    assert connected[-1] == ["claude"] and saved(config)["disconnected"] == ["codex", "antigravity"]
    invoke(["setup", "--yes", "--providers=codex"])
    cli.run_setup()
    assert connected[-1] == ["codex"] and saved(config)["disconnected"] == ["antigravity"]


def test_remote_setup_skips_a_disconnected_tool(config, monkeypatch):
    tools(monkeypatch)
    started = []
    monkeypatch.setattr(remote, "setup", lambda name, providers, **k: started.append(providers) or [])
    config.write_text(json.dumps({"disconnected": ["antigravity"]}), encoding="utf-8")
    invoke(["remote"])
    cli.run_remote()
    assert started == [["claude", "codex"]]


def test_remove_one_tool_keeps_the_others_the_watchdog_and_the_name(config, monkeypatch, tmp_path):
    monkeypatch.setattr(P, "IS_WINDOWS", True)
    monkeypatch.setattr(remote, "BIN", tmp_path)
    gone, once = [], []
    monkeypatch.setattr(remote, "_unregister", gone.append)
    monkeypatch.setattr(remote, "_ps", lambda script: (0, ""))
    monkeypatch.setattr(remote, "_run_once", lambda *a, **k: once.append(a) or "ok")
    monkeypatch.setattr(P, "find_exe", lambda exe: f"/bin/{exe}")
    config.write_text(json.dumps({"remote_name": "Box"}), encoding="utf-8")
    for name in ("claude-remote.cmd", "codex-remote.cmd", "remote-watchdog.ps1"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert remote.remove(["codex"]) == [("codex", True, "scheduled tasks removed, servers stopped")]
    assert gone == [remote.TASK_CODEX] and not once
    assert sorted(p.name for p in tmp_path.iterdir()) == ["claude-remote.cmd", "remote-watchdog.ps1"]
    assert remote.remove(["antigravity"]) == [("antigravity", True, "daemon stopped")]
    assert gone == [remote.TASK_CODEX] and len(once) == 1
    assert saved(config) == {"remote_name": "Box"}


def test_the_model_check_skips_a_disconnected_tool(config, monkeypatch):
    monkeypatch.setattr(P, "find_exe", lambda exe: None)
    config.write_text(json.dumps({"disconnected": ["codex", "antigravity"]}), encoding="utf-8")
    assert [r["provider"] for r in D.run(apply=False, say=None)["results"]] == ["claude"]


def test_doctor_shows_a_disconnected_tool_as_intended(config, capsys):
    config.write_text(json.dumps({"disconnected": ["codex"]}), encoding="utf-8")
    doctor.tool_line("codex", False, "Codex")
    doctor.tool_line("antigravity", False, "Antigravity")
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("[OK]") and "disconnected" in out[0] and "--providers=codex" in out[0]
    assert out[1].startswith("[!!]")


def test_an_undecided_probe_says_why(monkeypatch):
    monkeypatch.setattr(D, "_why", {})
    monkeypatch.setattr(P, "run", lambda argv, **k: (1, "ERROR: You've hit your usage limit. Try again at Nov 26th."))
    assert D.probe_codex("codex", "gpt-x") is None
    assert D._word(None, "gpt-x") == "could not check (usage limit reached)"
    assert D._word(None, "gpt-y") == "could not check"
    assert D.failure_reason(None, "TimeoutExpired") == "timed out"
    assert D.failure_reason(1, "starting\nnetwork unreachable") == "network unreachable"
