"""Small per-user state files shared by queue_state, the JEV cache and breaker (core) and discovery: an O_EXCL lock file
and a JSON map with expiring entries. Never raises into a hook: a failed lock is skipped."""
import json
import os
import time
from pathlib import Path

from . import platforms as P

LOCK_STALE_S = 10   # a store lock older than this belongs to a hook that died
LOCK_WAIT_S = 2.0   # a hook waits this long for a store lock, then proceeds without it


class FileLock:
    """An O_EXCL lock file with its owner's token. As a context manager it waits `wait_s`, then goes on WITHOUT the lock
    (`held` says which): a hook must not block. Stale files are taken over one taker at a time."""

    def __init__(self, path, stale_s, wait_s=0.0):
        self.path, self.stale_s, self.wait_s = Path(path), stale_s, wait_s
        nonce = os.urandom(8).hex()
        self.token = f"{os.getpid()} {nonce}\n"
        self._aside = self.path.with_name(f"{self.path.name}.{nonce}")
        self.held = False

    def acquire(self):
        try:
            P.ensure_dir(self.path.parent)
        except OSError:
            return False
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if self._stale() and self._take_over():  # a poll of a live lock only gets as far as the stat
                    continue
                return False
            except OSError:
                return False
            with os.fdopen(fd, "w") as f:
                f.write(self.token)
            self.held = True
            return True
        return False

    def _stale(self):
        try:
            return time.time() - self.path.stat().st_mtime > self.stale_s
        except OSError:
            return False

    def _take_over(self):
        """Removes the stale lock file, True when it did. Takers go one at a time through a second lock file and
        re-check under it: a late taker finds the quicker one's fresh lock and leaves it alone."""
        guard = FileLock(self.path.with_name(f"{self.path.name}.break"), self.stale_s)
        if not guard.acquire():
            return False
        try:
            if self._stale():
                os.unlink(self.path)
                return True
        except OSError:
            pass
        finally:
            guard.release()
        return False

    def release(self):
        """Moves the file to a name only this object knows, then checks and removes it there (read-then-unlink leaves a
        window for a taker); a file with another token, a taker's after a stale takeover, is moved straight back."""
        if not self.held:
            return
        self.held = False
        try:
            os.replace(self.path, self._aside)
        except OSError:
            return
        try:
            mine = self._aside.read_text(encoding="utf-8") == self.token
        except (OSError, ValueError):
            mine = False
        try:
            if mine:
                os.unlink(self._aside)
            else:
                os.replace(self._aside, self.path)
        except OSError:
            pass

    def __enter__(self):
        deadline = time.monotonic() + self.wait_s
        while not self.acquire() and time.monotonic() < deadline:
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.release()


def load(path):
    return P.read_json(path, {}, kind=dict)


def save(path, data):
    try:
        P.atomic_write(path, json.dumps(data), mode=0o600)
    except OSError:
        pass


def stamp(entry):
    """The entry's "ts" when it is a stamped object, else None (anything else in a state file is ignored)."""
    ts = entry.get("ts") if isinstance(entry, dict) else None
    return ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None


class Store:
    """One small JSON map in <state>/state/<name>.json under its own lock; entries carry a "ts" and expire after `ttl`,
    `limit` keeps the newest. Without the lock a hook works on what it read: a lost update beats a stale rewrite."""

    def __init__(self, name, ttl=None, limit=None):
        self.name, self.ttl, self.limit = name, ttl, limit

    def path(self, state_dir):
        return Path(state_dir) / "state" / f"{self.name}.json"

    def lock(self, path):
        return FileLock(path.with_suffix(".lock"), LOCK_STALE_S, LOCK_WAIT_S)

    def _fresh(self, entry, now):
        ts = stamp(entry)
        return ts is not None and (self.ttl is None or now - ts < self.ttl)

    def get(self, state_dir, key):
        """The key's entry while it is fresh, else None."""
        entry = load(self.path(state_dir)).get(key)
        return entry if self._fresh(entry, time.time()) else None

    def put(self, state_dir, key, entry):
        """Stores `entry`, stamped now, under `key` and drops the expired entries; None removes the key instead."""
        path, now = self.path(state_dir), time.time()
        with self.lock(path) as lock:
            if not lock.held:
                return
            data = {k: v for k, v in load(path).items() if self._fresh(v, now)}
            if entry is not None:
                data[key] = {**entry, "ts": now}
            elif data.pop(key, None) is None:
                return
            if self.limit:
                data = dict(sorted(data.items(), key=lambda kv: kv[1]["ts"])[-self.limit:])
            save(path, data)

    def pop(self, state_dir, key):
        path = self.path(state_dir)
        with self.lock(path) as lock:
            if not lock.held:
                return
            data = load(path)
            if data.pop(key, None) is not None:
                save(path, data)

    def update(self, state_dir, key, change, missing=None):
        """Runs change(entry) on the key's fresh entry under the lock and saves it; with `missing` an unknown key starts
        from that dict (a counter is created and bumped in one step). -> (result, entry copy), or (None, None)."""
        path, now = self.path(state_dir), time.time()
        with self.lock(path) as lock:
            data = load(path)
            entry = data.get(key)
            if not self._fresh(entry, now):
                if missing is None:
                    return None, None
                entry = data[key] = {**missing, "ts": now}
            result = change(entry)
            if lock.held:
                save(path, data)
            return result, dict(entry)
