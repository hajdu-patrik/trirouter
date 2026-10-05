#!/usr/bin/env python3
"""Shared "before prompt" hook for Claude Code, Codex CLI and Antigravity CLI. Always exits 0.

Antigravity has no prompt-submit event, only PreInvocation, which fires before every model call of a
turn and carries no prompt text. So the latest user input is read back from the transcript, and the
context is injected only once per user turn.

    python -m trirouter.hooks claude|codex UserPromptSubmit|Stop [--cloud-only]
    python -m trirouter.hooks claude StopFailure|SessionEnd|SubagentStart|SessionStart
    python -m trirouter.hooks antigravity  PreInvocation|Stop
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from . import core, queue_state

LOG_FILE = core.STATE_DIR / "logs" / "routing.jsonl"
SUBAGENT_LOG = core.STATE_DIR / "logs" / "subagents.jsonl"
SEEN_FILE = core.STATE_DIR / "state" / "antigravity_seen.json"
SKIP_TAGS = ("#norouter", "#privat")
# Harness-injected pseudo-prompts (background task results, reminders, subagent hand-backs) are not
# user requests.
SYSTEM_PREFIXES = ("<task-notification", "<system-reminder", "[system notification", "<command-", "<local-command",
                   "caveat: the messages below", "<agent-message")
STOP_EVENTS = ("Stop", "StopFailure", "SessionEnd")
PURGE_EVERY_S = 6 * 3600  # SessionStart: at most one quarantine purge per 6 hours
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|xox[abp]-[\w-]{10,}"
                       r"|\b\d/0A[\w-]{20,}|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}|\b(?=[\w+=-]*\d)(?=[\w+=-]*[A-Za-z])[\w+=-]{32,}\b)")


def redact(text):
    return SECRET_RE.sub("[redacted]", text)


def log(entry, path=None):
    path = path or LOG_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _short_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else None


AGY_MODE_PREFIX = re.compile(r"^\s*/plan\b\s*")  # plan mode stores the prompt as "/plan <prompt>"


def _user_request(content):
    """Antigravity wraps the typed prompt in <USER_REQUEST> tags, followed by metadata."""
    _, opened, rest = content.partition("<USER_REQUEST>")
    body, closed, _ = rest.partition("</USER_REQUEST>")
    return AGY_MODE_PREFIX.sub("", body if opened and closed else content)


def _antigravity_prompt(payload):
    """(prompt, turn_key) of the latest user turn in the transcript, or (None, None)."""
    try:
        path = payload.get("transcriptPath")
        if not path:
            return None, None
        lines = [l for l in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "USER_INPUT":
                continue
            text = _user_request(entry.get("content") or "").strip()
            if text:
                return text, f"{payload.get('conversationId', '')}:{entry.get('step_index', len(lines))}"
    except Exception:  # noqa: BLE001 - a hook must never crash the host tool
        pass
    return None, None


def _first_time(turn_key):
    try:
        seen = json.loads(SEEN_FILE.read_text(encoding="utf-8")) if SEEN_FILE.exists() else []
    except (OSError, ValueError):
        seen = []
    if turn_key in seen:
        return False
    seen = (seen + [turn_key])[-200:]
    try:
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        SEEN_FILE.write_text(json.dumps(seen), encoding="utf-8")
    except OSError:
        pass
    return True


def extract_prompt(raw, provider):
    """(prompt, payload, turn_key), each possibly None."""
    try:
        payload = json.loads(raw.decode("utf-8-sig"))  # PowerShell 5.1 pipes can prepend a BOM
    except (ValueError, AttributeError):
        return None, None, None
    if not isinstance(payload, dict):
        return None, None, None
    if provider == "antigravity":
        prompt, key = _antigravity_prompt(payload)
        return prompt, payload, key
    prompt = payload.get("prompt")
    return (prompt.strip() if isinstance(prompt, str) else None), payload, None


def emit(text, hook_event_name, provider):
    # ASCII-escaped JSON is safe for any console code page
    if provider == "antigravity":
        print(json.dumps({"injectSteps": [{"ephemeralMessage": text}]}))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": hook_event_name, "additionalContext": text}}))


def _json_object(raw):
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
        return payload if isinstance(payload, dict) else {}
    except (ValueError, AttributeError):
        return {}


def _session_id(payload):
    if not payload:
        return None
    return payload.get("session_id") or payload.get("sessionId") or payload.get("conversationId")


def _transcript_mtime(payload):
    """The previous turn's last activity: the current prompt is usually not written yet."""
    path = (payload or {}).get("transcript_path") or (payload or {}).get("transcriptPath")
    try:
        return Path(path).stat().st_mtime if path else None
    except OSError:
        return None


def on_stop(provider, hook_event_name, raw):
    """Stop, StopFailure (the turn ended on an API error) and SessionEnd all release the queue."""
    payload = _json_object(raw)
    if payload.get("fullyIdle") is not False:  # Antigravity: background work may still run
        queue_state.on_stop(core.STATE_DIR, provider, _session_id(payload))
    if hook_event_name == "SessionEnd":
        queue_state.forget(core.STATE_DIR, provider, _session_id(payload))
    if provider == "antigravity":
        print("{}")  # Antigravity expects a JSON object; no "decision" allows the stop


def on_subagent_start(provider, raw):
    """Logs which worker the model actually delegated to; `doctor` compares it with the advice."""
    payload = _json_object(raw)
    if not payload.get("agent_type"):
        return
    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "provider": provider, "session": _short_hash(_session_id(payload)),
         "prompt_id": payload.get("prompt_id"), "agent_type": payload["agent_type"]}, SUBAGENT_LOG)


def on_session_start(provider, raw):
    """Purges expired quarantine entries, at most every 6 hours. Cheap, silent (a SessionStart hook's stdout
    becomes model context) and fully wrapped: it can never block or crash the hook."""
    try:
        stamp = core.STATE_DIR / "state" / "quarantine_purge.txt"
        try:
            last = float(stamp.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            last = 0.0
        if 0 <= time.time() - last < PURGE_EVERY_S:
            return
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(time.time()), encoding="utf-8")  # first: a slow purge is not repeated by a parallel session
        from . import skillscan  # lazy: the prompt hooks never need it
        skillscan.purge_expired(apply=True, say=None)
    except Exception:  # noqa: BLE001 - a hook must never crash the host tool
        pass


def on_prompt(provider, hook_event_name, raw):
    prompt, payload, turn_key = extract_prompt(raw, provider)
    if prompt is None:
        if payload is None:
            log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "provider": provider, "error": "stdin: not a JSON object",
                 "stdin_head": redact(raw[:80].decode("utf-8", "replace"))})
        return
    low = prompt.lstrip().lower()
    if len(prompt) < 3 or low.startswith(("/",) + SYSTEM_PREFIXES):
        return
    if provider == "antigravity" and turn_key and not _first_time(turn_key):
        return  # same user turn, later model call

    cwd = (payload.get("cwd") or (payload.get("workspacePaths") or [""])[0]) if payload else ""
    private = any(t in low for t in SKIP_TAGS)
    queue_text = queue_state.render(queue_state.on_submit(
        core.STATE_DIR, provider, _session_id(payload), cwd, "" if private else redact(" ".join(prompt.split())[:60]),
        last_activity=_transcript_mtime(payload)))
    if not private:
        emit(route_and_log(prompt, provider, payload, cwd, queue_text), hook_event_name, provider)
    elif queue_text:  # #norouter / #privat: never sent to TypeSafe, only queue-protected
        emit(queue_text, hook_event_name, provider)


def route_and_log(prompt, provider, payload, cwd, queue_text):
    """Never raises; the decision is logged redacted."""
    t0 = time.perf_counter()
    session_id = _session_id(payload)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "provider": provider, "cwd": cwd,
             "sha": _short_hash(prompt), "session": _short_hash(session_id), "prompt_id": (payload or {}).get("prompt_id"),
             "prompt": redact(prompt if os.environ.get("ROUTER_LOG_PROMPTS") == "1" else prompt[:200])}
    try:
        # Codex reports the session's model, so a tier can stay in-session
        session_model = payload.get("model") if provider == "codex" and isinstance(payload.get("model"), str) else None
        previous = queue_state.recall(core.STATE_DIR, provider, session_id)
        d, text, _, error = core.route(prompt, provider, session_model=session_model, previous=previous)
        if error:
            entry["error"] = error
        queue_state.remember(core.STATE_DIR, provider, session_id, d)
    except Exception as exc:  # e.g. a malformed routes.json: never block
        entry["error"] = f"{type(exc).__name__}: {exc}"[:200]
        entry["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        log(entry)
        return ("[router] unavailable; answer directly in this session."
                + (" SAFETY: ask for explicit confirmation before any irreversible action." if core.is_destructive(prompt) else "")
                + " " + core.lang.respond_line(core.language_of(prompt)))
    entry.update(d)
    entry["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    if queue_text:
        entry["queued"] = True
    log(entry)
    foreign_note = core.foreign_project_note(prompt, cwd)
    if foreign_note:
        text += f" Note: {foreign_note}."
    return text + (" " + queue_text if queue_text else "")


def main(argv=None):
    """Always 0: the router never blocks a prompt."""
    args = list(sys.argv[1:] if argv is None else argv)
    cloud_only = "--cloud-only" in args
    args = [a for a in args if a != "--cloud-only"]
    if len(args) != 2:
        print("Usage: python -m trirouter.hooks <claude|codex|antigravity> <hook_event_name> [--cloud-only]", file=sys.stderr)
    elif not cloud_only or core.is_cloud():  # locally the global hook already runs
        provider, hook_event_name = args
        raw = sys.stdin.buffer.read()
        if hook_event_name in STOP_EVENTS:
            on_stop(provider, hook_event_name, raw)
        elif hook_event_name == "SubagentStart":
            on_subagent_start(provider, raw)
        elif hook_event_name == "SessionStart":
            on_session_start(provider, raw)
        else:
            on_prompt(provider, hook_event_name, raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
