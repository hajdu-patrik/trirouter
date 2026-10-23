"""Repository root on sys.path, and the user's ROUTER_* settings dropped before trirouter reads
them at import: the tests run with the defaults, as in CI."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _name in [n for n in os.environ if n.startswith("ROUTER_")]:
    del os.environ[_name]


@pytest.fixture(autouse=True)
def no_distilled_model(monkeypatch):
    """A model trained on the real traffic (~/.trirouter) must never change a test's routing; the distill tests
    switch it back on against a temporary state folder."""
    monkeypatch.setenv("ROUTER_DISTILLED", "0")


@pytest.fixture(autouse=True)
def isolated_machine(tmp_path_factory, monkeypatch):
    """No test may reach the real shim folders, ~/.local/bin, the Windows user PATH or a scheduled task. A test
    that exercises one of them patches it itself (a later monkeypatch wins)."""
    from trirouter import integrations, remote
    base = tmp_path_factory.mktemp("machine")
    monkeypatch.setattr(integrations, "BIN", base / "bin")
    monkeypatch.setattr(integrations, "LOCAL_BIN", base / "local-bin")
    for name, file in (("SHIM_HOOK", "run_hook.py"), ("SHIM_MCP", "mcp_server.py"), ("SHIM_ROUTE", "route.py")):
        monkeypatch.setattr(integrations, name, base / "bin" / file)
    store = {"path": ("C:/Windows", 2)}
    monkeypatch.setattr(integrations, "user_path_read", lambda: store["path"])
    monkeypatch.setattr(integrations, "user_path_write", lambda value, kind: store.update(path=(value, kind)))
    monkeypatch.setattr(integrations, "broadcast_env_change", lambda: None)
    monkeypatch.setattr(remote, "_ps", lambda script, *a, **k: (1, ""))
    monkeypatch.setattr(remote, "_rc_help", lambda claude: "")  # never start a real `claude remote-control --help`
    # the hooks start the daily model check in a detached process: never for real in a test
    from trirouter import platforms
    monkeypatch.setattr(platforms, "spawn_detached", lambda *a, **k: False)
    # the hooks read ~/.claude/settings.json for the session's model and log decisions, guarded errors and the
    # agent guard's actions: never into the real ~/.trirouter/logs
    from trirouter import hooks
    monkeypatch.setattr(hooks, "CLAUDE_SETTINGS", base / "claude-settings.json")
    for name, file in (("LOG_FILE", "routing.jsonl"), ("SUBAGENT_LOG", "subagents.jsonl"), ("GUARD_LOG", "guard.jsonl")):
        monkeypatch.setattr(hooks, name, base / "logs" / file)
    # the user's own config.json (e.g. hooks.transport = "http" after `daemon enable`) must not change a test
    from trirouter import core
    monkeypatch.setattr(core, "STATE_DIR", base / "state")
    monkeypatch.setenv("TRIROUTER_HOME", str(base / "state"))  # the modules that look the folder up per call
    # generated agents and the skill catalog (the daily check refreshes them): never the real ~/.claude, ~/.codex,
    # ~/.gemini or ~/.skills
    from trirouter import catalog, hub
    for name in ("CLAUDE_AGENTS", "CODEX_AGENTS", "AGY_AGENTS"):
        monkeypatch.setattr(hub, name, base / name.lower())
    monkeypatch.setattr(hub, "CODEX_CONFIG", base / "codex-config.toml")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "xdg"))
    monkeypatch.setattr(catalog, "HUB", base / "skills")
    monkeypatch.setattr(catalog, "CATALOG", base / "skills" / "catalog.json")
    monkeypatch.setattr(catalog, "SOURCES", [(base / "skills", False, catalog.PROVIDERS, None)])


@pytest.fixture
def scans(tmp_path, monkeypatch):
    """A fake scanner: verdict by skill name; returns the list of scanned names."""
    from skillscan_helpers import REPORTS
    from trirouter import skillscan
    calls = []
    monkeypatch.setattr(skillscan, "QUARANTINE", tmp_path / "quarantine")
    monkeypatch.setattr(skillscan, "CACHE", tmp_path / "state" / "skillscan.json")
    monkeypatch.setattr(skillscan, "find_scanner", lambda: "skillspector")
    monkeypatch.setattr(skillscan, "scanner_version", lambda exe: "2.12.0")

    def fake(exe, skill_dir, llm):
        calls.append(skill_dir.name)
        return dict(REPORTS[skill_dir.name], llm_available=llm)
    monkeypatch.setattr(skillscan, "run_scan", fake)
    return calls
