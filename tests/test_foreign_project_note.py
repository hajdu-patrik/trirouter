import pytest

from trirouter import core


def _make_project(tmp_path, name):
    project = tmp_path / name
    project.mkdir()
    (project / "CLAUDE.md").write_text("# guidelines\n", encoding="utf-8")
    return project


def test_no_note_without_a_foreign_path(tmp_path):
    here = _make_project(tmp_path, "here")
    assert core.foreign_project_note("just a normal request, no paths here", str(here)) is None


def test_notes_a_different_project_with_its_own_claude_md(tmp_path):
    here = _make_project(tmp_path, "here")
    other = _make_project(tmp_path, "other")
    prompt = f"work on the file at {other / 'src' / 'app.py'} please"
    note = core.foreign_project_note(prompt, str(here))
    assert note is not None
    assert str(other) in note
    assert str(here) in note


def test_no_note_for_a_path_inside_the_current_project(tmp_path):
    here = _make_project(tmp_path, "here")
    (here / "src").mkdir()
    prompt = f"edit {here / 'src' / 'app.py'}"
    assert core.foreign_project_note(prompt, str(here)) is None


def test_no_note_when_the_referenced_path_has_no_claude_config(tmp_path):
    here = _make_project(tmp_path, "here")
    plain = tmp_path / "plain"
    (plain / "docs").mkdir(parents=True)
    prompt = f"read {plain / 'docs' / 'notes.txt'}"
    assert core.foreign_project_note(prompt, str(here)) is None


def test_no_note_when_the_current_project_is_an_ancestor_of_the_referenced_path(tmp_path):
    here = _make_project(tmp_path, "here")
    (here / "vendor" / "lib").mkdir(parents=True)
    (here / "vendor" / "lib" / "CLAUDE.md").write_text("# vendored\n", encoding="utf-8")
    prompt = f"look at {here / 'vendor' / 'lib' / 'thing.py'}"
    assert core.foreign_project_note(prompt, str(here)) is None


def test_never_raises_on_empty_or_missing_cwd():
    assert core.foreign_project_note("anything C:\\nowhere\\real", "") is None
    assert core.foreign_project_note("anything", None) is None


def test_never_raises_on_garbage_prompt_text(tmp_path, monkeypatch):
    here = _make_project(tmp_path, "here")
    # as in CI, where the cwd is the repo checkout: "C:\" on POSIX must not resolve to it
    monkeypatch.chdir(_make_project(tmp_path, "process_cwd"))
    garbage = "C:\\ * ? | < > \" weird/// http://example.com/a/b/c ../../.. "
    assert core.foreign_project_note(garbage, str(here)) is None


def test_unrooted_candidate_never_resolves_to_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(_make_project(tmp_path, "process_cwd"))
    assert core._longest_existing_prefix("relative/dir and more words") is None


@pytest.mark.parametrize("remote", [
    "//attacker.example/share/log.txt", "\\\\attacker.example\\share\\log.txt", "\\\\?\\C:\\Windows\\x",
    "\\\\.\\PhysicalDrive0",
])
def test_remote_and_device_paths_are_never_probed(tmp_path, monkeypatch, remote):
    """On Windows exists() on a UNC path contacts the host (an SMB round trip with the user's credentials)."""
    here = _make_project(tmp_path, "here")
    probed = []
    monkeypatch.setattr(core.Path, "exists", lambda self: probed.append(str(self)) or False)
    assert core.foreign_project_note(f"check the logs at {remote} and see", str(here)) is None
    assert not any(p.startswith(("//", "\\\\")) for p in probed)


def test_only_the_sessions_own_drive_is_probed(tmp_path, monkeypatch):
    here = _make_project(tmp_path, "here")
    probed = []
    monkeypatch.setattr(core.Path, "exists", lambda self: probed.append(str(self)) or False)
    other = "Z:" if core.Path(str(here)).drive.upper() != "Z:" else "Y:"
    core.foreign_project_note(f"see {other}\\projects\\x\\file.py please", str(here))
    assert not any(p.upper().startswith(other) for p in probed)


@pytest.mark.parametrize("name", ["NUL", "con", "COM1", "lpt1.txt", "AUX.log", "PRN", "CONOUT$", "nul.tar.gz",
                                  "NUL:", "COM1:", "nul:stream", "AUX:.txt"])
def test_a_dos_device_name_in_any_component_is_never_probed(tmp_path, monkeypatch, name):
    """Windows resolves C:/NUL or C:/logs/COM1.txt to the device: exists() would open it (a serial port blocks)."""
    here = _make_project(tmp_path, "here")
    probed = []
    monkeypatch.setattr(core.Path, "exists", lambda self: probed.append(str(self)) or False)
    for text in (f"{here / name}", f"{here / name / 'file.py'}", f"{here}\\{name}", f"{here.drive}\\{name}"):
        assert core.foreign_project_note(f"look at {text} now", str(here)) is None
    assert not any(core._DOS_DEVICE_RE.fullmatch(part) for p in probed for part in core.Path(p).parts[1:])


@pytest.mark.parametrize("name", ["NULL", "console", "COM10", "auxiliary", "nul-file", "my.NUL", "COM1x:", "a:b"])
def test_an_ordinary_name_is_no_dos_device_name(name):
    assert not core._DOS_DEVICE_RE.fullmatch(name)
