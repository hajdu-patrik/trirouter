import io
import json
import sys
from pathlib import Path

import pytest

from trirouter import catalog as skill_index, core, hooks as run_hook, lang, mcp_server

CATALOG = [
    {"name": "anthropic-skills:pptx", "description": "Create and edit PowerPoint presentations, slide decks (.pptx).",
     "path": "C:/x/pptx", "native_in": ["claude"]},
    {"name": "anthropic-skills:xlsx", "description": "Work with Excel spreadsheets (.xlsx): formulas, charts, pivot tables.",
     "path": "C:/x/xlsx", "native_in": ["claude"]},
    {"name": "cloud-run-basics", "description": "Deploy and manage Cloud Run services, jobs and worker pools.",
     "path": "C:/x/crb", "native_in": ["claude", "codex", "antigravity"]},
    {"name": "imagegen", "description": "Generate images with an image model.", "path": "C:/x/img", "native_in": ["codex"]},
]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No test ever touches the real ~/.trirouter or ~/.skills, and never calls JEV."""
    monkeypatch.setattr(run_hook, "LOG_FILE", tmp_path / "routing.jsonl")
    monkeypatch.setattr(run_hook, "SUBAGENT_LOG", tmp_path / "subagents.jsonl")
    monkeypatch.setattr(run_hook, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)  # queue state, user config, model overrides
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(skill_index, "load_catalog", lambda: CATALOG)
    monkeypatch.setattr(core, "BACKEND", "local")
    monkeypatch.delenv("ROUTER_MODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    return tmp_path


@pytest.mark.parametrize("text,expected", [
    ("Mi Magyarország fővárosa?", "hu"),
    ("irj egy fuggvenyt ami megforditja a stringet", "hu"),
    ("Refaktoráld az auth modult", "hu"),
    ("Write a function that reverses a string", "en"),
    ("What is the capital of Hungary?", "en"),
    ("Explain the TCP three-way handshake", "en"),
    ("", "hu"),
])
def test_language(text, expected):
    assert lang.detect(text) == expected


@pytest.mark.parametrize("text", [
    "Töröld a régi branch-eket a repóból", "Delete all files in the temp folder", "rm -rf build/",
    "git push --force origin main", "Küldd el ezt az e-mailt Péternek", "Send an email to the team",
    "Utald át a 5000 forintot", "Buy me a new keyboard on Amazon", "DROP TABLE users;",
    "Publikáld a blogposztot", "Remove the user accounts older than a year", "Uninstall the program Steam",
    "Formázd meg a D: meghajtót", "format the disk", "Írd felül a konfigot",
])
def test_destructive_hits(text):
    assert core.is_destructive(text), text


@pytest.mark.parametrize("text", [
    "Remove the unused import in utils.py", "Sort these in order please", "Explain how payment APIs work",
    "Mi a különbség a list és a tuple között?", "Write a function to order a list", "4/0AVMBsJh-e_Rk7bG92cXyZ12",
    "Refaktoráld az auth modult",
])
def test_destructive_false_positives(text):
    assert not core.is_destructive(text), text


@pytest.mark.parametrize("text,task", [
    ("Mi Magyarország fővárosa?", "qa"), ("What is the capital of France?", "qa"),
    ("Írj pytest teszteket a parser modulhoz", "test"), ("Write unit tests for the parser", "test"),
    ("Javítsd ki a bugot a login függvényben", "code"), ("Fix the bug in the login function", "code"),
    ("Oldd meg: 2x + 3 = 11", "math"), ("Solve the equation 2x + 3 = 11", "math"),
    ("Foglald össze az egyetemi jegyzetemet", "study"), ("Summarize my lecture notes for the exam", "study"),
    ("Mi a legfrissebb hír a Fed kamatdöntéséről?", "research"), ("What is the latest news on the Fed rate decision?", "research"),
])
def test_local_task(text, task):
    assert core.local_answers(text)["task"]["choice"] == task


def test_oauth_code_is_not_math():
    assert core.local_answers("4/0AVMBsJh-e_Rk7bG92cXyZ12k3")["task"]["choice"] != "math"


def test_hard_difficulty():
    assert core.local_answers("Refaktoráld az egész kódbázist hexagonális architektúrára")["difficulty"]["score"] == 2
    assert core.local_answers("Migrate the entire codebase to microservices")["difficulty"]["score"] == 2
    assert core.local_answers("A teljes név mező legyen kötelező")["difficulty"]["score"] < 2  # 'teljes' alone is not hard


def test_clamp_effort():
    assert core.clamp_effort("max", ["high", "xhigh"]) == "xhigh"
    assert core.clamp_effort("low", ["high", "xhigh", "max"]) == "high"
    assert core.clamp_effort("ultra", ["low", "medium", "high", "xhigh", "max"]) == "max"
    assert core.clamp_effort("medium", ["low", "high"]) == "high"  # tie -> higher
    assert core.clamp_effort(None, ["low", "medium", "high"]) == "medium"


def test_claude_never_ultra():
    ans = core.classify("Prove the Riemann hypothesis in full detail", effort=core.effort_levels_for("claude"))
    assert ans["effort"]["choice"] != "ultra"


def test_route_claude_hard_code_goes_to_deep_worker_with_verify():
    d, text, hit, err = core.route("Refaktoráld az egész kódbázist hexagonális architektúrára", "claude")
    assert d["primary"] == "deep"
    assert d["verify"] == "codex"
    assert "`opus-worker-xhigh`" in text
    assert "cli-bridge" in text
    assert "Respond in Hungarian." in text
    assert not hit
    assert err is None


def test_route_codex_effort_always_valid_for_model():
    d, text, _, _ = core.route("Migrate the entire codebase to microservices", "codex")
    assert d["primary"] == "deep"
    assert "`gpt-5_6-terra-" in text
    assert d["effort"] in ("high", "xhigh", "max")
    assert "Respond in English." in text
    d, text, _, _ = core.route("Migrate the entire codebase to microservices", "codex", session_model="gpt-5.6-terra")
    assert "stay in this session" in text
    assert "Spawn" not in text


def test_ultra_is_never_offered_or_accepted(monkeypatch):
    for provider in ("claude", "codex", "antigravity"):
        assert "ultra" not in core.effort_levels_for(provider)["levels"]
        assert all("ultra" not in m["levels"] for m in core.models_for(provider).values())
        q = core.build_questions({}, core.effort_levels_for(provider), core.models_for(provider))
        assert "ultra" not in q["effort"]["criteria"]
        assert "ultra" not in json.dumps(q.get("model", {}))
    fake = {"answers": {"task": {"choice": "code", "confidence": 0.95}, "difficulty": {"score": 2, "confidence": 0.9},
                        "long_context": {"noul": 0.1}, "needs_web": {"noul": 0.1}, "destructive": {"noul": 0.05},
                        "effort": {"choice": "ultra", "confidence": 0.9}, "model": {"choice": "gpt-5.6-terra", "confidence": 0.9}}}
    monkeypatch.setattr(core, "ask_jev", lambda p, q: json.loads(json.dumps(fake)))
    d, text, _, _ = core.route("anything", "codex", backend="jev")
    assert d["effort"] == "max"
    assert "`gpt-5_6-terra-max`" in text
    assert "ultra" not in text


def test_jev_model_pick_every_provider(monkeypatch):
    def fake(model, effort):
        return {"answers": {"task": {"choice": "general", "confidence": 0.9}, "difficulty": {"score": 1, "confidence": 0.9},
                            "long_context": {"noul": 0.1}, "needs_web": {"noul": 0.1}, "destructive": {"noul": 0.05},
                            "effort": {"choice": effort, "confidence": 0.9}, "model": {"choice": model, "confidence": 0.9}}}
    monkeypatch.setattr(core, "ask_jev", lambda p, q: fake("sonnet", "low"))
    _, text, _, _ = core.route("Write a haiku", "claude", backend="jev")
    assert "`sonnet-worker-low`" in text
    monkeypatch.setattr(core, "ask_jev", lambda p, q: fake("gpt-6-sol", "high"))  # rejected for ChatGPT accounts
    d, text, _, _ = core.route("Write a haiku", "codex", backend="jev")
    assert d.get("model") is None
    assert "gpt-6-sol" not in text
    monkeypatch.setattr(core, "ask_jev", lambda p, q: fake("gemini-3.1-pro", "medium"))  # pro has low/high only
    _, text, _, _ = core.route("Write a haiku", "antigravity", backend="jev")
    assert "`gemini-3.1-pro-high`" in text
    monkeypatch.setattr(core, "ask_jev", lambda p, q: fake("gpt-daybreak-red-latest", "high"))  # not selectable
    d, _, _, _ = core.route("Write a haiku", "codex", backend="jev")
    assert d.get("model") is None


def test_route_antigravity_deep_delegates_to_the_pro_agent():
    d, text, _, _ = core.route("Migrate the entire codebase to microservices", "antigravity")
    assert d["primary"] == "deep"
    assert d["target_agent"] == "gemini-pro-worker"
    assert "`gemini-pro-worker`" in text
    assert "gemini-3.1-pro-high" in text


def test_route_antigravity_model_name_has_effort():
    d, text, _, _ = core.route("Mi Magyarország fővárosa?", "antigravity")
    assert d["primary"] == "fast"
    assert "gemini-3.8-flash-low" in text


def test_route_skill_native_vs_cross_tool():
    _, text, _, _ = core.route("Készíts egy prezentációt pptx formátumban a negyedéves eredményekről", "claude")
    assert "Relevant skill: `anthropic-skills:pptx` - use it." in text
    _, text, _, _ = core.route("Készíts egy prezentációt pptx formátumban a negyedéves eredményekről", "codex")
    assert "read and follow C:/x/pptx/SKILL.md" in text


def test_overrides_whole_tag_only():
    d, _, _, _ = core.route("#opus explain this", "claude")
    assert d["backend"] == "override"
    assert d["primary"] == "deep"
    d, _, _, _ = core.route("#faster please explain", "codex")
    assert d["backend"] != "override"


def test_cloud_mode_replaces_agents_and_cli(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    d, text, _, _ = core.route("#codex review this", "claude")
    assert d["primary"] == "main"
    assert "cli-bridge" not in text
    d, _, _, _ = core.route("Refaktoráld az egész kódbázist hexagonális architektúrára", "claude")
    assert d["primary"] == "main"
    assert d["verify"] == ""


def test_jev_failure_falls_back_to_local(monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("jev down")
    monkeypatch.setattr(core, "ask_jev", boom)
    d, text, _, err = core.route("What is 2+2?", "claude", backend="jev")
    assert d["backend"] == "local"
    assert "jev down" in err


def test_jev_answers_are_used(monkeypatch):
    fake = {"answers": {"task": {"choice": "code", "confidence": 0.95}, "difficulty": {"score": 2, "confidence": 0.9},
                        "long_context": {"noul": 0.1}, "needs_web": {"noul": 0.1}, "destructive": {"noul": 0.05},
                        "effort": {"choice": "ultra", "confidence": 0.8}}, "model": "jev-1.13.0"}
    monkeypatch.setattr(core, "ask_jev", lambda p, q: json.loads(json.dumps(fake)))
    d, text, _, _ = core.route("anything", "claude", backend="jev")
    assert d["backend"] == "jev"
    assert d["effort"] == "max"  # ultra stripped for Claude, clamped into deep tier


def run(monkeypatch, capsys, provider, event, payload):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8"))))
    assert run_hook.main([provider, event]) == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_hook_claude_codex_shape(monkeypatch, capsys, provider):
    out = run(monkeypatch, capsys, provider, "UserPromptSubmit", {"prompt": "Write unit tests for the parser"})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert ctx.startswith("[router]")


@pytest.mark.parametrize("prompt", ["<task-notification>\n<task-id>x</task-id>", "/help", "#privat titkos dolog", "hi"])
def test_hook_skips(monkeypatch, capsys, prompt):
    assert run(monkeypatch, capsys, "claude", "UserPromptSubmit", {"prompt": prompt}) is None


def test_hook_bad_stdin_never_crashes(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"\xef\xbb\xbf[1,2]")))
    assert run_hook.main(["claude", "UserPromptSubmit"]) == 0
    assert capsys.readouterr().out == ""


def test_hook_cloud_only_is_silent_locally(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"prompt": "Write tests"}')))
    assert run_hook.main(["claude", "UserPromptSubmit", "--cloud-only"]) == 0
    assert capsys.readouterr().out == ""


def test_hook_antigravity_reads_transcript_once_per_turn(monkeypatch, capsys, tmp_path):
    tr = tmp_path / "transcript.jsonl"
    tr.write_text(json.dumps({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                              "content": "<USER_REQUEST>\nÍrj pytest teszteket a parserhez\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x"
                                         "</ADDITIONAL_METADATA>"}) + "\n", encoding="utf-8")
    payload = {"conversationId": "c1", "transcriptPath": str(tr), "invocationNum": 1}
    out = run(monkeypatch, capsys, "antigravity", "PreInvocation", payload)
    msg = out["injectSteps"][0]["ephemeralMessage"]
    assert "task=test" in msg
    assert "Respond in Hungarian." in msg
    assert run(monkeypatch, capsys, "antigravity", "PreInvocation", {**payload, "invocationNum": 2}) is None
    log = [json.loads(l) for l in run_hook.LOG_FILE.read_text(encoding="utf-8").splitlines()]
    assert len(log) == 1
    assert log[0]["provider"] == "antigravity"


def test_hook_antigravity_plan_mode_prompt_is_routed(monkeypatch, capsys, tmp_path):
    tr = tmp_path / "transcript.jsonl"
    tr.write_text(json.dumps({"step_index": 0, "type": "USER_INPUT",
                              "content": "<USER_REQUEST>\n/plan Write pytest tests for the parser\n</USER_REQUEST>"}) + "\n",
                  encoding="utf-8")
    out = run(monkeypatch, capsys, "antigravity", "PreInvocation", {"conversationId": "p1", "transcriptPath": str(tr)})
    assert "task=test" in out["injectSteps"][0]["ephemeralMessage"]


def test_hook_antigravity_missing_transcript(monkeypatch, capsys, tmp_path):
    assert run(monkeypatch, capsys, "antigravity", "PreInvocation", {"transcriptPath": str(tmp_path / "nope")}) is None


def test_log_redacts_secrets(monkeypatch, capsys):
    run(monkeypatch, capsys, "claude", "UserPromptSubmit", {"prompt": "use key sk-abcdefghijklmnopqrstuvwxyz123456 in the code"})
    entry = json.loads(run_hook.LOG_FILE.read_text(encoding="utf-8").splitlines()[-1])
    assert "sk-abc" not in entry["prompt"]
    assert "[redacted]" in entry["prompt"]


def test_parse_frontmatter_block_description(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: demo\ndescription: >\n  Line one\n  line two.\nlicense: x\n---\nbody", encoding="utf-8")
    assert skill_index.parse_frontmatter(p) == ("demo", "Line one line two.")


def test_parse_frontmatter_value_on_next_line(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname:\ndescription:\n  Line one\n  line two.\nlicense: x\n---\nbody", encoding="utf-8")
    assert skill_index.parse_frontmatter(p) == (None, "Line one line two.")


def test_prefilter_hungarian_glossary():
    top = skill_index.prefilter("Excel táblázat összesítése", CATALOG)
    assert top
    assert top[0][1]["name"] == "anthropic-skills:xlsx"
    assert skill_index.prefilter("Mi Magyarország fővárosa?", CATALOG) == []


def test_mcp_protocol():
    init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["serverInfo"]["name"] == "trirouter"
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert names == {"route_prompt", "list_skills", "get_skill"}
    r = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "route_prompt", "arguments": {"prompt": "Refaktoráld az egész kódbázist"}}})
    text = r["result"]["content"][0]["text"]
    assert "[router]" in text
    assert "subagent" not in text  # claude-chat: advice only, no subagents
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"] == -32601


def test_local_skill_pick_needs_name_evidence():
    cat = CATALOG + [
        {"name": "google-cloud-recipe-auth", "description": "Authenticate to Google Cloud APIs, ADC, service accounts.",
         "path": "C:/x/auth", "native_in": ["claude"]},
        {"name": "anthropic-skills:learn", "description": "Learn from this session and save lessons to memory.",
         "path": "C:/x/learn", "native_in": ["claude"]},
    ]
    pick = lambda p: core.local_skill_answer(skill_index.prefilter(p, cat))  # noqa: E731
    assert pick("Refaktoráld az auth modult") is None                     # one stray name word
    assert pick("What does overfitting mean in machine learning?") is None  # short everyday name
    assert pick("use the learn skill to save this lesson")[0] == "anthropic-skills:learn"  # explicit
    assert pick("Írj egy PDF-et és egy pptx prezentációt")[0] == "anthropic-skills:pptx"   # format name + glossary


@pytest.mark.parametrize("text,expected", [
    ("Mi Magyarország fővárosa?", 0),
    ("Fix the bug in the login function", 0),
    ("Refaktoráld az egész auth modult hexagonális architektúrára", 0),                     # hard, one area
    ("Write the backend endpoint and the complete test suite for the entire payment flow", 1),
    ("Migrate the whole backend API, the React frontend and the database schema to the new auth", 2),
    ("Készíts egy teljes új oldalt semmiből: backend API, React frontend és adatbázis séma", 3),
    ("Build a complete new page from scratch: backend, frontend, and write a scraper tool for the product data", 4),
])
def test_extra_agents_mock(text, expected):
    d, text_out, _, _ = core.route(text, "claude")
    assert d["extra_agents"] == expected, text
    assert ("Parallelism: none" in text_out) == (expected == 0)


def test_extra_agents_strict_clamps():
    assert core.extra_agents({"choice": "4", "confidence": 0.95}, 2) == 4
    assert core.extra_agents({"choice": "4", "confidence": 0.5}, 2) == 3        # unsure -> one lower
    assert core.extra_agents({"choice": "3", "confidence": 0.95}, 1) == 1       # not hard -> at most +1
    assert core.extra_agents({"choice": "9", "confidence": 0.95}, 2) == 4       # never above the cap
    assert core.extra_agents({"choice": "x"}, 2) == 0
    assert core.extra_agents(None, 2) == 0
    q = core.build_questions({})
    assert list(q["agents"]["criteria"]) == ["0", "1", "2", "3", "4"]
    assert "strict" in q["agents"]["instructions"]
