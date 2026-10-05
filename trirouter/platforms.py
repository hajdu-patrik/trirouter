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


def run(argv, timeout=30):
    """(returncode, combined output); never raises."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           encoding="utf-8", errors="replace")
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{type(exc).__name__}: {exc}"


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
