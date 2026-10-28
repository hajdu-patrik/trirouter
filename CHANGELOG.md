# Changelog

All notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/); one line per
change, what changed rather than how; dates are ISO 8601.

## [Unreleased]

### Fixed
- The MCP server answers resource and prompt list requests with an empty list instead of an error, so clients that
  ask anyway (Codex) no longer log one.
- Tab completion offers quarantined skill names after `quarantine restore`, the tools for `--providers` (also after a
  comma), folders for `--workdir` in zsh and fish, and complete, readable flag descriptions.
- Fish completion no longer mixes up commands when a later word names another command (`help setup`, a route prompt).
- PowerShell: calling `TabExpansion2` without a cursor column works again with the completion loaded.
- `setup --providers` skips a tool that is not installed or not logged in instead of connecting it.
- `setup`, `remote` and `daemon enable` exit with status 1 when a step failed; `remote` says so when no tool is
  installed or nothing was set up to remove.
- A `config.json` that is not valid JSON is kept as `config.json.corrupt` before a command writes a new one.
- `remote`, `uninstall` and `remote --remove` stop only trirouter's own restart loops and the servers they started;
  a Claude remote-control session started by hand or by another task keeps running. Removal deletes the loop scripts
  and forgets the remote name, so `doctor` no longer reports a removed setup.
- `setup --dry-run --providers=...` still previews the named tools when they are not installed.
- `--quarantine-days` accepts at most 36500; a larger saved value no longer breaks `quarantine` and `doctor`.
- Choice values are case-insensitive end to end (`route --provider=CODEX` routes like `codex`).
- A subcommand may follow flags (`daemon -n enable`), as the conventions say.
- `quarantine purge --all --yes` documents that it deletes without asking.
- `setup --dry-run` no longer claims tab completion was installed; `models --main=off` creates no empty
  `settings.json`; the skill catalog is rewritten only when it changed; `learn` says when it saved nothing.
- Usage errors of `route` (empty prompt) and `sync --check --pre-commit` go to stderr with the program name, and an
  unknown command points at `help` instead of the setup command.

## [3.2.0] – 2026-10-23 · trirouter

### Added
- Renamed from jev-router to **trirouter** everywhere: package `trirouter/`, state folder `~/.trirouter/`, MCP server
  `trirouter`, remote-access tasks `\Automations\Trirouter - *`. An install from before the rename is not
  migrated: copy `~/.jev-router/config.json` to `~/.trirouter/`, run `python install.py` (it writes the hooks, MCP
  entries, launcher and services fresh), then delete `~/.jev-router`.
- The `trirouter` command (launcher in `~/.trirouter/bin`, on the user PATH / linked from `~/.local/bin`) with
  man-page help for every command (`trirouter help <command>`, `--help`), flag validation with "did you mean" hints,
  short flags `-y -n -a`, `--version`, and shell tab completion for PowerShell, bash, zsh and fish.
- `trirouter sync`: refreshes the generated files (tab completion, worker agents, skill catalog, docs/cli.md) and sets
  the git pre-commit hook, which runs `sync --pre-commit` (sync, tests, routing evaluation). `--check` for CI.
- `trirouter daemon enable`: Claude Code's hooks go over http to one resident router process (tens of milliseconds
  per hook instead of a Python start); hot-reloads source and config; `SessionStart` stays a command hook.
- `trirouter models --main=FAMILY --subagent-model=FAMILY`: Claude Code's default session and subagent model; a
  `PreToolUse` guard holds each prompt to its routed subagent budget and gives model-less `general-purpose` calls the
  routed worker's family.
- `trirouter learn`: a local Naive Bayes model distilled from JEV's logged decisions; when cross-validation shows it
  routes like JEV, confident prompts skip the network call. Retrained daily.
- Daily model discovery: new and retired models of all three tools, tried once, recorded in `models.local.json`;
  `trirouter models [--discover] [--auto=on|off]`.
- Skill security scan with SkillSpector before a skill is linked; a quarantine with a 3-day retention and the
  `quarantine list | restore | purge` commands; `--allow-skill`, `--accept-flagged`, `--scan-llm`, `--catalog-only`.
- Continuations: a go-ahead ("mehet", "yes, do it", "commit it") keeps the previous decision; questions about the
  session are answered inline; pasted blocks never decide the language or the task.
- JEV through OpenRouter (`JEV_OPENROUTER_API_KEY` or an `sk-or-` key); a circuit breaker after two failures; answers
  validated against their questions and cached by request hash; secrets redacted before anything leaves the machine.
- `trirouter route [--json]`: the routing decision for one prompt, side-effect free, also through the
  `~/.trirouter/bin/route.py` shim.
- `trirouter remote --start-session=on|off`; at logon only the connection opens, sessions start on demand.
- Clean interruption of every command (Ctrl+C / Ctrl+D: exit 130, children ended, files written atomically).
- Claude hooks `StopFailure`, `SessionEnd`, `SubagentStart`, `PostModelSwitch`; `doctor` reports delegation compliance
  and the agent guard's actions.
- Machine-readable `model=` / `effort=` fields in the `[router]` header; `route --json` and the MCP `Decision:` line.
- Haiku is routable like the other families; a `fast` tier on haiku, `#haiku` / `#fable` overrides.

### Changed
- Cost-aware routing: the in-session tier never delegates, trivial requests stay in-session, a cost cap per
  difficulty limits the model family, short plain prompts never run above Haiku.
- Lower latency: JEV timeout 1.8 s, short or locally certain prompts skip the network call, very long prompts are
  clipped for classification, the skill index is cached.
- Logs rotate and are owner-only; state writes are atomic and locked; a hook that cannot take a lock proceeds
  without rewriting shared state.
- Windows remote access runs windowless from logon (`conhost --headless`), with a watchdog task every 30 minutes.
- Worker templates are per role (`fast`, `balanced`, `deep`, `test`), chosen by each model's `role` in models.json.
- Setup stores the JEV key protected (Windows DPAPI, mode 0600 elsewhere), scrubs old backups and prints a note when a
  key is given on the command line (`--jev-token-stdin` is the safe form).
- `setup --yes` answers every question with its default; `--providers` validates its names and asks only those tools
  to log in; `--no-completion` leaves the shell profiles alone.
- Override tags count only at the start or the end of the user's own words, never inside prose, pasted or quoted text.
- The documentation is short: README, guides and CLAUDE.md files cut to the essentials; docs/cli.md is generated.

### Removed
- The `jev_router` compatibility package, `legacy.py` and every rename-aware code path (old task, MCP and PATH names).
- The `keys` command (`setup` protects the key, `doctor` reports it), `help --markdown` (`sync` generates docs/cli.md),
  `sync --dry-run` (`--check`), the `detect`, `completion` and `version` commands, `models --probe`. The old
  invocations give a usage error that names the replacement.

### Fixed
- Secrets were redacted after the queue summary and the log line were cut, so a cut key could slip through.
- A routing failure dropped the QUEUE / CONCURRENCY protection and the foreign-project note.
- Transcript lines were split on U+2028 / U+2029 as well, losing the newest Antigravity prompt or the session model.
- A skill path with `..` or control characters could be quoted to the model; a damaged `models.local.json` crashed
  rendering; the keep-alive retry could take another stale pooled connection; the MCP server died on a failed
  first load; `doctor` and the daemon failed on wrongly typed JSON.
- `-nh` ran the command instead of showing help; `--quarantine-days=²` crashed; `sync --pre-commit --check` wrote
  files; `uninstall -n` claimed to have removed things; `--workdir=~/x` was not expanded.
- The routing evaluation ran against the developer's real `~/.trirouter` (JEV key, catalog, trained model).
- Antigravity turns are de-duplicated under the store lock; hub config writes keep a `.bak`.

## [3.1.0] – 2026-09-24
- Standard package layout (`jev_router/` with relative imports, `pyproject.toml`), one CLI, `skills` and `doctor`
  commands; duplicate scripts removed.

## [3.0.1] – 2026-09-24
- Cross-platform fixes: macOS launchd and Linux systemd services, queue entries of a silent session, installer
  robustness, shell-safe hook commands, real-path link ownership.

## [3.0.0] – 2026-09-24
- Open-source release (MIT): cross-platform installer, queue protection, OS abstraction, remote access, per-account
  model availability, CI on Windows, macOS and Linux.

## [2.3.0] – 2026-09-24
- Parallel-agent decision, JEV model question, model-named workers, `ultra` banned.

## [2.0.0] – 2026-09-23
- Global hooks for all three tools, shared skill folder and catalog, MCP server, language detection, evaluation sets.

## [1.0.0] – 2026-09-23
- Initial router: Claude Code hook, routing table, subagents, cloud mode, local classifier with JEV fallback.
