"""trirouter/store.py: the lock file and the JSON map store behind the hook state. A hook never waits past
LOCK_WAIT_S, but without the lock it must not rewrite the shared file, and a holder taken over as stale must not
remove the new holder's lock."""
import json
import os
import threading
import time

import pytest

from trirouter import queue_state, store


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(store, "LOCK_WAIT_S", 0.1)


def _age(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_the_lock_holds_its_owners_token_and_a_stale_takeover_keeps_the_new_holders_lock(tmp_path):
    path = tmp_path / "x.lock"
    first, second = store.FileLock(path, stale_s=10), store.FileLock(path, stale_s=10)
    assert first.acquire() and path.read_text(encoding="utf-8") == first.token
    assert not second.acquire()  # fresh: not taken over
    _age(path, 11)
    assert second.acquire() and path.read_text(encoding="utf-8") == second.token
    first.release()  # the holder that hung past the stale age wakes up ...
    assert path.exists() and path.read_text(encoding="utf-8") == second.token  # ... and leaves the live lock alone
    second.release()
    assert not path.exists()
    first.release()  # released twice: nothing happens
    assert first.acquire() and path.exists()
    first.release()


def test_the_context_manager_proceeds_without_the_lock_and_says_so(tmp_path, quick):
    path = tmp_path / "x.lock"
    holder = store.FileLock(path, stale_s=10)
    assert holder.acquire()
    t0 = time.monotonic()
    with store.FileLock(path, stale_s=10, wait_s=store.LOCK_WAIT_S) as lock:
        assert not lock.held and time.monotonic() - t0 < 2
    assert path.read_text(encoding="utf-8") == holder.token  # exiting without the lock removed nothing
    holder.release()
    with store.FileLock(path, stale_s=10, wait_s=store.LOCK_WAIT_S) as lock:
        assert lock.held
    assert not path.exists()


def test_without_the_lock_the_store_file_is_never_rewritten(tmp_path, quick):
    s = store.Store("s", ttl=60)
    s.put(tmp_path, "a", {"n": 1})
    before = s.path(tmp_path).read_text(encoding="utf-8")
    holder = s.lock(s.path(tmp_path))
    assert holder.acquire()
    s.put(tmp_path, "b", {"n": 2})
    s.pop(tmp_path, "a")
    assert s.update(tmp_path, "a", lambda e: e.update(n=e["n"] + 1) or e["n"]) == (2, {**json.loads(before)["a"], "n": 2})
    assert s.update(tmp_path, "c", lambda e: e["n"], missing={"n": 9}) == (9, {"n": 9, "ts": pytest.approx(time.time(), abs=5)})
    assert s.path(tmp_path).read_text(encoding="utf-8") == before  # the read-only fallback changed nothing on disk
    inflight = queue_state.INFLIGHT.lock(queue_state.INFLIGHT.path(tmp_path))
    assert inflight.acquire()
    info = queue_state.on_submit(tmp_path, "claude", "sid", str(tmp_path), "work")
    assert info == {"ahead": [], "others": []} and not queue_state.INFLIGHT.path(tmp_path).exists()
    inflight.release()
    holder.release()
    s.put(tmp_path, "b", {"n": 2})
    assert s.get(tmp_path, "b")["n"] == 2  # with the lock free the next write lands


def test_update_creates_a_missing_entry_in_the_same_locked_step(tmp_path):
    s = store.Store("counter", ttl=60)
    assert s.update(tmp_path, "k", lambda e: e["n"]) == (None, None)
    assert s.update(tmp_path, "k", lambda e: e.update(n=e["n"] + 1) or e["n"], missing={"n": 0})[0] == 1
    assert s.update(tmp_path, "k", lambda e: e.update(n=e["n"] + 1) or e["n"], missing={"n": 0})[0] == 2
    assert s.get(tmp_path, "k")["n"] == 2
    data = json.loads(s.path(tmp_path).read_text(encoding="utf-8"))
    data["k"]["ts"] -= 120  # expired: starts over from `missing`
    s.path(tmp_path).write_text(json.dumps(data), encoding="utf-8")
    assert s.update(tmp_path, "k", lambda e: e["n"], missing={"n": 0}) == (0, {"n": 0, "ts": pytest.approx(time.time(), abs=5)})


def test_concurrent_updates_are_all_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LOCK_WAIT_S", 30.0)
    s = store.Store("counter")

    def bump():
        for _ in range(5):
            s.update(tmp_path, "k", lambda e: e.update(n=e["n"] + 1), missing={"n": 0})
    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert s.get(tmp_path, "k")["n"] == 40
    assert not s.lock(s.path(tmp_path)).path.exists()
