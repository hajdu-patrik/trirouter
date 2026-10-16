"""The distilled routing model (distill.py): training and calibration on synthetic data, the saved model, the fast
path that uses it, and the daily retraining check. Nothing here touches the real ~/.trirouter."""
import json
import os
import random
from pathlib import Path

import pytest

from trirouter import core, discovery, distill


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    """A temporary state folder, the distilled model switched back on (conftest switches it off), no JEV traffic."""
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    monkeypatch.delenv("ROUTER_DISTILLED", raising=False)
    monkeypatch.setattr(core.catalog, "load_catalog", lambda *a, **k: [])
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(distill, "eval_files", lambda: [])   # the base set is covered by its own test: keep the rest fast
    distill._cache.clear()
    yield tmp_path
    distill._cache.clear()


FILL = "alpha beta gamma delta kappa sigma omega lambda theta zeta".split()
CLUSTERS = [  # (template, task, level): disjoint vocabulary, tiers main / main / test / main in the Claude table
    ("write a short thank you note to {w} about the {v} dinner", "general", 0),
    ("fix the crash in module {w}.py when the {v} parser gets empty input", "code", 1),
    ("write pytest unit tests for the {w} service and mock the {v} client", "test", 1),
    ("explain in detail how {w} replication works and why {v} matters", "qa", 1),
]


def synthetic(n_per_cluster=25, seed=1, shuffle_labels=False):
    rnd = random.Random(seed)
    out = []
    for template, task, level in CLUSTERS:
        for _ in range(n_per_cluster):
            out.append([template.format(w=rnd.choice(FILL), v=rnd.choice(FILL)) + " " + " ".join(rnd.sample(FILL, 2)), task, level])
    if shuffle_labels:
        labels = [(t, l) for _, t, l in out]
        rnd.shuffle(labels)
        out = [[p, t, l] for (p, _, _), (t, l) in zip(out, labels)]
    return out


def entry(prompt, task, level, backend="jev", conf=0.9):
    return {"ts": "2026-01-01T00:00:00", "provider": "claude", "prompt": prompt, "task": task, "task_conf": conf,
            "level": level, "primary": "main", "backend": backend}


def write_log(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


@pytest.fixture
def log(state):
    path = state / "logs" / "routing.jsonl"
    write_log(path, [entry(*row) for row in synthetic()])
    return path


# ---- training data --------------------------------------------------------------------------------------------

def test_tier_of_strips_the_verify_suffix():
    table = {"math": ["main", "deep", "deep+verify:antigravity"], "qa": ["main", "main", "deep"]}
    assert distill.tier_of("math", 2, table) == "deep"
    assert distill.tier_of("qa", 0, table) == "main"
    assert distill.tier_of("unknown", 1, table) == "main"


def test_log_samples_dedupe_and_skip(state):
    path = state / "logs" / "routing.jsonl"
    write_log(path, [
        entry("Write the report for the quarter", "general", 1),
        entry("write  the REPORT for the quarter", "general", 2),          # same normalised prompt: newest label wins
        entry("fix the parser in main.py", "code", 1, backend="local"),   # not a JEV answer
        entry("#opus do it", "override", 1),                               # manual override
        entry("mehet", "general", 1),                                      # a go-ahead
        entry("which model are you?", "qa", 0),                            # a question about the session
        entry("summarise <pasted_content>lots of text</pasted_content>", "study", 1),
        entry("explain how TCP works", "bogus", 1),                        # unknown task
    ])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("not json\n")
    samples, entries = distill.log_samples(path)
    assert [(v[1], v[2]) for v in samples.values()] == [("general", 2)]
    assert list(entries.values()) == [2]


def test_base_samples_read_the_eval_csvs():
    files = sorted((Path(core.__file__).resolve().parents[1] / "eval").glob("*_prompts.csv"))
    assert len(files) >= 3
    base = distill.base_samples(files)
    assert len(base) > 100 and all(v[1] in distill.CLASSES["task"] and v[2] in (0, 1, 2) for v in base.values())


# ---- training, calibration, save/load -------------------------------------------------------------------------

def test_train_enables_on_clear_data_and_round_trips(log, state):
    rep = distill.train(apply=True, log=log, files=[])
    assert rep["enabled"] and rep["threshold"] is not None and rep["applied"]
    assert rep["cv"]["tier_agreement_all"] >= 0.92
    assert rep["cv"]["at_threshold"]["coverage"] >= distill.MIN_COVERAGE
    assert rep["cv"]["at_threshold"]["tier_agreement"] >= distill.TARGET_AGREEMENT
    assert rep["classes"]["code"] == [0, 25, 0]
    saved = json.loads(distill.model_file().read_text(encoding="utf-8"))
    assert saved["version"] == distill.VERSION and saved["enabled"] and saved["n_user"] == 100
    assert saved["log"]["size"] == log.stat().st_size and saved["log"]["sha"]
    assert saved["table"] == distill.claude_table()
    p = distill.predict("write a short thank you note to sigma about the beta dinner")
    assert (p["task"], p["level"], p["tier"]) == ("general", 0, "main") and p["confident"]
    p = distill.predict("write pytest unit tests for the zeta service and mock the omega client")
    assert (p["task"], p["tier"]) == ("test", "test") and p["confident"]
    assert any("tier" in line for line in distill.report_lines(rep))


def test_dry_run_writes_nothing(log, state):
    rep = distill.train(apply=False, log=log, files=[])
    assert rep["enabled"] and not rep["applied"] and rep["model_bytes"] > 0
    assert not distill.model_file().exists()
    assert distill.predict("anything") is None


def test_disabled_when_labels_are_ambiguous(state):
    path = state / "logs" / "routing.jsonl"
    write_log(path, [entry(*row) for row in synthetic(shuffle_labels=True)])
    rep = distill.train(apply=True, log=path, files=[])
    assert not rep["enabled"] and rep["threshold"] is None and "no threshold" in rep["reason"]
    saved = json.loads(distill.model_file().read_text(encoding="utf-8"))
    assert saved["enabled"] is False
    p = distill.predict("write a short thank you note to sigma about the beta dinner")
    assert p is not None and p["confident"] is False   # a disabled model never claims to be sure
    assert any(line.lstrip().startswith("disabled") for line in distill.report_lines(rep))


def test_disabled_when_too_little_data(state):
    path = state / "logs" / "routing.jsonl"
    write_log(path, [entry(*row) for row in synthetic(n_per_cluster=5)])
    rep = distill.train(apply=True, log=path, files=[])
    assert not rep["enabled"] and "distinct JEV-labelled prompts" in rep["reason"]
    assert json.loads(distill.model_file().read_text(encoding="utf-8"))["enabled"] is False


def test_no_log_trains_nothing(state):
    rep = distill.train(apply=True, log=state / "logs" / "missing.jsonl", files=[])
    assert not rep["enabled"] and not rep["applied"] and not distill.model_file().exists()


def test_calibrate_picks_the_lowest_qualifying_threshold():
    rows = [(0.99, True, True, True, 1)] * 20 + [(0.8, True, True, True, 1)] * 10 + [(0.8, True, True, False, 1)] * 10
    # >= 0.5: 40 rows, 30 right (75%); >= 0.85: 20 rows, all right
    t, agreement, coverage = distill.calibrate(rows)
    assert t == 0.85 and agreement == 1.0 and coverage == 0.5
    assert distill.calibrate([(0.99, True, True, False, 1)] * 40) is None
    assert distill.calibrate([(0.99, True, True, True, 1)] * 5 + [(0.1, True, True, False, 1)] * 95) is None  # < MIN_COVERED


def test_long_prompts_are_never_confident(log):
    distill.train(apply=True, log=log, files=[])
    short = "write a short thank you note to sigma about the beta dinner"
    assert distill.predict(short)["confident"]
    assert not distill.predict(short + " " + "please " * 40)["confident"]


def test_load_is_cached_by_mtime(log, state):
    distill.train(apply=True, log=log, files=[])
    first = distill.load()
    assert distill.load() is first
    path = distill.model_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["threshold"] = 0.123
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 5_000_000_000))
    assert distill.load()["threshold"] == 0.123


def test_broken_or_foreign_model_files_are_ignored(state):
    path = distill.model_file()
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert distill.load() is None and distill.predict("hello") is None
    path.write_text(json.dumps({"version": 99, "features": {}}), encoding="utf-8")
    assert distill.load() is None


def test_a_changed_route_table_makes_the_model_unsure(log, monkeypatch):
    distill.train(apply=True, log=log, files=[])
    text = "write a short thank you note to sigma about the beta dinner"
    assert distill.predict(text)["confident"]
    table = dict(distill.claude_table(), general=["fast", "main", "deep"])
    monkeypatch.setattr(distill, "claude_table", lambda: table)
    assert not distill.predict(text)["confident"]


# ---- the fast path ------------------------------------------------------------------------------------------

@pytest.fixture
def jev(monkeypatch):
    """A JEV key is configured, and every call is recorded (and answered as a code/deep task)."""
    monkeypatch.setattr(core, "BACKEND", "auto")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    calls = []

    def ask(prompt, questions):
        calls.append(prompt)
        return {"model": "jev-test", "answers": {
            "task": {"choice": "code", "confidence": 0.9}, "difficulty": {"score": 2, "confidence": 0.9},
            "long_context": {"noul": 0.1}, "needs_web": {"noul": 0.1}, "destructive": {"noul": 0.05},
            "agents": {"choice": "0", "confidence": 0.9}}}

    monkeypatch.setattr(core, "ask_jev", ask)
    return calls


SURE = "write a short thank you note to sigma about the beta dinner"


def test_fast_path_uses_the_model_only_when_confident(log, jev):
    distill.train(apply=True, log=log, files=[])
    d, _, _, err = core.route(SURE, "claude")
    assert jev == [] and err is None
    assert d["fast_path"] == "distilled" and d["backend"] == "local"
    assert d["task"] == "general" and d["level"] == 0 and d["primary"] == "main" and d["task_conf"] >= 0.6
    # nothing the model knows: JEV decides
    d, _, _, _ = core.route("zzqx vvkt plorp wubble", "claude")
    assert len(jev) == 1 and "fast_path" not in d and d["backend"] == "jev"


def test_fast_path_guards_still_hold(log, jev):
    distill.train(apply=True, log=log, files=[])
    pasted = SURE + " <pasted_content>some pasted text</pasted_content>"
    core.route(pasted, "claude")
    assert len(jev) == 1                                   # pasted text: JEV sees it
    core.route(SURE, "claude", backend="jev")
    assert len(jev) == 2                                   # an explicit backend is never second-guessed


def test_env_switch_turns_the_model_off(log, jev, monkeypatch):
    distill.train(apply=True, log=log, files=[])
    monkeypatch.setenv("ROUTER_DISTILLED", "0")
    d, _, _, _ = core.route(SURE, "claude")
    assert len(jev) == 1 and d["backend"] == "jev" and d.get("fast_path") != "distilled"


def test_disabled_model_is_not_used(state, jev):
    path = state / "logs" / "routing.jsonl"
    write_log(path, [entry(*row) for row in synthetic(shuffle_labels=True)])
    distill.train(apply=True, log=path, files=[])
    core.route(SURE, "claude")
    assert len(jev) == 1


def test_nothing_is_loaded_without_a_model_file(state, jev, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the model must not be used")
    monkeypatch.setattr(distill, "predict_with", boom)
    assert not distill.model_file().exists()
    d, _, _, _ = core.route(SURE, "claude")
    assert len(jev) == 1 and d["backend"] == "jev"
    assert distill._cache == {} and not distill.available()


def test_fast_path_not_taken_without_jev_access(log, monkeypatch):
    """No JEV key: the keyword classifier decides as before (the model is a stand-in for JEV, not for the fallback)."""
    distill.train(apply=True, log=log, files=[])
    monkeypatch.setattr(core, "BACKEND", "auto")
    d, _, _, _ = core.route(SURE, "claude")
    assert d.get("fast_path") != "distilled"


# ---- background retraining ----------------------------------------------------------------------------------

def test_maybe_retrain_throttles_and_needs_new_entries(log, state):
    t0 = 1_700_000_000.0
    rep = distill.maybe_retrain(now=t0)
    assert rep and rep["applied"] and distill.model_file().exists()          # no model yet: trains
    assert distill.maybe_retrain(now=t0 + 3600) is None                       # at most once a day
    assert distill.maybe_retrain(now=t0 + 25 * 3600) is None                  # a day later, but nothing new
    write_log(log, [entry(f"brand new prompt number {i} about {FILL[i % 10]}", "qa", 1) for i in range(distill.RETRAIN_MIN_NEW - 1)])
    assert distill.maybe_retrain(now=t0 + 50 * 3600) is None                  # 29 new entries: not enough
    write_log(log, [entry("one more prompt about kappa", "qa", 1)])
    write_log(log, [entry("local one", "qa", 1, backend="local")])           # local decisions do not count
    assert distill.maybe_retrain(now=t0 + 80 * 3600)["applied"]               # 30 new JEV entries
    assert distill.maybe_retrain(now=t0 + 81 * 3600) is None


def test_maybe_retrain_after_log_rotation(log, state):
    t0 = 1_700_000_000.0
    distill.maybe_retrain(now=t0)
    log.write_text("", encoding="utf-8")
    write_log(log, [entry(*row) for row in synthetic(seed=7)])               # a new, different log file
    assert distill.maybe_retrain(now=t0 + 25 * 3600)["applied"]


def test_maybe_retrain_respects_the_env_switch_and_never_raises(log, state, monkeypatch):
    monkeypatch.setenv("ROUTER_DISTILLED", "0")
    assert distill.maybe_retrain(now=1.0) is None and not distill.model_file().exists()
    monkeypatch.delenv("ROUTER_DISTILLED")
    monkeypatch.setattr(distill, "train", lambda **k: 1 / 0)
    assert distill.maybe_retrain(now=2.0, force=True) is None


def test_discovery_job_can_not_be_broken_by_learning(monkeypatch):
    monkeypatch.setattr(distill, "maybe_retrain", lambda: 1 / 0)
    discovery.learn_routing()                                                 # must not raise
    called = []
    monkeypatch.setattr(distill, "maybe_retrain", lambda: called.append(1))
    discovery.learn_routing()
    assert called == [1]
