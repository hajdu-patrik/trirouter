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


class _Store:
    """One small JSON map in <state>/state/<name>.json, rewritten under its own lock file. Entries carry a "ts" stamp:
    one older than `ttl` seconds is expired, and with `limit` only the newest entries are kept."""

    def __init__(self, name, ttl=None, limit=None):
        self.name, self.ttl, self.limit = name, ttl, limit

    def path(self, state_dir):
        return Path(state_dir) / "state" / f"{self.name}.json"

    def lock(self, path):
        return _Lock(path.with_suffix(".lock"))

    def _fresh(self, entry, now):
        return isinstance(entry, dict) and (self.ttl is None or now - entry.get("ts", 0) < self.ttl)

    def get(self, state_dir, key):
        """The key's entry while it is fresh, else None."""
        entry = _load(self.path(state_dir)).get(key)
        return entry if self._fresh(entry, time.time()) else None

    def put(self, state_dir, key, entry):
        """Stores `entry`, stamped now, under `key` and drops the expired entries; None removes the key instead."""
        path, now = self.path(state_dir), time.time()
        with self.lock(path):
            data = {k: v for k, v in _load(path).items() if self._fresh(v, now)}
            if entry is not None:
                data[key] = {**entry, "ts": now}
            elif data.pop(key, None) is None:
                return
            if self.limit:
                data = dict(sorted(data.items(), key=lambda kv: kv[1]["ts"])[-self.limit:])
            _save(path, data)

    def pop(self, state_dir, key):
        path = self.path(state_dir)
        with self.lock(path):
            data = _load(path)
            if data.pop(key, None) is not None:
                _save(path, data)

    def update(self, state_dir, key, change):
        """Runs change(entry) on the key's fresh entry under the lock and saves what it changed in place.
        -> (change's result, a copy of the entry), or (None, None) when the key is unknown or expired."""
        path = self.path(state_dir)
        with self.lock(path):
            data = _load(path)
            entry = data.get(key)
            if not self._fresh(entry, time.time()):
                return None, None
            result = change(entry)
            _save(path, data)
            return result, dict(entry)


INFLIGHT = _Store("inflight")  # {key: [entries]}: pruned per entry by on_submit, so no ttl of its own


def on_submit(state_dir, provider, session_id, cwd, summary, last_activity=None):
    """{"ahead": unfinished entries of this session, "others": other sessions in the same folder}.
    last_activity: the transcript's mtime; a session silent for IDLE_S had its turn cancelled."""
    if not session_id:
        return {"ahead": [], "others": []}
    path = INFLIGHT.path(state_dir)
    now = time.time()
    key = f"{provider}:{session_id}"
    norm_cwd = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
    with INFLIGHT.lock(path):
        data = _prune(_load(path), now)
        if last_activity is not None and now - last_activity > IDLE_S:
            data.pop(key, None)
        ahead = list(data.get(key, []))
        others = [e for k, v in data.items() if k != key for e in v[:1] if norm_cwd and e.get("cwd") == norm_cwd]
        data.setdefault(key, []).append({"ts": now, "cwd": norm_cwd, "summary": summary, "provider": provider})
        _save(path, data)
    return {"ahead": ahead, "others": others}


def on_stop(state_dir, provider, session_id):
    if session_id:
        INFLIGHT.pop(state_dir, f"{provider}:{session_id}")


LAST_KEYS = ("task", "task_conf", "level", "primary", "verify", "skill", "skill_path", "skill_native", "effort",
             "model", "extra_agents", "lang", "target_agent")
LAST = _Store("last_decision", ttl=_minutes("ROUTER_CONTINUATION_TTL_MIN", "180"))


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
MODELS = _Store("session_models", ttl=7 * 24 * 3600, limit=200)


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

PROMPTS = _Store("agent_budget", ttl=12 * 3600, limit=300)


def remember_prompt(state_dir, prompt_id, info):
    """Stores `info` (the routed decision's budget and target) for a prompt_id, with a fresh call counter. Bounded to
    the 300 newest prompts; entries expire after 12 hours."""
    if prompt_id:
        PROMPTS.put(state_dir, prompt_id, {**info, "calls": 0, "warned": False})


def update_prompt(state_dir, prompt_id, change):
    """Runs change(entry) on the prompt's stored entry (see _Store.update). Two hooks racing past the lock's wait
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
