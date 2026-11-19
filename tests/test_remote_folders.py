"""Remote folders: one Claude Remote Control server per saved folder in place of the single --workdir server.
_ps, the help text and the trust check are fakes."""
import json
from pathlib import Path

import pytest

from trirouter import cli, cliparse, platforms as P, remote

NEW_HELP = "OPTIONS\n  --remote-control-session-name-prefix <prefix>\n  --[no-]create-session-in-dir\n"


@pytest.fixture
def windows(monkeypatch, tmp_path):
    """A Windows machine with Claude installed; returns the PowerShell scripts that ran."""
    calls = []
    monkeypatch.setattr(P, "IS_WINDOWS", True)
    monkeypatch.setattr(remote, "BIN", tmp_path / "bin")
    monkeypatch.setattr(remote, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(cli, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(remote, "_ps", lambda script: calls.append(script) or (0, ""))
    monkeypatch.setattr(remote, "_rc_help", lambda claude: NEW_HELP)
    monkeypatch.setattr(remote, "claude_trusts", lambda folder: Path(folder).name != "untrusted")
    monkeypatch.setattr(P, "find_exe", lambda exe: r"C:\bin\claude.exe" if exe == "claude" else None)
    (tmp_path / "bin").mkdir()
    return calls


def script(name):
    return (remote.BIN / f"claude-remote-{name}.cmd").read_bytes().decode("utf-8")


def test_each_folder_gets_its_own_named_server_and_the_single_one_goes(windows, tmp_path):
    (remote.BIN / "claude-remote.cmd").write_text('"C:\\bin\\claude.exe" remote-control --name Box\n', encoding="utf-8")
    remote.save_folder("web", tmp_path / "web")
    remote.save_folder("docs", tmp_path / "docs")
    report = remote.setup("Box", ["claude"], workdir=tmp_path, start_session=True)
    assert [ok for _, ok, _ in report] == [True, True, True]  # two folders and the watchdog
    assert '"C:\\bin\\claude.exe" remote-control --name web\r\n' in script("web")
    assert f'cd /d "{tmp_path / "docs"}"' in script("docs")
    assert not (remote.BIN / "claude-remote.cmd").exists()
    assert any("Unregister-ScheduledTask" in c and f'"{remote.TASK_CLAUDE}"' in c for c in windows)
    watchdog = (remote.BIN / "remote-watchdog.ps1").read_text(encoding="utf-8")
    assert f"'{remote.TASK_CLAUDE} - web', '{remote.TASK_CLAUDE} - docs'" in watchdog


def test_without_start_session_a_folder_server_only_holds_the_connection(windows, tmp_path):
    remote.save_folder("web", tmp_path)
    (_, ok, msg), _ = remote.setup("Box", ["claude"], workdir=tmp_path, start_session=False)
    assert ok and "only the connection" in msg
    assert "remote-control --no-create-session-in-dir --remote-control-session-name-prefix web\r\n" in script("web")


def test_an_untrusted_folder_is_reported_and_the_others_still_start(windows, tmp_path):
    remote.save_folder("bad", tmp_path / "untrusted")
    remote.save_folder("good", tmp_path / "good")
    (_, bad_ok, bad_msg), (_, good_ok, _), _ = remote.setup("Box", ["claude"], workdir=tmp_path)
    assert not bad_ok and bad_msg.startswith("bad: ") and "trust dialog" in bad_msg
    assert good_ok and not (remote.BIN / "claude-remote-bad.cmd").exists()


def test_a_folder_is_moved_by_name_and_removed_with_its_server(windows, tmp_path):
    remote.save_folder("web", tmp_path / "a")
    remote.save_folder("WEB", tmp_path / "b")
    assert remote.folders_saved() == [{"name": "WEB", "workdir": str(tmp_path / "b")}]
    remote.setup("Box", ["claude"], workdir=tmp_path)
    assert remote.remove_folder("web") and not remote.remove_folder("web")
    assert remote.folders_saved() == [] and not (remote.BIN / "claude-remote-WEB.cmd").exists()
    assert any("Stop-Process" in c and "claude\\-remote\\-WEB\\.cmd" in c for c in windows)


def test_the_spawn_mode_is_given_so_a_git_folder_never_waits_for_an_answer(monkeypatch):
    monkeypatch.setattr(remote, "_rc_help", lambda claude: NEW_HELP + "  --spawn <mode>   Spawn mode\n")
    assert remote.claude_rc_args("claude", "web", True) == ["remote-control", "--name", "web", "--spawn", "same-dir"]
    assert remote.claude_rc_args("claude", "web")[:3] == ["remote-control", "--spawn", "same-dir"]
    monkeypatch.setattr(remote, "_rc_help", lambda claude: NEW_HELP)  # a Claude Code without the flag
    assert "--spawn" not in remote.claude_rc_args("claude", "web", True)


def test_broken_folder_entries_are_ignored(windows):
    remote.CONFIG.write_text(json.dumps({"remote_folders": [
        {"name": "ok", "workdir": "C:\\x"}, {"name": "has space", "workdir": "C:\\y"}, {"name": "w"}, "junk"]}),
        encoding="utf-8")
    assert remote.folders_saved() == [{"name": "ok", "workdir": "C:\\x"}]


def test_status_has_one_line_per_folder(windows, tmp_path):
    remote.save_folder("web", tmp_path)
    remote.setup("Box", ["claude"], workdir=tmp_path, start_session=True)
    tasks = {f"{remote.TASK_CLAUDE} - web": "Running"}
    running = 'claude.exe|"c:\\bin\\claude.exe" remote-control --name web'
    assert remote._folder_status(tasks, running)[0][1] is True
    assert remote._folder_status(tasks, running + "2")[0][1] is False  # another folder's server
    assert remote._folder_status({}, running)[0][1] is False


def test_remove_takes_the_folder_servers_too(windows, tmp_path):
    remote.save_folder("web", tmp_path)
    remote.setup("Box", ["claude"], workdir=tmp_path)
    assert remote.remove(["claude"]) == [("claude", True, "scheduled tasks removed, servers stopped")]
    assert not (remote.BIN / "claude-remote-web.cmd").exists()


@pytest.mark.parametrize("mac", [True, False])
def test_launchd_and_systemd_get_one_service_per_folder(monkeypatch, tmp_path, mac):
    calls = []
    monkeypatch.setattr(P, "IS_WINDOWS", False)
    monkeypatch.setattr(P, "IS_MAC", mac)
    monkeypatch.setattr(remote.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(remote, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(remote, "LAUNCHD", tmp_path / "LaunchAgents" / f"{remote.LAUNCHD_LABEL}.plist")
    monkeypatch.setattr(remote, "SYSTEMD", tmp_path / "systemd" / "trirouter-claude-remote.service")
    monkeypatch.setattr(remote, "LOG", tmp_path / "logs" / "claude-remote.log")
    monkeypatch.setattr(P, "run", lambda cmd, **k: calls.append(cmd) or (0, ""))
    monkeypatch.setattr(P, "find_exe", lambda exe: "/usr/bin/claude" if exe == "claude" else None)
    monkeypatch.setattr(remote, "claude_trusts", lambda folder: True)
    remote.save_folder("web", tmp_path)
    (_, ok, _), = remote.setup("Box", ["claude"], workdir=tmp_path, start_session=True)
    service = (tmp_path / "LaunchAgents" / f"{remote.LAUNCHD_LABEL}.web.plist" if mac
               else tmp_path / "systemd" / "trirouter-claude-remote-web.service")
    assert ok and service.exists() and remote.status() == [("claude", True, str(service))]
    assert remote.remove(["claude"]) == [("claude", True, f"{'launchd agent' if mac else 'systemd user service'} "
                                                          f"{service.name} removed")]
    assert not service.exists() and remote.status() == [("claude", False, f"{service.name} not set up")]


def run_cli(args, monkeypatch):
    monkeypatch.setattr(P, "detect", lambda deep=False: {"claude": {"installed": True}})
    monkeypatch.setattr(remote, "setup", lambda *a, **k: [])
    cli.configure(cliparse.resolve(["remote", *args], "trirouter", False))
    return cli.run_remote()


def test_cli_adds_and_removes_folders(windows, tmp_path, monkeypatch, capsys):
    assert run_cli([f"--add-folder=web={tmp_path}"], monkeypatch) == 0
    assert remote.folders_saved() == [{"name": "web", "workdir": str(tmp_path.resolve())}]
    assert run_cli(["--remove-folder", "web"], monkeypatch) == 0 and remote.folders_saved() == []
    assert run_cli(["--remove-folder", "web"], monkeypatch) == 1 and "no remote folder" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["web", "=C:\\x", "bad name=C:\\x", "web=", "web=missing-folder"])
def test_cli_refuses_an_unusable_folder(windows, monkeypatch, capsys, value):
    assert run_cli([f"--add-folder={value}"], monkeypatch) == 1 and "--add-folder" in capsys.readouterr().out
    assert remote.folders_saved() == []


def test_cli_dry_run_saves_nothing(windows, tmp_path, monkeypatch):
    assert run_cli([f"--add-folder=web={tmp_path}", "-n"], monkeypatch) == 0
    assert remote.folders_saved() == []
