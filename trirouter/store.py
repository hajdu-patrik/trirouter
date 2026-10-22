"""Small per-user state files shared by the hook state (queue_state.py), the JEV answer cache and circuit breaker
(core.py) and the daily model check (discovery.py): an O_EXCL lock file and a JSON map with expiring entries.
Nothing here raises into a hook: a lock that cannot be taken is skipped, a file that cannot be read counts as
empty, a file that cannot be written is left as it was."""
import json
import os
import time
from pathlib import Path

from . import platforms as P

LOCK_STALE_S = 10   # a store lock older than this belongs to a hook that died
LOCK_WAIT_S = 2.0   # a hook waits this long for a store lock, then proceeds without it


class FileLock:
    """An O_EXCL lock file holding its owner's token. `acquire()` takes it once (True when this process holds it
    now; a file older than `stale_s` is considered abandoned and taken over). As a context manager it retries for
    `wait_s`, then proceeds WITHOUT the lock (`held` says which): a hook must never block on one, but it must not
    rewrite the shared file either. `release()` removes the file only while it still holds this object's token:
    a holder that was taken over as stale never deletes the new holder's lock."""

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
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_s:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                return False
            except OSError:
                return False
            with os.fdopen(fd, "w") as f:
                f.write(self.token)
            self.held = True
            return True
        return False

    def release(self):
        """Reading the token and then unlinking would leave a window for a taker to replace the lock in between, so
        the file is first moved to a name only this object knows: what is checked there is what is removed. A file
        with another token (a taker's, after a stale takeover) is moved straight back; a lock that cannot be moved
        stays until it is stale."""
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
                self._aside.unlink()
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
    """One small JSON map in <state>/state/<name>.json, rewritten under its own lock file. Entries carry a "ts" stamp:
    one older than `ttl` seconds is expired, and with `limit` only the newest entries are kept. A hook that could
    not take the lock within LOCK_WAIT_S works on what it read and leaves the file alone: a lost update is
    acceptable, a file rewritten from a stale read is not."""

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
        """Runs change(entry) on the key's fresh entry under the lock and saves what it changed in place; with
        `missing` (a dict) an unknown or expired key starts from a stamped copy of it, so a counter is created and
        incremented in one locked step. -> (change's result, a copy of the entry), or (None, None) when the key is
        unknown or expired and nothing says what it starts from."""
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
