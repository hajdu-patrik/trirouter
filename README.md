# trirouter – One Prompt Router for Claude Code, Codex & Antigravity

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat&logo=python&logoColor=white)
![Stdlib Only](https://img.shields.io/badge/Dependencies-None-2E7D32?style=flat)
![Claude Code](https://img.shields.io/badge/Claude_Code-supported-D97757?style=flat&logo=anthropic&logoColor=white)
![Codex](https://img.shields.io/badge/OpenAI_Codex-supported-412991?style=flat&logo=openai&logoColor=white)
![Antigravity](https://img.shields.io/badge/Google_Antigravity-supported-4285F4?style=flat&logo=google&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-stdio_server-000000?style=flat)
![Platforms](https://img.shields.io/badge/Platforms-Windows_%7C_macOS_%7C_Linux-555555?style=flat)
![License](https://img.shields.io/badge/License-MIT-yellow?style=flat)

**trirouter** runs before every prompt you send to Claude Code, OpenAI Codex or Google Antigravity –
in every project, in the desktop apps and in remote sessions – and decides for that single request:

* **which model** and **which reasoning effort** to use,
* **how many extra agents** may work in parallel (strict token budget),
* **which skill** fits – from one shared folder that all three tools read,
* whether the request is **irreversible** (then the model must ask before acting),
* **which language** to answer in (the language of the prompt).

> Formerly **jev-router** (repository `claude-workspace`), renamed to trirouter everywhere: the Python
> package, `~/.trirouter/`, the `trirouter` MCP server and the scheduled tasks. An existing install is
> migrated by `python install.py` (the state folder moves, hooks, MCP entries, PATH and remote-access
> services are rewritten); see the CHANGELOG for the steps. `JEV` alone is the external routing engine
> and keeps its name.

It also **protects running work**: a prompt sent while an earlier one is still being processed is
queued behind it and must not stop or overwrite it.

Decisions come from **JEV (TypeSafe)** when you configure a TypeSafe token or an **OpenRouter key**
(JEV is on OpenRouter since September 2026). Without one, a built-in local
classifier with the same answer format decides – everything works out of the box.

---

## 🚀 Quick Start

Requirements: Python 3.10+ and at least one of [Claude Code](https://code.claude.com/docs/en/quickstart),
[Codex CLI](https://developers.openai.com/codex) or [Antigravity CLI](https://antigravity.google/docs/cli/install/).

```bash
git clone https://github.com/hajdu-patrik/trirouter.git trirouter
cd trirouter
python install.py          # first run: the `trirouter` command does not exist yet
```

Setup also installs the **`trirouter` command** (a launcher in `~/.trirouter/bin`, put on your PATH).
Open a new terminal afterwards and use `trirouter <command>` from any folder – `trirouter help` lists
the commands, `trirouter help <command>` shows a man-page-like page, and **[docs/cli.md](docs/cli.md)**
is the full reference. `python install.py <command>` and `python -m trirouter <command>` keep working
and do the same (use them for the first run, or when the launcher is not on PATH).

The installer walks you through five steps:

1. **Detect** which of Claude Code, Codex and Antigravity are installed and logged in, and tell you
   how to log in to the ones that are not.
2. **JEV access** – paste a TypeSafe token or an OpenRouter key (`sk-or-…`), or press Enter to use the
   built-in local classifier.
3. **Hooks + MCP server** for every logged-in tool, and the `trirouter` command.
4. **Shared skill folder** `~/.skills` – existing skills of every tool are moved there, scanned with
   [SkillSpector](#skill-security-scan-skillspector) when it is installed (a risky one can be
   quarantined), and linked back, so each
   tool sees all of them; worker agents are generated for every model × effort.
5. **Optional extras** – remote access from your phone, speech-to-text.

Every change is shown first, changed config files get a `.bak` copy, and re-running is safe.
Preview without changing anything: `python install.py --dry-run` (later: `trirouter setup --dry-run`).

| Command | Purpose |
| --- | --- |
| `trirouter setup [--dry-run]` | the interactive setup (what `python install.py` runs) |
| `trirouter models [--discover] [--auto=on\|off] [--dry-run]` | find new models, drop retired ones, test which ones your accounts may use (stored per user; also runs by itself once a day) |
| `trirouter remote --name "My PC" [--workdir <folder>]` | remote access from other devices ([guide](docs/remote-access.md)) |
| `trirouter uninstall` | remove hooks, MCP entries, remote access and the launcher (skills stay) |
| `trirouter skills [--apply] [--allow-skill=<name>] [--scan-llm=on\|off] [--accept-flagged] [--quarantine-days=N]` | scan + re-link skills, regenerate workers, rebuild the catalog |
| `trirouter quarantine [list \| restore <name>... \| purge [--all]]` | see, restore or delete the skills the scan quarantined |
| `trirouter doctor` | health report (also: which tools are installed, their version, logged in or not) |
| `trirouter route [--provider claude] [--json] <text>` | routing decision for one prompt or sub-task, side-effect free ([details](#routing-decision-on-demand)) |
| `trirouter help [<command>]`, `trirouter --version` | the manual pages, the version |

**Tab completion:** setup installs it for your shell – PowerShell (Windows PowerShell and pwsh), bash
(`~/.bashrc`; macOS: `~/.bash_profile`), zsh (`~/.zshrc`, the macOS default) or fish – as one marked block
in the profile (with a `.bak` of the original; skip it with `--no-completion`; `trirouter uninstall` removes it). In a new terminal type
`mo` + Tab after `trirouter` completes to `models`; after `trirouter models`, `--` + Tab lists its flags and `--auto=` + Tab offers `on` / `off`.

**Interrupting:** Ctrl+C (and Ctrl+D, also at a question) stops any command at once: it prints `Interrupted: nothing was
left half-written.`, ends the programs it started and exits with status 130. Config files are written atomically
(temporary file, then rename), so an interrupt never leaves a half-written file.

Every command validates its flags (an unknown or misplaced one is an error with a "did you mean"
hint) and has its own page: `trirouter help skills`, `trirouter skills --help`. The same pages, with a
getting-started guide and common tasks, are in **[docs/cli.md](docs/cli.md)**.

**One-time steps after installing:** Codex runs a new hook only after you trust it (`codex` → `/hooks`).
For Claude desktop *Chat/Cowork*, restart the app and add to *Settings → Profile → Personal preferences*:
*"Before answering any new request, call the trirouter route_prompt tool with my message and follow its instructions."*

---

## 🧭 How It Works

```
prompt ─► hook / MCP tool ─► trirouter/core.route()
                               ├─ is_continuation()         "mehet" / "yes, do it" → keep the last decision
                               ├─ split_pasted()            pasted blocks never decide language or task
                               ├─ lang.detect()             answer language
                               ├─ is_destructive()          regex safety net (+ JEV verdict)
                               ├─ catalog.prefilter()       shared skill catalog → ≤ 8 candidates
                               ├─ classify()                JEV (TypeSafe / OpenRouter) or built-in classifier
                               ├─ decide()                  routes.json: task × difficulty → tier
                               └─ render()                  targets.json: tier → worker, model, effort
       + queue_state            earlier work still running? → "finish it first"
─► "[router] … Delegate to `opus-worker-xhigh` (opus, effort xhigh). Parallelism: none. Respond in English."
```

| Surface | Mechanism |
| --- | --- |
| Claude Code (CLI, desktop *Code*, Remote Control) | `UserPromptSubmit`, `Stop`, `StopFailure`, `SessionEnd`, `SubagentStart` hooks; `SessionStart` only purges expired quarantine entries |
| Codex (CLI, ChatGPT app in Codex mode) | `UserPromptSubmit` + `Stop` hooks |
| Antigravity (CLI, desktop app) | `PreInvocation` + `Stop` hooks (prompt read from the transcript, injected once per turn) |
| Claude desktop *Chat / Cowork* (no hooks there) | MCP tool `route_prompt` |
| Claude Code on the web (cloud sandbox) | project hook with `--cloud-only` |
| Scripts, other agents and projects (per sub-task) | `route` command via the shim `~/.trirouter/bin/route.py` |

### Model and effort are enforced, not suggested

No tool lets a hook switch the running model. trirouter therefore generates one **worker agent per
(model, effort) pair** – Claude subagents `<model>-worker-<effort>` (plus `test-worker-<effort>`),
Codex roles `<model>-<effort>` – and the router delegates to the right one. Antigravity agents can
pin only a model tier (flash / pro), not an effort, and only as subagents: trirouter generates
`gemini-flash-worker` and `gemini-pro-worker` (`~/.gemini/config/agents/`), and hard requests are
delegated to the Pro one; otherwise the model choice is advisory (or enforced through `cli-bridge`).

Each agent's instructions come from the template of its model's **role** in `models.json` –
`fast`, `balanced`, `deep` (`trirouter/templates/agents/<role>-worker.md`), plus `test-worker` for
the test tier – so a new model needs one line in `models.json`, not a new template.

| Provider | Models (catalog: `trirouter/config/models.json`) | Effort levels |
| --- | --- | --- |
| Claude | fable, sonnet, opus – generic aliases only, never Haiku | low · medium · high · xhigh · max |
| Codex | gpt-6-luna, gpt-5.6-terra, gpt-5.6-luna, gpt-reserve by default; more after `trirouter models` | low … max (per model) |
| Antigravity | Gemini 3.8 / 3.7 / 3.6 Flash, Gemini 3.1 Pro, Claude Sonnet/Opus 4.6, GPT-OSS 120B | part of the model name |

The `ultra` effort level is never offered, stripped from any answer and has no worker.

**New and retired models are picked up by themselves.** Once a day (at most every 24 hours, started in the
background by a Claude Code session start or a Codex / Antigravity prompt; the hook itself returns at
once) trirouter compares what each tool offers with what it knows: `codex debug models`, `agy models`, and the
model aliases `claude --help` names. A model seen for the first time is tried with a one-word prompt and
becomes selectable when it answers – Claude only as a generic family alias, never Haiku or a dated ID; a
model no longer offered leaves the selection. Results go to `~/.trirouter/models.local.json` (never into the
repository), the worker agents are regenerated, the next Claude Code session gets a one-line note, and
`trirouter doctor` shows the last check. A check that cannot run (tool missing, logged out, offline) changes
nothing. Run it by hand with `trirouter models --discover [--dry-run]`; switch it off with
`trirouter models --auto=off`. Cost: on an ordinary day no model is prompted at all – only the model lists
are read; each newly listed Codex or Claude model costs one short prompt, once.

### Follow-ups and pasted text

A bare go-ahead or status check – "mehet", "igen, töröld!", "yes, do it", "hogy állunk?" – keeps the
previous decision of the same session (model, effort, worker) instead of being classified on its own,
where it would look trivial. At most one word beyond the go-ahead is allowed, so "ok, most írj
teszteket" is routed as new work. The safety check always runs on the new prompt.

Pasted blocks (`<pasted_content>`: logs, CI output, a README) are not the user's own words: they never
decide the answer language or the task type. JEV still sees them as context, and the safety regex
still scans them, because a pasted command can be the dangerous part. Subagent hand-backs
(`<agent-message>`) are not routed at all.

### Parallel agents

| Extra agents | When |
| --- | --- |
| **0** | default – the vast majority of requests |
| **+1** | two clearly independent, substantial parts |
| **+2** | three independent workstreams (e.g. backend + frontend + migration) |
| **+3** | a complete new page or feature from scratch (backend + frontend + data layer) |
| **+4** | very rare – the same, plus custom tooling such as a scraper |

Code-level clamps: one fewer when the answer is not confident, at most +1 unless the request is hard.

### Queue protection

All three tools already queue messages typed while the agent is busy. trirouter adds the missing
context: the new prompt is told that earlier work in the same session is still running and must be
finished first – never stopped, restarted or overwritten. If another session works in the same
folder, the prompt is warned not to modify that session's files. Claude's `StopFailure` (a turn that
died on an API error) and `SessionEnd` release the queue as well, and state expires automatically,
so a crashed session never blocks anything.

`doctor` also reports **delegation compliance**: the `SubagentStart` hook records which worker the
model really started, so you can see how often it followed the router's advice.

### Shared skills

`~/.skills` is the single skill folder. Claude Code and Codex see it through per-skill links
(junctions on Windows, symlinks elsewhere); Antigravity through its `skills.json`. Skills that tools
manage themselves (Claude desktop's synced skills, plugins, Codex built-ins) stay in place but are
indexed too, so the router can hand a skill of one tool to another ("read and follow `<path>/SKILL.md`").

### Skill security scan (SkillSpector)

Before a skill in `~/.skills` is linked into any tool, the installer (`setup` step 4 and `skills`)
scans it with NVIDIA's [SkillSpector](https://github.com/NVIDIA/SkillSpector) (prompt injection,
data exfiltration, privilege escalation, supply-chain risks, …) and acts on its recommendation:

| Verdict | Risk | Action |
| --- | --- | --- |
| `SAFE` | 0–20 | linked |
| `CAUTION` | 21–50 | linked, named in one summary warning with the command to review it |
| `DO_NOT_INSTALL` | 51–100 | after all scans you are asked **once** which of them to quarantine (all / none / select; default none); unattended runs (`--yes`, no terminal) only warn |

**Quarantine** moves the skill folder from `~/.skills/<name>` to `~/.trirouter/quarantine/<name>`:
no tool sees it any more – its links are removed, it leaves the catalog, and
Antigravity (which reads the whole hub) no longer finds it. The question is asked once for the whole
batch, as a numbered list:

```
  1. agent-platform-deploy (risk 79, max HIGH)
  2. impeccable (risk 100, max HIGH)
Quarantine these skills? [a]ll / [n]one / [s]elect (default: none)
```

`a` quarantines all, `n` or Enter keeps all, `s` asks for numbers (`1,3,5-8`), shows the two groups
and wants a final `y`. Skills you keep stay linked and are remembered for exactly that content: you are
asked again only when a skill changes.

**Automatic purge:** a quarantined skill is **deleted for good after 3 days** (a sidecar
`~/.trirouter/quarantine/<name>.json` records when it was quarantined, its risk and the purge date).
`skillscan.quarantine_days` in `config.json` (`--quarantine-days=N`, remembered; `0` = never) changes
the retention. Expired entries are purged by every `setup` and `skills --apply`, and, at most every 6
hours, when a Claude Code session starts. Only folders inside `~/.trirouter/quarantine` are ever
deleted, and links are never followed. Within the 3 days: `trirouter quarantine` lists the entries with
their purge date, `trirouter quarantine restore <name>` moves one back to `~/.skills` and allows it,
`trirouter quarantine purge [--all]` deletes now.

Why ask instead of blocking: in a test on the 19 skills of
[anthropics/skills](https://github.com/anthropics/skills), SkillSpector 2.12 rated 7 legitimate ones
`DO_NOT_INSTALL` (`docx`, `xlsx`, `pptx`, `mcp-builder`, `skill-creator`, `webapp-testing`,
`claude-api`) – scripts that call `subprocess` or read files look like exfiltration to static analysis,
and its LLM meta-analysis did not change those verdicts.

* **Override:** `--allow-skill=<name>[,<name>]` never asks about these skills and restores them from
  quarantine (so does `trirouter quarantine restore <name>`); the list is remembered in `config.json` (`skillscan.allow`).
* **Keep all after a review:** `skills --apply --accept-flagged` keeps every skill now rated
  `DO_NOT_INSTALL` without asking, like answering "none" – bound to its current content, so a
  changed skill is asked about again. A skill's source cannot be verified (its frontmatter can claim
  any author), so there is no allow-by-publisher list.
* **Static by default:** the scan runs with `--no-llm`, so no skill content leaves the machine.
  `--scan-llm=on` (remembered as `skillscan.llm`) adds SkillSpector's LLM analysis, configured through
  its own variables (`SKILLSPECTOR_PROVIDER`, e.g. `claude_cli`, and that provider's key). Without a
  working provider the static verdict is used and not cached.
* **Cache:** a verdict – a failed scan too – is kept per skill content hash, SkillSpector version and
  scan mode (`~/.trirouter/state/skillscan.json`), so only new or changed skills are scanned again.
  A first scan takes seconds per skill, minutes for a large one (timeout: 10 min static, 30 min with LLM);
  new or changed skills are scanned in parallel, up to 8 at a time (half the CPU cores), which makes
  a first scan of a large hub several times faster. Each outcome is reported in one summary line.
* **Scope:** skills bundled with trirouter (`trirouter/skills/`) and app-managed skills (Claude
  desktop, plugins, Codex built-ins) are not scanned.
* **Optional:** SkillSpector needs Python 3.12+ and runs as its own tool:
  `uv tool install git+https://github.com/NVIDIA/skillspector.git`. Without it, or when a scan fails,
  skills are linked unscanned with a warning.
* **Windows:** a packaged Python (Python install manager / Microsoft Store) does not see uv's default
  tool folder under `%APPDATA%`, so `skillspector` fails to start ("uv trampoline failed to
  canonicalize script path"); the installer then warns once and links skills unscanned. Reinstall with
  `UV_TOOL_DIR` set outside AppData (e.g. `%USERPROFILE%\.local\share\uv\tools`):
  `uv tool install --force git+https://github.com/NVIDIA/skillspector.git` – and use the same
  `UV_TOOL_DIR` for `uv tool upgrade`.

### Overrides

Claude `#fable #sonnet #opus #codex #antigravity` · Codex / Antigravity `#fast #main #deep` ·
`#norouter` / `#privat`: no routing, nothing is sent to TypeSafe (queue protection still applies).

### Routing decision on demand

The hooks route each user prompt. To get a decision for a single sub-task – from a script, another
agent or another project – call the `route` command. It runs the same pipeline (`core.route()`) but
has no side effects: no queue state, no log.

```bash
trirouter route --json "add a pagination parameter to the quotes API endpoint"
trirouter route [--provider claude|claude-chat|codex|antigravity] [--json] <text>   # no text: stdin
python ~/.trirouter/bin/route.py --json "<text>"     # the same through the shim, from any program
```

Without `--json` it prints the `[router] …` instruction. With `--json` it prints one object:
`model` (e.g. `sonnet`; `null` when the tier answers in-session), `effort`, `agent` (the worker to
delegate to, or `null`), `tier`, `task`, `difficulty`, `extra_agents`, `destructive`, `skill`,
`verify`, `lang`, `backend`, `text` and `note`. A `#norouter` / `#privat` prompt is not routed
(`model: null`, nothing is sent to TypeSafe). Exit codes: `0` success, `2` usage error (e.g. an
empty prompt), `1` unexpected error (only the exception type goes to stderr). The installer writes
the shim, so callers never need to know where the repository lives.

---

## 🎙️ Speech-to-Text

Dictate prompts into any app, in any language Whisper supports – offline and free.
See **[docs/speech-to-text.md](docs/speech-to-text.md)** for installation, model choice by hardware
and phone dictation.

---

## 📱 Remote Access

Control your computer from a phone or another device under one machine name, with every tool
starting its remote service at logon. See **[docs/remote-access.md](docs/remote-access.md)**.

---

## ⚙️ Configuration

| Setting | Where |
| --- | --- |
| JEV access | `trirouter setup --jev-token=<TypeSafe token or OpenRouter key>`, or the environment variable `TYPESAFE_API_KEY` / `JEV_OPENROUTER_API_KEY`. The generic `OPENROUTER_API_KEY` is ignored on purpose: another tool's key must not start spending credit on routing. |
| Per-user state | `~/.trirouter/` – `config.json`, `models.local.json` (per-account availability and discovered models), `logs/` (incl. `model_discovery.log`), `state/`, `quarantine/` (skills you quarantined), `completion/` (tab-completion scripts), `bin/` (shims `run_hook.py`, `mcp_server.py`, `route.py` and the `trirouter` launcher) |
| Daily model check | `config.json` → `model_discovery.auto` (default on); set with `trirouter models --auto=on\|off` |
| Skill scan | `config.json` → `skillscan`: `allow` (skills never asked about despite `DO_NOT_INSTALL`), `llm` (add the LLM analysis), `quarantine_days` (default 3, 0 = never purge); set with `--allow-skill` / `--scan-llm` / `--quarantine-days` |
| Model catalog, tiers, routing table | `trirouter/config/models.json`, `targets.json`, `routes.json` |

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `ROUTER_BACKEND` | `auto` | `jev`, `local` or `auto` (JEV when a token or key exists) |
| `ROUTER_CONTINUATION_TTL_MIN` | `180` | how long a go-ahead can continue the session's previous decision |
| `ROUTER_MIN_CONFIDENCE` | `0.6` | below it the task falls back to the default tier |
| `ROUTER_MAX_EXTRA_AGENTS` | `4` | hard cap for parallel agents |
| `ROUTER_QUEUE_TTL_MIN` | `120` | minutes after which an unfinished queue entry is ignored |
| `ROUTER_SKILL_CANDIDATES` | `8` | skills offered to JEV per request |
| `ROUTER_LOG_PROMPTS` | unset | `1` = log full prompts (default: first 200 characters, secrets redacted) |
| `TYPESAFE_API_URL`, `JEV_MODEL`, `JEV_TIMEOUT` | – | JEV endpoint, pinned model version, timeout in seconds |
| `JEV_OPENROUTER_URL`, `JEV_OPENROUTER_MODEL` | OpenRouter System One, `jev-1.13` | the same through OpenRouter |

---

## 📂 Repository Layout

```
install.py                 entry point: `python install.py [command]` (same as `python -m trirouter` and `trirouter`)
pyproject.toml             package metadata, console script `trirouter`, pytest settings
trirouter/                 the package
├── cli.py                 the commands (setup, models, skills, quarantine, remote, doctor, route, uninstall)
├── manual.py              every command, flag and help text as data (help pages, docs/cli.md)
├── cliparse.py            argument validation against manual.py
├── core.py                classification, decision, rendering, safety regex, JEV client + built-in classifier
├── lang.py                Hungarian / English detection
├── catalog.py             skill catalog and pre-filter
├── hooks.py               hook entry point for all three tools (python -m trirouter.hooks)
├── queue_state.py         queue protection
├── mcp_server.py          MCP server: route_prompt, list_skills, get_skill (python -m trirouter.mcp_server)
├── hub.py                 shared skill folder, links, worker generation
├── discovery.py           daily model discovery (new / retired models per tool, background run, lock)
├── completion.py          shell tab completion generated from manual.py, profile install (used by setup / uninstall)
├── interrupt.py           Ctrl+C / Ctrl+D handling for the commands (exit 130, key watcher)
├── skillscan.py           SkillSpector gate: scan, cache, quarantine before skills are linked
├── integrations.py        hook + MCP registration per tool, ~/.trirouter/bin shims
├── platforms.py           OS abstraction (paths, links, executables, detection)
├── remote.py              optional remote access
├── doctor.py              health report
├── config/                models.json (catalog + policy), routes.json (task → tier), targets.json (tier → worker)
├── templates/agents/      role templates fast/balanced/deep/test-worker.md (rendered into
│                          ~/.claude/agents, ~/.codex/agents and ~/.gemini/config/agents)
└── skills/                skills bundled with trirouter (cli-bridge)
tests/                     unit tests, model-policy test
eval/                      100 Hungarian + 100 English labelled prompts, 45 real-traffic shapes, evaluation script
docs/                      cli.md (command reference, generated), speech-to-text and remote-access guides
```

---

## 🧪 Development

```bash
pip install -e .[dev]              # optional: editable install, adds a `trirouter` console script
python -m pytest tests -q          # unit tests incl. the model-policy check
python eval/eval_router.py         # full pipeline on the labelled prompts (exit 1 below target)
trirouter help --markdown > docs/cli.md   # after editing trirouter/manual.py (a test checks the file)
```

Evaluation targets on the curated sets: task accuracy ≥ 85 % per language, "routing uncertain" ≤ 30 %,
destructive-request recall 100 %, false positives < 5 %, reply language 100 %. The real-traffic set
(`eval/real_prompts.csv`: typos, missing accents, pasted blocks, go-aheads) has lower regression
gates, because the built-in classifier is weak there and JEV is the real fix. Every set also reports
tier accuracy next to the best fixed-tier baseline: routing only pays off if it beats always picking
the same tier. The test suite runs on Windows, macOS and Linux in CI.

See [CHANGELOG.md](CHANGELOG.md) for the release history and [CLAUDE.md](CLAUDE.md) for contributor
conventions.

---

## 📄 License

[MIT](LICENSE.md). Product names are trademarks of their respective owners.
