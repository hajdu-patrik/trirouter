"""The "before prompt" hook for Claude Code, Codex and Antigravity (`python -m trirouter.hooks <provider> <event>`,
always exit 0); the daemon calls handle(). Antigravity sends no prompt: it is read from the transcript."""
import functools
import hashlib
import json
import os
import re
import sys
import time

from . import __version__, core, platforms as P, queue_state, store
from .secret import redact

LOG_FILE = core.STATE_DIR / "logs" / "routing.jsonl"
SUBAGENT_LOG = core.STATE_DIR / "logs" / "subagents.jsonl"
GUARD_LOG = core.STATE_DIR / "logs" / "guard.jsonl"
LOG_MAX = 16 * 1024 * 1024  # a log file is cut to its newer half beyond this (routing.jsonl trains the local model)
CLAUDE_SETTINGS = P.PATHS["claude_settings"]  # only read: the fallback for the session's model
SEEN = store.Store("antigravity_seen", ttl=24 * 3600, limit=200)  # Antigravity turns already routed
SKIP_TAGS = ("#norouter", "#privat")
NESTED_ENV = "TRIROUTER_NESTED"  # set by cli-bridge: the CLI it runs is already routed by the calling session
# Harness-injected pseudo-prompts (background task results, reminders, subagent hand-backs) are not
# user requests.
SYSTEM_PREFIXES = ("<task-notification", "<system-reminder", "[system notification", "<command-", "<local-command",
                   "caveat: the messages below", "<agent-message")
STOP_EVENTS = ("Stop", "StopFailure", "SessionEnd")
PURGE_EVERY_S = 6 * 3600  # SessionStart: at most one quarantine purge per 6 hours


def log(entry, path=None):
    """One JSON line, appended under an OS file lock: hooks running in parallel (several subagents starting at once)
    never tear a line. Never raises."""
    try:
        P.append_line(path or LOG_FILE, json.dumps(entry, ensure_ascii=False), max_bytes=LOG_MAX)
    except (TypeError, ValueError):  # an entry that is not JSON-serializable
        pass


def guarded(default):
    """The function never raises: a hook must never crash the host tool. The error is logged by type and the
    function answers `default` instead."""
    def wrap(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                log({"ts": _now(), "error": f"{fn.__name__}: {type(exc).__name__}: {exc}"[:200]})
                return default
        return run
    return wrap


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _short_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else None


def _text(value):
    """A string field of the host's payload, or None: a list or number where a string belongs is no value."""
    return value if isinstance(value, str) and value else None


AGY_MODE_PREFIX = re.compile(r"^\s*/plan\b\s*")  # plan mode stores the prompt as "/plan <prompt>"
TRANSCRIPT_TAIL_BYTES = 256 * 1024


def _transcript_tail(path):
    """The lines of the transcript's last TRANSCRIPT_TAIL_BYTES, newest first (the first one is usually cut)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - TRANSCRIPT_TAIL_BYTES))
        return reversed(f.read().decode("utf-8", "replace").split("\n"))  # not splitlines: U+2028 is no line end


def _user_request(content):
    """Antigravity wraps the typed prompt in <USER_REQUEST> tags, followed by metadata."""
    _, opened, rest = content.partition("<USER_REQUEST>")
    body, closed, _ = rest.partition("</USER_REQUEST>")
    return AGY_MODE_PREFIX.sub("", body if opened and closed else content)


@guarded((None, None))
def _antigravity_prompt(payload):
    """(prompt, turn_key) of the latest user turn in the transcript, or (None, None)."""
    path = _text(payload.get("transcriptPath"))
    if not path:
        return None, None
    for line in _transcript_tail(path):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "USER_INPUT":
            continue
        text = _user_request(_text(entry.get("content")) or "").strip()
        if text:
            step = entry.get("step_index")
            return text, f"{payload.get('conversationId', '')}:{_short_hash(text) if step is None else step}"
    return None, None


def _first_time(turn_key):
    """PreInvocation fires before every model call of a turn: only the first call of a user turn is routed."""
    def mark(entry):
        first = not entry.get("routed")
        entry["routed"] = True
        return first
    return bool(SEEN.update(core.STATE_DIR, turn_key, mark, missing={})[0])


def extract_prompt(raw, provider):
    """(prompt, payload, turn_key), each possibly None."""
    payload = _json_object(raw)
    if payload is None:
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


def _print(out):
    if out:
        print(out)


def _json_object(raw, default=None):
    """The host's JSON input as a dict; `default` when it is not one."""
    try:
        payload = json.loads(raw.decode("utf-8-sig"))  # PowerShell 5.1 pipes can prepend a BOM
    except (ValueError, AttributeError):
        return default
    return payload if isinstance(payload, dict) else default


def _session_id(payload):
    if not payload:
        return None
    return _text(payload.get("session_id")) or _text(payload.get("sessionId")) or _text(payload.get("conversationId"))


def _transcript_path(payload):
    return _text((payload or {}).get("transcript_path")) or _text((payload or {}).get("transcriptPath"))


def stop_output(provider, hook_event_name, raw):
    """Stop, StopFailure (the turn ended on an API error) and SessionEnd all release the queue."""
    payload = _json_object(raw, {})
    if payload.get("fullyIdle") is not False:  # Antigravity: background work may still run
        queue_state.on_stop(core.STATE_DIR, provider, _session_id(payload))
    if hook_event_name == "SessionEnd":
        queue_state.forget(core.STATE_DIR, provider, _session_id(payload))
    return "{}" if provider == "antigravity" else ""  # Antigravity expects a JSON object; no "decision" allows the stop


def subagent_start_output(provider, raw):
    """Logs which worker the model actually delegated to; `doctor` compares it with the advice."""
    payload = _json_object(raw, {})
    if not _text(payload.get("agent_type")):
        return ""
    log({"ts": _now(), "provider": provider, "session": _short_hash(_session_id(payload)),
         "prompt_id": payload.get("prompt_id"), "agent_type": payload["agent_type"]}, SUBAGENT_LOG)
    return ""


@guarded(None)
def start_model_discovery():
    """The daily model check (discovery.py) in a detached background process when it is due; returns at once."""
    from . import discovery  # lazy: only its small scheduling part runs here
    discovery.maybe_start()


@guarded("")
def model_note():
    """The one-line note about models the last daily check added or removed (shown once), or ''."""
    from . import discovery
    return discovery.pending_note()


@guarded(None)
def start_daemon():
    """Claude with the http hook transport: the hook daemon must be up before the first prompt."""
    if core.hook_transport() == "http":
        from . import daemon  # lazy: the command transport never needs it
        daemon.ensure_running()


@guarded("")
def welcome_line(payload):
    """What a new session shows the user, or ''. Only a real start: not a resume, /clear or compaction."""
    if payload.get("source") != "startup" or os.environ.get("ROUTER_WELCOME") == "0" or os.environ.get(NESTED_ENV):
        return ""
    label = core.JEV_LABELS.get(core.jev_access(core.user_config())[0]) or "built-in local model"
    return f"Welcome to trirouter {__version__}: every prompt is routed, decisions by {label}. #norouter skips one."


def session_start_output(provider, raw):
    """Starts the hook daemon (http transport) and the daily model check when due, tells the session once what the check
    changed, starts the quarantine purge, greets the user. Stdout's context part reaches the model, systemMessage does
    not, so the greeting costs no tokens."""
    payload = _json_object(raw, {})
    if provider == "claude":
        start_daemon()
    start_model_discovery()
    if provider == "claude":
        record_session_model(payload, "model")
    note = model_note()
    _purge_quarantine()
    out = json.loads(render(note, "SessionStart", provider)) if note else {}
    greeting = welcome_line(payload) if provider == "claude" else ""
    if greeting:
        out["systemMessage"] = greeting
    return json.dumps(out) if out else ""


@guarded(None)
def _purge_quarantine():
    """The purge runs detached (skillscan.main): deleting a large quarantined skill takes seconds, and a SessionStart
    hook must answer at once. The stamp is written first, so a parallel session does not start a second purge."""
    stamp = core.STATE_DIR / "state" / "quarantine_purge.txt"
    try:
        last = float(stamp.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        last = 0.0
    if 0 <= time.time() - last < PURGE_EVERY_S:
        return
    P.atomic_write(stamp, str(time.time()), mode=0o600)
    P.spawn_detached([P.python_exe_windowless(), "-m", "trirouter.skillscan", "--purge-expired"], cwd=str(P.REPO))


def prompt_output(provider, hook_event_name, raw):
    if os.environ.get(NESTED_ENV):
        return ""  # a CLI run by cli-bridge: the calling session was routed, its sub-call is not
    if provider != "claude":  # Codex and Antigravity have no SessionStart hook: the prompt hook starts the daily check
        start_model_discovery()
    prompt, payload, turn_key = extract_prompt(raw, provider)
    if prompt is None:
        if payload is None:
            log({"ts": _now(), "provider": provider, "error": "stdin: not a JSON object",
                 "stdin_head": redact(raw[:2000].decode("utf-8", "replace"))[:80]})
        return ""
    low = prompt.lstrip().lower()
    if len(prompt) < 3 or low.startswith(("/",) + SYSTEM_PREFIXES):
        return ""
    if provider == "antigravity" and turn_key and not _first_time(turn_key):
        return ""  # same user turn, later model call

    workspaces = payload.get("workspacePaths")
    cwd = _text(payload.get("cwd")) or (_text(workspaces[0]) if isinstance(workspaces, list) and workspaces else None) or ""
    private = any(t in low for t in SKIP_TAGS)
    queue = queue_state.on_submit(core.STATE_DIR, provider, _session_id(payload), cwd,
                                  "" if private else redact(" ".join(prompt.split()))[:60], _transcript_path(payload))
    if not private:
        return render(route_and_log(prompt, provider, payload, cwd, queue), hook_event_name, provider)
    queue_text = queue_state.render(queue)  # #norouter / #privat: never sent to TypeSafe, only queue-protected
    return render(queue_text, hook_event_name, provider) if queue_text else ""


def route_and_log(prompt, provider, payload, cwd, queue):
    """Never raises; the decision is logged redacted, with why the prompt was queued (earlier requests of this
    session, other sessions in the folder) and whether the safety check fired."""
    t0 = time.perf_counter()
    session_id = _session_id(payload)
    entry = {"ts": _now(), "provider": provider, "cwd": cwd,
             "sha": _short_hash(prompt), "session": _short_hash(session_id), "prompt_id": payload.get("prompt_id"),
             "prompt": redact(prompt) if os.environ.get("ROUTER_LOG_PROMPTS") == "1" else redact(prompt)[:200]}
    try:
        # Codex reports the session's model, Claude's is recorded per session: a tier on that model stays in-session
        session_model = _text(payload.get("model")) if provider == "codex" else None
        if provider == "claude":
            session_model = claude_session_model(payload)
            if session_model:
                entry["session_model"] = session_model
        previous = queue_state.recall(core.STATE_DIR, provider, session_id)
        d, text, hit, error = core.route(prompt, provider, session_model=session_model, previous=previous)
        if error:
            entry["error"] = error
        queue_state.remember(core.STATE_DIR, provider, session_id, d)
        if provider == "claude":
            remember_agent_budget(payload, d)
    except Exception as exc:  # e.g. a malformed routes.json: never block
        entry["error"] = f"{type(exc).__name__}: {exc}"[:200]
        text = ("[router] unavailable; answer directly in this session."
                + (" SAFETY: ask for explicit confirmation before any irreversible action." if core.is_destructive(prompt) else "")
                + " " + core.lang.respond_line(core.language_of(prompt)))
    else:
        entry.update(d, destructive=hit)
    if queue["ahead"] or queue["others"]:
        entry["queued"] = {"ahead": len(queue["ahead"]), "others": len(queue["others"])}
    entry["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    log(entry)
    foreign_note = core.foreign_project_note(prompt, cwd)
    if foreign_note:
        text += f" Note: {foreign_note}."
    queue_text = queue_state.render(queue)  # the queue protection survives a routing failure
    return text + (" " + queue_text if queue_text else "")


@guarded("")
def handle(provider, hook_event_name, raw):
    """The hook's output for one event (raw: the host's JSON input as bytes): a JSON string, or "" for none."""
    if hook_event_name in STOP_EVENTS:
        return stop_output(provider, hook_event_name, raw)
    by_event = {"SubagentStart": subagent_start_output, "SessionStart": session_start_output,
                "PostModelSwitch": model_switch_output, "PreToolUse": pre_tool_use_output}
    if hook_event_name in by_event:
        return by_event[hook_event_name](provider, raw)
    return prompt_output(provider, hook_event_name, raw)


AGENT_TOOLS = ("Agent", "Task")  # the subagent tool's current and former name


@guarded(None)
def record_session_model(payload, key):
    """SessionStart (`model`, not always sent) and PostModelSwitch (`to_model`): records the session's Claude family.
    A switch to something that names no family forgets the record, so the fallbacks apply."""
    if key in payload:
        queue_state.remember_model(core.STATE_DIR, "claude", _session_id(payload), core.claude_family(payload[key]))


@guarded(None)
def _recorded_family(payload):
    return queue_state.session_model(core.STATE_DIR, "claude", _session_id(payload))


@guarded(None)
def _transcript_family(payload):
    """The family of the newest main-session assistant message in the transcript tail, or None."""
    path = _text(payload.get("transcript_path"))
    if not path:
        return None
    for line in _transcript_tail(path):
        if '"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if entry.get("type") == "assistant" and not entry.get("isSidechain") and isinstance(message, dict):
            family = core.claude_family(message.get("model"))  # "<synthetic>" (an error turn) names none
            if family:
                return family
    return None


@guarded(None)
def _settings_family():
    return core.claude_family(P.read_json(CLAUDE_SETTINGS, {}, "utf-8-sig", dict).get("model"))


def claude_session_model(payload):
    """The Claude family this session runs: recorded at SessionStart / PostModelSwitch, else the newest assistant
    message of the transcript, else settings.json `model`; None when unknown."""
    return _recorded_family(payload) or _transcript_family(payload) or _settings_family()


def model_switch_output(provider, raw):
    """PostModelSwitch (`from_model` / `to_model`): the session now runs another family."""
    if provider == "claude":
        record_session_model(_json_object(raw, {}), "to_model")
    return ""


def agent_budget(d):
    """How many subagents a decision allows: its extra parallel agents plus the worker it delegates to."""
    return int(d.get("extra_agents") or 0) + (1 if d.get("target_agent") else 0)


@guarded(None)
def remember_agent_budget(payload, d):
    """Stores the decision's subagent budget and worker family under the prompt_id for the PreToolUse guard."""
    prompt_id = _text(payload.get("prompt_id"))
    if prompt_id:
        queue_state.remember_prompt(core.STATE_DIR, prompt_id, {
            "budget": agent_budget(d), "agent": d.get("target_agent"),
            "family": core.claude_family(d.get("target_model")) if d.get("target_agent") else None})


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


@guarded(None)
def agent_guard(payload):
    """PreToolUse for the Agent tool -> the hook output (a dict) or None. Over the prompt's subagent budget the first
    call is denied; a general-purpose call without a model gets the routed worker's family."""
    if not isinstance(payload, dict) or payload.get("agent_id") or payload.get("tool_name") not in AGENT_TOOLS:
        return None
    prompt_id = _text(payload.get("prompt_id"))
    if not prompt_id or not guard_enabled():
        return None
    verdict, entry = queue_state.update_prompt(core.STATE_DIR, prompt_id, _count_agent_call)
    if verdict is None:
        return None  # no router decision for this prompt (not routed, #norouter, expired)
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    agent_type = tool_input.get("subagent_type") or "general-purpose"
    record = {"ts": _now(), "session": _short_hash(_session_id(payload)), "prompt_id": prompt_id,
              "agent_type": agent_type, "budget": entry.get("budget"), "calls": entry.get("calls")}
    if verdict == "deny":
        log({**record, "action": "deny"}, GUARD_LOG)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": deny_text(entry.get("budget", 0))}}
    family = entry.get("family")
    if family and agent_type == "general-purpose" and not tool_input.get("model"):
        log({**record, "action": "model", "model": family}, GUARD_LOG)
        # No permissionDecision: the normal rules still apply and the model in updatedInput takes effect without an
        # "allow". updatedInput replaces the whole input, hence the copy of every original field.
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {**tool_input, "model": family}}}
    return None


def pre_tool_use_output(provider, raw):
    out = agent_guard(_json_object(raw)) if provider == "claude" else None
    return json.dumps(out) if out else ""


@guarded(0)
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
