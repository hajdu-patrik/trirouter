"""The startup banner. Art and color only on a real terminal: pipes, logs and scripts keep their plain output."""
import os
import shutil
import sys

from . import __version__, platforms as P

ART = (
    "████████╗██████╗ ██╗██████╗  ██████╗ ██╗   ██╗████████╗███████╗██████╗ ",
    "╚══██╔══╝██╔══██╗██║██╔══██╗██╔═══██╗██║   ██║╚══██╔══╝██╔════╝██╔══██╗",
    "   ██║   ██████╔╝██║██████╔╝██║   ██║██║   ██║   ██║   █████╗  ██████╔╝",
    "   ██║   ██╔══██╗██║██╔══██╗██║   ██║██║   ██║   ██║   ██╔══╝  ██╔══██╗",
    "   ██║   ██║  ██║██║██║  ██║╚██████╔╝╚██████╔╝   ██║   ███████╗██║  ██║",
    "   ╚═╝   ╚═╝  ╚═╝╚═╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝    ╚═╝   ╚══════╝╚═╝  ╚═╝",
)
TAGLINE = "one prompt router for Claude Code, Codex and Antigravity"
SPLITS = (20, 46)       # the columns between "tri", "rou" and "ter"
COLORS = (208, 43, 99)  # one per tool, from the 256-color palette: macOS Terminal has no truecolor
DIM, RESET = "\033[2m", "\033[0m"


def _shaded(row):
    a, b = SPLITS
    return "".join(f"\033[38;5;{c}m{part}" for c, part in zip(COLORS, (row[:a], row[a:b], row[b:]))) + RESET


def render(status="", stream=None):
    """The banner for `stream` (default stdout) with an optional status line; '' when it cannot show one."""
    stream = stream or sys.stdout
    info = [line for line in (f"Welcome to trirouter {__version__}: {TAGLINE}", status) if line]
    text = "\n".join(ART + tuple(info))
    if not P.is_terminal(stream) or shutil.get_terminal_size().columns <= len(ART[0]):
        return ""
    try:
        text.encode(stream.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return ""
    if "NO_COLOR" in os.environ or not P.enable_ansi(stream):
        return f"\n{text}\n"
    return "\n" + "\n".join([_shaded(row) for row in ART] + [DIM + line + RESET for line in info]) + "\n"
