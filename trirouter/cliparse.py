"""Argument parsing against manual.py: an unknown or misplaced flag, a value flag without a value or a boolean flag
given one is a usage error (exit 2) with a "did you mean" hint. `route` is parsed in cli."""
import difflib
from dataclasses import dataclass, field

from . import manual


MAX_INT = 36500  # days: a larger count overflows datetime arithmetic


class UsageError(Exception):
    def __init__(self, message, hint=""):
        super().__init__(message)
        self.hint = hint


@dataclass
class Invocation:
    kind: str                  # "run" | "help" | "overview" | "version" (--version)
    key: str = ""              # command key ("skills", "quarantine restore"), the page for kind == "help"
    flags: dict = field(default_factory=dict)
    positional: list = field(default_factory=list)


def _value_error(f, text, cmd):
    return UsageError(f"{cmd.key}: {f.long}: invalid value {text!r}" if text else f"{cmd.key}: {f.long} needs a value",
                      f"expected {f.choices_hint or ' or '.join(f.choices)}" if f.choices else
                      (f"expected a whole number from 0 to {MAX_INT}" if f.kind == "int" else ""))


def _check_value(f, text, cmd):
    if not text:
        raise _value_error(f, "", cmd)
    if f.kind == "int" and not (text.isascii() and text.isdigit() and int(text) <= MAX_INT):
        raise _value_error(f, text, cmd)
    if f.choices:
        if text.lower() not in f.choices:
            raise _value_error(f, text, cmd)
        return text.lower()
    return text


def _unknown(name, cmd, prog):
    removed = manual.REMOVED_FLAGS.get((cmd.key, name))
    if removed:
        return UsageError(f"{cmd.key}: {removed.replace('{prog}', prog)}", f"Run `{prog} help {cmd.key}` for the options.")
    own = [f.long for f in manual.all_flags(cmd)]
    near = difflib.get_close_matches(name, own, n=1, cutoff=0.5)
    elsewhere = sorted({c.key for c in manual.COMMANDS.values() if name in (f.long for f in c.flags)})
    msg = f"{cmd.key}: unknown option {name}" if not elsewhere else f"{cmd.key}: option {name} does not apply here"
    hints = []
    if elsewhere:
        hints.append(f"{name} is accepted by: {', '.join(elsewhere)}")
    elif near:
        hints.append(f"did you mean {near[0]}?")
    hints.append(f"Run `{prog} help {cmd.key}` for the options.")
    return UsageError(msg, "\n".join(hints))


def parse_flags(cmd, args, prog, command_names=()):
    """(flags, positional) of `args` for one command; flags are keyed by their long form."""
    by_long = {f.long: f for f in manual.all_flags(cmd)}
    by_short = {f.short: f for f in manual.all_flags(cmd) if f.short}
    flags, pos, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos += args[i + 1:]
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            f = by_long.get(name)
            if f is None:
                raise _unknown(name, cmd, prog)
            if not f.value:
                if eq:
                    raise UsageError(f"{cmd.key}: {name} does not take a value", f"Run `{prog} help {cmd.key}` for the options.")
                flags[name] = True
            elif eq:
                flags[name] = _check_value(f, val, cmd)
            elif f.optional:
                nxt = args[i + 1] if i + 1 < len(args) else None
                if nxt is not None and not nxt.startswith("-") and nxt not in command_names:
                    flags[name] = nxt
                    i += 1
                else:
                    flags[name] = True
            else:
                nxt = args[i + 1] if i + 1 < len(args) else None
                if nxt is None or (nxt.startswith("-") and nxt != "-"):
                    raise UsageError(f"{cmd.key}: {name} needs a value ({name}={f.value})",
                                     f"Run `{prog} help {cmd.key}` for the options.")
                flags[name] = _check_value(f, nxt, cmd)
                i += 1
        elif a.startswith("-") and len(a) > 1:
            for ch in a[1:]:
                f = by_short.get("-" + ch)
                if f is None:
                    raise UsageError(f"{cmd.key}: unknown option -{ch}", f"Run `{prog} help {cmd.key}` for the options.")
                if f.value:
                    raise UsageError(f"{cmd.key}: -{ch} needs a value; use {f.long}={f.value}")
                flags[f.long] = True
        else:
            pos.append(a)
        i += 1
    return flags, pos


def _union_value_flags():
    out = {}
    for c in manual.COMMANDS.values():
        for f in c.flags:
            if f.value:
                out[f.long] = f
    return out


def _locate_command(args, names):
    """Legacy `python install.py --yes skills`: the index of the first non-flag token, or None."""
    values = _union_value_flags()
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return None
        if a.startswith("-"):
            f = values.get(a) if "=" not in a else None
            if f and i + 1 < len(args) and not args[i + 1].startswith("-") and (not f.optional or args[i + 1] not in names):
                i += 1
        else:
            return i
        i += 1
    return None


def _first_word(args, cmd):
    """The index of the first token that is neither a flag nor the value of one of cmd's subcommands' flags."""
    values = {f.long for s in manual.SUBCOMMANDS[cmd] for f in manual.all_flags(manual.COMMANDS[f"{cmd} {s}"])
              if f.value and not f.optional}
    for i, a in enumerate(args):
        if not a.startswith("-") and not (i and args[i - 1] in values):
            return i
    return None


def suggest(word, choices):
    near = difflib.get_close_matches(word, list(choices), n=1, cutoff=0.5)
    return f"did you mean {near[0]}?" if near else ""


def resolve(argv, prog, install_py=False):
    """The Invocation for argv. `install_py`: python install.py, where no command means setup and flags may precede it."""
    args = list(argv)
    names = tuple(manual.ORDER)
    if not args:
        return Invocation("run", "setup") if install_py else Invocation("overview")
    if args[0] in ("-h", "--help"):
        return Invocation("overview")
    if args[0] in ("-V", "--version"):
        return Invocation("version")
    if not args[0].startswith("-"):
        cmd, rest = args[0], args[1:]
    else:
        idx = _locate_command(args, names)
        if idx is None:
            cmd, rest = "setup", args
            if not install_py:
                raise UsageError(f"unexpected option {args[0].partition('=')[0]} before a command",
                                 f"Run `{prog} help` for the command list.")
        else:
            cmd, rest = args[idx], args[:idx] + args[idx + 1:]
            if cmd == "route":
                raise UsageError("route: must be the first argument", f"Run `{prog} help route` for the usage.")
    if cmd in manual.REMOVED_COMMANDS:
        raise UsageError(manual.REMOVED_COMMANDS[cmd].replace("{prog}", prog))
    if cmd not in manual.COMMANDS or " " in cmd:
        raise UsageError(f"unknown command {cmd!r}", " ".join(
            x for x in (suggest(cmd, names), f"Run `{prog} help` for the command list.") if x))
    head = rest[:rest.index("--")] if "--" in rest else rest
    wants_help = any(a in ("-h", "--help") for a in head)
    key, page = cmd, cmd
    subs = manual.SUBCOMMANDS.get(cmd)
    if subs:
        at = _first_word(head, cmd)
        first = None if at is None else head[at]
        if first in subs:
            key = page = f"{cmd} {first}"
            rest = rest[:at] + rest[at + 1:]
        elif first is not None and not wants_help:
            raise UsageError(f"{cmd}: unknown subcommand {first!r}", " ".join(
                x for x in (suggest(first, subs), f"Run `{prog} help {cmd}` for the subcommands.") if x))
        elif first is None:
            key = f"{cmd} {manual.DEFAULT_SUBCOMMAND[cmd]}"
    if cmd == "help":
        flags, pos = parse_flags(manual.COMMANDS["help"], rest, prog)
        return _help_invocation(flags, pos, prog)
    if wants_help:
        return Invocation("help", page)
    flags, pos = parse_flags(manual.COMMANDS[key], rest, prog, names)
    if flags.pop("--help", None):  # -h inside a combined short flag (-nh)
        return Invocation("help", page)
    if pos and key != "quarantine restore":
        raise UsageError(f"{key}: unexpected argument {pos[0]!r}", f"Run `{prog} help {key}` for the usage.")
    if key == "quarantine restore" and not pos:
        raise UsageError("quarantine restore: give the name of at least one quarantined skill",
                         f"Run `{prog} quarantine` to list them.")
    return Invocation("run", key, flags, pos)


def _help_invocation(flags, pos, prog):
    if flags.get("--help") or not pos:
        return Invocation("overview")
    if len(pos) > 2:
        raise UsageError(f"help: unexpected argument {pos[2]!r}")
    key = " ".join(pos)
    if key not in manual.COMMANDS:
        top = pos[0]
        if top not in manual.COMMANDS:
            raise UsageError(f"help: no manual for {top!r}", " ".join(
                x for x in (suggest(top, manual.ORDER), f"Run `{prog} help` for the command list.") if x))
        raise UsageError(f"help: {top} has no subcommand {pos[1]!r}", suggest(pos[1], manual.SUBCOMMANDS.get(top, ())))
    return Invocation("help", key)
