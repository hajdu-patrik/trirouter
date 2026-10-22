# The trirouter package

Module map, the traps the code does not show, and what was verified about the three tools. Repo-wide conventions: `../CLAUDE.md`.

## Where things are

- `core.py` is the router: `route()` = override tag, continuation (a go-ahead keeps the last decision), classify (JEV, else the local
  keyword classifier / `distill.py`), `decide()` (`config/routes.json`, cost cap), `render()` (`config/targets.json`). Safety regex, JEV
  client, answer validation, decision cache and circuit breaker live here too.
- `hooks.py` is the hook for all three tools (`handle()` per event, `main()` for the command transport); `queue_state.py` and `store.py`
  keep its per-session state; `daemon.py` serves the same `handle()` over http for Claude, `hotreload.py` reloads the package under it.
- `cli.py` runs the commands; `manual.py` is their only definition (parser, help pages, completion, `docs/cli.md`); `cliparse.py` validates.
- `hub.py` (skills, worker agents), `integrations.py` (hooks, MCP entries, shims, launcher), `skillscan.py` (SkillSpector gate and
  quarantine), `discovery.py` (daily model check), `remote.py`, `completion.py`, `sync.py`, `doctor.py`, `secret.py` (key protection).
- `platforms.py` is the OS abstraction (paths, links, executables, processes, atomic writes, locked appends). `legacy.py` and the
  top-level `jev_router/` package exist for installs from before the rename; remove them together after the next release.

## Traps

- A hook must never fail the host: every entry point is `guarded`, the shim exits 0, an error is logged and the prompt just gets no
  router context.
- The daemon and the MCP server re-import the package when a file changes: module-level state must survive that, and `keep=` in
  `HotReloader` names the modules that are never purged. State folders come from `legacy.state_dir()`; set `TRIROUTER_HOME` in tests.
- `store.Store` rewrites a state file only under its lock. Without the lock a hook works on what it read: a lost update is fine, a
  rewrite from a stale read is not.
- After editing `manual.py` run `python -m trirouter sync`: it regenerates `docs/cli.md`, completion, agents and the skill catalog.
- Config JSON holds only keys the code reads. Claude models are generic family aliases (never a dated ID), `ultra` effort is stripped
  everywhere. Worker agents and Codex roles are generated from `templates/agents`, `targets.json` and `models.json`: edit the sources.
- Quarantine deletes only folders directly inside `~/.trirouter/quarantine` and never follows links; SkillSpector runs static
  (`--no-llm`) unless `skillscan.llm` is on, and without it skills are linked unscanned with a warning.
- The daemon binds 127.0.0.1 only. `GET /health`, `POST /hook/<provider>/<event>` and `POST /shutdown` (every POST with the
  `X-Trirouter-Token` header from `state/daemon.json`); a failing handler answers 200 empty, as if the daemon were down. Claude's
  `SessionStart` stays a command hook because it starts the daemon.
- Hub layout: `~/.skills/<name>` is the real folder; `~/.claude/skills/<name>` and `~/.agents/skills/<name>` (Codex) link to it,
  Antigravity reads `~/.gemini/config/skills.json`. One link per skill, never one for the whole folder (app-managed content lives there).

## Verified facts about the tools

- Claude Code (2.1.289): `claude --help` lists `--effort low|medium|high|xhigh|max` and `--model` aliases; `haiku` works too.
- Codex (0.156.1): ChatGPT-plan logins reject gpt-6-astra, gpt-6-sol and gpt-5.6-sol, and gpt-5.5 answers 404; `codex debug models`
  lists the CLI's whole catalog, not what an account may use. A new hook runs only after it is trusted once (`codex` -> `/hooks`);
  `codex exec` skips it silently until then. Roles are `[agents.<model_>-<effort>]` in `config.toml` (`.` becomes `_`).
- Antigravity (agy 1.2.x): the effort is part of the model slug; an agent's `model` accepts only inherit / flash / pro / flash_lite and
  pins no effort; `agy --agent` keeps the session's model; `.agents/agents/` in a workspace is ignored. `skills.json` paths must be
  absolute and links inside an entry are not followed. `PreInvocation` fires before every model call without the prompt text (read
  it from `transcriptPath`: a `USER_INPUT` line with `<USER_REQUEST>`); hooks run through `cmd /c`, so quoted paths with spaces
  break and the hook calls the space-free shim.
