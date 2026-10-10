"""Clean interruption of the command-line commands (not of the hooks or the MCP server).

* Ctrl+C at any point, and Ctrl+D (end of input) at a question or while a command runs: the command stops at
  once, child processes are ended, one line goes to stderr and the exit status is 130. Files are written
  atomically (platforms.atomic_write), so nothing is left half-written.
* While a command runs without asking anything, a small key watcher turns Ctrl+D into the same interrupt (a
  terminal only delivers Ctrl+C as a signal). It exists only when stdin is an interactive terminal, pauses while
  a question is shown (so typing an answer works normally) and always restores the terminal.
"""
import _thread
import atexit
import contextlib
import os
import sys
import threading
import time

from . import platforms as P

MESSAGE = "Interrupted: nothing was left half-written."
EXIT_CODE = 130
EOT = "\x04"  # Ctrl+D
POLL_S = 0.05


class _WindowsKeys:
    """msvcrt: kbhit / getwch polling; the console needs no mode change."""

    def __init__(self):
        import msvcrt
        self.m = msvcrt

    def enter(self):
        return True

    def leave(self):
        pass

    def pause(self):
        pass

    def resume(self):
        pass

    def poll(self):
        keys = []
        while self.m.kbhit():
            ch = self.m.getwch()
            if ch in ("\x00", "\xe0"):  # a function / arrow key: the second code belongs to it
                self.m.getwch()
                continue
            keys.append(ch)
        return "".join(keys)


class _PosixKeys:
    """termios cbreak mode (Ctrl+C keeps generating SIGINT) and select on stdin."""

    def __init__(self, fd):
        import select
        import termios
        import tty
        self.fd, self.select, self.termios, self.tty = fd, select, termios, tty
        self.saved = None

    def enter(self):
        try:
            if os.getpgrp() != os.tcgetpgrp(self.fd):  # a background job must not touch the terminal
                return False
            self.saved = self.termios.tcgetattr(self.fd)
            self.tty.setcbreak(self.fd)
            return True
        except (OSError, self.termios.error):
            self.saved = None
            return False

    def leave(self):
        self.pause()

    def pause(self):
        if self.saved is not None:
            with contextlib.suppress(OSError, self.termios.error):
                self.termios.tcsetattr(self.fd, self.termios.TCSADRAIN, self.saved)

    def resume(self):
        if self.saved is not None:
            with contextlib.suppress(OSError, self.termios.error):
                self.tty.setcbreak(self.fd)

    def poll(self):
        ready, _, _ = self.select.select([self.fd], [], [], POLL_S)
        if not ready:
            return ""
        return os.read(self.fd, 1024).decode("utf-8", errors="ignore")


def _backend(stream):
    """The platform's key reader for an interactive `stream`, or None."""
    try:
        return _WindowsKeys() if P.IS_WINDOWS else _PosixKeys(stream.fileno())
    except (ImportError, OSError, ValueError, AttributeError):
        return None


class KeyWatcher:
    """Turns Ctrl+D into `trigger()` (default: KeyboardInterrupt in the main thread) while a command runs.

    A no-op unless `stream` (stdin) is an interactive terminal. `backend` is for tests."""

    def __init__(self, stream=None, trigger=None, backend=None):
        self.stream = sys.stdin if stream is None else stream
        self.trigger = trigger or _thread.interrupt_main
        self.backend = backend
        self.active = False
        self._lock = threading.Lock()   # held for one poll, or while the terminal mode is switched
        self._paused = False
        self._stop = threading.Event()
        self._thread = None
        self.fired = False

    def start(self):
        if self.backend is None:
            if not P.is_terminal(self.stream):
                return False
            self.backend = _backend(self.stream)
        if self.backend is None or not self.backend.enter():
            return False
        self.active = True
        atexit.register(self.stop)  # the terminal is restored even when the interpreter exits some other way
        self._thread = threading.Thread(target=self._loop, name="trirouter-keys", daemon=True)
        self._thread.start()
        return True

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                if self._paused or self._stop.is_set():
                    keys = ""
                else:
                    try:
                        keys = self.backend.poll()
                    except (OSError, ValueError):
                        return
            if EOT in keys:
                self.fired = True
                self.trigger()
                return
            if not keys:
                self._stop.wait(POLL_S)

    def pause(self):
        """Hands the terminal back to normal line input (for a question)."""
        with self._lock:
            if self.active and not self._paused:
                self._paused = True
                self.backend.pause()

    def resume(self):
        with self._lock:
            if self.active and self._paused:
                self.backend.resume()
                self._paused = False

    def stop(self):
        """Stops watching and restores the terminal; safe to call twice."""
        if not self.active:
            return
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(1)
        with self._lock:
            self.active = False
            self.backend.leave()
        with contextlib.suppress(Exception):
            atexit.unregister(self.stop)


_WATCHER = None


@contextlib.contextmanager
def question():
    """Around input() / getpass(): the key watcher steps aside so typing an answer works normally."""
    watcher = _WATCHER
    if watcher is not None:
        watcher.pause()
    try:
        yield
    finally:
        if watcher is not None:
            watcher.resume()


def _console_line(prompt, echo=True, msvcrt=None):
    """Windows: a line read key by key, because the console does not treat Ctrl+D as end of input (it only knows
    Ctrl+Z, Enter). Ctrl+D / Ctrl+Z raise EOFError, Ctrl+C KeyboardInterrupt; polling keeps Ctrl+C responsive."""
    m = msvcrt or __import__("msvcrt")
    out = sys.stdout
    out.write(prompt)
    out.flush()
    chars = []
    while True:
        if not m.kbhit():
            time.sleep(0.02)
            continue
        ch = m.getwch()
        if ch in ("\r", "\n"):
            out.write("\n")
            out.flush()
            return "".join(chars).encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        if ch in (EOT, "\x1a"):
            out.write("\n")
            out.flush()
            raise EOFError
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\x00", "\xe0"):  # a function / arrow key: the second code belongs to it
            m.getwch()
        elif ch == "\x08":
            if chars:
                chars.pop()
                if echo:
                    out.write("\b \b")
                    out.flush()
        else:
            chars.append(ch)
            if echo:
                out.write(ch)
                out.flush()


def _on_console():
    return P.IS_WINDOWS and P.is_terminal(sys.stdin)


def ask_line(prompt=""):
    """input() that pauses the key watcher; Ctrl+D (EOFError) and Ctrl+C reach run_guarded."""
    with question():
        return _console_line(prompt) if _on_console() else input(prompt)


def ask_secret(prompt=""):
    """Like ask_line, but the typed text is not shown (getpass)."""
    with question():
        if _on_console():
            return _console_line(prompt, echo=False)
        import getpass
        return getpass.getpass(prompt)


def run_guarded(fn, *args):
    """fn(*args)'s result; on Ctrl+C or Ctrl+D (EOFError) child processes are ended, one line goes to stderr and
    the result is 130. SystemExit (usage errors, sys.exit) passes through."""
    global _WATCHER
    watcher = KeyWatcher()
    try:
        try:
            if watcher.start():
                _WATCHER = watcher
            return fn(*args)
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            _WATCHER = None
            watcher.stop()
    except KeyboardInterrupt:  # a second Ctrl+C while cleaning up
        pass
    with contextlib.suppress(KeyboardInterrupt):
        P.kill_children()
    with contextlib.suppress(Exception):
        sys.stdout.flush()
    print(MESSAGE, file=sys.stderr, flush=True)
    return EXIT_CODE
