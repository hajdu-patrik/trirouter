"""The command-line manual: every command, flag and help text as data.

One source of truth: `cliparse` validates arguments against these definitions, `render_help` prints the
man-page-like pages (`trirouter help <command>`, `<command> --help`), `render_overview` the short
command list, and `render_markdown` the wiki-style reference in docs/cli.md (`trirouter help --markdown`).
Edit the texts here, then regenerate the file: `trirouter help --markdown > docs/cli.md`
(a test fails when docs/cli.md is out of date). `{prog}` in a text stands for the program name:
`trirouter`, or `python install.py` when run from the checkout.
"""
import textwrap
from dataclasses import dataclass

WIDTH = 78
DOC_PROG = "trirouter"


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

    @property
    def name(self):
        return self.key.split()[0]


HELP_FLAG = Flag("--help", "-h", doc="Show this help page and exit.", default="-", remembered="no")

YES = Flag("--yes", "-y", doc="Never prompt: answer every question with its default. Unattended runs move, "
           "quarantine or delete nothing that needs a decision.")
DRY = Flag("--dry-run", "-n", doc="Show what would change and change nothing, config.json included.")
APPLY = Flag("--apply", "-a", doc="Write the changes. Without it the command only previews them.")
ALLOW = Flag("--allow-skill", value="NAME,...", default="none", remembered="yes (skillscan.allow)",
             doc="Never ask about these skills and restore them from quarantine. Names are added to the saved list.")
SCAN_LLM = Flag("--scan-llm", value="on|off", default="off", remembered="yes (skillscan.llm)",
                choices=("on", "off", "1", "0", "true", "false", "yes", "no"), choices_hint="on or off",
                doc="Add SkillSpector's LLM analysis to the static scan. Skill content then leaves the machine "
                    "through SkillSpector's own provider settings.")
ACCEPT = Flag("--accept-flagged", remembered="no (this run only)",
              doc="Keep every skill now rated DO_NOT_INSTALL without asking, like answering \"none\" to the "
                  "quarantine question. Bound to the skill's current content: it is asked about again when it changes.")
QDAYS = Flag("--quarantine-days", value="N", default="3", remembered="yes (skillscan.quarantine_days)", kind="int",
             doc="Days a quarantined skill is kept before it is deleted for good. 0 = never delete.")

COMMANDS = {}


def _add(**kw):
    c = Command(**kw)
    COMMANDS[c.key] = c


EXIT_STD = ("0 on success. 2 on a usage error (unknown or misused option, missing argument). "
            "1 when the command ran but failed.")

_add(key="setup", summary="Detect the AI tools, connect hooks and MCP, link and scan skills",
     synopsis=("{prog} setup [-y] [-n] [--providers=LIST] [--jev-token=TOKEN] [--remote[=NAME]] [options]",),
     description=(
         "Interactive setup in five steps: (1) detect Claude Code, Codex and Antigravity and help you log in; "
         "(2) the JEV / TypeSafe token or an OpenRouter key (optional; without one the built-in local model decides); "
         "(3) hooks and the MCP server for every logged-in tool, the `trirouter` command itself and its tab "
         "completion in your shell profile (PowerShell, bash, zsh or fish; skip it with --no-completion); "
         "(4) the shared skill folder ~/.skills: existing skills are moved there, scanned with SkillSpector when it "
         "is installed, linked into every tool, and expired quarantine entries are purged; worker agents are "
         "generated; (5) optional remote access and speech-to-text.\n\n"
         "Every change is shown first, a changed config file keeps a .bak copy, and re-running is safe. "
         "`python install.py` with no arguments does the same.\n\n"
         "An install from before the rename (state folder ~/.jev-router) is migrated first: the folder moves to "
         "~/.trirouter, and the hooks, MCP entries (renamed to `trirouter`), PATH entry and remote-access services "
         "that point into it are rewritten. A dry run only reports this. `skills --apply`, `remote`, `models` and "
         "`uninstall` do the same before they write."),
     flags=(YES, DRY,
            Flag("--providers", value="LIST", default="every installed, logged-in tool",
                 doc="Comma-separated tools to connect: claude, codex, antigravity."),
            Flag("--jev-token", value="TOKEN", default="none", remembered="yes (typesafe_api_key or openrouter_api_key)",
                 doc="A TypeSafe token, or an OpenRouter key (sk-or-...). The environment variables TYPESAFE_API_KEY "
                     "and JEV_OPENROUTER_API_KEY work too."),
            Flag("--openrouter-key", value="KEY", default="none", remembered="yes (openrouter_api_key)",
                 doc="An OpenRouter key; same as --jev-token=sk-or-..."),
            Flag("--remote", value="NAME", optional=True, default="ask", remembered="yes (remote_name)",
                 doc="Also set up remote access, under NAME (default: the saved name or the hostname)."),
            Flag("--workdir", value="DIR", default="the checkout", remembered="yes (remote_workdir)",
                 doc="Folder remote Claude sessions start in. Only used together with remote access."),
            Flag("--no-migrate", doc="Do not move the skills of other tools into ~/.skills."),
            Flag("--no-completion", doc="Do not install tab completion for `trirouter` into your shell profile "
                                        "(see `trirouter help completion`)."),
            ALLOW, SCAN_LLM, ACCEPT, QDAYS),
     changes="Changes files; preview with --dry-run",
     examples=(("{prog} setup", "interactive setup (python install.py alone does the same)"),
               ("{prog} setup --dry-run", "show every change, write nothing"),
               ("{prog} setup --yes --providers=claude,codex", "unattended, only these two tools"),
               ("{prog} setup --jev-token=sk-or-...", "add an OpenRouter key and keep the rest"),
               ("{prog} setup --yes --remote=\"My PC\"", "also set up remote access")),
     exit_status="0 on success. 2 on a usage error. 1 when no logged-in tool was found.",
     see_also=("detect", "skills", "remote", "doctor", "uninstall"))

_add(key="detect", summary="Report which AI tools are installed and logged in",
     synopsis=("{prog} detect",),
     description="Checks Claude Code, Codex and Antigravity: installed, logged in, version. The Antigravity login check "
                 "asks it for its model list, so this can take a minute.",
     flags=(),
     changes="Changes nothing",
     examples=(("{prog} detect", "print the table"),),
     see_also=("setup", "doctor"))

_add(key="models", summary="Find new models, drop retired ones, test which ones your accounts can use",
     synopsis=("{prog} models [--probe] [-n]", "{prog} models --discover [-n]", "{prog} models --auto=on|off"),
     description=(
         "Compares what each tool offers today with the models the router knows. Codex: `codex debug models`; "
         "Antigravity: `agy models`; Claude Code: the model aliases `claude --help` names (only generic family "
         "aliases such as opus or sonnet -- never Haiku, a dated model ID or a mode alias). A model seen for the first "
         "time is tried with a one-word prompt (Codex, Claude; about 10-60 s each) and becomes selectable when it "
         "answers; a model that is no longer offered is taken out of the selection (Claude: only after a prompt "
         "confirms it is gone). A check that cannot run -- tool missing, not logged in, network error -- changes "
         "nothing. Results are stored per user in ~/.trirouter/models.local.json, never in the repository, and the "
         "worker agents are regenerated when something changed. Effort levels outside the policy (ultra) are "
         "always dropped.\n\n"
         "Without --discover the command also sends the one-word prompt to every known Codex model, the full "
         "account check (--probe is accepted for compatibility). --discover runs only the cheap daily check, which "
         "also runs by itself in the background at most once every 24 hours, started by a Claude Code session start "
         "or a Codex / Antigravity prompt; the outcome is logged to ~/.trirouter/logs/model_discovery.log, the next "
         "Claude Code session is told once what changed, and `{prog} doctor` shows the last check. "
         "--auto=off switches the background check off."),
     flags=(Flag("--discover", doc="Only the daily check: list every tool's models, try only the new ones "
                                   "(and ones offered again)."),
            Flag("--probe", doc="Accepted for compatibility: the full check is what the command does without --discover."),
            Flag("--auto", value="on|off", default="on", remembered="yes (model_discovery.auto)",
                 choices=("on", "off", "1", "0", "true", "false", "yes", "no"), choices_hint="on or off",
                 doc="Switch the automatic daily check on or off. Given alone, only the setting is saved."),
            DRY),
     changes="Changes files (models.local.json, worker agents); preview with --dry-run",
     examples=(("{prog} models --discover", "the daily check now"),
               ("{prog} models --discover --dry-run", "show what it would add or remove, save nothing"),
               ("{prog} models", "full check: also try every known Codex model"),
               ("{prog} models --auto=off", "no automatic daily check")),
     exit_status="0 on success. 2 on a usage error. 1 when another model check is already running.",
     see_also=("skills", "doctor"))

_add(key="skills", summary="Scan and re-link skills, regenerate worker agents, rebuild the catalog",
     synopsis=("{prog} skills [-a] [-y] [--allow-skill=NAME,...] [--scan-llm=on|off] [--accept-flagged] "
               "[--quarantine-days=N]",),
     description=(
         "Scans every third-party skill in ~/.skills with NVIDIA SkillSpector (when installed), links the skills into "
         "Claude Code and Codex, registers the folder with Antigravity, regenerates the worker agents from "
         "config/models.json + config/targets.json and rebuilds the skill catalog. Expired quarantine entries are "
         "purged.\n\n"
         "A skill rated DO_NOT_INSTALL is not decided one by one: after all scans you are asked once -- "
         "[a]ll / [n]one / [s]elect (default: none) -- which of them go to quarantine "
         "(~/.trirouter/quarantine, linked nowhere, deleted after 3 days). A skill you keep is remembered for its "
         "exact content. With --yes or without a terminal nothing is quarantined and only a warning is printed."),
     flags=(APPLY, YES, DRY, ALLOW, SCAN_LLM, ACCEPT, QDAYS),
     changes="Preview by default; --apply writes",
     examples=(("{prog} skills", "preview what would be linked, quarantined or purged"),
               ("{prog} skills --apply", "scan, ask the quarantine question once, link"),
               ("{prog} skills --apply --accept-flagged", "keep every flagged skill after a review"),
               ("{prog} skills --apply --allow-skill=docx,xlsx", "trust two skills for good"),
               ("{prog} skills --apply --scan-llm=on", "add the LLM analysis to the scan")),
     see_also=("quarantine", "setup", "doctor"))

_add(key="quarantine", summary="List, restore or purge skills the scan moved to quarantine",
     synopsis=("{prog} quarantine [list]", "{prog} quarantine restore NAME... [-n]",
               "{prog} quarantine purge [--all] [-y] [-n] [--quarantine-days=N]"),
     description=(
         "A skill moved to quarantine (~/.trirouter/quarantine) is linked nowhere and left out of the catalog. "
         "It is deleted for good after 3 days (setting skillscan.quarantine_days; 0 = never) -- automatically at "
         "every `setup`, every `skills --apply` and at most every 6 hours when Claude Code starts a session.\n\n"
         "Subcommands: list (default), restore, purge. Run `{prog} help quarantine <subcommand>` for each."),
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
     flags=(Flag("--all", doc="Delete every quarantined skill, not only the expired ones. Asks first."), YES, DRY, QDAYS),
     changes="Changes files (deletes for good); preview with --dry-run",
     examples=(("{prog} quarantine purge", "delete the expired entries"),
               ("{prog} quarantine purge --dry-run", "show which ones"),
               ("{prog} quarantine purge --all", "delete everything, after a yes"),
               ("{prog} quarantine purge --quarantine-days=7", "keep entries a week from now on")),
     exit_status="0 on success. 2 on a usage error. 1 when --all was not confirmed (nothing deleted).",
     see_also=("quarantine list", "skills"))

_add(key="remote", summary="Set up or remove phone / other-device access",
     synopsis=("{prog} remote [--name=NAME] [--workdir=DIR] [-n]", "{prog} remote --remove [-n]"),
     description="Starts each tool's own remote service at logon (Claude Code Remote Control, Antigravity and Codex), "
                 "under one machine name. See docs/remote-access.md.",
     flags=(Flag("--name", value="NAME", default="saved name or hostname", remembered="yes (remote_name)",
                 doc="The machine name shown on your other devices."),
            Flag("--workdir", value="DIR", default="saved folder or the checkout", remembered="yes (remote_workdir)",
                 doc="Folder remote Claude sessions start in; it must be a trusted Claude Code folder."),
            Flag("--remove", doc="Remove the remote-access services instead of creating them."), DRY),
     changes="Changes files and system services; preview with --dry-run",
     examples=(("{prog} remote --name \"My PC\"", "set up under a name"),
               ("{prog} remote --name \"My PC\" --workdir ~/code", "choose the working folder"),
               ("{prog} remote --remove", "undo")),
     see_also=("setup", "doctor"))

_add(key="doctor", summary="Read-only health report",
     synopsis=("{prog} doctor",),
     description="Checks the tools, hooks, MCP entries, skill hub and agents, configuration, interpreters, the "
                 "`trirouter` command, the quarantine and recent router activity. Problems are marked [!!] with the "
                 "command that fixes them.",
     flags=(), changes="Changes nothing",
     examples=(("{prog} doctor", "print the report"),),
     exit_status="0 always: read the [!!] lines for problems. 2 on a usage error.",
     see_also=("setup", "skills"))

_add(key="route", summary="The routing decision for one prompt or sub-task",
     synopsis=("{prog} route [--provider=NAME] [--json] [--] TEXT...",),
     description="Runs the same pipeline as the hooks for one prompt, with no side effects (no queue state, no log). "
                 "With no TEXT the prompt is read from stdin. Without --json it prints the [router] instruction; "
                 "with --json one object: model, effort, agent, tier, task, difficulty, extra_agents, destructive, "
                 "skill, verify, lang, backend, text, note. #norouter / #privat prompts are not routed. "
                 "`route` must be the first argument; everything after `--` is prompt text.",
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

_add(key="completion", summary="Print or install shell tab completion for trirouter",
     synopsis=("{prog} completion [--shell=bash|zsh|fish|powershell]", "{prog} completion --install [--shell=SHELL] [-n]",
               "{prog} completion --remove [-n]"),
     description=(
         "Prints a completion script for commands, subcommands, flags and flag values, generated from the same "
         "definitions as this manual, like `gh completion -s bash`. Without --shell the current shell is used "
         "($SHELL when it is bash, zsh or fish -- Git Bash sets it on Windows -- else PowerShell on Windows, zsh "
         "on macOS and bash on Linux).\n\n"
         "--install writes the script to ~/.trirouter/completion/ and one marked block that loads it into the "
         "shell's profile: ~/.bashrc (macOS: ~/.bash_profile), ~/.zshrc ($ZDOTDIR is respected), the PowerShell "
         "$PROFILE of every installed edition (Windows PowerShell and pwsh, also on macOS and Linux); fish loads "
         "$XDG_CONFIG_HOME/fish/completions/trirouter.fish by itself. Setup installs it for your shell and, "
         "on Windows or wherever pwsh is installed, for PowerShell too. Re-running changes "
         "nothing when everything is up to date; a changed profile keeps its original once as <file>.bak. "
         "`{prog} setup` does this by default, and keeps the script current. Open a new terminal afterwards "
         "(or load the profile again)."),
     flags=(Flag("--shell", value="SHELL", default="the current shell", choices=("bash", "zsh", "fish", "powershell"),
                 choices_hint="bash, zsh, fish or powershell", doc="The shell to print or install the script for."),
            Flag("--install", doc="Install the script and the profile block instead of printing the script."),
            Flag("--remove", doc="Remove the profile blocks and scripts of every shell."), DRY),
     changes="Prints only; --install and --remove change files (preview with --dry-run)",
     examples=(("{prog} completion --install", "tab completion for the current shell"),
               ("{prog} completion --install --shell=powershell -n", "show what would change"),
               ("{prog} completion --shell=bash > ~/.trirouter-completion.bash", "just the script"),
               ("{prog} completion --remove", "take it out again")),
     see_also=("setup", "uninstall"))

_add(key="uninstall", summary="Remove hooks, MCP entries, remote access and the trirouter command",
     synopsis=("{prog} uninstall [-n]",),
     description="Removes the router's hooks and MCP entries from Claude Code, Codex and Antigravity, the remote-access "
                 "services, the `trirouter` launcher with its PATH entry and the tab completion in your shell "
                 "profiles. Your skills stay in ~/.skills (and linked).",
     flags=(DRY,), changes="Changes files; preview with --dry-run",
     examples=(("{prog} uninstall --dry-run", "show what would be removed"), ("{prog} uninstall", "remove it")),
     see_also=("setup", "remote"))

_add(key="help", summary="Show the manual of a command",
     synopsis=("{prog} help [COMMAND [SUBCOMMAND]]", "{prog} help --markdown"),
     description="Prints the man-page-like help of a command. `{prog} <command> --help` does the same. "
                 "With --markdown it prints the complete reference (docs/cli.md).",
     flags=(Flag("--markdown", doc="Print the whole reference as Markdown (this is how docs/cli.md is generated)."),),
     positional="COMMAND a command name; SUBCOMMAND for quarantine",
     changes="Changes nothing",
     examples=(("{prog} help", "the command list"), ("{prog} help skills", "the skills page"),
               ("{prog} help quarantine purge", "a subcommand"), ("{prog} help --markdown > docs/cli.md", "regenerate the reference")),
     see_also=("version",))

_add(key="version", summary="Print the version",
     synopsis=("{prog} version",), description="Prints the installed version.", flags=(), changes="Changes nothing",
     examples=(("{prog} version", ""),), see_also=("help",))

ORDER = ("setup", "detect", "models", "skills", "quarantine", "remote", "doctor", "route", "completion", "uninstall",
         "help", "version")
SUBCOMMANDS = {"quarantine": ("list", "restore", "purge")}
DEFAULT_SUBCOMMAND = {"quarantine": "list"}

GETTING_STARTED = """\
1. Install Python 3.10+ and at least one of Claude Code, Codex or Antigravity, and log in to it.
2. First run, from the checkout (the `trirouter` command does not exist yet):

   ```bash
   python install.py --dry-run     # preview every change
   python install.py               # interactive setup (same as `python install.py setup`)
   ```

3. Setup writes the `trirouter` launcher to `~/.trirouter/bin` and puts that folder on your PATH
   (Windows: the user PATH, never the system one; macOS / Linux: a link in `~/.local/bin`, and setup prints the
   line to add to your shell profile if that folder is not on PATH). **Reopen your terminal**, then:

   ```bash
   trirouter help                  # the command list
   trirouter doctor                # health report
   ```

   Setup also installs tab completion for your shell (PowerShell, bash, zsh or fish): type `trirouter `, a
   few letters, and press Tab to complete commands, subcommands, flags and flag values.

If you move the checkout, run `python install.py` from its new place once: the launcher and the hooks are
re-pointed. `python install.py <command>` and `python -m trirouter <command>` always keep working and
behave like `trirouter <command>`."""

CONVENTIONS = """\
* `trirouter <command> [subcommand] [flags]`; flags may come in any order after the command. `--flag=value`
  and `--flag value` are equivalent.
* Every command declares the flags it accepts. An unknown or not-applicable flag, a value flag without a value
  or a boolean flag given a value is an error: exit status 2, a "did you mean" suggestion and a pointer to the
  help page.
* `-h`, `--help` anywhere after a command shows its page; `trirouter help <command>` does the same.
* Short flags: `-h` help, `-y` --yes, `-n` --dry-run, `-a` --apply (boolean ones can be combined: `-yn`).
* Flags marked "remembered" are saved in `~/.trirouter/config.json` when the command writes, and apply to
  later runs.
* Exit status: 0 success, 2 usage error, 1 the command ran but failed."""

TASKS = (
    ("See what setup would do", "trirouter setup --dry-run"),
    ("Check that everything works", "trirouter doctor"),
    ("Re-scan skills after adding some to `~/.skills`", "trirouter skills --apply"),
    ("See what is in quarantine and when it is deleted", "trirouter quarantine"),
    ("Get a quarantined skill back", "trirouter quarantine restore <name>\ntrirouter skills --apply"),
    ("Keep quarantined skills a week instead of 3 days", "trirouter skills --apply --quarantine-days=7"),
    ("Never delete quarantined skills automatically", "trirouter skills --apply --quarantine-days=0"),
    ("Get the routing decision for a sub-task from a script", "trirouter route --json \"add a CSV export\""),
    ("Check for new or retired models now", "trirouter models --discover"),
    ("Turn the daily model check off", "trirouter models --auto=off"),
    ("Tab completion for trirouter in your shell", "trirouter completion --install"),
    ("Reach this computer from a phone", "trirouter remote --name \"My PC\""),
    ("Remove everything the installer added", "trirouter uninstall"),
)


def key_of(*words):
    return " ".join(words)


def all_flags(cmd):
    return cmd.flags + (HELP_FLAG,)


def prog_name(prog=None):
    return prog or DOC_PROG


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
    prog = prog_name(prog)
    lines = [f"usage: {prog} <command> [subcommand] [flags]", "", "Commands:"]
    lines += overview_lines(prog)
    lines += ["", f"Run `{prog} help <command>` for details, e.g. `{prog} help skills`.",
              "Full reference: docs/cli.md"]
    return "\n".join(lines)


def render_help(key, prog=None):
    prog = prog_name(prog)
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
    out += [_fmt(c.description, p).replace("\n\n", "\n\n"), ""]
    if c.positional:
        out += [f"Arguments: {_fmt(c.positional, p)}.", ""]
    out += ["| Flag | Value | Default | Remembered | Description |", "| --- | --- | --- | --- | --- |"]
    for f in all_flags(c):
        names = f"`{f.short}`, `{f.long}`" if f.short else f"`{f.long}`"
        value = (f"`{f.value}`" + (" (optional)" if f.optional else "")) if f.value else "-"
        default = "-" if f.long == "--help" else f.default
        remembered = "-" if f.long == "--help" else f.remembered
        out.append(f"| {names} | {value} | {default} | {remembered} | {f.doc.replace('|', '/')} |")
    out += ["", f"**Changes:** {c.changes}.", "", "Examples:", "", "```bash"]
    for cmd, note in c.examples:
        out.append(f"{_fmt(cmd, p)}" + (f"    # {note}" if note else ""))
    out += ["```", "", f"**Exit status:** {c.exit_status or EXIT_STD}", "",
            "**See also:** " + ", ".join(f"[{k}](#{anchor(k)})" for k in c.see_also), ""]
    return out


def render_markdown():
    p = DOC_PROG
    out = ["# trirouter command reference", "",
           "<!-- generated by `trirouter help --markdown` from trirouter/manual.py; do not edit by hand -->", "",
           "`trirouter <command> [subcommand] [flags]` -- the same pages are shown by `trirouter help <command>`.", "",
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
