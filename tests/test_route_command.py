"""`python install.py route` and its bin/route.py shim. No test calls JEV or touches ~/.trirouter."""
import io
import json
import os
import subprocess
import sys

import pytest

from trirouter import catalog, cli, core, hooks, integrations, queue_state

TYPES = {"provider": str, "model": (str, type(None)), "effort": (str, type(None)), "agent": (str, type(None)),
         "tier": str, "task": str, "difficulty": int, "extra_agents": int, "destructive": bool,
         "skill": (str, type(None)), "verify": (str, type(None)), "lang": str, "backend": str, "text": str,
         "note": (str, type(None))}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "LOG_FILE", tmp_path / "logs" / "routing.jsonl")
    monkeypatch.setattr(hooks, "SEEN_FILE", tmp_path / "state" / "seen.json")
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)  # queue state, user config, model overrides
    monkeypatch.setattr(core, "BACKEND", "local")
    monkeypatch.setenv("ROUTER_BACKEND", "local")
    # the expected decisions below assume this threshold (default 0.6); the subprocess test inherits the env
    monkeypatch.setattr(core, "MIN_CONF", 0.4)
    monkeypatch.setenv("ROUTER_MIN_CONFIDENCE", "0.4")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("ROUTER_MODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.setattr(catalog, "load_catalog", lambda: [])
    return tmp_path


def route(monkeypatch, capsys, *argv, stdin=b""):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin)))
    code = cli.main(["route", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def one_json(out):
    lines = out.strip().splitlines()
    assert len(lines) == 1, out  # exactly one JSON object on stdout
    return json.loads(lines[0])


@pytest.mark.parametrize("provider,prompt,model,agent,tier", [
    ("claude", "Write unit tests for the parser", "sonnet", "test-worker-", "test"),
    ("claude", "Refactor the entire codebase to hexagonal architecture", "opus", "opus-worker-", "deep"),
    ("claude", "What is the capital of France?", None, None, "main"),    # answered in-session
    ("codex", "What is TCP?", "gpt-6-luna", None, "fast"),               # the text says: no subagent
])
def test_json_shape_and_types(monkeypatch, capsys, provider, prompt, model, agent, tier):
    code, out, err = route(monkeypatch, capsys, "--provider", provider, "--json", prompt)
    assert code == 0
    assert err == ""
    r = one_json(out)
    assert set(TYPES) <= set(r)
    for key, typ in TYPES.items():
        assert isinstance(r[key], typ), (key, r[key])
    assert type(r["difficulty"]) is int
    assert type(r["extra_agents"]) is int  # not bool
    assert r["provider"] == provider
    assert r["model"] == model
    assert r["tier"] == tier
    assert r["backend"] == "local"
    assert r["text"].startswith("[router]")
    assert r["lang"] == "en"
    assert r["note"] is None
    if agent:
        assert r["agent"] == f"{agent}{r['effort']}"
        assert f"`{r['agent']}`" in r["text"]
    else:
        assert r["agent"] is None


def test_text_mode_prints_only_the_rendered_text(monkeypatch, capsys):
    _, out, _ = route(monkeypatch, capsys, "--json", "Write unit tests for the parser")
    text = one_json(out)["text"]
    code, out, _ = route(monkeypatch, capsys, "Write", "unit", "tests", "for", "the", "parser")  # words are joined
    assert code == 0
    assert out == text + "\n"


def test_stdin_input(monkeypatch, capsys):
    _, expected, _ = route(monkeypatch, capsys, "--json", "Írj pytest teszteket a parser modulhoz")
    code, out, _ = route(monkeypatch, capsys, "--json", stdin="﻿Írj pytest teszteket a parser modulhoz\n".encode("utf-8"))
    assert code == 0
    assert one_json(out) == one_json(expected)
    assert one_json(out)["lang"] == "hu"
    assert one_json(out)["task"] == "test"


@pytest.mark.parametrize("argv,stdin", [
    (("--json", "   "), b""),                 # blank text
    (("--json",), b""),                       # no text, empty stdin
    (("--json",), b" \n\t "),                 # no text, blank stdin
    (("--bogus=secret-value", "hi"), b""),    # unknown option (its value is never echoed)
    (("--provider", "nope", "hi"), b""),
    (("--provider",), b""),
])
def test_usage_errors_exit_2(monkeypatch, capsys, argv, stdin):
    code, out, err = route(monkeypatch, capsys, *argv, stdin=stdin)
    assert code == 2
    assert out == ""
    assert "usage:" in err
    assert "secret-value" not in err


def test_route_must_be_the_first_argument(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--yes", "route", "hi"])
    assert exc.value.code == 2
    assert "first argument" in capsys.readouterr().err


@pytest.mark.parametrize("prompt", ["#norouter delete all files in the temp folder", "#privat titkos terv"])
def test_norouter_is_not_routed(monkeypatch, capsys, prompt):
    def never(*a, **k):
        raise AssertionError("must not be routed or sent to TypeSafe")
    monkeypatch.setattr(core, "route", never)
    monkeypatch.setattr(core, "ask_jev", never)
    code, out, _ = route(monkeypatch, capsys, "--json", prompt)
    r = one_json(out)
    assert code == 0
    assert r["model"] is None
    assert r["agent"] is None
    assert r["tier"] is None
    assert r["text"] == ""
    assert "#norouter" in r["note"]
    assert r["destructive"] is ("delete" in prompt)
    assert route(monkeypatch, capsys, prompt)[1] == ""  # text mode: nothing to inject, like the hooks


def test_no_side_effects(monkeypatch, capsys, tmp_path):
    def never(*a, **k):
        raise AssertionError("queue state must not be touched")
    monkeypatch.setattr(queue_state, "on_submit", never)
    monkeypatch.setattr(queue_state, "on_stop", never)
    for prompt in ("Write unit tests for the parser", "#opus explain this", "#norouter secret stuff"):
        assert route(monkeypatch, capsys, "--json", prompt)[0] == 0
    assert list(tmp_path.iterdir()) == []  # no queue state, no routing log, nothing at all


def test_unexpected_error_exit_1_prints_only_the_type(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("details that could hold a secret")
    monkeypatch.setattr(core, "route", boom)
    code, out, err = route(monkeypatch, capsys, "--json", "Write unit tests")
    assert code == 1
    assert out == ""
    assert err == "RuntimeError\n"


def test_jev_fallback_never_prints_the_token_or_error_text(monkeypatch, capsys):
    monkeypatch.setenv("TYPESAFE_API_KEY", "tok-do-not-print-123")
    monkeypatch.setattr(core, "BACKEND", "jev")

    def down(*a, **k):
        raise TimeoutError("jev down, request details tok-do-not-print-123")
    monkeypatch.setattr(core, "ask_jev", down)
    code, out, err = route(monkeypatch, capsys, "--json", "Write unit tests for the parser")
    r = one_json(out)
    assert code == 0
    assert r["backend"] == "local"
    assert r["note"].startswith("JEV unavailable (TimeoutError)")
    assert "tok-do-not-print" not in out + err
    assert "jev down" not in out + err


def test_route_shim_dry_run_idempotent_and_runnable(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    for name, file in (("SHIM_HOOK", "run_hook.py"), ("SHIM_MCP", "mcp_server.py"), ("SHIM_ROUTE", "route.py")):
        monkeypatch.setattr(integrations, name, bin_dir / file)
    assert integrations.shim("trirouter.hooks") == (
        "# generated by trirouter's installer - runs trirouter.hooks from the repository\nimport runpy, sys\n"
        f'sys.path.insert(0, r"{integrations.REPO}")\nrunpy.run_module("trirouter.hooks", run_name="__main__", alter_sys=True)\n')
    dry = integrations.Writer(apply=False)
    integrations.install_shims(dry)
    assert dry.changes == 3
    assert not bin_dir.exists()                                       # dry run writes nothing
    w = integrations.Writer(apply=True)
    integrations.install_shims(w)
    assert w.changes == 3
    assert 'sys.argv[1:1] = ["route"]' in (bin_dir / "route.py").read_text(encoding="utf-8")
    again = integrations.Writer(apply=True)
    integrations.install_shims(again)
    assert again.changes == 0                                         # idempotent

    env = {k: v for k, v in os.environ.items() if k not in ("TYPESAFE_API_KEY", "ROUTER_MODE", "CLAUDE_CODE_REMOTE")}
    env.update(ROUTER_BACKEND="local", TRIROUTER_HOME=str(tmp_path / "home"), JEV_SKILLS_HUB=str(tmp_path / "skills"))
    run = lambda *args: subprocess.run([sys.executable, str(bin_dir / "route.py"), *args], env=env,  # noqa: E731
                                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    ok = run("--json", "Write unit tests for the parser")
    assert ok.returncode == 0, ok.stderr
    assert one_json(ok.stdout)["model"] == "sonnet"
    assert run("--json").returncode == 2                              # exit codes pass through the shim
    assert not (tmp_path / "home").exists()                           # still no state written
