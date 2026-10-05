# Working in this repository

jev-router: a prompt router for Claude Code, OpenAI Codex and Google Antigravity (see README.md).
Everything in the repository is English and generic: no personal data, machine names, account
details or absolute user paths. Per-user state belongs in `~/.jev-router/`, never in the repo.

## Answering

- Respond in the language of the user's prompt; the `[router]` context's "Respond in ..." line says which.
- Every prompt carries a `[router]` context from the global hook. Follow it: delegate to the named
  worker (e.g. `opus-worker-xhigh`), use the named skill, respect the `Parallelism:` line (the
  maximum number of EXTRA parallel agents; "none" means do not fan out).
- The router is a suggestion. If it is clearly wrong (e.g. long pasted text skews it), decide
  otherwise and say why in one line.
- `SAFETY:` in the context: list the exact actions and carry them out only after an explicit "yes".
- `QUEUE:` / `CONCURRENCY:` in the context: earlier work is still running – finish or protect it,
  never stop, restart or overwrite it.
- Overrides: Claude `#fable #sonnet #opus #codex #antigravity`; Codex / Antigravity
  `#fast #main #deep`; `#norouter` / `#privat` skip routing (nothing is sent to TypeSafe).

## Conventions

- Python 3.10+, standard library only (no third-party runtime dependencies). Must work on
  Windows, macOS and Linux – go through `jev_router/platforms.py` for paths, links and executables.
- Package layout: code in `jev_router/` (relative imports), data in `jev_router/config/`, worker
  templates in `jev_router/templates/agents/`, bundled skills in `jev_router/skills/`. One CLI
  (`trirouter <command>` = `python install.py <command>` = `python -m jev_router <command>`; the launcher
  only exists after the first setup); no stand-alone scripts. Commands, flags and help texts are data in
  `jev_router/manual.py`: after editing it run `trirouter help --markdown > docs/cli.md` (a test checks it).
- Hooks must never block or crash the host tool: catch everything, always exit 0.
- Model policy (`jev_router/config/models.json`): Claude only via generic aliases (fable / sonnet / opus),
  never Haiku, never a dated model ID; `ultra` effort is banned for every provider.
- Worker agents are generated from `jev_router/templates/agents/*.md` + `config/targets.json` +
  `config/models.json` (`trirouter skills --apply`, or `python install.py skills --apply`) – edit the sources, never the generated files.
- Installers are idempotent and dry-run by default; changed user config files get a `.bak` copy.

## Git

- One branch: `main`. No feature branches, worktrees or stashes that outlive a session – finished
  work is committed and pushed to `main` right away.
- Several sessions may work in this checkout at once (`CONCURRENCY:` in the context). Before
  committing, check `git status` / `git diff` for changes that are not yours: leave them unstaged and
  say so, unless the user asks to commit everything together. `git pull --rebase` before `git push`.
- Never discard or overwrite another session's uncommitted changes (`git checkout -- <file>`,
  `git reset --hard`, `git stash`).

## Checks before committing

```bash
python -m pytest tests -q        # includes the model-policy test
python eval/eval_router.py
```

Record user-visible changes in `CHANGELOG.md` (Keep a Changelog style).
