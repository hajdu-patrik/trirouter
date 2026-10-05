#!/usr/bin/env python3
"""Names the project had before it was renamed to trirouter, and the migration of an existing install.

Before the rename the project was called jev-router (package `jev_router`, state folder
`~/.jev-router`, MCP server `jev-router`, scheduled tasks `JevRouter-*`). This is the one place that
knows those names; everything else asks this module. Remove it, the `jev_router/` compatibility
package and the legacy branches that use it after the next release.

Only `setup`, `skills --apply`, `uninstall` and the other commands that write state move the state
folder (cli.prepare_state). A hook or the MCP server never does: they only READ the old folder when
the new one does not exist yet, so an install that has not been migrated keeps routing.
"""
import json
import os
import shutil
from pathlib import Path

NAME = "jev-router"                 # the old product name
STATE_NAME = ".jev-router"          # ~/.jev-router
PACKAGE = "jev_router"              # old Python package (kept as a thin compatibility package)
MCP_NAME = "jev-router"             # MCP server name in the tools' configs
ENV_HOME = "JEV_ROUTER_HOME"        # old state-folder override
TASK_CLAUDE, TASK_CODEX, TASK_WATCHDOG, TASK_ONCE = ("JevRouter-ClaudeRemote", "JevRouter-CodexRemote",
                                                     "JevRouter-Watchdog", "JevRouter-Setup")
TASK_PATTERN = "JevRouter-*"
LAUNCHD_LABEL = "com.jev-router.claude-remote"
SYSTEMD_UNIT = "jev-router-claude-remote.service"
GEN_MARK = "generated-by: jev-router"             # in generated agent files
TOML_END = "# <<< jev-router agents"

NEW_STATE_NAME = ".trirouter"
ENV_HOME_NEW = "TRIROUTER_HOME"
KEEP_ON_MERGE_SKIP = ("bin",)  # generated shims: never carried over, setup regenerates them


def state_dir(home=None, environ=None):
    """Where the router's state lives: ~/.trirouter, or ~/.jev-router while an old install has not been
    migrated yet. The folder that holds config.json wins when both exist (a half-created new folder must
    not hide the JEV token)."""
    environ = os.environ if environ is None else environ
    override = environ.get(ENV_HOME_NEW) or environ.get(ENV_HOME)
    if override:
        return Path(override)
    home = Path(home) if home else Path.home()
    new, old = home / NEW_STATE_NAME, home / STATE_NAME
    if old.is_dir() and (not new.is_dir() or ((old / "config.json").is_file() and not (new / "config.json").is_file())):
        return old
    return new


def old_state(home):
    return Path(home) / STATE_NAME


def _files(root):
    """Relative paths of every file under root, skipping the generated `bin` folder."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if rel == Path("."):
            dirnames[:] = [d for d in dirnames if d not in KEEP_ON_MERGE_SKIP]
        out += [rel / f for f in filenames]
    return out


def _merge_json_config(src, dst):
    """config.json in both folders: keys only the old one has are added; the new folder's values win."""
    try:
        old = json.loads(src.read_text(encoding="utf-8"))
        new = json.loads(dst.read_text(encoding="utf-8"))
        if not isinstance(old, dict) or not isinstance(new, dict):
            return False
        merged = {**old, **new}
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(merged, indent=2))
        if os.name != "nt":
            os.chmod(dst, 0o600)
        return True
    except (OSError, ValueError):
        return False


def _move_file(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:
        shutil.copy2(src, dst)  # keeps the mode bits (config.json stays owner-only)
        src.unlink()


def _remove_empty_dirs(root):
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        try:
            os.rmdir(dirpath)  # only succeeds when empty
        except OSError:
            pass


def plan_merge(old, new):
    """[(relative path, action)] with action in "move" (absent in the new folder), "replace" (the old copy is
    newer), "config" (config.json in both) or "keep" (the new copy is as new or newer: left alone)."""
    plan = []
    for rel in _files(old):
        src, dst = old / rel, new / rel
        if not dst.exists():
            plan.append((rel, "move"))
        elif rel == Path("config.json"):
            plan.append((rel, "config"))
        elif src.stat().st_mtime > dst.stat().st_mtime:
            plan.append((rel, "replace"))
        else:
            plan.append((rel, "keep"))
    return plan


def migrate_state(home=None, apply=False, say=print):
    """Moves ~/.jev-router to ~/.trirouter. Returns "none" (nothing to do), "moved", "merged", "failed", or,
    for a dry run, "would-move" / "would-merge".

    - the new folder does not exist: the whole old folder is renamed (permissions, logs, state, quarantine with
      its sidecars, models.local.json, tmp all come along; the generated `bin` shims are regenerated by setup);
    - both exist: files missing in the new folder are moved over; a file is replaced only when the old copy is
      newer; config.json keys are merged (the new value wins); what stays behind is reported.
    """
    home = Path(home) if home else Path.home()
    old, new = old_state(home), home / NEW_STATE_NAME
    if not old.is_dir() or old.is_symlink():
        return "none"
    if not new.exists():
        say(("[DO]  " if apply else "[DRY] ") + f"state folder: move {old} -> {new}")
        if not apply:
            return "would-move"
        try:
            os.rename(old, new)
        except OSError as exc:
            say(f"[FAIL] state folder: could not move {old} ({exc}). Close programs that use it "
                f"(a terminal in that folder, a remote-access task) and re-run.")
            return "failed"
        return "moved"
    plan = plan_merge(old, new)
    counts = {a: sum(1 for _, x in plan if x == a) for a in ("move", "replace", "config", "keep")}
    say(("[DO]  " if apply else "[DRY] ") + f"state folder: merge {old} into {new}: {counts['move']} moved, "
        f"{counts['replace']} replaced (older there), {counts['config']} config merged, {counts['keep']} left "
        f"(newer or equal in {new.name})")
    if not apply:
        return "would-merge"
    for rel, action in plan:
        src, dst = old / rel, new / rel
        try:
            if action in ("move", "replace"):
                _move_file(src, dst)
            elif action == "config" and _merge_json_config(src, dst):
                src.unlink()
        except OSError as exc:
            say(f"[!!]   state folder: {rel} not merged ({exc})")
    _remove_empty_dirs(old)
    if old.exists():
        left = sum(len(names) for _d, _n, names in os.walk(old))
        say(f"[!!]  {left} file(s) stay in {old} (the copies in {new.name} are newer, or generated shims); "
            f"delete that folder when you no longer need them.")
    return "merged"
