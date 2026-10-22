"""trirouter command line (`trirouter <command>`, `python install.py`, `python -m trirouter`; the last two run setup
without a command). Commands and help texts live in manual.py, flag validation in cliparse.py."""
import difflib
import json
import os
import sys
from pathlib import Path

from . import (__version__, cliparse, completion, core, discovery, doctor, hooks, hub, integrations, interrupt, legacy,
               manual, platforms as P, remote, secret, skillscan, sync)

STATE = legacy.state_dir(P.HOME)  # the old ~/.jev-router until migrated (prepare_state)
CONFIG = STATE / "config.json"
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


# Commands that write per-user state or rewrite the tools' configs: they migrate a pre-rename ~/.jev-router first, so
# nothing is ever written to a half-migrated pair of folders.
STATE_WRITERS = ("setup", "keys", "skills", "models", "remote", "uninstall", "quarantine restore", "quarantine purge")


def say(msg=""):
    print(msg, flush=True)


def interactive():
    """stdin is a real console, not a pipe, a file or NUL."""
    return P.is_terminal(sys.stdin)


ask_line = interrupt.ask_line  # input() that steps the Ctrl+D key watcher aside; Ctrl+D / Ctrl+C end the command


def ask(question, default="y"):
    """Without a terminal or --yes every answer is "no": an unattended run never moves files unasked."""
    if YES:
        return default.lower().startswith("y")
    if not interactive():
        return False
    ans = ask_line(f"{question} [{'Y/n' if default.lower().startswith('y') else 'y/N'}] ").strip().lower()
    return (ans or default).startswith("y")


def load_config():
    return P.read_json(CONFIG, {})


def save_config(cfg):
    if DRY:
        return
    # atomic and owner-only from the start; the JEV token is stored protected (secret.py), never as plain text
    # where the system can encrypt it
    P.atomic_write(CONFIG, json.dumps(secret.protect_config(cfg), indent=2), mode=0o600)


def rebase_state(state):
    """Points every module that captured the state folder at import time to `state` (after a migration)."""
    global STATE, CONFIG
    STATE, CONFIG = state, state / "config.json"
    core.STATE_DIR = state
    doctor.STATE = state
    remote.STATE, remote.BIN, remote.CONFIG = state, state / "bin", state / "config.json"
    remote.LOG = state / "logs" / "claude-remote.log"
    skillscan.STATE, skillscan.CONFIG = state, state / "config.json"
    skillscan.QUARANTINE, skillscan.CACHE = state / "quarantine", state / "state" / "skillscan.json"


def repoint_install(apply):
    """After the state folder moved: rewrites the hooks, MCP entries, PATH entry and remote-access services that point
    into the old bin folder (old entries are replaced, never duplicated)."""
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
            if ask_line("  Press Enter when done (s = skip this tool): ").strip().lower() == "s":
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
    return core.JEV_LABELS.get(core.jev_access(cfg)[0]) or \
        "built-in local model (add JEV later: pipe a TypeSafe token or OpenRouter key into `trirouter setup --jev-token-stdin`)"


def configure_jev(cfg):
    say("\n== 2/5  JEV / TypeSafe (the routing decision engine)")
    token = FLAGS.get("--openrouter-key") or FLAGS.get("--jev-token")
    if token:
        say("  Note: a key on the command line shows in process listings and shell history; prefer --jev-token-stdin.")
    elif "--jev-token-stdin" in FLAGS:
        token = read_stdin().partition("\n")[0].strip()
        if not token:
            say("  --jev-token-stdin: no key on standard input.")
    if token:
        store_jev_key(cfg, token)
    elif os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_OPENROUTER_API_KEY"):
        say("  Using the JEV key from the environment.")
    elif secret.reveal(cfg.get("typesafe_api_key")) or secret.reveal(cfg.get("openrouter_api_key")):
        say("  A JEV key is already configured.")
    elif secret.UNREADABLE in (secret.status(cfg.get(k)) for k in secret.KEY_FIELDS):
        say("  The stored JEV key cannot be decrypted on this account (copied from another machine or user?): "
            "give it again with --jev-token-stdin.")
    elif not YES and interactive():
        token = interrupt.ask_secret("  TypeSafe token or OpenRouter key (input hidden; Enter = built-in local model): ").strip()
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
    link_settings(cfg, persist)
    hub.SCAN = {"llm": bool(sc.get("llm")), "allow": tuple(sc.get("allow") or ()),
                "accept_flagged": "--accept-flagged" in FLAGS,  # one-off, never saved
                "quarantine_days": sc.get("quarantine_days", skillscan.DEFAULT_DAYS)}
    hub.CHOOSE = choose_quarantine


def link_settings(cfg, persist):
    """config.json skills.catalog_only plus --catalog-only (`none` clears it); saved only when applying."""
    sk = dict(cfg.get("skills") or {})
    value = FLAGS.get("--catalog-only")
    if isinstance(value, str):
        sk["catalog_only"] = [] if value.strip().lower() == "none" else hub.parse_catalog_only(value)
        names = hub.catalog_only_matches(sk["catalog_only"])
        say(f"  Catalog-only: {len(names)} hub skill(s) match {', '.join(sk['catalog_only']) or '(no pattern)'}"
            + (" - not linked into Claude/Codex, served through the catalog" if names else ""))
    if persist and sk != (cfg.get("skills") or {}):
        cfg["skills"] = sk
        save_config(cfg)
    hub.LINK = {"catalog_only": tuple(hub.parse_catalog_only(sk.get("catalog_only") or []))}


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
        ans = ask_line("Quarantine these skills? [a]ll / [n]one / [s]elect (default: none) ").strip().lower()
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
        if ask_line("Go ahead? [y/N] ").strip().lower().startswith("y"):
            return chosen


def select_numbers(count):
    """Asks for numbers like 1,3,5-8 until they are valid; the zero-based indexes, or None when the answer is empty."""
    while True:
        text = ask_line("Numbers to quarantine (e.g. 1,3,5-8; Enter = none): ").strip()
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
    setup_completion()
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
    return ask_line(f"  Name shown on your other devices [{default}]: ").strip() or default


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
        with interrupt.question():
            P.call(["winget", "install", "--id", "cjpais.Handy", "-e", "--accept-source-agreements",
                    "--accept-package-agreements"])


def remote_workdir(cfg):
    """Claude Code only serves trusted folders, and never the home directory."""
    wd = FLAGS.get("--workdir")
    return str(Path(wd).resolve()) if isinstance(wd, str) else (cfg.get("remote_workdir") or str(P.REPO))


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
    if "--no-completion" not in FLAGS:
        say("  * Tab completion is installed: in a new terminal type `trirouter ` and press Tab to complete commands, "
            "flags and values (`trirouter uninstall` removes it).")
    say(f"  * New and retired models are picked up once a day by themselves; `{P.command_hint('models')}` checks "
        "now which models your accounts can use.")
    say(f"  * Health check any time: {P.command_hint('doctor')}")


ON_WORDS = ("on", "1", "true", "yes")


def set_model_auto(value):
    """`models --auto=on|off`, remembered as config.json model_discovery.auto."""
    cfg = load_config()
    on = value.strip().lower() in ON_WORDS
    cfg["model_discovery"] = dict(cfg.get("model_discovery") or {}, auto=on)
    save_config(cfg)
    say(f"Daily model check: {'on' if on else 'off'}" + (" (dry run: not saved)" if DRY else " (saved in config.json)"))


def set_claude_models(main, sub):
    """`models --main / --subagent-model`: Claude Code's default model for new sessions (settings.json `model`) and for
    subagents that name none (env CLAUDE_CODE_SUBAGENT_MODEL); remembered and re-applied by setup, "off" removes it."""
    try:
        w = integrations.Writer(not DRY)
        integrations.claude_model_settings(w, main, sub)
    except (ValueError, integrations.BadConfig) as exc:
        say(f"[!!]  {exc}")
        return 1
    if not DRY:
        cfg = load_config()
        claude = dict(cfg.get("claude") or {})
        for key, value in (("main_model", main), ("subagent_model", sub)):
            if value is not None:
                claude[key] = value
        cfg["claude"] = claude
        save_config(cfg)
    if main and main != "off":
        say(f"New Claude Code sessions start on {main}; the router hands harder work to stronger workers. "
            "A running session keeps its model until /model.")
    return 0


def run_models():
    """The model check (discovery.py): --discover is the cheap daily one, without it every Codex model is tried."""
    main, sub = (FLAGS.get(f) if isinstance(FLAGS.get(f), str) else None for f in ("--main", "--subagent-model"))
    if main or sub:
        code = set_claude_models(main and main.strip().lower(), sub and sub.strip().lower())
        if code or ("--discover" not in FLAGS and not isinstance(FLAGS.get("--auto"), str)):
            return code
    auto = FLAGS.get("--auto")
    if isinstance(auto, str):
        set_model_auto(auto)
        if "--discover" not in FLAGS:
            return 0
    mode = "discover" if "--discover" in FLAGS else "probe"
    say(("Daily model check" if mode == "discover" else "Full model check (every Codex model gets a one-word prompt, "
         "about 10-60 s each)") + (" - dry run, nothing is saved" if DRY else ""))
    if not DRY and not discovery.acquire_lock():
        say("Another model check is running (perhaps the daily one in the background); try again in a few minutes.")
        return 1
    try:
        outcome = discovery.run(mode, apply=not DRY, say=say)
    finally:
        if not DRY:
            discovery.release_lock()
    say("\nResult:")
    for line in discovery.summary_lines(outcome["results"]):
        say(f"  {line}")
    if outcome["changed"]:
        say(f"{'Would save' if DRY else 'Saved'} to {discovery.local_file()}")
    else:
        say("Nothing to change.")
    return 0


def run_daemon(key):
    from . import daemon
    sub = key.split()[1] if " " in key else "status"
    if sub in ("enable", "disable"):
        getattr(daemon, sub)(apply=not DRY, say=say)
        return 0
    if sub == "start":
        ok = daemon.start()
        say(f"hook daemon: {'running' if ok else 'did not start'} (127.0.0.1:{daemon.configured_port()})"
            + ("" if ok else f" - see {daemon.log_file()}"))
        return 0 if ok else 1
    if sub == "stop":
        ok = daemon.stop()
        say("hook daemon: stopped" if ok else "hook daemon: still running")
        return 0 if ok else 1
    st = daemon.status()
    say(f"transport: {st['transport']}")
    if st["running"]:
        say(f"daemon:    running, pid {st['pid']}, 127.0.0.1:{st['port']}, answered in {st['latency_ms']} ms, "
            f"since {st['started']}")
    else:
        say(f"daemon:    not running (127.0.0.1:{st['port']})")
    say(f"token:     {st['token']}")
    return 0


def run_learn():
    """`learn`: trains the distilled routing model (distill.py) and prints its cross-validation report."""
    from . import distill
    rep = distill.train(apply=not DRY)
    for line in distill.report_lines(rep):
        say(line)
    if DRY:
        say("Dry run: nothing saved.")
    return 0


def setup_completion():
    """Setup step: tab completion for the current shell (and PowerShell on Windows), unless --no-completion.
    Scripts installed earlier are refreshed either way, so they always match the commands."""
    if "--no-completion" in FLAGS:
        shells = completion.installed()
        if not shells:
            say("  Tab completion: skipped (--no-completion)")
            return
    else:
        shells = completion.setup_shells()
    say(f"  Tab completion ({', '.join(shells)}):")
    completion.install(shells, apply=not DRY, say=lambda m: say("  " + m))


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
    out.update(model=d.get("route_model"), effort=d.get("route_effort") or d.get("effort"), agent=d.get("target_agent"), tier=d["primary"],
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
    if not DRY:  # setup also migrates: plaintext keys in older backups of config.json are removed
        for p, _fields in secret.scrub_backups(CONFIG, apply=True):
            say(f"  Removed the plaintext key from {p.name}.")
    connect(providers, cfg)
    extras(providers, load_config() if not DRY else cfg)
    next_steps(providers)
    return 0


def run_keys():
    """`keys`: protects the plaintext API keys in config.json (preview by default) and keeps no plaintext copy."""
    apply = "--apply" in FLAGS and not DRY
    say("trirouter keys" + ("" if apply else " (preview - nothing is changed; --apply writes)"))
    cfg = load_config()
    for field in secret.KEY_FIELDS:
        state = secret.status(cfg.get(field))
        if state != secret.NONE:
            say(f"  {field}: {state}")
    results = secret.migrate(CONFIG, apply)
    report([("keys", ok, msg) for ok, msg in results], indent="  ")
    return 0 if all(ok for ok, _ in results) else 1


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
    start = FLAGS.get("--start-session")
    start = start.strip().lower() in ON_WORDS if isinstance(start, str) else None  # None: the saved choice
    report(remote.setup(name, providers, apply=not DRY, workdir=remote_workdir(cfg), start_session=start))


def run_uninstall():
    integrations.install(ALL, apply=not DRY, uninstall=True)
    completion.remove(apply=not DRY, say=say)
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
            if not (interactive() and ask_line("Delete them all? [y/N] ").strip().lower().startswith("y")):
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
    """Runs a command; Ctrl+C / Ctrl+D end it with exit status 130 (see interrupt.run_guarded)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    return interrupt.run_guarded(_main, argv)


def _main(argv):
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
    commands = {"setup": run_setup, "keys": run_keys, "doctor": doctor.main, "skills": run_skills, "models": run_models,
                "remote": run_remote, "uninstall": run_uninstall, "learn": run_learn,
                "sync": lambda: sync.main(FLAGS, say)}
    if inv.key.startswith("quarantine"):
        return run_quarantine(inv.key)
    if inv.key.startswith("daemon"):
        return run_daemon(inv.key)
    return commands[inv.key]() or 0
