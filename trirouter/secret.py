"""Protected storage for the API keys in config.json (named `secret`, not `secrets`: stdlib): a plain string (macOS /
Linux, file mode 0600) or {"dpapi": "<base64>"} on Windows; `reveal` never raises."""
import base64
import json
import os
import re
from pathlib import Path

from . import platforms as P

KEY_FIELDS = ("openrouter_api_key", "typesafe_api_key")
IS_WINDOWS = P.IS_WINDOWS
DPAPI = "dpapi"

PLAIN, PROTECTED, UNREADABLE, NONE = "plaintext", "protected", "unreadable", "none"

# Each pattern keeps its `keep` group (a label, "Bearer", the URL user) and masks the `secret` after it: values after a
# password / key label, bearer tokens, URL passwords, tokens by shape (OpenAI, GitHub, Google, Slack, AWS, JWT).
_SECRET_RES = (
    re.compile(r"(?P<keep>\b(?:password|passwd|pwd|api[_-]?key|secret[_-]?key|access[_-]?key|auth[_-]?token"
               r"|api[_-]?token)\b\s*[=:]\s*[\"']?)(?P<secret>[^\s\"']{4,})", re.I),
    re.compile(r"(?P<keep>\bbearer\s+)(?P<secret>[\w.~+/=-]{8,})", re.I),
    re.compile(r"(?P<keep>://[^\s/:@]+:)(?P<secret>[^\s/@]+)(?=@)"),
    re.compile(r"(?P<keep>)(?P<secret>sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}"
               r"|xox[abp]-[\w-]{10,}|\bAKIA[0-9A-Z]{16}\b|\b\d/0A[\w-]{20,}|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"
               r"|\b(?=[\w+=-]*\d)(?=[\w+=-]*[A-Za-z])[\w+=-]{32,}\b)"),
)


def redact(text):
    """Masks the secrets a prompt may carry before the text is logged or sent to JEV."""
    for pattern in _SECRET_RES:
        text = pattern.sub(lambda m: m.group("keep") + "[redacted]", text)
    return text


def _dpapi(encrypt, data):
    """CryptProtectData / CryptUnprotectData for the current user; bytes in, bytes out. Raises OSError."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    fn.restype = wintypes.BOOL
    fn.argtypes = ([Blob, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                    ctypes.POINTER(Blob)] if encrypt else
                   [Blob, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                    ctypes.POINTER(Blob)])
    buf = ctypes.create_string_buffer(data, len(data))
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1  # a hook has no one to ask
    ok = (fn(src, "trirouter", None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)) if encrypt else
          fn(src, None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protection_available():
    """True where a key can be encrypted (Windows DPAPI); elsewhere the file mode is the protection."""
    return IS_WINDOWS


def protect(value):
    """The form to store: {"dpapi": ...} on Windows, the string itself elsewhere (and when encryption fails -
    a key that cannot be stored encrypted is still better stored than lost)."""
    if not isinstance(value, str) or not value or not IS_WINDOWS:
        return value
    try:
        return {DPAPI: base64.b64encode(_dpapi(True, value.encode("utf-8"))).decode("ascii")}
    except (OSError, ValueError, ImportError, AttributeError):
        return value


def reveal(stored):
    """The key as a string, or "" when there is none or it cannot be decrypted here. Never raises."""
    try:
        if isinstance(stored, str):
            return stored
        if isinstance(stored, dict) and isinstance(stored.get(DPAPI), str) and IS_WINDOWS:
            return _dpapi(False, base64.b64decode(stored[DPAPI], validate=True)).decode("utf-8")
    except Exception:  # noqa: BLE001 - a foreign or damaged blob is simply no key
        pass
    return ""


def status(stored):
    """NONE (not set), PLAIN, PROTECTED (readable here) or UNREADABLE (set, but not readable on this account)."""
    if not stored:
        return NONE
    if isinstance(stored, str):
        return PLAIN
    return PROTECTED if reveal(stored) else UNREADABLE


def plaintext_fields(cfg):
    """The key fields of a config dict that hold a plain string."""
    return [k for k in KEY_FIELDS if isinstance(cfg, dict) and isinstance(cfg.get(k), str) and cfg[k]]


def write_config(path, cfg):
    """config.json: keys protected where the system can, atomic and owner-only from the start."""
    P.atomic_write(path, json.dumps(protect_config(cfg), indent=2), mode=0o600)


def protect_config(cfg):
    """A copy of `cfg` with every plaintext key protected (unchanged when nothing can or needs to be)."""
    out = dict(cfg)
    for k in plaintext_fields(cfg):
        out[k] = protect(cfg[k])
    return out


def scrub_config(cfg):
    """A copy of `cfg` without its plaintext keys, for backups. A protected blob may stay: it is useless
    on any other account."""
    return {k: v for k, v in cfg.items() if k not in plaintext_fields(cfg)}


def restrict(path):
    """Owner-only (0600) on POSIX; on Windows the user profile's ACL already limits the folder."""
    if IS_WINDOWS:
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def mode_is_private(path):
    """False when a POSIX file is readable by group or others; always True on Windows."""
    if IS_WINDOWS:
        return True
    try:
        return (os.stat(path).st_mode & 0o077) == 0
    except OSError:
        return True


def _load(path):
    return P.read_json(path, encoding="utf-8-sig", kind=dict)


def backups(config_path):
    """trirouter's own copies of config.json (config.json.bak, config.json.<anything>) next to it."""
    config_path = Path(config_path)
    try:
        return sorted(p for p in config_path.parent.glob(config_path.name + ".*") if p.is_file())
    except OSError:
        return []


def backups_with_plaintext(config_path):
    return [p for p in backups(config_path) if plaintext_fields(_load(p) or {})]


def scrub_backups(config_path, apply):
    """[(path, fields)] of the backups that hold a plaintext key; with `apply` the keys are removed from them."""
    found = []
    for p in backups_with_plaintext(config_path):
        cfg = _load(p)
        found.append((p, plaintext_fields(cfg)))
        if apply:
            P.atomic_write(p, json.dumps(scrub_config(cfg), indent=2), mode=0o600)
    return found
