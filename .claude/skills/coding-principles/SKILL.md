---
name: coding-principles
description: 'Clean-code pass for changed Python in trirouter: dead code, two-line comments, shared helpers, behaviour unchanged, generated files in sync. Use after code changes, before a commit, or when asked for a cleanup.'
disable-model-invocation: true
---

Apply the rules of `CLAUDE.md` ("Conventions", "Code style") to every changed `.py` file of `trirouter/`, `tests/` and
`eval/`. Remediate, do not only report; behaviour never changes.

- Dead code goes: unused functions, constants, parameters, config keys, test helpers. Check the dynamic uses first
  (entry points, hook and MCP registration, `getattr` and dispatch strings, `manual.py` -> `docs/cli.md`,
  `config/*.json`, `templates/agents`) and the tests: a name only a test calls is test-only API, so change the test
  and remove the name.
- No wrapper that only forwards to one function; no comment that restates the code or tells its history.
- Verify with a scratch `TRIROUTER_HOME` (see `tests/CLAUDE.md`), never with a real `trirouter setup`:
  `python -m trirouter sync --pre-commit` (pyflakes, tests, the evaluation when routing code changed).

Output: files changed, rules applied, remaining risks.
