#!/usr/bin/env python3
"""Shared "before prompt" hook for Claude Code, Codex CLI and Antigravity CLI. Always exits 0.

Antigravity has no prompt-submit event, only PreInvocation, which fires before every model call of a
turn and carries no prompt text. So the latest user input is read back from the transcript, and the
context is injected only once per user turn.

    python -m trirouter.hooks claude|codex UserPromptSubmit|Stop [--cloud-only]
    python -m trirouter.hooks claude StopFailure|SessionEnd|SubagentStart|SessionStart|PostModelSwitch|PreToolUse
    python -m trirouter.hooks antigravity  PreInvocation|Stop

The handlers return their output (the JSON the host reads from stdout, or ""); main() prints it. The
daemon (daemon.py) calls handle() directly, from several threads, for Claude's http hooks.
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from . import core, platforms as P, queue_state

LOG_FILE = core.STATE_DIR / "logs" / "routing.jsonl"
SUBAGENT_LOG = core.STATE_DIR / "logs" / "subagents.jsonl"
GUARD_LOG = core.STATE_DIR / "logs" / "guard.jsonl"
CLAUDE_SETTINGS = P.PATHS["claude_settings"]  # only read: the fallback for the session's model
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
    """One JSON line, appended under an OS file lock: hooks running in parallel (several subagents starting at once)
    never tear a line. Never raises."""
    try:
        P.append_line(path or LOG_FILE, json.dumps(entry, ensure_ascii=False))
    except (TypeError, ValueError):  # an entry that is not JSON-serializable
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


def render(text, hook_event_name, provider):
    """The hook output that injects `text` as context; ASCII-escaped JSON is safe for any console code page."""
    if provider == "antigravity":
        return json.dumps({"injectSteps": [{"ephemeralMessage": text}]})
    return json.dumps({"hookSpecificOutput": {"hookEventName": hook_event_name, "additionalContext": text}})


def emit(text, hook_event_name, provider):
    print(render(text, hook_event_name, provider))


def _print(out):
    if out:
        print(out)


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


def stop_output(provider, hook_event_name, raw):
    """Stop, StopFailure (the turn ended on an API error) and SessionEnd all release the queue."""
    payload = _json_object(raw)
    if payload.get("fullyIdle") is not False:  # Antigravity: background work may still run
        queue_state.on_stop(core.STATE_DIR, provider, _session_id(payload))
    if hook_event_name == "SessionEnd":
        queue_state.forget(core.STATE_DIR, provider, _session_id(payload))
    return "{}" if provider == "antigravity" else ""  # Antigravity expects a JSON object; no "decision" allows the stop


def on_stop(provider, hook_event_name, raw):
    _print(stop_output(provider, hook_event_name, raw))


def subagent_start_output(provider, raw):
    """Logs which worker the model actually delegated to; `doctor` compares it with the advice."""
    payload = _json_object(raw)
    if not payload.get("agent_type"):
        return ""
    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "provider": provider, "session": _short_hash(_session_id(payload)),
         "prompt_id": payload.get("prompt_id"), "agent_type": payload["agent_type"]}, SUBAGENT_LOG)
    return ""


def on_subagent_start(provider, raw):
    _print(subagent_start_output(provider, raw))


def start_model_discovery():
    """The daily model check (discovery.py) in a detached background process when it is due; returns at once."""
    try:
        from . import discovery  # lazy: only its small scheduling part runs here
        discovery.maybe_start()
    except Exception:  # noqa: BLE001 - a hook must never crash the host tool
        pass


def model_note():
    """The one-line note about models the last daily check added or removed (shown once), or ''."""
    try:
        from . import discovery
        return discovery.pending_note()
    except Exception:  # noqa: BLE001
        return ""


def start_daemon():
    """Claude with the http hook transport: the hook daemon must be up before the first prompt. Never raises."""
    try:
        if (core.user_config().get("hooks") or {}).get("transport") == "http":
            from . import daemon  # lazy: the command transport never needs it
            daemon.ensure_running()
    except Exception:  # noqa: BLE001 - a hook must never crash the host tool
        pass


def session_start_output(provider, raw):
    """Starts the hook daemon (http transport) and the daily model check when due, tells the session once what
    the check changed, and purges expired quarantine entries at most every 6 hours. Cheap, silent unless the
    model list changed (a SessionStart hook's stdout becomes model context) and fully wrapped: it can never
    block or crash the hook."""
    if provider == "claude":
        start_daemon()
    start_model_discovery()
    if provider == "claude":
        record_session_model(_json_object(raw), "model")
    out = ""
    note = model_note()
    if note:
        try:
            out = render(note, "SessionStart", provider)
        except Exception:  # noqa: BLE001
            pass
    _purge_quarantine()
    return out


def on_session_start(provider, raw):
    _print(session_start_output(provider, raw))


def _purge_quarantine():
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


def prompt_output(provider, hook_event_name, raw):
    if provider != "claude":  # Codex and Antigravity have no SessionStart hook: the prompt hook starts the daily check
        start_model_discovery()
    prompt, payload, turn_key = extract_prompt(raw, provider)
    if prompt is None:
        if payload is None:
            log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "provider": provider, "error": "stdin: not a JSON object",
                 "stdin_head": redact(raw[:80].decode("utf-8", "replace"))})
        return ""
    low = prompt.lstrip().lower()
    if len(prompt) < 3 or low.startswith(("/",) + SYSTEM_PREFIXES):
        return ""
    if provider == "antigravity" and turn_key and not _first_time(turn_key):
        return ""  # same user turn, later model call

    cwd = (payload.get("cwd") or (payload.get("workspacePaths") or [""])[0]) if payload else ""
    private = any(t in low for t in SKIP_TAGS)
    queue_text = queue_state.render(queue_state.on_submit(
        core.STATE_DIR, provider, _session_id(payload), cwd, "" if private else redact(" ".join(prompt.split())[:60]),
        last_activity=_transcript_mtime(payload)))
    if not private:
        return render(route_and_log(prompt, provider, payload, cwd, queue_text), hook_event_name, provider)
    if queue_text:  # #norouter / #privat: never sent to TypeSafe, only queue-protected
        return render(queue_text, hook_event_name, provider)
    return ""


def on_prompt(provider, hook_event_name, raw):
    _print(prompt_output(provider, hook_event_name, raw))


def route_and_log(prompt, provider, payload, cwd, queue_text):
    """Never raises; the decision is logged redacted."""
    t0 = time.perf_counter()
    session_id = _session_id(payload)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "provider": provider, "cwd": cwd,
             "sha": _short_hash(prompt), "session": _short_hash(session_id), "prompt_id": (payload or {}).get("prompt_id"),
             "prompt": redact(prompt if os.environ.get("ROUTER_LOG_PROMPTS") == "1" else prompt[:200])}
    try:
        # Codex reports the session's model, Claude's is recorded per session: a tier on that model stays in-session
        session_model = payload.get("model") if provider == "codex" and isinstance(payload.get("model"), str) else None
        if provider == "claude":
            session_model = claude_session_model(payload)
            if session_model:
                entry["session_model"] = session_model
        previous = queue_state.recall(core.STATE_DIR, provider, session_id)
        d, text, _, error = core.route(prompt, provider, session_model=session_model, previous=previous)
        if error:
            entry["error"] = error
        queue_state.remember(core.STATE_DIR, provider, session_id, d)
        if provider == "claude":
            remember_agent_budget(payload, d)
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


def handle(provider, hook_event_name, raw):
    """The hook's output for one event (raw: the host's JSON input as bytes): a JSON string, or "" for none."""
    if hook_event_name in STOP_EVENTS:
        return stop_output(provider, hook_event_name, raw)
    if hook_event_name == "SubagentStart":
        return subagent_start_output(provider, raw)
    if hook_event_name == "SessionStart":
        return session_start_output(provider, raw)
    if hook_event_name == "PostModelSwitch":
        return model_switch_output(provider, raw)
    if hook_event_name == "PreToolUse":
        return pre_tool_use_output(provider, raw)
    return prompt_output(provider, hook_event_name, raw)


# ---- Claude: the session's model and the subagent guard ---------------------------------------------------------

AGENT_TOOLS = ("Agent", "Task")  # the subagent tool's current and former name
TRANSCRIPT_TAIL_BYTES = 256 * 1024


def record_session_model(payload, key):
    """SessionStart (`model`, not always sent) and PostModelSwitch (`to_model`): records the session's Claude family.
    A switch to something that names no family forgets the record, so the fallbacks apply. Never raises."""
    try:
        if key in payload:
            queue_state.remember_model(core.STATE_DIR, "claude", _session_id(payload), core.claude_family(payload[key]))
    except Exception:  # noqa: BLE001 - a hook must never crash the host tool
        pass


def _transcript_family(payload):
    """The family of the newest main-session assistant message in the transcript tail, or None."""
    path = (payload or {}).get("transcript_path")
    if not path:
        return None
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - TRANSCRIPT_TAIL_BYTES))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        if '"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:  # the first line of the tail is usually cut
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        if entry.get("type") == "assistant" and not entry.get("isSidechain") and isinstance(message, dict):
            family = core.claude_family(message.get("model"))  # "<synthetic>" (an error turn) names none
            if family:
                return family
    return None


def _settings_family():
    try:
        return core.claude_family(json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8-sig")).get("model"))
    except (OSError, ValueError, AttributeError):
        return None


def claude_session_model(payload):
    """The Claude family this session runs: recorded at SessionStart / PostModelSwitch, else the newest assistant
    message of the transcript, else the `model` of ~/.claude/settings.json; None when unknown. Never raises."""
    for source in (lambda: queue_state.session_model(core.STATE_DIR, "claude", _session_id(payload)),
                   lambda: _transcript_family(payload), _settings_family):
        try:
            family = source()
        except Exception:  # noqa: BLE001 - an unreadable source falls through to the next one
            family = None
        if family:
            return family
    return None


def model_switch_output(provider, raw):
    """PostModelSwitch (`from_model` / `to_model`): the session now runs another family."""
    if provider == "claude":
        record_session_model(_json_object(raw), "to_model")
    return ""


def on_model_switch(provider, raw):
    _print(model_switch_output(provider, raw))


def agent_budget(d):
    """How many subagents a decision allows: its extra parallel agents plus the worker it delegates to."""
    return int(d.get("extra_agents") or 0) + (1 if d.get("target_agent") else 0)


def remember_agent_budget(payload, d):
    """Stores the decision's subagent budget and worker family under the prompt_id for the PreToolUse guard."""
    try:
        prompt_id = (payload or {}).get("prompt_id")
        if prompt_id:
            queue_state.remember_prompt(core.STATE_DIR, prompt_id, {
                "budget": agent_budget(d), "agent": d.get("target_agent"),
                "family": core.claude_family(d.get("target_model")) if d.get("target_agent") else None})
    except Exception:  # noqa: BLE001 - a hook must never crash the host tool
        pass


def guard_enabled():
    """config.json {"guard": {"agents": false}} turns the subagent guard off; it is on by default."""
    guard = core.user_config().get("guard")
    return not (isinstance(guard, dict) and guard.get("agents") is False)


def _count_agent_call(entry):
    """One Agent call against the prompt's budget. Over the budget the first time: 'deny' (not counted, it starts
    nothing); every later call passes, so a retry the user asked for goes through."""
    calls = int(entry.get("calls") or 0) + 1
    if calls > int(entry.get("budget") or 0) and not entry.get("warned"):
        entry["warned"] = True
        return "deny"
    entry["calls"] = calls
    return "allow"


def deny_text(budget):
    return (f"[router] The router allowed {budget} subagent(s) for this request, and this call would exceed that. "
            "Do the work inline in this session or within that budget. If the user explicitly asked for (more) "
            "agents, simply call the tool again: the retry is allowed.")


def agent_guard(payload):
    """PreToolUse for the Agent tool (Claude) -> the hook output (a dict to print as JSON), or None to let the call
    through unchanged. Over the prompt's subagent budget the first call is denied with a reason; a general-purpose
    call that names no model gets the routed worker's family (updatedInput replaces the whole input, so every
    original field is copied). Calls inside a subagent, unknown prompts and a disabled guard pass. Never raises."""
    try:
        if not isinstance(payload, dict) or payload.get("agent_id") or payload.get("tool_name") not in AGENT_TOOLS:
            return None
        prompt_id = payload.get("prompt_id")
        if not prompt_id or not guard_enabled():
            return None
        verdict, entry = queue_state.update_prompt(core.STATE_DIR, prompt_id, _count_agent_call)
        if verdict is None:
            return None  # no router decision for this prompt (not routed, #norouter, expired)
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        agent_type = tool_input.get("subagent_type") or "general-purpose"
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": _short_hash(_session_id(payload)),
                  "prompt_id": prompt_id, "agent_type": agent_type, "budget": entry.get("budget"),
                  "calls": entry.get("calls")}
        if verdict == "deny":
            log({**record, "action": "deny"}, GUARD_LOG)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": deny_text(entry.get("budget", 0))}}
        family = entry.get("family")
        if family and agent_type == "general-purpose" and not tool_input.get("model"):
            log({**record, "action": "model", "model": family}, GUARD_LOG)
            # no permissionDecision: the normal permission rules still apply to the call (verified live: the
            # subagent runs on the model in updatedInput without an "allow")
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {**tool_input, "model": family}}}
    except Exception:  # noqa: BLE001 - a hook must never crash the host tool
        pass
    return None


def pre_tool_use_output(provider, raw):
    out = agent_guard(_json_object(raw)) if provider == "claude" else None
    return json.dumps(out) if out else ""


def on_pre_tool_use(provider, raw):
    _print(pre_tool_use_output(provider, raw))


def main(argv=None):
    """Always 0: the router never blocks a prompt."""
    args = list(sys.argv[1:] if argv is None else argv)
    cloud_only = "--cloud-only" in args
    args = [a for a in args if a != "--cloud-only"]
    if len(args) != 2:
        print("Usage: python -m trirouter.hooks <claude|codex|antigravity> <hook_event_name> [--cloud-only]", file=sys.stderr)
    elif not cloud_only or core.is_cloud():  # locally the global hook already runs
        provider, hook_event_name = args
        _print(handle(provider, hook_event_name, sys.stdin.buffer.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
