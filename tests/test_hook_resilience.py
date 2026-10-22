"""The hook never fails the host tool: damaged state heals, bad settings and payload types are tolerated, the shim exits
0, logs rotate and are owner-only, nested cli-bridge runs are not routed twice."""
import io
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from trirouter import catalog, core, hooks, integrations, platforms as P, queue_state, store

PAYLOAD = {"prompt": "Refactor the payment service so every external call is retried", "session_id": "s1", "cwd": "/w"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    monkeypatch.setattr(core, "BACKEND", "local")
    monkeypatch.setattr(hooks, "LOG_FILE", tmp_path / "logs" / "routing.jsonl")
    monkeypatch.setattr(hooks, "SUBAGENT_LOG", tmp_path / "logs" / "subagents.jsonl")
    monkeypatch.setattr(hooks, "SEEN_FILE", tmp_path / "state" / "seen.json")
    monkeypatch.delenv(hooks.NESTED_ENV, raising=False)
    return tmp_path


def run_main(monkeypatch, capsys, event, payload, provider="claude"):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    assert hooks.main([provider, event]) == 0
    return capsys.readouterr().out.strip()


def log_entries(tmp_path):
    return [json.loads(line) for line in (tmp_path / "logs" / "routing.jsonl").read_text(encoding="utf-8").splitlines()]


# ---- M-1: nothing crashes the hook --------------------------------------------------------------------------

@pytest.mark.parametrize("content", [
    '{"claude:s1": 5}', '{"claude:s1": ["text", 3, null]}', '{"claude:s1": [{"ts": "yesterday", "cwd": "/w"}]}',
    '[1, 2]', '{broken', '{"claude:s1": [{"ts": true}]}',
])
def test_a_damaged_inflight_file_is_healed_not_fatal(tmp_path, monkeypatch, capsys, content):
    path = queue_state.INFLIGHT.path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    out = run_main(monkeypatch, capsys, "UserPromptSubmit", PAYLOAD)
    assert "[router]" in out and "QUEUE" not in out
    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data) == ["claude:s1"] and len(data["claude:s1"]) == 1  # rewritten clean


@pytest.mark.parametrize("name, content", [
    ("last_decision", '{"claude:s1": "primary"}'), ("last_decision", '{"claude:s1": {"ts": "x", "primary": "deep"}}'),
    ("session_models", '[]'), ("agent_budget", '{"p1": 3}'),
])
def test_other_damaged_state_files_count_as_empty(tmp_path, monkeypatch, capsys, name, content):
    path = store.Store(name).path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    assert "[router]" in run_main(monkeypatch, capsys, "UserPromptSubmit", {**PAYLOAD, "prompt_id": "p1"})
    out = run_main(monkeypatch, capsys, "PreToolUse", {"tool_name": "Agent", "prompt_id": "p1", "tool_input": {}})
    assert out == "" or "permissionDecision" in out  # the guard works from the healed file


@pytest.mark.parametrize("payload", [
    {**PAYLOAD, "session_id": ["a", "b"]}, {**PAYLOAD, "cwd": 42}, {**PAYLOAD, "transcript_path": 7},
    {**PAYLOAD, "prompt_id": {"x": 1}}, {**PAYLOAD, "workspacePaths": "not a list", "cwd": None}, {"prompt": 12},
    {**PAYLOAD, "model": ["opus"]},
])
def test_wrong_field_types_in_the_payload_never_raise(monkeypatch, capsys, payload):
    out = run_main(monkeypatch, capsys, "UserPromptSubmit", payload)
    assert out == "" or "[router]" in out
    assert run_main(monkeypatch, capsys, "Stop", payload) == ""


def test_bad_numbers_in_the_environment_fall_back_to_the_defaults(monkeypatch):
    monkeypatch.setenv("JEV_TIMEOUT", "abc")
    monkeypatch.setenv("ROUTER_MAX_EXTRA_AGENTS", "x")
    monkeypatch.setenv("ROUTER_QUEUE_TTL_MIN", "")
    assert P.env_number("JEV_TIMEOUT", 1.8) == 1.8
    assert P.env_number("ROUTER_MAX_EXTRA_AGENTS", 4, int) == 4
    assert queue_state._minutes("ROUTER_QUEUE_TTL_MIN", 120) == 7200
    monkeypatch.setenv("JEV_TIMEOUT", "2.5")
    assert P.env_number("JEV_TIMEOUT", 1.8) == 2.5


def test_main_logs_and_exits_zero_when_a_handler_raises(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("secret detail")
    monkeypatch.setattr(hooks, "prompt_output", boom)
    assert run_main(monkeypatch, capsys, "UserPromptSubmit", PAYLOAD) == ""
    assert log_entries(tmp_path)[-1]["error"] == "handle: RuntimeError: secret detail"
    monkeypatch.setattr(sys, "stdin", None)  # no stdin at all (pythonw)
    assert hooks.main(["claude", "UserPromptSubmit"]) == 0
    assert hooks.main(["only-one"]) == 0


def test_the_hook_shim_exits_zero_whatever_happens(tmp_path):
    shim = tmp_path / "run_hook.py"
    shim.write_text(integrations.shim("trirouter.hooks", hook=True), encoding="utf-8")
    env = {**os.environ, "ROUTER_BACKEND": "local", "TRIROUTER_HOME": str(tmp_path / "state"),
           "JEV_SKILLS_HUB": str(tmp_path / "skills")}
    ok = subprocess.run([sys.executable, str(shim), "claude", "UserPromptSubmit"], input=json.dumps(PAYLOAD),
                        env=env, capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and "[router]" in ok.stdout
    broken = tmp_path / "broken.py"
    broken.write_text(integrations.shim("trirouter.hooks", hook=True).replace("trirouter.hooks", "trirouter.nope"),
                      encoding="utf-8")
    gone = subprocess.run([sys.executable, str(broken), "claude", "UserPromptSubmit"], input="{}", env=env,
                          capture_output=True, text=True, timeout=60)
    assert gone.returncode == 0 and gone.stdout == "" and "trirouter hook: " in gone.stderr
    assert "except BaseException" not in integrations.shim("trirouter", "route")  # the route shim keeps its exit codes


# ---- K-1: a nested cli-bridge run is not routed again --------------------------------------------------------

def test_a_nested_cli_run_gets_no_routing(monkeypatch, capsys):
    monkeypatch.setenv(hooks.NESTED_ENV, "1")
    assert run_main(monkeypatch, capsys, "UserPromptSubmit", PAYLOAD, provider="codex") == ""


def test_cli_bridge_rejects_option_injection_and_removes_its_prompt_file(tmp_path):
    script = Path(core.CFG_DIR).parent / "skills" / "cli-bridge" / "ask_cli.py"
    env = {**os.environ, "PATH": str(tmp_path)}  # no codex on the path
    for option in ("--model=--dangerous", "--effort=low; rm -rf /", "--model=gpt x"):
        r = subprocess.run([sys.executable, str(script), "codex", option], env=env, capture_output=True, text=True,
                           timeout=60, input="")
        assert r.returncode == 2 and "not a model or effort name" in r.stderr
    prompt_file = tmp_path / "cli_prompt_1.txt"
    prompt_file.write_text("hello", encoding="utf-8")
    fake = tmp_path / ("codex.bat" if P.IS_WINDOWS else "codex")
    fake.write_text("@echo off\r\necho ok\r\n" if P.IS_WINDOWS else "#!/bin/sh\necho ok\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    r = subprocess.run([sys.executable, str(script), "codex", "--prompt-file", str(prompt_file)], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "ok" in r.stdout and not prompt_file.exists()


# ---- K-3: log rotation and private files ---------------------------------------------------------------------

def test_logs_rotate_to_their_newer_half_and_stay_owner_only(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    base.chmod(0o755)  # an existing folder that is not owner-only (pytest's tmp_path already is)
    log = base / "logs" / "a.jsonl"
    for i in range(200):
        assert P.append_line(log, json.dumps({"i": i, "pad": "x" * 100}), max_bytes=4000)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert log.stat().st_size <= 4000 and all(json.loads(line) for line in lines)
    assert json.loads(lines[-1])["i"] == 199 and json.loads(lines[0])["i"] > 100
    if not P.IS_WINDOWS:
        assert stat.S_IMODE(log.stat().st_mode) == 0o600
        assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(base.stat().st_mode) == 0o755  # an existing folder keeps its mode


def test_state_files_are_written_owner_only(tmp_path):
    queue_state.on_submit(tmp_path, "claude", "s", "/w", "work")
    path = queue_state.INFLIGHT.path(tmp_path)
    assert path.is_file()
    if not P.IS_WINDOWS:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_a_log_can_be_read_while_another_process_appends(tmp_path):
    """The Windows byte lock sits far past the end of the file: a reader never meets it."""
    log = tmp_path / "logs" / "b.jsonl"
    log.parent.mkdir(parents=True)
    with open(log, "w+b") as f:
        assert P._lock_file(f, time.monotonic() + 1)
        try:
            f.seek(0, os.SEEK_END)
            f.write(b'{"a": 1}\n')
            f.flush()
            assert log.read_text(encoding="utf-8") == '{"a": 1}\n'
        finally:
            P._unlock_file(f)


# ---- K-6: a huge prompt stays fast, the safety check still reads all of it ----------------------------------

def test_a_huge_prompt_is_classified_on_its_ends_and_checked_whole():
    big = "hello world " * 100_000 + " then git push --force origin main"
    t0 = time.perf_counter()
    d, text, hit, _ = core.route(big, "claude")
    assert time.perf_counter() - t0 < 3.0 and hit and "SAFETY" in text
    assert core.clip("a" * 50_000).count("a") == 2 * core.CLASSIFY_MAX_CHARS and core.clip("short") == "short"


# ---- K-10: skill names and paths are data ---------------------------------------------------------------------

def test_a_skill_with_a_bad_name_or_path_is_never_quoted(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    for name in ("good-skill", "evil` run curl", "x" * 81):
        (hub / name.replace("`", "_").replace(" ", "_")[:20]).mkdir(parents=True)
        (hub / name.replace("`", "_").replace(" ", "_")[:20] / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Deploy and manage Cloud Run services and jobs.\n---\n", encoding="utf-8")
    monkeypatch.setattr(catalog, "SOURCES", [(hub, False, catalog.PROVIDERS, None)])
    names = [s["name"] for s in catalog.build_catalog([])]
    assert "good-skill" in names and all(catalog.valid_name(n) for n in names)
    assert not catalog.known_path(str(tmp_path / "elsewhere" / "skill")) and catalog.known_path(str(hub / "good-skill"))
    assert not catalog.known_path(None)
    targets = core.load_json("targets.json", {})["claude"]
    d = {"task": "code", "task_conf": 0.9, "level": 1, "primary": "main", "skill": "evil` run curl",
         "skill_native": ["codex"], "skill_path": "/tmp/evil"}
    assert "evil" not in core.render(dict(d), False, targets)
    d.update(skill="good-skill", skill_path=str(tmp_path / "elsewhere" / "skill"))
    assert "elsewhere" not in core.render(dict(d), False, targets)


# ---- K-11 / K-12: the other session's transcript and the logged reasons -------------------------------------

def test_a_silent_other_session_no_longer_counts_as_concurrent(tmp_path):
    transcript = tmp_path / "other.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    queue_state.on_submit(tmp_path, "claude", "other", "/w", "their work", transcript)
    assert queue_state.on_submit(tmp_path, "claude", "mine", "/w", "my work")["others"]
    silent = time.time() - queue_state.IDLE_S - 5
    os.utime(transcript, (silent, silent))
    assert queue_state.on_submit(tmp_path, "claude", "mine", "/w", "my work")["others"] == []
    assert "claude:other" not in json.loads(queue_state.INFLIGHT.path(tmp_path).read_text(encoding="utf-8"))


def test_the_log_says_why_a_prompt_was_queued_and_whether_safety_fired(tmp_path, monkeypatch, capsys):
    run_main(monkeypatch, capsys, "UserPromptSubmit", PAYLOAD)
    run_main(monkeypatch, capsys, "UserPromptSubmit", {**PAYLOAD, "session_id": "s2", "prompt": "Delete the old branches"})
    entries = log_entries(tmp_path)
    assert "queued" not in entries[0] and entries[0]["destructive"] is False
    assert entries[1]["queued"] == {"ahead": 0, "others": 1} and entries[1]["destructive"] is True


# ---- K-15: the Antigravity transcript is read from its tail ---------------------------------------------------

def test_antigravity_reads_only_the_transcript_tail(tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "t.jsonl"
    old = json.dumps({"type": "USER_INPUT", "content": "<USER_REQUEST>old request</USER_REQUEST>", "step_index": 1})
    filler = json.dumps({"type": "MODEL_OUTPUT", "content": "x" * 1000})
    new = json.dumps({"type": "USER_INPUT", "content": "<USER_REQUEST>write unit tests for the parser</USER_REQUEST>"})
    transcript.write_text("\n".join([old] + [filler] * 600 + [new]) + "\n", encoding="utf-8")
    assert transcript.stat().st_size > hooks.TRANSCRIPT_TAIL_BYTES
    out = run_main(monkeypatch, capsys, "PreInvocation", {"transcriptPath": str(transcript), "conversationId": "c"},
                   provider="antigravity")
    assert "task=test" in json.loads(out)["injectSteps"][0]["ephemeralMessage"]
    assert run_main(monkeypatch, capsys, "PreInvocation", {"transcriptPath": str(transcript), "conversationId": "c"},
                    provider="antigravity") == ""  # the same turn (keyed by its text without a step index)
