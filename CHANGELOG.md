# Changelog

All notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/);
dates are ISO 8601.

---

## [Unreleased]

### Added
- The `trirouter` command: `trirouter <command> [subcommand] [flags]` from any folder. Setup writes a
  launcher to `~/.jev-router/bin` (Windows: `trirouter.cmd` running `python.exe`, with that folder added
  to the user PATH through `HKCU\Environment`, type preserved, no duplicates, system PATH untouched,
  `WM_SETTINGCHANGE` broadcast; macOS / Linux: an executable plus a link in `~/.local/bin`, and the
  line for your shell profile is printed, never written). Dry-run aware, idempotent, removed by
  `uninstall`, shown by `doctor`. `python install.py ...` and `python -m jev_router ...` keep working.
- Man-page-like help for every command (NAME, SYNOPSIS, DESCRIPTION, OPTIONS with short/long form,
  value, default and whether it is remembered, CHANGES, EXAMPLES, EXIT STATUS, SEE ALSO):
  `trirouter help [command]`, `trirouter <command> --help`, `-h`. `trirouter` alone lists the commands.
  Short flags `-y`, `-n`, `-a`. Commands, flags and texts are data in `jev_router/manual.py`;
  `docs/cli.md` (getting started, common tasks, one section per command) is generated from it by
  `trirouter help --markdown`, and a test keeps the two identical. New commands `help` and `version`.
- Quarantine in one question: after all scans, the skills rated `DO_NOT_INSTALL` that nobody has decided
  on are listed and asked about once (`[a]ll / [n]one / [s]elect`, default none; `s` takes numbers like
  `1,3,5-8` and a final confirmation). A "none" is remembered for the skill's content as before;
  unattended runs still only warn.
- Quarantined skills are deleted for good after 3 days (`skillscan.quarantine_days`,
  `--quarantine-days=N`, `0` = never). Each entry gets a sidecar `~/.jev-router/quarantine/<name>.json`
  (name, quarantined_at, purge_after, risk, max_severity). The purge runs at every `setup` and
  `skills --apply` (dry runs only report) and, at most every 6 hours, from a new Claude `SessionStart`
  hook that prints nothing and cannot fail. It only ever deletes folders directly inside the quarantine
  folder, never follows a link and clears read-only files on Windows.
- `trirouter quarantine [list]`, `quarantine restore <name>...` (back to `~/.skills`, allowed in
  `skillscan.allow`) and `quarantine purge [--all]`; `doctor` reports the number of entries and the next
  purge date.

- Skill security scan with NVIDIA [SkillSpector](https://github.com/NVIDIA/SkillSpector): `setup`
  (step 4) and `skills` scan every third-party skill in `~/.skills` before it is linked into a tool.
  On `DO_NOT_INSTALL` you are asked (default: no) whether to move it to `~/.jev-router/quarantine/`
  (linked nowhere, left out of the catalog); a "no" is remembered until the skill changes, and
  unattended runs only warn, because static analysis also flags legitimate skills (7 of the 19
  `anthropics/skills`). `CAUTION` only warns. `--allow-skill=<name>` silences a skill and restores it
  from quarantine; `--scan-llm=on` adds SkillSpector's LLM analysis to the default static (`--no-llm`)
  scan. Both are remembered in `config.json` (`skillscan`). Verdicts, failed scans included, are
  cached per content hash, scanner version and mode; new or changed skills are scanned in parallel (up
  to 8 at a time: a first scan of 12 skills took 21 s instead of 78 s), and each outcome is reported in
  one summary line instead of a line per skill. After a review, `--accept-flagged` keeps every skill
  now rated `DO_NOT_INSTALL` without asking, bound to its current content (a skill's publisher cannot
  be verified, so there is no allow-by-source list). SkillSpector is optional (Python 3.12+,
  `uv tool install`): without it skills are linked unscanned with a warning.
- JEV through OpenRouter: an OpenRouter key (`--jev-token=sk-or-...` or `--openrouter-key=...`, or
  `JEV_OPENROUTER_API_KEY`) reaches JEV's System One API on OpenRouter (model `jev-1.13`). A TypeSafe
  token still wins when both exist. The generic `OPENROUTER_API_KEY` is ignored on purpose, so another
  tool's key never starts spending credit on routing. `doctor` shows which channel is active.
- Follow-ups keep the previous decision: a bare go-ahead or status check ("mehet", "igen, töröld!",
  "yes, do it", "hogy állunk?") re-uses the same session's last model, effort and worker instead of
  being classified on its own. The safety check still runs on the new prompt. The last decision is
  kept per session for `ROUTER_CONTINUATION_TTL_MIN` (default 180) and dropped at `SessionEnd`.
- Claude hooks `StopFailure` and `SessionEnd` release the queue protection, so a turn that died on an
  API error no longer makes the next prompt see a false "QUEUE" warning.
- Claude hook `SubagentStart` logs which worker the model really started; `doctor` reports the
  delegation compliance against the router's advice.
- `eval/real_prompts.csv`: 45 anonymized shapes of real traffic (typos, missing accents, pasted
  blocks, go-aheads with an earlier prompt). The eval now also reports the "routing uncertain" share
  and the tier accuracy next to the best fixed-tier baseline, with optional `lang` and `previous`
  columns. On the real-traffic set task accuracy went from 44 % to 71 %, uncertain decisions from
  67 % to 42 % and reply language from 87 % to 100 %, without tuning the vocabulary. The eval drops
  any local `ROUTER_*` setting first, so its numbers always match CI.

### Changed
- Every command validates its flags: an unknown or not-applicable flag, a value flag without a value
  or a boolean flag given a value is a usage error (exit 2) with a "did you mean" suggestion, where
  flags used to be global and silently ignored. `python install.py` without a command still runs the
  interactive setup; `trirouter` without a command prints the command list. `route` parsing is unchanged.
- Hints in `doctor`, `setup`, the remote setup and the skill scan name `trirouter ...` once the launcher
  exists (otherwise `python install.py ...`); the README and guides use the `trirouter` form.
- JEV's destructive question leaves out drafts, planning and purchase advice ("write an email to my
  colleague", "which domain should I buy?"): JEV's false positives on the eval sets fell from 4 / 4 / 5 %
  to 1 / 2 / 2 % (Hungarian / English / real traffic), recall unchanged at 100 %.
- Pasted blocks (`<pasted_content>`) no longer decide the answer language or the task type; JEV
  still gets them as context and the safety regex still scans them. Subagent hand-backs
  (`<agent-message>`) are no longer routed.
- A project with only an `AGENTS.md` now counts as a project for the cross-project heads-up (Claude
  Code reads `AGENTS.md` when there is no `CLAUDE.md`).
- Renamed the package and command to `trirouter`: `jev-router` is taken by unrelated packages on npm
  and PyPI. Internal names (`jev_router`, `~/.jev-router/`, the `jev-router` MCP server) stay the
  same, so existing installs keep working.
- Removed `gpt-5.4` from the Codex catalog (dropped from Codex's bundled catalogs in 0.158).

### Fixed
- A go-ahead is answered in its own language: "Mehet" after a prompt that was only a pasted English
  block asked for an English reply, because a continuation kept the previous language. A go-ahead
  with Hungarian or English words now sets the language; an ambiguous one ("ok", "lgtm") keeps it.
- Skill scan: a SkillSpector that does not start (e.g. a uv trampoline under a packaged Windows
  Python that cannot see `%APPDATA%`) now gives one warning with the reason and a `UV_TOOL_DIR` hint,
  and links skills unscanned, instead of caching a failed scan for every skill.
- Links into a previous checkout (`<old>/jev_router/skills/<bundled skill>`) are re-pointed to the
  current checkout instead of being skipped as foreign; Antigravity's `skills.json` drops another
  checkout's `jev_router/skills` entry.
- Antigravity in plan mode was never routed: agy stores the prompt as `/plan <prompt>`, which the hook
  took for a slash command and skipped. The `/plan` prefix is now stripped.
- Cross-project heads-up on Linux/macOS: a Windows-style path in the prompt (`C:\...`) is not rooted
  there, so its only existing "ancestor" was `.` - the hook's own working directory - and a session
  started inside any project got a false "different project" note (it also failed CI on Linux/macOS).
  Candidates that are not rooted on the current OS are now skipped.

### Changed
- Packaging polish for external users: the README quick-start's `git clone` placeholder now has the
  real repository URL; `pyproject.toml` gets `keywords`, `[project.urls]` (Homepage/Repository/
  Issues/Changelog) and broader classifiers (tested Python versions, intended audience, topic) -
  deliberately no `authors` entry, to keep the package name/attribution-free.
- Local mock classifier accuracy: 91%/90% (hu/en) -> 100%/100% on `eval/hu_prompts.csv` /
  `eval/en_prompts.csv`, tuned against the exact confusions in `eval/results_*.csv`, not guessed:
  added missing task vocabulary (Hungarian `pushol`/`konfigurác`/`felülír`, `architektúra`/
  `microservice`, `szórás`/`variance`, `squared`/`cubed`, `küldj`/`send`/`message`/`Slack`,
  English "currently" and "cost"/"right now" for research, `bugs` plural); reordered the tie-break
  priority (`test, math, study, research, code, qa, general`) so an exam/course context or a
  freshness word outranks one incidental tech-keyword match, and code outranks qa's generic
  interrogative opener alone; added a narrow extra-weight rule (`LOCAL_TASK_STRONG_RE`) so "mi az a
  X" / "what is a/an X" (an INDEFINITE article - a defining question) outranks a bare tech-keyword,
  without over-triggering on "what is THE latest/current/standard ..." research and math questions.
- Cross-project heads-up: when a prompt names an absolute path into a *different* project (one with
  its own `CLAUDE.md`/`.claude/`) than the session's own working directory, the injected `[router]`
  context now adds a one-line `Note:` explaining that Workflow/Agent tool custom subagent types are
  scoped to this session's own root, not to that other path. Purely additive and best-effort:
  `core.foreign_project_note()` never raises and is not required for routing to work - a path it
  cannot resolve, or any internal failure, silently yields no note. Handles path components with
  spaces (e.g. `7. Félév`).
- `route` command – `python install.py route [--provider claude] [--json] <prompt text...>` (no text:
  read from stdin) – returns the router's decision for one prompt or sub-task: the rendered
  `[router] …` text, or with `--json` one object with `model`, `effort`, `agent`, `tier`, `task`,
  `difficulty`, `extra_agents`, `destructive`, `skill`, `verify`, `lang`, `backend`, `text` and `note`.
  Side-effect free (no queue state, no log); `#norouter` / `#privat` are not routed and nothing is
  sent to TypeSafe. Exit codes: 0 success, 2 usage error, 1 unexpected error (exception type only).
- `~/.jev-router/bin/route.py` shim, written by the installer next to the hook and MCP shims, so
  other projects can run `python ~/.jev-router/bin/route.py --json "<text>"` without knowing where
  the repository lives.
- `doctor` checks remote access (tasks, servers, the Antigravity autostart entry, the Codex remote
  connection and whether this Codex version still accepts `app-server --remote-control`) and that
  the Python interpreter the hooks and MCP entries point at still exists.
- Windows watchdog task `JevRouter-Watchdog` (at logon and every 30 minutes): re-hides the
  Antigravity autostart entry after an agy update rewrote it and restarts anything not running.
- The installer warns when it changes the Claude desktop config while the app runs – the app
  rewrites the file from memory, so the app has to be restarted.
- Antigravity worker agents: `gemini-flash-worker` and `gemini-pro-worker` in
  `~/.gemini/config/agents/<name>/agent.md` (model tier `flash` / `pro`, subagent only – Antigravity
  agents cannot pin an effort, and as the main agent they keep the session's model). The deep tier
  delegates to `gemini-pro-worker`; `doctor` counts them.

### Changed
- Worker templates are per role instead of per model: `fast`, `balanced`, `deep` and `test-worker.md`,
  chosen by each model's `role` in `models.json`. Codex roles get their model's instructions and
  description (before, all 20 shared one generic text); a new model needs one line, not a template.
  Generated descriptions read "… tasks. Fixed model …" (the full stop was missing).
- The routing log records the worker a decision delegates to (`target_agent`).
- Windows remote access runs entirely in the background from logon: Claude Remote Control and the
  new Codex remote app server (`codex app-server --remote-control --listen off`, task
  `JevRouter-CodexRemote`) run under `conhost.exe --headless`, and Antigravity's autostart entry is
  wrapped the same way – no terminal window opens at logon.
- The ChatGPT desktop app is no longer started at logon (task `JevRouter-ChatGPT` is removed);
  opened by hand it works as usual.

### Fixed
- Windows: the MCP server is registered with `pythonw.exe` – a host without a console (the
  detached Antigravity remote daemon) opened a terminal window for `python.exe`.
- The loop scripts in `~/.jev-router/bin` were written with `\r\r\n` line endings.
- Windows: registry changes and the Antigravity setup run in a one-shot scheduled task, so they take
  effect even when the installer runs in a terminal of a packaged (MSIX) app such as the Claude
  desktop app, where HKCU writes only land in the app's private copy.
- Tests no longer depend on the user's `ROUTER_*` environment variables (a local
  `ROUTER_MIN_CONFIDENCE` made the `route` tests pass locally and fail in CI).
- Windows: the Antigravity daemon started by the installer is restarted from its (hidden)
  autostart entry – started detached it had no console, so every hook or MCP server it ran
  opened a terminal window.
- Windows: an unattended installer run with stdin from the null device (`< NUL`, Git Bash's
  `< /dev/null`) waited forever at the JEV token question – `isatty()` is True for NUL. Questions
  are now asked only on a real console; everywhere else the defaults apply.
- Skill catalog: a SKILL.md whose `description:` value starts on the next (indented) line is indexed
  with its whole description instead of only the first line, and an empty `name:` no longer takes
  the next line as the name.
- Code quality: every SonarQube finding resolved – complex functions split into smaller ones, and
  the frontmatter, transcript and TOML patterns rewritten so they run in linear time (no
  backtracking). Routing decisions and installer output are unchanged.

## [3.1.0] – 2026-09-24 · Standard package layout

### Changed
- Code moved from the flat `router/` scripts into the `jev_router` package with relative imports:
  `cli`, `core`, `lang`, `catalog`, `hooks`, `queue_state`, `mcp_server`, `hub`, `integrations`,
  `platforms`, `remote`, `doctor`. Data in `jev_router/config/`, worker templates in
  `jev_router/templates/agents/`, bundled skills in `jev_router/skills/`.
- One CLI: `python install.py <command>` = `python -m jev_router <command>` = `jev-router <command>`
  (after `pip install -e .`). New commands `skills` and `doctor`.
- `pyproject.toml` (metadata, console script, pytest settings); CI installs the package.
- Hook and MCP shims run the package modules (`jev_router.hooks`, `jev_router.mcp_server`).
- The generated Codex agents block is recognised by its marker prefix, so marker wording changes
  never duplicate the block; stale Antigravity `skills.json` paths are dropped.

### Removed
- Duplicate tools: `router/probe_models.py` (→ `install.py models --probe`),
  `scripts/check_tools.py` (→ `install.py doctor`), `scripts/check_models.py` (→ `tests/test_policy.py`),
  the modules' own `__main__` CLIs, the unused `should_skip` helper and legacy migration code.

## [3.0.1] – 2026-09-24 · Cross-platform review fixes

### Fixed
- macOS: the launchd plist is written with `plistlib` (names with `&`/`<` no longer break it), gets
  the caller's `PATH` (npm/Homebrew/nvm `claude` finds `node`), a 30 s throttle and a log file, and
  is loaded with `launchctl bootout`/`bootstrap` and verified with `launchctl print`.
- Linux: systemd `%`/`$`/quote escaping; `restart` after `enable`, so a new name or folder takes effect.
- Queue: a turn cancelled with Esc (Claude runs no `Stop` hook then) no longer blocks the next prompt –
  entries of a session whose transcript has been silent for `ROUTER_QUEUE_IDLE_MIN` (default 10) are
  dropped; Antigravity `Stop` clears only when `fullyIdle`; the lock is released only by its owner;
  atomic state writes; invalid `ROUTER_QUEUE_TTL_MIN` no longer crashes the hook.
- Installer: unknown sub-commands are rejected; without a terminal and without `--yes` nothing is
  moved; `remote --remove --dry-run` changes nothing; the token file is created owner-only; a config
  file with invalid JSON is reported and skipped instead of aborting; the first `.bak` is never
  overwritten; migration only touches the selected tools' skill folders.
- Hook commands are shell-safe (quoted on POSIX, short paths on Windows); MCP entries use the plain
  interpreter path; a virtualenv's base interpreter is used so hooks survive the venv's removal.
- Link ownership uses real path containment (`~/.skills-old` is not `~/.skills`); `LOCALAPPDATA`
  is only consulted on Windows.

## [3.0.0] – 2026-09-24 · Open-source release

### Added
- **Cross-platform installer** `install.py` (Windows, macOS, Linux): detects Claude Code, Codex and
  Antigravity, reports install / login state and guides the login, asks for an optional JEV token,
  connects hooks + MCP for every logged-in tool, migrates all tools' skills into `~/.skills`,
  generates workers, offers remote access and speech-to-text. Sub-commands `detect`,
  `models --probe`, `remote`, `uninstall`; flags `--yes`, `--dry-run`, `--providers`, `--jev-token`.
- **Queue protection** (`router/queue_state.py`): `Stop` hooks for all three tools; a prompt sent
  while earlier work in the same session is unfinished gets a `QUEUE:` instruction, a prompt in a
  folder where another session is working gets a `CONCURRENCY:` warning. Entries expire after
  `ROUTER_QUEUE_TTL_MIN`.
- **OS abstraction** (`router/platforms.py`): junctions on Windows, symlinks on macOS/Linux; config
  locations per OS; executable discovery; login detection.
- **Optional remote-access module** (`router/remote.py`, `docs/remote-access.md`) with a
  user-chosen machine name (default: hostname) and working folder (`--workdir`, must be a folder
  Claude Code trusts – never the home directory), stored in `~/.jev-router/config.json`.
- JEV token in `~/.jev-router/config.json` (environment variable still wins).
- Per-account model availability: `python install.py models --probe` writes
  `~/.jev-router/models.local.json`, which overrides the catalog's defaults.
- `docs/speech-to-text.md` (install, model choice by hardware, phone dictation).
- CI on Windows, macOS and Linux.

### Changed
- License: **MIT** (was proprietary).
- All documentation rewritten in English and made generic; no personal or machine data.
- Skill migration covers every tool's personal skill folder, not only Claude's.
- Hooks call the absolute Python interpreter (unless its path contains spaces).

### Fixed
- Codex `config.toml`: removing the MCP section no longer swallows the comment line that starts the
  generated agents block (which led to duplicate `[agents.*]` tables); the section is replaced in place.

### Removed
- Windows-only `scripts/setup-windows.ps1` and `start-rc.cmd` (replaced by `install.py`).

## [2.3.0] – 2026-09-24

### Added
- Parallel-agent decision (0 – +4) with code-level clamps.
- JEV `model` question over every selectable model; effort clamped to the chosen model.
- Model-named workers: Claude `<model>-worker-<effort>`, Codex roles `<model>-<effort>`.

### Changed
- `ultra` effort banned for every provider.
- Codex catalog verified per model; `codex debug models` shows the CLI's catalog, not what an
  account may use.
- Codex's own `config.toml` tables (hook trust) are kept outside the generated agents block.
- Antigravity `skills.json` lists the repository's `skills/` too (agy does not follow links).

## [2.0.0] – 2026-09-23

### Added
- Global hooks for Claude Code, Codex and Antigravity via shims in `~/.jev-router/bin`.
- Shared skill folder `~/.skills` and a machine-wide skill catalog with a Hungarian → English glossary.
- Stdio MCP server (`route_prompt`, `list_skills`, `get_skill`) for hook-less modes.
- Hungarian / English language detection; answer language follows the prompt.
- 100 + 100 labelled evaluation prompts; pytest suite; health report.

### Fixed
- The global hook read its configuration from the current project instead of the repository.
- Subagents existed only inside the repository.
- Destructive-request false positives ("remove the unused import", "sort in order").
- Math false positive on OAuth codes; invalid Codex effort levels; Antigravity hook firing on every
  model call; harness notifications being routed; secrets in the routing log.

## [1.0.0] – 2026-09-23

- Initial router: Claude Code hook, routing table, subagents, cloud mode, model-family policy,
  local keyword classifier with JEV fallback.

---

## Known limitations

- Hooks cannot switch the running model; model choice is enforced through generated workers.
  Antigravity has no fixed-model agents, so its model choice is advisory.
- Claude desktop *Chat/Cowork* and ChatGPT's plain chat run no hooks. Chat/Cowork is covered by the
  MCP tool (called when the personal-preferences instruction is set); plain ChatGPT chat is not.
- The Claude desktop `/` menu does not list link-based skills; the model still loads them.
- Antigravity `skills.json` paths must be absolute, and links inside a skills entry are not followed.
- On Windows, the Codex remote-control daemon cannot detach when every process runs inside a Job
  Object; the ChatGPT desktop app hosts remote access instead.
- The built-in classifier weighs pasted text like the user's own words; very long pasted English text
  can make it pick English or a higher difficulty for a Hungarian request.
