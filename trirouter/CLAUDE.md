# The trirouter package

Module map, the traps the code does not show, and what was verified about the three tools. Repo-wide conventions: `../CLAUDE.md`.

## Where things are

- `core.py` is the router: `route()` = override tag, continuation, classify (JEV, else the local keyword classifier /
  `distill.py`), `decide()` (`config/routes.json`, cost cap), `render()` (`config/targets.json`). Safety regex, JEV client,
  answer validation, decision cache and circuit breaker live here too. `lang.py` detects the language, `catalog.py` holds
  the skill catalog and its pre-filter.
- `hooks.py` is the hook for all three tools (`handle()` per event, `main()` for the command transport); `queue_state.py`
  and `store.py` keep its per-session state; `daemon.py` serves the same `handle()` over http for Claude, `hotreload.py`
  reloads the package under it and under `mcp_server.py` (tools in `mcp_tools.py`).
- `cli.py` runs the commands; `manual.py` is their only definition (parser, help pages, completion, `docs/cli.md`);
  `cliparse.py` validates; `interrupt.py` turns Ctrl+C / Ctrl+D into exit 130.
- `hub.py` (skills, worker agents), `integrations.py` (hooks, MCP entries, shims, launcher), `skillscan.py` (SkillSpector
  gate and quarantine), `discovery.py` (daily model check), `remote.py`, `completion.py`, `sync.py`, `doctor.py`,
  `secret.py` (key protection, redaction), `platforms.py` (OS abstraction; `platforms.STATE` is the state folder,
  `TRIROUTER_HOME` in tests).

## Traps

- The daemon and the MCP server re-import the package when a file changes: module-level state must survive that, and
  `keep=` in `HotReloader` names the modules that are never purged.
- `store.Store` rewrites a state file only under its lock. Without the lock a hook works on what it read: a lost
  update is fine, a rewrite from a stale read is not.
- Quarantine deletes only folders directly inside `~/.trirouter/quarantine` and never follows links; SkillSpector runs
  static (`--no-llm`) unless `skillscan.llm` is on, and without it skills are linked unscanned with a warning. A
  packaged Windows Python does not see uv's tool folder under `%APPDATA%`: the hint tells the user to reinstall
  SkillSpector with `UV_TOOL_DIR` outside AppData.
- The daemon binds 127.0.0.1 only. `GET /health`, `POST /hook/<provider>/<event>` and `POST /shutdown` (every POST
  with the `X-Trirouter-Token` header from `state/daemon.json`); a failing handler answers 200 empty. Claude's
  `SessionStart` stays a command hook because it starts the daemon.
- Hub layout: `~/.skills/<name>` is the real folder; `~/.claude/skills/<name>` and `~/.agents/skills/<name>` (Codex)
  link to it, Antigravity reads `~/.gemini/config/skills.json`. One link per skill, never one for the whole folder.
- Windows: a terminal inside a packaged (MSIX) app writes HKCU only to the app's private copy, so registry changes and
  the Antigravity setup run in a one-shot scheduled task; `codex remote-control start` cannot detach inside a Job
  Object, so a scheduled task runs the app server.

## Verified facts about the tools

- Claude Code (2.1.289): `claude --help` lists `--effort low|medium|high|xhigh|max` and `--model` aliases; `haiku` works
  too, but versions before 2.1.301 resolve it to Haiku 4.5.
- Codex (0.156.1): ChatGPT-plan logins reject gpt-6-astra, gpt-6-sol and gpt-5.6-sol, and gpt-5.5 answers 404;
  `codex debug models` lists the CLI's whole catalog, not what an account may use. A new hook runs only after it is
  trusted once (`codex` -> `/hooks`). Roles are `[agents.<model_>-<effort>]` in `config.toml` (`.` becomes `_`).
- Antigravity (agy 1.2.x): the effort is part of the model slug; an agent's `model` accepts only inherit / flash / pro /
  flash_lite and pins no effort; `agy --agent` keeps the session's model. `skills.json` paths must be absolute and
  links inside an entry are not followed. `PreInvocation` fires before every model call without the prompt text (read
  it from `transcriptPath`: a `USER_INPUT` line with `<USER_REQUEST>`); hooks run through `cmd /c`, so the hook calls
  the space-free shim.
