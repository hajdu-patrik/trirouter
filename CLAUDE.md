# Working in this repository

trirouter: a prompt router for Claude Code, OpenAI Codex and Google Antigravity (see README.md). Everything in the
repository is English and generic: no personal data, machine names, account details or absolute user paths. Per-user
state belongs in `~/.trirouter/`, never in the repo. Module notes and traps: `trirouter/CLAUDE.md`; tests: `tests/CLAUDE.md`.

## Answering

- Respond in the language of the user's prompt; the `[router]` context's "Respond in ..." line says which.
- Every prompt carries a `[router]` context from the global hook. Follow it: delegate to the named worker (e.g.
  `opus-worker-xhigh`), use the named skill, respect the `Parallelism:` line (the maximum number of EXTRA parallel
  agents; no such line means none). The router is a suggestion: if it is clearly wrong, decide otherwise and say why.
- `SAFETY:` in the context: list the exact actions and carry them out only after an explicit "yes".
- `QUEUE:` / `CONCURRENCY:` in the context: earlier work is still running – finish or protect it, never stop,
  restart or overwrite it.

## Conventions

- Python 3.10+, standard library only. Windows, macOS and Linux: paths, links, processes and executables go through
  `trirouter/platforms.py`.
- Code in `trirouter/`, data in `trirouter/config/`, worker templates in `trirouter/templates/agents/`, bundled skills
  in `trirouter/skills/`. One CLI: `trirouter <command>` = `python install.py <command>` = `python -m trirouter
  <command>`. Commands, flags and help texts are data in `trirouter/manual.py`; after editing it run
  `python -m trirouter sync` (regenerates docs/cli.md, completion, agents, the skill catalog).
- Hooks never block or crash the host tool: catch everything, always exit 0.
- Model policy (`config/models.json`): Claude only via generic family aliases, never a dated model ID; `ultra` is banned.
  Worker agents are generated from the templates + `targets.json` + `models.json`: edit the sources, never the output.
- Installers are idempotent, show every change first and keep a `.bak` of a changed user config; `skills` previews
  unless `--apply` is given, the others preview with `--dry-run`.

## Code style

- Comments and docstrings: at most two lines, only where the code cannot say it (a non-obvious why, a trap, an outside
  constraint). No decision history: that is the commit message and `CHANGELOG.md`.
- Less code beats clever code: remove what nothing calls, share a helper only when it reads at least as well.
  pyflakes stays silent. The `coding-principles` skill and agent in `.claude/` run a clean-code pass.
- One function, one job: a function that parses, decides and reports is split into named steps. Cyclomatic complexity
  stays below 21 per function (`radon cc trirouter -n D` prints nothing); a refactor never changes behaviour or output.
- Plain functions and data tables, no class hierarchies or patterns for their own sake: a class only where state and
  behaviour belong together.
- One definition per fact: `config.json` is read only through `platforms.read_config`, paths and processes only
  through `platforms.py`, commands and flags only through `manual.py`.
- Every change ships with its test, and a bug fix with the test that would have caught it. New code in a weakly
  covered module (`doctor`, `remote`, `platforms`) raises that module's coverage, never lowers it.
- Hashes that protect nothing say so (`usedforsecurity=False`); CI actions are pinned to a commit of their latest
  release.

## Git

- One branch: `main`. No feature branches, worktrees or stashes that outlive a session – finished work is committed
  and pushed to `main` right away.
- Several sessions may work in this checkout at once (`CONCURRENCY:` in the context). Before committing, check
  `git status` / `git diff` for changes that are not yours: leave them unstaged and say so, unless the user asks to
  commit everything together. `git pull --rebase` before `git push`. Never discard another session's uncommitted
  changes (`git checkout -- <file>`, `git reset --hard`, `git stash`).

## Checks before committing

The git pre-commit hook (`.githooks/pre-commit`, enabled by the first `python -m trirouter sync`) runs
`sync --pre-commit` = sync + stage docs/cli.md + tests + the routing evaluation when routing code is staged; up to
~2.5 minutes, so give `git commit` a 10-minute Bash timeout. By hand:

```bash
python -m trirouter sync --pre-commit   # or the pieces:
python -m pyflakes trirouter tests eval install.py
python -m pytest tests -q
python eval/eval_router.py
```

Record user-visible changes in `CHANGELOG.md`: one line per change, what changed, no numbers or internals.
