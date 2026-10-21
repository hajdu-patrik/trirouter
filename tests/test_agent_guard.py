"""The session's model (Claude), the PreToolUse agent guard, the Claude model defaults writer and the locked log
append. Everything runs in temp folders."""
import io
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from trirouter import core, doctor, hooks, integrations, platforms as P, queue_state

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "LOG_FILE", tmp_path / "routing.jsonl")
    monkeypatch.setattr(hooks, "SUBAGENT_LOG", tmp_path / "subagents.jsonl")
    monkeypatch.setattr(hooks, "GUARD_LOG", tmp_path / "guard.jsonl")
    monkeypatch.setattr(hooks, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(hooks, "CLAUDE_SETTINGS", tmp_path / "claude-settings.json")
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    monkeypatch.setattr(core, "BACKEND", "local")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return tmp_path


def run(monkeypatch, capsys, event, payload, provider="claude"):
    """The hook's parsed stdout (a dict) or None."""
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8"))))
    assert hooks.main([provider, event]) == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def jsonl(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines()] if Path(path).exists() else []


def agent_call(prompt_id="p1", session="s", **tool_input):
    return {"session_id": session, "prompt_id": prompt_id, "tool_name": "Agent", "hook_event_name": "PreToolUse",
            "tool_input": {"description": "d", "prompt": "do it", **tool_input}}


# ---- 1. the session's model --------------------------------------------------------------------------------

@pytest.mark.parametrize("name, family", [
    ("opus", "opus"), ("claude-opus-5-5", "opus"), ("opus[1m]", "opus"), ("Claude Sonnet 5.5", "sonnet"),
    ("claude-haiku-4-5-20251001", "haiku"), ("fable", "fable"), ("opusplan", None), ("default", None),
    ("gpt-6-luna", None), ("<synthetic>", None), (None, None), ("", None)])
def test_claude_family(name, family):
    assert core.claude_family(name) == family


def test_same_model_compares_claude_families_only():
    assert core.same_model("claude-opus-5-5", "opus", "claude")
    assert core.same_model("opus", "opus", "claude-chat")
    assert not core.same_model("claude-opus-5-5", "sonnet", "claude")
    assert not core.same_model("claude-sonnet-4-6", "sonnet", "antigravity")  # another provider: exact ids only
    assert core.same_model("gpt-6-luna", "gpt-6-luna", "codex")
    assert not core.same_model(None, "opus", "claude")


def test_delegating_to_the_session_model_becomes_inline():
    prompt = "Refactor the entire codebase to hexagonal architecture"
    d, text, _, _ = core.route(prompt, "claude")
    assert d["target_agent"].startswith("opus-worker-")
    d, text, _, _ = core.route(prompt, "claude", session_model="claude-opus-5-5")
    assert d["primary"] == "deep" and "target_agent" not in d and "target_model" not in d
    assert "Answer directly in this session (it already runs opus); do not spawn a subagent." in text
    assert "Reasoning effort:" not in text  # the running session's effort cannot be switched
    assert d["route_model"] == "opus"


def test_explicit_override_on_the_session_model_is_inline_too():
    d, text, _, _ = core.route("#sonnet explain this", "claude", session_model="sonnet")
    assert "target_agent" not in d and "it already runs sonnet" in text


def test_test_tier_always_delegates():
    d, text, _, _ = core.route("Write unit tests with pytest and mocks for the parser class, with coverage", "claude",
                         session_model="sonnet")
    assert d["primary"] == "test" and d["target_agent"].startswith("test-worker-") and "`test-worker-" in text


def test_in_session_tier_reports_the_session_family():
    d, text, _, _ = core.route("What is the capital of France?", "claude", session_model="claude-opus-5-5")
    assert d["primary"] == "main" and d["route_model"] == "opus"
    assert d["route_effort"] in core.models_for("claude")["opus"]["levels"]
    assert " model=opus effort=" in text.split(". ", 1)[0] + "."
    d, _, _, _ = core.route("What is the capital of France?", "claude")
    assert d["route_model"] == "sonnet"  # nothing known: machine_default_model, as before


def test_machine_route_session_model():
    targets = {"machine_default_model": "sonnet"}
    models = {"sonnet": {"levels": ["low", "medium"]}, "opus": {"levels": ["high", "max"]}}
    assert core.machine_route(targets, models, "claude", None, "low", "claude-opus-5-5") == ("opus", "high")
    assert core.machine_route(targets, models, "claude", None, "max", "haiku") == ("sonnet", "medium")  # not in pool
    assert core.machine_route(targets, models, "claude", "sonnet", "low", "opus") == ("sonnet", "low")  # tier model wins


def test_session_start_and_model_switch_are_recorded(tmp_path, monkeypatch, capsys):
    assert run(monkeypatch, capsys, "SessionStart", {"session_id": "s", "model": "claude-sonnet-5-5"}) is None
    assert queue_state.session_model(tmp_path, "claude", "s") == "sonnet"
    run(monkeypatch, capsys, "PostModelSwitch", {"session_id": "s", "from_model": "sonnet", "to_model": "opus[1m]"})
    assert queue_state.session_model(tmp_path, "claude", "s") == "opus"
    run(monkeypatch, capsys, "SessionStart", {"session_id": "s", "source": "resume"})  # no model: kept
    assert queue_state.session_model(tmp_path, "claude", "s") == "opus"
    run(monkeypatch, capsys, "PostModelSwitch", {"session_id": "s", "to_model": "opusplan"})  # names no family
    assert queue_state.session_model(tmp_path, "claude", "s") is None


def test_prompt_hook_routes_with_the_recorded_family(tmp_path, monkeypatch, capsys):
    run(monkeypatch, capsys, "PostModelSwitch", {"session_id": "s", "to_model": "claude-opus-5-5"})
    out = run(monkeypatch, capsys, "UserPromptSubmit",
              {"session_id": "s", "prompt": "Refactor the entire codebase to hexagonal architecture", "cwd": "/w"})
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "it already runs opus" in text and "-worker-" not in text
    entry = jsonl(tmp_path / "routing.jsonl")[-1]
    assert entry["session_model"] == "opus" and "target_agent" not in entry


def test_session_model_fallbacks(tmp_path):
    transcript = tmp_path / "t.jsonl"
    rows = [{"type": "assistant", "message": {"model": "claude-haiku-5-5"}},
            {"type": "assistant", "isSidechain": True, "message": {"model": "claude-opus-5-5"}},
            {"type": "assistant", "message": {"model": "<synthetic>"}},
            {"type": "user", "message": {"content": "hi"}}]
    transcript.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    payload = {"session_id": "x", "transcript_path": str(transcript)}
    assert hooks.claude_session_model(payload) == "haiku"           # newest main-session assistant message
    hooks.CLAUDE_SETTINGS.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    assert hooks.claude_session_model({"session_id": "x", "transcript_path": str(tmp_path / "gone.jsonl")}) == "opus"
    queue_state.remember_model(tmp_path, "claude", "x", "fable")
    assert hooks.claude_session_model(payload) == "fable"           # a recorded switch beats both
    hooks.CLAUDE_SETTINGS.write_text("{not json", encoding="utf-8")
    assert hooks.claude_session_model({"session_id": "y"}) is None


def test_session_model_store_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(queue_state.MODELS, "limit", 3)
    for i in range(5):
        queue_state.remember_model(tmp_path, "claude", f"s{i}", "opus")
    data = json.loads((tmp_path / "state" / "session_models.json").read_text(encoding="utf-8"))
    assert sorted(data) == ["claude:s2", "claude:s3", "claude:s4"]


# ---- 2. the agent guard ------------------------------------------------------------------------------------

def route_prompt(monkeypatch, capsys, prompt, prompt_id="p1", session="s"):
    run(monkeypatch, capsys, "UserPromptSubmit", {"session_id": session, "prompt_id": prompt_id, "prompt": prompt,
                                                 "cwd": "/w"})


def test_guard_denies_once_over_the_budget_then_lets_the_retry_through(tmp_path, monkeypatch, capsys):
    route_prompt(monkeypatch, capsys, "What is the capital of France?")  # inline, no extra agents: budget 0
    out = run(monkeypatch, capsys, "PreToolUse", agent_call(subagent_type="Explore"))
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse" and spec["permissionDecision"] == "deny"
    assert "allowed 0 subagent(s)" in spec["permissionDecisionReason"]
    assert "call the tool again" in spec["permissionDecisionReason"]
    assert run(monkeypatch, capsys, "PreToolUse", agent_call(subagent_type="Explore")) is None   # the retry
    assert run(monkeypatch, capsys, "PreToolUse", agent_call(subagent_type="Explore")) is None   # warned once only
    logged = jsonl(tmp_path / "guard.jsonl")
    assert [e["action"] for e in logged] == ["deny"]
    assert logged[0]["prompt_id"] == "p1" and logged[0]["agent_type"] == "Explore" and logged[0]["session"] != "s"


def test_guard_aligns_a_general_purpose_call_with_the_routed_worker(tmp_path, monkeypatch, capsys):
    route_prompt(monkeypatch, capsys, "#opus explain this")  # delegates to opus-worker-*: budget 1
    call = agent_call(subagent_type="general-purpose", run_in_background=False)
    out = run(monkeypatch, capsys, "PreToolUse", call)
    spec = out["hookSpecificOutput"]
    assert "permissionDecision" not in spec  # the permission rules still apply
    assert spec["updatedInput"] == {**call["tool_input"], "model": "opus"}  # every original field copied
    out = run(monkeypatch, capsys, "PreToolUse", agent_call(subagent_type="general-purpose"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"         # a second one is over the budget
    assert run(monkeypatch, capsys, "PreToolUse", agent_call()) is not None  # retry: allowed, model still aligned
    assert [e["action"] for e in jsonl(tmp_path / "guard.jsonl")] == ["model", "deny", "model"]


@pytest.mark.parametrize("tool_input", [{"subagent_type": "opus-worker-high"}, {"subagent_type": "Explore"},
                                        {"subagent_type": "general-purpose", "model": "haiku"}])
def test_guard_leaves_named_agents_and_explicit_models_alone(monkeypatch, capsys, tool_input):
    route_prompt(monkeypatch, capsys, "#opus explain this")
    assert run(monkeypatch, capsys, "PreToolUse", agent_call(**tool_input)) is None


def test_guard_ignores_subagents_unknown_prompts_other_tools_and_codex(tmp_path, monkeypatch, capsys):
    route_prompt(monkeypatch, capsys, "What is the capital of France?")
    assert run(monkeypatch, capsys, "PreToolUse", {**agent_call(), "agent_id": "a1"}) is None  # inside a subagent
    assert run(monkeypatch, capsys, "PreToolUse", agent_call(prompt_id="unknown")) is None
    assert run(monkeypatch, capsys, "PreToolUse", {**agent_call(), "tool_name": "TaskStop"}) is None
    assert run(monkeypatch, capsys, "PreToolUse", agent_call(), provider="codex") is None
    assert run(monkeypatch, capsys, "PreToolUse", {"prompt_id": "p1"}) is None  # no tool_name
    assert not (tmp_path / "guard.jsonl").exists()
    # the subagent's call was not counted: the main session's first call is still the one denied
    assert run(monkeypatch, capsys, "PreToolUse", agent_call())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_budget_counts_extra_agents():
    assert hooks.agent_budget({"extra_agents": 2, "target_agent": "opus-worker-high"}) == 3
    assert hooks.agent_budget({"extra_agents": 1}) == 1
    assert hooks.agent_budget({}) == 0


def test_guard_can_be_switched_off(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.json").write_text(json.dumps({"guard": {"agents": False}}), encoding="utf-8")
    route_prompt(monkeypatch, capsys, "What is the capital of France?")
    assert run(monkeypatch, capsys, "PreToolUse", agent_call()) is None


def test_guard_never_raises(monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(queue_state, "update_prompt", boom)
    assert hooks.agent_guard(agent_call()) is None
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"\xff not json")))
    assert hooks.main(["claude", "PreToolUse"]) == 0 and capsys.readouterr().out == ""


def test_prompt_budget_store_is_bounded_and_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(queue_state.PROMPTS, "limit", 2)
    for i in range(4):
        queue_state.remember_prompt(tmp_path, f"p{i}", {"budget": 0})
    assert queue_state.update_prompt(tmp_path, "p0", lambda e: "seen") == (None, None)
    assert queue_state.update_prompt(tmp_path, "p3", lambda e: "seen")[0] == "seen"
    monkeypatch.setattr(queue_state.PROMPTS, "ttl", 0)
    assert queue_state.update_prompt(tmp_path, "p3", lambda e: "seen") == (None, None)


def test_doctor_guard_summary():
    import datetime
    now = datetime.datetime(2026, 10, 16, 12, 0, 0)
    rows = [{"ts": "2026-10-15T10:00:00", "action": "deny", "prompt_id": "a"},
            {"ts": "2026-10-16T10:00:00", "action": "model", "prompt_id": "a"},
            {"ts": "2026-10-16T11:00:00", "action": "deny", "prompt_id": "b"},
            {"ts": "2026-09-01T11:00:00", "action": "deny", "prompt_id": "old"}, "garbage"]
    assert doctor.guard_summary(rows, days=7, now=now) == {"deny": 2, "model": 1, "prompts": 2}


# ---- 3. the Claude model defaults --------------------------------------------------------------------------

def test_claude_model_settings_write_dry_run_and_off(tmp_path, capsys):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"env": {"OTHER": "1"}, "hooks": {}}), encoding="utf-8")
    integrations.claude_model_settings(integrations.Writer(False), "opus", "sonnet", path=path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"env": {"OTHER": "1"}, "hooks": {}}  # dry run
    w = integrations.Writer(True)
    integrations.claude_model_settings(w, "opus", "sonnet", path=path)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert cfg["model"] == "opus" and cfg["env"] == {"OTHER": "1", "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}
    assert json.loads((tmp_path / "settings.json.bak").read_text(encoding="utf-8")) == {"env": {"OTHER": "1"}, "hooks": {}}
    integrations.claude_model_settings(w, None, "off", path=path)       # None leaves the main model
    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert cfg["model"] == "opus" and cfg["env"] == {"OTHER": "1"}
    integrations.claude_model_settings(w, "off", None, path=path)
    assert "model" not in json.loads(path.read_text(encoding="utf-8"))
    changes = w.changes
    integrations.claude_model_settings(w, "off", "off", path=path)      # idempotent
    assert w.changes == changes


@pytest.mark.parametrize("bad", ["gpt-6-luna", "claude-opus-5-5", "opusplan", "OPUSX"])
def test_claude_model_settings_refuses_anything_but_a_family(tmp_path, bad):
    path = tmp_path / "settings.json"
    with pytest.raises(ValueError):
        integrations.claude_model_settings(integrations.Writer(True), bad, None, path=path)
    assert not path.exists()


def test_empty_env_is_removed(tmp_path):
    cfg = integrations.apply_claude_models({"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}}, None, "off")
    assert cfg == {}


def test_remembered_models_survive_setup_and_leave_with_uninstall(tmp_path, monkeypatch):
    paths = {k: tmp_path / Path(v).relative_to(P.HOME) for k, v in P.PATHS.items()}
    monkeypatch.setattr(P, "PATHS", paths)
    monkeypatch.setattr(P, "claude_desktop_config", lambda: tmp_path / "claude_desktop_config.json")
    (tmp_path / "config.json").write_text(json.dumps({"claude": {"main_model": "opus", "subagent_model": "nope"}}),
                                          encoding="utf-8")
    assert integrations.remembered_claude_models() == ("opus", None)  # an invalid value is ignored
    integrations.install(providers=("claude",), apply=True, launcher=False)
    s = json.loads(paths["claude_settings"].read_text(encoding="utf-8"))
    assert s["model"] == "opus" and "env" not in s and "PreToolUse" in s["hooks"]
    assert integrations.install(providers=("claude",), apply=True, launcher=False) == 0  # idempotent
    integrations.install(providers=("claude",), apply=True, uninstall=True, launcher=False)
    s = json.loads(paths["claude_settings"].read_text(encoding="utf-8"))
    assert "model" not in s and "hooks" not in s


def test_remembered_off_is_not_reapplied(tmp_path):
    cfg = {"model": "haiku", "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}}
    integrations._reapply_claude_models(cfg, ("off", "off"))
    assert cfg == {"model": "haiku", "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}}  # set by hand later: kept
    integrations._forget_claude_models(cfg, ("opus", "haiku"))
    assert cfg == {"model": "haiku"}  # only what still equals the remembered value is removed


# ---- 4. the locked log append ------------------------------------------------------------------------------

WRITER = """
import sys
sys.path.insert(0, sys.argv[1])
from trirouter import platforms
tag, path = sys.argv[2], sys.argv[3]
for i in range(int(sys.argv[4])):
    platforms.append_line(path, '{"w": "%s", "i": %d, "pad": "%s"}' % (tag, i, tag * 3000))
"""


def check_lines(path, writers, per_writer):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    rows = [json.loads(l) for l in lines]  # a torn or interleaved line would not parse
    assert len(rows) == writers * per_writer
    for w in range(writers):
        assert sorted(r["i"] for r in rows if r["w"] == str(w)) == list(range(per_writer))


def test_append_line_concurrent_processes(tmp_path):
    path = tmp_path / "logs" / "shared.jsonl"
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(ROOT), str(w), str(path), "150"])
             for w in range(4)]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    check_lines(path, 4, 150)


def test_append_line_concurrent_threads(tmp_path):
    path = tmp_path / "shared.jsonl"

    def write(tag):
        for i in range(200):
            P.append_line(path, json.dumps({"w": tag, "i": i, "pad": tag * 3000}))
    threads = [threading.Thread(target=write, args=(str(w),)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check_lines(path, 4, 200)


def test_append_line_never_raises(tmp_path):
    assert P.append_line(tmp_path, "x") is False  # a folder, not a file
    assert P.append_line(tmp_path / "ok.jsonl", "line\n") is True
    assert (tmp_path / "ok.jsonl").read_text(encoding="utf-8") == "line\n"


def test_append_line_writes_through_a_held_lock_after_the_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_lock_file", lambda f, deadline: False)  # someone holds the lock past the wait
    t0 = time.monotonic()
    assert P.append_line(tmp_path / "a.jsonl", "x") is True
    assert time.monotonic() - t0 < 1 and (tmp_path / "a.jsonl").read_text(encoding="utf-8") == "x\n"


def test_wrong_type_json_in_a_state_file_counts_as_empty(isolated, monkeypatch, capsys):
    """Valid JSON of the wrong type (a list config.json, a dict seen file, a string settings.json) is as harmless as
    an unreadable file: the hook still routes."""
    (isolated / "config.json").write_text("[1, 2]", encoding="utf-8")
    (isolated / "models.local.json").write_text("[]", encoding="utf-8")
    hooks.SEEN_FILE.write_text('{"turn": 1}', encoding="utf-8")
    hooks.CLAUDE_SETTINGS.write_text('"opus"', encoding="utf-8")
    assert P.read_json(isolated / "config.json", 5, kind=list) == [1, 2]
    assert core.user_config() == {} and core.model_overrides() == {} and hooks._settings_family() is None
    assert hooks._first_time("t1") and not hooks._first_time("t1")
    out = run(monkeypatch, capsys, "UserPromptSubmit", {"session_id": "s", "prompt": "Write a function that sums a list"})
    assert "[router]" in out["hookSpecificOutput"]["additionalContext"]
