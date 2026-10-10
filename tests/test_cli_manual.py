"""The command-line manual: flag validation, help pages, docs/cli.md in sync, and the `trirouter` launcher.

Nothing here runs a command that changes the machine, and the launcher tests never touch the real PATH or
registry (the PATH-editing functions are replaced by fakes).
"""
import os
import sys
from pathlib import Path

import pytest

from trirouter import cli, cliparse, integrations as I, manual, platforms as P

ROOT = Path(__file__).resolve().parent.parent
SECTIONS = ("NAME", "SYNOPSIS", "DESCRIPTION", "OPTIONS", "CHANGES", "EXAMPLES", "EXIT STATUS", "SEE ALSO")


@pytest.fixture(autouse=True)
def as_launcher(monkeypatch):
    monkeypatch.setenv("TRIROUTER_PROG", "trirouter")


def run(capsys, *argv):
    """(exit code, stdout, stderr) of cli.main for argv; only commands that change nothing are run through it."""
    try:
        code = cli.main(list(argv))
    except SystemExit as exc:
        code = exc.code
    out = capsys.readouterr()
    return code, out.out, out.err


# ---- help pages ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("key", list(manual.COMMANDS))
def test_every_help_page_has_all_sections(key):
    page = manual.render_help(key, "trirouter")
    positions = [page.index(f"\n{s}\n") if s != "NAME" else page.index("NAME\n") for s in SECTIONS]
    assert positions == sorted(positions)
    assert f"trirouter {key} - " in page and "-h, --help" in page
    assert page.count("EXAMPLES\n") == 1 and len(manual.COMMANDS[key].examples) >= 1


@pytest.mark.parametrize("key", list(manual.COMMANDS))
def test_every_flag_is_documented_with_default_and_memory(key):
    page = manual.render_help(key, "trirouter")
    for f in manual.COMMANDS[key].flags:
        assert f.long in page and f.doc
        assert f"Default: {f.default}. Remembered in config.json: {f.remembered}." in page.replace("\n          ", " ")


def test_changes_lines_are_plain_statements():
    assert manual.COMMANDS["skills"].changes == "Preview by default; --apply writes"
    assert manual.COMMANDS["setup"].changes.startswith("Changes files; preview with --dry-run")
    assert manual.COMMANDS["doctor"].changes == "Changes nothing"


def test_all_commands_of_the_spec_exist():
    assert list(manual.ORDER) == ["setup", "models", "skills", "quarantine", "remote", "doctor", "route", "uninstall", "help"]
    assert {"quarantine list", "quarantine restore", "quarantine purge"} <= set(manual.COMMANDS)


def test_help_command_and_flag_forms_print_the_same_page(capsys):
    _, by_command, _ = run(capsys, "help", "skills")
    _, by_flag, _ = run(capsys, "skills", "--help")
    _, by_short, _ = run(capsys, "skills", "-h")
    assert by_command == by_flag == by_short and "OPTIONS" in by_command
    assert run(capsys, "help", "quarantine", "purge")[1] == run(capsys, "quarantine", "purge", "-h")[1]
    assert run(capsys, "quarantine", "--help")[1].startswith("NAME\n    trirouter quarantine - ")
    code, out, _ = run(capsys, "route", "--help")
    assert code == 0 and "trirouter route - " in out


def test_launcher_without_arguments_prints_the_overview(capsys):
    code, out, _ = run(capsys)
    assert code == 0 and "Run `trirouter help <command>` for details" in out
    assert all(f"  {c} " in out for c in manual.ORDER)


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag(capsys, flag):
    from trirouter import __version__
    assert run(capsys, flag) == (0, f"trirouter {__version__}\n", "")


def test_overview_lists_exactly_the_nine_commands_and_the_version_flag(capsys):
    _, out, _ = run(capsys)
    rows = [line.split()[0] for line in out.split("Commands:\n")[1].split("\n\n")[0].splitlines()]
    assert rows == ["setup", "models", "skills", "quarantine", "remote", "doctor", "route", "uninstall", "help"]
    assert "-V, --version" in out and "-h, --help" in out


@pytest.mark.parametrize("old, replacement", [("detect", "trirouter doctor"), ("version", "trirouter --version"),
                                              ("completion", "trirouter setup")])
@pytest.mark.parametrize("extra", [[], ["--yes"], ["--help"], ["--shell=bash", "--install"]])
def test_removed_commands_give_a_usage_error_naming_the_replacement(capsys, old, replacement, extra):
    code, out, err = run(capsys, old, *extra)
    assert code == 2 and out == "" and f"{old} was removed" in err and replacement in err


def test_removed_models_probe_flag(capsys):
    code, out, err = run(capsys, "models", "--probe")
    assert code == 2 and out == "" and "--probe was removed" in err and "trirouter models" in err


def test_install_py_forms_of_the_removed_commands():
    for argv in (["detect"], ["--yes", "detect"], ["completion", "--install"], ["version"]):
        with pytest.raises(cliparse.UsageError, match="was removed"):
            cliparse.resolve(argv, "python install.py", True)
    assert cliparse.resolve(["--version"], "python install.py", True).kind == "version"


def test_unknown_help_topic_suggests(capsys):
    code, _, err = run(capsys, "help", "skils")
    assert code == 2 and "did you mean skills?" in err


# ---- flag validation ----------------------------------------------------------------------------------

def test_unknown_flag_is_exit_2_with_a_suggestion_and_a_pointer(capsys):
    code, out, err = run(capsys, "skills", "--aply")
    assert code == 2 and out == ""
    assert "unknown option --aply" in err and "did you mean --apply?" in err and "trirouter help skills" in err


def test_a_flag_of_another_command_is_not_applicable(capsys):
    code, _, err = run(capsys, "doctor", "--yes")
    assert code == 2 and "option --yes does not apply here" in err and "accepted by:" in err and "setup" in err
    code, _, err = run(capsys, "skills", "--providers=claude")
    assert code == 2 and "does not apply here" in err and "accepted by:" in err and "setup" in err


def test_value_flag_without_value_and_boolean_with_value_are_errors(capsys):
    assert "needs a value" in run(capsys, "setup", "--jev-token")[2]
    assert "needs a value" in run(capsys, "setup", "--jev-token", "--yes")[2]
    assert run(capsys, "setup", "--yes=1")[0] == 2
    assert "does not take a value" in run(capsys, "skills", "--apply=yes")[2]
    assert "invalid value" in run(capsys, "skills", "--quarantine-days=abc")[2]
    assert "invalid value" in run(capsys, "skills", "--quarantine-days=-1")[2]
    assert "expected on or off" in run(capsys, "skills", "--scan-llm=maybe")[2]


def test_short_flags_and_clusters():
    inv = cliparse.resolve(["skills", "-ay"], "trirouter", False)
    assert inv.flags == {"--apply": True, "--yes": True}
    assert cliparse.resolve(["setup", "-n"], "trirouter", False).flags == {"--dry-run": True}
    with pytest.raises(cliparse.UsageError):
        cliparse.resolve(["doctor", "-n"], "trirouter", False)
    with pytest.raises(cliparse.UsageError):
        cliparse.resolve(["skills", "-q"], "trirouter", False)


def test_each_command_accepts_exactly_its_flags():
    for key, c in manual.COMMANDS.items():
        if key in ("route", "help", "quarantine") or key == "quarantine restore":
            continue
        for f in c.flags:
            arg = [f.long + ("=1" if f.kind == "int" else ("=" + f.choices[0]) if f.choices else "=x")] if f.value else [f.long]
            cmd = key.split()
            assert cliparse.resolve(cmd + arg, "trirouter", False).flags[f.long]
    with pytest.raises(cliparse.UsageError):
        cliparse.resolve(["quarantine", "list", "--all"], "trirouter", False)
    assert cliparse.resolve(["quarantine", "purge", "--all", "-n"], "trirouter", False).key == "quarantine purge"


def test_value_forms_and_the_optional_remote_value():
    assert cliparse.resolve(["setup", "--remote"], "trirouter", False).flags["--remote"] is True
    assert cliparse.resolve(["setup", "--remote=My PC"], "trirouter", False).flags["--remote"] == "My PC"
    assert cliparse.resolve(["setup", "--remote", "My PC", "-y"], "trirouter", False).flags["--remote"] == "My PC"
    assert cliparse.resolve(["remote", "--name", "Box"], "trirouter", False).flags["--name"] == "Box"


def test_quarantine_subcommands():
    assert cliparse.resolve(["quarantine"], "trirouter", False).key == "quarantine list"
    inv = cliparse.resolve(["quarantine", "restore", "a", "b", "-n"], "trirouter", False)
    assert inv.key == "quarantine restore" and inv.positional == ["a", "b"] and inv.flags == {"--dry-run": True}
    with pytest.raises(cliparse.UsageError, match="at least one"):
        cliparse.resolve(["quarantine", "restore"], "trirouter", False)
    with pytest.raises(cliparse.UsageError, match="unknown subcommand"):
        cliparse.resolve(["quarantine", "purg"], "trirouter", False)
    with pytest.raises(cliparse.UsageError, match="unexpected argument"):
        cliparse.resolve(["skills", "extra"], "trirouter", False)


def test_unknown_command_suggests(capsys):
    code, _, err = run(capsys, "skils")
    assert code == 2 and "unknown command 'skils'" in err and "did you mean skills?" in err


def test_install_py_keeps_its_old_forms():
    """`python install.py` (no TRIROUTER_PROG): setup by default, flags before the command."""
    assert cliparse.resolve([], "python install.py", True).key == "setup"
    inv = cliparse.resolve(["--yes", "--providers=claude", "--no-migrate"], "python install.py", True)
    assert inv.key == "setup" and set(inv.flags) == {"--yes", "--providers", "--no-migrate"}
    assert cliparse.resolve(["--dry-run", "skills"], "python install.py", True).key == "skills"
    assert cliparse.resolve(["--name", "My PC", "remote"], "python install.py", True).flags == {"--name": "My PC"}
    assert cliparse.resolve(["models", "--discover"], "python install.py", True).flags == {"--discover": True}
    assert cliparse.resolve([], "trirouter", False).kind == "overview"
    with pytest.raises(cliparse.UsageError, match="unknown option --bogus"):
        cliparse.resolve(["setup", "--bogus"], "python install.py", True)


def test_route_keeps_its_parsing(capsys):
    assert cli.parse_route_args(["--json", "--", "--not-an-option", "x"]) == ("claude", True, "--not-an-option x")
    with pytest.raises(ValueError, match="did you mean --json"):
        cli.parse_route_args(["--jsn", "x"])


# ---- docs/cli.md ----------------------------------------------------------------------------------------

def test_docs_cli_md_is_generated_from_the_manual():
    text = (ROOT / "docs" / "cli.md").read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert text == manual.render_markdown(), "regenerate: TRIROUTER_PROG=trirouter python -m trirouter help --markdown > docs/cli.md"


def test_help_markdown_prints_the_same(capsys):
    code, out, _ = run(capsys, "help", "--markdown")
    assert code == 0 and out.replace("\r\n", "\n") == manual.render_markdown()


def test_markdown_has_contents_and_a_section_per_command():
    md = manual.render_markdown()
    for heading in ("## Getting started", "## Common tasks", "## Contents"):
        assert heading in md
    for key in manual.COMMANDS:
        assert f"\n{'###' if ' ' in key else '##'} {key}\n" in md


def test_every_command_in_docs_exists():
    """No doc mentions a trirouter command or flag that does not exist."""
    import re
    known_flags = {f.long for c in manual.COMMANDS.values() for f in manual.all_flags(c)} | {t[0] for t in manual.TOP_FLAGS}
    for path in [ROOT / "README.md", ROOT / "CLAUDE.md", *(ROOT / "docs").glob("*.md")]:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"(?:`|^\s*(?:\$ )?)(?:trirouter|python install\.py|python -m trirouter) ([a-z]+)", text, re.M):
            assert m.group(1) in manual.COMMANDS, (path.name, m.group(0))
        for flag in re.findall(r"`(?:trirouter|python install\.py)[^`\n]*?(--[a-z-]+)", text):
            assert flag in known_flags, (path.name, flag)


# ---- the launcher ---------------------------------------------------------------------------------------

def test_windows_launcher_uses_python_exe_and_passes_arguments():
    text = I.launcher_cmd("C:/Python/python.exe")
    assert text.startswith("@echo off\r\n") and text.endswith("\r\n")
    assert '"C:/Python/python.exe" "%~dp0trirouter.py" %*' in text and "pythonw" not in text
    assert "TRIROUTER_PROG" in I.launcher_py(windows=True) and 'run_module("trirouter"' in I.launcher_py(windows=True)


def test_windows_launcher_never_uses_the_windowless_interpreter(monkeypatch):
    monkeypatch.setattr(P, "python_exe", lambda: "C:/Python/python.exe")
    assert "pythonw" not in I.launcher_cmd()


def test_posix_launcher_has_a_shebang_and_the_repo_path():
    text = I.launcher_py(python="/usr/bin/python3", windows=False)
    assert text.startswith("#!/usr/bin/python3\n") and str(I.REPO) in text and "TRIROUTER_PROG" in text
    spaced = I.launcher_py(python="/opt/my py/python3", windows=False)
    assert spaced.startswith("#!/bin/sh\n") and 'exec "/opt/my py/python3" "$0" "$@"' in spaced
    compile(text, "trirouter", "exec")
    compile(spaced, "trirouter", "exec")


def test_user_path_editing_is_idempotent_and_keeps_everything_else():
    entry = r"C:\Users\x\.trirouter\bin"
    base = r"C:\Windows;%USERPROFILE%\bin;"
    once = I.path_with(base, entry)
    assert once == r"C:\Windows;%USERPROFILE%\bin;" + entry
    assert I.path_with(once, entry) == once
    assert I.path_with(once, entry.lower() + "\\") == once  # same folder, different spelling: no duplicate
    assert I.path_with("", entry) == entry
    assert I.path_without(once, entry) == r"C:\Windows;%USERPROFILE%\bin"
    assert I.path_without(base, entry) == r"C:\Windows;%USERPROFILE%\bin"


@pytest.fixture
def fake_windows(tmp_path, monkeypatch):
    """Windows behavior on any OS, with a fake user PATH instead of the registry."""
    monkeypatch.setattr(P, "IS_WINDOWS", True)
    monkeypatch.setattr(P, "python_exe", lambda: "C:/Python/python.exe")
    monkeypatch.setattr(I, "BIN", tmp_path / "bin")
    store = {"path": (r"C:\Windows;%USERPROFILE%\bin", 2), "broadcast": 0}
    monkeypatch.setattr(I, "user_path_read", lambda: store["path"])
    monkeypatch.setattr(I, "user_path_write", lambda value, kind: store.update(path=(value, kind)))
    monkeypatch.setattr(I, "broadcast_env_change", lambda: store.update(broadcast=store["broadcast"] + 1))
    return store


def test_install_launcher_on_windows_dry_run_apply_and_idempotence(fake_windows, tmp_path, capsys):
    I.install_launcher(I.Writer(apply=False))
    out = capsys.readouterr().out
    assert "[DRY] trirouter command: create" in out and "to your user PATH" in out
    assert not (tmp_path / "bin").exists() and fake_windows["path"][0] == r"C:\Windows;%USERPROFILE%\bin"

    w = I.Writer(apply=True)
    I.install_launcher(w)
    out = capsys.readouterr().out
    assert "[DO]  trirouter command: create" in out and "reopen" in out and w.changes == 3
    assert (tmp_path / "bin" / "trirouter.cmd").read_bytes().startswith(b"@echo off\r\n")
    value, kind = fake_windows["path"]
    assert value == r"C:\Windows;%USERPROFILE%\bin;" + str(tmp_path / "bin") and kind == 2  # REG_EXPAND_SZ kept
    assert fake_windows["broadcast"] == 1

    w = I.Writer(apply=True)
    I.install_launcher(w)
    assert "[OK]  trirouter command: up to date" in capsys.readouterr().out and w.changes == 0
    assert fake_windows["path"][0] == value and fake_windows["broadcast"] == 1

    I.uninstall_launcher(I.Writer(apply=True))
    assert not (tmp_path / "bin" / "trirouter.cmd").exists() and not (tmp_path / "bin" / "trirouter.py").exists()
    assert fake_windows["path"][0] == r"C:\Windows;%USERPROFILE%\bin"
    I.uninstall_launcher(I.Writer(apply=True))
    assert "not installed" in capsys.readouterr().out


def test_launcher_status_reports_missing_and_present(fake_windows, tmp_path):
    ok, detail = I.launcher_status()
    assert not ok and "missing" in detail
    I.install_launcher(I.Writer(apply=True))
    ok, detail = I.launcher_status()
    assert ok and "on PATH" in detail
    fake_windows["path"] = (r"C:\Windows", 2)
    assert not I.launcher_status()[0]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="needs symlinks and POSIX permissions")
def test_install_launcher_on_posix_links_into_local_bin(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(I, "BIN", tmp_path / "bin")
    monkeypatch.setattr(I, "LOCAL_BIN", tmp_path / "local-bin")
    monkeypatch.setenv("PATH", "/usr/bin")
    I.install_launcher(I.Writer(apply=True))
    out = capsys.readouterr().out
    exe, link = tmp_path / "bin" / "trirouter", tmp_path / "local-bin" / "trirouter"
    assert os.access(exe, os.X_OK) and link.is_symlink() and os.path.realpath(link) == os.path.realpath(exe)
    assert 'export PATH="$HOME/.local/bin:$PATH"' in out and "shell profile" in out
    monkeypatch.setenv("PATH", f"/usr/bin:{tmp_path / 'local-bin'}")
    I.install_launcher(I.Writer(apply=True))
    assert "[OK]  trirouter command: up to date" in capsys.readouterr().out
    I.uninstall_launcher(I.Writer(apply=True))
    assert not exe.exists() and not link.is_symlink()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="needs symlinks")
def test_a_foreign_file_in_local_bin_is_left_alone(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(I, "BIN", tmp_path / "bin")
    monkeypatch.setattr(I, "LOCAL_BIN", tmp_path / "local-bin")
    (tmp_path / "local-bin").mkdir()
    (tmp_path / "local-bin" / "trirouter").write_text("mine", encoding="utf-8")
    I.install_launcher(I.Writer(apply=True))
    assert (tmp_path / "local-bin" / "trirouter").read_text(encoding="utf-8") == "mine"
    assert "left alone" in capsys.readouterr().out
    I.uninstall_launcher(I.Writer(apply=True))
    assert (tmp_path / "local-bin" / "trirouter").read_text(encoding="utf-8") == "mine"


def test_command_hint_follows_the_launcher(monkeypatch, tmp_path):
    monkeypatch.delenv("TRIROUTER_PROG")
    monkeypatch.setattr(P, "launcher_path", lambda: tmp_path / "nope")
    assert P.command_hint("doctor") == "python install.py doctor"
    (tmp_path / "there").write_text("x", encoding="utf-8")
    monkeypatch.setattr(P, "launcher_path", lambda: tmp_path / "there")
    assert P.command_hint("doctor") == "trirouter doctor"
