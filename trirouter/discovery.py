#!/usr/bin/env python3
"""Model discovery: which models each harness offers today, compared with what the router knows.

    trirouter models --discover          the daily check, by hand (--dry-run: report only)
    trirouter models                     the full probe: also tries every known Codex model
    python -m trirouter.discovery --scheduled
                                         the daily check in the background, started by the hooks (maybe_start)
                                         at most once per 24 hours unless `models --auto=off`

Sources:
    Codex        `codex debug models` (JSON). A model not seen before is tried with a one-word prompt
                 before it becomes selectable; a hidden one (visibility "hide") is recorded, not selected.
    Antigravity  `agy models`: one slug per model and effort; the list is what the account may use.
    Claude Code  the aliases `claude --help` names for --model. Only generic family aliases count
                 (core.claude_alias_allowed: never Haiku, a dated ID or a mode alias). The help names its
                 aliases as examples, so an alias is tried with a one-word prompt before it is added, and
                 before one that is no longer named is removed.

Results go to ~/.trirouter/models.local.json only, never to the repository's models.json: a new model gets a
full entry (levels, role, description, ...), a model that is no longer offered gets "selectable": false and
"listed": false (it comes back by itself when it is offered again). A check that could not run -- CLI
missing, not logged in, network or auth error, unreadable output, a list that names none of the known
models -- changes nothing. Effort levels outside the policy (models.json policy.excluded_efforts) are
stripped from every discovered model.
"""
import copy
import json
import os
import re
import sys
import time
from pathlib import Path

from . import core, platforms as P

PKG = Path(__file__).resolve().parent
REPO = PKG.parent
ALL = ("claude", "codex", "antigravity")
LABELS = {"claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity"}
EXE = {p: P.PROVIDERS[p]["exe"] for p in ALL}
LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_LEVELS = ["low", "medium", "high"]
INTERVAL_S = 24 * 3600
LOCK_STALE_S = 2 * 3600       # a check that died keeps its lock this long at most
LOCK_WAIT_S = 10              # the background process waits this long for the hook to hand over the lock
PROBE_PROMPT = "#norouter Reply with exactly: OK"
PROBE_TIMEOUT_S = 180
CHILD_ENV = "TRIROUTER_DISCOVERY_CHILD"
LOG_LINES = 200
# A model the service refuses (as opposed to a call that failed for another reason: auth, network, quota).
REJECT_MARKERS = ("not supported", "does not exist", "model_not_found", "not_found_error", "no such model",
                  "unknown model", "invalid model", "may not exist", "issue with the selected model",
                  '"status":404', "status 404", "404 not found")


class Unchecked(Exception):
    """The provider could not be checked; nothing about it is changed."""


# ---- files in ~/.trirouter ---------------------------------------------------------------------------------

def local_file():
    return core.STATE_DIR / "models.local.json"


def state_file():
    return core.STATE_DIR / "state" / "model_discovery.json"


def lock_file():
    return core.STATE_DIR / "state" / "model_discovery.lock"


def note_file():
    return core.STATE_DIR / "state" / "model_discovery_note.txt"


def log_file():
    return core.STATE_DIR / "logs" / "model_discovery.log"


def _read_json(path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def _write_atomic(path, text):
    P.atomic_write(path, text)


def read_state():
    return _read_json(state_file(), {})


def write_state(state):
    _write_atomic(state_file(), json.dumps(state, indent=2))


def load_local():
    """models.local.json; Unchecked when it exists but is not a JSON object (it is then never overwritten)."""
    path = local_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unchecked(f"{path.name} is not valid JSON ({type(exc).__name__}); fix or delete it") from exc
    if not isinstance(data, dict):
        raise Unchecked(f"{path.name} does not hold a JSON object; fix or delete it")
    return data


def save_local(data):
    _write_atomic(local_file(), json.dumps(data, indent=2) + "\n")


def auto_enabled(cfg=None):
    """config.json model_discovery.auto; on unless switched off with `models --auto=off`."""
    cfg = core.user_config() if cfg is None else cfg
    return (cfg.get("model_discovery") or {}).get("auto", True) is not False


# ---- lock and schedule -------------------------------------------------------------------------------------

def acquire_lock():
    """True when this process now holds the lock; a lock older than LOCK_STALE_S is taken over."""
    path = lock_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > LOCK_STALE_S:
                    path.unlink()
                    continue
            except OSError:
                pass
            return False
        except OSError:
            return False
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()} {time.time():.0f}\n")
        return True
    return False


def release_lock():
    try:
        lock_file().unlink()
    except OSError:
        pass


def wait_lock(seconds=LOCK_WAIT_S):
    end = time.time() + seconds
    while True:
        if acquire_lock():
            return True
        if time.time() >= end:
            return False
        time.sleep(0.5)


def due(now=None):
    """The daily check is switched on and the last one started 24 hours ago or more (or never)."""
    now = time.time() if now is None else now
    if not auto_enabled():
        return False
    try:
        last = float(read_state().get("last_start") or 0)
    except (TypeError, ValueError):
        last = 0.0
    return not 0 <= now - last < INTERVAL_S


def child_env():
    """The background check's environment: marked (so its own probes never start another check), and without
    the variables that tell Claude Code it runs inside another Claude Code session."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDECODE") and k != "CLAUDE_CODE_ENTRYPOINT"}
    env[CHILD_ENV] = "1"
    return env


def background_argv():
    return [P.python_exe_windowless(), "-m", "trirouter.discovery", "--scheduled"]


def maybe_start(now=None):
    """Hook side: starts the background check when it is due. Returns at once, True when one was started;
    never raises. The start time is stamped under the lock, so parallel sessions start it only once."""
    try:
        if os.environ.get(CHILD_ENV) or core.is_cloud():
            return False
        now = time.time() if now is None else now
        if not due(now) or not acquire_lock():
            return False
        try:
            if not due(now):  # another session started it while we waited for the lock
                return False
            state = read_state()
            state["last_start"] = now
            write_state(state)
            return P.spawn_detached(background_argv(), env=child_env(), cwd=str(REPO))
        finally:
            release_lock()
    except Exception:  # noqa: BLE001 - called from hooks: never block or crash the host tool
        return False


def pending_note():
    """The one-line note about the last check's changes, once: it is removed when read. '' when none."""
    path = note_file()
    try:
        text = path.read_text(encoding="utf-8").strip()
        path.unlink()
        return text
    except OSError:
        return ""


# ---- listing ------------------------------------------------------------------------------------------------

def _first_line(text):
    return (text.strip().splitlines() or ["no output"])[-1][:160]


def list_codex(exe):
    """{slug: {levels, description, hidden}} from `codex debug models`."""
    code, out = P.run([exe, "debug", "models"], timeout=120)
    start = out.find("{")
    if code != 0 or start < 0:
        raise Unchecked(f"`codex debug models` failed: {_first_line(out)}")
    try:
        data, _ = json.JSONDecoder().raw_decode(out[start:])
    except ValueError as exc:
        raise Unchecked("`codex debug models` printed no readable JSON") from exc
    found = {}
    for m in (data.get("models") if isinstance(data, dict) else None) or []:
        slug = m.get("slug") if isinstance(m, dict) else None
        if not isinstance(slug, str) or not slug.strip():
            continue
        levels = [l.get("effort") for l in m.get("supported_reasoning_levels") or [] if isinstance(l, dict)]
        name, desc = m.get("display_name") or slug, (m.get("description") or "").strip().rstrip(".")
        found[slug] = {"levels": [l for l in levels if l in LEVELS], "hidden": m.get("visibility") not in (None, "list"),
                       "description": f"{name} - {desc}" if desc else name}
    if not found:
        raise Unchecked("`codex debug models` listed no model")
    return found


_AGY_LEVEL = re.compile(r"(.+)-(" + "|".join(LEVELS) + r")")
_PAREN_LEVEL = re.compile(r"\s*\((?:" + "|".join(LEVELS) + r")\)\s*$", re.I)


def list_antigravity(exe):
    """{model id: {levels, description, slugs}} from `agy models` (one `<slug> <name>` line per model and effort)."""
    code, out = P.run([exe, "models"], timeout=120)
    low = out.lower()
    if code != 0 or "not logged in" in low or "sign in" in low:
        raise Unchecked(f"`agy models` failed: {_first_line(out)}")
    found = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2 or line.startswith("Fetching") or not re.fullmatch(r"[A-Za-z0-9][\w.\-]*", parts[0]):
            continue
        slug, name = parts
        m = _AGY_LEVEL.fullmatch(slug)
        base, level = (m.group(1), m.group(2)) if m else (slug, None)
        entry = found.setdefault(base, {"levels": [], "slugs": [], "description": _PAREN_LEVEL.sub("", name).strip()})
        entry["slugs"].append(slug)
        if level and level not in entry["levels"]:
            entry["levels"].append(level)
    if not found:
        raise Unchecked("`agy models` listed no model")
    for entry in found.values():
        entry["levels"].sort(key=LEVELS.index)
    return found


def _option_block(text, option):
    """The help text of one option, joined into one line ('' when the option is not there)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"\s*(?:-\w,\s*)?" + re.escape(option) + r"(?=[\s=]|$)", line):
            block = [line]
            for nxt in lines[i + 1:]:
                if not nxt.strip() or re.match(r"\s*-", nxt):
                    break
                block.append(nxt)
            return " ".join(" ".join(block).split())
    return ""


def parse_claude_help(text):
    """(model names quoted in --model's help, effort levels of --effort's help)."""
    names = re.findall(r"'([^'\s]+)'", _option_block(text, "--model"))
    m = re.search(r"\(([^)]*)\)", _option_block(text, "--effort"))
    levels = [l.strip() for l in m.group(1).split(",")] if m else []
    return list(dict.fromkeys(n.lower() for n in names)), [l for l in levels if l in LEVELS]


def list_claude(exe, catalog):
    """({alias: {levels, description}}, [(name, reason)] rejected by the policy) from `claude --help`."""
    code, out = P.run([exe, "--help"], timeout=60)
    if code != 0:
        raise Unchecked(f"`claude --help` failed: {_first_line(out)}")
    names, levels = parse_claude_help(out)
    if not names:
        raise Unchecked("`claude --help` names no model alias")
    excluded = catalog.get("claude", {}).get("excluded_families", ["haiku"])
    found, rejected = {}, []
    for n in names:
        if core.claude_alias_allowed(n, catalog):
            found[n] = {"levels": levels, "description": f"Claude {n.capitalize()} - generic Claude Code alias"}
        else:
            reason = "excluded family" if any(x in n for x in excluded) else "not a generic family alias"
            rejected.append((n, reason))
    return found, rejected


# ---- probing ------------------------------------------------------------------------------------------------

def verdict(code, out):
    """True: the model answered; False: the service refused the model; None: the call failed otherwise."""
    if any(m in (out or "").lower() for m in REJECT_MARKERS):
        return False
    return True if code == 0 else None


def probe_codex(exe, model_id):
    return verdict(*P.run([exe, "exec", "--skip-git-repo-check", "-m", model_id, "-c", "model_reasoning_effort=low",
                           PROBE_PROMPT], timeout=PROBE_TIMEOUT_S, env=child_env()))


def probe_claude(exe, alias):
    return verdict(*P.run([exe, "-p", "--model", alias, PROBE_PROMPT], timeout=PROBE_TIMEOUT_S, env=child_env()))


PROBES = {"codex": probe_codex, "claude": probe_claude, "antigravity": lambda exe, model_id: True}


# ---- one provider -------------------------------------------------------------------------------------------

def infer_role(model_id, text=""):
    s = f"{model_id} {text}".lower()
    if re.search(r"\b(pro|opus|ultra|strongest|most capable|frontier)\b", s):
        return "deep"
    if re.search(r"\b(lite|mini|nano)\b", s):
        return "fast"
    return "balanced"


def clean_levels(levels, banned):
    out = [l for l in levels or [] if l in LEVELS and l not in banned]
    return out or [l for l in DEFAULT_LEVELS if l not in banned]


def new_entry(provider, model_id, info, ok, today, banned):
    levels = [l for l in info.get("levels") or [] if l in LEVELS and l not in banned]
    if provider != "antigravity":  # Antigravity: no suffix means one fixed setting, not "unknown"
        levels = clean_levels(levels, banned)
    entry = {"selectable": bool(ok), "listed": True, "discovered": today, "role": infer_role(model_id, info.get("description", "")),
             "levels": levels, "description": f"{info.get('description') or model_id} (discovered {today})"}
    if provider == "antigravity":
        entry["slug"] = "{id}-{effort}" if levels else "{id}"
    if info.get("hidden"):
        entry["hidden"] = True
    return entry


def _selectable(cat_model, entry):
    if cat_model is not None and not cat_model.get("routable", True):
        return False
    if isinstance(entry, dict) and "selectable" in entry:
        return bool(entry["selectable"])
    return bool(cat_model and cat_model.get("selectable"))


def _is_listed(provider, model_id, cat_model, listing):
    if model_id in listing:
        return True
    if provider == "antigravity" and cat_model is not None:
        slugs = {s for e in listing.values() for s in e["slugs"]}
        tpl = cat_model.get("slug") or "{id}"
        return bool({tpl.format(id=model_id, effort=e) for e in (cat_model.get("levels") or [""])} & slugs)
    return False


def _word(ok):
    return {True: "available", False: "not available", None: "could not check"}[ok]


def check_provider(provider, exe, catalog, local, mode, today, say):
    """Updates local[provider] in place; returns the result. Raises Unchecked when nothing could be learnt."""
    res = {"provider": provider, "status": "checked", "reason": "", "added": [], "removed": [], "restored": [],
           "found_unavailable": [], "hidden": [], "rejected": [], "pending": []}
    banned = set(catalog.get("policy", {}).get("excluded_efforts", ["ultra"]))
    if provider == "claude":
        listing, res["rejected"] = list_claude(exe, catalog)
    else:
        listing = list_codex(exe) if provider == "codex" else list_antigravity(exe)
    known = {m["id"]: m for m in catalog.get(provider, {}).get("models", [])}
    loc = local.setdefault(provider, {})
    ids = list(known) + [i for i in loc if i not in known]
    selected = [i for i in ids if _selectable(known.get(i), loc.get(i))]
    if selected and not any(_is_listed(provider, i, known.get(i), listing) for i in selected):
        raise Unchecked("the list names none of the models in use; not trusted")
    probe = PROBES[provider]
    full = mode == "probe" and provider == "codex"

    for model_id, info in listing.items():  # new models
        if model_id in known or model_id in loc:
            continue
        if info.get("hidden"):
            loc[model_id] = new_entry(provider, model_id, info, False, today, banned)
            res["hidden"].append(model_id)
            say(f"  {model_id:<28} new, hidden in the model picker: recorded, not selected")
            continue
        ok = probe(exe, model_id)
        say(f"  {model_id:<28} new, {_word(ok)}")
        if ok is None:
            res["pending"].append(model_id)
            continue
        loc[model_id] = new_entry(provider, model_id, info, ok, today, banned)
        res["added" if ok else "found_unavailable"].append(model_id)

    for model_id in ids:  # models the router already knows
        cat, entry = known.get(model_id), loc.get(model_id)
        if cat is not None and not cat.get("routable", True):
            continue
        was, listed = _selectable(cat, entry), _is_listed(provider, model_id, cat, listing)
        if full:
            if isinstance(entry, dict) and entry.get("hidden"):
                continue
            ok = probe(exe, model_id)
            say(f"  {model_id:<28} {_word(ok)}")
            if ok is None:
                res["pending"].append(model_id)
            else:
                _set(loc, model_id, selectable=ok, listed=listed, today=today)
                if was != ok:
                    res["restored" if ok else "removed"].append(model_id)
        elif listed and isinstance(entry, dict) and entry.get("listed") is False:  # offered again
            ok = probe(exe, model_id)
            say(f"  {model_id:<28} offered again, {_word(ok)}")
            if ok is None:
                res["pending"].append(model_id)
            else:
                _set(loc, model_id, selectable=ok, listed=True, today=today)
                if ok:
                    res["restored"].append(model_id)
        elif not listed and was:  # no longer offered
            ok = probe(exe, model_id) if provider == "claude" else False  # Claude's help names examples only
            if ok is False:
                _set(loc, model_id, selectable=False, listed=False, today=today)
                res["removed"].append(model_id)
                say(f"  {model_id:<28} no longer offered: removed from the selection")
            elif ok is None:
                res["pending"].append(model_id)
    if not loc:
        local.pop(provider, None)
    return res


def _set(loc, model_id, selectable, listed, today):
    entry = dict(loc.get(model_id) or {})
    entry.update(selectable=bool(selectable), listed=bool(listed))
    if selectable:
        entry.pop("removed", None)
    elif not listed:
        entry.setdefault("removed", today)
    loc[model_id] = entry


# ---- the whole check ----------------------------------------------------------------------------------------

def unchecked(provider, reason):
    return {"provider": provider, "status": "unchecked", "reason": reason, "added": [], "removed": [], "restored": [],
            "found_unavailable": [], "hidden": [], "rejected": [], "pending": []}


def run(mode="discover", apply=False, say=print, providers=ALL):
    """mode "discover": list every harness's models, try only new (or returning) ones; "probe": also try every
    known Codex model. With apply, models.local.json, the state, the log and the note are written and the
    worker agents regenerated when something changed. Returns {"results": [...], "changed": bool, "local": {...}}."""
    say = say or (lambda *_a, **_k: None)
    catalog = core.load_json("models.json", {})
    today = time.strftime("%Y-%m-%d")
    try:
        local = load_local()
    except Unchecked as exc:
        results = [unchecked(p, str(exc)) for p in providers]
        if apply:
            record(results, False, mode)
        return {"results": results, "changed": False, "local": None}
    new_local = copy.deepcopy(local)
    results = []
    for provider in providers:
        exe = P.find_exe(EXE[provider])
        if not exe:
            results.append(unchecked(provider, f"`{EXE[provider]}` is not installed"))
            continue
        say(f"{LABELS[provider]}: checking the model list...")
        trial = copy.deepcopy(new_local)
        try:
            res = check_provider(provider, exe, catalog, trial, mode, today, say)
            new_local = trial
        except Unchecked as exc:
            res = unchecked(provider, str(exc))
        except Exception as exc:  # noqa: BLE001 - one provider's surprise must not stop the others
            res = unchecked(provider, f"unexpected {type(exc).__name__}")
        results.append(res)
    changed = new_local != local
    if apply:
        if changed:
            save_local(new_local)
        record(results, changed, mode)
        if changed:
            regenerate_agents(say)
    return {"results": results, "changed": changed, "local": new_local}


def regenerate_agents(say):
    from . import hub  # lazy: the hooks never need it
    hub.APPLY = True
    hub.PROVIDERS = tuple(p for p in ALL if P.find_exe(EXE[p])) or ALL
    say("Regenerating the worker agents:")
    hub.cmd_agents()


def summary_lines(results):
    out = []
    for r in results:
        label = f"{LABELS[r['provider']]:<12}"
        if r["status"] != "checked":
            out.append(f"{label} could not check: {r['reason']} (nothing changed)")
            continue
        parts = [f"new: {', '.join(r['added'])}" if r["added"] else "",
                 f"back: {', '.join(r['restored'])}" if r["restored"] else "",
                 f"removed: {', '.join(r['removed'])}" if r["removed"] else "",
                 f"found but not available: {', '.join(r['found_unavailable'])}" if r["found_unavailable"] else "",
                 f"new hidden (not selected): {', '.join(r['hidden'])}" if r["hidden"] else "",
                 f"policy rejected: {', '.join(n for n, _ in r['rejected'])}" if r["rejected"] else "",
                 f"retry next time: {', '.join(r['pending'])}" if r["pending"] else ""]
        out.append(f"{label} " + ("; ".join(p for p in parts if p) or "no change"))
    return out


def note_text(results):
    added = [f"{LABELS[r['provider']]} {m}" for r in results for m in r["added"] + r["restored"]]
    removed = [f"{LABELS[r['provider']]} {m}" for r in results for m in r["removed"]]
    if not added and not removed:
        return ""
    parts = ([f"new model available: {', '.join(added)}"] if added else []) + \
            ([f"removed (no longer offered): {', '.join(removed)}"] if removed else [])
    return "[trirouter] Model list updated - " + "; ".join(parts) + ". The worker agents were regenerated."


def record(results, changed, mode):
    """State (last check, per-provider outcome), one log line, and the note for the next session."""
    now = time.time()
    state = read_state()
    state.update(last_start=max(float(state.get("last_start") or 0), now), last_finished=now,
                 last_run=time.strftime("%Y-%m-%dT%H:%M:%S"), mode=mode, changed=changed,
                 results={r["provider"]: {k: r[k] for k in ("status", "reason", "added", "removed", "restored")}
                          for r in results})
    write_state(state)
    _log(f"{state['last_run']} {mode}: " + " | ".join(" ".join(l.split()) for l in summary_lines(results)))
    note = note_text(results)
    if note:
        _write_atomic(note_file(), note + "\n")


def _log(line):
    path = log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        path.write_text("\n".join((old + [line])[-LOG_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def last_check():
    """(time text, changed, results) of the last finished check, or None."""
    state = read_state()
    return (state["last_run"], state.get("changed"), state.get("results") or {}) if state.get("last_run") else None


# ---- the background process ---------------------------------------------------------------------------------

def run_scheduled():
    if not wait_lock():
        return
    try:
        work = core.STATE_DIR / "tmp"
        work.mkdir(parents=True, exist_ok=True)
        os.chdir(work)  # the probes run here, not in a project folder
        run("discover", apply=True, say=None)
    finally:
        release_lock()


def main(argv=None):
    """Always 0. Started detached by maybe_start; every error ends up in the log."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--scheduled" not in args:
        print("usage: python -m trirouter.discovery --scheduled   (run `trirouter models --discover` by hand)")
        return 0
    if sys.stdout is None:  # pythonw: no console
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    P.NO_WINDOW = True
    try:
        run_scheduled()
    except Exception as exc:  # noqa: BLE001
        _log(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} error: {type(exc).__name__}: {exc}"[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
