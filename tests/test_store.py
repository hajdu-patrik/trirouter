"""trirouter/store.py: the lock file and the JSON map store behind the hook state. A hook never waits past
LOCK_WAIT_S, but without the lock it must not rewrite the shared file, and neither a holder taken over as stale nor
a second taker of a stale lock may remove the new holder's lock."""
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


def _replay_before(monkeypatch, path, step, action):
    """Runs `action` once, right before the first os.open / os.replace / os.unlink (`step`) whose target is `path` or
    starts with it (a file moved from it); -> a list that is empty until it ran."""
    real, replayed = getattr(os, step), []

    def hooked(target, *args, **kwargs):
        if not replayed and os.fspath(target).startswith(os.fspath(path)):
            replayed.append(step)
            action()
        return real(target, *args, **kwargs)
    monkeypatch.setattr(os, step, hooked)
    return replayed


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
    assert list(tmp_path.iterdir()) == []  # no moved lock file left behind


@pytest.mark.parametrize("step", ["replace", "unlink"])
def test_release_never_removes_the_lock_of_a_taker_that_arrives_mid_release(tmp_path, monkeypatch, step):
    """A stale takeover between the release's check and its removal must not make the old holder delete the new
    holder's lock. The takeover is replayed right before the release's first move and right before its first
    removal, whichever file that is."""
    path = tmp_path / "x.lock"
    first, taker = store.FileLock(path, stale_s=10), store.FileLock(path, stale_s=10)
    assert first.acquire()

    def take_over():
        if path.exists():  # still in place: the taker finds it stale and takes it over
            _age(path, 11)
        taker.acquire()
    replayed = _replay_before(monkeypatch, path, step, take_over)
    first.release()
    assert replayed and taker.held and path.read_text(encoding="utf-8") == taker.token
    assert list(tmp_path.iterdir()) == [path]
    taker.release()
    assert list(tmp_path.iterdir()) == []


def test_release_of_a_lock_that_is_already_gone_does_nothing(tmp_path):
    path = tmp_path / "x.lock"
    lock = store.FileLock(path, stale_s=10)
    assert lock.acquire()
    path.unlink()
    lock.release()
    assert not lock.held and list(tmp_path.iterdir()) == []


def test_a_lock_that_cannot_be_moved_stays_until_it_is_stale(tmp_path, monkeypatch):
    path = tmp_path / "x.lock"
    lock = store.FileLock(path, stale_s=10)
    assert lock.acquire()

    def refuse(src, dst):
        raise PermissionError(13, "in use")
    with monkeypatch.context() as m:
        m.setattr(os, "replace", refuse)
        lock.release()
    assert not lock.held and path.read_text(encoding="utf-8") == lock.token and list(tmp_path.iterdir()) == [path]
    _age(path, 11)
    assert store.FileLock(path, stale_s=10).acquire()


def test_a_lock_file_that_is_not_text_is_given_back_and_release_does_not_raise(tmp_path):
    path = tmp_path / "x.lock"
    lock = store.FileLock(path, stale_s=10)
    assert lock.acquire()
    path.write_bytes(b"\xff\xfe\x00")  # damaged content: not this lock's token
    lock.release()
    assert path.read_bytes() == b"\xff\xfe\x00" and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("step", ["replace", "unlink"])
def test_two_takers_of_a_stale_lock_never_both_get_it(tmp_path, monkeypatch, step):
    """One taker is replayed right before the other's first move and right before its first removal, whichever
    file that is: whichever of them ends up with the lock, it is only one."""
    path = tmp_path / "x.lock"
    dead, taker, other = (store.FileLock(path, stale_s=10) for _ in range(3))
    assert dead.acquire()
    _age(path, 11)  # its holder died
    replayed = _replay_before(monkeypatch, path, step, other.acquire)
    taker.acquire()
    holders = [lock for lock in (taker, other) if lock.held]
    assert replayed and len(holders) == 1 and path.read_text(encoding="utf-8") == holders[0].token
    holders[0].release()
    assert list(tmp_path.iterdir()) == []


def test_a_late_taker_finds_the_fresh_lock_of_a_quicker_one_and_leaves_it_alone(tmp_path, monkeypatch):
    """The quicker taker completes its whole takeover right before the late one gets as far as the guard."""
    path = tmp_path / "x.lock"
    dead, quick, late = (store.FileLock(path, stale_s=10) for _ in range(3))
    assert dead.acquire()
    _age(path, 11)
    replayed = _replay_before(monkeypatch, tmp_path / "x.lock.break", "open", quick.acquire)
    assert not late.acquire()
    assert replayed and quick.held and path.read_text(encoding="utf-8") == quick.token
    assert list(tmp_path.iterdir()) == [path]
    quick.release()
    assert list(tmp_path.iterdir()) == []


def test_a_taker_stays_away_while_another_one_is_taking_over(tmp_path):
    path = tmp_path / "x.lock"
    dead, taker = store.FileLock(path, stale_s=10), store.FileLock(path, stale_s=10)
    busy = store.FileLock(tmp_path / "x.lock.break", stale_s=10)
    assert dead.acquire() and busy.acquire()  # another taker is at it
    _age(path, 11)
    assert not taker.acquire() and path.read_text(encoding="utf-8") == dead.token  # the stale lock was not touched
    busy.release()
    assert taker.acquire() and path.read_text(encoding="utf-8") == taker.token


def test_a_guard_left_by_a_dead_taker_goes_stale_and_is_taken_over_too(tmp_path):
    path, guard_path = tmp_path / "x.lock", tmp_path / "x.lock.break"
    dead, taker = store.FileLock(path, stale_s=10), store.FileLock(path, stale_s=10)
    dead_taker = store.FileLock(guard_path, stale_s=10)
    assert dead.acquire() and dead_taker.acquire()
    _age(path, 11)
    assert not taker.acquire()  # the guard is still fresh
    _age(guard_path, 11)
    assert taker.acquire() and path.read_text(encoding="utf-8") == taker.token
    assert list(tmp_path.iterdir()) == [path]


def test_a_poll_of_a_live_lock_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "x.lock"
    holder, poller = store.FileLock(path, stale_s=10), store.FileLock(path, stale_s=10, wait_s=0.15)
    assert holder.acquire()
    opened, real_open = [], os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: opened.append(os.fspath(p)) or real_open(p, *a, **k))
    for name in ("replace", "unlink"):
        monkeypatch.setattr(os, name, lambda *a, **k: pytest.fail("a live lock was touched"))
    with poller:
        assert not poller.held
    assert set(opened) == {str(path)} and path.read_text(encoding="utf-8") == holder.token
    assert list(tmp_path.iterdir()) == [path]


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
    assert [p.name for p in s.path(tmp_path).parent.iterdir()] == ["counter.json"]  # no lock, no moved lock file left
