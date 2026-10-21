#!/usr/bin/env python3
"""Queue protection: a new prompt must never stop, restart or overwrite work still in progress.

The tools queue messages typed while the agent is busy, but never tell the model that earlier work is
unfinished. This module tracks work in flight and adds QUEUE: / CONCURRENCY: context. Entries expire
after a TTL, or when the session's transcript goes quiet: Claude runs no Stop hook on Esc, and a
cancelled or crashed turn must never make the next prompt resume old work.

It also keeps the other small per-session hook state, each file bounded, expiring and replaced atomically: the
last decision (a go-ahead continues it), the model a Claude session runs, and each prompt's subagent budget
for the PreToolUse agent guard.
"""
import json
import os
import time
from pathlib import Path

from . import platforms as P

def _minutes(var, default):
    try:
        return 60 * float(os.environ.get(var, default))
    except ValueError:
        return 60 * float(default)


TTL_S = _minutes("ROUTER_QUEUE_TTL_MIN", "120")
IDLE_S = _minutes("ROUTER_QUEUE_IDLE_MIN", "10")
LOCK_WAIT_S = 2.0


def _paths(state_dir):
    d = Path(state_dir) / "state"
    return d / "inflight.json", d / "inflight.lock"


class _Lock:
    """O_EXCL lock file; one older than 10 s is considered abandoned."""

    def __init__(self, path):
        self.path = path
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + LOCK_WAIT_S
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 10:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    return self  # never block a prompt: proceed without the lock
                time.sleep(0.05)

    def __exit__(self, *exc):
        if not self.acquired:
            return  # never remove a lock someone else holds
        try:
            self.path.unlink()
        except OSError:
            pass


def _save(path, data):
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _load(path):
    return P.read_json(path, {}, kind=dict)


def _prune(data, now):
    fresh = {key: [e for e in entries if now - e.get("ts", 0) < TTL_S] for key, entries in data.items()}
    return {key: entries for key, entries in fresh.items() if entries}


def on_submit(state_dir, provider, session_id, cwd, summary, last_activity=None):
    """{"ahead": unfinished entries of this session, "others": other sessions in the same folder}.
    last_activity: the transcript's mtime; a session silent for IDLE_S had its turn cancelled."""
    if not session_id:
        return {"ahead": [], "others": []}
    path, lock = _paths(state_dir)
    now = time.time()
    key = f"{provider}:{session_id}"
    norm_cwd = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
    with _Lock(lock):
        data = _prune(_load(path), now)
        if last_activity is not None and now - last_activity > IDLE_S:
            data.pop(key, None)
        ahead = list(data.get(key, []))
        others = [e for k, v in data.items() if k != key for e in v[:1] if norm_cwd and e.get("cwd") == norm_cwd]
        data.setdefault(key, []).append({"ts": now, "cwd": norm_cwd, "summary": summary, "provider": provider})
        _save(path, data)
    return {"ahead": ahead, "others": others}


def on_stop(state_dir, provider, session_id):
    if not session_id:
        return
    path, lock = _paths(state_dir)
    with _Lock(lock):
        data = _load(path)
        if data.pop(f"{provider}:{session_id}", None) is not None:
            _save(path, data)


LAST_TTL_S = _minutes("ROUTER_CONTINUATION_TTL_MIN", "180")
LAST_KEYS = ("task", "task_conf", "level", "primary", "verify", "skill", "skill_path", "skill_native", "effort",
             "model", "extra_agents", "lang", "target_agent")


def _last_path(state_dir):
    return Path(state_dir) / "state" / "last_decision.json"


def remember(state_dir, provider, session_id, decision):
    """The session's latest decision, so a following "mehet" can continue it."""
    if not session_id:
        return
    path = _last_path(state_dir)
    now = time.time()
    with _Lock(path.with_suffix(".lock")):
        data = {k: v for k, v in _load(path).items() if now - v.get("ts", 0) < LAST_TTL_S}
        data[f"{provider}:{session_id}"] = {"ts": now, **{k: decision[k] for k in LAST_KEYS if k in decision}}
        _save(path, data)


def recall(state_dir, provider, session_id):
    if not session_id:
        return None
    entry = _load(_last_path(state_dir)).get(f"{provider}:{session_id}")
    if not isinstance(entry, dict) or time.time() - entry.get("ts", 0) >= LAST_TTL_S:
        return None
    return entry


def forget(state_dir, provider, session_id):
    if not session_id:
        return
    path = _last_path(state_dir)
    with _Lock(path.with_suffix(".lock")):
        data = _load(path)
        if data.pop(f"{provider}:{session_id}", None) is not None:
            _save(path, data)


# ---- the model a session runs (Claude: SessionStart / PostModelSwitch) --------------------------------------

MODEL_TTL_S = 7 * 24 * 3600  # a session can stay open for days; a switch re-records it
MODEL_MAX = 200


def _models_path(state_dir):
    return Path(state_dir) / "state" / "session_models.json"


def remember_model(state_dir, provider, session_id, model):
    """Records the model (Claude: the family) a session runs; None forgets it (a switch to an unknown model).
    Bounded to the MODEL_MAX newest sessions."""
    if not session_id:
        return
    path = _models_path(state_dir)
    now = time.time()
    key = f"{provider}:{session_id}"
    with _Lock(path.with_suffix(".lock")):
        data = {k: v for k, v in _load(path).items() if isinstance(v, dict) and now - v.get("ts", 0) < MODEL_TTL_S}
        if model:
            data[key] = {"ts": now, "model": model}
        elif data.pop(key, None) is None:
            return
        _save(path, dict(sorted(data.items(), key=lambda kv: kv[1]["ts"])[-MODEL_MAX:]))


def session_model(state_dir, provider, session_id):
    """The recorded model of the session, or None."""
    if not session_id:
        return None
    entry = _load(_models_path(state_dir)).get(f"{provider}:{session_id}")
    if not isinstance(entry, dict) or time.time() - entry.get("ts", 0) >= MODEL_TTL_S:
        return None
    return entry.get("model") or None


# ---- per-prompt subagent budget (Claude PreToolUse guard) ---------------------------------------------------

PROMPT_TTL_S = 12 * 3600
PROMPT_MAX = 300


def _prompts_path(state_dir):
    return Path(state_dir) / "state" / "agent_budget.json"


def remember_prompt(state_dir, prompt_id, info):
    """Stores `info` (the routed decision's budget and target) for a prompt_id, with a fresh call counter. Bounded to
    the PROMPT_MAX newest prompts; entries expire after PROMPT_TTL_S."""
    if not prompt_id:
        return
    path = _prompts_path(state_dir)
    now = time.time()
    with _Lock(path.with_suffix(".lock")):
        data = {k: v for k, v in _load(path).items() if isinstance(v, dict) and now - v.get("ts", 0) < PROMPT_TTL_S}
        data[prompt_id] = {**info, "ts": now, "calls": 0, "warned": False}
        _save(path, dict(sorted(data.items(), key=lambda kv: kv[1]["ts"])[-PROMPT_MAX:]))


def update_prompt(state_dir, prompt_id, change):
    """Runs change(entry) on the prompt's stored entry under the lock and saves what it changed in place.
    -> (change's result, a copy of the entry), or (None, None) when the prompt is unknown or expired. Two hooks
    racing past the lock's wait both proceed: a missed count is acceptable, a blocked tool call is not."""
    if not prompt_id:
        return None, None
    path = _prompts_path(state_dir)
    with _Lock(path.with_suffix(".lock")):
        data = _load(path)
        entry = data.get(prompt_id)
        if not isinstance(entry, dict) or time.time() - entry.get("ts", 0) >= PROMPT_TTL_S:
            return None, None
        result = change(entry)
        _save(path, data)
        return result, dict(entry)


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
