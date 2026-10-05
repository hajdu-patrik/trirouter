#!/usr/bin/env python3
"""trirouter command line: `trirouter <command> [subcommand] [flags]`, or `python install.py ...` /
`python -m trirouter ...` (same commands; without a command these two run the interactive setup).

The commands, flags and help texts live in manual.py (one source for the parser, the help pages and
docs/cli.md); argument validation is in cliparse.py; this module does the work.

    trirouter help [command]     manual page; `trirouter <command> --help` too
    trirouter setup | detect | models | skills | quarantine | remote | doctor | route | uninstall | version

`python install.py --help` prints the command list generated from manual.py. Other programs call the
route command through the installer's shim: python ~/.trirouter/bin/route.py --json "<text>"
Requires Python 3.10+ and nothing else.
"""
import difflib
import getpass
import json
import os
import sys
from pathlib import Path

from . import __version__, cliparse, core, doctor, hooks, hub, integrations, legacy, manual, platforms as P, remote, skillscan

PKG = Path(__file__).resolve().parent
REPO = PKG.parent

STATE = legacy.state_dir(P.HOME)  # the old ~/.jev-router until migrated (prepare_state)
CONFIG = STATE / "config.json"
MODELS_LOCAL = STATE / "models.local.json"
ALL = ("claude", "codex", "antigravity")

FLAGS, POSITIONAL, COMMAND, YES, DRY = {}, [], "setup", False, False


def program_name():
    """`trirouter` when started through the launcher (it sets TRIROUTER_PROG), else how it was started."""
    if os.environ.get("TRIROUTER_PROG"):
        return os.environ["TRIROUTER_PROG"]
    name = Path(sys.argv[0] or "").name.lower()
    if name in ("trirouter", "trirouter.exe", "trirouter.py"):  # the launcher or a pip console script
        return "trirouter"
    return "python install.py" if name == "install.py" else "python -m trirouter"


def configure(inv):
    """Publishes a parsed invocation to the module-level state the commands read."""
    global FLAGS, POSITIONAL, COMMAND, YES, DRY
    FLAGS, POSITIONAL, COMMAND = inv.flags, inv.positional, inv.key
    YES = "--yes" in FLAGS
    DRY = "--dry-run" in FLAGS


# Commands that write per-user state (config.json, models.local.json, quarantine, remote-access scripts) or
# rewrite the tools' configs: they migrate a pre-rename ~/.jev-router first, so nothing is ever written to a
# half-migrated pair of folders.
STATE_WRITERS = ("setup", "skills", "models", "remote", "uninstall", "quarantine restore", "quarantine purge")


def say(msg=""):
    print(msg, flush=True)


def interactive():
    """stdin is a real console, not a pipe, a file or NUL."""
    return P.is_terminal(sys.stdin)


def ask(question, default="y"):
    """Without a terminal or --yes every answer is "no": an unattended run never moves files unasked."""
    if YES:
        return default.lower().startswith("y")
    if not interactive():
        return False
    ans = input(f"{question} [{'Y/n' if default.lower().startswith('y') else 'y/N'}] ").strip().lower()
    return (ans or default).startswith("y")


def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    if DRY:
        return
    STATE.mkdir(parents=True, exist_ok=True)
    # owner-only from the start: it holds the JEV token
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, indent=2))
    if not P.IS_WINDOWS:
        os.chmod(CONFIG, 0o600)  # os.open's mode does not apply to an existing file


def rebase_state(state):
    """Points every module that captured the state folder at import time to `state` (after a migration)."""
    global STATE, CONFIG, MODELS_LOCAL
    STATE, CONFIG, MODELS_LOCAL = state, state / "config.json", state / "models.local.json"
    core.STATE_DIR = state
    doctor.STATE = state
    remote.STATE, remote.BIN, remote.CONFIG = state, state / "bin", state / "config.json"
    remote.LOG = state / "logs" / "claude-remote.log"
    skillscan.STATE, skillscan.CONFIG = state, state / "config.json"
    skillscan.QUARANTINE, skillscan.CACHE = state / "quarantine", state / "state" / "skillscan.json"


def repoint_install(apply):
    """After the state folder moved: the shims in the old bin folder are gone, so every hook, MCP entry, the PATH
    entry and every remote-access service that points at them is rewritten (old entries are recognized as ours and
    replaced, never duplicated). Only tools that already have router entries are touched."""
    providers, has_old = integrations.configured_providers()
    if providers:
        say(f"\nRe-pointing the existing hooks and MCP entries ({', '.join(providers)}) to {integrations.BIN}:")
        integrations.install(providers, apply=apply)
    cfg = load_config()
    name = cfg.get("remote_name") or P.hostname()
    report(remote.migrate(name, remote_workdir(cfg), apply=apply), indent="  ")


def prepare_state(key):
    """Moves ~/.jev-router to ~/.trirouter before a command that writes state (see legacy.migrate_state).
    Returns an exit code to stop with, or None to continue. A dry run only reports."""
    if key not in STATE_WRITERS or (key == "remote" and "--remove" in FLAGS):
        return None
    apply = not DRY and (key != "skills" or "--apply" in FLAGS)
    status = legacy.migrate_state(P.HOME, apply=apply, say=say)
    if status == "failed":
        say("Nothing else was changed. The hooks keep working from the old folder.")
        return 1
    if status in ("moved", "merged"):
        rebase_state(P.STATE)
        if key != "uninstall":  # an uninstall removes every entry anyway
            repoint_install(apply=True)
    elif status.startswith("would-") and key != "uninstall":
        say("[DRY] ...then every hook, MCP entry, the PATH entry and remote-access service that points at the old "
            "folder is rewritten.")
    elif apply and key != "uninstall" and integrations.configured_providers()[1]:
        repoint_install(apply=True)  # the folder was moved by hand, but old entries remain
    return None


def report(lines, indent=""):
    for tool, ok, msg in lines:
        say(f"{indent}[{'OK' if ok else '!!'}] {tool}: {msg}")


def print_report(found):
    say(f"\n{'Tool':<26}{'Installed':<11}{'Logged in':<11}Version")
    for info in found.values():
        li = {True: "yes", False: "NO", None: "unknown"}[info["logged_in"]] if info["installed"] else "-"
        say(f"{info['label']:<26}{'yes' if info['installed'] else 'no':<11}{li:<11}{info['version'] or ''}")


def detect_and_login():
    say("== 1/5  Detecting AI tools (this can take a minute: Antigravity is asked for its model list)")
    found = P.detect()
    print_report(found)
    for p, info in found.items():
        spec = P.PROVIDERS[p]
        if not info["installed"]:
            say(f"\n  {spec['label']} is not installed - optional. Install: {spec['install']}")
            continue
        while info["logged_in"] is False and not YES and interactive():
            say(f"\n  {spec['label']} is installed but NOT logged in. In another terminal run:\n      {spec['login']}")
            if input("  Press Enter when done (s = skip this tool): ").strip().lower() == "s":
                break
            info.update(P.detect_one(p))
    chosen = FLAGS.get("--providers")
    if isinstance(chosen, str):
        providers = [p for p in chosen.split(",") if p in ALL]
    else:
        providers = [p for p, i in found.items() if i["installed"] and i["logged_in"] is not False]
    say(f"\n  Tools to connect: {', '.join(providers) or 'none'}")
    return providers


def store_jev_key(cfg, key):
    """An OpenRouter key (sk-or-...) reaches JEV through OpenRouter, anything else is a TypeSafe token."""
    cfg["openrouter_api_key" if key.startswith("sk-or-") else "typesafe_api_key"] = key


def jev_backend_label(cfg):
    if cfg.get("typesafe_api_key") or os.environ.get("TYPESAFE_API_KEY"):
        return "JEV (TypeSafe token)"
    if cfg.get("openrouter_api_key") or os.environ.get("JEV_OPENROUTER_API_KEY"):
        return "JEV through OpenRouter"
    return "built-in local model (add JEV later with `trirouter setup --jev-token=<TypeSafe token or OpenRouter key>`)"


def configure_jev(cfg):
    say("\n== 2/5  JEV / TypeSafe (the routing decision engine)")
    token = FLAGS.get("--openrouter-key") or FLAGS.get("--jev-token")
    if isinstance(token, str) and token:
        store_jev_key(cfg, token)
    elif os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_OPENROUTER_API_KEY"):
        say("  Using the JEV key from the environment.")
    elif cfg.get("typesafe_api_key") or cfg.get("openrouter_api_key"):
        say("  A JEV key is already configured.")
    elif not YES and interactive():
        token = getpass.getpass("  TypeSafe token or OpenRouter key (input hidden; Enter = built-in local model): ").strip()
        if token:
            store_jev_key(cfg, token)
    say("  Backend: " + jev_backend_label(cfg))


def scan_settings(cfg, persist):
    """config.json's skillscan block plus --allow-skill / --scan-llm; flags are saved only when applying."""
    sc = dict(cfg.get("skillscan") or {})
    names = FLAGS.get("--allow-skill")
    if isinstance(names, str):
        sc["allow"] = sorted(set(sc.get("allow") or []) | {n.strip() for n in names.split(",") if n.strip()})
    llm = FLAGS.get("--scan-llm")
    if isinstance(llm, str):
        sc["llm"] = llm.strip().lower() in ("on", "1", "true", "yes")
    days = FLAGS.get("--quarantine-days")
    if isinstance(days, str):
        sc["quarantine_days"] = int(days)
    if persist and sc != (cfg.get("skillscan") or {}):
        cfg["skillscan"] = sc
        save_config(cfg)
    hub.SCAN = {"llm": bool(sc.get("llm")), "allow": tuple(sc.get("allow") or ()),
                "accept_flagged": "--accept-flagged" in FLAGS,  # one-off, never saved
                "quarantine_days": sc.get("quarantine_days", skillscan.DEFAULT_DAYS)}
    hub.CHOOSE = choose_quarantine


def choose_quarantine(items):
    """One question for all undecided DO_NOT_INSTALL skills: the set of names to quarantine, or None when
    nobody can be asked (--yes, no terminal). Anything but an explicit choice keeps the skills."""
    if YES or not interactive():
        return None
    say(f"\n{len(items)} skill(s) rated {skillscan.BLOCK} (review one: {skillscan.EXE} scan \"<path>\"):")
    for n, it in enumerate(items, 1):
        say(f"  {n}. {it['name']} (risk {it['score']}, max {it['max_severity']})")
    names = [it["name"] for it in items]
    while True:
        ans = input("Quarantine these skills? [a]ll / [n]one / [s]elect (default: none) ").strip().lower()
        if ans in ("", "n", "none"):
            return set()
        if ans in ("a", "all"):
            return set(names)
        if ans not in ("s", "select"):
            say("  Please answer a, n or s.")
            continue
        picked = select_numbers(len(items))
        if picked is None:
            continue
        chosen = {names[i] for i in picked}
        say("  quarantine: " + (", ".join(n for n in names if n in chosen) or "-"))
        say("  keep:       " + (", ".join(n for n in names if n not in chosen) or "-"))
        if input("Go ahead? [y/N] ").strip().lower().startswith("y"):
            return chosen


def select_numbers(count):
    """Asks for numbers like 1,3,5-8 until they are valid; the zero-based indexes, or None when the answer is empty."""
    while True:
        text = input("Numbers to quarantine (e.g. 1,3,5-8; Enter = none): ").strip()
        if not text:
            return None
        try:
            picked = skillscan.parse_selection(text, count)
        except ValueError as exc:
            say(f"  {exc}.")
            continue
        if picked:
            return picked


def connect(providers, cfg):
    say("\n== 3/5  Hooks + MCP server")
    integrations.install(providers, apply=not DRY)
    say("\n== 4/5  Shared skill folder (~/.skills) and worker agents")
    hub.PROVIDERS = tuple(providers)
    hub.APPLY = not DRY
    scan_settings(cfg, persist=not DRY)
    if "--no-migrate" not in FLAGS:
        hub.APPLY = False
        say("  Skills that would move into ~/.skills (each replaced by a link, so every tool keeps it):")
        hub.cmd_migrate()
        hub.APPLY = not DRY and ask("  Move them now?", "y")
        if hub.APPLY:
            hub.cmd_migrate()
        hub.APPLY = not DRY
    hub.cmd_link()
    purge_old_quarantine()
    hub.cmd_agents()
    if not DRY:
        hub.cmd_catalog()
    hub.cmd_doctor()


def purge_old_quarantine():
    """Deletes quarantine entries past their retention (a dry run only reports them)."""
    skillscan.purge_expired(apply=hub.APPLY, days=hub.SCAN.get("quarantine_days"))


def ask_remote_name(cfg):
    default = cfg.get("remote_name") or P.hostname()
    if YES or not interactive():
        return default
    return input(f"  Name shown on your other devices [{default}]: ").strip() or default


def extras(providers, cfg):
    say("\n== 5/5  Optional extras")
    name = FLAGS.get("--remote")
    if name or (not YES and ask("  Set up remote access (control this computer from a phone / another device)?", "n")):
        if not isinstance(name, str):
            name = ask_remote_name(cfg)
        report(remote.setup(name, providers, apply=not DRY, workdir=remote_workdir(cfg)), indent="  ")
    say("  Speech-to-text (dictation into any app): see docs/speech-to-text.md")
    offer_handy = P.IS_WINDOWS and not YES and not DRY and not P.find_exe("handy")
    if offer_handy and ask("  Install Handy (offline dictation) with winget now?", "n"):
        os.system("winget install --id cjpais.Handy -e --accept-source-agreements --accept-package-agreements")


def remote_workdir(cfg):
    """Claude Code only serves trusted folders, and never the home directory."""
    wd = FLAGS.get("--workdir")
    return str(Path(wd).resolve()) if isinstance(wd, str) else (cfg.get("remote_workdir") or str(REPO))


def next_steps(providers):
    say("\nDone. Next steps:")
    if "codex" in providers:
        say("  * Codex runs a new or changed hook only after you trust it once: run `codex`, type /hooks, trust the router hooks.")
    if "claude" in providers:
        say("  * Claude desktop Chat/Cowork: restart the app, then add to Settings > Profile > Personal preferences:")
        say('      "Before answering any new request, call the trirouter route_prompt tool with my message and follow its instructions."')
        say(f"    The MCP server was renamed from {legacy.MCP_NAME} to {integrations.MCP_NAME}: if you added the old sentence "
            f"(\"... call the {legacy.MCP_NAME} route_prompt tool ...\"), replace it with this one.")
    if not DRY:
        say("  * The `trirouter` command is installed: reopen your terminal (a new PATH is only seen by new terminals), "
            "then run `trirouter help`.")
    say(f"  * Optional: `{P.command_hint('models')}` checks which Codex/Antigravity models your account can use.")
    say(f"  * Health check any time: {P.command_hint('doctor')}")


def codex_accepts(codex, model_id):
    code, out = P.run([codex, "exec", "--skip-git-repo-check", "-m", model_id, "-c", "model_reasoning_effort=low",
                       "#norouter Reply with exactly: OK"], timeout=180)
    low = out.lower()
    return code == 0 and "ok" in low and "not supported" not in low and "does not exist" not in low


def record_model(local, provider, model_id, ok):
    local.setdefault(provider, {})[model_id] = {"selectable": ok}
    say(f"  {model_id:<28} {'available' if ok else 'not available'}")


def probe_models():
    """Writes models.local.json, which overrides models.json per account."""
    catalog = json.loads((PKG / "config" / "models.json").read_text(encoding="utf-8"))
    local = json.loads(MODELS_LOCAL.read_text(encoding="utf-8")) if MODELS_LOCAL.exists() else {}
    if codex := P.find_exe("codex"):
        say("Codex: testing each model with a one-word prompt (about 10-60 s per model)...")
        for m in catalog["codex"]["models"]:
            record_model(local, "codex", m["id"], codex_accepts(codex, m["id"]))
    if agy := P.find_exe("agy"):
        _, out = P.run([agy, "models"], timeout=180)
        listed = {line.split()[0] for line in out.splitlines() if line.strip() and not line.startswith("Fetching")}
        for m in catalog["antigravity"]["models"]:
            slugs = {m["slug"].format(id=m["id"], effort=e) for e in (m["levels"] or [""])}
            record_model(local, "antigravity", m["id"], bool(slugs & listed))
    if not DRY:
        STATE.mkdir(parents=True, exist_ok=True)
        MODELS_LOCAL.write_text(json.dumps(local, indent=2), encoding="utf-8")
        hub.APPLY = True
        hub.cmd_agents()
    say(f"Saved to {MODELS_LOCAL}")


def run_skills():
    found = P.detect(deep=False)
    hub.PROVIDERS = tuple(p for p, i in found.items() if i["installed"])
    hub.APPLY = "--apply" in FLAGS and not DRY
    scan_settings(load_config(), persist=hub.APPLY)
    hub.cmd_link()
    purge_old_quarantine()
    hub.cmd_agents()
    if hub.APPLY:
        hub.cmd_catalog()
    hub.cmd_doctor()
    if not hub.APPLY:
        say(f"\nPreview only. Re-run with --apply to write the changes: {P.command_hint('skills --apply')}")


def route_usage():
    return f"usage: {program_name()} route [--provider claude] [--json] [--] <prompt text...>   (no text: read stdin)"


ROUTE_PROVIDERS = ALL + ("claude-chat",)
ROUTE_KEYS = ("provider", "model", "effort", "agent", "tier", "task", "difficulty", "extra_agents", "destructive",
              "skill", "verify", "lang", "backend", "text", "note")


def provider_option(argv, i):
    _, eq, provider = argv[i].partition("=")
    if not eq:
        i += 1
        provider = argv[i] if i < len(argv) else ""
    if provider not in ROUTE_PROVIDERS:
        raise ValueError(f"--provider must be one of: {', '.join(ROUTE_PROVIDERS)}")
    return provider, i


def parse_route_args(argv):
    """(provider, as_json, prompt or None), or None for --help. A usage error's message never
    echoes an option's value."""
    provider, as_json, words, i = "claude", False, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            words += argv[i + 1:]
            break
        if a in ("-h", "--help"):
            return None
        if a == "--json":
            as_json = True
        elif a == "--provider" or a.startswith("--provider="):
            provider, i = provider_option(argv, i)
        elif a.startswith("--"):
            name = a.partition("=")[0]
            near = difflib.get_close_matches(name, ["--json", "--provider", "--help"], n=1, cutoff=0.5)
            raise ValueError(f"unknown option {name}" + (f" (did you mean {near[0]}?)" if near else ""))
        else:
            words.append(a)
        i += 1
    return provider, as_json, (" ".join(words) if words else None)


def read_stdin():
    """'' for an interactive terminal: never wait for typing."""
    stream = sys.stdin
    if stream is None or P.is_terminal(stream):
        return ""
    data = stream.buffer.read() if hasattr(stream, "buffer") else stream.read()
    return data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data


def route_decision(prompt, provider):
    """No side effects: no queue state, no routing log. #norouter / #privat never leave this machine."""
    out = dict.fromkeys(ROUTE_KEYS)
    out.update(provider=provider, extra_agents=0, text="")
    if any(tag in prompt.lower() for tag in hooks.SKIP_TAGS):
        out.update(destructive=core.is_destructive(prompt), lang=core.lang.detect(prompt),
                   note="#norouter / #privat: not routed, nothing sent to TypeSafe.")
        return out
    d, text, hit, error = core.route(prompt, provider)
    out.update(model=d.get("target_model"), effort=d.get("effort"), agent=d.get("target_agent"), tier=d["primary"],
               task=d["task"], difficulty=d["level"], extra_agents=d.get("extra_agents", 0), destructive=bool(hit),
               skill=d.get("skill"), verify=d.get("verify") or None, lang=d["lang"], backend=d.get("backend"), text=text)
    if error:  # only the exception type: its message could carry request details
        out["note"] = f"JEV unavailable ({error.split(':', 1)[0]}); the built-in classifier decided."
    return out


def run_route(argv):
    """Exit codes: 0 success, 2 usage error, 1 unexpected error."""
    try:
        parsed = parse_route_args(argv)
    except ValueError as exc:
        print(f"route: {exc}\n{route_usage()}\nRun `{program_name()} help route` for the options.", file=sys.stderr)
        return 2
    if parsed is None:
        print(manual.render_help("route", program_name()))
        return 0
    provider, as_json, prompt = parsed
    try:
        prompt = (read_stdin() if prompt is None else prompt).strip()
        if not prompt:
            print(f"route: empty prompt\n{route_usage()}", file=sys.stderr)
            return 2
        result = route_decision(prompt, provider)
        if as_json:
            print(json.dumps(result))  # ASCII-escaped, safe for any console code page
        elif result["text"]:
            print(result["text"])
    except Exception as exc:  # noqa: BLE001 - stable exit code; never print details (could hold secrets)
        print(type(exc).__name__, file=sys.stderr)
        return 1
    return 0


def run_setup():
    say("trirouter setup" + (" (dry run - nothing will be changed)" if DRY else ""))
    providers = detect_and_login()
    if not providers:
        say("\nNo logged-in tool found. Install and log in to at least one of them, then run this again.")
        return 1
    cfg = load_config()
    configure_jev(cfg)
    save_config(cfg)
    connect(providers, cfg)
    extras(providers, load_config() if not DRY else cfg)
    next_steps(providers)
    return 0


def run_remote():
    if "--remove" in FLAGS:
        if DRY:
            say("Dry run: would remove the remote-access services (scheduled tasks / launchd / systemd) "
                "and stop the Antigravity and Codex remote daemons.")
        else:
            report(remote.remove())
        return
    found = P.detect(deep=False)
    providers = [p for p, i in found.items() if i["installed"]]
    cfg = load_config()
    name = FLAGS.get("--name") if isinstance(FLAGS.get("--name"), str) else (cfg.get("remote_name") or P.hostname())
    report(remote.setup(name, providers, apply=not DRY, workdir=remote_workdir(cfg)))


def run_uninstall():
    integrations.install(ALL, apply=not DRY, uninstall=True)
    if not DRY:
        report(remote.remove())
    say("Hooks, MCP entries and remote access removed. Your skills stay in ~/.skills (and linked).")


def run_quarantine(key):
    cfg = load_config()
    if key == "quarantine list":
        return quarantine_list(cfg)
    if key == "quarantine restore":
        return quarantine_restore(cfg)
    return quarantine_purge(cfg)


def quarantine_list(cfg):
    days = cfg.get("skillscan", {}).get("quarantine_days", skillscan.DEFAULT_DAYS)
    rows = skillscan.entries(apply=False, days=days)
    if not rows:
        say("Quarantine is empty.")
        return 0
    now = skillscan.utcnow()
    say(f"{'NAME':<30}{'QUARANTINED':<13}{'PURGED':<28}RISK")
    for r in rows:
        left = f"{r['purge_at']:%Y-%m-%d} ({skillscan.human_left(r['purge_at'] - now)})" if r["purge_at"] else "never"
        risk = f"{r['risk']} ({r['max_severity']})" if r["risk"] is not None else "-"
        say(f"{r['name']:<30}{r['quarantined_at']:%Y-%m-%d}   {left:<28}{risk}")
    say(f"\n{len(rows)} skill(s). Restore one: {P.command_hint('quarantine restore <name>')}")
    return 0


def quarantine_restore(cfg):
    hub.APPLY = not DRY
    restored, problems = skillscan.restore(POSITIONAL, hub.HUB, hub.act)
    for msg in problems:
        say(f"[!!]  {msg}")
    if restored and not DRY:
        sc = dict(cfg.get("skillscan") or {})
        sc["allow"] = sorted(set(sc.get("allow") or []) | set(restored))
        cfg["skillscan"] = sc
        save_config(cfg)
    if restored:
        say(f"Restored {', '.join(restored)}: {'now in' if not DRY else 'would go to'} {hub.HUB} and allowed in "
            f"config.json. Link {'it' if len(restored) == 1 else 'them'} into the tools with "
            f"`{P.command_hint('skills --apply')}`.")
    return 1 if problems else 0


def quarantine_purge(cfg):
    scan_settings(cfg, persist=not DRY)
    days = hub.SCAN["quarantine_days"]
    everything = "--all" in FLAGS
    if everything:
        rows = skillscan.entries(apply=False, days=days)
        if not rows:
            say("Quarantine is empty.")
            return 0
        if not DRY and not YES:
            say(f"This permanently deletes {len(rows)} quarantined skill(s): {', '.join(r['name'] for r in rows)}")
            if not (interactive() and input("Delete them all? [y/N] ").strip().lower().startswith("y")):
                say("Cancelled: nothing deleted.")
                return 1
    elif not days:
        say("Retention is off (quarantine_days = 0): nothing expires. Use --all to delete everything.")
        return 0
    purged = skillscan.purge_expired(apply=not DRY, days=days, everything=everything)
    if not purged:
        say("Nothing expired.")
    return 0


def show_help(inv):
    prog = manual.DOC_PROG if inv.kind == "markdown" else program_name()
    if inv.kind == "markdown":
        sys.stdout.flush()  # bytes, so the file has LF endings on every platform
        sys.stdout.buffer.write(manual.render_markdown().encode("utf-8"))
        sys.stdout.buffer.flush()
    elif inv.kind == "overview":
        say(manual.render_overview(prog))
    else:
        say(manual.render_help(inv.key, prog))
    return 0


def usage_error(exc, prog):
    print(f"{prog}: {exc}", file=sys.stderr)
    if exc.hint:
        print("  " + exc.hint.replace("\n", "\n  "), file=sys.stderr)
    return 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    prog = program_name()
    if argv[:1] == ["route"]:  # before parsing: the prompt text is free-form
        return run_route(argv[1:])
    legacy = prog != "trirouter"
    try:
        inv = cliparse.resolve(argv, prog, legacy)
    except cliparse.UsageError as exc:
        sys.exit(usage_error(exc, prog))
    if inv.kind in ("help", "overview", "markdown"):
        return show_help(inv)
    if inv.kind == "version":
        say(f"trirouter {__version__}")
        return 0
    configure(inv)
    if (code := prepare_state(inv.key)) is not None:
        return code
    commands = {"setup": run_setup, "doctor": doctor.main, "skills": run_skills, "models": probe_models,
                "detect": lambda: print_report(P.detect()), "remote": run_remote, "uninstall": run_uninstall}
    if inv.key.startswith("quarantine"):
        return run_quarantine(inv.key)
    return commands[inv.key]() or 0
