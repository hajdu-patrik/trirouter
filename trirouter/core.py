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
import threading
import time
import unicodedata
from pathlib import Path

from . import catalog, lang, legacy, secret

CFG_DIR = Path(__file__).resolve().parent / "config"
# ~/.trirouter, or ~/.jev-router until `trirouter setup` has migrated it (hooks and the MCP server only read)
STATE_DIR = legacy.state_dir()

API_URL = os.environ.get("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-1.13.0")
OPENROUTER_URL = os.environ.get("JEV_OPENROUTER_URL", "https://openrouter.ai/api/v1/systemone")
OPENROUTER_JEV_MODEL = os.environ.get("JEV_OPENROUTER_MODEL", "jev-1.13")  # OpenRouter takes the bare id
TIMEOUT_S = float(os.environ.get("JEV_TIMEOUT", "1.8"))  # the hook runs before every prompt: fail fast to the local classifier
MIN_CONF = float(os.environ.get("ROUTER_MIN_CONFIDENCE", "0.6"))
DESTRUCTIVE_T = float(os.environ.get("ROUTER_DESTRUCTIVE_THRESHOLD", "0.3"))
LONG_CTX_T = float(os.environ.get("ROUTER_LONG_CONTEXT_THRESHOLD", "0.7"))
MAX_STATE_CHARS = 6000  # well under JEV's 32k-token limit: long states degrade its answers
SKILL_CANDIDATES = int(os.environ.get("ROUTER_SKILL_CANDIDATES", "5"))
SKILL_DESC_CHARS = 120  # per skill description sent to the classifier: request size is latency
CACHE_MAX = 128  # decision cache entries (classifier answers by prompt hash)
CACHE_TTL_S = 24 * 3600
LOCAL_CONFIDENT = 0.9  # local task confidence at which the network call is skipped (replayed on real traffic: 0.8 agreed with JEV on 3 of 4, 0.9 on 2 of 2)
LOCAL_FAST_MAX_CHARS = 400  # longer prompts are not decided on keywords alone
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
    "Trivial: a plain fact or definition, one fully specified action (send this email, pay a bill, delete a named "
    "folder), or a very short text (a note, a list, a one-paragraph translation) that needs no planning. "
    "E.g. 'Who painted the Mona Lisa?', 'Remove the cache folder', 'Write a short get-well note', 'Order a pizza'",
    "Moderate: needs reasoning, planning or a real work product - explaining how or why, any code, script or "
    "edited file, a multi-step task (find, run or check something, then act), a multi-part document, plan or "
    "spreadsheet, a proof or multi-step calculation. "
    "E.g. 'Fix the bug in utils.py', 'Explain how DNS resolution works', 'Draft a one-page project plan', "
    "'Run the linter and fix what it reports', 'Find the roots of this cubic polynomial'",
    "Hard: a very large input (50+ pages) or scope (a whole module, backend or system), architecture design, "
    "open-ended performance work, a thesis chapter, or a research-level problem. "
    "E.g. 'Rewrite the whole frontend from Vue to React', 'Review the entire codebase for security issues', "
    "'Design the data model and API of a ride-sharing platform'",
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
    "mehet", "mehetsz", "mehetunk", "mehetnek", "mehetne", "mehetnenek", "menjen", "menjenek", "indulhat", "indulj",
    "igen", "ja", "jah", "aha", "ok", "oke", "okes", "okay", "okey", "rendben", "persze", "jo", "johet", "nyomjad", "nyomd",
    "nyomj", "csinald", "csinaljuk", "folytasd", "folytassuk", "folytatas", "kovetkezo", "kovi", "hajra", "tovabb", "lehet",
    "tessek", "pontosan", "szuper", "koszi", "koszonom", "jovahagyom", "elfogadom", "kezdhetjuk", "kezdd",
    "yes", "yeah", "yep", "yup", "sure", "go", "continue", "proceed", "lgtm", "next", "retry", "please", "thanks", "great",
    "perfect", "done", "try", "approved", "approve", "confirmed", "confirm", "accept", "accepted", "agreed", "y", "ya",
    "yea", "fine", "good", "alright", "right",
}
GO_AHEAD_FILLER = {
    "am", "akkor", "is", "meg", "csak", "most", "kerlek", "fel", "tovabb", "ezt", "azt", "mind", "mindet", "mindent",
    "igy", "ugy", "nyugodtan", "nyomjad", "mehet", "igen", "ok", "please", "do", "it", "ahead", "on", "again",
    "then", "all", "that", "this", "now", "go", "keep", "going", "yes", "sure", "ujra", "mindegyiket", "osszeset",
    "mehetnek", "everything", "too", "also", "them",
}
# A verb that acts on the previous work ("commit it", "push", "fix that"): a continuation only with a pronoun object.
ACTION_VERBS = {
    "commit", "commitold", "commitolj", "commitoljuk", "push", "pushold", "pusholj", "pusholjuk", "merge", "mergeld",
    "mergeljuk", "deploy", "deployold", "ship", "apply", "alkalmazd", "run", "futtasd", "implement", "implementald",
    "implementalj", "do", "fix", "javitsd", "start", "inditsd", "finish", "fejezd",
}
ACTION_OBJECTS = {
    "it", "this", "that", "them", "those", "these", "all", "everything", "now", "please", "too", "also", "again",
    "changes", "the", "ezt", "azt", "ezeket", "azokat", "mindet", "mindent", "mind", "osszeset", "most", "kerlek", "is",
    "el", "fel", "ki", "be", "valtozasokat", "valtoztatasokat", "ugyanezt", "ujra", "right", "away", "meg",
}
# A pronoun-only follow-up ("and this?", "ezt is", "ugyanezt a masikra"): only words from this closed set, one a pronoun.
FOLLOWUP_PRONOUNS = {"this", "that", "it", "those", "these", "them", "same", "other", "another", "ez", "ezt", "azt",
                     "ezzel", "azzal", "ugyanezt", "ugyanaz", "ugyanigy", "masik", "masikat", "masikra", "ezek", "azok"}
FOLLOWUP_WORDS = FOLLOWUP_PRONOUNS | {
    "and", "also", "too", "what", "about", "how", "for", "with", "the", "one", "ones", "is", "do", "does", "to", "as",
    "well", "then", "so", "but", "es", "a", "az", "akkor", "de", "viszont", "pedig", "mi", "hogy", "hogyan",
    "van", "lesz", "ugye", "egyebkent", "igy", "nekem", "ra", "re", "val", "vel",
}
STATUS_RE = re.compile(r"^(hogy allunk|hol tartunk|mi a helyzet|kesz vagy|kesz van|elkeszult|megvan|"
                       r"how is it going|how's it going|how are we doing|any progress|are you done|status)\b")
# Questions about the session or the running model: only the parent session can answer them, a fresh
# subagent would not know what is being asked about.
META_RE = re.compile(
    r"\b(which|what)\s+(llm|model|version)\b[^.?!]{0,40}\b(are you|you(\s+are)?\s+(using|running)|is this|am i\s+(using|talking)|do you use)"
    r"|\b(who|what)\s+are\s+you\b"
    r"|\b(melyik|milyen)\s+(nyelvi\s+)?modell?\w*[^.?!]{0,40}\b(vagy|vagytok|dolgozol|futsz|hasznalsz|hasznalod|beszelek|fut)"
    r"|\bki\s+vagy\b"
    r"|\bhow\s+much\s+(context|token)\w*|\b(context|kontextus)\w*\s+(left|remain\w*|usage|window)|\bmennyi\s+(kontextus|token)\w*"
    r"|\bwhat\s+(did|have)\s+(we|you|i)\s+(just\s+)?(do|done|change\w*|talk\w*|discuss\w*)\b"
    r"|\bmit\s+(csinaltunk|csinaltal|csinaltam|vegeztunk|valtoztattal|valtoztattunk|beszeltunk)\b"
    r"|\bsummari[sz]e\s+(our|this|the)\s+(chat|conversation|session|discussion)\b"
    r"|\bfoglald\s+ossze\s+(a\s+)?(beszelgetest|sessiont|eddigieket|eddig)\b")
META_MAX_WORDS = 14
CONTINUATION_MAX_WORDS = 5
# A go-ahead in one language is answered in it ("Mehet" after an English paste); "ok" keeps the previous language.
CONTINUATION_LANG_WORDS = {
    "hu": {"mehet", "mehetsz", "mehetunk", "mehetnek", "mehetne", "igen", "rendben", "persze", "jo", "johet", "nyomjad",
           "nyomd", "nyomj", "csinald", "csinaljuk", "folytasd", "folytassuk", "folytatas", "kovetkezo", "kovi", "hajra",
           "tovabb", "lehet", "tessek", "pontosan", "szuper", "koszi", "koszonom", "kerlek", "akkor", "meg", "csak", "most",
           "mind", "mindet", "mindent", "nyugodtan", "ujra", "hogy", "allunk", "hol", "tartunk", "helyzet", "kesz",
           "elkeszult", "megvan", "ezt", "azt", "ugyanezt", "jovahagyom", "elfogadom"},
    "en": {"yes", "yeah", "yep", "yup", "sure", "go", "continue", "proceed", "next", "retry", "please", "thanks",
           "great", "perfect", "done", "try", "do", "ahead", "again", "then", "keep", "going", "how", "progress",
           "status", "are", "you", "doing", "commit", "push", "it", "approved", "everything"},
}


def is_continuation(prompt):
    """A bare go-ahead ("mehet", "Mehetnek igen mind", "yes, do it", "igen torold!"), an action on the previous work
    ("commit it") or a pronoun-only follow-up ("and this?"), or a status check ("hogy allunk?"): at most one word
    beyond the go-ahead and filler words, so "ok, most irj teszteket" is new work."""
    own, pasted = split_pasted(prompt)
    if pasted:
        return False
    words = re.findall(r"[a-z0-9']+", _norm(own))
    if not words or len(words) > CONTINUATION_MAX_WORDS:
        return False
    if STATUS_RE.match(" ".join(words)):
        return True
    if words[0] in GO_AHEAD:
        return len([w for w in words[1:] if w not in GO_AHEAD_FILLER and w not in GO_AHEAD]) <= 1
    if words[0] in ACTION_VERBS:
        return all(w in ACTION_OBJECTS or w in GO_AHEAD for w in words[1:])
    return all(w in FOLLOWUP_WORDS for w in words) and any(w in FOLLOWUP_PRONOUNS for w in words)


def is_meta_question(prompt):
    """A question about the session or the running model ("which model are you?", "mit csinaltunk eddig?")."""
    own, pasted = split_pasted(prompt)
    if pasted or FENCE_RE.search(own) or len(own) > 120:
        return False
    text = _norm(own)
    return len(text.split()) <= META_MAX_WORDS and bool(META_RE.search(text))


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
    r"|\b(and|then|and\s+then)\s+send\s+(it|them|this|that|out|off)\b|\bsend\s+(it|them)\s+(out|off|now)\b"
    r"|\bsend\w*\s[^.?!\n]{0,50}\b(to|by|via)\s+(my\s|him\b|her\b|them\b|the\s+(team|client|customer|boss|group|channel)|\S+@\S+|e-?mail|slack|teams|whatsapp|messenger)"
    r"|\bpublikal\w*|\bkozze\b|\bkozzete\w*|\bpublish\w*|\bposztol\w*|\btweetel\w*|\btweet\s+(it|this)"
    r"|\bpost\w*\s[^.?!\n]{0,30}\b(on|to)\s+(twitter|x\b|linkedin|facebook|instagram|reddit|slack|discord|the\s+blog|my\s+blog)"
    r"|\butal(d|j|jon|ok|junk)\b|\batutal\w*|\bfizes(s|sd|sen)\b|\bfizesd\s+ki|\bvasarol(j|d|jon)\b|\bvegy(el|ed|uk)\s+(meg|egy)"
    r"|\brendeld\s+meg|\bplace\s+(an?\s+)?order|\border\s+[\w\s]{1,30}\b(from|on|online)\b|\bbuy\s+(me\s+)?(an?|the|some|\d+)\b"
    r"|\bpurchase\w*|\bpay\s+(for|the|my|\$|\d)|\btransfer\s+(the\s+)?(\$|\d|money|funds|rent|payment|salary)|\bcheckout\b)"
)


def is_destructive(prompt):
    return bool(DESTRUCTIVE_RE.search(_norm(prompt)))


# Writing a draft for the user (an email, a letter) is harmless; sending it is not. The destructive regex still
# sees "and send it", so this only silences the classifier's score for a pure drafting request.
DRAFTING_RE = re.compile(
    r"^\W*(please\s+|kerlek\s+)?(write|draft|compose|prepare|rewrite|proofread|polish|irj|irjal|ird\s+meg|fogalmazz?"
    r"(\s+meg)?|keszits|javitsd\s+ki)\b[^.?!\n]{0,80}\b(e-?mails?|letters?|cover\s+letter|messages?|notes?|replies|reply|"
    r"emailt|e-?mailt|levelet|level|uzenetet|uzenet|valaszt|valasz|jegyzetet)\b")
SENDING_RE = re.compile(r"\b(send|post|publish|submit|deliver|dispatch|forward|kuld\w*|elkuld\w*|kozzet\w*|publikal\w*|tedd\s+ki)\b")


def is_drafting(prompt):
    t = _norm(split_pasted(prompt)[0])
    return bool(DRAFTING_RE.search(t)) and not SENDING_RE.search(t) and not is_destructive(prompt)


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
    that account's credit without the user ever opting in.
    A stored key may be protected (secret.py); one that cannot be decrypted here counts as no key."""
    cfg = user_config()
    key = os.environ.get("TYPESAFE_API_KEY") or secret.reveal(cfg.get("typesafe_api_key"))
    if key:
        return "typesafe", API_URL, key, JEV_MODEL
    key = os.environ.get("JEV_OPENROUTER_API_KEY") or secret.reveal(cfg.get("openrouter_api_key"))
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
    """The Claude policy: only a generic family alias (one plain word such as `opus` or `haiku`), never a pinned
    or dated model ID (`claude-opus-4-1-20250805`), never a mode alias (`opusplan`, `default`). Families listed
    in the catalog's optional `excluded_families` are refused (none by default)."""
    claude = (cfg if cfg is not None else load_json("models.json", {})).get("claude", {})
    a = str(alias or "").strip().lower()
    if any(x in a for x in claude.get("excluded_families", [])):
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


ROLE_ORDER = ("fast", "balanced", "deep")


def _catalog_role(provider, model_id):
    """The role models.json (or models.local.json) gives the model, even when it is not selectable."""
    cfg = load_json("models.json", {})
    base = base_provider(provider)
    for model in (cfg.get(provider) or cfg.get(base) or {}).get("models", []):
        if model.get("id") == model_id:
            return model.get("role", "balanced")
    entry = model_overrides().get(base, {}).get(model_id)
    return (entry.get("role") if isinstance(entry, dict) else None) or "balanced"


def available_model(provider, model_id, models=None):
    """`model_id` while it is selectable. Once it is not (a tier in targets.json names a model the daily discovery
    removed), the closest selectable model of the provider: the same role first, then the nearest role (ties go to
    the stronger one), in catalog order. With no selectable model at all, `model_id` unchanged."""
    models = models_for(provider) if models is None else models
    if not model_id or not models or model_id in models:
        return model_id
    rank = {r: i for i, r in enumerate(ROLE_ORDER)}
    want = rank.get(_catalog_role(provider, model_id), 1)
    return min(models, key=lambda m: (abs(rank.get(models[m].get("role"), 1) - want),
                                      -rank.get(models[m].get("role"), 1)))


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


def _short_desc(desc):
    desc = " ".join(str(desc or "").split())
    return desc if len(desc) <= SKILL_DESC_CHARS else desc[:SKILL_DESC_CHARS - 3].rstrip() + "..."


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
        skills = {name: _short_desc(desc) for name, desc in skills.items()}
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
    import urllib.request  # lazy: the network stack is only paid for when JEV is actually called
    body = json.dumps({"state": prompt[:MAX_STATE_CHARS], "model": model, "questions": questions}).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if KEEPALIVE and not urllib.request.getproxies().get(url.split(":", 1)[0]):  # http.client knows no proxies
        return json.loads(_post_keepalive(url, body, headers).decode("utf-8"))
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


# Keep-alive for the long-running hook daemon (daemon.py sets KEEPALIVE): a short-lived command hook pays a new TLS
# handshake per call anyway. Idle connections are pooled per (scheme, host, port); a request takes one out under
# the lock, so parallel prompts never share a connection.
KEEPALIVE = False
_IDLE = {}
_IDLE_LOCK = threading.Lock()
_IDLE_MAX = 4


def _post_keepalive(url, body, headers):
    """POSTs on a pooled connection and returns the response body. A reused connection the server has closed in
    the meantime is replaced once and the request retried; a timeout is not retried (TIMEOUT_S stays the budget)."""
    import http.client
    import urllib.parse
    u = urllib.parse.urlsplit(url)
    pool_key = (u.scheme, u.hostname, u.port)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    for attempt in (0, 1):
        with _IDLE_LOCK:
            idle = _IDLE.get(pool_key) or []
            conn = idle.pop() if idle else None
        reused = conn is not None
        if conn is None:
            cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
            conn = cls(u.hostname, u.port, timeout=TIMEOUT_S)
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        except TimeoutError:
            conn.close()
            raise
        except (OSError, http.client.HTTPException):
            conn.close()
            if reused and not attempt:
                continue  # stale keep-alive connection: reconnect once
            raise
        if resp.will_close:
            conn.close()
        else:
            with _IDLE_LOCK:
                idle = _IDLE.setdefault(pool_key, [])
                if len(idle) < _IDLE_MAX:
                    idle.append(conn)
                    conn = None
            if conn is not None:
                conn.close()
        if not 200 <= resp.status < 300:
            raise RuntimeError(f"JEV answered HTTP {resp.status}")
        return data
    raise RuntimeError("unreachable")


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
    questions = build_questions({s["name"]: s["description"] for _, s in candidates}, effort, models)
    key = _cache_key(prompt, questions)
    cached = _cache_get(key)
    if cached is not None:
        cached["_cached"] = True
        return cached
    result = ask_jev(prompt, questions)
    answers = result["answers"]
    answers["_backend"] = "jev"
    answers["_jev_model"] = result.get("model")
    _cache_put(key, answers)
    return answers


# Decision cache: the classifier's answers by hash of the exact request (state text + questions), so a repeated
# prompt costs no network call. Bounded, expiring, atomically written, and never a reason for a hook to fail.
def _cache_path():
    return Path(STATE_DIR) / "state" / "answer_cache.json"


def _cache_key(prompt, questions):
    import hashlib
    blob = json.dumps([prompt[:MAX_STATE_CHARS], questions], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _cache_load():
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _cache_get(key):
    try:
        entry = _cache_load().get(key)
        fresh = isinstance(entry, dict) and time.time() - float(entry.get("ts", 0)) < CACHE_TTL_S
        if fresh and isinstance(entry.get("answers"), dict):
            return json.loads(json.dumps(entry["answers"]))
    except Exception:  # noqa: BLE001 - a cache problem is a cache miss
        pass
    return None


def _cache_put(key, answers):
    try:
        from . import platforms
        now = time.time()
        data = {k: v for k, v in _cache_load().items() if isinstance(v, dict) and now - float(v.get("ts", 0)) < CACHE_TTL_S}
        data[key] = {"ts": now, "answers": answers}
        if len(data) > CACHE_MAX:  # drop the oldest
            for k in sorted(data, key=lambda k: data[k].get("ts", 0))[:len(data) - CACHE_MAX]:
                del data[k]
        platforms.atomic_write(_cache_path(), json.dumps(data, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


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


# Real code work: a named file, a path, a function or a multi-step verb. The classifier rates these 0 about a
# quarter of the time; level 0 would cap the model too low and send the work to the cheapest tier.
CODE_WORK_RE = re.compile(
    r"\b[\w.-]+\.(py|js|jsx|ts|tsx|json|md|java|go|rs|cs|cpp|c|h|rb|php|html|css|sql|ya?ml|toml|sh|ps1)\b"
    r"|[\w.-]+[/\\][\w.-]+|\b\w+\(\)|\b(function|method|class|fuggveny|metodus|osztaly)\b"
    r"|\b(implement\w*|refactor\w*|refaktor\w*|migrat\w*|rename|atnevez\w*|then|majd|utana|step\s+by\s+step)\b")
# A short, plain question or chat line: no code, path or file name. Never worth more than the cheapest subagent.
CODE_SIGNS_RE = re.compile(r"`|[\w.-]+\.[a-z]{1,5}\b|[/\\]|\b\w+\(\)|[{};=<>]")


def is_short_chat(prompt, max_chars):
    own = " ".join(split_pasted(prompt)[0].split())
    norm = _norm(own)
    return (bool(own) and len(own) < max_chars and not CODE_SIGNS_RE.search(own)
            and not re.search(LOCAL_TASK_RE["code"], norm) and not re.search(LOCAL_TASK_RE["test"], norm))


def _family_rank(cc, family):
    try:
        return cc["families"].index(family)
    except (KeyError, ValueError):
        return None


def family_cap(cc, task, level):
    """The highest model family the cost-cap table allows for this task and difficulty, or None."""
    by_task = (cc.get("by_task") or {}).get(task) or {}
    return by_task.get(str(level)) or (cc.get("max_family") or {}).get(str(level))


def apply_cost_cap(result, cc):
    """Level 0 never reaches opus/fable and so on (routes.json `cost_cap`): lowers the tier and the model JEV picked
    to the cap. A tier that names no Claude family (main, cli:*) is left alone."""
    apply_family_cap(result, cc, family_cap(cc, result["task"], result["level"]), f"difficulty {result['level']}")


def apply_family_cap(result, cc, cap, why):
    cap_rank = _family_rank(cc, cap)
    if cap_rank is None:
        return
    tier_family = (cc.get("tier_family") or {}).get(result["primary"])
    rank = _family_rank(cc, tier_family)
    if rank is not None and rank > cap_rank:
        result["primary"] = (cc.get("family_tier") or {}).get(cap, result["primary"])
        result["notes"].append(f"capped to {cap} ({why})")
    model_rank = _family_rank(cc, result.get("model"))
    if model_rank is not None and model_rank > cap_rank:
        result["model"] = cap


def decide(answers, routes, skills_by_name=None, prompt=""):
    """'primary' is a tier name; render() turns it into a concrete agent/model/text. `prompt` (the user's own
    words) feeds the code-work bump and the short-chat rule."""
    task = answers["task"]
    diff = answers["difficulty"]
    level = max(0, min(int(round(float(diff.get("score", 1)))), len(DIFFICULTY) - 1))
    if float(diff.get("confidence", 0)) < MIN_CONF:
        level = max(level, 1)
    if prompt and task.get("choice") in ("code", "test") and CODE_WORK_RE.search(prompt):
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
    cc = routes.get("cost_cap")
    if cc:
        apply_cost_cap(result, cc)
        if prompt and result["task"] in cc.get("short_chat_tasks", ("general", "qa")) \
                and is_short_chat(prompt, int(cc.get("short_chat_chars", 40))):
            apply_family_cap(result, cc, cc.get("short_chat_max_family", "haiku"), "short plain request")
    return result


def force_inline(d, note):
    """The decision turned into 'answer in this session' (no subagent, no extra agents, no cross-check)."""
    d.update(primary="main", verify="", extra_agents=0)
    d.pop("model", None)
    d["notes"] = [n for n in d.get("notes", []) if n != "routing uncertain"] + [note]
    return d


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


def resolve_tier(d, targets, models=None, session_model=None, provider=None):
    """(text, effort, model, agent). A plain-text tier answers in-session (model and agent None).
    Placeholders in agent/text: {model}, {model_} (TOML-safe), {effort}, {slug}, {agent}.
    With `provider`, a tier whose model is no longer selectable falls back to the closest one that is."""
    spec = _tier_spec(d, targets)
    if isinstance(spec, str):
        return spec, d.get("effort"), None, None
    model = spec.get("model", "")
    if provider:
        spec, models = _available_spec(d, spec, targets, models, provider)
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


def tier_provider(tier, provider):
    """The provider whose models a tier names: `cli:codex` runs Codex models whatever tool routes it."""
    name = tier or ""
    return name.split(":", 1)[1] if name.startswith("cli:") else base_provider(provider)


def _available_spec(d, spec, targets, models, provider):
    """(spec, models of the tier's provider), the spec re-pointed at a selectable model when its own is not."""
    owner = tier_provider(d.get("primary"), provider)
    pool = models if models is not None and owner == base_provider(provider) else models_for(owner)
    model = spec.get("model", "")
    substitute = available_model(owner, model, pool)
    if substitute == model:
        return spec, pool
    d.setdefault("notes", []).append(f"{model} is not available, using {substitute}")
    agent = spec.get("agent", "")
    if not agent or "{" in agent:
        return dict(spec, model=substitute), pool
    # A literal agent (Antigravity's gemini-pro-worker) belongs to the old model's agent tier.
    tier = pool[substitute].get("agent_tier")
    template = targets.get("agent_template", "")
    if tier and "{tier}" in template:
        return dict(spec, model=substitute, agent=template.format(tier=tier)), pool
    return dict(targets.get("model_pick") or {"text": "Answer directly in this session."}, model=substitute), pool


def _tier_effort(d, spec, mdef):
    """JEV's own model pick is not bound to the tier's effort range, only to the model's levels."""
    effort = d.get("effort")
    if spec.get("efforts") and not (d.get("model") and d.get("backend") == "jev"):
        effort = clamp_effort(effort, spec["efforts"])
    if mdef is not None:
        effort = clamp_effort(effort, mdef["levels"]) if mdef["levels"] else None
    return effort


def machine_route(targets, models, provider, model, effort):
    """(model, effort) for machine readers (the header fields, `route --json`, the MCP Decision). A tier that names
    a model keeps it; a tier that answers in-session names none, so targets.json `machine_default_model` stands in
    (Claude: sonnet) with the effort clamped to its levels. The instruction text is never changed by this."""
    if model:
        return model, effort
    default = targets.get("machine_default_model")
    if not default:
        return None, effort
    default = available_model(provider, default, models)
    mdef = (models if models is not None else models_for(provider)).get(default)
    if mdef is not None and effort and mdef["levels"]:
        effort = clamp_effort(effort, mdef["levels"])
    return default, effort


def render(d, destructive_hit, targets, provider="claude", lang_code="hu", models=None, session_model=None):
    text, effort, model, agent = resolve_tier(d, targets, models, session_model, provider)
    d["effort"] = effort
    if model:
        d["target_model"] = model
    if agent:
        d["target_agent"] = agent
    route_model, route_effort = machine_route(targets, models, provider, model, effort)
    d["route_model"], d["route_effort"] = route_model, route_effort
    machine = "".join(f" {k}={v}" for k, v in (("model", route_model), ("effort", route_effort)) if v)
    parts = [f"[router] backend={d.get('backend', 'override')} task={d['task']} difficulty={d['level']} "
             f"conf={d['task_conf']} lang={lang_code}{machine}.", text]
    if effort and effort not in text and (d.get("target_model") or d.get("target_agent")):
        parts.append(f"Reasoning effort: {effort}.")
    n = d.get("extra_agents", 0)
    if n:
        parts.append(f"Parallelism: up to {n} extra agent(s) may run in parallel (same model and effort as above), "
                     f"only for genuinely independent parts; split the work, then merge and verify the results.")
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

    continuation = is_continuation(prompt)
    if previous and continuation:  # re-use it as is: a go-ahead never downgrades the work it approves
        d = continued_decision(previous, continuation_lang(prompt, previous.get("lang") or lang_code))
        return d, render(d, regex_hit, targets, provider, d["lang"], models, session_model), regex_hit, None

    candidates = catalog.prefilter(own, catalog.load_catalog(), SKILL_CANDIDATES)
    by_name = {s["name"]: s for _, s in candidates}
    # Needs the parent's context (a go-ahead with no history to continue, a question about the session or the
    # model): a fresh subagent would know nothing, so it is answered inline.
    inline_reason = ("a follow-up needs this session's context: answer inline" if continuation
                     else "a question about this session: answer inline" if is_meta_question(prompt) else None)
    fast = "needs-context" if inline_reason else _fast_path_reason(own, pasted, candidates, routes, backend)
    if fast in ("short", "needs-context"):
        candidates, by_name = [], {}  # a one-liner or a follow-up needs no skill: keep the injected text minimal
    error = None
    try:
        answers = classify(own, candidates, backend="local" if fast else backend, effort=effort, models=models,
                           pasted=pasted[:JEV_PASTED_CHARS])
    except Exception as exc:  # any JEV failure falls back to the local classifier
        error = f"{type(exc).__name__}: {exc}"[:200]
        answers = classify(own, candidates, backend="local", effort=effort, models=models)
    d = decide(answers, routes, by_name, prompt=own)
    if inline_reason:
        force_inline(d, inline_reason)
    d["backend"] = answers.get("_backend", "local")
    if fast and not inline_reason:
        d["fast_path"] = fast
    if answers.get("_cached"):
        d["cached"] = True
    d["lang"] = lang_code
    d["skill_candidates"] = [s["name"] for _, s in candidates[:5]]
    if answers.get("_jev_model"):
        d["jev_model"] = answers["_jev_model"]
    jev_hit = d["destructive_p"] is not None and d["destructive_p"] >= DESTRUCTIVE_T and not is_drafting(own)
    hit = regex_hit or jev_hit
    return d, render(d, hit, targets, provider, lang_code, models, session_model), hit, error


def _same_tier_at_low_levels(task, routes):
    """True when the route table sends this task to the same tier at difficulty 0 and 1: only then the local
    classifier's trivial-vs-moderate guess (weak on real prompts) cannot change where the prompt goes."""
    row = (routes.get("table") or {}).get(task)
    if not row:
        return True
    tier = lambda level: row[min(level, len(row) - 1)].partition("+verify:")[0]  # noqa: E731
    return tier(0) == tier(1)


def _fast_path_reason(own, pasted, candidates, routes, backend):
    """Why the network call can be skipped, or None. A trivially short plain line, or a prompt the local classifier
    is sure about (a clear task, nothing hard, no skill in play), is decided locally: JEV adds seconds, not insight."""
    if pasted or backend or BACKEND != "auto" or not use_jev(None):  # an explicit backend is never second-guessed
        return None
    a = local_answers(own)
    if not _same_tier_at_low_levels(a["task"]["choice"], routes):
        return None  # the local trivial/moderate guess is a coin flip and the tier depends on it: that is JEV's
    cc = routes.get("cost_cap") or {}
    if cc and is_short_chat(own, int(cc.get("short_chat_chars", 40))) and a["task"]["confidence"] >= MIN_CONF:
        return "short"  # an unsure local guess would land on the 'uncertain' default: JEV does better there
    if candidates and candidates[0][0] >= 3.0:
        return None  # a skill may be needed: that choice is JEV's
    if len(own) > LOCAL_FAST_MAX_CHARS:
        return None
    if a["task"]["confidence"] >= LOCAL_CONFIDENT and a["task"]["choice"] != "research" \
            and a["difficulty"]["score"] <= 1 and a["needs_web"]["noul"] < LONG_CTX_T and a["long_context"]["noul"] < LONG_CTX_T:
        return "local-confident"
    return None


def override_for(prompt, targets):
    """Whole-tag match, so #fast never fires on #faster."""
    low = prompt.lower()
    for tag, tier in targets.get("overrides", {}).items():
        if re.search(r"(?<![\w#])" + re.escape(tag.lower()) + r"(?![\w-])", low):
            return tier
    return None
