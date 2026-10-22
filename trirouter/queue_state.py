"""Queue protection: a new prompt never stops or overwrites work in flight (QUEUE: / CONCURRENCY: context); entries
expire after a TTL or a silent transcript. Also the per-session state: last decision, model, subagent budget."""
import os
import time

from . import platforms as P, store


def _minutes(var, default):
    return 60 * P.env_number(var, default)


TTL_S = _minutes("ROUTER_QUEUE_TTL_MIN", 120)
IDLE_S = _minutes("ROUTER_QUEUE_IDLE_MIN", 10)

INFLIGHT = store.Store("inflight")  # {key: [entries]}: pruned per entry by on_submit, so no ttl of its own


def _fresh_entries(value, now):
    """The entries of one session still within TTL_S; whatever else sits in the file (a number, a list of strings,
    an entry with a text stamp) is dropped, so a damaged file heals on the next prompt."""
    return [e for e in value if store.stamp(e) is not None and now - e["ts"] < TTL_S] if isinstance(value, list) else []


def _idle(transcript, now):
    """True when the session's transcript has been silent for IDLE_S: its turn was cancelled or crashed (Claude runs no
    Stop hook on Esc)."""
    try:
        return bool(transcript) and now - os.stat(transcript).st_mtime > IDLE_S
    except (OSError, TypeError):
        return False


def _prune(data, now, key, transcript):
    """The sessions still in flight: expired entries and silent sessions (this one by `transcript`, the others by
    the transcript each recorded) are dropped."""
    fresh = {}
    for k, entries in data.items():
        entries = _fresh_entries(entries, now)
        if entries and not _idle(transcript if k == key else entries[0].get("transcript"), now):
            fresh[k] = entries
    return fresh


def on_submit(state_dir, provider, session_id, cwd, summary, transcript=None):
    """{"ahead": unfinished entries of this session, "others": other sessions in the same folder}. `transcript` is
    stored with the entry, so other sessions can tell a silent session from a working one."""
    if not session_id:
        return {"ahead": [], "others": []}
    path = INFLIGHT.path(state_dir)
    now = time.time()
    key = f"{provider}:{session_id}"
    norm_cwd = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
    with INFLIGHT.lock(path) as lock:
        data = _prune(store.load(path), now, key, transcript)
        ahead = list(data.get(key, []))
        others = [e for k, v in data.items() if k != key for e in v[:1] if norm_cwd and e.get("cwd") == norm_cwd]
        entry = {"ts": now, "cwd": norm_cwd, "summary": summary, "provider": provider}
        if transcript:
            entry["transcript"] = str(transcript)
        data.setdefault(key, []).append(entry)
        if lock.held:
            store.save(path, data)
    return {"ahead": ahead, "others": others}


def on_stop(state_dir, provider, session_id):
    if session_id:
        INFLIGHT.pop(state_dir, f"{provider}:{session_id}")


LAST_KEYS = ("task", "task_conf", "level", "primary", "verify", "skill", "skill_path", "skill_native", "effort",
             "model", "extra_agents", "lang", "target_agent")
LAST = store.Store("last_decision", ttl=_minutes("ROUTER_CONTINUATION_TTL_MIN", 180))


def remember(state_dir, provider, session_id, decision):
    """The session's latest decision, so a following "mehet" can continue it."""
    if session_id:
        LAST.put(state_dir, f"{provider}:{session_id}", {k: decision[k] for k in LAST_KEYS if k in decision})


def recall(state_dir, provider, session_id):
    return LAST.get(state_dir, f"{provider}:{session_id}") if session_id else None


def forget(state_dir, provider, session_id):
    if session_id:
        LAST.pop(state_dir, f"{provider}:{session_id}")


# ---- the model a session runs (Claude: SessionStart / PostModelSwitch) --------------------------------------

# a session can stay open for days; a switch re-records it
MODELS = store.Store("session_models", ttl=7 * 24 * 3600, limit=200)


def remember_model(state_dir, provider, session_id, model):
    """Records the model (Claude: the family) a session runs; None forgets it (a switch to an unknown model).
    Bounded to the 200 newest sessions."""
    if session_id:
        MODELS.put(state_dir, f"{provider}:{session_id}", {"model": model} if model else None)


def session_model(state_dir, provider, session_id):
    """The recorded model of the session, or None."""
    entry = MODELS.get(state_dir, f"{provider}:{session_id}") if session_id else None
    return (entry or {}).get("model") or None


# ---- per-prompt subagent budget (Claude PreToolUse guard) ---------------------------------------------------

PROMPTS = store.Store("agent_budget", ttl=12 * 3600, limit=300)


def remember_prompt(state_dir, prompt_id, info):
    """Stores `info` (the routed decision's budget and target) for a prompt_id, with a fresh call counter. Bounded to
    the 300 newest prompts; entries expire after 12 hours."""
    if prompt_id:
        PROMPTS.put(state_dir, prompt_id, {**info, "calls": 0, "warned": False})


def update_prompt(state_dir, prompt_id, change):
    """Runs change(entry) on the prompt's stored entry (see store.Store.update). Two hooks racing past the lock's wait
    both proceed: a missed count is acceptable, a blocked tool call is not."""
    return PROMPTS.update(state_dir, prompt_id, change) if prompt_id else (None, None)


def render(info):
    parts = []
    if info.get("ahead"):
        first = info["ahead"][0].get("summary") or "an earlier request"
        n = len(info["ahead"])
        parts.append(f"QUEUE: {n} earlier request(s) in this session are still in progress (oldest: \"{first}\"). "
                     "Do not stop, restart, undo or overwrite that work. Finish it first, then handle this request.")
    if info.get("others"):
        e = info["others"][0]
        parts.append(f"CONCURRENCY: another {e.get('provider', 'agent')} session is working in this folder "
                     f"(\"{e.get('summary') or 'unknown task'}\"). Do not modify or revert files it is changing; "
                     "if you must touch the same files, ask the user first.")
    return " ".join(parts)
