"""The MCP server's protocol answers and its three tools; the catalog and the router are fakes."""
import json

import pytest

from trirouter import core, mcp_server, mcp_tools


@pytest.fixture
def skills(tmp_path, monkeypatch):
    folder = tmp_path / "demo"
    folder.mkdir()
    (folder / "SKILL.md").write_text("# Demo\nDo the demo.", encoding="utf-8")
    entries = [{"name": "plugin:demo", "path": str(folder), "native_in": ["claude"], "description": "A demo skill"},
               {"name": "gone", "path": str(tmp_path / "missing"), "native_in": [], "description": "No folder"}]
    monkeypatch.setattr(mcp_tools.catalog, "load_catalog", lambda: entries)
    return entries


def call(method, params=None, mid=1):
    return mcp_server._handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}, mcp_tools)


def test_initialize_ping_and_lists_answer_without_errors():
    init = call("initialize", {"protocolVersion": "2024-11-05"})["result"]
    assert init["protocolVersion"] == "2024-11-05" and init["serverInfo"]["name"] == "trirouter"
    assert call("ping")["result"] == {}
    assert [t["name"] for t in call("tools/list")["result"]["tools"]] == ["route_prompt", "list_skills", "get_skill"]
    assert call("resources/list")["result"] == {"resources": []} and call("prompts/list")["result"] == {"prompts": []}


def test_notifications_get_no_answer_and_unknown_requests_an_error():
    assert call("notifications/initialized", mid=None) is None
    assert call("nope")["error"]["code"] == -32601
    assert call("tools/call", {"name": "nope"})["error"]["code"] == -32602


def test_a_failing_tool_is_reported_not_raised(monkeypatch):
    monkeypatch.setitem(mcp_tools.HANDLERS, "get_skill", lambda args: 1 / 0)
    result = call("tools/call", {"name": "get_skill", "arguments": {"name": "x"}})["result"]
    assert result["isError"] is True and "ZeroDivisionError" in result["content"][0]["text"]


def test_respond_handles_bad_json_batches_and_non_objects(monkeypatch):
    monkeypatch.setattr(mcp_server, "handle", lambda msg: {"id": msg["id"]} if msg.get("id") else None)
    assert mcp_server.respond("{not json")["error"]["code"] == -32700
    assert mcp_server.respond(json.dumps([{"id": 1}, {"method": "note"}, 5])) == [{"id": 1}]
    assert mcp_server.respond("3") is None


def test_route_prompt_returns_the_text_the_decision_and_the_skill(skills, monkeypatch):
    decision = {"task": "code", "level": 1, "primary": "main", "skill": "plugin:demo", "lang": "en", "backend": "local",
                "skill_candidates": ["plugin:demo"], "route_model": "sonnet", "route_effort": "high"}
    monkeypatch.setattr(core, "route", lambda prompt, provider: (decision, f"[router] {provider}", False, "timeout"))
    text, is_error = mcp_tools.tool_route_prompt({"prompt": " fix it "})
    assert not is_error and text.startswith("[router] claude-chat")
    assert '"model": "sonnet"' in text and "JEV unavailable, local router used: timeout" in text
    assert "=== Skill `plugin:demo` (SKILL.md) ===" in text and "Do the demo." in text
    assert mcp_tools.tool_route_prompt({"prompt": "  "}) == ("Empty prompt - nothing to route.", True)


def test_list_skills_lists_the_catalog_or_says_it_is_empty(skills, monkeypatch):
    text, is_error = mcp_tools.tool_list_skills({"limit": 1})
    assert not is_error and text == "- plugin:demo [claude]: A demo skill"
    monkeypatch.setattr(mcp_tools.catalog, "load_catalog", lambda: [])
    assert mcp_tools.tool_list_skills({}) == ("No matching skill.", False)


def test_get_skill_finds_a_skill_by_its_short_name_and_cuts_a_long_one(skills, monkeypatch):
    text, is_error = mcp_tools.tool_get_skill({"name": "demo"})
    assert not is_error and "Skill `plugin:demo`" in text and "Do the demo." in text
    monkeypatch.setattr(mcp_tools, "MAX_SKILL_CHARS", 6)
    assert mcp_tools.tool_get_skill({"name": "plugin:demo"})[0].endswith("# Demo\n...[truncated]")
    assert "could not read" in mcp_tools.tool_get_skill({"name": "gone"})[0]
    assert mcp_tools.tool_get_skill({"name": "nope"}) == ("Unknown skill 'nope'. Use list_skills.", True)
