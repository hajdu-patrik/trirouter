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
def isolated_machine(tmp_path_factory, monkeypatch):
    """No test may reach the real shim folders, ~/.local/bin, the Windows user PATH or a scheduled task. A test
    that exercises one of them patches it itself (a later monkeypatch wins)."""
    from trirouter import integrations, remote
    base = tmp_path_factory.mktemp("machine")
    monkeypatch.setattr(integrations, "BIN", base / "bin")
    monkeypatch.setattr(integrations, "LEGACY_BIN", base / "old-bin")
    monkeypatch.setattr(integrations, "LOCAL_BIN", base / "local-bin")
    for name, file in (("SHIM_HOOK", "run_hook.py"), ("SHIM_MCP", "mcp_server.py"), ("SHIM_ROUTE", "route.py")):
        monkeypatch.setattr(integrations, name, base / "bin" / file)
    store = {"path": ("C:/Windows", 2)}
    monkeypatch.setattr(integrations, "user_path_read", lambda: store["path"])
    monkeypatch.setattr(integrations, "user_path_write", lambda value, kind: store.update(path=(value, kind)))
    monkeypatch.setattr(integrations, "broadcast_env_change", lambda: None)
    monkeypatch.setattr(remote, "_ps", lambda script, *a, **k: (1, ""))
