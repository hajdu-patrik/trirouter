"""`trirouter sync` (trirouter/sync.py): docs/cli.md, the git pre-commit hook setting, the installed tab completion
and the generated agents follow the sources; --check changes nothing; --pre-commit stages the regenerated reference
and stops on failure. Every test works on a temporary checkout and home; nothing touches the real ones."""
import shutil
import subprocess
from pathlib import Path

import pytest

from trirouter import cli, cliparse, completion as C, hub, manual, platforms as P, sync

ROOT = Path(__file__).resolve().parent.parent
HAS_GIT = shutil.which("git") is not None


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A temporary git checkout with an out-of-date docs/cli.md, used as sync.REPO."""
    if not HAS_GIT:
        pytest.skip("needs git")
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "trirouter").mkdir()
    (repo / "trirouter" / "manual.py").write_text("# manual\n", encoding="utf-8")
    (repo / "docs" / "cli.md").write_text("old\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    monkeypatch.setattr(sync, "REPO", repo)
    monkeypatch.setattr(sync, "DOCS_CLI", repo / "docs" / "cli.md")
    return repo


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    """No completion and no agents installed, unless a test adds them."""
    home = tmp_path / "home"
    monkeypatch.setattr(P, "HOME", home)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for name in ("CLAUDE_AGENTS", "CODEX_AGENTS", "AGY_AGENTS"):
        monkeypatch.setattr(hub, name, home / name.lower())
    monkeypatch.setattr(hub, "CODEX_CONFIG", home / "codex-config.toml")
    return home


def by_label(lines):
    return {label: (stale, detail) for label, stale, detail in lines}


def test_check_reports_and_changes_nothing(checkout, user_home, capsys):
    assert sync.main({"--check": True}) == 1
    out = capsys.readouterr().out
    assert "docs/cli.md: out of date" in out and "core.hooksPath not set" in out and "trirouter sync" in out
    assert (checkout / "docs" / "cli.md").read_text(encoding="utf-8") == "old\n"
    assert subprocess.run(["git", "-C", str(checkout), "config", "--get", "core.hooksPath"]).returncode == 1
    assert sync.main({"--dry-run": True}) == 0  # a preview is not a failure


def test_check_passes_on_a_fresh_clone_with_current_files(checkout, user_home, capsys):
    """CI: docs/cli.md is current, the clone has no core.hooksPath - reported, but not a failure."""
    (checkout / "docs" / "cli.md").write_bytes(manual.render_markdown().encode("utf-8"))
    assert sync.main({"--check": True}) == 0
    assert "core.hooksPath not set" in capsys.readouterr().out


def test_sync_regenerates_docs_with_lf_and_sets_the_hook_folder(checkout, user_home):
    assert sync.main({}) == 0
    raw = (checkout / "docs" / "cli.md").read_bytes()
    assert raw.decode("utf-8") == manual.render_markdown() and b"\r\n" not in raw
    assert git(checkout, "config", "--get", "core.hooksPath").strip() == ".githooks"
    assert sync.main({"--check": True}) == 0  # idempotent


def test_another_hooks_setup_is_left_alone(checkout, user_home):
    git(checkout, "config", "core.hooksPath", ".husky")
    stale, detail = by_label(sync.items(apply=True))["pre-commit hook"]
    assert not stale and ".husky" in detail
    git(checkout, "config", "--unset", "core.hooksPath")
    (checkout / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    stale, detail = by_label(sync.items(apply=True))["pre-commit hook"]
    assert not stale and "your own hooks" in detail
    assert subprocess.run(["git", "-C", str(checkout), "config", "--get", "core.hooksPath"]).returncode == 1


def test_outside_a_checkout_only_the_user_side_runs(user_home, monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "REPO", tmp_path / "site-packages")
    assert set(by_label(sync.items(apply=False))) == {"tab completion", "worker agents"}


def test_completion_scripts_are_refreshed_profiles_untouched(user_home):
    path = C.script_path("bash")
    path.parent.mkdir(parents=True)
    path.write_text("# an old script without the newest commands\n", encoding="utf-8")
    profile = user_home / ".bashrc"
    assert C.stale() == ["bash"]
    stale, detail = by_label(sync.items(apply=True, repo=False))["tab completion"]
    assert stale and "rewritten: bash" in detail
    assert path.read_text(encoding="utf-8") == C.script("bash") and " sync" in path.read_text(encoding="utf-8")
    assert not profile.exists() and C.stale() == []
    assert not by_label(sync.items(apply=True, repo=False))["tab completion"][0]


def test_agents_are_kept_current_only_where_generated(user_home):
    before = hub.APPLY, hub.PROVIDERS
    assert sync.agent_providers() == ()
    assert "none generated yet" in by_label(sync.items(apply=True, repo=False))["worker agents"][1]
    folder = hub.CLAUDE_AGENTS
    folder.mkdir(parents=True)
    (folder / "mine.md").write_text("hand-written\n", encoding="utf-8")
    assert sync.agent_providers() == ()  # a hand-written agent is not ours
    (folder / "opus-worker-high.md").write_text(f"---\n# {hub.GEN_MARK}\n---\nold body\n", encoding="utf-8")
    assert sync.agent_providers() == ("claude",)
    stale, detail = by_label(sync.items(apply=True, repo=False))["worker agents"]
    assert stale and "written" in detail
    assert "old body" not in (folder / "opus-worker-high.md").read_text(encoding="utf-8")
    assert (folder / "mine.md").read_text(encoding="utf-8") == "hand-written\n"
    assert not (user_home / "codex_agents").exists()  # no agents created for another tool
    assert not by_label(sync.items(apply=False, repo=False))["worker agents"][0]
    assert (hub.APPLY, hub.PROVIDERS) == before  # globals restored


def test_a_failing_item_is_reported_not_raised(user_home, monkeypatch):
    monkeypatch.setattr(sync, "_agents", lambda apply: 1 / 0)
    lines = sync.items(apply=True, repo=False)
    assert any(d.startswith("failed: ZeroDivisionError") for _, _, d in lines)


# ---- the pre-commit hook ------------------------------------------------------------------------------------

def test_pre_commit_stages_the_regenerated_reference(checkout, user_home):
    (checkout / "trirouter" / "core.py").write_text("x = 1\n", encoding="utf-8")
    git(checkout, "add", "trirouter/core.py")
    said = []
    assert sync.pre_commit(said.append, run_tests=False) == 0
    assert "docs/cli.md" in git(checkout, "diff", "--cached", "--name-only")
    text = "\n".join(said)
    assert "git add docs/cli.md" in text and "CHANGELOG.md is not" in text


def test_pre_commit_refuses_a_reference_for_unstaged_manual_edits(checkout, user_home):
    (checkout / "trirouter" / "manual.py").write_text("# changed, not staged\n", encoding="utf-8")
    said = []
    assert sync.pre_commit(said.append, run_tests=False) == 1
    assert "unstaged changes" in "\n".join(said)
    assert "docs/cli.md" not in git(checkout, "diff", "--cached", "--name-only")


def test_pre_commit_runs_the_tests_and_the_evaluation_only_for_routing_code(checkout, user_home, monkeypatch):
    ran = []
    monkeypatch.setattr(sync, "_run", lambda label, argv, say: ran.append(label) or True)
    (checkout / "CHANGELOG.md").write_text("x\n", encoding="utf-8")
    git(checkout, "add", "CHANGELOG.md")
    assert sync.pre_commit(lambda m: None) == 0 and ran == ["Tests"]
    (checkout / "trirouter" / "config").mkdir()
    (checkout / "trirouter" / "config" / "routes.json").write_text("{}", encoding="utf-8")
    git(checkout, "add", "trirouter/config/routes.json")
    ran.clear()
    assert sync.pre_commit(lambda m: None) == 0 and ran == ["Tests", "Routing evaluation"]
    monkeypatch.setattr(sync, "_run", lambda label, argv, say: label != "Tests")
    assert sync.pre_commit(lambda m: None) == 1


def test_the_hook_script_is_in_the_repository():
    hook = ROOT / ".githooks" / "pre-commit"
    text = hook.read_bytes().decode("utf-8")
    assert text.startswith("#!/bin/sh\n") and "\r" not in text
    assert "-m trirouter sync --pre-commit" in text and "--no-verify" in text
    if HAS_GIT:
        mode = git(ROOT, "ls-files", "-s", ".githooks/pre-commit").split()[:1]
        assert mode in ([], ["100755"])  # executable once committed


def test_cli_dispatches_sync(monkeypatch):
    got = {}
    monkeypatch.setattr(sync, "main", lambda flags, say: got.update(flags) or 0)
    assert cli.main(["sync", "--check"]) == 0 and got == {"--check": True}
    assert cliparse.resolve(["sync", "--pre-commit"], "trirouter", False).flags == {"--pre-commit": True}


def test_the_daily_check_refreshes_the_user_side(monkeypatch, tmp_path):
    from trirouter import core, discovery
    calls = []
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    monkeypatch.setattr(discovery, "wait_lock", lambda: True)
    monkeypatch.setattr(discovery, "release_lock", lambda: None)
    monkeypatch.setattr(discovery, "run", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "learn_routing", lambda: None)
    monkeypatch.setattr(sync, "user_side", lambda: calls.append(1))
    monkeypatch.chdir(tmp_path)
    discovery.run_scheduled()
    assert calls == [1]
