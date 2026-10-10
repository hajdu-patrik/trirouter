#!/usr/bin/env python3
"""Provider-independent router core: (prompt, provider) -> decision -> instruction text.

JEV (TypeSafe) classifies when a token is set; otherwise, or when the call fails, the local keyword
classifier does. Config is read from this package, never from the current project: the hook runs
globally inside arbitrary other projects.
"""
import json
import os
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

from . import catalog, lang, legacy

CFG_DIR = Path(__file__).resolve().parent / "config"
# ~/.trirouter, or ~/.jev-router until `trirouter setup` has migrated it (hooks and the MCP server only read)
STATE_DIR = legacy.state_dir()

API_URL = os.environ.get("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-1.13.0")
OPENROUTER_URL = os.environ.get("JEV_OPENROUTER_URL", "https://openrouter.ai/api/v1/systemone")
OPENROUTER_JEV_MODEL = os.environ.get("JEV_OPENROUTER_MODEL", "jev-1.13")  # OpenRouter takes the bare id
TIMEOUT_S = float(os.environ.get("JEV_TIMEOUT", "4"))
MIN_CONF = float(os.environ.get("ROUTER_MIN_CONFIDENCE", "0.6"))
DESTRUCTIVE_T = float(os.environ.get("ROUTER_DESTRUCTIVE_THRESHOLD", "0.3"))
LONG_CTX_T = float(os.environ.get("ROUTER_LONG_CONTEXT_THRESHOLD", "0.7"))
MAX_STATE_CHARS = 6000  # well under JEV's 32k-token limit: long states degrade its answers
SKILL_CANDIDATES = int(os.environ.get("ROUTER_SKILL_CANDIDATES", "8"))
BACKEND = os.environ.get("ROUTER_BACKEND", "auto").lower()

TASKS = {
    "qa": "A short factual question or a request to explain a concept",
    "general": "A practical everyday task: writing, planning, organizing files or documents",
    "math": "A math problem, calculation, or proof",
    "study": "University coursework: lecture notes, long documents, exam preparation",
    "code": "Writing, fixing, or refactoring program code",
    "test": "Writing, running, or analyzing software tests",
    "research": "Needs current information from the web",
}
DIFFICULTY = [
    "Trivial: an expert answers in seconds",
    "Moderate: needs a few minutes of focused work",
    "Hard: needs deep multi-step reasoning or large changes",
]
EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max", "ultra"]


# A pasted block (a log, a README, CI output) is not the user's own words: it must not decide the
# answer language or the task type.
PASTED_RE = re.compile(r"<pasted_content\b[^>]*>.*?(?:</pasted_content\b[^>]*>|\Z)", re.S)
FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.S)
JEV_PASTED_CHARS = 2000


def split_pasted(prompt):
    """(the user's own text, the pasted blocks). The own text falls back to the whole prompt."""
    pasted = "\n".join(m.group(0) for m in PASTED_RE.finditer(prompt))
    own = " ".join(PASTED_RE.sub(" ", prompt).split())
    return (own or prompt), pasted


def language_of(prompt):
    own, _ = split_pasted(prompt)
    return lang.detect(" ".join(FENCE_RE.sub(" ", own).split()) or own)


# A bare go-ahead or status check continues the previous request: it keeps that request's model
# and effort instead of being classified on its own (where it would look trivial or uncertain).
GO_AHEAD = {
    "mehet", "mehetsz", "mehetunk", "igen", "ja", "jah", "aha", "ok", "oke", "okes", "okay", "okey", "rendben",
    "persze", "jo", "johet", "nyomjad", "nyomd", "nyomj", "csinald", "folytasd", "folytassuk", "folytatas",
    "kovetkezo", "kovi", "hajra", "tovabb", "lehet", "tessek", "pontosan", "szuper", "koszi", "koszonom",
    "yes", "yeah", "yep", "yup", "sure", "go", "continue", "proceed", "lgtm", "next", "retry", "please",
    "thanks", "great", "perfect", "done", "try",
}
GO_AHEAD_FILLER = {
    "am", "akkor", "is", "meg", "csak", "most", "kerlek", "fel", "tovabb", "ezt", "azt", "mind", "mindet", "mindent",
    "igy", "ugy", "nyugodtan", "nyomjad", "mehet", "igen", "ok", "please", "do", "it", "ahead", "on", "again",
    "then", "all", "that", "this", "now", "go", "keep", "going", "yes", "sure", "ujra",
}
STATUS_RE = re.compile(r"^(hogy allunk|hol tartunk|mi a helyzet|kesz vagy|kesz van|elkeszult|megvan|"
                       r"how is it going|how's it going|how are we doing|any progress|are you done|status)\b")
CONTINUATION_MAX_WORDS = 5
# A go-ahead in one language is answered in it ("Mehet" after an English paste); "ok" keeps the previous language.
CONTINUATION_LANG_WORDS = {
    "hu": {"mehet", "mehetsz", "mehetunk", "igen", "rendben", "persze", "jo", "johet", "nyomjad", "nyomd", "nyomj",
           "csinald", "folytasd", "folytassuk", "folytatas", "kovetkezo", "kovi", "hajra", "tovabb", "lehet", "tessek",
           "pontosan", "szuper", "koszi", "koszonom", "kerlek", "akkor", "meg", "csak", "most", "mind", "mindet",
           "mindent", "nyugodtan", "ujra", "hogy", "allunk", "hol", "tartunk", "helyzet", "kesz", "elkeszult", "megvan"},
    "en": {"yes", "yeah", "yep", "yup", "sure", "go", "continue", "proceed", "next", "retry", "please", "thanks",
           "great", "perfect", "done", "try", "do", "ahead", "again", "then", "keep", "going", "how", "progress",
           "status", "are", "you", "doing"},
}


def is_continuation(prompt):
    """A bare go-ahead ("mehet", "yes, do it", "igen torold!") or a status check ("hogy allunk?"):
    at most one word beyond the go-ahead and filler words, so "ok, most irj teszteket" is new work."""
    own, pasted = split_pasted(prompt)
    if pasted:
        return False
    words = re.findall(r"[a-z0-9']+", _norm(own))
    if not words or len(words) > CONTINUATION_MAX_WORDS:
        return False
    if STATUS_RE.match(" ".join(words)):
        return True
    if words[0] not in GO_AHEAD:
        return False
    return len([w for w in words[1:] if w not in GO_AHEAD_FILLER and w not in GO_AHEAD]) <= 1


def continuation_lang(prompt, previous_lang):
    words = re.findall(r"[a-z0-9']+", _norm(split_pasted(prompt)[0]))
    votes = {code: sum(w in vocab for w in words) for code, vocab in CONTINUATION_LANG_WORDS.items()}
    hu, en = votes["hu"], votes["en"]
    return previous_lang if hu == en else ("hu" if hu > en else "en")


def continued_decision(previous, lang_code):
    """The previous decision, re-used for a continuation (safety is always judged on the new prompt)."""
    keep = ("task", "task_conf", "level", "primary", "verify", "skill", "skill_path", "skill_native", "effort",
            "model", "extra_agents")
    d = {k: previous[k] for k in keep if k in previous}
    d.setdefault("primary", "main")
    d.setdefault("task", "general")
    d.setdefault("task_conf", 0.0)
    d.setdefault("level", 1)
    d.update(backend="continuation", lang=lang_code, destructive_p=None, verify=d.get("verify") or "",
             notes=["continues the previous request: keep its model and effort"])
    return d


def _norm(text):
    t = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


# Code-level backup for JEV's destructive question, which prompt injection can sway. Object-bound on
# purpose: a bare "remove"/"order"/"pay" is harmless ("Remove the unused import").
_OBJ = (r"(file|fajl|folder|mappa|director|konyvtar|branch|repo|database|adatbazis|tabla|table|"
        r"record|rekord|user|felhasznalo|account|fiok|profil|email|level|uzenet|message|commit|"
        r"backup|mentes|data|adat|disk|lemez|partition|particio|bucket|cluster|server|szerver|"
        r"container|kontener|volume|project|projekt|everything|mindent|all\b|osszes|program|app\b|"
        r"alkalmazas|package|csomag|photo|foto|kep|document|dokumentum)")
DESTRUCTIVE_RE = re.compile(
    r"(\btorol\w*|\btorl\w*|\bdelete\w*|\berase\w*|\bwipe\w*|\bpurge\w*|\bdestroy\w*"
    r"|\b(remove|eltavolit\w*|tavolits\w*\s+el|uninstall\w*)\b[^.?!\n]{0,40}" + _OBJ +
    r"|\brm\s+-\w*[rf]|\brmdir\b|\bdel\s+/[sq]|drop\s+(table|database|schema)|truncate\s+(table\s+)?\w+"
    r"|git\s+(reset\s+--hard|clean\s+-\w*f|push\s+\S*\s*(-f\b|--force))|force[- ]?push|force-?szal"
    r"|\bfelulir\w*|\bird\s+felul\b|\boverwrite\w*"
    r"|\bforma(t|z)\w*\s+(meg\s+)?(a\s+|az\s+|the\s+)?(\w:\s*)?(lemez|disk|meghajto|drive|partition|particio)"
    r"|\bkuld(d|jed|jon)?\s+el\b|\belkuld\w*|\bkuld\w*\s+([\w-]+\s+){0,3}(uzenet|e-?mail|level|sms)"
    r"|send\s+(an?\s+|the\s+|this\s+|that\s+)?(e-?mail|message|mail|dm|sms|invite|text)"
    r"|\bsend\w*\s[^.?!\n]{0,50}\b(to|by|via)\s+(my\s|him\b|her\b|them\b|the\s+(team|client|customer|boss|group|channel)|\S+@\S+|e-?mail|slack|teams|whatsapp|messenger)"
    r"|\bpublikal\w*|\bkozze\b|\bkozzete\w*|\bpublish\w*|\bposztol\w*|\btweetel\w*|\btweet\s+(it|this)"
    r"|\bpost\w*\s[^.?!\n]{0,30}\b(on|to)\s+(twitter|x\b|linkedin|facebook|instagram|reddit|slack|discord|the\s+blog|my\s+blog)"
    r"|\butal(d|j|jon|ok|junk)\b|\batutal\w*|\bfizes(s|sd|sen)\b|\bfizesd\s+ki|\bvasarol(j|d|jon)\b|\bvegy(el|ed|uk)\s+(meg|egy)"
    r"|\brendeld\s+meg|\bplace\s+(an?\s+)?order|\border\s+[\w\s]{1,30}\b(from|on|online)\b|\bbuy\s+(me\s+)?(an?|the|some|\d+)\b"
    r"|\bpurchase\w*|\bpay\s+(for|the|my|\$|\d)|\btransfer\s+(the\s+)?(\$|\d|money|funds|rent|payment|salary)|\bcheckout\b)"
)


def is_destructive(prompt):
    return bool(DESTRUCTIVE_RE.search(_norm(prompt)))


# Local keyword classifier (Hungarian + English, accent-stripped). Its answers have JEV's shape.
LOCAL_TASK_RE = {
    "test": r"\bteszt|pytest|unit ?test|unittest|\bjest\b|vitest|playwright|cypress|coverage|lefedettseg|\btests?\b|"
            r"\bmock|assert|\bqa\b|\bspec\b|\btesting\b",
    "code": r"\bkod|\bcode\b|refaktor|refactor|\bbugs?\b|fuggveny|\bfunction\b|osztaly|\bclass\b|\bmodul|\bmodule\b|"
            r"\bapi\b|endpoint|python|javascript|typescript|"
            r"react|next\.?js|\bjava\b|c#|\bsql\b|script|exception|\berror\b|stack ?trace|\bgit\b|commit|\bmerge\b|deploy|"
            r"docker|\.py\b|\.js\b|\.ts\b|implementa|debug|compile|backend|frontend|\brepo|\bpush\b|branch|pull request|"
            r"vegpont|fastapi|django|flask|node_modules|fuggoseg|\bdependenc|npm\b|\bpip\b|\bhook|\bconfig|\bcli\b|"
            r"\bbuild\b|\bregex|\bjson\b|\byaml\b|\bhtml\b|\bcss\b|\bbash\b|powershell|\bfix\b|javits|"
            r"pushol|architekt|microservice|mikroszolgaltatas|valida|konfigurac|felulir|\boverwrite\b",
    "math": r"\bmatek|matematik|\bmath\b|oldd meg|\bsolve\b|egyenlet|\bequation\b|bizonyits|\bproof|\bprove\b|integral|deriv|"
            r"matrix|sajatertek|eigenvalue|valoszinuseg|probability|szamold ki|\bcalculate\b|hatarertek|\blimit\b|"
            r"\bprim\b|primszam|\bprime\b|lemma|negyzete|gyoke|square root|szazalek|percent|"
            r"\bszoras|variancia|\bvariance\b|standard deviation|\bsquared\b|\bcubed\b|"
            r"(?<![a-z0-9])\d+(\.\d+)?\s*[-+*/^]\s*\d+(\.\d+)?(?![a-z0-9])|(?<![a-z0-9])\d+\s?[a-z]\s*[-+*/=]\s*\d",
    "study": r"egyetemi|jegyzet|eloadas|vizsga|\bzh\b|kollokvium|tantargy|szakdolgozat|diplomamunka|\btetel|egyetem|felev|"
             r"kurzus|foglald ossze|osszefoglal|konspektus|flashcard|"
             r"\buniversity\b|\blecture\b|\bexam\b|midterm|\bcourse\b|\bthesis\b|\bsemester\b|\bsummari[sz]e\b|study notes",
    "research": r"legfrissebb|legujabb|aktualis|\bma\b|\bmai\b|jelenleg|hirek|\bnews\b|latest|\bcurrently?\b|arfolyam|"
                r"mennyibe kerul|holnap|\btomorrow\b|\btoday\b|idojaras|\bweather\b|hany fok|\bara\b|\bprice\b|exchange rate|"
                r"ki (a|az) (jelenlegi )?\w+ (elnoke|vezerigazgatoja|miniszterelnoke)|who is the current \w+|"
                r"\bthis (week|month|year)\b|\bezen a heten\b|\bidei\b|\bcost\b|how much (does|is|do|can)|\bright now\b|"
                r"milyen ido lesz",
    "qa": r"^(mi|mik|ki|kik|mikor|hol|miert|hogyan|hany|melyik|mennyi|what|who|when|where|why|how|is|are|does|do)\b|"
          r"magyarazd|mit jelent|mi az a|\bexplain|what (does|is)|what's the difference|kulonbseg|difference between",
    "general": r"\birj\b|keszits|tervezd|szervezd|rendezd|\blista|e-?mail|\blevel|mappa|fajl|jegyzokonyv|"
               r"\bwrite\b|\bcreate\b|\bplan\b|\borganize\b|\blist\b|\bfolder\b|\bfile\b|\bdocument\b|\bletter\b|"
               r"\bnote\b|\bemail\b|\bdraft\b|\btranslate\b|forditsd|\bprezentac|\bpresentation\b|\btablazat|spreadsheet|"
               r"kuldj|uzenet|\bsend\b|\bmessage\b|\bslack\b|csatorna",
}
# Extra weight: "what is a/an X" (indefinite article) is a defining question and outranks one
# incidental tech keyword. "what is THE latest ..." is research, so the pattern stays this narrow.
LOCAL_TASK_STRONG_RE = {
    "qa": r"mi az a|what is (a|an)\b",
}
# Tie-break order, tuned against the confusions in eval/results_*.csv.
LOCAL_PRIORITY = ["test", "math", "study", "research", "code", "qa", "general"]
LOCAL_HARD_RE = (r"(egesz|teljes|osszes) (kodbazis|repo|projekt|rendszer|alkalmazas|architektur|modul)|architektur|"
                 r"\bnehez|bonyolult|reszletes|mikroszolgaltatas|migral|optimaliz|hexagonal|\d{2,}\s*oldal|"
                 r"tobb (fajl|modul)|bizonyits|\bentire\b|\bwhole\b|\barchitecture\b|\bcomplex\b|\bdetailed\b|"
                 r"microservice|\bmigrate\b|\bmigration\b|optimi[sz]e|\d{2,}\s*pages?|multiple (files|modules)|"
                 r"\bprove\b|\bproof\b|from scratch|nulladrol|semmibol|end-to-end|\be2e\b|security audit|biztonsagi audit")
LOCAL_LONG_RE = (r"\d{2,}\s*oldal|(egesz|teljes|osszes) (kodbazis|repo|projekt|konyv|fajl)|"
                 r"\d{2,}\s*pages?|(entire|whole|full) (codebase|repo|project|book|file)")


# Extra parallel agents multiply token usage, so 0 is the answer for almost every request.
MAX_EXTRA_AGENTS = int(os.environ.get("ROUTER_MAX_EXTRA_AGENTS", "4"))
AGENTS_MIN_CONF = 0.7  # independent of ROUTER_MIN_CONFIDENCE; below it, one agent fewer
AGENT_CRITERIA = {
    "0": "DEFAULT - choose this for the vast majority of requests. One agent does the whole job: questions, "
         "explanations, a bug fix, a feature in one area, a test file, a document, a refactor of one module.",
    "1": "+1 extra agent: the task has two clearly independent, substantial parts that gain real time in parallel "
         "(e.g. implement a feature AND independently write its test suite, or research AND implement).",
    "2": "+2 extra agents: a genuinely complex task with three independent substantial workstreams "
         "(e.g. backend change + frontend change + database migration for one feature).",
    "3": "+3 extra agents: building a complete new page/product feature FROM SCRATCH with backend + frontend + "
         "data layer/tests.",
    "4": "+4 extra agents: very rare - only an exceptionally large from-scratch build that ALSO requires writing "
         "extra tooling first (e.g. a scraper/crawler or custom tools) on top of backend + frontend.",
}
_STREAMS = {
    "backend": r"\bbackend|\bapi\b|endpoint|vegpont|\bserver\b|szerver|\bservice\b|szolgaltatas",
    "frontend": r"\bfrontend|\bui\b|felulet|\bpage\b|\boldal\b|oldalt|\breact\b|\bvue\b|\bcomponent|komponens",
    "data": r"adatbazis|\bdatabase\b|\bdb\b|migrac|migration|\bschema\b|\bsema\b",
    "tests": r"\btest|\bteszt",
    "tooling": r"scraper|scrape|crawler|\btool(s|ing)?\b|eszkoz|\bparser\b",
}
_FROM_SCRATCH = r"from scratch|semmibol|nulladrol|\bnew (page|site|app|feature)\b|uj (oldal|oldalt|alkalmazas|appot)|teljes (oldal|oldalt)"


def local_agents_answer(t, level):
    if level < 2:
        return {"choice": "0", "confidence": 0.9}
    streams = {k for k, p in _STREAMS.items() if re.search(p, t)}
    extra = 0
    if len(streams) >= 2:
        extra = 1
    if len(streams) >= 3:
        extra = 2
    if re.search(_FROM_SCRATCH, t) and {"backend", "frontend"} <= streams:
        extra = 3
        if "tooling" in streams:
            extra = 4
    return {"choice": str(extra), "confidence": 0.75}


def local_answers(prompt):
    t = _norm(prompt)
    scores = {k: len(re.findall(p, t)) for k, p in LOCAL_TASK_RE.items()}
    for k, p in LOCAL_TASK_STRONG_RE.items():
        scores[k] += len(re.findall(p, t))
    ranked = sorted(LOCAL_PRIORITY, key=lambda k: (-scores[k], LOCAL_PRIORITY.index(k)))
    top, second = ranked[0], ranked[1]
    if scores[top] == 0:
        task, conf = "general", 0.3
    else:
        task, conf = top, min(0.9, 0.5 + 0.15 * (scores[top] - scores[second]))
    if re.search(LOCAL_HARD_RE, t):
        level = 2
    elif len(t) < 60 and task in ("qa", "general", "research", "math") and not re.search(r"bizonyits|proof|prove", t):
        level = 0
    else:
        level = 1
    return {
        "task": {"choice": task, "confidence": conf},
        "difficulty": {"score": level, "confidence": 0.7},
        "long_context": {"noul": 0.8 if re.search(LOCAL_LONG_RE, t) else 0.1},
        "needs_web": {"noul": 0.8 if task == "research" else 0.1},
        "destructive": {"noul": 0.9 if is_destructive(prompt) else 0.05},
        "agents": local_agents_answer(t, level),
    }


def local_skill_answer(candidates):
    """The pre-filter's best candidate, only when it clearly wins."""
    if not candidates:
        return None
    best_score, best = candidates[0]
    second = candidates[1][0] if len(candidates) > 1 else 0.0
    strong = best.get("_name_hit")  # the skill's own name must match - never commit on stray description words
    if strong and best_score >= 6.0 and best_score >= 1.3 * second:
        return best["name"], min(0.9, 0.5 + best_score / 30)
    return None


def load_json(name, default):
    try:
        return json.loads((CFG_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def user_config():
    try:
        return json.loads((STATE_DIR / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def jev_endpoint():
    """(channel, url, key, model), or None. A TypeSafe token wins over an OpenRouter key. The generic
    OPENROUTER_API_KEY is deliberately not read: other tools set it, and routing would then spend
    that account's credit without the user ever opting in."""
    cfg = user_config()
    key = os.environ.get("TYPESAFE_API_KEY") or cfg.get("typesafe_api_key")
    if key:
        return "typesafe", API_URL, key, JEV_MODEL
    key = os.environ.get("JEV_OPENROUTER_API_KEY") or cfg.get("openrouter_api_key")
    if key:
        return "openrouter", OPENROUTER_URL, key, OPENROUTER_JEV_MODEL
    return None


def jev_key():
    endpoint = jev_endpoint()
    return endpoint[2] if endpoint else None


def model_overrides():
    """Per-account model availability written by `models --probe`; models.json is only the catalog."""
    try:
        return json.loads((STATE_DIR / "models.local.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def base_provider(provider):
    """'claude-chat' (Claude desktop Chat/Cowork via MCP) shares Claude's routes/models/skills."""
    return provider.split("-")[0]


def safe_key(name):
    return "skill_" + re.sub(r"[^a-z0-9_]", "_", name.lower())


def excluded_efforts():
    """Levels no provider may ever get (policy.excluded_efforts, default 'ultra')."""
    return set(load_json("models.json", {}).get("policy", {}).get("excluded_efforts", ["ultra"]))


# Claude Code aliases that name a mode or a setting, not a model family
CLAUDE_MODE_ALIASES = frozenset({"default", "best", "inherit", "opusplan"})
_GENERIC_ALIAS = re.compile(r"[a-z]+")


def claude_alias_allowed(alias, cfg=None):
    """The Claude policy: only a generic family alias (one plain word such as `opus`), never Haiku, never a
    pinned or dated model ID (`claude-opus-4-1-20250805`), never a mode alias (`opusplan`, `default`)."""
    claude = (cfg if cfg is not None else load_json("models.json", {})).get("claude", {})
    a = str(alias or "").strip().lower()
    if any(x in a for x in claude.get("excluded_families", ["haiku"])):
        return False
    return bool(_GENERIC_ALIAS.fullmatch(a)) and a not in CLAUDE_MODE_ALIASES


def _local_model(provider, model_id, entry, cfg):
    """A model only models.local.json knows (found by the daily discovery), or None when it is not usable."""
    if not isinstance(entry, dict) or not isinstance(entry.get("levels"), list):
        return None
    if provider == "claude" and not claude_alias_allowed(model_id, cfg):
        return None
    mdef = {k: v for k, v in entry.items() if k in ("role", "levels", "slug", "description", "agent_tier")}
    mdef.setdefault("description", model_id)
    if provider == "antigravity":
        mdef.setdefault("slug", "{id}-{effort}" if mdef["levels"] else "{id}")
    return dict(mdef, id=model_id, selectable=bool(entry.get("selectable")))


def models_for(provider):
    """Selectable models of the provider, with the excluded effort levels already stripped: the catalog's, with
    the per-account availability of models.local.json, plus the models only models.local.json knows. A model
    marked `"routable": false` in the catalog is never selectable, whatever a probe recorded."""
    cfg = load_json("models.json", {})
    base = base_provider(provider)
    m = cfg.get(provider) or cfg.get(base) or {}
    local = model_overrides().get(base, {})
    local = local if isinstance(local, dict) else {}
    banned = excluded_efforts()
    out = {}
    for model in m.get("models", []):
        override = local.get(model["id"])
        selectable = (override or {}).get("selectable", model.get("selectable")) if isinstance(override, dict) \
            else model.get("selectable")
        if selectable and model.get("routable", True):
            out[model["id"]] = dict(model, levels=[l for l in model.get("levels", []) if l not in banned])
    known = {model["id"] for model in m.get("models", [])}
    for model_id, entry in local.items():
        mdef = None if model_id in known else _local_model(base, model_id, entry, cfg)
        if mdef and mdef["selectable"]:
            out[model_id] = dict(mdef, levels=[l for l in mdef["levels"] if l not in banned])
    return out


def effort_levels_for(provider):
    models = models_for(provider)
    levels = sorted({l for m in models.values() for l in m["levels"]},
                    key=lambda l: EFFORT_ORDER.index(l) if l in EFFORT_ORDER else 99)
    if not levels:
        return None
    return {"levels": levels, "excluded": sorted(excluded_efforts()),
            "note": "The chosen level is clamped to what the chosen model supports."}


def clamp_effort(effort, allowed):
    """Nearest allowed level to `effort`; ties go to the higher one."""
    if not allowed:
        return effort
    if not effort or effort in allowed:
        return effort or allowed[len(allowed) // 2]
    pos = EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else 2
    return min(allowed, key=lambda a: (abs((EFFORT_ORDER.index(a) if a in EFFORT_ORDER else 2) - pos),
                                       -(EFFORT_ORDER.index(a) if a in EFFORT_ORDER else 2)))


def build_questions(skills, effort=None, models=None):
    """skills: only the pre-filtered candidates, never the whole catalog."""
    q = {
        "task": {"type": "choice", "instructions": "What kind of request is this? The text may be in Hungarian or English.",
                 "criteria": TASKS},
        "difficulty": {"type": "score", "instructions": "How hard is this request for a skilled expert?",
                       "criteria": DIFFICULTY},
        "long_context": {"type": "noul",
                         "instructions": "The request involves reading a long document, many files, or a whole codebase"},
        "needs_web": {"type": "noul", "instructions": "Answering requires up-to-date information from the internet"},
        "destructive": {"type": "noul",
                        "instructions": "The request asks to delete data, send a message to someone, publish "
                                        "something, or spend money. Writing a draft for the user (a letter, an "
                                        "email), planning, or advising what to buy does not count"},
        "agents": {"type": "choice",
                   "instructions": "How many EXTRA agents should work in parallel next to the main one? Be very strict: "
                                   "every extra agent multiplies token usage. When in doubt, choose the lower number.",
                   "criteria": {k: v for k, v in AGENT_CRITERIA.items() if int(k) <= MAX_EXTRA_AGENTS}},
    }
    if skills:
        criteria = dict(skills)
        criteria["none"] = "No listed skill is clearly needed for this request"
        q["skill"] = {"type": "choice", "instructions": "Which skill best fits this request?", "criteria": criteria}
        for name, desc in skills.items():
            q[safe_key(name)] = {"type": "noul", "instructions": f"The request needs this capability: {desc}"}
    if effort and effort.get("levels"):
        usable = [l for l in effort["levels"] if l not in set(effort.get("excluded", []))]
        instructions = "Which reasoning-effort level should the model use for this request?"
        if effort.get("note"):
            instructions += " " + effort["note"]
        q["effort"] = {"type": "choice", "instructions": instructions,
                       "criteria": {l: f"Use the '{l}' reasoning-effort level for this request." for l in usable}}
    if models and len(models) > 1:
        q["model"] = {"type": "choice",
                      "instructions": "Which model of this provider fits this request best (capability vs. cost and speed)?",
                      "criteria": {mid: m.get("description", mid) + (f" (effort levels: {', '.join(m['levels'])})" if m["levels"] else "")
                                   for mid, m in models.items()}}
    return q


def ask_jev(prompt, questions):
    endpoint = jev_endpoint()
    if not endpoint:
        raise RuntimeError("no JEV access (a TypeSafe token or an OpenRouter key: trirouter setup --jev-token=...)")
    _, url, key, model = endpoint
    body = json.dumps({"state": prompt[:MAX_STATE_CHARS], "model": model, "questions": questions}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def use_jev(backend=None):
    if backend:
        return backend == "jev"
    return BACKEND == "jev" or (BACKEND == "auto" and bool(jev_key()))


def classify(prompt, skills=None, backend=None, effort=None, models=None, pasted=""):
    """pasted: blocks the user pasted; JEV sees them as context, the local classifier ignores them."""
    candidates = skills or []
    if use_jev(backend):
        state = prompt + (f"\n\n[Pasted by the user, not their own words]\n{pasted}" if pasted else "")
        answers = _jev_answers(state, candidates, effort, models)
    else:
        answers = _mock_answers(prompt, candidates, effort)
    return _enforce_policy(answers, effort, models)


def _jev_answers(prompt, candidates, effort, models):
    result = ask_jev(prompt, build_questions({s["name"]: s["description"][:300] for _, s in candidates}, effort, models))
    answers = result["answers"]
    answers["_backend"] = "jev"
    answers["_jev_model"] = result.get("model")
    return answers


def _mock_answers(prompt, candidates, effort):
    answers = local_answers(prompt)
    answers["_backend"] = "local"
    picked = local_skill_answer(candidates)
    if picked:
        answers["skill"] = {"choice": picked[0], "confidence": picked[1]}
        answers[safe_key(picked[0])] = {"noul": 0.8}
    choice = _mock_effort(float(answers["difficulty"]["score"]), effort)
    if choice:
        answers["effort"] = {"choice": choice, "confidence": 0.5}
    return answers


def _mock_effort(lvl, effort):
    """The mock never picks the very top level on its own; that is left to JEV or an override."""
    if not effort or not effort.get("levels"):
        return None
    usable = [l for l in effort["levels"] if l not in set(effort.get("excluded", []))]
    if not usable:
        return None
    idx = round(lvl / 2 * (len(usable) - 1) * 0.8) if len(usable) > 1 else 0
    return usable[max(0, min(idx, len(usable) - 1))]


def _enforce_policy(answers, effort, models):
    """Hard policy in code, never trusting the model-based answer alone."""
    banned = excluded_efforts()
    if answers.get("effort", {}).get("choice") in banned:
        usable = [l for l in (effort or {}).get("levels", EFFORT_ORDER) if l not in banned]
        answers["effort"] = {"choice": usable[-1] if usable else "max", "confidence": answers["effort"].get("confidence", 0.5)}
    if "model" in answers and (not models or answers["model"].get("choice") not in models):
        answers.pop("model")
    return answers


def is_cloud():
    return os.environ.get("ROUTER_MODE", "").lower() == "cloud" or os.environ.get("CLAUDE_CODE_REMOTE", "").lower() == "true"


def apply_cloud(primary, verify, routes):
    """No Codex/Antigravity login and no user-level agents in a cloud sandbox."""
    if is_cloud():
        return routes.get("cloud_replace", {}).get(primary, primary), ""
    return primary, verify


def decide(answers, routes, skills_by_name=None):
    """'primary' is a tier name; render() turns it into a concrete agent/model/text."""
    task = answers["task"]
    diff = answers["difficulty"]
    level = max(0, min(int(round(float(diff.get("score", 1)))), len(DIFFICULTY) - 1))
    if float(diff.get("confidence", 0)) < MIN_CONF:
        level = max(level, 1)

    notes = []
    if float(task.get("confidence", 0)) < MIN_CONF:
        target = routes.get("default", "main")
        notes.append("routing uncertain")
    else:
        target = routes.get("table", {}).get(task["choice"], ["main"] * 3)[level]

    if float(answers["long_context"]["noul"]) >= LONG_CTX_T and task["choice"] in ("study", "qa", "general", "research"):
        target = routes.get("long_context_target", "deep")
    if float(answers["needs_web"]["noul"]) >= LONG_CTX_T:
        notes.append("needs current information: use web search")

    primary, _, verify = target.partition("+verify:")
    primary, verify = apply_cloud(primary, verify, routes)

    skill = None
    s = answers.get("skill")
    if s and s.get("choice") not in (None, "none") and float(s.get("confidence", 0)) >= MIN_CONF:
        if float(answers.get(safe_key(s["choice"]), {}).get("noul", 0)) >= 0.5:
            skill = s["choice"]

    result = {
        "task": task["choice"], "task_conf": round(float(task.get("confidence", 0)), 2), "level": level,
        "primary": primary, "verify": verify, "skill": skill,
        "destructive_p": round(float(answers["destructive"]["noul"]), 2), "notes": notes,
    }
    if skill and skills_by_name and skill in skills_by_name:
        result["skill_path"] = skills_by_name[skill]["path"]
        result["skill_native"] = skills_by_name[skill]["native_in"]
    if "effort" in answers:
        result["effort"] = answers["effort"].get("choice")
    m = answers.get("model")
    if m and float(m.get("confidence", 0)) >= MIN_CONF:
        result["model"] = m["choice"]
    result["extra_agents"] = extra_agents(answers.get("agents"), level)
    return result


def extra_agents(ans, level):
    """Never more than 1 extra agent below the 'hard' level."""
    if not ans:
        return 0
    try:
        n = int(str(ans.get("choice", "0")).lstrip("+"))
    except ValueError:
        return 0
    if float(ans.get("confidence", 0)) < AGENTS_MIN_CONF:
        n -= 1
    if level < 2:
        n = min(n, 1)
    return max(0, min(n, MAX_EXTRA_AGENTS))


def resolve_tier(d, targets, models=None, session_model=None):
    """(text, effort, model, agent). A plain-text tier answers in-session (model and agent None).
    Placeholders in agent/text: {model}, {model_} (TOML-safe), {effort}, {slug}, {agent}."""
    spec = _tier_spec(d, targets)
    if isinstance(spec, str):
        return spec, d.get("effort"), None, None
    model = spec.get("model", "")
    mdef = (models or {}).get(model)
    effort = _tier_effort(d, spec, mdef)
    slug = ((mdef or {}).get("slug") or spec.get("slug") or "{id}").format(id=model, effort=effort or "")
    fields = {"model": model, "model_": model.replace(".", "_"), "effort": effort or "default", "slug": slug}
    fields["agent"] = spec.get("agent", "").format(**fields)
    same_model = session_model and session_model == model and spec.get("same_model_text")
    template = spec["same_model_text"] if same_model else spec.get("text", "")
    agent = fields["agent"] if fields["agent"] and "{agent}" in template else None
    return template.format(**fields), effort, model, agent


def _tier_spec(d, targets):
    tiers = targets.get("tiers", {})
    chosen = d.get("model")
    if chosen and targets.get("model_pick") and d["primary"] in targets.get("model_pick_tiers", []):
        return dict(targets["model_pick"], model=chosen)
    return tiers.get(d["primary"]) or tiers.get("main") or "Answer directly in this session."


def _tier_effort(d, spec, mdef):
    """JEV's own model pick is not bound to the tier's effort range, only to the model's levels."""
    effort = d.get("effort")
    if spec.get("efforts") and not (d.get("model") and d.get("backend") == "jev"):
        effort = clamp_effort(effort, spec["efforts"])
    if mdef is not None:
        effort = clamp_effort(effort, mdef["levels"]) if mdef["levels"] else None
    return effort


def render(d, destructive_hit, targets, provider="claude", lang_code="hu", models=None, session_model=None):
    text, effort, model, agent = resolve_tier(d, targets, models, session_model)
    d["effort"] = effort
    if model:
        d["target_model"] = model
    if agent:
        d["target_agent"] = agent
    parts = [f"[router] backend={d.get('backend', 'override')} task={d['task']} difficulty={d['level']} "
             f"conf={d['task_conf']} lang={lang_code}.", text]
    if effort and effort not in text:
        parts.append(f"Reasoning effort: {effort}.")
    n = d.get("extra_agents", 0)
    if n:
        parts.append(f"Parallelism: up to {n} extra agent(s) may run in parallel (same model and effort as above), "
                     f"only for genuinely independent parts; split the work, then merge and verify the results.")
    elif d.get("task") != "override":
        parts.append("Parallelism: none - no extra parallel agents.")
    if d.get("verify") in targets.get("verify", {}):
        parts.append(targets["verify"][d["verify"]])
    if d.get("skill"):
        if base_provider(provider) in d.get("skill_native", [base_provider(provider)]):
            parts.append(f"Relevant skill: `{d['skill']}` - use it.")
        else:
            parts.append(f"Relevant skill: `{d['skill']}` (from another tool) - read and follow "
                         f"{d.get('skill_path', '')}/SKILL.md before starting.")
    if destructive_hit:
        parts.append("SAFETY: this may be irreversible. List the exact actions and ask for explicit confirmation before executing any of them.")
    if d.get("notes"):
        parts.append("Note: " + ", ".join(d["notes"]) + ".")
    parts.append(lang.respond_line(lang_code))
    return " ".join(p for p in parts if p)


# Only the start: a real path can contain spaces, so the rest is grown word by word.
FOREIGN_PATH_START_RE = re.compile(r'[A-Za-z]:[\\/]|(?<![:\w])/(?=\S)')
# Bounds that keep a pathological prompt from slowing the hook down.
FOREIGN_PATH_MAX_CHARS = 200
FOREIGN_PATH_MAX_WORDS = 12
FOREIGN_PATH_MAX_STARTS = 20


def _project_root(path):
    """Nearest ancestor (itself included) with a CLAUDE.md, AGENTS.md or .claude/, or None."""
    node = path if path.is_dir() else path.parent
    for _ in range(50):
        if node.exists() and ((node / "CLAUDE.md").is_file() or (node / "AGENTS.md").is_file()
                              or (node / ".claude").is_dir()):
            return node
        parent = node.parent
        if parent == node:
            return None
        node = parent
    return None


def _deepest_existing_ancestor(path):
    for node in (path, *path.parents):
        if node.exists():
            return node
    return None


def _longest_existing_prefix(text):
    """Longest existing path at the start of `text`. Growing word by word handles both trailing
    prose and path components that contain spaces."""
    words = text[:FOREIGN_PATH_MAX_CHARS].split()
    best_len, best = -1, None
    candidate = ""
    for word in words[:FOREIGN_PATH_MAX_WORDS]:
        candidate = f"{candidate} {word}".strip() if candidate else word
        trimmed = candidate.rstrip(".,;:'\")]}")
        path = Path(trimmed)
        if not path.anchor:
            # Not rooted on this OS ("C:\..." on POSIX): its only existing ancestor is the cwd.
            break
        try:
            ancestor = _deepest_existing_ancestor(path)
        except OSError:
            break
        if ancestor is not None and len(str(ancestor)) > best_len:
            best_len, best = len(str(ancestor)), str(ancestor)
    return best


def foreign_project_note(prompt, cwd):
    """A heads-up when `prompt` names a path in a different project than `cwd`: custom subagent types
    are scoped to the session's own root, so that project's agents cannot be resolved from here."""
    try:
        if not cwd:
            return None
        cwd_path = Path(cwd).resolve()
        here = _project_root(cwd_path) or cwd_path
        starts = FOREIGN_PATH_START_RE.finditer(prompt)
        for count, match in enumerate(starts):
            if count >= FOREIGN_PATH_MAX_STARTS:
                break
            existing = _longest_existing_prefix(prompt[match.start():])
            if not existing:
                continue
            try:
                resolved = Path(existing).resolve()
            except OSError:
                continue
            project = _project_root(resolved)
            if not project or project == here or here.is_relative_to(project) or project.is_relative_to(here):
                continue
            return (f"this prompt names {project}, a different project with its own CLAUDE.md/AGENTS.md/.claude config - "
                    f"Workflow and Agent tool custom subagent types are scoped to this session's own root "
                    f"({here}), not to that path")
    except Exception:  # noqa: BLE001 - must never affect routing
        pass
    return None


def route(prompt, provider, backend=None, session_model=None, previous=None):
    """-> (decision, rendered_text, destructive_hit, error). session_model: the model the calling
    session already runs, so a tier can say "stay in-session". previous: this session's last
    decision, re-used when the prompt only continues it."""
    routes_all, targets_all = load_json("routes.json", {}), load_json("targets.json", {})
    routes = routes_all.get(provider) or routes_all.get(base_provider(provider)) or {"default": "main", "table": {}}
    targets = targets_all.get(provider) or targets_all.get(base_provider(provider)) or {"tiers": {"main": "Answer directly in this session."}}
    effort = effort_levels_for(provider)
    models = models_for(provider)
    own, pasted = split_pasted(prompt)
    lang_code = language_of(prompt)
    regex_hit = is_destructive(prompt)  # the whole prompt: a pasted command can be the dangerous part

    forced = override_for(prompt, targets)
    if forced:
        primary, _ = apply_cloud(forced, "", routes)
        d = {"task": "override", "task_conf": 1.0, "level": 1, "primary": primary, "verify": "", "skill": None,
             "destructive_p": None, "notes": ["manual override"], "backend": "override", "lang": lang_code}
        return d, render(d, regex_hit, targets, provider, lang_code, models, session_model), regex_hit, None

    if previous and is_continuation(prompt):
        d = continued_decision(previous, continuation_lang(prompt, previous.get("lang") or lang_code))
        return d, render(d, regex_hit, targets, provider, d["lang"], models, session_model), regex_hit, None

    candidates = catalog.prefilter(own, catalog.load_catalog(), SKILL_CANDIDATES)
    by_name = {s["name"]: s for _, s in candidates}
    error = None
    try:
        answers = classify(own, candidates, backend=backend, effort=effort, models=models,
                           pasted=pasted[:JEV_PASTED_CHARS])
    except Exception as exc:  # any JEV failure falls back to the local classifier
        error = f"{type(exc).__name__}: {exc}"[:200]
        answers = classify(own, candidates, backend="local", effort=effort, models=models)
    d = decide(answers, routes, by_name)
    d["backend"] = answers.get("_backend", "local")
    d["lang"] = lang_code
    d["skill_candidates"] = [s["name"] for _, s in candidates[:5]]
    if answers.get("_jev_model"):
        d["jev_model"] = answers["_jev_model"]
    hit = regex_hit or (d["destructive_p"] is not None and d["destructive_p"] >= DESTRUCTIVE_T)
    return d, render(d, hit, targets, provider, lang_code, models, session_model), hit, error


def override_for(prompt, targets):
    """Whole-tag match, so #fast never fires on #faster."""
    low = prompt.lower()
    for tag, tier in targets.get("overrides", {}).items():
        if re.search(r"(?<![\w#])" + re.escape(tag.lower()) + r"(?![\w-])", low):
            return tier
    return None
