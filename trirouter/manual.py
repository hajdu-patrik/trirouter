"""The command-line manual as data: one source for cliparse, the help pages and docs/cli.md. After editing run
`trirouter sync` (a test fails when docs/cli.md is stale); `{prog}` is the program name."""
import textwrap
from dataclasses import dataclass

from . import platforms as P

WIDTH = 78
DOC_PROG = "trirouter"
# the Claude families a default model may name (models.json claude.allowed_families), for --main / --subagent-model
FAMILIES = tuple(P.read_json(P.PKG / "config" / "models.json", {}).get("claude", {}).get("allowed_families") or ())


@dataclass(frozen=True)
class Flag:
    long: str
    short: str = ""
    value: str = ""          # metavar; "" for a boolean flag
    optional: bool = False   # the value may be left out (--remote[=NAME])
    default: str = "off"
    remembered: str = "no"   # "no", or where it is kept in config.json
    doc: str = ""
    kind: str = "str"        # "str" | "int" (>= 0)
    choices: tuple = ()      # allowed values, case-insensitive; empty: anything
    choices_hint: str = ""
    items: tuple = ()        # the names a comma-separated value is made of (completion only)


@dataclass(frozen=True)
class Command:
    key: str                 # "setup", "quarantine restore"
    summary: str
    synopsis: tuple
    description: str         # paragraphs separated by a blank line
    flags: tuple
    changes: str
    examples: tuple          # ((command line, comment), ...)
    see_also: tuple
    exit_status: str = ""
    positional: str = ""     # what the non-flag arguments are (documentation only)


HELP_FLAG = Flag("--help", "-h", doc="Show this help page and exit.", default="-", remembered="no")

YES = Flag("--yes", "-y", doc="Never prompt: every question gets its default answer (skills are moved into the hub, "
           "nothing is quarantined or deleted).")
DRY = Flag("--dry-run", "-n", doc="Show what would change and change nothing, config.json included.")
APPLY = Flag("--apply", "-a", doc="Write the changes. Without it the command only previews them.")
ALLOW = Flag("--allow-skill", value="NAME,...", default="none", remembered="yes (skillscan.allow)",
             doc="Never ask about these skills and restore them from quarantine. Names are added to the saved list.")
SCAN_LLM = Flag("--scan-llm", value="on|off", default="off", remembered="yes (skillscan.llm)",
                choices=("on", "off", "1", "0", "true", "false", "yes", "no"), choices_hint="on or off",
                doc="Add SkillSpector's LLM analysis; skill content then leaves the machine through its provider.")
ACCEPT = Flag("--accept-flagged", remembered="no (this run only)",
              doc="Keep every skill rated DO_NOT_INSTALL without asking (like answering \"none\"); asked again when "
                  "a skill changes.")
CATALOG_ONLY = Flag("--catalog-only", value="PATTERN,...", default="none", remembered="yes (skills.catalog_only)",
                    doc="Hub skills whose folder name matches these fnmatch patterns (e.g. gke-*,google-*) stay in "
                        "~/.skills and the catalog but are not linked into Claude Code or Codex, so they do not load "
                        "into every session; the router still names them. Replaces the saved list; `none` clears it.")
QDAYS = Flag("--quarantine-days", value="N", default="3", remembered="yes (skillscan.quarantine_days)", kind="int",
             doc="Days a quarantined skill is kept before it is deleted for good. 0 = never delete.")
KEY_ARG_DEPRECATED = "Deprecated (visible in process listings and shell history): use --jev-token-stdin or the environment."

COMMANDS = {}


def _add(**kw):
    c = Command(**kw)
    COMMANDS[c.key] = c


EXIT_STD = ("0 on success. 2 on a usage error (unknown or misused option, missing argument). "
            "1 when the command ran but failed.")

_add(key="setup", summary="Detect the AI tools, connect hooks and MCP, link and scan skills",
     synopsis=("{prog} setup [-y] [-n] [--providers=LIST] [--jev-token-stdin] [--remote[=NAME]] [options]",),
     description=(
         "Interactive setup: detect Claude Code, Codex and Antigravity; take an optional JEV / TypeSafe token or "
         "OpenRouter key (without one the built-in local classifier decides); connect hooks and the MCP server; "
         "install the `trirouter` command and its tab completion; move every tool's skills into ~/.skills, scan them "
         "with SkillSpector when it is installed and link them into every tool; generate the worker agents; optionally "
         "set up remote access. Every change is reported, a changed config file keeps a .bak, re-running is safe and "
         "reports only what changed; `python install.py` with no arguments does the same. Without a terminal it "
         "needs --yes or --jev-token-stdin, so a stray run changes nothing.\n\n"
         "The JEV key is stored protected (Windows DPAPI; owner-only file elsewhere); a plaintext key from an older "
         "version is re-saved protected and removed from old backups of config.json."),
     flags=(YES, DRY,
            Flag("--providers", value="LIST", default="every installed, logged-in tool", items=P.PROVIDER_NAMES,
                 doc="Comma-separated tools to connect: claude, codex, antigravity."),
            Flag("--jev-token-stdin", remembered="yes (typesafe_api_key or openrouter_api_key)",
                 doc="Read a TypeSafe token or an OpenRouter key (sk-or-...) from the first line of standard input. "
                     "TYPESAFE_API_KEY / JEV_OPENROUTER_API_KEY win over the saved key."),
            Flag("--jev-token", value="TOKEN", default="none", remembered="yes (typesafe_api_key or openrouter_api_key)",
                 doc=KEY_ARG_DEPRECATED),
            Flag("--openrouter-key", value="KEY", default="none", remembered="yes (openrouter_api_key)",
                 doc=KEY_ARG_DEPRECATED + " Same as --jev-token=sk-or-..."),
            Flag("--remote", value="NAME", optional=True, default="ask", remembered="yes (remote_name)",
                 doc="Also set up remote access, under NAME (default: the saved name or the hostname)."),
            Flag("--workdir", value="DIR", default="the checkout", remembered="yes (remote_workdir)",
                 doc="Folder remote Claude sessions start in. Only used together with remote access."),
            Flag("--no-migrate", doc="Do not move the skills of other tools into ~/.skills."),
            Flag("--no-completion", doc="Do not install tab completion into your shell profile."),
            ALLOW, SCAN_LLM, ACCEPT, QDAYS, CATALOG_ONLY),
     changes="Changes files; preview with --dry-run",
     examples=(("{prog} setup", "interactive setup (python install.py alone does the same)"),
               ("{prog} setup --dry-run", "show every change, write nothing"),
               ("{prog} setup --yes --providers=claude,codex", "unattended, only these two tools"),
               ("cat key.txt | {prog} setup --jev-token-stdin", "add a TypeSafe token or OpenRouter key from a file and keep the rest"),
               ("{prog} setup --yes --remote=\"My PC\"", "also set up remote access")),
     exit_status="0 on success. 2 on a usage error. 1 when no logged-in tool was found, a step failed, or no terminal "
                 "and no --yes.",
     see_also=("models", "skills", "remote", "doctor", "uninstall"))


_add(key="models", summary="Find new models, drop retired ones, test which ones your accounts can use",
     synopsis=("{prog} models [-n]", "{prog} models --discover [-n]", "{prog} models --auto=on|off",
               "{prog} models --main=FAMILY|off [--subagent-model=FAMILY|off] [-n]"),
     description=(
         "Compares the models each tool offers today (`codex debug models`, `agy models`, the aliases `claude --help` "
         "names) with the ones the router knows. A new model is tried with a one-word prompt and becomes selectable "
         "when it answers; a Codex or Antigravity model no longer offered leaves the selection. Results go to "
         "~/.trirouter/models.local.json and the worker agents are regenerated. A check that cannot run changes "
         "nothing.\n\n"
         "Plain `models` also tries every known Codex model (10-60 s each). --discover is the cheap check that also "
         "runs by itself once a day in the background (`--auto=off` stops it); `{prog} doctor` shows the last one.\n\n"
         "--main and --subagent-model set Claude Code's default session model (~/.claude/settings.json) and the model "
         "of subagents that name none (CLAUDE_CODE_SUBAGENT_MODEL). Most prompts are answered in the session itself, "
         "so a sonnet session with the router escalating hard work costs far less than an opus session. Both are "
         "remembered and re-applied by setup; `off` removes them."),
     flags=(Flag("--discover", doc="Only the daily check: try new models (and ones offered again)."),
            Flag("--auto", value="on|off", default="on", remembered="yes (model_discovery.auto)",
                 choices=("on", "off", "1", "0", "true", "false", "yes", "no"), choices_hint="on or off",
                 doc="Switch the automatic daily check on or off. Given alone, only the setting is saved."),
            Flag("--main", value="FAMILY|off", default="unchanged", remembered="yes (claude.main_model)",
                 choices=FAMILIES + ("off",), choices_hint=", ".join(FAMILIES) + " or off",
                 doc="Claude Code's default model for new sessions; off removes it."),
            Flag("--subagent-model", value="FAMILY|off", default="unchanged", remembered="yes (claude.subagent_model)",
                 choices=FAMILIES + ("off",), choices_hint=", ".join(FAMILIES) + " or off",
                 doc="The model of Claude subagents that name none (general-purpose, plugin agents); off removes it."),
            DRY),
     changes="Changes files (models.local.json, worker agents, ~/.claude/settings.json); preview with --dry-run",
     examples=(("{prog} models --discover", "the daily check now"),
               ("{prog} models --discover --dry-run", "show what it would add or remove, save nothing"),
               ("{prog} models", "full check: also try every known Codex model"),
               ("{prog} models --auto=off", "no automatic daily check"),
               ("{prog} models --main=sonnet --subagent-model=sonnet", "cheap sessions; the router escalates")),
     exit_status="0 on success. 2 on a usage error. 1 when another model check is already running.",
     see_also=("skills", "doctor"))

_add(key="skills", summary="Scan and re-link skills, regenerate worker agents, rebuild the catalog",
     synopsis=("{prog} skills [-a] [-y] [--allow-skill=NAME,...] [--scan-llm=on|off] [--accept-flagged] "
               "[--quarantine-days=N] [--catalog-only=PATTERN,...]",),
     description=(
         "Scans every third-party skill in ~/.skills with SkillSpector (when installed), links the skills into Claude "
         "Code and Codex, registers the folder with Antigravity, regenerates the worker agents and rebuilds the skill "
         "catalog; expired quarantine entries are purged.\n\n"
         "After all scans the skills rated DO_NOT_INSTALL are listed once and you choose which go to quarantine "
         "([a]ll / [n]one / [s]elect, default none); a skill you keep is remembered for its content. With --yes or "
         "without a terminal nothing is quarantined, only a warning is printed."),
     flags=(APPLY, YES, DRY, ALLOW, SCAN_LLM, ACCEPT, QDAYS, CATALOG_ONLY),
     changes="Preview by default; --apply writes",
     examples=(("{prog} skills", "preview what would be linked, quarantined or purged"),
               ("{prog} skills --apply", "scan, ask the quarantine question once, link"),
               ("{prog} skills --apply --accept-flagged", "keep every flagged skill after a review"),
               ("{prog} skills --apply --allow-skill=docx,xlsx", "trust two skills for good"),
               ("{prog} skills --apply --scan-llm=on", "add the LLM analysis to the scan"),
               ("{prog} skills --apply --catalog-only=gke-*,google-*", "keep rarely used skills out of every session")),
     see_also=("quarantine", "setup", "doctor"))

_add(key="quarantine", summary="List, restore or purge skills the scan moved to quarantine",
     synopsis=("{prog} quarantine [list]", "{prog} quarantine restore NAME... [-n]",
               "{prog} quarantine purge [--all] [-y] [-n] [--quarantine-days=N]"),
     description=(
         "A quarantined skill (~/.trirouter/quarantine) is linked nowhere and left out of the catalog; it is deleted "
         "for good after the retention period (default 3 days, skillscan.quarantine_days; 0 = never) by `setup`, "
         "`skills --apply` and a Claude Code session start. Subcommands: list (default), restore, purge."),
     flags=(),
     changes="Changes nothing by itself; see the subcommands",
     examples=(("{prog} quarantine", "list the quarantined skills"),
               ("{prog} quarantine restore docx", "bring a skill back"),
               ("{prog} quarantine purge", "delete the expired ones now")),
     see_also=("quarantine list", "quarantine restore", "quarantine purge", "skills"))

_add(key="quarantine list", summary="Show quarantined skills and when they will be deleted",
     synopsis=("{prog} quarantine [list]",),
     description="One line per quarantined skill: name, quarantine date, the date it is purged (and the time left) "
                 "and the scan's risk score. The purge date uses the current retention setting.",
     flags=(),
     changes="Changes nothing",
     examples=(("{prog} quarantine", "same as `quarantine list`"),),
     see_also=("quarantine restore", "quarantine purge"))

_add(key="quarantine restore", summary="Move a quarantined skill back into ~/.skills",
     synopsis=("{prog} quarantine restore NAME... [-n]",),
     description="Moves the newest quarantined copy of each NAME back to ~/.skills/NAME, provided that name is free, and "
                 "adds it to skillscan.allow like --allow-skill does, so it is not flagged again. It is linked into the "
                 "tools by the next `{prog} skills --apply`.",
     flags=(DRY,), positional="NAME... one or more quarantined skill names (see `quarantine list`)",
     changes="Changes files; preview with --dry-run",
     examples=(("{prog} quarantine restore docx", "restore one skill"),
               ("{prog} quarantine restore docx xlsx -n", "preview restoring two"),
               ("{prog} skills --apply", "then re-link")),
     exit_status="0 on success. 2 on a usage error. 1 when a name was not found or the hub already has that name.",
     see_also=("quarantine list", "skills"))

_add(key="quarantine purge", summary="Delete expired quarantine entries now",
     synopsis=("{prog} quarantine purge [--all] [-y] [-n] [--quarantine-days=N]",),
     description="Permanently deletes the quarantined skills whose retention has run out (folder and metadata). "
                 "With --all it deletes every entry, expired or not, after a confirmation. Only entries directly inside "
                 "~/.trirouter/quarantine are ever deleted; links are never followed.",
     flags=(Flag("--all", doc="Delete every quarantined skill, not only the expired ones. Asks first."),
            Flag("--yes", "-y", doc="Do not ask before --all deletes every entry."), DRY, QDAYS),
     changes="Changes files (deletes for good); preview with --dry-run",
     examples=(("{prog} quarantine purge", "delete the expired entries"),
               ("{prog} quarantine purge --dry-run", "show which ones"),
               ("{prog} quarantine purge --all", "delete everything, after a yes"),
               ("{prog} quarantine purge --quarantine-days=7", "keep entries a week from now on")),
     exit_status="0 on success. 2 on a usage error. 1 when --all was not confirmed (nothing deleted).",
     see_also=("quarantine list", "skills"))

_add(key="remote", summary="Set up or remove phone / other-device access",
     synopsis=("{prog} remote [--name=NAME] [--workdir=DIR] [--start-session=on|off] [-n]", "{prog} remote --remove [-n]"),
     description="Starts each tool's own remote service at logon (Claude Code Remote Control, Antigravity, Codex) under "
                 "one machine name. Only the connection opens; sessions start from the Claude app, claude.ai/code, the "
                 "ChatGPT app or antigravity.google.com. See docs/remote-access.md.",
     flags=(Flag("--name", value="NAME", default="saved name or hostname", remembered="yes (remote_name)",
                 doc="The machine name shown on your other devices."),
            Flag("--workdir", value="DIR", default="saved folder or the checkout", remembered="yes (remote_workdir)",
                 doc="Folder remote Claude sessions start in; it must be a trusted Claude Code folder."),
            Flag("--start-session", value="on|off", default="off", remembered="yes (remote_start_session)",
                 choices=("on", "off", "1", "0", "true", "false", "yes", "no"), choices_hint="on or off",
                 doc="Also pre-create one Claude session at every logon (on) or only keep the connection (off)."),
            Flag("--remove", doc="Remove the remote-access services instead of creating them."), DRY),
     changes="Changes files and system services; preview with --dry-run",
     examples=(("{prog} remote --name \"My PC\"", "set up under a name"),
               ("{prog} remote --name \"My PC\" --workdir ~/code", "choose the working folder"),
               ("{prog} remote --start-session=on", "also pre-create a Claude session at logon"),
               ("{prog} remote --remove", "undo")),
     see_also=("setup", "doctor"))

_add(key="sync", summary="Bring everything generated from the sources up to date (after a pull or a change)",
     synopsis=("{prog} sync [--check]", "{prog} sync --pre-commit"),
     description=(
         "Run after a pull or a change: refreshes the installed tab-completion scripts (profiles untouched), the "
         "worker agents of the tools that have generated ones and the skill catalog, and in a trirouter checkout "
         "regenerates docs/cli.md and sets git's core.hooksPath to .githooks (never over your own hooks). The daily "
         "background check refreshes the user-side items by itself.\n\n"
         "The pre-commit hook runs `{prog} sync --pre-commit`: the sync, `git add docs/cli.md`, a CHANGELOG reminder, "
         "the tests and, when routing code is staged, the evaluation. `git commit --no-verify` skips it once."),
     flags=(Flag("--check", doc="Change nothing; exit 1 when something is out of date (for CI)."),
            Flag("--pre-commit", doc="What the git pre-commit hook runs: sync, stage docs/cli.md, tests, evaluation. "
                                     "Only in a trirouter checkout.")),
     changes="Changes files; preview with --check",
     examples=(("{prog} sync", "after a pull or a change"), ("{prog} sync --check", "is anything out of date?"),
               ("{prog} sync --pre-commit", "the checks the git hook runs, by hand")),
     exit_status="0 on success. 2 on a usage error. 1 when --check found something out of date, an item failed, "
                 "or a --pre-commit check failed.",
     see_also=("setup", "skills", "doctor"))

_add(key="daemon", summary="The local hook daemon: Claude Code hooks without a Python start per prompt",
     synopsis=("{prog} daemon [status]", "{prog} daemon enable|disable [-n]", "{prog} daemon start|stop"),
     description=(
         "By default every Claude Code hook starts a Python process (0.3-0.5 s on Windows). With the daemon, Claude Code "
         "sends its hooks as HTTP requests to a token-protected trirouter server on 127.0.0.1 (config.json daemon.port, "
         "default 47621) that keeps the router and the JEV connection loaded: tens of milliseconds per hook. "
         "SessionStart stays a command hook and starts the daemon; if the daemon is down, Claude Code goes on without "
         "router context. Codex and Antigravity keep command hooks. Subcommands: status (default), enable, disable, "
         "start, stop."),
     flags=(), changes="Changes nothing by itself; see the subcommands",
     examples=(("{prog} daemon", "is it enabled and running?"), ("{prog} daemon enable", "switch Claude's hooks to it"),
               ("{prog} daemon disable", "back to command hooks")),
     see_also=("daemon enable", "doctor"))

_add(key="daemon status", summary="Show whether the hook daemon is enabled and answering",
     synopsis=("{prog} daemon [status]",),
     description="Prints the hook transport (command or http), whether the daemon answers on its port, its process id, "
                 "start time and answer time, and whether Claude's settings.json carries the daemon's token.",
     flags=(), changes="Changes nothing", examples=(("{prog} daemon status", "the report"),),
     see_also=("daemon enable", "daemon start"))

_add(key="daemon enable", summary="Send Claude Code's hooks to the daemon over HTTP",
     synopsis=("{prog} daemon enable [-n]",),
     description="Sets hooks.transport = http in config.json, starts the daemon and rewrites trirouter's Claude Code "
                 "hooks in ~/.claude/settings.json as http hooks (SessionStart stays a command hook; a .bak of the "
                 "original is kept). Takes effect in new and running sessions after Claude Code re-reads its settings.",
     flags=(DRY,), changes="Changes files; preview with --dry-run",
     examples=(("{prog} daemon enable", "switch"), ("{prog} daemon enable -n", "preview")),
     see_also=("daemon disable", "daemon status"))

_add(key="daemon disable", summary="Back to command hooks; stop the daemon",
     synopsis=("{prog} daemon disable [-n]",),
     description="Sets hooks.transport = command, rewrites trirouter's Claude Code hooks as command hooks and stops "
                 "the daemon.",
     flags=(DRY,), changes="Changes files; preview with --dry-run",
     examples=(("{prog} daemon disable", "switch back"),),
     see_also=("daemon enable",))

_add(key="daemon start", summary="Start the hook daemon now",
     synopsis=("{prog} daemon start",),
     description="Starts the daemon in the background unless it already runs (the SessionStart hook does this by "
                 "itself when the transport is http).",
     flags=(), changes="Starts a background process", examples=(("{prog} daemon start", "start it"),),
     exit_status="0 when it answers. 1 when it did not start (see ~/.trirouter/logs/daemon.log).",
     see_also=("daemon stop", "daemon status"))

_add(key="daemon stop", summary="Stop the hook daemon",
     synopsis=("{prog} daemon stop",),
     description="Stops the running daemon. With the http transport still enabled the next Claude Code session start "
                 "starts it again; use `daemon disable` to switch it off for good.",
     flags=(), changes="Stops a background process", examples=(("{prog} daemon stop", "stop it"),),
     exit_status="0 when no daemon runs afterwards. 1 otherwise.",
     see_also=("daemon start", "daemon disable"))

_add(key="learn", summary="Train the local routing model on JEV's past decisions",
     synopsis=("{prog} learn [-n]",),
     description=(
         "Trains a small local classifier on JEV's decisions for your own prompts (~/.trirouter/logs/routing.jsonl) "
         "plus the repository's labelled prompts, saved to ~/.trirouter/state/distilled.json. It is enabled only when "
         "cross-validation shows it routes like JEV; confident prompts then skip the JEV call. Retrained by itself in "
         "the daily background check; ROUTER_DISTILLED=0 switches it off."),
     flags=(DRY,), changes="Writes ~/.trirouter/state/distilled.json; preview with --dry-run",
     examples=(("{prog} learn", "train, measure and save"), ("{prog} learn --dry-run", "only measure")),
     see_also=("route", "doctor"))

_add(key="doctor", summary="Read-only health report",
     synopsis=("{prog} doctor",),
     description="Checks the tools (installed, version, logged in), hooks, MCP entries, skill hub and agents, "
                 "configuration, interpreters, the `trirouter` command, the quarantine and recent router activity. "
                 "Problems are marked [!!] with the command that fixes them.",
     flags=(), changes="Changes nothing",
     examples=(("{prog} doctor", "print the report"),),
     exit_status="0 always: read the [!!] lines for problems. 2 on a usage error.",
     see_also=("setup", "skills"))

_add(key="route", summary="The routing decision for one prompt or sub-task",
     synopsis=("{prog} route [--provider=NAME] [--json] [--] TEXT...",),
     description="Runs the same pipeline as the hooks for one prompt, with no side effects (no queue state, no log). "
                 "With no TEXT the prompt is read from stdin. Without --json it prints the [router] instruction; "
                 "with --json one object: provider, model, effort, agent, tier, task, difficulty, extra_agents, destructive, "
                 "skill, verify, lang, backend, text, note (for Claude, model is always concrete). #norouter / #privat "
                 "prompts are not routed. `route` must be the first argument; everything after `--` is prompt text.",
     flags=(Flag("--provider", value="NAME", default="claude",
                 choices=("claude", "codex", "antigravity", "claude-chat"), choices_hint="claude, codex, antigravity or claude-chat",
                 doc="Whose worker names and models to answer with."),
            Flag("--json", doc="Print one JSON object instead of the instruction text.")),
     positional="TEXT... the prompt (free-form; quote it or use --)",
     changes="Changes nothing",
     examples=(("{prog} route \"add a pagination parameter to the quotes API\"", "the instruction text"),
               ("{prog} route --json --provider codex \"fix the flaky login test\"", "machine-readable"),
               ("echo \"refactor the parser\" | {prog} route --json", "prompt from stdin"),
               ("{prog} route -- --not-an-option", "text that starts with dashes")),
     exit_status="0 on success. 2 on a usage error (unknown option, empty prompt). 1 on an unexpected error.",
     see_also=("doctor",))

_add(key="uninstall", summary="Remove hooks, MCP entries, remote access and the trirouter command",
     synopsis=("{prog} uninstall [-n]",),
     description="Removes the router's hooks and MCP entries from Claude Code, Codex and Antigravity, the remote-access "
                 "services, the `trirouter` launcher with its PATH entry and the tab completion in your shell "
                 "profiles. Your skills stay in ~/.skills (and linked).",
     flags=(DRY,), changes="Changes files; preview with --dry-run",
     examples=(("{prog} uninstall --dry-run", "show what would be removed"), ("{prog} uninstall", "remove it")),
     see_also=("setup", "remote"))

_add(key="help", summary="Show the manual of a command",
     synopsis=("{prog} help [COMMAND [SUBCOMMAND]]",),
     description="Prints the man-page-like help of a command. `{prog} <command> --help` does the same; docs/cli.md "
                 "holds every page (regenerated by `{prog} sync`).",
     flags=(), positional="COMMAND a command name; SUBCOMMAND for quarantine and daemon",
     changes="Changes nothing",
     examples=(("{prog} help", "the command list"), ("{prog} help skills", "the skills page"),
               ("{prog} help quarantine purge", "a subcommand")),
     see_also=("doctor",))

ORDER = ("setup", "models", "skills", "quarantine", "remote", "sync", "daemon", "learn", "doctor", "route", "uninstall", "help")

# Flags of the bare program (no command): (long, short, help text)
TOP_FLAGS = (("--version", "-V", "Print the version and exit."), ("--help", "-h", "Show the command list and exit."))
# Commands and flags that no longer exist: what to say, with {prog} for the program name
REMOVED_COMMANDS = {
    "detect": "detect was removed: use `{prog} doctor` (it shows whether each tool is installed, its version and whether it is logged in)",
    "completion": "completion was removed: `{prog} setup` installs tab completion (skip it with --no-completion) and "
                  "`{prog} uninstall` removes it",
    "version": "version was removed: use `{prog} --version`",
    "keys": "keys was removed: `{prog} setup` stores the key protected and scrubs old backups; `{prog} doctor` reports "
            "the state",
}
REMOVED_FLAGS = {("models", "--probe"): "--probe was removed: plain `{prog} models` already does the full check",
                 ("help", "--markdown"): "--markdown was removed: `{prog} sync` regenerates docs/cli.md",
                 ("sync", "--dry-run"): "--dry-run was removed: use --check"}
SUBCOMMANDS = {"quarantine": ("list", "restore", "purge"), "daemon": ("status", "enable", "disable", "start", "stop")}
DEFAULT_SUBCOMMAND = {"quarantine": "list", "daemon": "status"}

GETTING_STARTED = """\
1. From the checkout (the `trirouter` command does not exist yet):

   ```bash
   python install.py --dry-run     # preview every change
   python install.py               # interactive setup
   ```

2. Setup writes the `trirouter` launcher to `~/.trirouter/bin` and puts it on your PATH (Windows: the user PATH;
   macOS / Linux: a link in `~/.local/bin`, with the profile line printed when that folder is not on PATH).
   **Reopen your terminal**, then `trirouter help` and `trirouter doctor`. Tab completion is installed for your shell.

If you move the checkout, run `python install.py` from its new place once. `python install.py <command>` and
`python -m trirouter <command>` always behave like `trirouter <command>`."""

CONVENTIONS = """\
* `trirouter <command> [subcommand] [flags]`; flags may come in any order after the command, `--flag=value` and
  `--flag value` are equivalent. An unknown or not-applicable flag is a usage error (exit 2) with a hint.
* `-h` / `--help` anywhere after a command shows its page. Short flags: `-y` --yes, `-n` --dry-run, `-a` --apply;
  boolean ones combine (`-yn`).
* Flags marked "remembered" are saved in `~/.trirouter/config.json` when the command writes.
* Ctrl+C (or Ctrl+D) stops a command at once: exit status 130, child processes ended, files written atomically.
* Exit status: 0 success, 2 usage error, 1 the command ran but failed, 130 interrupted."""

TASKS = (
    ("See what setup would do", "trirouter setup --dry-run"),
    ("Check that everything works", "trirouter doctor"),
    ("Re-scan skills after adding some to `~/.skills`", "trirouter skills --apply"),
    ("See, restore or purge quarantined skills", "trirouter quarantine\ntrirouter quarantine restore <name>"),
    ("Change the quarantine retention (0 = never delete)", "trirouter skills --apply --quarantine-days=7"),
    ("Get the routing decision for a sub-task from a script", "trirouter route --json \"add a CSV export\""),
    ("Check for new or retired models now", "trirouter models --discover"),
    ("Reach this computer from a phone", "trirouter remote --name \"My PC\""),
    ("Update completion, agents, the skill catalog and docs after a pull", "trirouter sync"),
    ("Remove everything the installer added", "trirouter uninstall"),
)


def all_flags(cmd):
    return cmd.flags + (HELP_FLAG,)


def _fmt(text, prog):
    return text.replace("{prog}", prog)


def _wrap(text, indent, width=WIDTH):
    out = []
    for para in text.split("\n\n"):
        out.append(textwrap.fill(" ".join(para.split()), width=width, initial_indent=indent, subsequent_indent=indent,
                                  break_on_hyphens=False))
    return "\n\n".join(out)


def flag_label(f):
    names = f"{f.short}, {f.long}" if f.short else f"    {f.long}"
    if f.value:
        names += f"[={f.value}]" if f.optional else f"={f.value}"
    return names


def flag_meta(f):
    if f.long == "--help":
        return ""
    return f"Default: {f.default}. Remembered in config.json: {f.remembered}."


def overview_lines(prog):
    rows = [(c, COMMANDS[c]) for c in ORDER]
    pad = max(len(c) for c, _ in rows) + 2
    return [f"  {c:<{pad}}{_fmt(cmd.summary, prog)}" for c, cmd in rows]


def render_overview(prog=None):
    prog = prog or DOC_PROG
    lines = [f"usage: {prog} <command> [subcommand] [flags]", "", "Commands:"]
    lines += overview_lines(prog)
    lines += ["", "Options:"]
    labels = [f"{short}, {long}" for long, short, _ in TOP_FLAGS]
    lines += [f"  {label:<{max(map(len, labels)) + 2}}{doc}" for label, (_, _, doc) in zip(labels, TOP_FLAGS)]
    lines += ["", f"Run `{prog} help <command>` for details, e.g. `{prog} help skills`.",
              "Full reference: docs/cli.md"]
    return "\n".join(lines)


def render_help(key, prog=None):
    prog = prog or DOC_PROG
    c = COMMANDS[key]
    out = ["NAME", textwrap.fill(f"{prog} {c.key} - {_fmt(c.summary, prog)}", width=WIDTH, initial_indent="    ",
                                 subsequent_indent="        "), "", "SYNOPSIS"]
    out += [textwrap.fill(_fmt(s, prog), width=WIDTH, initial_indent="    ", subsequent_indent="        ",
                          break_on_hyphens=False, break_long_words=False) for s in c.synopsis]
    out += ["", "DESCRIPTION", _wrap(_fmt(c.description, prog), "    ")]
    if c.positional:
        out += ["", _wrap(_fmt("Arguments: " + c.positional + ".", prog), "    ")]
    out += ["", "OPTIONS"]
    for f in all_flags(c):
        out.append(f"    {flag_label(f)}")
        out.append(_wrap(f.doc, "          "))
        if flag_meta(f):
            out.append(_wrap(flag_meta(f), "          "))
    out += ["", "CHANGES", f"    {c.changes}", "", "EXAMPLES"]
    for cmd, note in c.examples:
        out.append(f"    {_fmt(cmd, prog)}")
        if note:
            out.append(f"        {note}")
    out += ["", "EXIT STATUS", _wrap(c.exit_status or EXIT_STD, "    "), "", "SEE ALSO",
            "    " + ", ".join(f"{prog} help {k}" for k in c.see_also)]
    return "\n".join(out)


def anchor(key):
    return key.replace(" ", "-")


def _md_command(c, level):
    p = DOC_PROG
    out = [f"{'#' * level} {c.key}", "", f"{_fmt(c.summary, p)}.", "", "```", *(_fmt(s, p) for s in c.synopsis), "```", ""]
    out += [_fmt(c.description, p), ""]
    if c.positional:
        out += [f"Arguments: {_fmt(c.positional, p)}.", ""]
    if c.flags:
        out += ["| Flag | Value | Default | Remembered | Description |", "| --- | --- | --- | --- | --- |"]
        for f in c.flags:
            names = f"`{f.short}`, `{f.long}`" if f.short else f"`{f.long}`"
            value = (f"`{f.value}`" + (" (optional)" if f.optional else "")) if f.value else "-"
            out.append(f"| {names} | {value} | {f.default} | {f.remembered} | {f.doc.replace('|', '/')} |")
        out.append("")
    out += [f"**Changes:** {c.changes}.", "", "```bash"]
    for cmd, note in c.examples:
        out.append(f"{_fmt(cmd, p)}" + (f"    # {note}" if note else ""))
    out += ["```", ""]
    if c.exit_status:
        out += [f"**Exit status:** {c.exit_status}", ""]
    out += ["**See also:** " + ", ".join(f"[{k}](#{anchor(k)})" for k in c.see_also), ""]
    return out


def render_markdown():
    p = DOC_PROG
    out = ["# trirouter command reference", "",
           "<!-- generated by `trirouter sync` from trirouter/manual.py; do not edit by hand -->", "",
           "`trirouter <command> [subcommand] [flags]` -- the same pages are shown by `trirouter help <command>`; "
           "every command also takes `-h` / `--help`. Exit status unless a page says otherwise: " + EXIT_STD, "",
           "## Contents", "", "* [Getting started](#getting-started)", "* [Conventions](#conventions)",
           "* [Common tasks](#common-tasks)"]
    for k in ORDER:
        out.append(f"* [{k}](#{anchor(k)}) -- {_fmt(COMMANDS[k].summary, p)}")
        for sub in SUBCOMMANDS.get(k, ()):
            out.append(f"  * [{k} {sub}](#{anchor(k + ' ' + sub)})")
    out += ["", "## Getting started", "", GETTING_STARTED, "", "## Conventions", "", CONVENTIONS, "", "## Common tasks", ""]
    out += ["| I want to | Command |", "| --- | --- |"]
    for what, cmd in TASKS:
        out.append(f"| {what} | " + "<br>".join(f"`{line}`" for line in cmd.split("\n")) + " |")
    out.append("")
    for k in ORDER:
        out += _md_command(COMMANDS[k], 2)
        for sub in SUBCOMMANDS.get(k, ()):
            out += _md_command(COMMANDS[f"{k} {sub}"], 3)
    return "\n".join(out).rstrip("\n") + "\n"
