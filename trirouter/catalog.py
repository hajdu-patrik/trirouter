"""Skill catalog of this machine (~/.skills/catalog.json) and a cheap pre-filter: IDF-weighted keyword overlap narrows
~170 skills before JEV is asked (a small HU->EN glossary bridges Hungarian prompts and English descriptions)."""
import fnmatch
import json
import math
import os
import re
import time
from pathlib import Path, PurePath

from . import lang, platforms as P

HOME = Path.home()
HUB = Path(os.environ.get("JEV_SKILLS_HUB", str(HOME / ".skills")))
CATALOG = HUB / "catalog.json"
PROVIDERS = ["claude", "codex", "antigravity"]
BUNDLED = Path(__file__).resolve().parent / "skills"  # skills shipped with trirouter: never catalog-only
# A skill name is written into the model's context verbatim: a plain identifier, optionally "<vendor>:<name>".
NAME_RE = re.compile(r"[\w:.-]{1,80}")

SOURCES = [
    # (root, recursive, native_in, name prefix). The prefixes match Claude's own skill names, so
    # "Relevant skill: X" is invocable as-is.
    (HUB, False, PROVIDERS, None),
    (HOME / ".claude" / "skills" / "synced", True, ["claude"], "anthropic-skills"),
    (HOME / ".claude" / "plugins" / "synced", True, ["claude"], "plugin"),
    (HOME / ".codex" / "skills" / ".system", False, ["codex"], None),
    (HOME / ".gemini" / "antigravity-cli" / "builtin" / "skills", False, ["antigravity"], None),
]

STOP = {
    "the", "and", "for", "with", "use", "used", "using", "when", "this", "that", "from", "into", "your",
    "you", "are", "not", "any", "all", "can", "will", "about", "such", "also", "other", "than", "then",
    "user", "users", "asks", "asked", "wants", "want", "need", "needs", "skill", "skills", "does", "dont",
    "only", "like", "more", "these", "those", "their", "them", "have", "has", "it's", "its", "via", "e.g",
    "egy", "hogy", "nem", "kell", "legyen", "kerlek", "nekem", "majd", "csak", "illetve", "valamint",
    "irj", "keszits", "csinald", "nezd", "ezt", "azt", "meg", "van", "vagy", "mint", "minden",
}
# Hungarian (accent-stripped) stem -> English words that skill descriptions actually use.
GLOSSARY = {
    "prezentac": "presentation slides deck pptx", "dia": "slides deck", "diasor": "slides deck pptx",
    "dokumentum": "document docx", "word": "docx document", "tablazat": "spreadsheet excel xlsx",
    "excel": "spreadsheet xlsx", "pdf": "pdf", "teszt": "test testing", "hibakeres": "debug debugging bug",
    "hiba": "bug debug error", "terv": "plan planning", "tervez": "design plan", "dizajn": "design ui",
    "felulet": "ui interface frontend design", "weboldal": "website frontend web", "adatbazis": "database sql",
    "kep": "image", "grafikon": "chart visualization", "diagram": "diagram chart", "osszefoglal": "summarize summary",
    "kodellenor": "code review", "review": "review", "atnez": "review", "bongesz": "browser",
    "telepit": "deploy install", "felho": "cloud", "kereses": "search", "jegyzet": "notes memory",
    "emlekez": "memory", "reggel": "morning briefing", "otlet": "brainstorming ideas", "refaktor": "refactor",
    "skill": "skill skills", "keszseg": "skill skills", "agens": "agent agents", "utemez": "schedule",
}
FORMAT_NAMES = {"pdf", "docx", "xlsx", "pptx", "csv"}
SKILL_FILE = "SKILL.md"
_TOK = re.compile(r"[a-z0-9][a-z0-9+#.-]{2,}")
# Linear-time patterns: no two adjacent quantifiers that can match the same characters.
_FRONTMATTER = re.compile(r"^﻿?---[ \t]*\n(.*?)\n---", re.S)
_NAME = re.compile(r"^name:(.*)$", re.M)
_DESCRIPTION = re.compile(r"^description:(.*)$", re.M)
_BLOCK_MARKERS = (">", "|", ">-", "|-", "")


def stem(t):
    for suf in ("ing", "ions", "ion", "ed", "es", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def tokens(text):
    out = set()
    for t in _TOK.findall(lang.fold(text)):
        t = t.strip(".-")
        if t not in STOP and len(t) >= 3:
            out.add(stem(t))
    return out


def _unquote(value):
    value = value.strip()
    if value[:1] in ("'", '"'):
        value = value[1:]
    if value[-1:] in ("'", '"'):
        value = value[:-1]
    return value.strip()


def _indented_block(lines):
    out = []
    for line in lines:
        if line.startswith((" ", "\t")):
            out.append(line.strip())
        elif line.strip():
            break
    return " ".join(out)


def parse_frontmatter(skill_md):
    """(name, description) from a SKILL.md frontmatter, without a YAML dependency."""
    try:
        text = Path(skill_md).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    m = _FRONTMATTER.match(text)
    if not m:
        return None, None
    fm, desc = m.group(1), None
    nm = _NAME.search(fm)
    name = (_unquote(nm.group(1)) or None) if nm else None
    dm = _DESCRIPTION.search(fm)
    if dm:
        first = dm.group(1).strip()
        if first in _BLOCK_MARKERS:
            desc = _indented_block(fm[dm.end():].split("\n")[1:])
        else:
            desc = first.strip("'\"")
    return name, desc


def is_skill_dir(path):
    return (Path(path) / SKILL_FILE).is_file()


def valid_name(name):
    return isinstance(name, str) and bool(NAME_RE.fullmatch(name))


def known_path(path):
    """True when `path` lies inside one of the skill roots this catalog indexes: catalog.json is data, so a path
    from it is only ever quoted to the model when it points where a skill can be."""
    if not isinstance(path, str) or not path:
        return False
    p = PurePath(os.path.normcase(path))
    return any(p.is_relative_to(PurePath(os.path.normcase(str(root)))) for root, _, _, _ in SOURCES)


def _skill_dirs(root, recursive):
    if not root.is_dir():
        return []
    if recursive:
        return sorted({p.parent for p in root.rglob(SKILL_FILE)})
    return sorted(p for p in root.iterdir() if is_skill_dir(p))


def _qualified_name(name, skill_dir, root, prefix):
    if prefix == "plugin":
        try:  # .../plugins/synced/<account>/<plugin>/skills/<skill>/SKILL.md
            return f"{skill_dir.relative_to(root).parts[1]}:{name}"
        except (ValueError, IndexError):
            return name
    return f"{prefix}:{name}" if prefix else name


def parse_patterns(value):
    """Comma-separated fnmatch patterns (or a list of them) -> lowercase, de-duplicated, in order. "" -> []."""
    parts = value.split(",") if isinstance(value, str) else [x for x in (value or ()) if isinstance(x, str)]
    return list(dict.fromkeys(p.strip().lower() for p in parts if p.strip()))


def catalog_only_patterns():
    """config.json -> skills.catalog_only; hub linking and the catalog both read it. Never raises: hooks call it."""
    try:
        cfg = json.loads((P.state_dir() / "config.json").read_text(encoding="utf-8"))
        return parse_patterns(cfg["skills"]["catalog_only"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []


def is_bundled(skill_dir, bundled=None):
    """A skill that ships with trirouter (also from a moved clone: <any>/trirouter/skills)."""
    parent = Path(os.path.realpath(skill_dir)).parent
    if parent == Path(os.path.realpath(bundled or BUNDLED)):
        return True
    return tuple(os.path.normcase(x) for x in parent.parts[-2:]) == (os.path.normcase("trirouter"), "skills")


def is_catalog_only(skill_dir, patterns, bundled=None):
    """Folder name matches a pattern (case-insensitive fnmatch) and the skill is not a bundled one."""
    name = Path(skill_dir).name.lower()
    return any(fnmatch.fnmatchcase(name, p) for p in patterns) and not is_bundled(skill_dir, bundled)


def build_catalog(catalog_only=None):
    """First source wins a name clash (the hub beats app-managed copies). Catalog-only hub skills (default: config.json)
    are native to Antigravity alone: it reads the whole hub, while Claude and Codex are pointed at their SKILL.md."""
    patterns = catalog_only_patterns() if catalog_only is None else parse_patterns(catalog_only)
    seen, items = set(), []
    for root, recursive, native_in, prefix in SOURCES:
        for d in _skill_dirs(root, recursive):
            name, desc = parse_frontmatter(d / SKILL_FILE)
            name = _qualified_name(name if valid_name(name) else d.name, d, root, prefix)
            if name in seen or not valid_name(name):
                continue
            seen.add(name)
            native = list(native_in)
            if root == HUB and is_catalog_only(d, patterns):
                native = [p for p in native if p == "antigravity"]
            items.append({"name": name, "description": (desc or "")[:600],
                          "path": str(d).replace("\\", "/"), "native_in": native})
    return items


def write_catalog(items=None, catalog_only=None):
    items = build_catalog(catalog_only) if items is None else items
    HUB.mkdir(parents=True, exist_ok=True)
    P.atomic_write(CATALOG, json.dumps({"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "skills": items},
                                       indent=1, ensure_ascii=False))
    return items


class _Catalog(list):
    """The skill list plus the identity of the file it came from (the key of the on-disk index cache)."""
    index_key = None


INDEX_VERSION = 1  # bump when the token/IDF index format or the tokenizer changes
_INDEX_MEMO = {}   # (key) -> (docs, df) for this process


def _index_path():
    return P.state_dir() / "catalog-index.json"


def load_catalog():
    try:
        st = CATALOG.stat()
        skills = json.loads(CATALOG.read_text(encoding="utf-8")).get("skills", [])
    except (OSError, ValueError, AttributeError):
        return []
    out = _Catalog(skills)
    out.index_key = [INDEX_VERSION, str(CATALOG), st.st_mtime_ns, st.st_size, len(skills)]
    return out


def _build_index(catalog):
    # the plugin/vendor prefix says nothing about the task
    docs = [(s, tokens(s["name"].split(":")[-1].replace("-", " ")), tokens(s["description"])) for s in catalog]
    return docs, _doc_freq(docs)


def _cached_index(catalog):
    """(docs, df) for the catalog; reuses a token index cached on disk (keyed by the catalog file's
    mtime + size + format version) and silently recomputes on any problem: hooks must never crash."""
    key = getattr(catalog, "index_key", None)
    if not key:
        return _build_index(catalog)
    tkey = tuple(map(str, key))
    if tkey in _INDEX_MEMO:
        return _INDEX_MEMO[tkey]
    try:
        raw = json.loads(_index_path().read_text(encoding="utf-8"))
        if raw["key"] == key and len(raw["docs"]) == len(catalog):
            docs = [(s, frozenset(nt), frozenset(dt)) for s, (nt, dt) in zip(catalog, raw["docs"])]
            result = (docs, {str(k): int(v) for k, v in raw["df"].items()})
            _INDEX_MEMO.clear()
            _INDEX_MEMO[tkey] = result
            return result
    except Exception:  # missing, corrupt, stale: recompute
        pass
    docs, df = _build_index(catalog)
    try:
        P.atomic_write(_index_path(), json.dumps(
            {"key": key, "docs": [[sorted(nt), sorted(dt)] for _, nt, dt in docs], "df": df}, separators=(",", ":")))
    except Exception:
        pass
    _INDEX_MEMO.clear()
    _INDEX_MEMO[tkey] = (docs, df)
    return docs, df


def _expand(prompt_tokens):
    out = set(prompt_tokens)
    for t in prompt_tokens:
        for prefix, words in GLOSSARY.items():
            if t.startswith(prefix):
                out.update(stem(w) for w in words.split())
    return out


def _doc_freq(docs):
    df = {}
    for _, nt, dt in docs:
        for t in nt | dt:
            df[t] = df.get(t, 0) + 1
    return df


def _overlap(q, nt, dt, df, n_docs):
    """(score, hits, name_hits); a hit on the skill's name counts double."""
    matched = [t for t in q if t in nt or t in dt]
    score = sum(((2 if t in nt else 1) * math.log(1 + n_docs / df.get(t, 1)) for t in matched), 0.0)
    return score, len(matched), sum(t in nt for t in matched)


def _name_evidence(nt, name_hits, hits, df):
    """Most of the name's words must match AND the prompt must overlap the skill in two places:
    "auth" alone does not make google-cloud-recipe-auth the skill for "refactor the auth module"."""
    if not nt:
        return False
    rare_name = name_hits == len(nt) and all(df.get(t, 0) <= 3 and len(t) >= 6 for t in nt)
    only = next(iter(nt))
    short_single = len(nt) == 1 and len(only) < 6 and only not in FORMAT_NAMES  # "learn", "docs"
    return name_hits / len(nt) >= 0.66 and (hits >= 2 or rare_name) and not short_single


def _mention_bonus(skill_name, raw):
    if skill_name.lower() in raw:
        return 6
    base = skill_name.split(":")[-1].lower()
    distinctive = "-" in base or base in FORMAT_NAMES or re.search(r"\b" + re.escape(base) + r"\s+(skill|keszseg)", raw)
    # (?![a-z0-9]) rather than (?![\w-]): Hungarian suffixes are hyphenated ("pdf-et")
    if distinctive and re.search(r"(?<![\w-])" + re.escape(base) + r"(?![a-z0-9])", raw):
        return 4
    return 0


def prefilter(prompt, catalog, n=8, min_score=1.5):
    """[(score, skill_dict)] best-first, at most n, each scoring >= min_score."""
    if not catalog:
        return []
    docs, df = _cached_index(catalog)
    q = _expand(tokens(prompt))
    raw = lang.fold(prompt)
    scored = []
    for s, nt, dt in docs:
        score, hits, name_hits = _overlap(q, nt, dt, df, len(docs))
        name_hit = _name_evidence(nt, name_hits, hits, df)
        bonus = _mention_bonus(s["name"], raw)
        if bonus:
            score += bonus
            name_hit = True
        if score >= min_score:
            # evidence strength: the local classifier commits to a skill only on a name hit
            scored.append((round(score, 2), dict(s, _hits=hits, _name_hit=name_hit)))
    scored.sort(key=lambda x: -x[0])
    return scored[:n]

