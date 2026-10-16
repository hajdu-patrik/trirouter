#!/usr/bin/env python3
"""Hook daemon: serves Claude Code's http hooks from one long-running process.

A command hook starts a new Python process for every prompt (0.4-0.5 s on Windows before any work is done)
and opens a new TLS connection to JEV each time. With `hooks.transport: "http"` in ~/.trirouter/config.json
Claude Code POSTs the hook input to this daemon instead, which runs exactly the same handlers (hooks.handle)
and keeps the JEV connection alive. Codex and Antigravity keep their command hooks.

    python -m trirouter.daemon [--port N]      serve on 127.0.0.1 (config.json daemon.port, default 47621)

    GET  /health                          {"ok": true, "service": "trirouter", "pid": ..}  (no token)
    POST /hook/<provider>/<event>         the hook's JSON input; answers the hook's output (200, maybe empty)
    POST /shutdown                        stops the daemon
    Every POST needs the header X-Trirouter-Token (state/daemon.json "token", also written into Claude's
    settings.json); a wrong or missing token gets 403.

The daemon binds to 127.0.0.1 only, exits after IDLE_EXIT_S without a request, on SIGTERM and on /shutdown,
and exits quietly when another trirouter daemon already serves its port. A failing handler answers 200 with an
empty body: Claude Code then continues without router context, exactly as when the daemon is down. Package
source and config changes are picked up on the next request (hotreload.py); a failed reload keeps the old code.
Claude's SessionStart stays a command hook: it starts the daemon (ensure_running) when it is down.
"""
import hmac
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, core, platforms as P
from .hotreload import HotReloader

PKG = Path(__file__).resolve().parent
REPO = PKG.parent
HOST = "127.0.0.1"  # never another interface: the hooks carry prompts
DEFAULT_PORT = 47621
IDLE_EXIT_S = 12 * 3600
MAX_BODY = 2 * 1024 * 1024
TOKEN_HEADER = "X-Trirouter-Token"
PROVIDERS = ("claude", "codex", "antigravity")
EVENT_RE = re.compile(r"^[A-Za-z]{1,64}$")
HEALTH_TIMEOUT_S = 0.2   # the SessionStart hook's check: a local answer takes a few milliseconds
LOG_MAX = 512 * 1024     # daemon.log is cut to its newer half beyond this


# ---- state: ~/.trirouter/state/daemon.json ------------------------------------------------------------------

def state_file():
    return core.STATE_DIR / "state" / "daemon.json"


def log_file():
    return core.STATE_DIR / "logs" / "daemon.log"


def read_state():
    try:
        data = json.loads(state_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state):
    P.atomic_write(state_file(), json.dumps(state, indent=2) + "\n", mode=0o600)  # holds the token: owner-only


def token(create=True):
    """The shared secret between Claude's settings.json and the daemon: created once, kept across restarts.
    None when there is none yet and `create` is False."""
    state = read_state()
    tok = state.get("token")
    if isinstance(tok, str) and len(tok) >= 32:
        return tok
    if not create:
        return None
    state["token"] = secrets.token_urlsafe(32)
    _write_state(state)
    return state["token"]


def configured_port(cfg=None):
    """config.json daemon.port, else DEFAULT_PORT."""
    cfg = core.user_config() if cfg is None else cfg
    try:
        port = int((cfg.get("daemon") or {}).get("port") or DEFAULT_PORT)
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


def transport(cfg=None):
    """config.json hooks.transport: "http" (this daemon) or "command" (the default)."""
    cfg = core.user_config() if cfg is None else cfg
    return "http" if (cfg.get("hooks") or {}).get("transport") == "http" else "command"


_LOG_LOCK = threading.Lock()


def log(message):
    """One line in logs/daemon.log (never a prompt, never the token); bounded, never raises."""
    try:
        with _LOG_LOCK:
            path = log_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
            if path.stat().st_size > LOG_MAX:
                data = path.read_bytes()[-LOG_MAX // 2:]
                P.atomic_write(path, data[data.find(b"\n") + 1:])
    except Exception:  # noqa: BLE001 - logging must never stop a request
        pass


# ---- the server ----------------------------------------------------------------------------------------------

class HookRunner:
    """Runs hooks.handle with the freshest code. A reload waits until no request is in flight, so one request
    never sees half-purged modules; requests run in parallel otherwise."""

    def __init__(self, keepalive=True):
        self.reloader = HotReloader("trirouter", PKG, "hooks", keep=("hotreload", "daemon"))
        self.keepalive = keepalive
        self.cond = threading.Condition()
        self.active = 0

    def _acquire(self):
        with self.cond:
            while self.reloader.stale():
                if self.active:
                    self.cond.wait()
                    continue
                hooks = self.reloader.current()  # raises only when the very first load fails
                if self.reloader.warning:
                    log(self.reloader.warning[:300])
                if self.keepalive:
                    hooks.core.KEEPALIVE = True  # a reload brings a fresh core module with the flag off
                break
            self.active += 1
            return self.reloader.module

    def _release(self):
        with self.cond:
            self.active -= 1
            self.cond.notify_all()

    def warm(self):
        """Loads the hook modules ahead of the first prompt (about 80 ms otherwise). Never raises."""
        try:
            self._acquire()
            self._release()
        except Exception as exc:  # noqa: BLE001 - the first request retries and answers empty if it fails again
            log(f"loading the hooks failed: {type(exc).__name__}: {str(exc)[:200]}")

    def run(self, provider, event, raw):
        hooks = self._acquire()
        try:
            return hooks.handle(provider, event, raw) or ""
        finally:
            self._release()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not P.IS_WINDOWS  # on Windows SO_REUSEADDR would let a second daemon share the port

    def __init__(self, port, secret, runner=None, idle_s=IDLE_EXIT_S):
        self.token = secret.encode("utf-8")
        self.runner = runner or HookRunner()
        self.idle_s = idle_s
        self.last_request = time.monotonic()
        super().__init__((HOST, port), Handler)

    def server_bind(self):
        if P.IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind would also resolve the host's FQDN, which can take seconds on Windows
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = HOST, self.server_address[1]

    def handle_error(self, request, client_address):
        """socketserver prints a traceback to stderr; a client that drops its keep-alive connection is normal."""
        exc = sys.exc_info()[1]
        if not isinstance(exc, (ConnectionError, TimeoutError)):
            log(f"request error: {type(exc).__name__}: {str(exc)[:200]}")

    def stop_soon(self):
        """shutdown() waits for serve_forever to return: never call it on the serving thread itself."""
        threading.Thread(target=self.shutdown, daemon=True).start()

    def watch_idle(self):
        while True:
            time.sleep(max(0.05, min(60.0, self.idle_s / 4)))
            if time.monotonic() - self.last_request > self.idle_s:
                log(f"idle for {int(self.idle_s)} s: exiting")
                self.shutdown()
                return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: Claude Code reuses the connection
    server_version = "trirouter"
    sys_version = ""
    timeout = 60  # an idle keep-alive connection is dropped after this

    def log_message(self, fmt, *args):  # the default writes every request to stderr
        pass

    def _send(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _reject(self, status):
        self.close_connection = True  # the unread body must never be parsed as the next request
        self._send(status)
        return status

    def _authorized(self):
        given = (self.headers.get(TOKEN_HEADER) or "").encode("utf-8")
        return hmac.compare_digest(given, self.server.token)

    def _read_body(self):
        """The request body, or None when it is larger than MAX_BODY."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks, total = [], 0
            while True:
                size = int(self.rfile.readline(1024).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(65536) not in (b"\r\n", b"\n", b""):
                        pass  # trailers
                    return b"".join(chunks)
                total += size
                if total > MAX_BODY:
                    return None
                chunks.append(self.rfile.read(size))
                self.rfile.readline(1024)
        length = max(0, int(self.headers.get("Content-Length") or 0))
        if length > MAX_BODY:
            return None
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        self.server.last_request = time.monotonic()
        if self.path.split("?", 1)[0] != "/health":
            return self._reject(404)
        body = json.dumps({"ok": True, "service": "trirouter", "pid": os.getpid(), "port": self.server.server_port,
                           "version": __version__}).encode("utf-8")
        self._send(200, body)

    def do_POST(self):
        t0 = time.perf_counter()
        self.server.last_request = time.monotonic()
        path = self.path.split("?", 1)[0]
        status = self._post(path)
        log(f"POST {path} {status} {int((time.perf_counter() - t0) * 1000)}ms")

    def _post(self, path):
        parts = path.strip("/").split("/")
        is_hook = len(parts) == 3 and parts[0] == "hook" and parts[1] in PROVIDERS and EVENT_RE.match(parts[2])
        if not is_hook and path != "/shutdown":
            return self._reject(404)
        if not self._authorized():
            return self._reject(403)
        try:
            raw = self._read_body()
        except (ValueError, OSError):
            return self._reject(400)
        if raw is None:
            return self._reject(413)
        if path == "/shutdown":
            self.close_connection = True
            self._send(200, b'{"ok": true}')
            log("shutdown requested")
            self.server.stop_soon()
            return 200
        try:
            out = self.server.runner.run(parts[1], parts[2], raw)
        except Exception as exc:  # noqa: BLE001 - the host gets an empty answer, never a 500 or a traceback
            log(f"hook {parts[1]}/{parts[2]} failed: {type(exc).__name__}: {str(exc)[:200]}")
            out = ""
        self._send(200, out.encode("utf-8"))
        return 200


def make_server(port, secret=None, runner=None, idle_s=IDLE_EXIT_S):
    """A bound, not yet serving Server (port 0: an ephemeral port, see server.server_port)."""
    return Server(port, secret or token(create=True), runner=runner, idle_s=idle_s)


def serve(port=None, idle_s=IDLE_EXIT_S):
    """Runs the daemon in this process until it is stopped or idle; the exit code."""
    port = port or configured_port()
    try:
        server = make_server(port, idle_s=idle_s)
    except OSError as exc:
        if health(port, 1.0):
            return 0  # another trirouter daemon already serves this port
        log(f"cannot listen on {HOST}:{port}: {exc}")
        return 1
    state = read_state()
    state.update(pid=os.getpid(), port=server.server_port, started=time.strftime("%Y-%m-%dT%H:%M:%S"),
                 version=__version__)
    _write_state(state)
    log(f"started: pid {os.getpid()}, {HOST}:{server.server_port}, version {__version__}")
    for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), lambda *_: server.stop_soon())
            except (ValueError, OSError):  # not the main thread, or not settable on this OS
                pass
    threading.Thread(target=server.watch_idle, daemon=True).start()
    threading.Thread(target=server.runner.warm, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state = read_state()
        if state.get("pid") == os.getpid():
            state["pid"] = None
            try:
                _write_state(state)
            except OSError:
                pass
        log(f"stopped: pid {os.getpid()}")
    return 0


# ---- lifecycle: used by the SessionStart hook and the CLI -----------------------------------------------------

def _request(method, port, path, timeout, headers=None):
    """(status, parsed JSON body or None); raises OSError when nothing answers."""
    import http.client
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        conn.request(method, path, body=b"" if method == "POST" else None, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
    except http.client.HTTPException as exc:
        raise OSError(str(exc)) from exc
    finally:
        conn.close()
    try:
        return resp.status, json.loads(data) if data else None
    except ValueError:
        return resp.status, None


def health(port=None, timeout=HEALTH_TIMEOUT_S):
    """The /health answer of a trirouter daemon on the port, or None."""
    try:
        status, body = _request("GET", port or configured_port(), "/health", timeout)
    except OSError:
        return None
    return body if status == 200 and isinstance(body, dict) and body.get("service") == "trirouter" else None


def daemon_argv(port=None):
    return [P.python_exe_windowless(), "-m", "trirouter.daemon"] + (["--port", str(port)] if port else [])


def start(wait=2.0, port=None, check=True):
    """Starts the daemon detached unless it already runs; True once it answers /health (within `wait` s).
    check=False skips the first probe (a second daemon on a taken port exits quietly)."""
    port = port or configured_port()
    if check and health(port):
        return True
    token(create=True)  # here, not in two racing daemons
    if not P.spawn_detached(daemon_argv(port), cwd=str(REPO)):
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.05)
        if health(port):
            return True
    return False


def stop(wait=3.0):
    """Stops the daemon on the configured port; True when none runs afterwards. Only a process that answered
    /health as trirouter is ever signalled (a stale pid may belong to another program by now)."""
    port = read_state().get("port") or configured_port()
    info = health(port, 0.5)
    if not info:
        return True
    tok = token(create=False) or ""
    try:
        _request("POST", port, "/shutdown", 1.0, {TOKEN_HEADER: tok})
    except OSError:
        pass
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not health(port):
            return True
        time.sleep(0.1)
    pid = info.get("pid")
    if isinstance(pid, int) and pid > 0 and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGTERM)  # Windows: TerminateProcess
        except OSError:
            pass
        time.sleep(0.3)
    return not health(port)


def ensure_running():
    """Cheap check from the SessionStart hook: starts the daemon when it does not answer. Never raises."""
    try:
        # Windows needs about 2 s to refuse a closed loopback port, so a probe of a stopped daemon costs its whole
        # timeout: none when the daemon recorded a clean exit
        if read_state().get("pid") and health(configured_port()):
            return True
        return start(check=False)
    except Exception:  # noqa: BLE001 - called from a hook: never block or crash the host tool
        return False


def status():
    """{"transport", "running", "pid", "port", "latency_ms", "version", "started", "token"} for the CLI and doctor;
    "token" is "ok", "missing" or "mismatch" (Claude's settings.json carries another token than the daemon)."""
    port = configured_port()
    t0 = time.perf_counter()
    info = health(port, 0.5)
    latency = round((time.perf_counter() - t0) * 1000, 1)
    state = read_state()
    return {"transport": transport(), "running": bool(info), "pid": (info or {}).get("pid"), "port": port,
            "latency_ms": latency if info else None, "version": (info or {}).get("version"),
            "started": state.get("started") if info else None, "token": _token_check(state.get("token"))}


def _token_check(tok):
    """Do Claude's http hooks (if any) carry the daemon's token?"""
    try:
        from . import integrations
        sent = integrations.http_hook_tokens(integrations.load(P.PATHS["claude_settings"]))
    except Exception:  # noqa: BLE001 - a read-only report
        return "unknown"
    if not sent:
        return "missing" if transport() == "http" else "ok"
    return "ok" if tok and all(hmac.compare_digest(s.encode(), tok.encode()) for s in sent) else "mismatch"


# ---- switching the Claude hooks between command and http ------------------------------------------------------

def _save_config(cfg):
    from . import secret
    P.atomic_write(core.STATE_DIR / "config.json", json.dumps(secret.protect_config(cfg), indent=2), mode=0o600)


def _switch(mode, apply, say):
    """Sets hooks.transport, re-registers Claude's hooks for it (dry run unless `apply`; a changed settings.json
    keeps its .bak) and starts or stops the daemon. The number of changes (made or due)."""
    from . import integrations
    cfg = core.user_config()
    changes = 0
    if transport(cfg) != mode:
        changes += 1
        say(("[DO]  " if apply else "[DRY] ") + f"config.json: hooks.transport = {mode}")
        if apply:
            cfg["hooks"] = {**(cfg.get("hooks") if isinstance(cfg.get("hooks"), dict) else {}), "transport": mode}
            _save_config(cfg)
    else:
        say(f"[OK]  config.json: hooks.transport is {mode}")
    if mode == "http" and apply:
        token(create=True)
        ok = start()
        say(("[OK]  " if ok else "[!!]  ") + f"hook daemon: {'running' if ok else 'did not start'} on "
            f"{HOST}:{configured_port()}")
    settings = P.PATHS["claude_settings"]
    if integrations.has_router_hooks(settings):  # never adds Claude hooks to a setup that has none
        w = integrations.Writer(apply)
        integrations.claude_codex_hooks(settings, "claude", w, transport=mode)
        changes += w.changes
    else:
        say(f"[!!]  Claude Code has no trirouter hooks yet: run {P.command_hint('setup --yes')}")
    if mode == "command" and apply:
        say("[OK]  hook daemon: stopped" if stop() else "[!!]  hook daemon: still running")
    return changes


def enable(apply=False, say=print):
    """`daemon enable`: Claude's hooks go over http to the daemon (SessionStart stays a command hook)."""
    return _switch("http", apply, say)


def disable(apply=False, say=print):
    """`daemon disable`: back to command hooks; the daemon is stopped."""
    return _switch("command", apply, say)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    port = None
    if "--port" in args:
        try:
            port = int(args[args.index("--port") + 1])
        except (IndexError, ValueError):
            print("Usage: python -m trirouter.daemon [--port N]", file=sys.stderr)
            return 2
    return serve(port)


if __name__ == "__main__":
    sys.exit(main())
