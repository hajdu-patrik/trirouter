"""What the user sees: the banner, the session greeting and a setup run that changes nothing or cannot ask."""
import json
import os
import shutil

import pytest

from trirouter import __version__, banner, cli, core, hooks, hub, integrations as integ, platforms as P


class Screen:
    encoding = "utf-8"


@pytest.fixture(autouse=True)
def no_key_and_clean_flags(monkeypatch):
    """The key may come from this machine's environment; cli and hub keep the last run's flags in module state."""
    for name in ("TYPESAFE_API_KEY", "JEV_OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "load_config", dict)
    monkeypatch.setattr(core, "user_config", dict)
    monkeypatch.setattr(cli, "YES", False)
    monkeypatch.setattr(cli, "DRY", False)
    monkeypatch.setattr(hub, "APPLY", False)


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(P, "is_terminal", lambda stream: True)
    monkeypatch.setattr(P, "enable_ansi", lambda stream: True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((120, 40)))
    monkeypatch.delenv("NO_COLOR", raising=False)


def test_banner_stays_out_of_pipes_and_logs(monkeypatch):
    monkeypatch.setattr(P, "is_terminal", lambda stream: False)
    assert banner.render("status", Screen()) == ""


def test_banner_colors_one_part_per_tool_and_names_the_version(terminal):
    text = banner.render("status", Screen())
    assert all(f"\033[38;5;{c}m" in text for c in banner.COLORS)
    assert f"Welcome to trirouter {__version__}" in text and "status" in text


def test_banner_rows_line_up_at_the_color_splits():
    assert len({len(row) for row in banner.ART}) == 1


def test_no_color_keeps_the_art_plain(terminal, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\033" not in banner.render(stream=Screen()) and banner.ART[0] in banner.render(stream=Screen())


def test_banner_needs_room_and_an_encoding_that_has_its_characters(terminal, monkeypatch):
    class Latin(Screen):
        encoding = "ascii"
    assert banner.render(stream=Latin()) == ""
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((70, 40)))
    assert banner.render(stream=Screen()) == ""


def test_welcome_says_whether_setup_has_run(monkeypatch, capsys):
    monkeypatch.setattr(banner, "render", lambda status: status)
    cli.welcome()
    assert "Not set up yet" in capsys.readouterr().out
    P.atomic_write(integ.SHIM_HOOK, "shim")
    cli.welcome()
    assert "built-in local model" in capsys.readouterr().out


def test_python_dash_m_alone_shows_the_overview_and_changes_nothing(monkeypatch, capsys):
    monkeypatch.delenv("TRIROUTER_PROG", raising=False)
    monkeypatch.setattr(cli, "run_setup", lambda: pytest.fail("setup ran"))
    monkeypatch.setattr(cli, "program_name", lambda: "python -m trirouter")
    assert cli.main([]) == 0 and "Commands:" in capsys.readouterr().out


def test_setup_without_a_terminal_or_yes_changes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(P, "detect", lambda *a: pytest.fail("setup went on"))
    assert cli.main(["setup"]) == 1
    assert "--dry-run" in capsys.readouterr().out


def test_a_piped_key_still_runs_setup_without_a_terminal(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "detect_and_login", lambda chosen: seen.append(chosen) or [])
    monkeypatch.setattr(cli, "read_stdin", lambda: "key\n")
    assert cli.main(["setup", "--jev-token-stdin"]) == 1 and seen == [None]


def test_a_run_that_changed_nothing_skips_the_one_time_steps(monkeypatch, capsys):
    cli.next_steps(["claude", "codex"], 0, False)
    out = capsys.readouterr().out
    assert "Connected: claude, codex" in out and "built-in local model" in out
    assert "Next steps" not in out and "/hooks" not in out


def test_a_run_that_changed_something_lists_the_one_time_steps(capsys):
    cli.next_steps(["codex"], 1, True)
    out = capsys.readouterr().out
    assert "1 step(s) failed" in out and "/hooks" in out


def test_extras_ask_nothing_on_a_rerun_or_when_remote_is_set_up(monkeypatch, capsys):
    monkeypatch.setattr(cli, "ask", lambda *a: pytest.fail("asked"))
    monkeypatch.setattr(cli, "FLAGS", {})
    monkeypatch.setattr(P, "find_exe", lambda name: name)
    assert cli.extras(["claude"], {}, False) == 0
    assert cli.extras(["claude"], {"remote_name": "pc"}, True) == 0
    assert "remote" in capsys.readouterr().out


def test_nothing_to_migrate_means_no_question(monkeypatch, capsys):
    monkeypatch.setattr(hub, "migrate_plan", lambda: ([], []))
    monkeypatch.setattr(cli, "ask", lambda *a: pytest.fail("asked"))
    cli.migrate_skills()
    assert capsys.readouterr().out == ""


def test_same_json_in_another_format_is_no_change(tmp_path, capsys):
    path = tmp_path / "desktop.json"
    path.write_text('{"mcpServers":   {"x": 1}}', encoding="utf-8")
    w = integ.Writer(True, quiet=True)
    w.write_json(path, {"mcpServers": {"x": 1}}, "desktop")
    assert w.changes == 0 and capsys.readouterr().out == "" and "   " in path.read_text(encoding="utf-8")
    w.write_json(path, {"mcpServers": {"x": 2}}, "desktop")
    assert w.changes == 1


@pytest.fixture
def quiet_start(monkeypatch):
    for name in ("start_daemon", "start_model_discovery", "_purge_quarantine"):
        monkeypatch.setattr(hooks, name, lambda: None)


def test_a_new_session_is_greeted_outside_the_model_context(quiet_start):
    out = json.loads(hooks.session_start_output("claude", b'{"source": "startup"}'))
    assert list(out) == ["systemMessage"] and f"trirouter {__version__}" in out["systemMessage"]


@pytest.mark.parametrize("source", ["resume", "clear", "compact"])
def test_only_a_real_start_is_greeted(quiet_start, source):
    assert hooks.session_start_output("claude", json.dumps({"source": source}).encode()) == ""


def test_the_greeting_can_be_switched_off_and_skips_nested_runs(quiet_start, monkeypatch):
    monkeypatch.setenv("ROUTER_WELCOME", "0")
    assert hooks.session_start_output("claude", b'{"source": "startup"}') == ""
    monkeypatch.delenv("ROUTER_WELCOME")
    monkeypatch.setenv(hooks.NESTED_ENV, "1")
    assert hooks.session_start_output("claude", b'{"source": "startup"}') == ""


def test_the_greeting_travels_with_a_model_note(quiet_start, monkeypatch):
    monkeypatch.setattr(hooks, "model_note", lambda: "new model")
    out = json.loads(hooks.session_start_output("claude", b'{"source": "startup"}'))
    assert out["hookSpecificOutput"]["additionalContext"] == "new model" and "systemMessage" in out


def test_a_broken_config_loses_the_greeting_not_the_hook(quiet_start, monkeypatch):
    monkeypatch.setattr(core, "user_config", lambda: 1 / 0)
    assert hooks.session_start_output("claude", b'{"source": "startup"}') == ""
