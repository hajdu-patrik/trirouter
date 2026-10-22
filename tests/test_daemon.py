"""The hook daemon: token, health, hook round trips, limits, lifecycle, hook registration for http and back, the pooled
JEV connection. Servers run on ephemeral ports with a temporary state folder."""
import http.client
import io
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from trirouter import core, daemon, hooks, integrations as integ, platforms as P

TOKEN = "t" * 43


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    monkeypatch.setattr(core, "BACKEND", "local")
    monkeypatch.setattr(core, "KEEPALIVE", False)
    monkeypatch.setattr(hooks, "LOG_FILE", tmp_path / "logs" / "routing.jsonl")
    monkeypatch.setattr(hooks, "SUBAGENT_LOG", tmp_path / "logs" / "subagents.jsonl")
    monkeypatch.setattr(hooks, "SEEN_FILE", tmp_path / "state" / "seen.json")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_OPENROUTER_API_KEY", raising=False)
    paths = {k: tmp_path / "home" / Path(v).relative_to(P.HOME) for k, v in P.PATHS.items()}
    monkeypatch.setattr(P, "PATHS", paths)
    return tmp_path


@pytest.fixture
def server():
    srv = daemon.make_server(0, TOKEN, runner=daemon.HookRunner(keepalive=False))
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, token=TOKEN, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
    hdrs = {**({daemon.TOKEN_HEADER: token} if token else {}), **(headers or {})}
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def write_config(tmp_path, cfg):
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


# ---- the server ---------------------------------------------------------------------------------------------

def test_health_needs_no_token(server):
    status, data = call(server, "GET", "/health", token=None)
    body = json.loads(data)
    assert status == 200 and body["ok"] is True and body["service"] == "trirouter" and isinstance(body["pid"], int)
    assert daemon.health(server.server_port, 2.0)["port"] == server.server_port


def test_hooks_need_the_token(server):
    payload = json.dumps({"prompt": "Write unit tests for the parser", "session_id": "s"}).encode()
    assert call(server, "POST", "/hook/claude/UserPromptSubmit", payload, token=None)[0] == 403
    assert call(server, "POST", "/hook/claude/UserPromptSubmit", payload, token="x" * 43)[0] == 403
    assert call(server, "POST", "/shutdown", b"", token="wrong")[0] == 403
    assert not (core.STATE_DIR / "logs" / "routing.jsonl").exists()  # nothing was routed


def test_user_prompt_submit_round_trip(server, tmp_path):
    payload = {"prompt": "Write unit tests for the parser module", "session_id": "s1", "cwd": "/w"}
    status, data = call(server, "POST", "/hook/claude/UserPromptSubmit", json.dumps(payload).encode())
    out = json.loads(data)["hookSpecificOutput"]
    assert status == 200 and out["hookEventName"] == "UserPromptSubmit" and out["additionalContext"].startswith("[router]")
    # the same handler as the command hook: the decision is logged and the queue holds the turn
    assert json.loads((tmp_path / "logs" / "routing.jsonl").read_text(encoding="utf-8").splitlines()[-1])["provider"] == "claude"
    second = call(server, "POST", "/hook/claude/UserPromptSubmit",
                  json.dumps({**payload, "prompt": "Also add a README"}).encode())[1]
    assert "QUEUE" in json.loads(second)["hookSpecificOutput"]["additionalContext"]
    assert call(server, "POST", "/hook/claude/Stop", json.dumps({"session_id": "s1"}).encode()) == (200, b"")
    assert "POST /hook/claude/UserPromptSubmit 200" in (tmp_path / "logs" / "daemon.log").read_text(encoding="utf-8")


def test_output_matches_the_command_hook(server, monkeypatch, capsys):
    raw = json.dumps({"prompt": "#norouter just a note", "session_id": "n"}).encode()
    assert call(server, "POST", "/hook/claude/UserPromptSubmit", raw) == (200, b"")  # private, nothing queued
    monkeypatch.setattr(hooks, "route_and_log", lambda *a, **k: "[router] fixed")
    raw = json.dumps({"prompt": "Explain TCP slow start", "session_id": "m"}).encode()
    via_http = call(server, "POST", "/hook/codex/UserPromptSubmit", raw)[1].decode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    assert hooks.main(["codex", "UserPromptSubmit"]) == 0
    assert via_http == capsys.readouterr().out.strip() and "[router] fixed" in via_http


def test_a_failing_handler_answers_200_empty(server, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret detail")
    monkeypatch.setattr(hooks, "handle", boom)
    assert call(server, "POST", "/hook/claude/UserPromptSubmit", b'{"prompt": "hello there"}') == (200, b"")
    assert "RuntimeError" in (core.STATE_DIR / "logs" / "daemon.log").read_text(encoding="utf-8")


def test_unknown_paths_and_malformed_bodies(server):
    assert call(server, "POST", "/hook/other/UserPromptSubmit", b"{}")[0] == 404
    assert call(server, "POST", "/hook/claude/../x", b"{}")[0] == 404
    assert call(server, "GET", "/hook/claude/Stop")[0] == 404
    assert call(server, "POST", "/hook/claude/UserPromptSubmit", b"not json") == (200, b"")


def test_oversized_body_is_rejected_unread(server):
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=10) as s:
        s.sendall((f"POST /hook/claude/UserPromptSubmit HTTP/1.1\r\nHost: x\r\n{daemon.TOKEN_HEADER}: {TOKEN}\r\n"
                   f"Content-Length: {daemon.MAX_BODY + 1}\r\n\r\n").encode())
        assert s.recv(200).startswith(b"HTTP/1.1 413")


def test_chunked_body_is_read(server):
    body = json.dumps({"prompt": "#privat keep this local", "session_id": "c"}).encode()
    chunked = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=10) as s:
        s.sendall((f"POST /hook/claude/UserPromptSubmit HTTP/1.1\r\nHost: x\r\n{daemon.TOKEN_HEADER}: {TOKEN}\r\n"
                   "Transfer-Encoding: chunked\r\n\r\n").encode() + chunked)
        assert s.recv(200).startswith(b"HTTP/1.1 200")


def test_shutdown_stops_the_server():
    srv = daemon.make_server(0, TOKEN, runner=daemon.HookRunner(keepalive=False))
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    assert call(srv, "POST", "/shutdown", b"")[0] == 200
    thread.join(5)
    srv.server_close()
    assert not thread.is_alive()


def test_idle_server_exits():
    srv = daemon.make_server(0, TOKEN, runner=daemon.HookRunner(keepalive=False), idle_s=0.2)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    srv.watch_idle()  # returns once it has shut the server down
    thread.join(5)
    srv.server_close()
    assert not thread.is_alive()


def test_a_second_daemon_on_a_taken_port_exits_quietly(server, monkeypatch):
    assert daemon.serve(server.server_port) == 0
    assert daemon.read_state().get("pid") is None  # the loser never claims the state file


def test_binds_to_loopback_only(server):
    assert server.server_address[0] == "127.0.0.1"


# ---- lifecycle helpers ----------------------------------------------------------------------------------------

def test_token_is_created_once_and_kept(tmp_path):
    assert daemon.token(create=False) is None
    tok = daemon.token()
    assert len(tok) >= 32 and daemon.token() == tok and daemon.token(create=False) == tok


def test_port_from_config(tmp_path):
    assert daemon.configured_port() == daemon.DEFAULT_PORT
    write_config(tmp_path, {"daemon": {"port": 50123}})
    assert daemon.configured_port() == 50123
    write_config(tmp_path, {"daemon": {"port": "nope"}})
    assert daemon.configured_port() == daemon.DEFAULT_PORT


def test_status_and_stop_against_a_running_server(server, tmp_path):
    write_config(tmp_path, {"daemon": {"port": server.server_port}, "hooks": {"transport": "http"}})
    s = daemon.status()
    assert s["running"] and s["pid"] and s["port"] == server.server_port and s["transport"] == "http"
    daemon._write_state({**daemon.read_state(), "token": TOKEN, "port": server.server_port})
    assert daemon.stop(wait=3.0) is True
    assert daemon.health(server.server_port, 0.5) is None


def test_ensure_running_never_raises(monkeypatch):
    monkeypatch.setattr(daemon, "configured_port", lambda cfg=None: (_ for _ in ()).throw(RuntimeError("x")))
    assert daemon.ensure_running() is False


def test_start_reports_a_failed_spawn(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(P, "spawn_detached", lambda argv, **k: spawned.append(argv) or False)
    write_config(tmp_path, {"daemon": {"port": 1}})
    assert daemon.start(wait=0.2, check=False) is False
    assert spawned and spawned[0][1:] == ["-m", "trirouter.daemon", "--port", "1"]
    assert daemon.token(create=False)  # created before the spawn


def test_session_start_starts_the_daemon_only_for_http(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "ensure_running", lambda: calls.append(1) or True)
    assert hooks.session_start_output("claude", b"{}") == ""
    assert calls == []
    write_config(tmp_path, {"hooks": {"transport": "http"}})
    hooks.session_start_output("claude", b"{}")
    hooks.session_start_output("codex", b"{}")
    assert calls == [1]
    monkeypatch.setattr(daemon, "ensure_running", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert hooks.session_start_output("claude", b"{}") == ""  # swallowed


# ---- hook registration ----------------------------------------------------------------------------------------

def _entries(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    return {e: [h for g in groups for h in g["hooks"]] for e, groups in cfg.get("hooks", {}).items()}


def test_http_registration_and_back(tmp_path, capsys):
    settings = P.PATHS["claude_settings"]
    settings.parent.mkdir(parents=True)
    foreign_http = {"type": "http", "url": "https://example.com/hook", "timeout": 5}
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [foreign_http]}]}}), encoding="utf-8")
    integ.claude_codex_hooks(settings, "claude", integ.Writer(True), transport="command")
    assert all(h["type"] == "command" for e, hs in _entries(settings).items() for h in hs if integ.is_router_hook(h))

    write_config(tmp_path, {"hooks": {"transport": "http"}, "daemon": {"port": 50124}})
    integ.claude_codex_hooks(settings, "claude", integ.Writer(True))  # the transport comes from config.json
    entries, tok = _entries(settings), daemon.token(create=False)
    for event in integ.HOOK_EVENTS["claude"]:
        ours = [h for h in entries[event] if integ.is_router_hook(h)]
        assert len(ours) == 1, event
        if event in integ.COMMAND_ONLY_EVENTS:
            assert ours[0]["type"] == "command"
        else:
            assert ours[0] == {"type": "http", "url": f"http://127.0.0.1:50124/hook/claude/{event}",
                               "headers": {daemon.TOKEN_HEADER: tok}, "timeout": 10}
    assert foreign_http in entries["Stop"] and not integ.is_router_hook(foreign_http)
    assert integ.http_hook_tokens(json.loads(settings.read_text(encoding="utf-8"))) == {tok}
    assert integ.has_router_hooks(settings) and integ.configured_providers()[0][:1] == ["claude"]

    integ.claude_codex_hooks(settings, "claude", integ.Writer(True), transport="command")
    entries = _entries(settings)
    assert not any(h["type"] == "http" for hs in entries.values() for h in hs if integ.is_router_hook(h))
    assert foreign_http in entries["Stop"]


def test_uninstall_removes_http_entries(tmp_path):
    settings = P.PATHS["claude_settings"]
    integ.claude_codex_hooks(settings, "claude", integ.Writer(True), transport="http")
    assert any(h["type"] == "http" for hs in _entries(settings).values() for h in hs)
    integ.claude_codex_hooks(settings, "claude", integ.Writer(True), uninstall=True)
    assert "hooks" not in json.loads(settings.read_text(encoding="utf-8"))


def test_codex_never_gets_http_hooks(tmp_path):
    path = P.PATHS["codex_hooks"]
    integ.claude_codex_hooks(path, "codex", integ.Writer(True), transport="http")
    assert all(h["type"] == "command" for hs in _entries(path).values() for h in hs)


def test_dry_run_creates_no_token(tmp_path):
    settings = P.PATHS["claude_settings"]
    integ.claude_codex_hooks(settings, "claude", integ.Writer(False), transport="http")
    assert not settings.exists() and daemon.token(create=False) is None


def test_router_url_recognition():
    assert integ.is_router_url("http://127.0.0.1:47621/hook/claude/Stop")
    assert integ.is_router_url("http://localhost:9/hook/claude/PreToolUse")
    assert not integ.is_router_url("http://10.0.0.1:47621/hook/claude/Stop")
    assert not integ.is_router_url("https://example.com/hook/claude/Stop")


def test_enable_and_disable(tmp_path, monkeypatch, capsys):
    started, stopped = [], []
    monkeypatch.setattr(daemon, "start", lambda *a, **k: started.append(1) or True)
    monkeypatch.setattr(daemon, "stop", lambda *a, **k: stopped.append(1) or True)
    settings = P.PATHS["claude_settings"]
    write_config(tmp_path, {"keep": 1})
    daemon.enable(apply=True)  # no Claude hooks installed: the transport is saved, no hook is added
    assert daemon.transport() == "http" and started == [1] and not settings.exists()
    assert "setup --yes" in capsys.readouterr().out

    write_config(tmp_path, {"keep": 1})
    integ.claude_codex_hooks(settings, "claude", integ.Writer(True), transport="command")
    before = settings.read_text(encoding="utf-8")
    assert daemon.enable(apply=False) > 0  # dry run: nothing written, nothing started
    assert daemon.transport() == "command" and settings.read_text(encoding="utf-8") == before and started == [1]

    daemon.enable(apply=True)
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["hooks"]["transport"] == "http" and cfg["keep"] == 1
    assert _entries(settings)["UserPromptSubmit"][0]["type"] == "http"
    assert _entries(settings)["SessionStart"][0]["type"] == "command"
    assert settings.with_name(settings.name + ".bak").read_text(encoding="utf-8") == before
    assert daemon.enable(apply=True) == 0  # idempotent

    daemon.disable(apply=True)
    assert daemon.transport() == "command" and stopped == [1]
    assert _entries(settings)["UserPromptSubmit"][0]["type"] == "command"


# ---- JEV keep-alive -----------------------------------------------------------------------------------------

class _Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    peers = []

    def do_POST(self):
        _Echo.peers.append(self.client_address[1])
        body = self.rfile.read(int(self.headers["Content-Length"]))
        status = 500 if b"fail" in body else 200
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def echo(monkeypatch):
    monkeypatch.setattr(core, "_IDLE", {})
    _Echo.peers = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()
    srv.server_close()


def test_keepalive_reuses_one_connection(echo):
    assert core._post_keepalive(echo, b'{"a": 1}', {}) == b'{"a": 1}'
    assert core._post_keepalive(echo, b'{"a": 2}', {}) == b'{"a": 2}'
    assert len(_Echo.peers) == 2 and len(set(_Echo.peers)) == 1


def test_keepalive_reconnects_once_after_a_dropped_connection(echo):
    class Dropped:
        def request(self, *a, **k):
            raise ConnectionResetError("closed by the server")

        def close(self):
            pass
    port = int(echo.split(":")[2].split("/")[0])
    core._IDLE[("http", "127.0.0.1", port)] = [Dropped()]
    assert core._post_keepalive(echo, b"{}", {}) == b"{}"
    assert len(_Echo.peers) == 1


def test_keepalive_raises_on_an_error_status(echo):
    with pytest.raises(RuntimeError, match="HTTP 500"):
        core._post_keepalive(echo, b"fail", {})


def test_ask_jev_uses_the_pool_only_when_switched_on(echo, monkeypatch):
    monkeypatch.setattr(core, "jev_endpoint", lambda: ("typesafe", echo, "k", "m"))
    monkeypatch.setattr("urllib.request.getproxies", lambda: {})  # a system proxy would reroute the call
    used = []
    real = core._post_keepalive
    monkeypatch.setattr(core, "_post_keepalive", lambda *a: used.append(1) or real(*a))
    assert core.ask_jev("hello", {})["state"] == "hello"
    assert used == []
    monkeypatch.setattr(core, "KEEPALIVE", True)
    assert core.ask_jev("hello", {})["state"] == "hello" and used == [1]


def test_dropped_client_connections_are_not_logged(server):
    for exc, logged in ((ConnectionResetError("gone"), False), (ValueError("odd"), True)):
        try:
            raise exc
        except Exception:  # noqa: BLE001
            server.handle_error(None, ("127.0.0.1", 1))
        log = core.STATE_DIR / "logs" / "daemon.log"
        assert (log.exists() and type(exc).__name__ in log.read_text(encoding="utf-8")) is logged


def test_a_negative_chunk_size_is_a_bad_request(server):
    headers = {"Transfer-Encoding": "chunked"}
    assert call(server, "POST", "/hook/claude/UserPromptSubmit", b"-5\r\n{}\r\n0\r\n\r\n", headers=headers)[0] == 400
    assert call(server, "POST", "/hook/claude/UserPromptSubmit", b"2\r\n{}\r\n0\r\n\r\n", headers=headers)[0] == 200


def test_a_reload_does_not_wait_forever_for_a_stuck_request(monkeypatch):
    monkeypatch.setattr(daemon, "RELOAD_WAIT_S", 0.1)
    runner = daemon.HookRunner(keepalive=False)
    runner.active = 1  # a request that never finishes
    checks = []
    monkeypatch.setattr(runner.reloader, "stale", lambda: not checks and not checks.append(1))  # stale once
    monkeypatch.setattr(runner.reloader, "current", lambda: setattr(runner.reloader, "module", "fresh"))
    t0 = time.monotonic()
    assert runner._acquire() == "fresh" and time.monotonic() - t0 < 2.0
    assert "still running" in (core.STATE_DIR / "logs" / "daemon.log").read_text(encoding="utf-8")
