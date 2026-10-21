"""`trirouter sync`: brings everything that is generated from the sources up to date in one step, so a new command,
flag or model works everywhere right after a development round.

  docs/cli.md          the CLI reference, from manual.py                          (checkout only)
  git pre-commit hook  core.hooksPath = .githooks, which runs `sync --pre-commit` (checkout only)
  tab completion       the installed shell scripts, from manual.py (profiles are left alone)
  worker agents        the generated Claude / Codex / Antigravity agents, from templates + targets.json + models.json
                       (only for tools that already have generated agents: `skills --apply` creates them)
  skill catalog        ~/.skills/catalog.json, the skill list the router reads, from the skills on this machine
                       (only once the hub folder exists: `setup` creates it)

`--check` changes nothing and exits 1 when something is out of date (CI). `--pre-commit` is what the git hook runs:
the sync, then `git add docs/cli.md` when it was regenerated, a CHANGELOG reminder, the tests and, when routing code
is staged, the routing evaluation; a failure stops the commit (`git commit --no-verify` skips the hook once).
The daily background check (discovery.py) runs the user-side part (completion, agents, catalog) by itself.
"""
import contextlib
import io
import subprocess
import sys
from pathlib import Path

from . import catalog, completion, hub, manual, platforms as P

PKG = Path(__file__).resolve().parent
REPO = PKG.parent
DOCS_CLI = REPO / "docs" / "cli.md"
HOOKS_DIR = ".githooks"
LOCAL_SETTINGS = ("pre-commit hook",)  # reported by --check and doctor, but never a reason for --check to fail
# staged paths that change routing decisions: the pre-commit hook then also runs eval/eval_router.py
ROUTING_PATHS = ("trirouter/core.py", "trirouter/lang.py", "trirouter/distill.py", "trirouter/config/", "eval/")
# staged paths that are user-visible: the hook reminds to record them in CHANGELOG.md
VISIBLE_PATHS = ("trirouter/", "install.py", "docs/", "README.md")


def is_checkout():
    return (REPO / ".git").exists() and (REPO / "docs").is_dir()


def git(*args, timeout=30):
    return P.run(["git", "-C", str(REPO), *args], timeout=timeout)


# ---- the items: each returns [(label, stale, detail)] and fixes what is stale when apply is set ---------------

def _docs(apply):
    want = manual.render_markdown()
    try:
        have = DOCS_CLI.read_bytes().decode("utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError):
        have = None
    if have == want:
        return [("docs/cli.md", False, "matches manual.py")]
    if apply:
        P.atomic_write(DOCS_CLI, want.encode("utf-8"))  # bytes: LF on every platform
    return [("docs/cli.md", True, "regenerated from manual.py" if apply else "out of date with manual.py")]


def _foreign_git_hooks():
    folder = REPO / ".git" / "hooks"
    return sorted(f.name for f in folder.iterdir() if f.is_file() and not f.name.endswith(".sample")) if folder.is_dir() else []


def _git_hook(apply):
    code, out = git("config", "--get", "core.hooksPath")
    if code is None:
        return [("pre-commit hook", False, "git not found")]
    current = out.strip() if code == 0 else ""
    if current == HOOKS_DIR:
        return [("pre-commit hook", False, f"core.hooksPath = {HOOKS_DIR}")]
    if current:
        return [("pre-commit hook", False, f"core.hooksPath is {current!r}, left alone: run "
                                           f"`python -m trirouter sync --pre-commit` from your own hook")]
    if foreign := _foreign_git_hooks():
        return [("pre-commit hook", False, f".git/hooks has your own hooks ({', '.join(foreign)}): left alone")]
    if apply:
        git("config", "core.hooksPath", HOOKS_DIR)
    return [("pre-commit hook", True, f"core.hooksPath set to {HOOKS_DIR}" if apply else f"core.hooksPath not set to {HOOKS_DIR}")]


def _completion(apply):
    shells = completion.installed()
    if not shells:
        return [("tab completion", False, f"not installed (`{P.command_hint('setup')}` installs it)")]
    old = completion.refresh(apply=apply, say=lambda _m: None)
    detail = ", ".join(shells)
    return [("tab completion", bool(old), (f"rewritten: {', '.join(old)}" if apply else f"out of date: {', '.join(old)}")
             if old else f"up to date ({detail})")]


def _generated(folder, pattern):
    try:
        return folder.is_dir() and any(any(m in f.read_text(encoding="utf-8", errors="replace") for m in hub.GEN_MARKS)
                                       for f in folder.glob(pattern))
    except OSError:
        return False


def agent_providers():
    """Tools whose generated worker agents exist: sync keeps them current, it never creates them for a new tool."""
    found = {"claude": _generated(hub.CLAUDE_AGENTS, "*.md"), "codex": _generated(hub.CODEX_AGENTS, "*.toml"),
             "antigravity": _generated(hub.AGY_AGENTS, "*/agent.md")}
    return tuple(p for p, ok in found.items() if ok)


def _agents(apply):
    providers = agent_providers()
    if not providers:
        return [("worker agents", False, f"none generated yet (`{P.command_hint('skills --apply')}` creates them)")]
    saved = hub.PROVIDERS, hub.APPLY
    hub.PROVIDERS = providers
    try:
        hub.APPLY = False
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            hub.cmd_agents()
        changes = [ln for ln in buf.getvalue().splitlines() if ln.startswith("[DRY]")]
        if changes and apply:
            hub.APPLY = True
            with contextlib.redirect_stdout(io.StringIO()):
                hub.cmd_agents()
    finally:
        hub.PROVIDERS, hub.APPLY = saved
    if not changes:
        return [("worker agents", False, f"up to date ({', '.join(providers)})")]
    return [("worker agents", True, f"{len(changes)} change(s) {'written' if apply else 'pending'} ({', '.join(providers)})")]


def _skill_catalog(apply):
    if not catalog.HUB.is_dir():
        return [("skill catalog", False, f"no skill hub yet (`{P.command_hint('setup')}` creates it)")]
    have, want = catalog.load_catalog(), catalog.build_catalog(hub.catalog_only_setting())
    if have == want:
        return [("skill catalog", False, f"up to date ({len(want)} skills)")]
    if apply:
        catalog.write_catalog(want)
    return [("skill catalog", True, f"rewritten ({len(want)} skills)" if apply else
             f"out of date ({len(have)} listed, {len(want)} found)")]


def items(apply, user=True, repo=None):
    """Runs every item; [(label, stale, detail)]. A failing item is reported, never raised."""
    repo = is_checkout() if repo is None else repo
    steps = ([("docs/cli.md", _docs), ("pre-commit hook", _git_hook)] if repo else []) + \
            ([("tab completion", _completion), ("worker agents", _agents), ("skill catalog", _skill_catalog)] if user else [])
    out = []
    for label, step in steps:
        try:
            out += step(apply)
        except Exception as exc:  # noqa: BLE001 - one broken item must not stop the others
            out.append((label, True, f"failed: {type(exc).__name__}: {exc}"[:200]))
    return out


def user_side():
    """The daily background check: completion scripts, agents and the skill catalog, silently. Never raises."""
    try:
        items(apply=True, user=True, repo=False)
    except Exception:  # noqa: BLE001
        pass


def show(lines, apply, say):
    for label, stale, detail in lines:
        mark = "[OK]  " if not stale else ("[DO]  " if apply else "[!!]  ")
        if detail.startswith("failed"):
            mark = "[!!]  "
        say(f"  {mark}{label}: {detail}")


# ---- the git pre-commit hook --------------------------------------------------------------------------------

def staged():
    code, out = git("diff", "--cached", "--name-only")
    return [ln.strip() for ln in out.splitlines() if ln.strip()] if code == 0 else []


def _docs_unstaged():
    return git("diff", "--quiet", "--", "docs/cli.md")[0] == 1


def _stage_docs(say, files):
    """docs/cli.md differs from the index after the sync: stage it when it describes exactly the manual.py of this
    commit. With unstaged manual.py edits it describes code outside the commit (perhaps another session's work in
    progress): then it stays unstaged, and only a commit that stages part of manual.py is stopped."""
    if not _docs_unstaged():
        return True
    if git("diff", "--quiet", "--", "trirouter/manual.py")[0] != 1:
        git("add", "--", "docs/cli.md")
        say("  [DO]  git add docs/cli.md (regenerated from manual.py)")
        return True
    if "trirouter/manual.py" in files:
        say("  [!!]  trirouter/manual.py is only partly staged, so docs/cli.md cannot match this commit: stage all of "
            "manual.py (or none of it) and commit again")
        return False
    say("  [OK]  docs/cli.md left unstaged: it follows unstaged manual.py edits that are not part of this commit")
    return True


def _run(label, argv, say):
    say(f"\n{label}: {' '.join(argv[1:])}")
    sys.stdout.flush()
    try:
        return subprocess.call(argv, cwd=str(REPO)) == 0
    except OSError as exc:
        say(f"  [!!]  {label} could not start: {exc}")
        return False


def pre_commit(say=print, run_tests=True):
    """0 when the commit may go ahead."""
    say("trirouter pre-commit: sync, tests, evaluation (skip once: git commit --no-verify)")
    show(items(apply=True), True, say)
    files = staged()
    ok = _stage_docs(say, files)
    if any(f.startswith(VISIBLE_PATHS) for f in files) and "CHANGELOG.md" not in files:
        say("  [!!]  user-visible files are staged but CHANGELOG.md is not: record the change under [Unreleased] "
            "(not blocking)")
    if not ok:
        return 1
    if not run_tests:
        return 0
    if not _run("Tests", [sys.executable, "-m", "pytest", "tests", "-q"], say):
        say("\nCommit stopped: the tests failed.")
        return 1
    if any(f.startswith(ROUTING_PATHS) for f in files):
        if not _run("Routing evaluation", [sys.executable, "eval/eval_router.py"], say):
            say("\nCommit stopped: the routing evaluation failed.")
            return 1
    else:
        say("\nRouting evaluation: skipped (no routing code staged)")
    return 0


def main(flags, say=print):
    if "--pre-commit" in flags:
        if not is_checkout():
            say("--pre-commit only works in a trirouter checkout")
            return 1
        return pre_commit(say)
    check = "--check" in flags or "--dry-run" in flags
    say("trirouter sync" + (" (check - nothing is changed)" if check else ""))
    lines = items(apply=not check)
    show(lines, not check, say)
    failed = any(d.startswith("failed") for _, _, d in lines)
    # the hook folder is a setting of this clone, not a generated file: a fresh clone (CI) is not out of date
    if "--check" in flags and any(s for label, s, _ in lines if label not in LOCAL_SETTINGS):
        say(f"\nOut of date: run `{P.command_hint('sync')}`")
        return 1
    return 1 if failed else 0
