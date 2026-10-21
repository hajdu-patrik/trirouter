#!/usr/bin/env python3
"""Router quality through the full core.route pipeline.

    python eval/eval_router.py [eval/en_prompts.csv ...]

Sets: hu_prompts / en_prompts (curated) and real_prompts (anonymized shapes of real traffic: typos,
missing accents, pasted blocks, go-aheads). Optional columns: `lang` (expected reply language, else
taken from the file name) and `previous` (an earlier prompt of the same session, routed first).

Besides task accuracy the report shows the share of "routing uncertain" decisions and the tier
accuracy next to the best fixed-tier baseline: a router is only useful if it beats always picking the
same tier. Exits 1 if any target is missed. Details: eval/results_<name>.csv.
"""
import csv
import operator
import os
import sys
from collections import Counter
from pathlib import Path
from statistics import fmean

# Measure with the defaults, as CI does: a local ROUTER_MIN_CONFIDENCE would change every number.
for _var in [v for v in os.environ if v.startswith("ROUTER_")]:
    del os.environ[_var]
os.environ["ROUTER_DISTILLED"] = "0"  # never let a model trained on the user's own traffic (~/.trirouter) change the numbers

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trirouter import core  # noqa: E402

# level0_delegated (share of all prompts): level-0 prompts whose expected tier is the inline `main` but
# which the final routing outcome hands to a subagent / another tool.
TARGETS = {"task": 0.85, "uncertain": 0.30, "destr_recall": 1.0, "destr_fp": 0.05, "lang": 1.0, "level0_delegated": 0.05}
# Regression gates for the real-traffic set, not goals: the local classifier is weak on it by design,
# JEV is the fix. Raise them whenever the measured numbers improve.
REAL_TARGETS = {**TARGETS, "task": 0.70, "uncertain": 0.45}
# (metric, how the RESULT line states its target, passes(measured, target)); a metric measured as None is skipped
GATES = (("task", "task>={:.0%}", operator.ge), ("uncertain", "uncertain<={:.0%}", operator.le),
         ("destr_recall", "recall {:.0%}", operator.ge), ("destr_fp", "FP<{:.0%}", operator.lt),
         ("lang", "lang {:.0%}", operator.ge), ("level0_delegated", "level-0 delegated<={:.0%}", operator.le))


def expected_tier(task, difficulty, routes):
    row = routes.get("table", {}).get(task) or ["main"] * 3
    return row[min(int(difficulty), len(row) - 1)].partition("+verify:")[0]


def model_family(model):
    """'sonnet' for a Claude alias, 'gpt' / 'gemini' for the other CLIs, '' when nothing is delegated."""
    return (model or "").lower().split("-")[0]


def delegation_of(d):
    """Final routing outcome from the decision core.route returns (render() records target_agent /
    target_model on it): ('inline', '', '') or ('delegated', agent, family). ('unknown', '', '') when
    the decision lacks what the outcome is built from (core.py changed under us)."""
    if not isinstance(d, dict) or "primary" not in d:
        return "unknown", "", ""
    agent, model = d.get("target_agent") or "", d.get("target_model") or ""
    if agent or model:
        return "delegated", agent, model_family(model)
    if d["primary"] != "main" and "target_model" not in d and "target_agent" not in d and d.get("backend") != "override":
        return "unknown", "", ""  # a non-inline tier that was never rendered: cannot tell
    return "inline", "", ""


def route_row(r, lang_expected, routes):
    previous = None
    if r.get("previous"):
        previous = core.route(r["previous"], "claude")[0]
    d, _, hit, err = core.route(r["prompt"], "claude", previous=previous)
    outcome, agent, family = delegation_of(d)
    return {"id": r["id"], "prompt": r["prompt"], "task_true": r["task"], "task_pred": d["task"],
            "task_conf": d["task_conf"], "diff_true": int(r["difficulty"]), "diff_pred": d["level"],
            "destr_true": int(r["destructive"]), "destr_pred": int(hit), "lang": d.get("lang"),
            "lang_true": r.get("lang") or lang_expected, "uncertain": int("routing uncertain" in d.get("notes", [])),
            "tier_true": expected_tier(r["task"], r["difficulty"], routes), "tier": d["primary"],
            "verify": d.get("verify") or "", "effort": d.get("effort"), "skill": d.get("skill") or "",
            "backend": d["backend"], "outcome": outcome, "agent": agent, "family": family,
            "error": err or ""}


def counts(values):
    return ", ".join(f"{k} {v}" for k, v in Counter(values).most_common())


def print_prompts(label, rows):
    for o in rows:
        print(f"    {label}: {o['prompt'][:110]}")


def report_task(out):
    task_acc = fmean(o["task_true"] == o["task_pred"] for o in out)
    print(f"Task type accuracy:   {task_acc:.0%}")
    for (t, p), c in Counter((o["task_true"], o["task_pred"]) for o in out if o["task_true"] != o["task_pred"]).most_common(6):
        print(f"    {t} -> {p}: {c}x")
    exact = fmean(o["diff_true"] == o["diff_pred"] for o in out)
    near = fmean(abs(o["diff_true"] - o["diff_pred"]) <= 1 for o in out)
    print(f"Difficulty:           exact {exact:.0%}, within +-1 {near:.0%}")
    uncertain = fmean(o["uncertain"] for o in out)
    print(f"Routing uncertain:    {uncertain:.0%}")
    return {"task": task_acc, "uncertain": uncertain}


def report_tiers(out):
    acc = fmean(o["tier"] == o["tier_true"] for o in out)
    best_tier, best_n = Counter(o["tier_true"] for o in out).most_common(1)[0]
    print(f"Tier accuracy:        {acc:.0%}  (best fixed tier '{best_tier}': {best_n / len(out):.0%})")
    print("Tiers picked:         " + counts(o["tier"] + (f"+{o['verify']}" if o["verify"] else "") for o in out))
    return {}


def report_delegation(out):
    """Final outcome per row (inline vs delegated). level0_delegated: the share of level-0 'main'-expected
    prompts that were delegated anyway, None when the outcome cannot be determined."""
    known = [o for o in out if o["outcome"] != "unknown"]
    if len(known) < len(out):
        print(f"Delegation:           n/a - {len(out) - len(known)} of {len(out)} decisions carry no render "
              "info (target_agent/target_model); is trirouter.core.route/render mid-change?")
        if not known:
            return {"level0_delegated": None}
    deleg = [o for o in known if o["outcome"] == "delegated"]
    print(f"Delegation rate:      {len(deleg) / len(known):.0%} ({len(deleg)} of {len(known)})")
    print("Model families:       " + (counts(o["family"] or "?" for o in deleg) or "none (all inline)"))
    print("Agents:               " + (counts(o["agent"] or "(no agent)" for o in deleg) or "none"))
    lvl0 = [o for o in known if o["diff_true"] == 0]
    lvl0_deleg = [o for o in lvl0 if o["outcome"] == "delegated"]
    bad = [o for o in lvl0_deleg if o["tier_true"] == "main"]
    print(f"Delegated at level 0: {len(lvl0_deleg)} of {len(lvl0)} level-0 prompts; "
          f"{len(bad)} of them expected inline (counted as failures)")
    print_prompts("DELEGATED@0", bad)
    return {"level0_delegated": len(bad) / len(known)}


def report_destructive(out):
    pos = [o for o in out if o["destr_true"]]
    neg = [o for o in out if not o["destr_true"]]
    recall = fmean(o["destr_pred"] for o in pos) if pos else 1.0
    fp = fmean(o["destr_pred"] for o in neg) if neg else 0.0
    print(f"Destructive:          recall {recall:.0%} ({len(pos)} positives), false positives {fp:.0%}")
    print_prompts("MISSED", [o for o in pos if not o["destr_pred"]])
    print_prompts("FALSE+", [o for o in neg if o["destr_pred"]])
    return {"destr_recall": recall, "destr_fp": fp}


def report_lang(out):
    lang_acc = fmean(o["lang"] == o["lang_true"] for o in out)
    print(f"Reply language:       {lang_acc:.0%}")
    print_prompts("LANG", [o for o in out if o["lang"] != o["lang_true"]])
    return {"lang": lang_acc}


def write_results(name, out):
    with open(ROOT / "eval" / f"results_{name}.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)


def verdict(measured, targets):
    """(passed, the RESULT line's text after the label)."""
    ok = all(measured[m] is None or passes(measured[m], targets[m]) for m, _, passes in GATES)
    return ok, ("PASS" if ok else "FAIL") + f"  (targets: {', '.join(shown.format(targets[m]) for m, shown, _ in GATES)})"


def evaluate(path):
    name = Path(path).stem
    lang_expected = "en" if name.startswith("en_") else "hu"
    targets = REAL_TARGETS if name.startswith("real_") else TARGETS
    routes = core.load_json("routes.json", {}).get("claude", {})
    with open(path, encoding="utf-8-sig", newline="") as fh:
        out = [route_row(r, lang_expected, routes) for r in csv.DictReader(fh)]
    print(f"\n=== {name}  ({len(out)} prompts, backend: {counts(o['backend'] for o in out)})")
    measured = {}  # each report prints its section and returns the metrics it measured
    for report in (report_task, report_tiers, report_delegation, report_destructive, report_lang):
        measured.update(report(out))
    print("Efforts:              " + counts(o["effort"] for o in out))
    print("Skills picked:        " + (counts(o["skill"] for o in out if o["skill"]) or "none"))
    write_results(name, out)
    ok, result = verdict(measured, targets)
    print("RESULT:               " + result)
    return ok


def main(paths):
    failed = [p for p in paths if not evaluate(p)]  # every file is reported, even after a failure
    return 1 if failed else 0


if __name__ == "__main__":
    default = [str(ROOT / "eval" / f"{n}_prompts.csv") for n in ("hu", "en", "real")]
    sys.exit(main(sys.argv[1:] or default))
