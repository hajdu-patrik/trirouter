"""`doctor`'s hook and MCP sections against scratch config files, and the shared config.json reader."""
import json

import pytest

from trirouter import doctor, integrations, platforms as P


@pytest.fixture
def machine(tmp_path, monkeypatch):
    paths = {key: tmp_path / f"{key}.json" for key in ("claude_settings", "codex_hooks", "agy_hooks", "agy_mcp")}
    paths["codex_config"] = tmp_path / "config.toml"
    for key, path in paths.items():
        monkeypatch.setitem(P.PATHS, key, path)
    monkeypatch.setattr(P, "claude_desktop_config", lambda: tmp_path / "desktop.json")
    monkeypatch.setattr(doctor, "report_transport", lambda: None)
    return paths


def hooks(provider, events):
    return {"hooks": {e: [{"hooks": [{"type": "command", "command": integrations.hook_cmd(provider, e)}]}] for e in events}}


def test_hooks_report_names_what_is_missing(machine, capsys):
    events = integrations.HOOK_EVENTS["claude"]
    machine["claude_settings"].write_text(json.dumps(hooks("claude", events[:-1])), encoding="utf-8")
    machine["codex_hooks"].write_text(json.dumps(hooks("codex", ("UserPromptSubmit", "Stop"))), encoding="utf-8")
    doctor.report_hooks()
    out = capsys.readouterr().out
    assert f"[!!]  Claude prompt/stop/session hooks   missing {events[-1]}" in out
    assert "[OK]  Codex UserPromptSubmit + Stop" in out and "[!!]  Antigravity PreInvocation + Stop" in out


def test_mcp_report_finds_each_tools_entry(machine, tmp_path, capsys):
    entry = {"mcpServers": {integrations.MCP_NAME: {"command": "python"}}}
    (tmp_path / "desktop.json").write_text(json.dumps(entry), encoding="utf-8")
    machine["codex_config"].write_text(f"[mcp_servers.{integrations.MCP_NAME}]\ncommand = \"python\"\n", encoding="utf-8")
    doctor.report_mcp()
    out = capsys.readouterr().out
    assert "[OK]  Claude desktop (Chat/Cowork)" in out and "[OK]  Codex" in out and "[!!]  Antigravity" in out


def test_read_config_accepts_a_bom_and_never_raises(tmp_path):
    path = tmp_path / "config.json"
    assert P.read_config(path) == {}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"remote_name": "Box"}).encode())
    assert P.read_config(path) == {"remote_name": "Box"}
    path.write_text("[1, 2]", encoding="utf-8")
    assert P.read_config(path) == {}
    path.write_text("{broken", encoding="utf-8")
    assert P.read_config(path) == {}
