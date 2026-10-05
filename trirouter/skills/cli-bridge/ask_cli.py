#!/usr/bin/env python3
"""Run the Codex or Antigravity CLI headless and print its answer.

Usage:  python ask_cli.py codex       [--model M] [--effort E] < prompt.txt
        python ask_cli.py antigravity [--model M] [--effort E] < prompt.txt
Exit codes: 0 success, 2 bad usage / not installed / cloud sandbox, 3 timeout,
            anything else: the tool's own exit code.

Codex runs in its read-only sandbox, Antigravity in plan mode, so neither can edit files.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

TIMEOUT_S = int(os.environ.get("CLI_BRIDGE_TIMEOUT", "600"))
FALLBACK = {"agy": Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"}


def find(exe):
    found = shutil.which(exe)
    if found:
        return found
    fb = FALLBACK.get(exe)
    return str(fb) if fb and fb.is_file() else None


def build(tool, exe, model, effort, prompt):
    if tool == "codex":
        argv = [exe, "exec", "--skip-git-repo-check"]
        if model:
            argv += ["-m", model]
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        return argv + ["-"], prompt.encode("utf-8")
    # -p must come last: agy treats the token right after -p as the prompt (verified, agy 1.2.9)
    argv = [exe, "--mode", "plan", "--print-timeout", f"{TIMEOUT_S}s"]
    if model:
        argv += ["--model", model]
    if effort in ("low", "medium", "high"):
        argv += ["--effort", effort]
    return argv + ["-p", prompt], None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tool", choices=["codex", "antigravity"])
    ap.add_argument("--model")
    ap.add_argument("--effort")
    a = ap.parse_args()
    if os.environ.get("ROUTER_MODE", "").lower() == "cloud" or os.environ.get("CLAUDE_CODE_REMOTE", "").lower() == "true":
        print("cli-bridge: not available in a cloud sandbox (no Codex/Antigravity login).", file=sys.stderr)
        return 2
    exe_name = "codex" if a.tool == "codex" else "agy"
    exe = find(exe_name)
    if not exe:
        print(f"cli-bridge: '{exe_name}' is not installed / not on PATH.", file=sys.stderr)
        return 2
    prompt = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace").strip()
    if not prompt:
        print("cli-bridge: empty prompt.", file=sys.stderr)
        return 2
    argv, stdin = build(a.tool, exe, a.model, a.effort, prompt)
    env = os.environ.copy()
    env["TRIROUTER_NESTED"] = "1"
    try:
        r = subprocess.run(argv, input=stdin, capture_output=True, timeout=TIMEOUT_S + 30, env=env,
                           stdin=None if stdin is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        print(f"cli-bridge: {exe_name} timed out ({TIMEOUT_S} s).", file=sys.stderr)
        return 3
    sys.stdout.buffer.write(r.stdout)
    if r.returncode:
        sys.stderr.buffer.write(r.stderr[-4000:])
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
