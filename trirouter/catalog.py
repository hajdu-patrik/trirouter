#!/usr/bin/env python3
"""Catalog of every skill on this machine (~/.skills/catalog.json) + a cheap pre-filter.

A skill native to one tool can be used by another: the router then points at its SKILL.md.
Asking JEV about ~170 skills at once is too much, so prefilter() narrows them first with an
IDF-weighted keyword overlap. Descriptions are English while prompts are often Hungarian, hence
the small HU->EN glossary.
"""
import json
import math
import os
import re
import time
import unicodedata
from pathlib import Path

HOME = Path.home()
HUB = Path(os.environ.get("JEV_SKILLS_HUB", str(HOME / ".skills")))
CATALOG = HUB / "catalog.json"
PROVIDERS = ["claude", "codex", "antigravity"]

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


def _norm(text):
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def stem(t):
    for suf in ("ing", "ions", "ion", "ed", "es", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def tokens(text):
    out = set()
    for t in _TOK.findall(_norm(text)):
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


def build_catalog():
    """First source wins a name clash: the hub beats app-managed copies."""
    seen, items = set(), []
    for root, recursive, native_in, prefix in SOURCES:
        for d in _skill_dirs(root, recursive):
            name, desc = parse_frontmatter(d / SKILL_FILE)
            name = _qualified_name(name or d.name, d, root, prefix)
            if name in seen:
                continue
            seen.add(name)
            items.append({"name": name, "description": (desc or "")[:600],
                          "path": str(d).replace("\\", "/"), "native_in": list(native_in)})
    return items


def write_catalog(items=None):
    items = build_catalog() if items is None else items
    HUB.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text(json.dumps({"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "skills": items},
                                  indent=1, ensure_ascii=False), encoding="utf-8")
    return items


def load_catalog():
    try:
        return json.loads(CATALOG.read_text(encoding="utf-8")).get("skills", [])
    except (OSError, ValueError):
        return []


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
    # the plugin/vendor prefix says nothing about the task
    docs = [(s, tokens(s["name"].split(":")[-1].replace("-", " ")), tokens(s["description"])) for s in catalog]
    df = _doc_freq(docs)
    q = _expand(tokens(prompt))
    raw = _norm(prompt)
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

