"""A small local model (Naive Bayes over word and char n-grams) distilled from JEV's answers in the routing log, so most
prompts need no network call; it skips JEV only above a threshold calibrated by cross-validation."""
import csv
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

from . import core, lang, platforms

VERSION = 1
CLASSES = {"task": list(core.TASKS), "level": [0, 1, 2]}
HEADS = ("task", "level")
SKIP_TASKS = {"override", "continuation"}

ALPHA = 0.1             # smoothing of the feature likelihoods (CV: 0.03-1.0 are within noise of each other)
SCALE = 30.0            # the likelihood sum is divided by this: ~600 correlated features otherwise give posteriors of 0/1
MIN_DF = 2              # features seen in fewer samples are pruned (keeps the file small and generalises better)
MAX_CHARS = 400         # feature extraction looks at this much of a prompt
DECIDE_MAX_CHARS = 200  # the log keeps only the first 200 chars of a prompt: the model never saw longer ones (and
                        # 23 of 26 'deep' answers were on such prompts), so it never decides one
BASE_WEIGHT = 0.5       # an eval-CSV prompt counts half as much as one of the user's own JEV-labelled prompts
LOWCONF_WEIGHT = 1.0    # weight of a log prompt JEV itself was unsure about (task_conf below ROUTER_MIN_CONFIDENCE)
CV_FOLDS = 5
TARGET_AGREEMENT = 0.92
MIN_COVERAGE = 0.10
MIN_COVERED = 15        # fewer held-out prompts than this above a threshold prove nothing
MIN_KNOWN = 6           # known features a prompt needs before the model's answer counts (else it is just the prior)
MIN_SAMPLES = 60        # distinct JEV-labelled prompts needed before a model is even trained
CONF_MODES = ("tier", "product", "min")
THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.97, 0.98, 0.99, 0.995, 0.999, 0.9995, 0.9999]
RETRAIN_MIN_NEW = 30    # new JEV entries in the log before maybe_retrain() trains again
CHECK_INTERVAL_S = 24 * 3600
ENV_OFF = "ROUTER_DISTILLED"
JEV_LATENCY_MS = 600    # p50 of the JEV call (measured on the routing log), for the "time saved" estimate

_WORD_RE = re.compile(r"[a-z0-9_]+")


# ---- files --------------------------------------------------------------------------------------------------

def model_file():
    return Path(core.STATE_DIR) / "state" / "distilled.json"


def log_file():
    return Path(core.STATE_DIR) / "logs" / "routing.jsonl"


def check_file():
    return Path(core.STATE_DIR) / "state" / "distilled_check.json"


def eval_files():
    return sorted((Path(core.__file__).resolve().parents[1] / "eval").glob("*_prompts.csv"))


# ---- features -----------------------------------------------------------------------------------------------

def features(text):
    """The set of binary features of a prompt: words, word pairs, char 3-5-grams inside words, a length bucket."""
    words = _WORD_RE.findall(lang.fold(text)[:MAX_CHARS])
    f = {"w:" + w for w in words}
    f.update("b:" + a + " " + b for a, b in zip(words, words[1:]))
    for w in words:
        p = "<" + w + ">"
        for n in (3, 4, 5):
            f.update("c:" + p[i:i + n] for i in range(len(p) - n + 1))
    f.add("n:%d" % min(len(words) // 3, 8))
    return f


def tier_of(task, level, table):
    """The tier a (task, level) pair routes to in a route table (core.tier_of on a bare table)."""
    return core.tier_of({"table": table}, task, level)


def claude_table():
    return ((core.load_json("routes.json", {}).get("claude") or {}).get("table")) or {}


def default_tier():
    return (core.load_json("routes.json", {}).get("claude") or {}).get("default") or "main"


# ---- training data ------------------------------------------------------------------------------------------

def key_of(text):
    return " ".join(lang.fold(text).split())


def _usable(prompt):
    """A prompt whose JEV label says something about the prompt alone (no pasted text, not a go-ahead or a
    question about the session)."""
    own, pasted = core.split_pasted(prompt)
    if pasted or not own.strip() or len(own.replace("[redacted]", "").strip()) < 3:
        return False
    return not (core.is_continuation(prompt) or core.is_meta_question(prompt))


def log_samples(path=None):
    """-> (samples, entries). samples: {key: (text, task, level, jev_task_conf)} deduplicated on the normalised
    prompt, the newest label wins; entries: how many JEV log lines each key has (its weight in real traffic)."""
    path = Path(path) if path else log_file()
    samples, entries = {}, {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return samples, entries
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get("backend") != "jev" or r.get("task") in SKIP_TASKS:
            continue
        task, level, prompt = r.get("task"), r.get("level"), r.get("prompt")
        if task not in CLASSES["task"] or level not in CLASSES["level"] or not isinstance(prompt, str) or not _usable(prompt):
            continue
        try:
            conf = float(r.get("task_conf"))
        except (TypeError, ValueError):
            conf = 1.0
        k = key_of(prompt)
        samples[k] = (prompt, task, level, conf)
        entries[k] = entries.get(k, 0) + 1
    return samples, entries


def base_samples(files=None):
    """The repo's labelled eval prompts (id,prompt,task,difficulty,...): {key: (text, task, level, 1.0)}."""
    out = {}
    for path in (files if files is not None else eval_files()):
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    task, prompt = r.get("task"), r.get("prompt") or ""
                    try:
                        level = int(r.get("difficulty"))
                    except (TypeError, ValueError):
                        continue
                    if task in CLASSES["task"] and level in CLASSES["level"] and prompt.strip():
                        out[key_of(prompt)] = (prompt, task, level, 1.0)
        except (OSError, ValueError, csv.Error):
            continue
    return out


# ---- the model ----------------------------------------------------------------------------------------------

def fit(samples, weights=None, min_df=MIN_DF):
    """samples: [(text, task, level, ...)]; weights: per sample (default 1). -> the trained part of the model:
    per feature the weighted class counts [task classes..., level classes...], plus class totals."""
    weights = weights or [1.0] * len(samples)
    nt = len(CLASSES["task"])
    df, table = {}, {}
    cls_n = {h: [0.0] * len(CLASSES[h]) for h in HEADS}
    for (text, task, level, *_), w in zip(samples, weights):
        ti, li = CLASSES["task"].index(task), CLASSES["level"].index(level)
        cls_n["task"][ti] += w
        cls_n["level"][li] += w
        for f in features(text):
            row = table.get(f)
            if row is None:
                row = table[f] = [0.0] * (nt + len(CLASSES["level"]))
                df[f] = 0
            row[ti] += w
            row[nt + li] += w
            df[f] += 1
    table = {f: [round(x, 2) for x in row] for f, row in table.items() if df[f] >= min_df}
    totals = {"task": [0.0] * nt, "level": [0.0] * len(CLASSES["level"])}
    for row in table.values():
        for i in range(nt):
            totals["task"][i] += row[i]
        for i in range(len(CLASSES["level"])):
            totals["level"][i] += row[nt + i]
    return {"features": table, "class_n": {h: [round(x, 2) for x in cls_n[h]] for h in HEADS},
            "totals": {h: [round(x, 2) for x in totals[h]] for h in HEADS}}


def _posterior(model, feats, head):
    """Smoothed Naive Bayes posterior of one head over the features the model knows -> list of probabilities."""
    n_cls = len(CLASSES[head])
    off = 0 if head == "task" else len(CLASSES["task"])
    table, a = model["features"], model.get("alpha", ALPHA)
    scale = model.get("scale", SCALE)
    vocab = len(table)
    known = [table[f] for f in feats if f in table]
    class_n = model["class_n"][head]
    total_docs = sum(class_n) or 1.0
    scores = []
    for c in range(n_cls):
        ll = sum(math.log(row[off + c] + a) for row in known) - len(known) * math.log(model["totals"][head][c] + a * vocab)
        scores.append(math.log((class_n[c] + 1.0) / (total_docs + n_cls)) + ll / scale)
    top = max(scores)
    exps = [math.exp(s - top) for s in scores]
    z = sum(exps)
    return [e / z for e in exps]


def predict_with(model, text, table=None):
    """The model's answer for one prompt: the likeliest tier (task and level taken as independent), inside it the
    likeliest (task, level). -> {task, task_p, level, level_p, tier, tier_p, known, head_task, head_level}."""
    table = table if table is not None else model.get("table") or claude_table()
    feats = features(text)
    known = sum(f in model["features"] for f in feats)
    pt, pl = _posterior(model, feats, "task"), _posterior(model, feats, "level")
    tier_p, best = {}, {}
    for i, task in enumerate(CLASSES["task"]):
        for j, level in enumerate(CLASSES["level"]):
            tier, p = tier_of(task, level, table), pt[i] * pl[j]
            tier_p[tier] = tier_p.get(tier, 0.0) + p
            if tier not in best or p > best[tier][0]:
                best[tier] = (p, i, j)
    tier = max(tier_p, key=tier_p.get)
    _, i, j = best[tier]
    return {"task": CLASSES["task"][i], "task_p": pt[i], "level": CLASSES["level"][j], "level_p": pl[j],
            "tier": tier, "tier_p": tier_p[tier], "known": known,
            "head_task": CLASSES["task"][max(range(len(pt)), key=pt.__getitem__)],
            "head_level": CLASSES["level"][max(range(len(pl)), key=pl.__getitem__)]}


def confidence(pred, mode):
    """The number the threshold applies to: 'tier' = probability of the winning tier (default), or the min /
    product of the two heads' posteriors. 0 for a prompt the model hardly knows (its answer is then the prior)."""
    if pred["known"] < MIN_KNOWN:
        return 0.0
    if mode == "min":
        return min(pred["task_p"], pred["level_p"])
    if mode == "product":
        return pred["task_p"] * pred["level_p"]
    return pred["tier_p"]


def sample_weight(sample):
    """A log prompt JEV itself was unsure about counts LOWCONF_WEIGHT."""
    return 1.0 if sample[3] >= core.MIN_CONF else LOWCONF_WEIGHT


def true_tier(sample, table, default):
    """The tier JEV's answer routed to: below ROUTER_MIN_CONFIDENCE the router takes the default tier, whatever
    the task label says (decide() marks it 'routing uncertain')."""
    return default if sample[3] < core.MIN_CONF else tier_of(sample[1], sample[2], table)


# ---- evaluation ---------------------------------------------------------------------------------------------

def _fold_of(key, k):
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], 16) % k


def training_set(user, base, held=(), base_weight=BASE_WEIGHT):
    """(samples, weights): the user prompts outside `held` plus the base set."""
    mine = [v for key, v in user.items() if key not in held]
    return mine + list(base.values()), [sample_weight(v) for v in mine] + [base_weight] * len(base)


def cross_validate(user, base, table, k=CV_FOLDS, base_weight=BASE_WEIGHT, **fit_kw):
    """Out-of-fold predictions for every user sample: {key: prediction}. The folds are by normalised prompt hash,
    so a prompt the log holds many times is never in its own training set. The base set is always trained on."""
    pred = {}
    for fold in range(k):
        held = {key for key in user if _fold_of(key, k) == fold}
        samples, weights = training_set(user, base, held, base_weight)
        model = fit(samples, weights, **fit_kw)
        for key in held:
            pred[key] = predict_with(model, user[key][0], table)
    return pred


def evaluate(user, pred, table, default, entries=None, mode="tier"):
    """Per-key outcome rows [(conf, task_ok, level_ok, tier_ok, weight)]; weight = the number of log entries."""
    rows = []
    for key, v in user.items():
        p = pred[key]
        rows.append((0.0 if len(v[0]) >= DECIDE_MAX_CHARS else confidence(p, mode), p["task"] == v[1], p["level"] == v[2],
                     p["tier"] == true_tier(v, table, default), (entries or {}).get(key, 1)))
    return rows


def calibrate(rows, target=TARGET_AGREEMENT, min_cov=MIN_COVERAGE, min_n=MIN_COVERED):
    """The lowest threshold on THRESHOLDS whose held-out tier agreement is >= target with enough coverage:
    (threshold, agreement, coverage) or None. Counted per unique prompt (rows are deduplicated)."""
    total = len(rows)
    for t in THRESHOLDS:
        above = [r for r in rows if r[0] >= t]
        if total and len(above) >= max(min_n, min_cov * total) and sum(r[3] for r in above) / len(above) >= target:
            return t, sum(r[3] for r in above) / len(above), len(above) / total
    return None


def stats_at(rows, t):
    """Agreement / coverage at threshold t, per unique prompt and weighted by log entries (real traffic)."""
    above = [r for r in rows if t is not None and r[0] >= t]
    w_all, w_above = sum(r[4] for r in rows) or 1, sum(r[4] for r in above)
    return {"n_covered": len(above),
            "coverage": round(len(above) / len(rows), 3) if rows else 0.0,
            "tier_agreement": round(sum(r[3] for r in above) / len(above), 3) if above else None,
            "traffic_coverage": round(w_above / w_all, 3),
            "traffic_agreement": round(sum(r[3] * r[4] for r in above) / w_above, 3) if w_above else None}


def curve(rows):
    """[[threshold, coverage, tier agreement, share of real calls covered]] over THRESHOLDS: what a different
    target would buy."""
    out = []
    for t in THRESHOLDS:
        st = stats_at(rows, t)
        if st["n_covered"]:
            out.append([t, st["coverage"], st["tier_agreement"], st["traffic_coverage"]])
    return out


def keyword_baseline(user, table, default, entries=None):
    """The built-in keyword classifier on the same prompts: its task / level / tier agreement with JEV, and what
    its own fast path (local_answers confident enough to skip JEV) would cover and how often it agrees."""
    task_ok = level_ok = tier_ok = 0
    fast = []
    for key, v in user.items():
        a = core.local_answers(core.split_pasted(v[0])[0])
        task, level = a["task"]["choice"], int(a["difficulty"]["score"])
        agree = tier_of(task, level, table) == true_tier(v, table, default)
        task_ok, level_ok, tier_ok = task_ok + (task == v[1]), level_ok + (level == v[2]), tier_ok + agree
        if (a["task"]["confidence"] >= core.LOCAL_CONFIDENT and task != "research" and level <= 1
                and len(v[0]) <= core.LOCAL_FAST_MAX_CHARS):
            fast.append((agree, (entries or {}).get(key, 1)))
    n, w_all = len(user) or 1, sum((entries or {}).get(k, 1) for k in user) or 1
    return {"task_accuracy": round(task_ok / n, 3), "level_accuracy": round(level_ok / n, 3),
            "tier_agreement_all": round(tier_ok / n, 3),
            "fast_coverage": round(len(fast) / n, 3),
            "fast_agreement": round(sum(a for a, _ in fast) / len(fast), 3) if fast else None,
            "fast_traffic_coverage": round(sum(w for _, w in fast) / w_all, 3)}


def log_fingerprint(path):
    """(size, sha1 of the first 4 KB): which log a model was trained on, and whether it was replaced since."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
            size = os.fstat(fh.fileno()).st_size
        return size, hashlib.sha1(head).hexdigest()[:16]
    except OSError:
        return 0, ""


def train(apply=True, log=None, files=None, now=None):
    """Trains on the JEV log + the eval CSVs, cross-validates, calibrates and (with apply) saves.
    -> report dict (see report_lines). Never raises for missing or too little data: the model is then disabled."""
    log = Path(log) if log else log_file()
    user, entries = log_samples(log)
    base = base_samples(files)
    size, sha = log_fingerprint(log)
    table, default = claude_table(), default_tier()
    report = {"trained_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now or time.time())),
              "n_user": len(user), "n_base": len(base), "n_entries": sum(entries.values()),
              "enabled": False, "threshold": None, "reason": "", "applied": False,
              "log": {"size": size, "sha": sha}, "classes": {}, "model_bytes": 0}
    mode = CONF_MODES[0]
    if len(user) < MIN_SAMPLES:
        report["reason"] = f"only {len(user)} distinct JEV-labelled prompts in the log (need {MIN_SAMPLES})"
    else:
        pred = cross_validate(user, base, table)
        best = None  # the confidence measure that calibrates to the highest coverage ('tier' wins ties)
        for m in CONF_MODES:
            cal = calibrate(evaluate(user, pred, table, default, entries, m))
            if cal and (best is None or cal[2] > best[1][2]):
                best = (m, cal)
        mode = best[0] if best else mode
        rows = evaluate(user, pred, table, default, entries, mode)
        if best:
            report.update(enabled=True, threshold=best[1][0])
        else:
            report["reason"] = (f"no threshold reaches {TARGET_AGREEMENT:.0%} held-out tier agreement "
                                f"with >= {MIN_COVERAGE:.0%} of the prompts covered")
        report["cv"] = {
            "task_accuracy": round(sum(pred[k]["head_task"] == v[1] for k, v in user.items()) / len(user), 3),
            "level_accuracy": round(sum(pred[k]["head_level"] == v[2] for k, v in user.items()) / len(user), 3),
            "tier_agreement_all": round(sum(r[3] for r in rows) / len(rows), 3),
            "tier_agreement_default": round(sum(true_tier(v, table, default) == default for v in user.values()) / len(user), 3),
            "at_threshold": stats_at(rows, report["threshold"]), "curve": curve(rows),
            "keyword": keyword_baseline(user, table, default, entries)}
    report["conf_mode"] = mode
    for v in user.values():
        report["classes"].setdefault(v[1], [0, 0, 0])[v[2]] += 1
    if user:
        samples, weights = training_set(user, base)
        model = fit(samples, weights)
        doc = {"version": VERSION, "trained_at": report["trained_at"], "n_user": len(user), "n_base": len(base),
               "enabled": report["enabled"], "threshold": report["threshold"], "conf_mode": mode,
               "alpha": ALPHA, "scale": SCALE, "reason": report["reason"], "table": table, "default": default,
               "log": report["log"], "cv": report.get("cv"), "class_counts": report["classes"], **model}
        text = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
        report["model_bytes"] = len(text.encode("utf-8"))
        report["vocab"] = len(model["features"])
        if apply:
            platforms.atomic_write(model_file(), text, mode=0o600)  # the vocabulary of the user's own prompts
            _cache.clear()
            report["applied"] = True
    return report


def report_lines(rep):
    """Human-readable summary of train()'s report (for `trirouter learn`)."""
    out = [f"Distilled routing model: {rep['n_user']} distinct JEV-labelled prompts from the log "
           f"({rep['n_entries']} calls) + {rep['n_base']} base prompts"]
    cv = rep.get("cv")
    if cv:
        out.append(f"  cross-validation ({CV_FOLDS}-fold, held-out): task {cv['task_accuracy']:.0%}, "
                   f"difficulty {cv['level_accuracy']:.0%}, tier {cv['tier_agreement_all']:.0%} "
                   f"(always the default tier: {cv['tier_agreement_default']:.0%})")
    if cv and cv.get("keyword"):
        k = cv["keyword"]
        out.append(f"  keyword classifier on the same prompts: task {k['task_accuracy']:.0%}, difficulty "
                   f"{k['level_accuracy']:.0%}, tier {k['tier_agreement_all']:.0%}; its fast path covers "
                   f"{k['fast_traffic_coverage']:.0%} of the calls at {k['fast_agreement'] or 0:.0%} agreement")
    if rep["enabled"] and cv:
        s = cv["at_threshold"]
        out.append(f"  threshold {rep['threshold']} ({rep['conf_mode']} confidence): tier agreement "
                   f"{s['tier_agreement']:.0%} on {s['coverage']:.0%} of the prompts "
                   f"({s['traffic_coverage']:.0%} of real calls)")
        out.append(f"  -> skips about {s['traffic_coverage']:.0%} of JEV calls (~{JEV_LATENCY_MS} ms and the fee each)")
    else:
        out.append(f"  disabled: {rep['reason']}")
        best = max((c for c in (cv or {}).get("curve", []) if c[1] >= MIN_COVERAGE), key=lambda c: c[2], default=None)
        if best:
            out.append(f"  (best at >= {MIN_COVERAGE:.0%} coverage: {best[2]:.0%} agreement at threshold {best[0]})")
    if rep.get("model_bytes"):
        out.append(f"  model: {rep.get('vocab', 0)} features, {rep['model_bytes'] / 1024:.0f} KiB"
                   + (" (saved)" if rep["applied"] else " (dry run, nothing written)"))
    return out


# ---- loading and prediction ---------------------------------------------------------------------------------

_cache = {}   # path -> (mtime_ns, model); the hook process is short-lived, the MCP server is not


def load(path=None):
    """The saved model dict, cached by mtime; None when there is none or it cannot be read. Never raises."""
    path = Path(path) if path else model_file()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _cache.pop(str(path), None)
        return None
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        model = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(model, dict) or model.get("version") != VERSION or not isinstance(model.get("features"), dict):
            model = None
    except (OSError, ValueError):
        model = None
    _cache[str(path)] = (mtime, model)
    return model


def disabled_by_env():
    return os.environ.get(ENV_OFF, "").strip().lower() in ("0", "off", "false", "no")


def predict(text):
    """-> predict_with()'s dict plus conf and confident, or None without a model file. `confident` needs an enabled
    model, a confidence at the calibrated threshold and the Claude route table the model was calibrated against."""
    try:
        model = load()
        if not model:
            return None
        p = predict_with(model, text, model.get("table") or {})
        p["conf"] = confidence(p, model.get("conf_mode", "tier"))
        thr = model.get("threshold")
        p["confident"] = bool(model.get("enabled") and thr is not None and p["conf"] >= thr
                              and len(text) < DECIDE_MAX_CHARS
                              and model.get("table") == claude_table())
        return p
    except Exception:  # noqa: BLE001 - a broken model must never break routing
        return None


# ---- background retraining ----------------------------------------------------------------------------------

def count_new_jev(path, saved):
    """JEV entries appended to the log since the model was trained, or None when the log was replaced/rotated."""
    size, sha = log_fingerprint(path)
    start = int(saved.get("size") or 0)
    if size < start or (start and sha != saved.get("sha")):
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            return sum(1 for line in fh if b'"backend": "jev"' in line or b'"backend":"jev"' in line)
    except OSError:
        return 0


def maybe_retrain(now=None, force=False):
    """Daily background job: trains when RETRAIN_MIN_NEW new JEV entries arrived since the saved model (or there is
    none, or the route table changed); at most once a day. -> the report, else None. Never raises."""
    try:
        if disabled_by_env():
            return None
        now = time.time() if now is None else now
        try:
            last = float(json.loads(check_file().read_text(encoding="utf-8")).get("last") or 0)
        except (OSError, ValueError, AttributeError, TypeError):
            last = 0.0
        if not force and 0 <= now - last < CHECK_INTERVAL_S:
            return None
        platforms.atomic_write(check_file(), json.dumps({"last": now}))
        log = log_file()
        if not log.exists():
            return None
        model = load()
        if model and model.get("table") == claude_table():
            new = count_new_jev(log, model.get("log") or {})
            if new is not None and new < RETRAIN_MIN_NEW:
                return None
        return train(apply=True, now=now)
    except Exception:  # noqa: BLE001 - runs inside the discovery background job: must never break it
        return None
