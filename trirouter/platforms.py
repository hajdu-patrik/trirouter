#!/usr/bin/env python3
"""OS abstraction: config locations, directory links, executables, tool detection.

Windows uses directory junctions, which need no admin rights or Developer Mode.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
STATE = HOME / ".trirouter"  # per-user state; see legacy.state_dir() for the pre-migration fallback


def claude_desktop_config():
    if IS_WINDOWS:
        app_data = Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))
    elif IS_MAC:
        app_data = HOME / "Library" / "Application Support"
    else:
        app_data = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))
    return app_data / "Claude" / "claude_desktop_config.json"


CLAUDE_HOME = HOME / ".claude"
CODEX_HOME = HOME / ".codex"
AGY_CONFIG = HOME / ".gemini" / "config"
PATHS = {
    "claude_settings": CLAUDE_HOME / "settings.json",
    "claude_skills": CLAUDE_HOME / "skills",
    "claude_agents": CLAUDE_HOME / "agents",
    "codex_home": CODEX_HOME,
    "codex_hooks": CODEX_HOME / "hooks.json",
    "codex_config": CODEX_HOME / "config.toml",
    "codex_agents": CODEX_HOME / "agents",
    "codex_skills": HOME / ".agents" / "skills",
    "agy_config": AGY_CONFIG,
    "agy_hooks": AGY_CONFIG / "hooks.json",
    "agy_skills_json": AGY_CONFIG / "skills.json",
    "agy_mcp": AGY_CONFIG / "mcp_config.json",
    "agy_agents": AGY_CONFIG / "agents",
}


def is_link(p):
    """Symlink or junction. Path.is_junction() needs Python 3.12+."""
    p = Path(p)
    try:
        if p.is_symlink():
            return True
        if hasattr(p, "is_junction"):
            return p.is_junction()
        return IS_WINDOWS and bool(os.lstat(p).st_file_attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except (OSError, AttributeError):
        return False


def link_dir(link, target):
    link, target = Path(link), Path(target)
    link.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if r.returncode:
            raise OSError((r.stderr or r.stdout).strip())
    else:
        os.symlink(target, link, target_is_directory=True)


def unlink_dir(link):
    """Removes the link only, never the target's contents."""
    link = Path(link)
    if IS_WINDOWS:
        os.rmdir(link)
    else:
        link.unlink()


def find_exe(name):
    """PATH first, then default install locations: a fresh install is often not on PATH yet."""
    found = shutil.which(name)
    if found:
        return found
    candidates = []
    if name == "agy":
        candidates = [HOME / ".local" / "bin" / "agy", HOME / ".agy" / "bin" / "agy"]
        if IS_WINDOWS and os.environ.get("LOCALAPPDATA"):
            candidates.insert(0, Path(os.environ["LOCALAPPDATA"]) / "agy" / "bin" / "agy.exe")
    elif name == "skillspector":  # `uv tool install` default bin folder
        candidates = [HOME / ".local" / "bin" / ("skillspector.exe" if IS_WINDOWS else "skillspector")]
    elif name == "claude":
        candidates = [HOME / ".local" / "bin" / ("claude.exe" if IS_WINDOWS else "claude"), CLAUDE_HOME / "local" / "claude"]
    for c in candidates:
        if str(c) and Path(c).is_file():
            return str(c)
    return None


def short_path(p):
    """Windows 8.3 form, which needs no quotes: Antigravity's `cmd /c` mangles quoted paths."""
    p = str(p)
    if not IS_WINDOWS or " " not in p:
        return p
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(p, buf, 1024):
            return buf.value
    except (OSError, AttributeError):
        pass
    return p


def is_terminal(stream):
    """On Windows isatty() is also True for the NUL device, where waiting for input hangs forever."""
    try:
        if stream is None or not stream.isatty():
            return False
        if not IS_WINDOWS:
            return True
        import ctypes
        import msvcrt
        mode = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(mode)))
    except (OSError, ValueError, AttributeError):
        return False


def python_exe():
    """The base interpreter inside a virtualenv: the venv may be deleted later."""
    exe = sys.executable or ("python" if IS_WINDOWS else "python3")
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        exe = getattr(sys, "_base_executable", exe) or exe
    return exe


def app_running(name):
    if IS_WINDOWS:
        code, out = run(["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/FO", "CSV", "/NH"])
        return code == 0 and f'"{name.lower()}.exe"' in out.lower()
    return run(["pgrep", "-x", name])[0] == 0


def python_exe_windowless():
    """pythonw.exe on Windows: a host without a console would open a terminal window for python.exe."""
    exe = python_exe()
    if IS_WINDOWS:
        w = Path(exe).with_name("pythonw.exe")
        if Path(exe).name.lower() == "python.exe" and w.is_file():
            return str(w)
    return exe


def shell_arg(p):
    """Short path on Windows (see short_path), shell-quoted on POSIX."""
    import shlex
    if IS_WINDOWS:
        return short_path(p).replace("\\", "/")
    return shlex.quote(str(p))


def python_cmd():
    return shell_arg(python_exe())


# ---- files: atomic writes ----------------------------------------------------------------------------------

def atomic_write(path, text, mode=None, newline=None, encoding="utf-8"):
    """Writes `text` (str, or bytes) to `path` so that an interrupt (Ctrl+C, a crash, a full disk) never leaves a half-written
    file: the content goes to a temporary file in the same folder, which then replaces the target in one step
    (os.replace). The old file stays intact until that last step. `mode` (e.g. 0o600) is applied to the new
    file before it is put in place; `newline` is passed to open() ("" keeps the line ends exactly). A symlink
    target is written through, the link itself is kept."""
    path = Path(path)
    if path.is_symlink():
        path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
                     0o600 if mode is None else mode)
        raw = isinstance(text, bytes)
        with os.fdopen(fd, "wb" if raw else "w", **({} if raw else {"encoding": encoding, "newline": newline})) as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        if mode is not None and not IS_WINDOWS:
            os.chmod(tmp, mode)  # os.open's mode is subject to the umask
        elif mode is None:
            _copy_mode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _copy_mode(path, tmp):
    """A file that is rewritten keeps its permissions; a new one gets the usual 0o666 & ~umask."""
    try:
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        elif not IS_WINDOWS:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
    except OSError:
        pass


# ---- child processes ----------------------------------------------------------------------------------------

# Windows: set by a background process that has no console (spawn_detached), so the console programs it runs
# (codex.cmd, agy.exe, ...) do not each open a visible console window.
NO_WINDOW = False
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS, _NEW_GROUP, _BREAKAWAY = 0x00000008, 0x00000200, 0x01000000

_CHILDREN = set()  # running children started by run() / call(): killed when the command is interrupted
_CHILDREN_LOCK = threading.Lock()


def kill_tree(proc):
    """Ends `proc` and everything it started (codex.cmd -> cmd.exe -> node, ...); never raises."""
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=20, creationflags=_CREATE_NO_WINDOW)
        else:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # run() starts children in their own session/group
            except (ProcessLookupError, PermissionError):
                pass
        proc.kill()
    except (OSError, subprocess.SubprocessError, ValueError):
        pass


def kill_children():
    """Kills every child started through run() / call() that is still running."""
    with _CHILDREN_LOCK:
        procs = list(_CHILDREN)
    for proc in procs:
        if proc.poll() is None:
            kill_tree(proc)


def _popen(argv, **kw):
    extra = {"creationflags": _CREATE_NO_WINDOW} if IS_WINDOWS and NO_WINDOW else {}
    if not IS_WINDOWS:
        extra["start_new_session"] = True  # one group per child, so it can be ended as a whole
    proc = subprocess.Popen(argv, **kw, **extra)
    with _CHILDREN_LOCK:
        _CHILDREN.add(proc)
    return proc


def _forget(proc):
    with _CHILDREN_LOCK:
        _CHILDREN.discard(proc)


def run(argv, timeout=30, env=None):
    """(returncode, combined output); never raises, except for KeyboardInterrupt (Ctrl+C), which first ends the
    child and its whole process tree. The wait polls in short sleeps: unlike a lock wait, a sleep is
    interrupted by Ctrl+C on Windows too."""
    try:
        proc = _popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                      encoding="utf-8", errors="replace", env=env)
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    box = {}

    def collect():
        try:
            box["out"] = proc.communicate()
        except Exception as exc:  # noqa: BLE001 - reported below
            box["err"] = exc

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout if timeout else None
    try:
        while reader.is_alive():
            if deadline is not None and time.monotonic() > deadline:
                kill_tree(proc)
                reader.join(5)
                return None, f"TimeoutExpired: Command '{argv}' timed out after {timeout} seconds"
            time.sleep(0.05)
    except BaseException:
        kill_tree(proc)
        raise
    finally:
        _forget(proc)
    if "err" in box:
        return None, f"{type(box['err']).__name__}: {box['err']}"
    out, err = box["out"]
    return proc.returncode, ((out or "") + (err or "")).strip()


def call(argv):
    """Runs argv with the console's stdin/stdout/stderr and returns its exit code (None when it cannot start). Ctrl+C
    ends the child and its tree."""
    try:
        proc = _popen(argv)
    except (OSError, ValueError):
        return None
    try:
        while proc.poll() is None:
            time.sleep(0.05)
        return proc.returncode
    except BaseException:
        kill_tree(proc)
        raise
    finally:
        _forget(proc)


def spawn_detached(argv, env=None, cwd=None):
    """Starts argv in the background, fully detached: no console window, no inherited stdin/stdout/stderr (a
    hook's host waits for its output pipes to close, so they must never reach the child), its own process
    group / session, and on Windows out of the host's job object when the job allows it. True when started;
    never raises."""
    common = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                  env=env, cwd=cwd)
    try:
        if not IS_WINDOWS:
            subprocess.Popen(argv, start_new_session=True, **common)
            return True
        flags = _DETACHED_PROCESS | _NEW_GROUP | _CREATE_NO_WINDOW
        for extra in (_BREAKAWAY, 0):  # breaking away is refused inside a job that forbids it
            try:
                subprocess.Popen(argv, creationflags=flags | extra, **common)
                return True
            except OSError:
                if not extra:
                    raise
    except (OSError, ValueError):
        return False
    return False


PROVIDERS = {
    "claude": {"label": "Claude Code", "exe": "claude",
               "install": "https://code.claude.com/docs/en/quickstart",
               "login": "claude  (then run /login inside it)"},
    "codex": {"label": "OpenAI Codex CLI", "exe": "codex",
              "install": "npm install -g @openai/codex   (or https://developers.openai.com/codex)",
              "login": "codex login"},
    "antigravity": {"label": "Google Antigravity CLI", "exe": "agy",
                    "install": "https://antigravity.google/docs/cli/install/",
                    "login": "agy  (sign in with Google in the browser window that opens)"},
}


def detect_one(provider, deep=True):
    """logged_in is None when unknown."""
    spec = PROVIDERS[provider]
    exe = find_exe(spec["exe"])
    info = {"provider": provider, "label": spec["label"], "installed": bool(exe), "path": exe,
            "version": None, "logged_in": None, "detail": ""}
    if not exe:
        return info
    _, out = run([exe, "--version"], timeout=30)
    info["version"] = (out.splitlines() or [""])[0][:80]
    if provider == "claude":
        code, out = run([exe, "auth", "status"], timeout=30)
        try:
            info["logged_in"] = bool(json.loads(out[out.index("{"):]).get("loggedIn"))
        except (ValueError, AttributeError):
            info["detail"] = out[:120]
    elif provider == "codex":
        code, out = run([exe, "login", "status"], timeout=30)
        info["logged_in"] = code == 0 and "logged in" in out.lower() and "not logged in" not in out.lower()
    elif provider == "antigravity" and deep:
        code, out = run([exe, "models"], timeout=120)
        low = out.lower()
        if "not logged in" in low or "sign in" in low:
            info["logged_in"] = False
        elif re.search(r"^\S+\s+\S", out, re.M) and code == 0:
            info["logged_in"] = True
        else:
            info["detail"] = out[:120]
    return info


def detect(deep=True):
    return {p: detect_one(p, deep) for p in PROVIDERS}


def hostname():
    import socket
    return socket.gethostname()


def launcher_path():
    """The `trirouter` launcher the installer writes (see integrations.install_launcher)."""
    return STATE / "bin" / ("trirouter.cmd" if IS_WINDOWS else "trirouter")


def command_hint(args=""):
    """How to spell a command in a hint: `trirouter ...` once the launcher exists, else `python install.py ...`."""
    prog = "trirouter" if os.environ.get("TRIROUTER_PROG") == "trirouter" or launcher_path().exists() else "python install.py"
    return f"{prog} {args}".strip()
