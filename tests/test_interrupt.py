"""Clean interruption: Ctrl+C / Ctrl+D end any command with exit status 130 and one line on stderr, children are
killed, files are written atomically, and the Ctrl+D key watcher only exists on an interactive terminal.

No real terminal and no real child process is used: input(), the key backend and the process objects are fakes.
"""
import io
import os
import threading
import time

import pytest

from trirouter import cli, doctor, interrupt, platforms as P

MESSAGE = "Interrupted: nothing was left half-written.\n"


@pytest.fixture(autouse=True)
def as_launcher(monkeypatch):
    monkeypatch.setenv("TRIROUTER_PROG", "trirouter")
    monkeypatch.setattr(interrupt.KeyWatcher, "start", lambda self: False)  # never touch the test runner's terminal


# ---- Ctrl+C and Ctrl+D end a command ------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [KeyboardInterrupt, EOFError])
def test_an_interrupt_in_a_command_is_exit_130_with_one_line_and_no_traceback(capsys, monkeypatch, exc):
    def boom():
        raise exc()
    monkeypatch.setattr(doctor, "main", boom)
    assert cli.main(["doctor"]) == 130
    out, err = capsys.readouterr()
    assert err == MESSAGE and "Traceback" not in out + err


def test_ctrl_c_during_route_and_help_too(capsys, monkeypatch):
    monkeypatch.setattr(cli, "route_decision", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert cli.main(["route", "hello there"]) == 130 and capsys.readouterr().err == MESSAGE


def test_eof_at_a_question_cancels_the_whole_command(capsys, monkeypatch):
    monkeypatch.setattr(cli, "interactive", lambda: True)
    monkeypatch.setattr(cli, "YES", False)
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError()))
    monkeypatch.setattr(cli, "run_remote", lambda: cli.ask("Set up remote access?", "n"))
    monkeypatch.setattr(cli, "prepare_state", lambda key: None)
    assert cli.main(["remote"]) == 130
    out, err = capsys.readouterr()
    assert err == MESSAGE and "Traceback" not in out + err


def test_ctrl_c_at_a_question_reaches_the_guard(capsys, monkeypatch):
    monkeypatch.setattr(cli, "interactive", lambda: True)
    monkeypatch.setattr(cli, "YES", False)
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(cli, "run_remote", lambda: cli.ask("Set up remote access?", "n"))
    monkeypatch.setattr(cli, "prepare_state", lambda key: None)
    assert cli.main(["remote"]) == 130 and capsys.readouterr().err == MESSAGE


def test_children_are_killed_on_an_interrupt(monkeypatch):
    killed = []
    monkeypatch.setattr(P, "kill_children", lambda: killed.append(True))
    assert interrupt.run_guarded(lambda: (_ for _ in ()).throw(KeyboardInterrupt())) == 130 and killed


def test_a_second_ctrl_c_while_cleaning_up_still_ends_with_130(capsys, monkeypatch):
    def cleanup_interrupted():
        raise KeyboardInterrupt
    monkeypatch.setattr(P, "kill_children", cleanup_interrupted)
    assert interrupt.run_guarded(lambda: (_ for _ in ()).throw(KeyboardInterrupt())) == 130
    assert capsys.readouterr().err == MESSAGE


def test_usage_errors_and_normal_results_are_not_changed(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["nonsense"])
    assert exc.value.code == 2
    assert interrupt.run_guarded(lambda: 7) == 7


# ---- running programs ---------------------------------------------------------------------------------------

class FakeProc:
    pid = 4242
    returncode = None

    def __init__(self):
        self.release = threading.Event()
        self.killed = False

    def communicate(self, timeout=None):
        self.release.wait(10)
        return "out", ""

    def poll(self):
        return None if not self.release.is_set() else 0

    def kill(self):
        self.killed = True
        self.release.set()


def test_run_ends_the_child_tree_on_ctrl_c(monkeypatch):
    proc, tree = FakeProc(), []
    monkeypatch.setattr(P, "_popen", lambda argv, **kw: proc)
    monkeypatch.setattr(P, "kill_tree", lambda p: (tree.append(p), p.kill()))
    monkeypatch.setattr(P.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        P.run(["codex", "exec", "x"], timeout=60)
    assert tree == [proc] and proc.killed


def test_run_ends_the_child_tree_on_timeout(monkeypatch):
    proc, tree = FakeProc(), []
    monkeypatch.setattr(P, "_popen", lambda argv, **kw: proc)
    monkeypatch.setattr(P, "kill_tree", lambda p: (tree.append(p), p.kill()))
    code, out = P.run(["codex"], timeout=0.05)
    assert code is None and out.startswith("TimeoutExpired") and tree == [proc]


def test_run_returns_output_and_forgets_the_child(monkeypatch):
    proc = FakeProc()
    proc.release.set()
    proc.returncode = 0
    monkeypatch.setattr(P, "_popen", lambda argv, **kw: proc)
    assert P.run(["x"]) == (0, "out")
    assert not P._CHILDREN or proc not in P._CHILDREN


def test_kill_children_reaches_every_running_child(monkeypatch):
    a, b, done = FakeProc(), FakeProc(), FakeProc()
    done.release.set()
    seen = []
    monkeypatch.setattr(P, "kill_tree", lambda p: seen.append(p))
    monkeypatch.setattr(P, "_CHILDREN", {a, b, done})
    P.kill_children()
    assert sorted(map(id, seen)) == sorted([id(a), id(b)])


# ---- atomic writes ------------------------------------------------------------------------------------------

def leftovers(folder):
    return sorted(p.name for p in folder.iterdir())


def test_atomic_write_creates_replaces_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "sub" / "config.json"
    P.atomic_write(target, "one\n")
    P.atomic_write(target, "two\n", newline="")
    assert target.read_bytes() == b"two\n" and leftovers(target.parent) == ["config.json"]
    P.atomic_write(target, b"\x00\x01")
    assert target.read_bytes() == b"\x00\x01"


@pytest.mark.parametrize("where", ["fsync", "replace"])
def test_an_interrupt_mid_write_keeps_the_old_file_and_removes_the_temp_file(tmp_path, monkeypatch, where):
    target = tmp_path / "config.json"
    target.write_text("old", encoding="utf-8")

    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync" if where == "fsync" else "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        P.atomic_write(target, "new content that is long enough")
    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == "old" and leftovers(tmp_path) == ["config.json"]


def test_an_interrupt_before_the_first_write_leaves_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        P.atomic_write(tmp_path / "new.json", "x")
    monkeypatch.undo()
    assert leftovers(tmp_path) == []


@pytest.mark.skipif(P.IS_WINDOWS, reason="POSIX permission bits")
def test_atomic_write_applies_the_mode_and_keeps_the_mode_of_an_existing_file(tmp_path):
    secret = tmp_path / "secret.json"
    P.atomic_write(secret, "{}", mode=0o600)
    assert secret.stat().st_mode & 0o777 == 0o600
    script = tmp_path / "run.sh"
    script.write_text("a")
    script.chmod(0o755)
    P.atomic_write(script, "b")
    assert script.stat().st_mode & 0o777 == 0o755


def test_save_config_is_atomic(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"keep": true}', encoding="utf-8")
    monkeypatch.setattr(cli, "CONFIG", cfg)
    monkeypatch.setattr(cli, "STATE", tmp_path)
    monkeypatch.setattr(cli, "DRY", False)
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        cli.save_config({"typesafe_api_key": "x" * 50})
    monkeypatch.undo()
    assert cfg.read_text(encoding="utf-8") == '{"keep": true}' and leftovers(tmp_path) == ["config.json"]


# ---- the Ctrl+D key watcher ---------------------------------------------------------------------------------

@pytest.fixture
def real_start(monkeypatch):
    monkeypatch.undo()  # the autouse stub of KeyWatcher.start
    monkeypatch.setenv("TRIROUTER_PROG", "trirouter")


class FakeKeys:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def enter(self):
        self.calls.append("enter")
        return True

    def leave(self):
        self.calls.append("leave")

    def pause(self):
        self.calls.append("pause")

    def resume(self):
        self.calls.append("resume")

    def poll(self):
        self.calls.append("poll")
        time.sleep(0.01)
        return self.script.pop(0) if self.script else ""


def wait_for(cond, seconds=3):
    end = time.time() + seconds
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_the_watcher_is_a_no_op_without_a_terminal(real_start):
    for stream in (io.StringIO(), None):
        w = interrupt.KeyWatcher(stream=stream or io.StringIO(), trigger=lambda: pytest.fail("fired"))
        assert w.start() is False and w.active is False and w._thread is None
        w.stop()
        w.pause()
        w.resume()


def test_the_watcher_ignores_a_pipe_even_where_isatty_lies(real_start, monkeypatch):
    class Piped(io.StringIO):
        def isatty(self):
            return False
    assert interrupt.KeyWatcher(stream=Piped()).start() is False


def test_ctrl_d_fires_the_trigger_once_and_other_keys_do_not(real_start):
    fired = []
    keys = FakeKeys(["a", "b", "\x04", "\x04"])
    w = interrupt.KeyWatcher(trigger=lambda: fired.append(1), backend=keys)
    assert w.start() is True
    assert wait_for(lambda: fired)
    w.stop()
    assert fired == [1] and w.fired and keys.calls[0] == "enter" and keys.calls[-1] == "leave"


def test_the_watcher_does_not_read_while_a_question_is_shown(real_start):
    keys = FakeKeys([])
    w = interrupt.KeyWatcher(trigger=lambda: None, backend=keys)
    w.start()
    interrupt._WATCHER = w
    try:
        with interrupt.question():
            assert "pause" in keys.calls
            polls = keys.calls.count("poll")
            time.sleep(0.2)
            assert keys.calls.count("poll") == polls  # nothing is read: the answer belongs to input()
        assert keys.calls[-1] == "resume" or wait_for(lambda: keys.calls.count("poll") > polls)
    finally:
        interrupt._WATCHER = None
        w.stop()
    assert keys.calls[-1] == "leave"


def test_the_terminal_is_restored_when_the_command_fails(real_start, monkeypatch):
    keys = FakeKeys([])
    monkeypatch.setattr(interrupt, "_backend", lambda stream: keys)
    monkeypatch.setattr(P, "is_terminal", lambda stream: True)
    with pytest.raises(ValueError):
        interrupt.run_guarded(lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert keys.calls[0] == "enter" and keys.calls[-1] == "leave" and interrupt._WATCHER is None


def test_ctrl_d_while_a_command_runs_is_exit_130(real_start, monkeypatch, capsys):
    keys = FakeKeys(["\x04"])
    monkeypatch.setattr(interrupt, "_backend", lambda stream: keys)
    monkeypatch.setattr(P, "is_terminal", lambda stream: True)

    def slow_command():
        for _ in range(300):
            time.sleep(0.01)  # interrupt_main raises in the main thread at the next bytecode
        return 0
    assert interrupt.run_guarded(slow_command) == 130
    assert capsys.readouterr().err == MESSAGE and keys.calls[-1] == "leave"


def test_ask_line_steps_the_watcher_aside(real_start, monkeypatch):
    keys = FakeKeys([])
    w = interrupt.KeyWatcher(trigger=lambda: None, backend=keys)
    w.start()
    interrupt._WATCHER = w
    seen = []
    monkeypatch.setattr("builtins.input", lambda prompt="": seen.append(list(keys.calls)) or "y")
    try:
        assert interrupt.ask_line("ok? ") == "y"
    finally:
        interrupt._WATCHER = None
        w.stop()
    assert "pause" in seen[0] and "resume" not in seen[0]


# ---- Ctrl+D at a question on the Windows console ----------------------------------------------------------------

class FakeMsvcrt:
    def __init__(self, keys):
        self.keys = list(keys)

    def kbhit(self):
        return bool(self.keys)

    def getwch(self):
        return self.keys.pop(0)


def test_console_line_reads_edits_and_ends_on_enter(capsys):
    keys = FakeMsvcrt(["y", "x", "\x08", "e", "\xe0", "H", "s", "\r"])  # y x <backspace> e <arrow up> s <Enter>
    assert interrupt._console_line("ok? ", msvcrt=keys) == "yes"
    assert capsys.readouterr().out.startswith("ok? yx\b \bes")


def test_console_line_hides_a_secret(capsys):
    assert interrupt._console_line("key: ", echo=False, msvcrt=FakeMsvcrt(list("sk-1") + ["\r"])) == "sk-1"
    assert "sk-1" not in capsys.readouterr().out


@pytest.mark.parametrize("key", ["\x04", "\x1a"])
def test_console_line_ctrl_d_is_end_of_input(key):
    with pytest.raises(EOFError):
        interrupt._console_line("? ", msvcrt=FakeMsvcrt(["a", key]))


def test_console_line_ctrl_c_is_a_keyboard_interrupt():
    with pytest.raises(KeyboardInterrupt):
        interrupt._console_line("? ", msvcrt=FakeMsvcrt(["\x03"]))


def test_ask_secret_uses_getpass_off_the_console(monkeypatch):
    import getpass
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "secret")
    monkeypatch.setattr(interrupt, "_on_console", lambda: False)
    assert interrupt.ask_secret("key: ") == "secret"
