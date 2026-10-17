"""Remote access (trirouter/remote.py): Claude's Remote Control server only holds the connection at logon (no
pre-created session) unless --start-session=on, on every OS; old Claude Code versions keep the classic `--name`.
Nothing here registers a real task or service: _ps and the help text are fakes."""
import json
import plistlib

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
    remote._setup_claude("My PC", r"C:\bin\claude.exe", tmp_path)
    script = (tmp_path / "claude-remote.cmd").read_bytes().decode("utf-8")
    assert ('"C:\\bin\\claude.exe" remote-control --no-create-session-in-dir '
            '--remote-control-session-name-prefix "My PC"\r\n') in script
    remote._setup_claude("My PC", r"C:\bin\claude.exe", tmp_path, start_session=True)
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
    monkeypatch.setattr(remote, "_setup_claude", lambda name, claude, wd, start: seen.append(start) or (True, ""))
    monkeypatch.setattr(P, "IS_WINDOWS", False)
    (tool, ok, msg), = remote.setup("Box", ["claude"], workdir=tmp_path)
    assert ok and seen == [False] and "only the connection opens" in msg
    remote.setup("Box", ["claude"], workdir=tmp_path, start_session=True)
    assert json.loads((tmp_path / "config.json").read_text())["remote_start_session"] is True
    (_, _, msg), = remote.setup("Box", ["claude"], workdir=tmp_path)  # None: the saved choice
    assert seen == [False, True, True] and "pre-created" in msg


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
