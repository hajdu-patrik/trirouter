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
import os
import sys
from collections import Counter
from pathlib import Path

# Measure with the defaults, as CI does: a local ROUTER_MIN_CONFIDENCE would change every number.
for _var in [v for v in os.environ if v.startswith("ROUTER_")]:
    del os.environ[_var]

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trirouter import core  # noqa: E402

TARGETS = {"task": 0.85, "destr_recall": 1.0, "destr_fp": 0.05, "lang": 1.0, "uncertain": 0.30}
# Regression gates for the real-traffic set, not goals: the local classifier is weak on it by design,
# JEV is the fix. Raise them whenever the measured numbers improve.
REAL_TARGETS = {"task": 0.70, "destr_recall": 1.0, "destr_fp": 0.05, "lang": 1.0, "uncertain": 0.45}


def expected_tier(task, difficulty, routes):
    row = routes.get("table", {}).get(task) or ["main"] * 3
    return row[min(int(difficulty), len(row) - 1)].partition("+verify:")[0]


def route_row(r, lang_expected, routes):
    previous = None
    if r.get("previous"):
        previous, _, _, _ = core.route(r["previous"], "claude")
    d, _, hit, err = core.route(r["prompt"], "claude", previous=previous)
    return {"id": r["id"], "prompt": r["prompt"], "task_true": r["task"], "task_pred": d["task"],
            "task_conf": d["task_conf"], "diff_true": int(r["difficulty"]), "diff_pred": d["level"],
            "destr_true": int(r["destructive"]), "destr_pred": int(hit), "lang": d.get("lang"),
            "lang_true": r.get("lang") or lang_expected, "uncertain": int("routing uncertain" in d.get("notes", [])),
            "tier_true": expected_tier(r["task"], r["difficulty"], routes), "tier": d["primary"],
            "verify": d.get("verify") or "", "effort": d.get("effort"), "skill": d.get("skill") or "",
            "backend": d["backend"], "error": err or ""}


def counts(values):
    return ", ".join(f"{k} {v}" for k, v in Counter(values).most_common())


def print_prompts(label, rows):
    for o in rows:
        print(f"    {label}: {o['prompt'][:110]}")


def report_task(out):
    task_acc = sum(o["task_true"] == o["task_pred"] for o in out) / len(out)
    print(f"Task type accuracy:   {task_acc:.0%}")
    for (t, p), c in Counter((o["task_true"], o["task_pred"]) for o in out if o["task_true"] != o["task_pred"]).most_common(6):
        print(f"    {t} -> {p}: {c}x")
    exact = sum(o["diff_true"] == o["diff_pred"] for o in out) / len(out)
    near = sum(abs(o["diff_true"] - o["diff_pred"]) <= 1 for o in out) / len(out)
    print(f"Difficulty:           exact {exact:.0%}, within +-1 {near:.0%}")
    uncertain = sum(o["uncertain"] for o in out) / len(out)
    print(f"Routing uncertain:    {uncertain:.0%}")
    return task_acc, uncertain


def report_tiers(out):
    acc = sum(o["tier"] == o["tier_true"] for o in out) / len(out)
    tier_counts = Counter(o["tier_true"] for o in out)
    best_tier, best_n = tier_counts.most_common(1)[0]
    print(f"Tier accuracy:        {acc:.0%}  (best fixed tier '{best_tier}': {best_n / len(out):.0%})")
    print("Tiers picked:         " + counts(o["tier"] + (f"+{o['verify']}" if o["verify"] else "") for o in out))


def report_destructive(out):
    pos = [o for o in out if o["destr_true"]]
    neg = [o for o in out if not o["destr_true"]]
    recall = sum(o["destr_pred"] for o in pos) / len(pos) if pos else 1.0
    fp = sum(o["destr_pred"] for o in neg) / len(neg) if neg else 0.0
    print(f"Destructive:          recall {recall:.0%} ({len(pos)} positives), false positives {fp:.0%}")
    print_prompts("MISSED", [o for o in pos if not o["destr_pred"]])
    print_prompts("FALSE+", [o for o in neg if o["destr_pred"]])
    return recall, fp


def report_lang(out):
    lang_acc = sum(o["lang"] == o["lang_true"] for o in out) / len(out)
    print(f"Reply language:       {lang_acc:.0%}")
    print_prompts("LANG", [o for o in out if o["lang"] != o["lang_true"]])
    return lang_acc


def write_results(name, out):
    with open(ROOT / "eval" / f"results_{name}.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)


def evaluate(path):
    name = Path(path).stem
    lang_expected = "en" if name.startswith("en_") else "hu"
    targets = REAL_TARGETS if name.startswith("real_") else TARGETS
    routes = core.load_json("routes.json", {}).get("claude", {})
    with open(path, encoding="utf-8-sig", newline="") as fh:
        out = [route_row(r, lang_expected, routes) for r in csv.DictReader(fh)]
    print(f"\n=== {name}  ({len(out)} prompts, backend: {counts(o['backend'] for o in out)})")
    task_acc, uncertain = report_task(out)
    report_tiers(out)
    recall, fp = report_destructive(out)
    lang_acc = report_lang(out)
    print("Efforts:              " + counts(o["effort"] for o in out))
    print("Skills picked:        " + (counts(o["skill"] for o in out if o["skill"]) or "none"))
    write_results(name, out)
    ok = (task_acc >= targets["task"] and recall >= targets["destr_recall"] and fp < targets["destr_fp"]
          and lang_acc >= targets["lang"] and uncertain <= targets["uncertain"])
    print("RESULT:               " + ("PASS" if ok else "FAIL")
          + f"  (targets: task>={targets['task']:.0%}, uncertain<={targets['uncertain']:.0%}, recall 100%, FP<5%, lang 100%)")
    return ok


def main(paths):
    failed = [p for p in paths if not evaluate(p)]  # every file is reported, even after a failure
    return 1 if failed else 0


if __name__ == "__main__":
    default = [str(ROOT / "eval" / f"{n}_prompts.csv") for n in ("hu", "en", "real")]
    sys.exit(main(sys.argv[1:] or default))
