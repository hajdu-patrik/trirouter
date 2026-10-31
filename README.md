# trirouter – One Prompt Router for Claude Code, Codex & Antigravity

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat&logo=python&logoColor=white)
![Stdlib Only](https://img.shields.io/badge/Dependencies-None-2E7D32?style=flat)
![Platforms](https://img.shields.io/badge/Platforms-Windows_%7C_macOS_%7C_Linux-555555?style=flat)
![License](https://img.shields.io/badge/License-MIT-yellow?style=flat)

**trirouter** runs before every prompt you send to Claude Code, OpenAI Codex or Google Antigravity –
in every project, in the desktop apps and in remote sessions – and decides for that single request:

* **which model** and **which reasoning effort** to use,
* **how many extra agents** may work in parallel (strict token budget),
* **which skill** fits – from one shared folder that all three tools read,
* whether the request is **irreversible** (then the model must ask before acting),
* **which language** to answer in (the language of the prompt).

It also **protects running work**: a prompt sent while an earlier one is still being processed is told to
finish that work first, never stop or overwrite it. Decisions come from **JEV (TypeSafe)** when you configure a
TypeSafe token or an OpenRouter key; without one a built-in local classifier decides, so everything works out
of the box.

## Quick start

Requirements: Python 3.10+ and at least one of [Claude Code](https://code.claude.com/docs/en/quickstart),
[Codex CLI](https://developers.openai.com/codex) or [Antigravity CLI](https://antigravity.google/docs/cli/install/).

```bash
git clone https://github.com/hajdu-patrik/trirouter.git trirouter
cd trirouter
python install.py --dry-run   # preview every change
python install.py             # interactive setup
```

Setup detects the tools, takes an optional JEV key, connects hooks and the MCP server, installs the `trirouter`
command with tab completion, moves every tool's skills into the shared `~/.skills` folder (scanned with
SkillSpector when installed), generates the worker agents and offers remote access. Every change is reported,
changed config files keep a `.bak`, and a re-run reports only what changed and asks again only for a missing JEV key.
Without a terminal setup needs `--yes`, so a stray run changes nothing.

Open a new terminal afterwards: `trirouter` greets you and lists the commands, `trirouter help <command>` shows a
page, and **[docs/cli.md](docs/cli.md)** is the full reference. New Claude Code sessions show a one-line welcome
that never reaches the model (`ROUTER_WELCOME=0` turns it off). The everyday commands:

| Command | Purpose |
| --- | --- |
| `trirouter doctor` | health report: tools, hooks, MCP, skills, agents, keys, remote access |
| `trirouter skills --apply` | re-scan and re-link skills after adding some to `~/.skills` |
| `trirouter models` | find new models, drop retired ones; `--main=sonnet` sets Claude Code's session model |
| `trirouter sync` | after a pull or a change: tab completion, agents, skill catalog, docs |
| `trirouter route --json "<text>"` | the routing decision for one prompt, from a script |

One-time steps: Codex runs a new hook only after you trust it (`codex` → `/hooks`). For Claude desktop *Chat/Cowork*
(no hooks there) restart the app and add to *Settings → Profile → Personal preferences*: *"Before answering any new
request, call the trirouter route_prompt tool with my message and follow its instructions."*

## How it works

```
prompt ─► hook / MCP tool ─► trirouter/core.route()
                               ├─ is_continuation()   a go-ahead ("mehet", "yes, do it") keeps the last decision
                               ├─ split_pasted()      pasted blocks never decide language or task
                               ├─ is_destructive()    regex safety net (+ JEV verdict)
                               ├─ catalog.prefilter() shared skill catalog → ≤ 5 candidates
                               ├─ classify()          JEV (1.8 s timeout, cached) or the built-in classifier
                               ├─ decide()            routes.json: task × difficulty → tier, then the cost cap
                               └─ render()            targets.json: tier → worker, model, effort
       + queue_state            earlier work still running? → "finish it first"
─► "[router] … lang=en model=opus effort=xhigh. Delegate to `opus-worker-xhigh` (opus, effort xhigh). Respond in English."
```

| Surface | Mechanism |
| --- | --- |
| Claude Code (CLI, desktop *Code*, Remote Control) | `UserPromptSubmit`, `Stop`, `SessionStart`, `SubagentStart`, `PostModelSwitch`, `PreToolUse` hooks |
| Codex (CLI, ChatGPT app) | `UserPromptSubmit` + `Stop` hooks |
| Antigravity (CLI, desktop app) | `PreInvocation` + `Stop` hooks (prompt read from the transcript, once per turn) |
| Claude desktop *Chat / Cowork* | MCP tool `route_prompt` |
| Claude Code on the web | the project hook with `--cloud-only` |
| Scripts and other agents | `trirouter route` (shim `~/.trirouter/bin/route.py`) |

**Model and effort are enforced, not suggested.** No tool lets a hook switch the running model, so trirouter
generates one worker agent per (model, effort) pair – Claude `<model>-worker-<effort>`, Codex roles
`<model>-<effort>`, Antigravity `gemini-flash-worker` / `gemini-pro-worker` – and the router delegates to the
right one. The catalog is `trirouter/config/models.json`; Claude is addressed by generic family aliases only
(haiku, sonnet, opus, fable), the `ultra` effort is never offered, and a daily background check picks up new
and retired models (`trirouter models`).

**Cost.** Most prompts are answered in the session itself, so the session's own model is the biggest lever:
`trirouter models --main=sonnet --subagent-model=sonnet` starts new Claude Code sessions on Sonnet and the router
escalates hard work to Opus / Fable workers. A `PreToolUse` guard holds each prompt to its routed subagent budget
(a retry passes). `trirouter daemon enable` serves Claude's hooks from one resident process (tens of milliseconds
instead of a Python start per prompt). `trirouter learn` trains a local model on JEV's past decisions; when it
routes like JEV, confident prompts skip the network call.

**Parallel agents:** 0 by default; +1 for two independent substantial parts, up to +4 for a from-scratch feature
with custom tooling. **Overrides:** Claude `#haiku #sonnet #opus #fable #codex #antigravity`, Codex / Antigravity
`#fast #main #deep`; `#norouter` / `#privat` skip routing entirely (nothing is sent to TypeSafe).

**Skills.** `~/.skills` is the one skill folder: Claude Code and Codex see it through per-skill links, Antigravity
through its `skills.json`; app-managed skills stay where they are but are indexed too, so a skill of one tool can be
handed to another. Before a third-party skill is linked, [SkillSpector](https://github.com/NVIDIA/SkillSpector)
(optional, `uv tool install git+https://github.com/NVIDIA/skillspector.git`) scans it: `SAFE` and `CAUTION` are
linked, `DO_NOT_INSTALL` skills are listed once and you choose which go to quarantine (`~/.trirouter/quarantine`,
deleted after 3 days; `trirouter quarantine` lists and restores them). Static analysis flags legitimate skills
too (7 of the 19 `anthropics/skills`), hence the question; `--allow-skill` and `--accept-flagged` remember your
answer. `--catalog-only=gke-*` keeps rarely used skills out of every session while the router still finds them.

## Configuration

| Setting | Where |
| --- | --- |
| JEV access | `trirouter setup --jev-token-stdin` (`cat key.txt \| trirouter setup --jev-token-stdin`), or `TYPESAFE_API_KEY` / `JEV_OPENROUTER_API_KEY`. The generic `OPENROUTER_API_KEY` is ignored on purpose. |
| Key storage | Windows: encrypted with DPAPI (this account only); macOS / Linux: `config.json` mode 0600. `doctor` reports the state; a plaintext key from an older version is re-saved protected by `setup`. |
| Per-user state | `~/.trirouter/`: `config.json`, `models.local.json`, `logs/`, `state/`, `quarantine/`, `completion/`, `bin/` (shims and the launcher) |
| Routing data | `trirouter/config/models.json` (catalog + policy), `routes.json` (task → tier, cost cap), `targets.json` (tier → worker) |

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `ROUTER_BACKEND` | `auto` | `jev`, `local` or `auto` (JEV when a key exists) |
| `JEV_TIMEOUT` | `1.8` | seconds for the whole JEV call; on timeout the local classifier decides |
| `ROUTER_MAX_EXTRA_AGENTS` | `4` | hard cap for parallel agents |
| `ROUTER_DISTILLED` | on | `0` switches the local distilled model off |
| `ROUTER_WELCOME` | on | `0` drops the welcome line of new Claude Code sessions |
| `ROUTER_LOG_PROMPTS` | unset | `1` logs full prompts (default: 200 characters; secrets are redacted everywhere) |
| `TRIROUTER_HOME` | `~/.trirouter` | the state folder (tests point it at a scratch folder) |

The other knobs (`ROUTER_MIN_CONFIDENCE`, `ROUTER_QUEUE_TTL_MIN`, `ROUTER_QUEUE_IDLE_MIN`, `ROUTER_CONTINUATION_TTL_MIN`,
`ROUTER_SKILL_CANDIDATES`, `ROUTER_DESTRUCTIVE_THRESHOLD`, `ROUTER_LONG_CONTEXT_THRESHOLD`, `TYPESAFE_API_URL`,
`JEV_MODEL`, `JEV_OPENROUTER_URL`, `JEV_OPENROUTER_MODEL`) are read at the top of `core.py` and `queue_state.py`.

## Guides

* **[docs/cli.md](docs/cli.md)** – every command, flag and example (generated from `trirouter/manual.py`).
* **[docs/remote-access.md](docs/remote-access.md)** – control your computer from a phone: one machine name, the
  connection opens at logon, sessions start when you open one.
* **[docs/speech-to-text.md](docs/speech-to-text.md)** – offline dictation into any app, phone dictation.

## Development

```
trirouter/        the package: core.py (router), hooks.py, cli.py + manual.py, hub.py (skills, agents), ...
trirouter/config/ models.json, routes.json, targets.json          tests/   pytest suite (~90 s)
eval/             labelled prompt sets + eval_router.py            docs/    guides, generated cli.md
```

```bash
pip install -e .[dev]
python -m trirouter sync --pre-commit   # = pyflakes + pytest + the routing evaluation, as the git hook runs them
```

Conventions, module notes and the tool facts that were verified live: [CLAUDE.md](CLAUDE.md),
[trirouter/CLAUDE.md](trirouter/CLAUDE.md), [tests/CLAUDE.md](tests/CLAUDE.md). Release history: [CHANGELOG.md](CHANGELOG.md).

## Limitations

* Hooks cannot switch the running model; Antigravity agents pin a tier, not an effort, so its model choice is advisory.
* Claude desktop *Chat/Cowork* is covered by the MCP tool only when the personal-preferences sentence is set; plain
  ChatGPT chat runs no hooks at all.
* On Windows the Codex remote daemon cannot detach inside a Job Object; a scheduled task runs the app server instead.
* The built-in classifier is weak on real traffic (typos, pasted text); JEV is the fix.

## License

[MIT](LICENSE.md). Product names are trademarks of their respective owners.
