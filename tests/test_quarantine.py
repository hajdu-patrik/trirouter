"""Tests for the batch quarantine question, sidecars, the automatic purge and the restore.

Everything runs in tmp dirs: nothing here touches the real ~/.jev-router or ~/.skills.
"""
import io
import json
import os
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_skillscan import REPORTS, all_of, apply_act, make_skills, scans  # noqa: F401 - `scans` is a fixture

from jev_router import cli, hooks as run_hook, platforms as P, skillscan


def flagged_reports(monkeypatch, *names):
    for n in names:
        monkeypatch.setitem(REPORTS, n, {"recommendation": "DO_NOT_INSTALL", "score": 80, "max_severity": "HIGH"})


def test_the_choice_is_asked_once_for_all_flagged_skills(tmp_path, scans, monkeypatch):
    flagged_reports(monkeypatch, "evil2", "evil3")
    hub_dir, asked = tmp_path / ".skills", []
    skills = make_skills(hub_dir, "evil", "evil2", "evil3", "fine")
    blocked = skillscan.gate(skills, apply_act, apply=True, choose=lambda items: asked.append(items) or {"evil", "evil3"})
    assert blocked == {"evil", "evil3"} and len(asked) == 1
    assert [i["name"] for i in asked[0]] == ["evil", "evil2", "evil3"]
    assert asked[0][0]["score"] == 100 and asked[0][0]["max_severity"] == "CRITICAL"
    assert (hub_dir / "evil2").is_dir() and not (hub_dir / "evil").exists()


def test_none_keeps_all_and_remembers_them(tmp_path, scans):
    hub_dir, asked = tmp_path / ".skills", []
    skills = make_skills(hub_dir, "evil")
    assert skillscan.gate(skills, apply_act, apply=True, choose=lambda items: asked.append(1) or set()) == set()
    assert skillscan.gate(skills, apply_act, apply=True, choose=lambda items: asked.append(1) or set()) == set()
    assert asked == [1] and (hub_dir / "evil").is_dir()


def test_nobody_to_ask_quarantines_nothing_and_remembers_nothing(tmp_path, scans, capsys):
    skills = make_skills(tmp_path / ".skills", "evil")
    assert skillscan.gate(skills, apply_act, apply=True, choose=lambda items: None) == set()
    assert not skillscan.QUARANTINE.exists() and "decide in an interactive" in capsys.readouterr().out
    assert "accepted" not in skillscan.load_cache()["evil"]


def test_quarantine_writes_a_sidecar(tmp_path, scans):
    skillscan.gate(make_skills(tmp_path / ".skills", "evil"), apply_act, apply=True, choose=all_of, days=3)
    meta = json.loads((skillscan.QUARANTINE / "evil.json").read_text(encoding="utf-8"))
    assert set(meta) == {"name", "quarantined_at", "purge_after", "risk", "max_severity"}
    assert meta["name"] == "evil" and meta["risk"] == 100 and meta["max_severity"] == "CRITICAL"
    when, due = (datetime.strptime(meta[k], "%Y-%m-%dT%H:%M:%SZ") for k in ("quarantined_at", "purge_after"))
    assert due - when == timedelta(days=3)


@pytest.mark.parametrize("text,expected", [("1", {0}), ("1,3", {0, 2}), ("2-4", {1, 2, 3}), (" 1, 3 ,5-6 ", {0, 2, 4, 5}),
                                           ("3,3,2-3", {1, 2})])
def test_selection_parsing(text, expected):
    assert skillscan.parse_selection(text, 6) == expected


@pytest.mark.parametrize("text", ["0", "7", "x", "1-", "-3", "4-2", "1;2", "2-9"])
def test_selection_parsing_rejects_garbage(text):
    with pytest.raises(ValueError):
        skillscan.parse_selection(text, 6)


ITEMS = [{"name": n, "score": 100, "max_severity": "HIGH", "path": f"/h/{n}"} for n in ("a", "b", "c")]


def ask_with(monkeypatch, answers):
    monkeypatch.setattr(cli, "YES", False)
    monkeypatch.setattr(cli, "interactive", lambda: True)
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    return cli.choose_quarantine(ITEMS)


def test_choose_all_none_and_default(monkeypatch, capsys):
    assert ask_with(monkeypatch, ["a"]) == {"a", "b", "c"}
    assert "  1. a (risk 100, max HIGH)" in capsys.readouterr().out
    assert ask_with(monkeypatch, ["n"]) == set()
    assert ask_with(monkeypatch, [""]) == set()
    assert ask_with(monkeypatch, ["what?", "ALL"]) == {"a", "b", "c"}


def test_choose_select_with_ranges_and_confirmation(monkeypatch, capsys):
    assert ask_with(monkeypatch, ["s", "1,3", "y"]) == {"a", "c"}
    out = capsys.readouterr().out
    assert "quarantine: a, c" in out and "keep:       b" in out
    assert ask_with(monkeypatch, ["s", "2-3", "Y"]) == {"b", "c"}


def test_choose_select_invalid_input_is_asked_again(monkeypatch, capsys):
    assert ask_with(monkeypatch, ["s", "9", "x", "1;", "2", "y"]) == {"b"}
    out = capsys.readouterr().out
    assert "out of range: choose from 1 to 3" in out and "not a number or a range" in out


def test_choose_declined_confirmation_goes_back_to_the_question(monkeypatch):
    assert ask_with(monkeypatch, ["s", "1", "n", "a"]) == {"a", "b", "c"}
    assert ask_with(monkeypatch, ["s", "", "n"]) == set()  # empty selection = none


def test_choose_unattended_asks_nobody(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked"))
    monkeypatch.setattr(cli, "YES", True)
    monkeypatch.setattr(cli, "interactive", lambda: True)
    assert cli.choose_quarantine(ITEMS) is None
    monkeypatch.setattr(cli, "YES", False)
    monkeypatch.setattr(cli, "interactive", lambda: False)
    assert cli.choose_quarantine(ITEMS) is None


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def put(q, name, days_ago, sidecar=True, files=("SKILL.md",)):
    """A quarantine entry quarantined `days_ago` days before NOW."""
    (q / name).mkdir(parents=True, exist_ok=True)
    for f in files:
        (q / name / f).parent.mkdir(parents=True, exist_ok=True)
        (q / name / f).write_text("x", encoding="utf-8")
    if sidecar:
        skillscan.write_sidecar(q / name, name.split("@")[0], 100, "HIGH", NOW - timedelta(days=days_ago), 3)
    return q / name


@pytest.fixture
def q(tmp_path, monkeypatch):
    monkeypatch.setattr(skillscan, "QUARANTINE", tmp_path / "quarantine")
    monkeypatch.setattr(skillscan, "CONFIG", tmp_path / "config.json")
    skillscan.QUARANTINE.mkdir()
    return skillscan.QUARANTINE


def test_purge_deletes_only_expired_entries(q, capsys):
    put(q, "old", 4)
    put(q, "fresh", 1)
    assert skillscan.purge_expired(True, now=NOW, days=3) == ["old"]
    assert not (q / "old").exists() and not (q / "old.json").exists()
    assert (q / "fresh" / "SKILL.md").is_file() and (q / "fresh.json").is_file()
    assert "skillscan: purge old (quarantined 2026-10-06)" in capsys.readouterr().out


def test_purge_dry_run_only_reports(q, capsys):
    put(q, "old", 9)
    assert skillscan.purge_expired(False, now=NOW, days=3) == ["old"]
    assert (q / "old").is_dir() and "[DRY] skillscan: purge old" in capsys.readouterr().out


def test_retention_zero_never_purges(q):
    put(q, "ancient", 400)
    assert skillscan.purge_expired(True, now=NOW, days=0) == []
    assert (q / "ancient").is_dir()


def test_retention_comes_from_config_json(q):
    put(q, "old", 4)
    assert skillscan.configured_days() == 3
    skillscan.CONFIG.write_text(json.dumps({"skillscan": {"quarantine_days": 10}}), encoding="utf-8")
    assert skillscan.configured_days() == 10
    assert skillscan.purge_expired(True, now=NOW) == []
    skillscan.CONFIG.write_text(json.dumps({"skillscan": {"quarantine_days": -1}}), encoding="utf-8")
    assert skillscan.configured_days() == 3


def test_entry_without_a_sidecar_uses_its_mtime_and_gets_one_only_when_applying(q):
    old = put(q, "legacy", 0, sidecar=False)
    stamp = (NOW - timedelta(days=5)).timestamp()
    os.utime(old, (stamp, stamp))
    rows = skillscan.entries(apply=False, days=3)
    assert rows[0]["quarantined_at"].date() == (NOW - timedelta(days=5)).date()
    assert not (q / "legacy.json").exists()
    skillscan.entries(apply=True, days=3)
    assert json.loads((q / "legacy.json").read_text(encoding="utf-8"))["name"] == "legacy"
    assert skillscan.purge_expired(True, now=NOW, days=3) == ["legacy"]


def test_purge_removes_read_only_files(q):
    entry = put(q, "ro", 9, files=("SKILL.md", "sub/x.py"))
    for f in (entry / "SKILL.md", entry / "sub" / "x.py"):
        os.chmod(f, stat.S_IREAD)
    assert skillscan.purge_expired(True, now=NOW, days=3) == ["ro"]
    assert not entry.exists()


def test_purge_refuses_a_link_out_of_the_quarantine_folder(q, tmp_path):
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    try:
        P.link_dir(q / "trap", outside)
    except OSError:
        pytest.skip("cannot create a link here")
    skillscan.write_sidecar(q / "trap", "trap", 100, "HIGH", NOW - timedelta(days=30), 3)
    assert not skillscan.inside_quarantine(q / "trap")
    assert skillscan.purge_expired(True, now=NOW, days=3, say=None) == []
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_purge_never_follows_a_link_inside_a_skill(q, tmp_path):
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    entry = put(q, "sneaky", 9)
    try:
        P.link_dir(entry / "inner", outside)
    except OSError:
        pytest.skip("cannot create a link here")
    assert skillscan.purge_expired(True, now=NOW, days=3) == ["sneaky"]
    assert not entry.exists() and (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_delete_refuses_paths_outside_the_quarantine(q, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    nested = put(q, "a", 0) / "deep"
    nested.mkdir()
    for bad in (other, q, nested, tmp_path):
        assert not skillscan.inside_quarantine(bad)
        with pytest.raises(OSError):
            skillscan._delete_entry(bad)
    assert other.is_dir() and nested.is_dir()


def test_restore_moves_the_newest_copy_back_and_tells_what_is_in_the_way(q, tmp_path):
    hub_dir = tmp_path / ".skills"
    put(q, "evil", 5)
    put(q, "evil@20261009-120000", 1)
    (q / "evil@20261009-120000" / "SKILL.md").write_text("newest", encoding="utf-8")
    restored, problems = skillscan.restore(["evil", "nope"], hub_dir, apply_act)
    assert restored == ["evil"] and problems == ["nope: not in quarantine"]
    assert (hub_dir / "evil" / "SKILL.md").read_text(encoding="utf-8") == "newest"
    assert not (q / "evil@20261009-120000").exists() and not (q / "evil@20261009-120000.json").exists()
    assert (q / "evil").is_dir()  # the older copy stays
    _, problems = skillscan.restore(["evil"], hub_dir, apply_act)  # the hub has the name now
    assert "already exists" in problems[0]


def test_restore_dry_run_moves_nothing(q, tmp_path):
    put(q, "evil", 1)
    skillscan.restore(["evil"], tmp_path / ".skills", lambda msg, fn=None: None)
    assert (q / "evil").is_dir() and not (tmp_path / ".skills").exists()


def test_restore_command_allows_the_skill_in_config(q, tmp_path, monkeypatch, capsys):
    from jev_router import hub
    put(q, "evil", 1)
    monkeypatch.setattr(hub, "HUB", tmp_path / ".skills")
    monkeypatch.setattr(cli, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(cli, "STATE", tmp_path)
    monkeypatch.setattr(cli, "DRY", False)
    monkeypatch.setattr(cli, "POSITIONAL", ["evil"])
    assert cli.quarantine_restore({}) == 0
    assert (tmp_path / ".skills" / "evil").is_dir()
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["skillscan"]["allow"] == ["evil"]
    assert "skills --apply" in capsys.readouterr().out
    monkeypatch.setattr(cli, "POSITIONAL", ["ghost"])
    assert cli.quarantine_restore({}) == 1


def test_purge_all_needs_a_confirmation_unless_yes(q, monkeypatch, capsys):
    put(q, "fresh", 0)
    monkeypatch.setattr(cli, "FLAGS", {"--all": True})
    monkeypatch.setattr(cli, "DRY", False)
    monkeypatch.setattr(cli, "YES", False)
    monkeypatch.setattr(cli, "interactive", lambda: False)
    monkeypatch.setattr(cli, "STATE", q.parent)
    monkeypatch.setattr(cli, "CONFIG", q.parent / "config.json")
    assert cli.quarantine_purge({}) == 1 and (q / "fresh").is_dir()
    assert "Cancelled" in capsys.readouterr().out
    monkeypatch.setattr(cli, "YES", True)
    assert cli.quarantine_purge({}) == 0 and not (q / "fresh").exists()


def test_quarantine_list_shows_the_purge_date(q, capsys):
    put(q, "evil", 0)
    assert cli.quarantine_list({}) == 0
    out = capsys.readouterr().out
    assert "evil" in out and "100 (HIGH)" in out and "NAME" in out


def test_session_start_hook_purges_at_most_every_six_hours_and_never_raises(q, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_hook.core, "STATE_DIR", tmp_path / "state-dir")
    put(q, "old", 9999)
    run_hook.on_session_start("claude", b"{}")
    assert not (q / "old").exists() and capsys.readouterr().out == ""
    put(q, "old2", 9999)
    run_hook.on_session_start("claude", b"{}")  # inside the 6 hours
    assert (q / "old2").is_dir()
    stamp = tmp_path / "state-dir" / "state" / "quarantine_purge.txt"
    stamp.write_text(str(time.time() - 7 * 3600), encoding="utf-8")
    run_hook.on_session_start("claude", b"{}")
    assert not (q / "old2").exists()

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(skillscan, "purge_expired", boom)
    stamp.write_text("0", encoding="utf-8")
    run_hook.on_session_start("claude", b"{}")  # swallowed
    monkeypatch.setattr(run_hook.core, "STATE_DIR", tmp_path / "state-dir" / "state" / "quarantine_purge.txt")  # a file, not a folder
    run_hook.on_session_start("claude", b"{}")


def test_session_start_through_main_exits_zero_and_prints_nothing(q, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_hook.core, "STATE_DIR", tmp_path / "s")
    put(q, "old", 9999)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"session_id": "x"}')))
    assert run_hook.main(["claude", "SessionStart"]) == 0
    assert capsys.readouterr().out == "" and not (q / "old").exists()
