"""The MCP server picks up source and config changes without a restart and survives a broken edit."""
import itertools
import os
import sys

import pytest

from trirouter import mcp_server
from trirouter.hotreload import HotReloader

_steps = itertools.count(1)


def bump(path):
    """A modification time that no earlier bump gave: a file system clock that has not ticked between two quick edits
    stamps them alike, and the reloader would take the second edit of the same size for no change at all."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + next(_steps) * 5_000_000_000))


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    root = tmp_path / "hotpkg"
    (root / "config").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "config" / "m.json").write_text('{"v": 1}')
    (root / "tools.py").write_text(
        "import json\nfrom pathlib import Path\nSTAMP = json.loads((Path(__file__).parent / 'config' / 'm.json').read_text())['v']\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield root
    for name in [n for n in sys.modules if n == "hotpkg" or n.startswith("hotpkg.")]:
        del sys.modules[name]


def test_changed_config_value_is_picked_up_without_restart(pkg):
    reloader = HotReloader("hotpkg", pkg, "tools")
    assert reloader.current().STAMP == 1
    assert reloader.current() is reloader.current()  # unchanged files: no reload
    (pkg / "config" / "m.json").write_text('{"v": 2}')
    bump(pkg / "config" / "m.json")
    assert reloader.current().STAMP == 2 and reloader.warning == ""


def test_broken_module_keeps_old_behaviour_and_warns(pkg):
    reloader = HotReloader("hotpkg", pkg, "tools")
    assert reloader.current().STAMP == 1
    (pkg / "tools.py").write_text("STAMP = (\n")
    bump(pkg / "tools.py")
    assert reloader.current().STAMP == 1 and "SyntaxError" in reloader.warning
    (pkg / "tools.py").write_text("STAMP = 3\n")
    bump(pkg / "tools.py")
    assert reloader.current().STAMP == 3 and reloader.warning == ""


def test_server_result_carries_the_reload_warning(monkeypatch, pkg):
    reloader = HotReloader("hotpkg", pkg, "tools")
    (pkg / "tools.py").write_text("TOOLS = []\nHANDLERS = {'t': lambda a: ('ok', False)}\n")
    monkeypatch.setattr(mcp_server, "RELOADER", reloader)
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "t"}}
    assert mcp_server.handle(call)["result"]["content"][0]["text"] == "ok"
    (pkg / "tools.py").write_text("def broken(:\n")
    bump(pkg / "tools.py")
    text = mcp_server.handle(call)["result"]["content"][0]["text"]
    assert text.startswith("ok") and "warning: trirouter reload failed" in text


def test_initialize_reports_the_package_version():
    from trirouter import __version__, mcp_server
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert reply["result"]["serverInfo"] == {"name": "trirouter", "version": __version__}


def test_resource_and_prompt_lists_are_empty_not_errors():
    from trirouter import mcp_server
    for method, key in mcp_server.EMPTY_LISTS.items():
        assert mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": method})["result"] == {key: []}
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "nope/list"})["error"]["code"] == -32601
