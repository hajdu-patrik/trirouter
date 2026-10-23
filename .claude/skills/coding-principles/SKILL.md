---
name: coding-principles
description: 'Clean-code pass for changed Python in trirouter: dead code, two-line comments, shared helpers, behaviour unchanged, generated files in sync. Use after code changes, before a commit, or when asked for a cleanup.'
disable-model-invocation: true
---

Use this on every changed `.py` file of `trirouter/`, `tests/` and `eval/`. Remediate, do not only report; behaviour never changes.

## Rules

- Dead code goes: unused functions, constants, parameters, config keys, test helpers. Check the dynamic uses first (entry points, hook and
  MCP registration, `getattr` and dispatch strings, `manual.py` -> `docs/cli.md`, `config/*.json`, `templates/agents`) and the tests: a name
  that only a test calls is test-only API, so change the test and remove the name.
- Less code beats clever code: share a helper only when it reads at least as well, prefer plain code to abstraction, no wrapper that only
  forwards to one function.
- Comments and docstrings are at most two lines and only say what the code cannot: a non-obvious why, a trap, an outside constraint.
  Drop those that restate the code or tell its history (the commit message and `CHANGELOG.md` do).
- Python 3.10+, standard library only; paths, links, processes and executables go through `trirouter/platforms.py`. A hook never blocks
  or crashes the host: catch everything, exit 0.

## Verify

Use a scratch `TRIROUTER_HOME` (see `tests/CLAUDE.md`); never run `install.py` or `trirouter setup` for real.

```bash
python -m pyflakes trirouter tests eval install.py   # silent
python -m pytest tests -q                                       # all green
python -m trirouter sync --check                                # docs/cli.md matches manual.py; `python -m trirouter sync` after editing it
python eval/eval_router.py                                      # when routing code or config changed
```

## Output

Files changed, rules applied, remaining risks.
