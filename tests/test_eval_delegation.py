"""The eval's delegation metric and pass/fail gates, on synthetic decisions (no routing, no network)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("eval_router", Path(__file__).resolve().parents[1] / "eval" / "eval_router.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def test_delegation_of():
    assert ev.delegation_of({"primary": "main", "backend": "local"}) == ("inline", "", "")
    assert ev.delegation_of({"primary": "fast", "target_agent": "haiku-worker-low", "target_model": "haiku"}) \
        == ("delegated", "haiku-worker-low", "haiku")
    assert ev.delegation_of({"primary": "cli:codex", "target_model": "gpt-5.6-terra"}) == ("delegated", "", "gpt")
    assert ev.delegation_of({"primary": "deep"})[0] == "unknown"
    assert ev.delegation_of(None)[0] == "unknown"


def row(outcome, diff=0, tier_true="main", family="fable"):
    return {"outcome": outcome, "diff_true": diff, "tier_true": tier_true, "family": family if outcome == "delegated" else "",
            "agent": "x-worker-low" if outcome == "delegated" else "", "prompt": "p"}


def test_report_delegation_counts_level0_failures(capsys):
    out = [row("inline"), row("delegated"), row("delegated", diff=0, tier_true="fast"), row("inline", diff=2)]
    share = ev.report_delegation(out)
    assert share == 1 / 4
    text = capsys.readouterr().out
    assert "Delegation rate:      50%" in text and "fable 2" in text


def test_report_delegation_degrades(capsys):
    assert ev.report_delegation([row("unknown")]) is None
    assert "n/a" in capsys.readouterr().out


def test_verdict_checks_and_states_every_target():
    measured = {"task": 0.85, "uncertain": 0.30, "destr_recall": 1.0, "destr_fp": 0.0, "lang": 1.0, "level0_delegated": None}
    assert ev.verdict(measured, ev.TARGETS) == (True, "PASS  (targets: task>=85%, uncertain<=30%, recall 100%, FP<5%, "
                                                      "lang 100%, level-0 delegated<=5%)")
    assert ev.verdict(measured, ev.REAL_TARGETS)[1].startswith("PASS  (targets: task>=70%, uncertain<=45%, recall 100%")
    for metric, value in (("task", 0.84), ("uncertain", 0.31), ("destr_recall", 0.99), ("destr_fp", 0.05), ("lang", 0.99),
                          ("level0_delegated", 0.06)):
        assert ev.verdict({**measured, metric: value}, ev.TARGETS)[0] is False, metric
