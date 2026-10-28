"""Remote access: Claude's Remote Control server only holds the connection at logon unless --start-session=on, on every
OS; old Claude Code versions keep `--name`. _ps and the help text are fakes."""
import contextlib
import json
import plistlib
import sys
import types

import pytest

from trirouter import cli, cliparse, platforms as P, remote

NEW_HELP = """OPTIONS
  --name <name>                    Name for the session (shown in claude.ai/code)
  --remote-control-session-name-prefix <prefix>
  --[no-]create-session-in-dir     Pre-create a session in the current directory (default: on)
"""


@pytest.fixture
def help_text(monkeypatch):
    def set_help(text):
        monkeypatch.setattr(remote, "_rc_help", lambda claude: text)
    set_help(NEW_HELP)
    return set_help


def test_by_default_only_the_connection_opens(help_text):
    assert remote.claude_rc_args("claude", "My PC") == [
        "remote-control", "--no-create-session-in-dir", "--remote-control-session-name-prefix", "My PC"]


def test_start_session_and_old_versions_keep_the_named_session(help_text):
    assert remote.claude_rc_args("claude", "My PC", start_session=True) == ["remote-control", "--name", "My PC"]
    help_text("OPTIONS\n  --name <name>\n")  # a Claude Code from before the flag
    assert remote.claude_rc_args("claude", "My PC") == ["remote-control", "--name", "My PC"]
    help_text("OPTIONS\n  --[no-]create-session-in-dir\n")  # the flag without the prefix option
    assert remote.claude_rc_args("claude", "Box") == ["remote-control", "--no-create-session-in-dir"]


def test_windows_loop_runs_the_connection_only_server(help_text, monkeypatch, tmp_path):
    monkeypatch.setattr(P, "IS_WINDOWS", True)
    monkeypatch.setattr(remote, "BIN", tmp_path)
    monkeypatch.setattr(remote, "_ps", lambda script: (0, ""))
    remote._setup_claude("My PC", r"C:\bin\claude.exe", tmp_path, remote.claude_rc_args("claude", "My PC"))
    script = (tmp_path / "claude-remote.cmd").read_bytes().decode("utf-8")
    assert ('"C:\\bin\\claude.exe" remote-control --no-create-session-in-dir '
            '--remote-control-session-name-prefix "My PC"\r\n') in script
    remote._setup_claude("My PC", r"C:\bin\claude.exe", tmp_path, remote.claude_rc_args("claude", "My PC", True))
    assert '"C:\\bin\\claude.exe" remote-control --name "My PC"\r\n' in (tmp_path / "claude-remote.cmd").read_bytes().decode("utf-8")


def test_launchd_and_systemd_get_the_same_arguments(help_text):
    args = remote.claude_rc_args("claude", "My PC")
    plist = plistlib.loads(remote._launchd_plist("My PC", "/usr/local/bin/claude", "/Users/x/code", args))
    assert plist["ProgramArguments"] == ["/usr/local/bin/claude", *args] and plist["KeepAlive"] is True
    unit = remote._systemd_unit("My PC", "/usr/bin/claude", "/home/x/code", args)
    assert ('ExecStart="/usr/bin/claude" remote-control --no-create-session-in-dir '
            '--remote-control-session-name-prefix "My PC"\n') in unit
    assert "remote-control --name Box\n" in remote._systemd_unit("Box", "/usr/bin/claude", "/home/x")  # the default
    assert 'remote-control --name "50%% $$off"\n' in remote._systemd_unit("50% $off", "/usr/bin/claude", "/home/x")


def test_the_choice_is_saved_and_reused(help_text, monkeypatch, tmp_path):
    monkeypatch.setattr(remote, "CONFIG", tmp_path / "config.json")
    seen = []
    monkeypatch.setattr(P, "find_exe", lambda exe: "claude" if exe == "claude" else None)
    monkeypatch.setattr(remote, "claude_trusts", lambda folder: True)
    monkeypatch.setattr(remote, "_setup_claude", lambda name, claude, wd, args: seen.append("--name" in args) or (True, ""))
    monkeypatch.setattr(P, "IS_WINDOWS", False)
    (tool, ok, msg), = remote.setup("Box", ["claude"], workdir=tmp_path)
    assert ok and seen == [False] and "only the connection opens" in msg
    remote.setup("Box", ["claude"], workdir=tmp_path, start_session=True)
    assert json.loads((tmp_path / "config.json").read_text())["remote_start_session"] is True
    (_, _, msg), = remote.setup("Box", ["claude"], workdir=tmp_path)  # None: the saved choice
    assert seen == [False, True, True] and "pre-created" in msg


def test_an_old_claude_code_is_reported_honestly(help_text, monkeypatch, tmp_path):
    """Too old for --no-create-session-in-dir: the classic command runs, and the report says so."""
    help_text("OPTIONS\n  --name <name>\n")
    monkeypatch.setattr(remote, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(P, "find_exe", lambda exe: "claude" if exe == "claude" else None)
    monkeypatch.setattr(remote, "claude_trusts", lambda folder: True)
    monkeypatch.setattr(remote, "_setup_claude", lambda name, claude, wd, args: (True, ""))
    monkeypatch.setattr(P, "IS_WINDOWS", False)
    (_, ok, msg), = remote.setup("Box", ["claude"], workdir=tmp_path)
    assert ok and "too old" in msg and "claude update" in msg and "only the connection" not in msg


def test_cli_flag_reaches_setup(monkeypatch):
    got = {}
    monkeypatch.setattr(P, "detect", lambda deep=False: {"claude": {"installed": True}})
    monkeypatch.setattr(cli, "load_config", lambda: {"remote_name": "Box"})
    monkeypatch.setattr(remote, "setup", lambda name, providers, apply, workdir, start_session: got.update(
        name=name, start=start_session) or [])
    for value, expected in (("on", True), ("off", False)):
        cli.configure(cliparse.resolve(["remote", f"--start-session={value}"], "trirouter", False))
        cli.run_remote()
        assert got == {"name": "Box", "start": expected}
    cli.configure(cliparse.resolve(["remote"], "trirouter", False))
    cli.run_remote()
    assert got["start"] is None


def test_server_status_tells_claude_from_antigravity():
    tasks = {remote.TASK_CLAUDE: "Running"}
    agy_only = "agy.exe|agy remote-control serve --name box"
    assert remote._server_status(tasks, agy_only)[0][1] is False
    for cmd in ('claude.exe|"c:\\claude.exe" remote-control --no-create-session-in-dir',
                'claude.exe|"c:\\claude.exe" remote-control --name "box"'):
        assert remote._server_status(tasks, agy_only + "\n" + cmd)[0][1] is True


@pytest.mark.parametrize("entry, serving, ok, detail", [
    (r'"C:\Windows\System32\conhost.exe" --headless "C:\agy\agy.exe" remote-control serve', True, True,
     "daemon running, autostart hidden"),
    (r'"C:\agy\agy.exe" remote-control serve', True, False, "daemon running, autostart opens a window"),
    ("", False, False, "daemon NOT running, autostart not registered"),
])
def test_windows_status_reads_the_autostart_entry_without_a_scheduled_task(monkeypatch, entry, serving, ok, detail):
    procs = "agy.exe|agy remote-control serve --name box" if serving else ""
    monkeypatch.setattr(P, "IS_WINDOWS", True)
    monkeypatch.setattr(P, "find_exe", lambda name: r"C:\agy\agy.exe" if name == "agy" else None)
    monkeypatch.setattr(remote, "_ps", lambda script: (0, procs if "Get-CimInstance" in script else ""))
    monkeypatch.setattr(remote, "_agy_autostart_entry", lambda: entry)
    monkeypatch.setattr(remote, "_run_once", lambda *a, **k: pytest.fail("status() registered a scheduled task"))
    assert ("antigravity", ok, detail) in remote.status()


def test_the_autostart_entry_is_read_from_the_current_users_run_key(monkeypatch):
    entry = '"agy.exe" --headless remote-control serve'
    run_key = ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run")
    registry = {run_key: {"AntigravityCliDaemon": (entry, 1)}}

    def open_key(root, subkey):
        if (root, subkey) not in registry:
            raise FileNotFoundError(subkey)
        return contextlib.nullcontext(registry[root, subkey])

    def query_value(key, name):
        if name not in key:
            raise FileNotFoundError(name)
        return key[name]

    monkeypatch.setitem(sys.modules, "winreg", types.SimpleNamespace(
        HKEY_CURRENT_USER="HKCU", OpenKey=open_key, QueryValueEx=query_value))
    assert remote._agy_autostart_entry() == entry
    registry[run_key].clear()  # the key without the value
    assert remote._agy_autostart_entry() == ""
    registry.clear()  # no key at all
    assert remote._agy_autostart_entry() == ""


def test_remove_leaves_remote_control_alone_when_trirouter_never_set_it_up(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(remote, "BIN", tmp_path)
    monkeypatch.setattr(remote, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(remote, "_ps", lambda script: calls.append(script) or (0, ""))
    monkeypatch.setattr(remote, "_run_once", lambda *a, **k: calls.append("run_once") or (0, ""))
    monkeypatch.setattr(P, "run", lambda *a, **k: calls.append(a) or (0, ""))
    monkeypatch.setattr(P, "find_exe", lambda exe: f"/bin/{exe}")
    assert remote.remove() == []
    assert not any("Stop-Process" in str(c) or "remote-control" in str(c) for c in calls)
    (tmp_path / "config.json").write_text(json.dumps({"remote_name": "Box", "remote_workdir": "x"}), encoding="utf-8")
    (tmp_path / "claude-remote.cmd").write_text("x", encoding="utf-8")
    assert remote.remove()
    assert not (tmp_path / "claude-remote.cmd").exists() or not P.IS_WINDOWS
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {"remote_workdir": "x"}


def test_remote_without_any_tool_says_so_and_fails(monkeypatch, capsys):
    monkeypatch.setattr(P, "detect", lambda deep=False: {"claude": {"installed": False}})
    monkeypatch.setattr(remote, "setup", lambda *a, **k: pytest.fail("setup ran without a tool"))
    cli.configure(cliparse.resolve(["remote"], "trirouter", False))
    assert cli.run_remote() == 1 and "No AI tool" in capsys.readouterr().out


def test_a_loop_restart_stops_only_its_own_processes(tmp_path):
    script = tmp_path / "claude-remote.cmd"
    script.write_text('@echo off\n"C:\\bin\\claude.exe" remote-control --remote-control-session-name-prefix Box\n', encoding="utf-8")
    tail = remote._script_tail(script)
    assert tail == "remote-control --remote-control-session-name-prefix Box"
    ps = remote._stop_loop("claude-remote.cmd", tail)
    assert r"claude\-remote\.cmd" in ps and ".EndsWith('remote-control --remote-control-session-name-prefix Box')" in ps
    assert "ParentProcessId" in ps and '-match " remote-control "' not in ps  # never every remote-control session
    assert remote._script_tail(tmp_path / "missing.cmd") == ""
