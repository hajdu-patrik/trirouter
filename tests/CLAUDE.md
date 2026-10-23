# Tests

`python -m pytest tests -q` (about 90 s; CI runs it on Linux, macOS and Windows with Python 3.10 to 3.14). Nothing here may touch the
real home, `~/.trirouter`, `~/.claude`, a real CLI or the network.

- From a checkout that a live hook runs, point `TRIROUTER_HOME` (and `USERPROFILE` / `HOME`) at a scratch folder first.
- `conftest.py` isolates every test: shim, state, log, agent and skill-hub folders, the PATH and registry fakes, no detached spawn, no
  `ROUTER_*` variables. A test that exercises one of those patches it itself (a later `monkeypatch` wins).
- Fakes over mocks of internals: JEV is a function or a local echo server, a CLI is a `platforms.run` fake, a terminal is a fake backend.
- A fixture shared by several files goes into `conftest.py` and plain helpers into a module such as `skillscan_helpers.py`; never import a
  fixture from another test module (pyflakes reports it, and it is easy to break).
- Comments say why a test does what it does; the test name says what it checks. pyflakes must stay clean:
  `python -m pyflakes trirouter tests eval install.py`.
- `test_policy.py` checks the model policy across `config/*.json`, `test_cli_manual.py` that `docs/cli.md` matches `manual.py`
  (`python -m trirouter sync` regenerates it), `test_eval_delegation.py` the evaluation script's own bookkeeping.
