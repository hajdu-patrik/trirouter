"""The daily model discovery (trirouter/discovery.py): new models are added, retired ones removed, a check that
cannot run changes nothing, the Claude and effort policies hold, the 24-hour throttle and the lock work, and the
hooks never fail. Every CLI call is faked: no test runs codex, agy or claude."""
import io
import json
import os
import subprocess
import sys
import time

import pytest

from trirouter import cli, core, discovery as D, doctor, hooks, platforms as P

SPAWN = P.spawn_detached  # the real one: conftest replaces it with a no-op for every test

CATALOG = {
    "policy": {"excluded_efforts": ["ultra"]},
    "claude": {"allowed_families": ["sonnet", "opus", "fable"], "excluded_families": ["haiku"], "models": [
        {"id": "sonnet", "selectable": True, "role": "balanced", "levels": ["low", "high"], "description": "Sonnet"},
        {"id": "opus", "selectable": True, "role": "deep", "levels": ["low", "high"], "description": "Opus"}]},
    "codex": {"models": [
        {"id": "gpt-a", "selectable": True, "role": "balanced", "levels": ["low", "high"], "description": "A"},
        {"id": "gpt-b", "selectable": True, "role": "fast", "levels": ["low", "high"], "description": "B"},
        {"id": "gpt-old", "selectable": False, "levels": ["low"], "description": "not on this plan"},
        {"id": "helper", "selectable": False, "routable": False, "levels": ["low"], "description": "specialist"}]},
    "antigravity": {"models": [
        {"id": "gem-flash", "selectable": True, "role": "balanced", "levels": ["low", "high"], "slug": "{id}-{effort}",
         "description": "Flash"},
        {"id": "gem-pro", "selectable": True, "role": "deep", "levels": ["low", "high"], "slug": "{id}-{effort}",
         "description": "Pro"}]},
}

CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  --effort <level>                      Effort level for the current session
                                        (low, medium, high, xhigh, max)
  --fallback-model <model>              Enable automatic fallback to 'haiku'
  --model <model>                       Model for the current session. Provide
                                        an alias for the latest model (e.g.
                                        {aliases}) or a model's full name.
  -n, --name <name>                     Set a display name
"""


def codex_json(*models):
    """`codex debug models` output: (slug, levels, visibility)."""
    return json.dumps({"models": [{"slug": s, "display_name": s.upper(), "description": f"{s} model.",
                                   "visibility": vis, "supported_reasoning_levels": [{"effort": l} for l in levels]}
                                  for s, levels, vis in models]})


def agy_text(*slugs):
    return "Fetching available models...\n" + "\n".join(f"{s}\t{s.title()}" for s in slugs)


class Fake:
    """Answers the CLI calls discovery makes; records every call."""

    def __init__(self):
        self.installed = {"claude", "codex", "agy"}
        self.codex_list = (0, codex_json(("gpt-a", ["low", "high"], "list"), ("gpt-b", ["low", "high"], "list")))
        self.agy_list = (0, agy_text("gem-flash-low", "gem-flash-high", "gem-pro-low", "gem-pro-high"))
        self.claude_help = (0, CLAUDE_HELP.format(aliases="'opus', or 'sonnet'"))
        self.codex_probe = {}   # model -> (code, output); default: answers
        self.claude_probe = {}
        self.calls = []

    def find_exe(self, name):
        return name if name in self.installed else None

    def run(self, argv, timeout=30, env=None):
        self.calls.append(list(argv))
        exe, rest = argv[0], argv[1:]
        if exe == "codex" and rest[:2] == ["debug", "models"]:
            return self.codex_list
        if exe == "codex" and rest[0] == "exec":
            return self.codex_probe.get(rest[rest.index("-m") + 1], (0, "user\n#norouter Reply with exactly: OK\nOK"))
        if exe == "agy":
            return self.agy_list
        if exe == "claude" and rest == ["--help"]:
            return self.claude_help
        if exe == "claude" and rest[0] == "-p":
            return self.claude_probe.get(rest[rest.index("--model") + 1], (0, "OK"))
        raise AssertionError(f"unexpected call {argv}")

    def probes(self, exe):
        return [c for c in self.calls if c[0] == exe and c[1] in ("exec", "-p")]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "models.json").write_text(json.dumps(CATALOG), encoding="utf-8")
    monkeypatch.setattr(core, "CFG_DIR", cfg_dir)
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    f = Fake()
    monkeypatch.setattr(P, "find_exe", f.find_exe)
    monkeypatch.setattr(P, "run", f.run)
    monkeypatch.setattr(D, "regenerate_agents", lambda say: f.calls.append(["regenerate"]))
    return f


def local():
    path = D.local_file()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def result(outcome, provider):
    return next(r for r in outcome["results"] if r["provider"] == provider)


# ---- new models -----------------------------------------------------------------------------------------------

def test_new_codex_model_is_probed_added_and_selectable_without_ultra(fake):
    fake.codex_list = (0, codex_json(("gpt-a", ["low", "high"], "list"), ("gpt-b", ["low", "high"], "list"),
                                     ("gpt-new", ["low", "high", "max", "ultra"], "list")))
    out = D.run("discover", apply=True, say=None)
    assert result(out, "codex")["added"] == ["gpt-new"] and out["changed"]
    entry = local()["codex"]["gpt-new"]
    assert entry["selectable"] and entry["listed"] and entry["levels"] == ["low", "high", "max"]
    assert entry["role"] in ("fast", "balanced", "deep") and "discovered" in entry and entry["description"]
    assert [c[c.index("-m") + 1] for c in fake.probes("codex")] == ["gpt-new"]  # only the new model costs a prompt
    models = core.models_for("codex")
    assert "gpt-new" in models and "ultra" not in models["gpt-new"]["levels"]
    assert ["regenerate"] in fake.calls


def test_new_model_rejected_by_the_account_is_recorded_but_not_selectable(fake):
    fake.codex_list = (0, codex_json(("gpt-a", ["low"], "list"), ("gpt-b", ["low"], "list"), ("gpt-pro", ["low"], "list")))
    fake.codex_probe["gpt-pro"] = (1, '{"error":{"message":"The \'gpt-pro\' model is not supported when using Codex '
                                      'with a ChatGPT account."}}')
    out = D.run("discover", apply=True, say=None)
    assert result(out, "codex")["found_unavailable"] == ["gpt-pro"]
    assert local()["codex"]["gpt-pro"]["selectable"] is False and "gpt-pro" not in core.models_for("codex")
    D.run("discover", apply=True, say=None)
    assert len(fake.probes("codex")) == 1  # known now: not prompted again every day


def test_new_hidden_codex_model_is_recorded_not_selected_and_not_probed(fake):
    fake.codex_list = (0, codex_json(("gpt-a", ["low"], "list"), ("gpt-b", ["low"], "list"), ("gpt-x", ["low"], "hide")))
    out = D.run("discover", apply=True, say=None)
    assert result(out, "codex")["hidden"] == ["gpt-x"] and not fake.probes("codex")
    assert local()["codex"]["gpt-x"] == dict(local()["codex"]["gpt-x"], selectable=False, hidden=True)


def test_new_antigravity_model_takes_its_levels_from_the_slugs(fake):
    fake.agy_list = (0, agy_text("gem-flash-low", "gem-flash-high", "gem-pro-low", "gem-pro-high",
                                 "gem-ultra-pro-low", "gem-ultra-pro-medium", "gem-ultra-pro-ultra", "other-model"))
    out = D.run("discover", apply=True, say=None)
    assert sorted(result(out, "antigravity")["added"]) == ["gem-ultra-pro", "other-model"]
    entries = local()["antigravity"]
    assert entries["gem-ultra-pro"]["levels"] == ["low", "medium"] and entries["gem-ultra-pro"]["slug"] == "{id}-{effort}"
    assert entries["gem-ultra-pro"]["role"] == "deep"
    assert entries["other-model"]["levels"] == [] and entries["other-model"]["slug"] == "{id}"
    models = core.models_for("antigravity")
    assert {"gem-ultra-pro", "other-model"} <= set(models)
    route_slug = models["gem-ultra-pro"]["slug"].format(id="gem-ultra-pro", effort="low")
    assert route_slug == "gem-ultra-pro-low"


# ---- removed models -------------------------------------------------------------------------------------------

def test_model_no_longer_listed_is_removed_and_comes_back_when_offered_again(fake):
    fake.codex_list = (0, codex_json(("gpt-a", ["low", "high"], "list")))
    out = D.run("discover", apply=True, say=None)
    assert result(out, "codex")["removed"] == ["gpt-b"]
    assert local()["codex"]["gpt-b"]["selectable"] is False and local()["codex"]["gpt-b"]["listed"] is False
    assert "gpt-b" not in core.models_for("codex") and "gpt-a" in core.models_for("codex")
    assert "removed" in D.note_file().read_text(encoding="utf-8") and "gpt-b" in D.note_file().read_text(encoding="utf-8")

    fake.codex_list = (0, codex_json(("gpt-a", ["low"], "list"), ("gpt-b", ["low"], "list")))
    out = D.run("discover", apply=True, say=None)
    assert result(out, "codex")["restored"] == ["gpt-b"] and "gpt-b" in core.models_for("codex")
    assert "removed" not in local()["codex"]["gpt-b"]


def test_antigravity_model_gone_from_the_list_is_removed(fake):
    fake.agy_list = (0, agy_text("gem-flash-low", "gem-flash-high"))
    out = D.run("discover", apply=True, say=None)
    assert result(out, "antigravity")["removed"] == ["gem-pro"] and "gem-pro" not in core.models_for("antigravity")


def test_full_probe_mode_tries_every_known_codex_model(fake):
    fake.codex_probe["gpt-b"] = (1, "ERROR: The 'gpt-b' model does not exist")
    fake.codex_probe["gpt-old"] = (0, "OK")
    out = D.run("probe", apply=True, say=None)
    assert sorted(c[c.index("-m") + 1] for c in fake.probes("codex")) == ["gpt-a", "gpt-b", "gpt-old"]  # never the specialist
    r = result(out, "codex")
    assert r["removed"] == ["gpt-b"] and r["restored"] == ["gpt-old"]
    assert set(core.models_for("codex")) == {"gpt-a", "gpt-old"}


# ---- checks that cannot run change nothing --------------------------------------------------------------------

@pytest.mark.parametrize("breakage", ["codex_fails", "codex_garbage", "agy_logged_out", "missing_cli", "empty_list",
                                      "names_none_known"])
def test_a_check_that_cannot_run_changes_nothing(fake, breakage):
    D.save_local({"codex": {"gpt-a": {"selectable": True}}})
    before = D.local_file().read_bytes()
    if breakage == "codex_fails":
        fake.codex_list = (1, "error: network unreachable")
    elif breakage == "codex_garbage":
        fake.codex_list = (0, "{not json")
    elif breakage == "agy_logged_out":
        fake.agy_list = (1, "Not logged in. Please sign in.")
    elif breakage == "missing_cli":
        fake.installed = set()
    elif breakage == "empty_list":
        fake.codex_list = (0, '{"models": []}')
        fake.agy_list = (0, "Fetching available models...\n")
    else:  # a fallback list that has none of the models in use
        fake.codex_list = (0, codex_json(("totally-else", ["low"], "list")))
        fake.agy_list = (0, agy_text("other-low"))
    out = D.run("discover", apply=True, say=None)
    broken = {"codex_fails": ["codex"], "codex_garbage": ["codex"], "agy_logged_out": ["antigravity"],
              "missing_cli": ["claude", "codex", "antigravity"], "empty_list": ["codex", "antigravity"],
              "names_none_known": ["codex", "antigravity"]}[breakage]
    for p in broken:
        assert result(out, p)["status"] == "unchecked" and result(out, p)["reason"]
    assert D.local_file().read_bytes() == before
    assert set(core.models_for("codex")) == {"gpt-a", "gpt-b"}
    assert any("could not check" in line for line in D.summary_lines(out["results"]))


def test_new_model_whose_probe_fails_for_another_reason_is_retried_not_added(fake):
    fake.codex_list = (0, codex_json(("gpt-a", ["low"], "list"), ("gpt-b", ["low"], "list"), ("gpt-new", ["low"], "list")))
    fake.codex_probe["gpt-new"] = (1, "stream disconnected before completion: error sending request")
    out = D.run("discover", apply=True, say=None)
    assert result(out, "codex")["pending"] == ["gpt-new"] and "gpt-new" not in local().get("codex", {})
    fake.codex_probe["gpt-new"] = (None, "TimeoutExpired: timed out")
    D.run("discover", apply=True, say=None)
    assert "gpt-new" not in local().get("codex", {})


def test_a_corrupt_models_local_json_is_never_overwritten(fake):
    D.local_file().parent.mkdir(parents=True, exist_ok=True)
    D.local_file().write_text("{broken", encoding="utf-8")
    out = D.run("discover", apply=True, say=None)
    assert all(r["status"] == "unchecked" for r in out["results"])
    assert D.local_file().read_text(encoding="utf-8") == "{broken"


def test_dry_run_writes_nothing(fake):
    fake.codex_list = (0, codex_json(("gpt-a", ["low"], "list")))
    out = D.run("discover", apply=False, say=None)
    assert out["changed"] and result(out, "codex")["removed"] == ["gpt-b"]
    assert not core.STATE_DIR.exists()


# ---- Claude policy --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("alias, ok", [("opus", True), ("fable", True), ("mythos", True), ("haiku", False),
                                       ("Haiku", False), ("claude-haiku-4-5-20251001", False),
                                       ("claude-opus-4-1-20250805", False), ("claude-sonnet-5", False),
                                       ("sonnet[1m]", False), ("opusplan", False), ("default", False), ("", False)])
def test_claude_alias_policy(fake, alias, ok):
    assert core.claude_alias_allowed(alias) is ok


def test_claude_help_aliases_new_family_added_haiku_and_dated_ids_rejected(fake):
    fake.claude_help = (0, CLAUDE_HELP.format(aliases="'fable', 'opus', 'sonnet', 'haiku', 'opusplan' or "
                                                      "'claude-opus-4-1-20250805'"))
    out = D.run("discover", apply=True, say=None)
    r = result(out, "claude")
    assert r["added"] == ["fable"] and [n for n, _ in r["rejected"]] == ["haiku", "opusplan", "claude-opus-4-1-20250805"]
    assert dict(r["rejected"])["haiku"] == "excluded family"
    assert set(local()["claude"]) == {"fable"} and local()["claude"]["fable"]["levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert "fable" in core.models_for("claude") and "haiku" not in core.models_for("claude")


def test_a_hand_written_haiku_or_dated_entry_is_never_selectable(fake):
    D.save_local({"claude": {"haiku": {"selectable": True, "levels": ["low"]},
                             "claude-opus-4-1-20250805": {"selectable": True, "levels": ["low"]}}})
    assert set(core.models_for("claude")) == {"sonnet", "opus"}


def test_claude_alias_missing_from_help_is_removed_only_when_a_prompt_confirms(fake):
    fake.claude_help = (0, CLAUDE_HELP.format(aliases="'sonnet'"))
    fake.claude_probe["opus"] = (1, "API Error: 500 overloaded")
    D.run("discover", apply=True, say=None)
    assert "opus" in core.models_for("claude")  # could not check: kept
    fake.claude_probe["opus"] = (1, "There's an issue with the selected model (opus). It may not exist or you may "
                                    "not have access to it.")
    out = D.run("discover", apply=True, say=None)
    assert result(out, "claude")["removed"] == ["opus"] and "opus" not in core.models_for("claude")


def test_routable_false_beats_a_recorded_probe(fake):
    D.save_local({"codex": {"helper": {"selectable": True}}})
    assert "helper" not in core.models_for("codex")


def test_parse_claude_help_handles_wrapped_text():
    names, levels = D.parse_claude_help(CLAUDE_HELP.format(aliases="'fable', 'opus', or\n                    'sonnet'"))
    assert names == ["fable", "opus", "sonnet"] and levels == ["low", "medium", "high", "xhigh", "max"]


def test_verdict_tells_rejection_from_failure():
    assert D.verdict(0, "OK") is True
    assert D.verdict(1, "The 'x' model is not supported when using Codex with a ChatGPT account.") is False
    assert D.verdict(1, '{"status":404,"error":"model_not_found"}') is False
    assert D.verdict(1, "401 Unauthorized: please log in") is None
    assert D.verdict(None, "TimeoutExpired") is None


# ---- schedule, lock, state ------------------------------------------------------------------------------------

@pytest.fixture
def spawned(fake, monkeypatch):
    calls = []
    monkeypatch.setattr(P, "spawn_detached", lambda argv, env=None, cwd=None: calls.append((argv, env)) or True)
    monkeypatch.delenv(D.CHILD_ENV, raising=False)
    monkeypatch.setattr(core, "is_cloud", lambda: False)
    return calls


def test_maybe_start_spawns_once_per_24_hours(spawned):
    now = 1_000_000_000.0
    assert D.maybe_start(now) is True and len(spawned) == 1
    argv, env = spawned[0]
    assert argv[1:] == ["-m", "trirouter.discovery", "--scheduled"] and env[D.CHILD_ENV] == "1"
    assert D.maybe_start(now + 3600) is False and D.maybe_start(now + 23 * 3600) is False
    assert D.maybe_start(now + 24 * 3600 + 1) is True and len(spawned) == 2
    assert not D.lock_file().exists()  # the hook only holds the lock while it stamps


def test_maybe_start_respects_the_lock_and_takes_over_a_stale_one(spawned):
    D.lock_file().parent.mkdir(parents=True, exist_ok=True)
    D.lock_file().write_text("123 0", encoding="utf-8")
    assert D.maybe_start() is False and not spawned  # a check is running
    old = time.time() - D.LOCK_STALE_S - 60
    os.utime(D.lock_file(), (old, old))
    assert D.maybe_start() is True and len(spawned) == 1


def test_parallel_sessions_start_only_one_check(spawned):
    assert [D.maybe_start(), D.maybe_start(), D.maybe_start()] == [True, False, False] and len(spawned) == 1


def test_auto_off_cloud_and_the_child_itself_never_start_a_check(spawned, monkeypatch):
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (core.STATE_DIR / "config.json").write_text(json.dumps({"model_discovery": {"auto": False}}), encoding="utf-8")
    assert D.maybe_start() is False
    (core.STATE_DIR / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(D.CHILD_ENV, "1")
    assert D.maybe_start() is False
    monkeypatch.delenv(D.CHILD_ENV)
    monkeypatch.setattr(core, "is_cloud", lambda: True)
    assert D.maybe_start() is False and not spawned


def test_child_env_drops_the_nested_session_markers(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    env = D.child_env()
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env and env[D.CHILD_ENV] == "1"


def test_scheduled_run_records_state_log_and_a_note_shown_once(fake, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    fake.codex_list = (0, codex_json(("gpt-a", ["low"], "list"), ("gpt-b", ["low"], "list"), ("gpt-new", ["low"], "list")))
    assert D.main(["--scheduled"]) == 0
    state = D.read_state()
    assert state["changed"] and state["results"]["codex"]["added"] == ["gpt-new"] and state["last_start"] > 0
    assert "gpt-new" in D.log_file().read_text(encoding="utf-8")
    assert not D.lock_file().exists()
    note = D.pending_note()
    assert "new model available: Codex gpt-new" in note and D.pending_note() == ""
    assert D.last_check()[1] is True


def test_scheduled_run_skips_while_another_check_holds_the_lock(fake, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(D, "LOCK_WAIT_S", 0)
    assert D.acquire_lock()
    D.main(["--scheduled"])
    assert not fake.calls and not D.state_file().exists()
    D.release_lock()


def test_scheduled_run_logs_an_unexpected_error_and_exits_zero(fake, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(D, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert D.main(["--scheduled"]) == 0
    assert "RuntimeError: boom" in D.log_file().read_text(encoding="utf-8") and not D.lock_file().exists()


# ---- hooks never fail -----------------------------------------------------------------------------------------

def test_session_start_shows_the_note_once_and_never_raises(spawned, capsys):
    D._write_atomic(D.note_file(), "[trirouter] Model list updated - new model available: Codex gpt-new.\n")
    hooks.on_session_start("claude", b"{}")
    out = json.loads(capsys.readouterr().out)
    assert "gpt-new" in out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart" and len(spawned) == 1
    hooks.on_session_start("claude", b"{}")
    assert capsys.readouterr().out == ""


def test_hooks_survive_a_broken_discovery(spawned, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(D, "maybe_start", boom)
    monkeypatch.setattr(D, "pending_note", boom)
    hooks.on_session_start("claude", b"{}")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"session_id": "x"}')))
    assert hooks.main(["claude", "SessionStart"]) == 0
    assert capsys.readouterr().out == ""


def test_maybe_start_with_an_unusable_state_folder_returns_false(spawned, monkeypatch, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(core, "STATE_DIR", blocker)
    monkeypatch.setattr(P, "spawn_detached", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    assert D.maybe_start() is False
    hooks.on_session_start("claude", b"{}")


def test_codex_and_antigravity_prompts_start_the_check_too(spawned, monkeypatch):
    monkeypatch.setattr(hooks, "route_and_log", lambda *a, **k: "[router] ok")
    hooks.on_prompt("codex", "UserPromptSubmit", json.dumps({"prompt": "hello there", "session_id": "s"}).encode())
    assert len(spawned) == 1


# ---- detached spawn per OS ------------------------------------------------------------------------------------

class Popen:
    calls = []

    def __init__(self, argv, **kw):
        if kw.get("creationflags", 0) & P._BREAKAWAY and Popen.refuse_breakaway:
            raise PermissionError("access denied")
        Popen.calls.append(kw)


@pytest.fixture
def popen(monkeypatch):
    Popen.calls, Popen.refuse_breakaway = [], False
    monkeypatch.setattr(P.subprocess, "Popen", Popen)
    return Popen


@pytest.mark.parametrize("windows", [False, True])
def test_spawn_detached_never_inherits_the_hook_pipes(popen, monkeypatch, windows):
    monkeypatch.setattr(P, "IS_WINDOWS", windows)
    assert SPAWN(["python", "-m", "x"]) is True
    kw = popen.calls[0]
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL and kw["close_fds"]
    if windows:
        assert kw["creationflags"] & P._DETACHED_PROCESS and kw["creationflags"] & P._BREAKAWAY
        assert "start_new_session" not in kw
    else:
        assert kw["start_new_session"] is True and "creationflags" not in kw


def test_spawn_detached_on_windows_falls_back_when_the_job_forbids_breakaway(popen, monkeypatch):
    monkeypatch.setattr(P, "IS_WINDOWS", True)
    popen.refuse_breakaway = True
    assert SPAWN(["python"]) is True and not popen.calls[0]["creationflags"] & P._BREAKAWAY


def test_spawn_detached_reports_failure(monkeypatch):
    monkeypatch.setattr(P.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("x")))
    for windows in (False, True):
        monkeypatch.setattr(P, "IS_WINDOWS", windows)
        assert SPAWN(["missing"]) is False


# ---- CLI and doctor -------------------------------------------------------------------------------------------

def test_models_auto_flag_is_remembered(fake, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CONFIG", core.STATE_DIR / "config.json")
    monkeypatch.setattr(cli, "STATE", core.STATE_DIR)
    monkeypatch.setattr(cli, "prepare_state", lambda key: None)
    assert cli.main(["models", "--auto=off"]) == 0
    assert json.loads((core.STATE_DIR / "config.json").read_text())["model_discovery"]["auto"] is False
    assert not fake.calls and not D.auto_enabled()
    assert cli.main(["models", "--auto", "on"]) == 0 and D.auto_enabled()


def test_models_discover_prints_one_line_per_tool(fake, monkeypatch, capsys):
    monkeypatch.setattr(cli, "prepare_state", lambda key: None)
    fake.codex_list = (0, codex_json(("gpt-a", ["low"], "list")))
    assert cli.main(["models", "--discover", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Claude Code  no change" in out and "removed: gpt-b" in out and "Would save" in out
    assert not core.STATE_DIR.exists()


def test_models_refuses_while_a_check_runs(fake, monkeypatch, capsys):
    monkeypatch.setattr(cli, "prepare_state", lambda key: None)
    assert D.acquire_lock()
    assert cli.main(["models", "--discover"]) == 1 and "Another model check is running" in capsys.readouterr().out
    D.release_lock()


def test_discover_and_probe_cannot_be_combined(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["models", "--discover", "--probe"])
    assert exc.value.code == 2 and "cannot be combined" in capsys.readouterr().err


def test_doctor_shows_the_last_check(fake, monkeypatch, capsys, tmp_path):
    doctor.report_discovery({})
    assert "no check yet" in capsys.readouterr().out
    monkeypatch.chdir(tmp_path)
    D.main(["--scheduled"])
    doctor.report_discovery({})
    out = capsys.readouterr().out
    assert "Daily model check" in out and "on; last " in out and "no change" in out
    doctor.report_discovery({"model_discovery": {"auto": False}})
    assert "off (" in capsys.readouterr().out


# ---- tiers naming a model that is no longer available -----------------------------------------------------------

def retire(provider, *ids):
    path = D.local_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = local()
    for model_id in ids:
        data.setdefault(provider, {})[model_id] = {"selectable": False}
    path.write_text(json.dumps(data), encoding="utf-8")


def tier_decision(tier):
    return {"primary": tier, "effort": "high", "task": "code", "level": 1, "task_conf": 1.0, "notes": []}


CODEX_TARGETS = {"agent_template": "{model_}-{effort}", "tiers": {"deep": {
    "model": "gpt-a", "efforts": ["high"], "agent": "{model_}-{effort}",
    "text": "Spawn a subagent with agent_type `{agent}` ({model}, effort {effort})."}}}


def test_available_model_keeps_a_selectable_model(fake):
    assert core.available_model("codex", "gpt-a") == "gpt-a"


def test_removed_tier_model_falls_back_to_the_closest_selectable_one(fake):
    retire("codex", "gpt-a")
    assert core.available_model("codex", "gpt-a") == "gpt-b"
    d = tier_decision("deep")
    text, effort, model, agent = core.resolve_tier(d, CODEX_TARGETS, core.models_for("codex"), provider="codex")
    assert (model, agent, effort) == ("gpt-b", "gpt-b-high", "high") and "gpt-b-high" in text
    assert d["notes"] == ["gpt-a is not available, using gpt-b"]


def test_fallback_prefers_the_same_role_then_the_stronger_neighbour(fake):
    catalog = json.loads(json.dumps(CATALOG))
    catalog["codex"]["models"] += [{"id": "gpt-deep", "selectable": True, "role": "deep", "levels": ["high"]},
                                   {"id": "gpt-bal", "selectable": True, "role": "balanced", "levels": ["high"]}]
    (core.CFG_DIR / "models.json").write_text(json.dumps(catalog), encoding="utf-8")
    retire("codex", "gpt-a")
    assert core.available_model("codex", "gpt-a") == "gpt-bal"   # same role
    retire("codex", "gpt-bal")
    assert core.available_model("codex", "gpt-a") == "gpt-deep"  # fast and deep are equally near: the stronger


def test_cli_tier_checks_the_other_tools_models(fake):
    retire("codex", "gpt-a")
    targets = {"tiers": {"cli:codex": {"model": "gpt-a", "efforts": ["high"], "text": "Codex --model {model}"}}}
    text, _, model, _ = core.resolve_tier(tier_decision("cli:codex"), targets, core.models_for("claude"),
                                          provider="claude")
    assert model == "gpt-b" and text == "Codex --model gpt-b"


def test_literal_tier_agent_follows_the_substitutes_agent_tier(fake):
    catalog = json.loads(json.dumps(CATALOG))
    catalog["antigravity"]["models"][0]["agent_tier"] = "flash"
    catalog["antigravity"]["models"][1]["agent_tier"] = "pro"
    (core.CFG_DIR / "models.json").write_text(json.dumps(catalog), encoding="utf-8")
    retire("antigravity", "gem-pro")
    targets = {"agent_template": "gemini-{tier}-worker", "tiers": {"deep": {
        "model": "gem-pro", "efforts": ["high"], "agent": "gemini-pro-worker", "text": "Delegate to `{agent}` ({slug})."}}}
    text, _, model, agent = core.resolve_tier(tier_decision("deep"), targets, core.models_for("antigravity"),
                                              provider="antigravity")
    assert (model, agent, text) == ("gem-flash", "gemini-flash-worker", "Delegate to `gemini-flash-worker` (gem-flash-high).")


def test_literal_tier_agent_without_a_substitute_tier_uses_model_pick(fake):
    retire("antigravity", "gem-pro")
    targets = {"agent_template": "gemini-{tier}-worker", "model_pick": {"text": "Best-fit model: `{slug}`."},
               "tiers": {"deep": {"model": "gem-pro", "efforts": ["high"], "agent": "gemini-pro-worker",
                                  "text": "Delegate to `{agent}`."}}}
    text, _, model, agent = core.resolve_tier(tier_decision("deep"), targets, core.models_for("antigravity"),
                                              provider="antigravity")
    assert (model, agent, text) == ("gem-flash", None, "Best-fit model: `gem-flash-high`.")


def test_tier_agents_are_generated_on_the_substitute_model(fake):
    from trirouter import hub
    retire("claude", "sonnet")
    tiers = {"test": {"model": "sonnet", "efforts": ["high"], "agent": "test-worker-{effort}"}}
    assert list(hub._tier_variants("claude", tiers, "{model}-worker-{effort}")) == \
        [("test-worker-{effort}", "opus", "high", "test-worker")]
