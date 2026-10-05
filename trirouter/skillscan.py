"""SkillSpector gate for the shared skill folder (https://github.com/NVIDIA/SkillSpector).

Every hub skill is scanned before it is linked into the tools. When all verdicts are known, the
DO_NOT_INSTALL skills nobody has decided on yet are put to the user in ONE question (all / none /
select, default none): the chosen ones move to ~/.trirouter/quarantine/, where no tool (Antigravity
reads the whole hub) and no catalog sees them; the others are kept, remembered for that exact content.
Unattended runs (no terminal, --yes) only warn: static analysis also flags legitimate skills that run
scripts. `skillscan.allow` in config.json (or --allow-skill) silences the verdict and restores a
quarantined skill. CAUTION only warns.

Quarantine entries are `<name>` folders with a sidecar `<name>.json` (name, quarantined_at, purge_after,
risk, max_severity; an entry without one gets it from its folder's mtime). They are deleted for good
after `skillscan.quarantine_days` days (default 3, 0 = never) by `purge_expired`, which runs at every
setup / `skills --apply` and, at most every 6 hours, from the Claude SessionStart hook. The retention
now in force decides, not the purge_after written earlier. Only folders directly inside the quarantine
folder are ever deleted, and links are never followed.

Static analysis by default (`--no-llm`: nothing leaves the machine); `skillscan.llm: true` lets
SkillSpector's own provider settings (SKILLSPECTOR_PROVIDER, ...) add its LLM analysis.
SkillSpector is optional (Python 3.12+, installed as its own tool): without it skills are linked
unscanned with a warning (also when it does not start), and so is a skill whose scan fails.
"""
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import legacy, platforms as P

STATE = legacy.state_dir(P.HOME)
QUARANTINE = STATE / "quarantine"
CACHE = STATE / "state" / "skillscan.json"
CONFIG = STATE / "config.json"
DEFAULT_DAYS = 3  # quarantine retention; 0 = never purge
EXE = "skillspector"
INSTALL_HINT = "uv tool install git+https://github.com/NVIDIA/skillspector.git"
SAFE, CAUTION, BLOCK = "SAFE", "CAUTION", "DO_NOT_INSTALL"
TIMEOUT_S = {False: 600, True: 1800}  # static / with LLM analysis; large skills take minutes
WORKERS = max(1, min(8, (os.cpu_count() or 2) // 2))  # parallel scans; each one is a CPU-bound process
_SKIP_PARTS = {".git", "__pycache__", "node_modules"}


def find_scanner():
    return P.find_exe(EXE)


UV_TOOL_HINT = ("SkillSpector's uv tool folder is probably not visible to this Python (a packaged Python on "
                "Windows virtualizes %APPDATA%). Reinstall it outside AppData: set UV_TOOL_DIR to e.g. "
                r"%USERPROFILE%\.local\share\uv\tools and run "
                "`uv tool install --force git+https://github.com/NVIDIA/skillspector.git` "
                "(`uv tool upgrade` then needs the same UV_TOOL_DIR).")


class StartError(str):
    """scanner_version() result when the scanner does not start: the first line of its output."""


def scanner_version(exe):
    """The version; "unknown" when it starts but prints nothing usable; a StartError when it does not start."""
    code, out = P.run([exe, "--version"], timeout=30)
    out = (out or "").strip()
    if code != 0:
        return StartError(out.splitlines()[0][:200] if out else f"exit {code}")
    return out.split()[-1] if out else "unknown"


def fingerprint(skill_dir):
    """Content hash of the whole skill folder: any edit or added script triggers a new scan."""
    h = hashlib.sha256()
    root = Path(skill_dir)
    for f in sorted(p for p in root.rglob("*") if p.is_file() and not _SKIP_PARTS & set(p.relative_to(root).parts)):
        h.update(f.relative_to(root).as_posix().encode("utf-8") + b"\0")
        try:
            h.update(f.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


def scan_command(exe, skill_dir, report, llm):
    return [exe, "scan", str(skill_dir), "--format", "json", "--output", str(report)] + ([] if llm else ["--no-llm"])


def run_scan(exe, skill_dir, llm):
    """{recommendation, score, max_severity, llm_available}, or {error}. The JSON decides, not the exit code."""
    fd, report = tempfile.mkstemp(prefix="skillscan-", suffix=".json")
    os.close(fd)
    try:
        code, out = P.run(scan_command(exe, skill_dir, report, llm), timeout=TIMEOUT_S[bool(llm)])
        try:
            data = json.loads(Path(report).read_text(encoding="utf-8"))
            risk = data["risk_assessment"]
            return {"recommendation": risk["recommendation"], "score": risk.get("score"),
                    "max_severity": risk.get("max_issue_severity"),
                    "llm_available": bool((data.get("metadata") or {}).get("llm_available"))}
        except (OSError, ValueError, KeyError, TypeError):
            last = out.strip().splitlines()[-1:] if out else []
            return {"error": f"exit {code}: {last[0][:200] if last else 'no report'}"}
    finally:
        Path(report).unlink(missing_ok=True)


def load_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")


def _scan(exe, skill_dir, llm):
    """(result, cacheable): a static fallback for a missing LLM provider is not cached."""
    result = run_scan(exe, skill_dir, llm)
    if llm and "error" not in result and not result.get("llm_available"):
        print(f"[WARN] skillscan: {Path(skill_dir).name}: LLM analysis unavailable (check SKILLSPECTOR_PROVIDER "
              "and its key) - static verdict used, not cached", flush=True)
        return run_scan(exe, skill_dir, False), False
    return result, True


def _progress(done, total):
    if P.is_terminal(sys.stdout):
        print(f"\r  scanned {done}/{total}", end="\n" if done == total else "", flush=True)


def verdicts(skills, exe, version, llm, cache):
    """{name: result}, cached per content hash, scanner version and scan mode, failed scans too (no retry
    until a change). Each scan is its own process, so new or changed skills are scanned WORKERS at a time."""
    out, todo = {}, []
    for s in skills:
        key = f"{fingerprint(s)}:{version}:{'llm' if llm else 'static'}"
        hit = cache.get(s.name)
        if hit and hit.get("key") == key:
            out[s.name] = dict(hit["result"], accepted=hit.get("accepted", False))
        else:
            todo.append((s, key))
    if not todo:
        return out
    workers = max(1, min(WORKERS, len(todo)))
    print(f"skillscan: scanning {len(todo)} new or changed skill(s), {workers} at a time", flush=True)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_scan, exe, s, llm): (s, key) for s, key in todo}
        for done, future in enumerate(as_completed(futures), 1):
            (s, key), (result, cacheable) = futures[future], future.result()
            out[s.name] = result
            if cacheable:
                cache[s.name] = {"key": key, "result": result}
            _progress(done, len(todo))
    print(f"skillscan: scanned in {time.monotonic() - started:.0f} s")
    return out


_TS = "%Y-%m-%dT%H:%M:%SZ"


def utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime(_TS)


def _parse_iso(text):
    try:
        return datetime.strptime(text, _TS).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def configured_days(default=DEFAULT_DAYS):
    """`skillscan.quarantine_days` from config.json; never raises."""
    try:
        days = json.loads(CONFIG.read_text(encoding="utf-8"))["skillscan"]["quarantine_days"]
        return days if isinstance(days, int) and not isinstance(days, bool) and days >= 0 else default
    except (OSError, ValueError, KeyError, TypeError):
        return default


def _quarantine_dest(name):
    dest = QUARANTINE / name
    return dest if not dest.exists() else QUARANTINE / f"{name}@{time.strftime('%Y%m%d-%H%M%S')}"


def sidecar(entry):
    return QUARANTINE / f"{Path(entry).name}.json"


def write_sidecar(entry, name, risk, max_severity, quarantined_at, days):
    meta = {"name": name, "quarantined_at": _iso(quarantined_at),
            "purge_after": _iso(quarantined_at + timedelta(days=days)) if days else None,
            "risk": risk, "max_severity": max_severity}
    sidecar(entry).write_text(json.dumps(meta, indent=1), encoding="utf-8")


def _move_to_quarantine(skill, dest, risk, max_severity, days):
    QUARANTINE.mkdir(parents=True, exist_ok=True)
    shutil.move(str(skill), str(dest))
    write_sidecar(dest, Path(skill).name, risk, max_severity, utcnow(), days)


def entries(apply=False, days=None):
    """Quarantine entries, oldest first: [{entry, name, quarantined_at, purge_at, risk, max_severity}].
    purge_at is quarantined_at + the retention now in force (None: never). An entry without a sidecar takes
    its folder's mtime as quarantined_at; the sidecar is written then only when `apply`."""
    days = configured_days() if days is None else days
    out = []
    if not QUARANTINE.is_dir():
        return out
    for e in sorted(QUARANTINE.iterdir()):
        if not e.is_dir():
            continue
        meta = {}
        try:
            meta = json.loads(sidecar(e).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        meta = meta if isinstance(meta, dict) else {}
        when = _parse_iso(meta.get("quarantined_at"))
        if when is None:
            try:
                when = datetime.fromtimestamp(e.lstat().st_mtime, timezone.utc).replace(microsecond=0)
            except OSError:
                continue
            if apply and not P.is_link(e):
                try:
                    write_sidecar(e, e.name.split("@")[0], None, None, when, days)
                except OSError:
                    pass
        out.append({"entry": e, "name": meta.get("name") or e.name.split("@")[0], "quarantined_at": when,
                    "purge_at": when + timedelta(days=days) if days else None,
                    "risk": meta.get("risk"), "max_severity": meta.get("max_severity")})
    return sorted(out, key=lambda r: r["quarantined_at"])


def inside_quarantine(path):
    """True only for a real entry directly inside the quarantine folder: no link, no escape."""
    try:
        p = Path(path)
        if P.is_link(p) or not p.exists():
            return False
        root = QUARANTINE.resolve(strict=True)
        return p.resolve(strict=True).parent == root and p.parent.resolve(strict=True) == root
    except (OSError, RuntimeError):
        return False


def _clear_readonly(func, path, *_):
    os.chmod(path, stat.S_IRWXU)
    func(path)


def _unlink_inner_links(root):
    """Links inside a skill (a junction, a symlink) are removed as links, so nothing ever follows them out."""
    for dirpath, dirnames, filenames in os.walk(root):
        for d in list(dirnames):
            full = Path(dirpath, d)
            if P.is_link(full):
                dirnames.remove(d)
                try:
                    P.unlink_dir(full)
                except OSError:
                    full.unlink()
        for f in filenames:
            full = Path(dirpath, f)
            if full.is_symlink():
                full.unlink()


def _delete_entry(entry):
    if not inside_quarantine(entry):
        raise OSError(f"refusing to delete {entry}: not a plain entry of the quarantine folder")
    entry = Path(entry)
    _unlink_inner_links(entry)
    if sys.version_info >= (3, 12):
        shutil.rmtree(entry, onexc=_clear_readonly)
    else:
        shutil.rmtree(entry, onerror=_clear_readonly)
    side = sidecar(entry)
    if side.is_file() and not side.is_symlink():
        side.unlink()


def purge_expired(apply, now=None, days=None, say=print, everything=False):
    """Deletes quarantine entries older than the retention (all of them with `everything`); returns the
    names purged (or that a dry run would purge). `say=None` is silent (the hook). Never raises."""
    now = now or utcnow()
    purged = []
    try:
        days = configured_days() if days is None else days
        if not days and not everything:
            return purged
        for r in entries(apply, days):
            if not everything and not (r["purge_at"] and r["purge_at"] <= now):
                continue
            try:
                if apply:
                    _delete_entry(r["entry"])
                if say:
                    say(("[DO]  " if apply else "[DRY] ") + f"skillscan: purge {r['name']} "
                        f"(quarantined {r['quarantined_at']:%Y-%m-%d})")
                purged.append(r["name"])
            except OSError as exc:
                if say:
                    say(f"[WARN] skillscan: could not purge {r['name']}: {exc}")
        if apply and QUARANTINE.is_dir():  # metadata whose folder is gone
            for f in QUARANTINE.glob("*.json"):
                if f.is_file() and not f.is_symlink() and not (QUARANTINE / f.stem).exists():
                    f.unlink()
    except Exception as exc:  # noqa: BLE001 - housekeeping must never break its caller
        if say:
            say(f"[WARN] skillscan: purge failed: {type(exc).__name__}")
    return purged


def human_left(delta):
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "expired"
    days, hours = secs // 86400, secs % 86400 // 3600
    if days:
        return f"in {days} day{'s' if days != 1 else ''} {hours} h"
    return f"in {hours} h" if hours else "in under 1 h"


def find_copies(name):
    """Quarantine entries for a skill name (or one exact entry name), newest last."""
    return [r for r in entries() if name in (r["name"], r["entry"].name)]


def restore(names, hub, act):
    """Moves the newest quarantined copy of each name back to hub/<name> (unless that name is taken).
    Returns (restored names, problems)."""
    restored, problems = [], []
    for name in names:
        copies = find_copies(name)
        if not copies:
            problems.append(f"{name}: not in quarantine")
            continue
        src, skill = copies[-1]["entry"], copies[-1]["name"]
        dest = Path(hub) / skill
        if dest.exists():
            problems.append(f"{name}: {dest} already exists - remove or rename it first")
            continue
        act(f"skillscan: restore {src} -> {dest}", lambda s=src, d=dest: (
            Path(hub).mkdir(parents=True, exist_ok=True), shutil.move(str(s), str(d)),
            sidecar(s).unlink(missing_ok=True)))
        restored.append(skill)
    return restored, problems


def parse_selection(text, count):
    """'1,3,5-8' -> {0, 2, 4, 5, 6, 7} (zero-based); ValueError with a user-facing message otherwise."""
    picked = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        lo, dash, hi = part.partition("-")
        if not lo.isdigit() or (dash and not hi.isdigit()):
            raise ValueError(f"'{part}' is not a number or a range like 5-8")
        a, b = int(lo), int(hi) if dash else int(lo)
        if a > b:
            raise ValueError(f"'{part}' is a backwards range")
        if a < 1 or b > count:
            raise ValueError(f"'{part}' is out of range: choose from 1 to {count}")
        picked.update(range(a - 1, b))
    return picked


def restore_allowed(hub, allow, act):
    """An allowed skill comes back from quarantine (the newest copy) unless the hub has that name again."""
    for name in sorted(allow):
        if find_copies(name):
            restore([name], hub, act)


def _resolve_flagged(flagged, act, choose, cache, days):
    """The one decision for the undecided DO_NOT_INSTALL skills: [outcome, ...] in order, each
    "quarantined", "kept" or "unasked" (choose() is None: nobody to ask)."""
    items = [{"name": s.name, "score": r.get("score"), "max_severity": r.get("max_severity"), "path": str(s)}
             for s, r in flagged]
    chosen = choose(items)
    out = []
    for s, r in flagged:
        if chosen is None:
            out.append("unasked")
        elif s.name in chosen:
            dest = _quarantine_dest(s.name)
            act(f"skillscan: {s.name}: {BLOCK} (risk {r.get('score')}, max {r.get('max_severity')}) "
                f"- quarantine -> {dest}",
                lambda s=s, r=r, dest=dest: _move_to_quarantine(s, dest, r.get("score"), r.get("max_severity"), days))
            out.append("quarantined")
        else:
            cache.get(s.name, {})["accepted"] = True
            out.append("kept")
    return out


# one summary line per outcome instead of a line per skill: a large hub has dozens of CAUTION verdicts
_SUMMARY = (("unasked", f"{BLOCK}, linked - decide in an interactive `{P.command_hint('skills --apply')}`, "
                        "or keep all of them after a review with --accept-flagged"),
            ("kept", f"{BLOCK}, linked - kept by you (asked again only if it changes)"),
            ("accepted", f"{BLOCK}, linked - accepted with --accept-flagged (asked again only if it changes)"),
            ("kept earlier", f"{BLOCK}, linked - kept by you earlier"),
            ("allowed", f"{BLOCK}, linked - allowed in config"),
            (CAUTION, f"{CAUTION}, linked"),
            ("failed", "scan failed, linked unscanned until they change"))


def gate(skills, act, apply, llm=False, allow=(), choose=lambda items: None, accept_flagged=False,
         days=None):
    """Scan the given hub skills; returns the names that must not be linked. `choose(items)` is asked once,
    after all verdicts, which flagged skills to quarantine: a set of names, or None when nobody can be asked."""
    allow = set(allow)
    if not skills:
        return set()
    exe = find_scanner()
    if not exe:
        print(f"[WARN] skillscan: SkillSpector not found - {len(skills)} skill(s) linked unscanned. Install: {INSTALL_HINT}")
        return set()
    version = scanner_version(exe)
    if isinstance(version, StartError):
        hint = f" {UV_TOOL_HINT}" if "trampoline" in version.lower() else ""
        print(f"[WARN] skillscan: SkillSpector does not start ({version}) - {len(skills)} skill(s) linked "
              f"unscanned.{hint}")
        return set()
    cache = load_cache()
    print(f"skillscan: SkillSpector {version}, {'static + LLM' if llm else 'static'} analysis of {len(skills)} skill(s)")
    results, groups, flagged = verdicts(skills, exe, version, llm, cache), {}, []
    days = configured_days() if days is None else days
    for s in skills:
        r = results[s.name]
        rec, score = r.get("recommendation"), r.get("score")
        if "error" in r:
            outcome, label = "failed", f"{s.name} ({r['error']})"
        elif rec == BLOCK:
            label = f"{s.name} ({score})"
            if s.name in allow:
                outcome = "allowed"
            elif r.get("accepted"):
                outcome = "kept earlier"
            elif accept_flagged:
                cache.get(s.name, {})["accepted"] = True
                outcome = "accepted"
            else:
                flagged.append((s, r))
                continue
        elif rec == CAUTION:
            outcome, label = CAUTION, f"{s.name} ({score})"
        elif rec == SAFE:
            continue
        else:
            outcome, label = "unknown", f"{s.name} ({rec!r})"
        groups.setdefault(outcome, []).append(label)
    if flagged:
        for (s, r), outcome in zip(flagged, _resolve_flagged(flagged, act, choose, cache, days)):
            groups.setdefault(outcome, []).append(f"{s.name} ({r.get('score')})")
    for outcome, text in _SUMMARY + (("unknown", "unknown verdict, linked"),):
        if groups.get(outcome):
            print(f"[WARN] skillscan: {len(groups[outcome])} {text}: {', '.join(groups[outcome])}")
    if groups.get(CAUTION) or groups.get("unasked"):
        print(f"  Review one: {EXE} scan \"{skills[0].parent / '<name>'}\"")
    blocked = {label.split(" (")[0] for label in groups.get("quarantined", [])}
    if blocked:
        print(f"skillscan: {len(blocked)} skill(s) quarantined: {', '.join(sorted(blocked))}; "
              f"restore one with `{P.command_hint('quarantine restore <name>')}`")
    if apply:
        save_cache(cache)
    return blocked
