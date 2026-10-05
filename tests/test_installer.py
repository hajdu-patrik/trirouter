"""Tests for the queue protection, user config and the cross-platform installer pieces."""
import io
import json
import os
import sys
from pathlib import Path

import pytest

from jev_router import core, hooks as run_hook, integrations as install_hooks, platforms as P, queue_state


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(run_hook, "LOG_FILE", tmp_path / "routing.jsonl")
    monkeypatch.setattr(run_hook, "SUBAGENT_LOG", tmp_path / "subagents.jsonl")
    monkeypatch.setattr(run_hook, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    monkeypatch.setattr(core, "BACKEND", "local")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return tmp_path


def hook(monkeypatch, capsys, provider, event, payload):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8"))))
    assert run_hook.main([provider, event]) == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def ctx(out):
    return out["hookSpecificOutput"]["additionalContext"] if out else ""


def test_queue_same_session(monkeypatch, capsys):
    first = hook(monkeypatch, capsys, "claude", "UserPromptSubmit", {"prompt": "Refactor the parser module", "session_id": "s1", "cwd": "/w"})
    assert "QUEUE" not in ctx(first)
    second = hook(monkeypatch, capsys, "claude", "UserPromptSubmit", {"prompt": "Also add a README", "session_id": "s1", "cwd": "/w"})
    assert "QUEUE: 1 earlier request" in ctx(second)
    assert "Refactor the parser module" in ctx(second)
    assert hook(monkeypatch, capsys, "claude", "Stop", {"session_id": "s1"}) is None
    third = hook(monkeypatch, capsys, "claude", "UserPromptSubmit", {"prompt": "What is TCP?", "session_id": "s1", "cwd": "/w"})
    assert "QUEUE" not in ctx(third)


def test_queue_other_session_same_folder(monkeypatch, capsys):
    hook(monkeypatch, capsys, "codex", "UserPromptSubmit", {"prompt": "Migrate the database schema", "session_id": "a", "cwd": "/repo"})
    out = hook(monkeypatch, capsys, "claude", "UserPromptSubmit", {"prompt": "Fix the login bug", "session_id": "b", "cwd": "/repo"})
    assert "CONCURRENCY: another codex session" in ctx(out)
    out = hook(monkeypatch, capsys, "claude", "UserPromptSubmit", {"prompt": "Fix the login bug", "session_id": "c", "cwd": "/other"})
    assert "CONCURRENCY" not in ctx(out)


def test_queue_private_prompt_still_protected_but_not_routed(monkeypatch, capsys):
    hook(monkeypatch, capsys, "claude", "UserPromptSubmit", {"prompt": "Build the report", "session_id": "p", "cwd": "/w"})
    out = hook(monkeypatch, capsys, "claude", "UserPromptSubmit", {"prompt": "#privat secret stuff", "session_id": "p", "cwd": "/w"})
    assert ctx(out).startswith("QUEUE")
    assert "[router]" not in ctx(out)


def test_queue_stale_entries_expire(tmp_path, monkeypatch):
    monkeypatch.setattr(queue_state, "TTL_S", 0)
    queue_state.on_submit(tmp_path, "claude", "s", "/w", "old")
    assert queue_state.on_submit(tmp_path, "claude", "s", "/w", "new")["ahead"] == []


def test_antigravity_stop_prints_json(monkeypatch, capsys):
    assert hook(monkeypatch, capsys, "antigravity", "Stop", {"conversationId": "x", "fullyIdle": True}) == {}


def test_jev_token_from_config_and_env(tmp_path, monkeypatch):
    assert core.jev_key() is None
    assert not core.use_jev()
    (tmp_path / "config.json").write_text(json.dumps({"typesafe_api_key": "tok"}), encoding="utf-8")
    monkeypatch.setattr(core, "BACKEND", "auto")
    assert core.jev_key() == "tok"
    assert core.use_jev()
    monkeypatch.setenv("TYPESAFE_API_KEY", "env")
    assert core.jev_key() == "env"


def test_model_overrides_per_account(tmp_path):
    assert "gpt-6-luna" in core.models_for("codex")
    (tmp_path / "models.local.json").write_text(json.dumps({"codex": {"gpt-6-luna": {"selectable": False},
                                                                      "gpt-6-sol": {"selectable": True}}}), encoding="utf-8")
    models = core.models_for("codex")
    assert "gpt-6-luna" not in models
    assert "gpt-6-sol" in models
    assert "ultra" not in models["gpt-6-sol"]["levels"]


def test_link_dir_roundtrip(tmp_path):
    target = tmp_path / "hub" / "skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("x", encoding="utf-8")
    link = tmp_path / "tool" / "skill"
    P.link_dir(link, target)
    assert P.is_link(link)
    assert (link / "SKILL.md").read_text(encoding="utf-8") == "x"
    P.unlink_dir(link)
    assert not link.exists()
    assert (target / "SKILL.md").exists()  # the target is never touched


def isolate_launcher(tmp_path, monkeypatch):
    """The `trirouter` launcher writes a PATH entry: never let a test reach the real registry or ~/.local/bin."""
    monkeypatch.setattr(install_hooks, "BIN", tmp_path / "bin")
    monkeypatch.setattr(install_hooks, "LOCAL_BIN", tmp_path / "local-bin")
    store = {"path": ("C:/Windows", 2)}
    monkeypatch.setattr(install_hooks, "user_path_read", lambda: store["path"])
    monkeypatch.setattr(install_hooks, "user_path_write", lambda value, kind: store.update(path=(value, kind)))
    monkeypatch.setattr(install_hooks, "broadcast_env_change", lambda: store.setdefault("broadcast", []).append(1))
    return store


def test_install_hooks_idempotent_and_uninstall(tmp_path, monkeypatch):
    paths = {k: tmp_path / Path(v).relative_to(P.HOME) for k, v in P.PATHS.items()}
    monkeypatch.setattr(P, "PATHS", paths)
    monkeypatch.setattr(P, "claude_desktop_config", lambda: tmp_path / "claude_desktop_config.json")
    monkeypatch.setattr(install_hooks, "SHIM_HOOK", tmp_path / "bin" / "run_hook.py")
    monkeypatch.setattr(install_hooks, "SHIM_MCP", tmp_path / "bin" / "mcp_server.py")
    monkeypatch.setattr(install_hooks, "SHIM_ROUTE", tmp_path / "bin" / "route.py")
    isolate_launcher(tmp_path, monkeypatch)
    paths["claude_settings"].parent.mkdir(parents=True)
    paths["claude_settings"].write_text(json.dumps({"model": "sonnet", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}))
    assert install_hooks.install(apply=True) > 0
    assert install_hooks.install(apply=True) == 0                      # second run: nothing changes
    s = json.loads(paths["claude_settings"].read_text())
    assert s["model"] == "sonnet"
    assert len(s["hooks"]["Stop"]) == 2                                # foreign hook kept, ours added
    assert set(s["hooks"]) == {"UserPromptSubmit", "Stop", "StopFailure", "SessionEnd", "SubagentStart", "SessionStart"}
    assert set(json.loads(paths["codex_hooks"].read_text())["hooks"]) == {"UserPromptSubmit", "Stop"}
    agy = json.loads(paths["agy_hooks"].read_text())
    assert set(agy["router"]) == {"PreInvocation", "Stop"}
    assert "jev-router" in paths["codex_config"].read_text()
    install_hooks.install(apply=True, uninstall=True)
    s = json.loads(paths["claude_settings"].read_text())
    assert s["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}
    assert "jev-router" not in paths["codex_config"].read_text()


def test_null_device_stdin_is_not_a_terminal(monkeypatch):
    """Windows: isatty() is True for NUL, so an unattended run waited forever at the first question."""
    from jev_router import cli
    with open(os.devnull, encoding="utf-8") as nul:
        assert not P.is_terminal(nul)
        monkeypatch.setattr(sys, "stdin", nul)
        monkeypatch.setattr(cli, "YES", False)
        assert cli.ask("Move them now?", "y") is False
        assert cli.ask_remote_name({"remote_name": "box"}) == "box"


def test_python_cmd_has_no_spaces():
    assert " " not in P.python_cmd()


def test_codex_mcp_keeps_following_generated_block(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "x"\n\n[mcp_servers.jev-router]\ncommand = "old"\nargs = ["a"]\n\n'
                   "# >>> jev-router agents (generated - edit router/targets.json, not this block)\n"
                   '[agents.a-low]\ndescription = "d"\n# <<< jev-router agents\n', encoding="utf-8")
    w = install_hooks.Writer(apply=True)
    install_hooks.codex_mcp(cfg, w)
    text = cfg.read_text(encoding="utf-8")
    assert "# >>> jev-router agents" in text
    assert text.count("[mcp_servers.jev-router]") == 1
    w2 = install_hooks.Writer(apply=True)
    install_hooks.codex_mcp(cfg, w2)
    assert w2.changes == 0  # idempotent
    install_hooks.codex_mcp(cfg, install_hooks.Writer(apply=True), uninstall=True)
    text = cfg.read_text(encoding="utf-8")
    assert "mcp_servers.jev-router" not in text
    assert "[agents.a-low]" in text


def test_queue_drops_cancelled_turn(tmp_path):
    import time
    queue_state.on_submit(tmp_path, "claude", "s", "/w", "old task")
    assert queue_state.on_submit(tmp_path, "claude", "s", "/w", "next", last_activity=time.time())["ahead"]
    # transcript silent for longer than IDLE_S: the earlier turn was cancelled (no Stop hook on Esc)
    info = queue_state.on_submit(tmp_path, "claude", "s", "/w", "new", last_activity=time.time() - queue_state.IDLE_S - 5)
    assert info["ahead"] == []


def test_antigravity_stop_waits_for_fully_idle(monkeypatch, capsys):
    hook(monkeypatch, capsys, "claude", "UserPromptSubmit", {"prompt": "long running work", "session_id": "g", "cwd": "/w"})
    from jev_router import queue_state as q
    q.on_submit(core.STATE_DIR, "antigravity", "conv", "/x", "bg work")
    hook(monkeypatch, capsys, "antigravity", "Stop", {"conversationId": "conv", "fullyIdle": False})
    assert q.on_submit(core.STATE_DIR, "antigravity", "conv", "/x", "next")["ahead"]   # still running
    hook(monkeypatch, capsys, "antigravity", "Stop", {"conversationId": "conv", "fullyIdle": True})
    assert not q.on_submit(core.STATE_DIR, "antigravity", "conv", "/x", "after")["ahead"]


def test_owned_is_real_containment(tmp_path, monkeypatch):
    from jev_router import hub as skills_hub
    hub, old = tmp_path / ".skills", tmp_path / ".skills-old"
    (hub / "a").mkdir(parents=True)
    (old / "b").mkdir(parents=True)
    monkeypatch.setattr(skills_hub, "HUB", hub)
    P.link_dir(tmp_path / "l1", hub / "a")
    P.link_dir(tmp_path / "l2", old / "b")
    assert skills_hub.owned(tmp_path / "l1")
    assert not skills_hub.owned(tmp_path / "l2")


def test_agents_for_every_provider_follow_the_model_role(monkeypatch):
    from jev_router import hub as skills_hub
    monkeypatch.setattr(skills_hub, "PROVIDERS", ("claude", "codex", "antigravity"))
    plan = {(p, name): (model, effort, desc, body) for p, name, model, effort, desc, body in skills_hub.planned_agents()}
    deep = skills_hub._split_template(skills_hub.AGENT_TEMPLATES / "deep-worker.md")
    fast = skills_hub._split_template(skills_hub.AGENT_TEMPLATES / "fast-worker.md")
    assert plan["claude", "opus-worker-max"][3] == deep[1]
    assert plan["codex", "gpt-5_6-terra-high"][2].startswith(deep[0]["description"].rstrip(".") + ". Fixed model gpt-5.6-terra")
    assert plan["codex", "gpt-5_6-luna-low"][3] == fast[1]
    assert plan["antigravity", "gemini-pro-worker"][:2] == ("pro", None)
    assert plan["antigravity", "gemini-flash-worker"][0] == "flash"
    assert not any(effort == "ultra" for _, effort, _, _ in plan.values())


def test_antigravity_agent_files(tmp_path, monkeypatch, capsys):
    """Stale generated agents go, hand-written ones stay."""
    from jev_router import hub as skills_hub
    agents = tmp_path / "agents"
    for name, text in (("gemini-old-worker", f"---\nname: x\n# {skills_hub.GEN_MARK}\n---\n"), ("mine", "---\nname: mine\n---\n")):
        (agents / name).mkdir(parents=True)
        (agents / name / "agent.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(skills_hub, "AGY_AGENTS", agents)
    monkeypatch.setattr(skills_hub, "PROVIDERS", ("antigravity",))
    monkeypatch.setattr(skills_hub, "APPLY", True)
    skills_hub.cmd_agents()
    fm, body = skills_hub._split_template(agents / "gemini-pro-worker" / "agent.md")
    assert fm["model"] == "pro"
    assert fm["subagent"] == "true"
    assert fm["mainAgent"] == "false"
    assert json.loads(fm["description"]).startswith("Strong worker")
    assert body.startswith("# Instructions\n")
    assert not (agents / "gemini-old-worker").exists()
    assert (agents / "mine" / "agent.md").exists()
    capsys.readouterr()
    skills_hub.cmd_agents()
    assert "[DO]" not in capsys.readouterr().out  # idempotent


def test_mcp_command_is_unquoted_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(install_hooks, "SHIM_MCP", tmp_path / "bin" / "mcp_server.py")
    install_hooks.json_mcp(tmp_path / "mcp.json", "x", install_hooks.Writer(apply=True))
    cmd = json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]["jev-router"]["command"]
    assert "'" not in cmd
    assert '"' not in cmd
    assert Path(cmd.replace("/", "\\") if P.IS_WINDOWS else cmd).name.startswith("python")


def test_bad_json_config_is_reported_not_fatal(tmp_path, monkeypatch, capsys):
    paths = {k: tmp_path / Path(v).relative_to(P.HOME) for k, v in P.PATHS.items()}
    monkeypatch.setattr(P, "PATHS", paths)
    monkeypatch.setattr(P, "claude_desktop_config", lambda: tmp_path / "cd.json")
    monkeypatch.setattr(install_hooks, "SHIM_HOOK", tmp_path / "bin" / "run_hook.py")
    monkeypatch.setattr(install_hooks, "SHIM_MCP", tmp_path / "bin" / "mcp_server.py")
    monkeypatch.setattr(install_hooks, "SHIM_ROUTE", tmp_path / "bin" / "route.py")
    paths["claude_settings"].parent.mkdir(parents=True)
    paths["claude_settings"].write_text("{broken", encoding="utf-8")
    install_hooks.install(("claude", "codex"), apply=True)
    assert "[FAIL]" in capsys.readouterr().out
    assert paths["claude_settings"].read_text(encoding="utf-8") == "{broken"      # left untouched
    assert "run_hook.py codex" in paths["codex_hooks"].read_text(encoding="utf-8")  # others still installed


def test_windows_remote_loops_run_headless(tmp_path, monkeypatch):
    from jev_router import remote
    calls = []
    monkeypatch.setattr(remote, "BIN", tmp_path)
    monkeypatch.setattr(remote, "_ps", lambda script: calls.append(script) or (0, ""))
    remote._win_loop("JevRouter-Test", "test-remote.cmd", tmp_path, 'call "codex.cmd" app-server', '$_.Name -eq "x"')
    script = (tmp_path / "test-remote.cmd").read_bytes().decode("utf-8")
    assert ':loop\r\ncall "codex.cmd" app-server\r\ntimeout /t 30' in script
    assert "goto loop\r\n" in script
    assert "\r\r" not in script
    assert "Stop-ScheduledTask" in calls[0]
    assert "test-remote.cmd" in calls[0]
    assert "conhost.exe" in calls[1]
    assert "--headless cmd.exe /c" in calls[1]
    assert "-AtLogOn" in calls[1]
    exe, args = remote._hidden_ps("'ok'")
    assert exe.lower().endswith("conhost.exe")
    assert args.startswith("--headless powershell.exe")


def test_agy_setup_and_watchdog_scripts(monkeypatch):
    from jev_router import remote
    seen = []
    monkeypatch.setattr(remote, "_run_once", lambda script, wait_s=30: seen.append(script) or "ok")
    ok, msg = remote._setup_agy_windows(r"C:\agy\agy.exe", "Bob's PC")
    s = seen[0]
    assert ok
    assert "starts hidden" in msg
    assert "'Bob''s PC'" in s
    assert s.index("remote-control stop") < s.index("remote-control start") < s.index("--headless")
    assert "Stop-Process" in s
    assert "Start-Process -FilePath $conhost" in s
    w = remote._watchdog_script()
    assert "Start-Sleep" in w
    assert "AntigravityCliDaemon" in w
    assert "Start-ScheduledTask" in w
    assert remote.TASK_CLAUDE in w
    assert remote.TASK_CODEX in w


def test_codex_connection_from_log(tmp_path, monkeypatch):
    import sqlite3
    import time
    from jev_router import remote
    monkeypatch.setattr(P, "HOME", tmp_path)
    monkeypatch.setattr(remote.P, "HOME", tmp_path)
    (tmp_path / ".codex").mkdir()
    assert remote.codex_connection() is None
    con = sqlite3.connect(tmp_path / ".codex" / "logs_2.sqlite")
    con.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, target TEXT, feedback_log_body TEXT)")
    t = "codex_app_server_transport::transport::remote_control::websocket"
    con.execute("INSERT INTO logs (ts, target, feedback_log_body) VALUES (?, ?, ?)",
                (int(time.time()), t, 'failed to connect: 409 Conflict {"detail":"Remote app server already online"}'))
    con.commit()
    ok, detail = remote.codex_connection()
    assert ok
    assert "holds the connection" in detail
    con.execute("INSERT INTO logs (ts, target, feedback_log_body) VALUES (?, ?, ?)",
                (int(time.time()), t, "status changed previous_status=Connecting next_status=Connected"))
    con.commit()
    con.close()
    assert remote.codex_connection() == (True, "connected")


def test_doctor_lists_hook_and_mcp_interpreters(tmp_path, monkeypatch):
    from jev_router import doctor
    monkeypatch.setattr(P, "PATHS", {**P.PATHS, "agy_mcp": tmp_path / "mcp.json"})
    monkeypatch.setattr(P, "claude_desktop_config", lambda: tmp_path / "none.json")
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {"jev-router": {"command": "/opt/py/pythonw", "args": []}}}))
    settings = {"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "/usr/bin/python3 ~/.jev-router/bin/run_hook.py claude x"}]}]}}
    toml = '[mcp_servers.jev-router]\ncommand = "/opt/py/python"\nargs = []\n'
    assert doctor.interpreters(settings, toml) == [("Claude hook", "/usr/bin/python3"), ("Antigravity MCP", "/opt/py/pythonw"),
                                                   ("Codex MCP", "/opt/py/python")]
