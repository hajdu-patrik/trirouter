"""Protected storage for the API keys in ~/.trirouter/config.json (named `secret`, not `secrets`: stdlib).

A stored key is either

  * a plain string          - written by an older version, or on macOS / Linux; still read as is, or
  * {"dpapi": "<base64>"}   - Windows only: encrypted with the Windows Data Protection API for the current
                              user (CryptProtectData). The blob opens only on this account of this machine.

macOS and Linux have no standard-library keystore, so the key stays a string there and the protection is the
file mode: config.json (and its backups) are owner-only, 0600.

`reveal` never raises: whatever cannot be read (a blob copied from another machine or user, a damaged value)
is "no key", so a hook can never crash on it. Standard library only; the hook path imports this module.
"""
import base64
import json
import os
import sys
from pathlib import Path

KEY_FIELDS = ("openrouter_api_key", "typesafe_api_key")
IS_WINDOWS = sys.platform.startswith("win")
DPAPI = "dpapi"

PLAIN, PROTECTED, UNREADABLE, NONE = "plaintext", "protected", "unreadable", "none"


# ---- Windows DPAPI -------------------------------------------------------------------------------------------

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


# ---- files ---------------------------------------------------------------------------------------------------

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
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


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
    from . import platforms as P
    found = []
    for p in backups_with_plaintext(config_path):
        cfg = _load(p)
        found.append((p, plaintext_fields(cfg)))
        if apply:
            P.atomic_write(p, json.dumps(scrub_config(cfg), indent=2), mode=0o600)
    return found


def migrate(config_path, apply):
    """Protects the plaintext keys in config.json and keeps no plaintext copy anywhere.

    Returns [(ok, message)]. Order: the (scrubbed) backup first, then config.json, then older backups. The new
    value is decrypted again before anything is written; if that does not give the original key, nothing changes."""
    from . import platforms as P
    path, out = Path(config_path), []
    cfg = _load(path)
    if cfg is None:
        return [(True, "no config.json: no key stored")]
    plain = plaintext_fields(cfg)
    if plain and protection_available():
        new = protect_config(cfg)
        bad = [k for k in plain if reveal(new[k]) != cfg[k]]
        if bad:
            return [(False, f"cannot protect {', '.join(bad)} on this machine: config.json left unchanged")]
        out.append((True, f"{'would protect' if not apply else 'protected'} {', '.join(plain)} "
                          f"(Windows DPAPI, readable only by this user on this machine)"))
        if apply:
            bak = path.with_name(path.name + ".bak")
            P.atomic_write(bak, json.dumps(scrub_config(cfg), indent=2), mode=0o600)  # no plaintext key in the copy
            P.atomic_write(path, json.dumps(new, indent=2), mode=0o600)
            out.append((True, f"previous config.json kept as {bak.name} without its key"))
        else:
            out.append((True, "would keep the previous config.json as config.json.bak without its key"))
    elif plain:
        out.append((True, "plaintext key kept: this system has no keystore in the standard library; "
                          "config.json is limited to its owner (mode 0600)"))
    else:
        out.append((True, "no plaintext key in config.json"))
    if path.exists() and not mode_is_private(path):
        out.append((True, f"{'restricted' if apply else 'would restrict'} config.json to its owner (mode 0600)"))
        if apply:
            restrict(path)
    for p, fields in scrub_backups(path, apply):
        out.append((True, f"{'removed' if apply else 'would remove'} the plaintext key from {p.name}"))
    if apply and not IS_WINDOWS:
        for p in [path, *backups(path)]:
            if p.exists():
                restrict(p)
    return out
