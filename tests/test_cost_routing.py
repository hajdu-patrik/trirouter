"""Cost-aware routing: inline stays inline, the model-family cap, go-aheads that need the parent's context,
the local fast path and the decision cache, request size, injected boilerplate and the quality fixes."""
import json
import subprocess
import sys

import pytest

from trirouter import core, store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    monkeypatch.setattr(core, "BACKEND", "local")
    monkeypatch.setattr(core.catalog, "load_catalog", lambda *a, **k: [])
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return tmp_path


def jev_answers(task="general", level=1, model=None, effort="medium", destructive=0.05, conf=0.9):
    answers = {"task": {"choice": task, "confidence": conf}, "difficulty": {"score": level, "confidence": 0.9},
               "long_context": {"noul": 0.1}, "needs_web": {"noul": 0.1}, "destructive": {"noul": destructive},
               "effort": {"choice": effort, "confidence": 0.9}}
    if model:
        answers["model"] = {"choice": model, "confidence": 0.9}
    return {"model": "jev-test", "answers": answers}


def fake_jev(monkeypatch, **kw):
    calls = []

    def ask(prompt, questions):
        calls.append(prompt)
        return json.loads(json.dumps(jev_answers(**kw)))

    monkeypatch.setattr(core, "ask_jev", ask)
    return calls


def auto_jev(monkeypatch):
    """BACKEND=auto with a JEV key: the situation in which the fast path may skip the call."""
    monkeypatch.setattr(core, "BACKEND", "auto")
    monkeypatch.setattr(core, "jev_endpoint", lambda: ("typesafe", "https://x", "k", "m"))


# ---- 1. the in-session tier stays in-session -------------------------------------------------------------

@pytest.mark.parametrize("model", ["fable", "opus", "haiku", "sonnet"])
def test_main_tier_never_delegates_whatever_jev_picks(monkeypatch, model):
    fake_jev(monkeypatch, task="qa", level=1, model=model)
    d, text, _, _ = core.route("Explain briefly how a hash map handles collisions in practice", "claude", backend="jev")
    assert d["primary"] == "main"
    assert "target_agent" not in d and "worker" not in text
    assert "Answer directly in this session; do not spawn a subagent." in text


def test_delegating_tiers_still_take_jevs_model_pick(monkeypatch):
    fake_jev(monkeypatch, task="general", level=2, model="sonnet", effort="low")
    d, text, _, _ = core.route("Write a long cycle of haiku about the seasons, with commentary", "claude", backend="jev")
    assert d["target_agent"] == "sonnet-worker-low" and "`sonnet-worker-low`" in text


# ---- 2. cost cap -------------------------------------------------------------------------------------------

def _result(primary, task="general", level=0, model=None):
    d = {"primary": primary, "task": task, "level": level, "notes": []}
    if model:
        d["model"] = model
    return d


CC = core.load_json("routes.json", {})["claude"]["cost_cap"]


@pytest.mark.parametrize("primary, task, level, model, want_primary, want_model", [
    ("deep", "qa", 0, None, "sonnet", None),               # level 0 never reaches opus
    ("fable", "general", 0, None, "sonnet", None),         # ... nor fable
    ("fast", "general", 0, "fable", "fast", "sonnet"),     # JEV's pick is lowered to the cap
    ("deep", "qa", 1, None, "sonnet", None),               # task table: plain level 1 stays on sonnet
    ("deep", "code", 1, "opus", "deep", "opus"),           # code level 1 may use opus
    ("fable", "code", 1, "fable", "deep", "opus"),
    ("deep", "code", 2, "fable", "deep", "fable"),         # hard work is not capped
    ("main", "general", 0, "opus", "main", "sonnet"),
])
def test_cost_cap_table(primary, task, level, model, want_primary, want_model):
    r = _result(primary, task, level, model)
    core.apply_cost_cap(r, CC)
    assert r["primary"] == want_primary and r.get("model") == want_model


def test_level_zero_never_gets_opus_or_fable_through_decide(monkeypatch):
    routes = core.load_json("routes.json", {})["claude"]
    for family in ("opus", "fable"):
        answers = jev_answers("general", 0, family)["answers"]
        d = core.decide(answers, routes, prompt="Organize my downloads folder by file type and date")
        assert d.get("model") in (None, "haiku", "sonnet"), d


def test_short_plain_requests_never_run_above_haiku(monkeypatch):
    fake_jev(monkeypatch, task="general", level=2, model="fable")
    d, text, _, _ = core.route("Reply with the single word ok", "claude", backend="jev")
    assert d["primary"] == "fast" and d.get("target_model") == "haiku", d
    d, _, _, _ = core.route("Mi a főváros Franciaországban?", "claude", backend="jev")
    assert d["primary"] == "fast" and d.get("target_model") == "haiku", d


def test_trivial_short_requests_stay_inline(monkeypatch):
    # a subagent for trivial work costs more (an extra main-model turn, a fresh context) than it saves
    fake_jev(monkeypatch, task="general", level=0, model="fable")
    d, text, _, _ = core.route("Reply with the single word ok", "claude", backend="jev")
    assert d["primary"] == "main" and "target_model" not in d and "target_agent" not in d, d
    assert "do not spawn a subagent" in text


def test_short_but_code_is_not_inline_by_the_short_rule():
    assert not core.is_short_chat("fix utils.py", 40)
    assert not core.is_short_chat("what does `ls -la` do", 40)
    assert not core.is_short_chat("write a python function", 40)
    assert core.is_short_chat("what is the capital of Peru", 40)
    assert not core.is_short_chat("Organize my downloads folder by file type", 40)


@pytest.mark.parametrize("tag, agent", [("#opus", "opus-worker-"), ("#fable", "fable-worker-"), ("#haiku", "haiku-worker-")])
def test_explicit_override_still_wins_over_the_cap_and_the_short_rule(tag, agent):
    d, text, _, _ = core.route(f"{tag} ok", "claude")
    assert d["backend"] == "override" and d["target_agent"].startswith(agent)


# ---- 3. go-aheads and session questions need the parent's context ------------------------------------------

@pytest.mark.parametrize("prompt", [
    "Mehetnek igen mind", "mehetnek mind", "Mehet, igen mind", "Igen, mehet mindet", "mehetnek", "yes do all",
    "go ahead", "Yes, do all of it", "commit it", "push it", "commit", "fix that", "and this?", "ezt is", "ugyanezt a masikra",
    "what about the other one", "ok", "Rendben, csinaljuk", "jovahagyom",
])
def test_go_aheads_and_pronoun_followups_are_continuations(prompt):
    assert core.is_continuation(prompt), prompt


@pytest.mark.parametrize("prompt", [
    "commit the report to the wiki and notify finance", "Mehet, de előbb írj egy új modult a backendhez",
    "fix the login bug in auth.py", "what is this error in the logs", "mi az a docker", "run the full benchmark suite again",
])
def test_real_work_is_not_a_continuation(prompt):
    assert not core.is_continuation(prompt), prompt


def test_go_ahead_reuses_the_previous_delegation_never_downgrades(monkeypatch):
    previous = {"task": "code", "level": 2, "primary": "deep", "effort": "xhigh", "model": "opus", "extra_agents": 0,
                "verify": "", "lang": "hu"}
    d, text, _, _ = core.route("Mehetnek igen mind", "claude", previous=previous)
    assert d["backend"] == "continuation" and d["primary"] == "deep" and d["target_agent"] == "opus-worker-xhigh"
    d, text, _, _ = core.route("commit it", "claude", previous=dict(previous, primary="main", level=1, model=None, effort="low"))
    assert d["primary"] == "main" and "target_agent" not in d


def test_go_ahead_without_history_stays_inline_instead_of_spawning_a_fresh_subagent(monkeypatch):
    fake_jev(monkeypatch, task="code", level=2, model="fable")
    for prompt in ("Mehetnek igen mind", "yes do all", "commit it"):
        d, text, _, _ = core.route(prompt, "claude", backend="jev")
        assert d["primary"] == "main" and "target_agent" not in d and "worker" not in text, prompt


@pytest.mark.parametrize("prompt", ["Which model are you using?", "melyik modellel dolgozol?", "how much context is left",
                                    "mit csináltunk eddig?", "What did we just change?"])
def test_meta_questions_are_answered_inline_even_after_a_delegation(prompt):
    assert core.is_meta_question(prompt)
    previous = {"task": "code", "level": 2, "primary": "deep", "effort": "high", "model": "opus", "lang": "en"}
    d, text, _, _ = core.route(prompt, "claude", previous=previous)
    assert d["primary"] == "main" and "target_agent" not in d


@pytest.mark.parametrize("prompt", ["What model is best for time series forecasting?", "Explain what a model class is",
                                    "Write a function that summarizes the conversation history"])
def test_ordinary_questions_are_not_meta(prompt):
    assert not core.is_meta_question(prompt)


# ---- 4. latency: fast path, timeout, cache --------------------------------------------------------------

def test_jev_timeout_is_short():
    assert core.TIMEOUT_S <= 2.0


def test_short_plain_prompt_skips_the_network(monkeypatch):
    auto_jev(monkeypatch)
    calls = fake_jev(monkeypatch)
    d, _, _, err = core.route("What is the capital of Peru?", "claude")
    assert calls == [] and err is None and d["fast_path"] == "short" and d["backend"] == "local"


def test_confident_local_classifier_skips_the_network(monkeypatch):
    auto_jev(monkeypatch)
    calls = fake_jev(monkeypatch)
    d, _, _, _ = core.route("Write unit tests with pytest and mocks for the parser class, with coverage", "claude")
    assert calls == [] and d["fast_path"] == "local-confident" and d["task"] == "test"


def test_tasks_whose_tier_depends_on_trivial_vs_moderate_always_ask_jev(monkeypatch):
    # math: main vs deep. The local level guess is a coin flip there.
    auto_jev(monkeypatch)
    calls = fake_jev(monkeypatch)
    d, _, _, _ = core.route("Compute the derivative of x squared", "claude")
    assert "fast_path" not in d and len(calls) == 1


def test_same_tier_at_low_levels_follows_the_route_table():
    routes = {"table": {"qa": ["main", "main", "deep"], "general": ["fast", "main", "deep"],
                        "math": ["main", "deep+verify:codex", "deep"], "test": ["test", "test", "deep"]}}
    assert core._same_tier_at_low_levels("qa", routes) and core._same_tier_at_low_levels("test", routes)
    assert not core._same_tier_at_low_levels("general", routes) and not core._same_tier_at_low_levels("math", routes)
    assert core._same_tier_at_low_levels("unknown", routes)


def test_difficulty_criteria_stay_short_and_self_contained():
    # They are sent on every JEV request: keep them small, and keep the examples generic.
    assert sum(len(c) for c in core.DIFFICULTY) < 1500 and len(core.DIFFICULTY) == 3


def test_uncertain_or_hard_prompts_still_ask_jev(monkeypatch):
    auto_jev(monkeypatch)
    calls = fake_jev(monkeypatch)
    core.route("Refaktoráld az egész kódbázist hexagonális architektúrára", "claude")
    core.route("Hmm, can you take a look at the thing from before and tell me what you think about it", "claude")
    assert len(calls) == 2


def test_explicit_backend_is_never_skipped(monkeypatch):
    calls = fake_jev(monkeypatch)
    core.route("What is the capital of Peru?", "claude", backend="jev")
    assert len(calls) == 1


def test_decision_cache_answers_a_repeated_prompt_without_the_network(monkeypatch, tmp_path):
    calls = fake_jev(monkeypatch, task="code", level=2)
    prompt = "Refaktoráld az egész kódbázist hexagonális architektúrára"
    first, _, _, _ = core.route(prompt, "claude", backend="jev")
    second, _, _, _ = core.route(prompt, "claude", backend="jev")
    assert len(calls) == 1 and second.get("cached") and not first.get("cached")
    assert (first["primary"], first["level"]) == (second["primary"], second["level"])
    assert (tmp_path / "state" / "answer_cache.json").is_file()
    core.route(prompt + " most", "claude", backend="jev")  # a different prompt is a different key
    assert len(calls) == 2


def test_decision_cache_is_bounded_and_expires(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "ANSWERS", store.Store("answer_cache", ttl=core.CACHE_TTL_S, limit=5))
    for i in range(12):
        core._cache_put(f"k{i}", {"task": {"choice": "qa"}})
    data = json.loads((tmp_path / "state" / "answer_cache.json").read_text(encoding="utf-8"))
    assert len(data) == 5 and "k11" in data and "k0" not in data
    monkeypatch.setattr(core.ANSWERS, "ttl", -1)
    assert core._cache_get("k11") is None


def test_broken_cache_never_breaks_routing(monkeypatch, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "answer_cache.json").write_text("{not json", encoding="utf-8")
    fake_jev(monkeypatch)
    d, _, _, err = core.route("Draft a one-page project plan for the new onboarding flow", "claude", backend="jev")
    assert err is None and d["backend"] == "jev"
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state" / "answer_cache.json")  # a file where a folder is needed
    core._cache_put("x", {})  # swallowed


def test_urllib_is_imported_lazily():
    code = "import sys, trirouter.core; print('urllib.request' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(core.CFG_DIR.parents[1]))
    assert out.stdout.strip() == "False", out.stderr


# ---- 5. request size ------------------------------------------------------------------------------------

def test_skill_descriptions_are_trimmed_and_candidates_capped():
    assert core.SKILL_CANDIDATES == 5
    q = core.build_questions({"pdf": "x" * 500, "docs": "short description"})
    assert len(q["skill_pdf"]["instructions"]) < 120 + 80
    assert q["skill"]["criteria"]["pdf"].endswith("...") and len(q["skill"]["criteria"]["pdf"]) <= core.SKILL_DESC_CHARS
    assert q["skill"]["criteria"]["docs"] == "short description"


# ---- 6. boilerplate ------------------------------------------------------------------------------------

def test_no_parallelism_line_when_none_and_minimal_inline_text():
    d, text, _, _ = core.route("Explain the difference between a process and a thread in detail please", "claude")
    assert "Parallelism" not in text and "Reasoning effort" not in text
    assert text.count("[router]") == 1 and text.endswith("Respond in English.")


def test_parallelism_line_still_names_extra_agents():
    _, text, _, _ = core.route("Migrate the whole backend API, the React frontend and the database schema to the new auth",
                               "claude")
    assert "Parallelism: up to 2 extra agent(s)" in text


# ---- 7. quality -----------------------------------------------------------------------------------------

def test_difficulty_criteria_carry_examples():
    assert all("E.g." in c for c in core.DIFFICULTY)


@pytest.mark.parametrize("prompt", ["Fix the bug in utils.py", "Rename getUser() to fetchUser everywhere", "Write a function that parses src/data.csv",
                                    "Implement the endpoint and then add a test"])
def test_code_work_is_at_least_level_one_even_if_jev_says_zero(prompt):
    routes = core.load_json("routes.json", {})["claude"]
    d = core.decide(jev_answers("code", 0)["answers"], routes, prompt=prompt)
    assert d["level"] == 1
    d = core.decide(jev_answers("code", 0)["answers"], routes, prompt="print hello in python")
    assert d["level"] == 0


def test_code_bump_does_not_undo_the_level_one_cap():
    routes = core.load_json("routes.json", {})["claude"]
    d = core.decide(jev_answers("code", 0, "fable")["answers"], routes, prompt="Fix the bug in utils.py")
    assert d["level"] == 1 and d["primary"] == "main" and d.get("model") == "opus"  # level 1: opus at most


@pytest.mark.parametrize("prompt", [
    "Write a short thank-you email to my colleague.", "Write a polite letter to my landlord saying the heating repair is delayed.",
    "irj egy rovid emailt a csapatnak hogy holnap csuszik a meeting", "Írj egy köszönő levelet a tanáromnak",
    "Draft a reply to this message", "Compose a cover letter for a data analyst job",
])
def test_drafting_is_not_flagged_even_when_jev_is_nervous(monkeypatch, prompt):
    assert core.is_drafting(prompt) and not core.is_destructive(prompt)
    fake_jev(monkeypatch, task="general", level=0, destructive=0.8)
    _, text, hit, _ = core.route(prompt, "claude", backend="jev")
    assert not hit and "SAFETY" not in text


@pytest.mark.parametrize("prompt", [
    "Write a thank-you email and send it to my boss", "Write an email to the client and send it", "Send an email to John",
    "irj egy emailt es kuldd el", "Küldd el ezt az e-mailt Péternek", "Write a letter and then send it", "Delete the draft email",
    "Write an email that deletes nothing",
])
def test_sending_and_deleting_are_still_flagged(monkeypatch, prompt):
    assert core.is_destructive(prompt) and not core.is_drafting(prompt)
    fake_jev(monkeypatch, destructive=0.05)
    _, text, hit, _ = core.route(prompt, "claude", backend="jev")
    assert hit and "SAFETY" in text


def test_jev_destructive_verdict_still_counts_for_non_drafting(monkeypatch):
    fake_jev(monkeypatch, destructive=0.8)
    _, text, hit, _ = core.route("Clean up everything on the staging cluster for me", "claude", backend="jev")
    assert hit and "SAFETY" in text


def test_short_math_is_not_capped_by_the_short_chat_rule(monkeypatch):
    fake_jev(monkeypatch, task="math", level=2, model="opus")
    d, _, _, _ = core.route("Solve the integral of x*e^x dx", "claude", backend="jev")
    assert d["primary"] != "fast" and d.get("target_model") != "haiku", d
