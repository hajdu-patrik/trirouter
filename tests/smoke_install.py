"""End-to-end install check on Linux and macOS: a real setup, hook, doctor, providers switch and uninstall in a throwaway
HOME, with fake `claude` and `codex` CLIs. Not a pytest module: CI runs `python tests/smoke_install.py`."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FAKE_CLI = """#!/bin/sh
case "$*" in
  --version) echo "{version}" ;;
  "auth status") echo '{{"loggedIn": true}}' ;;
  "login status") echo "Logged in using ChatGPT" ;;
esac
exit 0
"""


def fail(msg, out=""):
    sys.exit(f"SMOKE FAIL: {msg}\n{out}")


def main():
    if os.name == "nt":  # setup writes the user PATH in the registry there: CI covers Windows with the unit tests
        sys.exit("smoke_install.py runs on Linux and macOS only")
    home = Path(tempfile.mkdtemp(prefix="trirouter-smoke-"))
    fake = home / "fakebin"
    fake.mkdir()
    for exe, version in (("claude", "2.1.400 (Claude Code)"), ("codex", "codex-cli 0.200.0")):
        (fake / exe).write_text(FAKE_CLI.format(version=version), encoding="utf-8")
        (fake / exe).chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ROUTER_", "TRIROUTER_", "XDG_"))}
    env.update(HOME=str(home), PATH=f"{fake}{os.pathsep}{env.get('PATH', '')}", SHELL="/bin/bash")

    def trirouter(*args, stdin=None, expect=0):
        p = subprocess.run([sys.executable, "-m", "trirouter", *args], cwd=REPO, env=env, input=stdin,
                           capture_output=True, text=True, timeout=300)
        if expect is not None and p.returncode != expect:
            fail(f"`trirouter {' '.join(args)}` exited {p.returncode}", p.stdout + p.stderr)
        return p.stdout + p.stderr

    def router_hooks(path, event):
        cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return [h["command"] for g in cfg.get("hooks", {}).get(event, []) for h in g.get("hooks", [])
                if "run_hook" in h.get("command", "")]

    claude_settings, codex_hooks = home / ".claude" / "settings.json", home / ".codex" / "hooks.json"
    trirouter("setup", "--yes", "--providers=claude,codex", "--skip=migrate,remote,dictation")
    hooks = router_hooks(claude_settings, "UserPromptSubmit")
    if not hooks or not router_hooks(codex_hooks, "UserPromptSubmit"):
        fail("setup wrote no router hook", claude_settings.read_text(encoding="utf-8") if claude_settings.exists() else "")
    launcher = home / ".local" / "bin" / "trirouter"
    if not os.access(launcher, os.X_OK):
        fail(f"no executable launcher at {launcher}")

    prompt = json.dumps({"prompt": "fix the failing login test", "session_id": "smoke", "cwd": str(home),
                         "hook_event_name": "UserPromptSubmit"})
    p = subprocess.run(hooks[0], shell=True, env=env, input=prompt, capture_output=True, text=True, timeout=120)
    if p.returncode != 0 or "[router]" not in p.stdout:
        fail(f"the UserPromptSubmit hook exited {p.returncode} without a [router] context", p.stdout + p.stderr)
    p = subprocess.run([str(launcher), "route", "--json", "refactor the parser"], env=env, capture_output=True,
                       text=True, timeout=120)
    if p.returncode != 0 or "task" not in json.loads(p.stdout):
        fail("the installed launcher could not route", p.stdout + p.stderr)

    trirouter("doctor")
    trirouter("providers", "disable", "codex")
    if router_hooks(codex_hooks, "UserPromptSubmit") or "switched off" not in trirouter("providers", "status"):
        fail("providers disable left the Codex hooks")
    trirouter("providers", "enable", "codex")
    if not router_hooks(codex_hooks, "UserPromptSubmit"):
        fail("providers enable did not reconnect Codex")

    trirouter("uninstall")
    if router_hooks(claude_settings, "UserPromptSubmit") or launcher.exists():
        fail("uninstall left a hook or the launcher behind")
    print(f"smoke install OK ({sys.platform})")


if __name__ == "__main__":
    main()
