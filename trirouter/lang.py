"""Hungarian vs English: Hungarian-only letters plus a stopword vote. The unaccented stopwords
catch accent-less typing ("irj egy fuggvenyt")."""
import functools
import re
import unicodedata

HU_CHARS = set("áéíóöőúüűÁÉÍÓÖŐÚÜŰ")
HU_WORDS = {
    "az", "egy", "es", "és", "hogy", "nem", "meg", "mi", "mit", "mik", "ki", "van", "vagy",
    "ez", "ezt", "azt", "kell", "kerlek", "kérlek", "nekem", "legyen", "majd", "csak", "hogyan",
    "miert", "miért", "mikor", "hol", "irj", "írj", "keszits", "készíts", "csinald", "csináld",
    "nezd", "nézd", "javitsd", "javítsd", "oldd", "magyarazd", "magyarázd", "egyetemi", "matek",
    "fajl", "fájl", "mappa", "teszt", "tesztet", "kodot", "kódot", "fuggveny", "függvény", "de",
    "mert", "ha", "akkor", "valamint", "illetve", "szerint", "utan", "után", "elott", "előtt",
}
EN_WORDS = {
    "the", "and", "are", "was", "to", "of", "in", "for", "with", "on", "this", "that", "it",
    "please", "write", "make", "create", "fix", "explain", "what", "why", "how", "when", "where",
    "can", "could", "would", "should", "my", "your", "an", "be", "do", "does", "from", "into",
    "run", "test", "tests", "code", "function", "file", "files", "about", "which", "who",
    "solve", "prove", "calculate", "compute", "find", "summarize", "translate", "delete", "send", "add",
    "implement", "review", "design", "plan", "help", "me", "all", "by", "at", "as", "not",
}
_WORD_RE = re.compile(r"[a-záéíóöőúüű]+", re.I)


def detect(text):
    """Returns "hu" or "en". Ties default to Hungarian: a wrong "en" is worse for a Hungarian user."""
    if not text:
        return "hu"
    hu_chars = sum(1 for c in text if c in HU_CHARS)
    words = [w.lower() for w in _WORD_RE.findall(text)]
    hu = sum(1 for w in words if w in HU_WORDS and w not in EN_WORDS) + 2 * min(hu_chars, 5)
    en = sum(1 for w in words if w in EN_WORDS and w not in HU_WORDS)
    return "en" if en > hu else "hu"


@functools.lru_cache(maxsize=16)
def fold(text):
    """Lowercase without accents ("Írj függvényt" -> "irj fuggvenyt"): the keyword patterns are written that way.
    Memoised: one prompt is folded by a dozen checks."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def respond_line(lang):
    return "Respond in English." if lang == "en" else "Respond in Hungarian."
