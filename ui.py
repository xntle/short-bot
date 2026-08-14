"""Terminal presentation for shorts_bot.

Kept apart from the engine on purpose: shorts_bot.py should stay something you
can run headless, pipe to a file or drive from tests without any of this.

Everything here degrades. If stdout is not a terminal (piped to a log, run
under launchd, captured by pytest) colour is dropped and progress bars print
as plain one-line updates instead of redrawing in place.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time

WIDTH = 52


# ---------------------------------------------------------------- colour


def _colour_ok() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


COLOUR = _colour_ok()


def _c(code: str) -> str:
    return f"\033[{code}m" if COLOUR else ""


DIM = _c("2")
BOLD = _c("1")
RESET = _c("0")
GREEN = _c("32")
RED = _c("31")
YELLOW = _c("33")
BLUE = _c("36")
GREY = _c("90")


def plain(text: str) -> str:
    """Strip escapes, so widths can be measured and logs stay readable."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - len(plain(text)))


def _clip(text: str, width: int) -> str:
    """Trim to width, counting visible characters only."""
    if len(plain(text)) <= width:
        return text
    return plain(text)[:max(1, width - 1)] + "…"


# ---------------------------------------------------------------- panels


def rule() -> None:
    print(f"{GREY}{'─' * WIDTH}{RESET}")


def panel(title: str, lines: list[str]) -> None:
    """A boxed block. Title may be empty for an untitled panel."""
    head = f"╭─ {title} " if title else "╭"
    head += "─" * max(0, WIDTH - len(plain(head)) - 1) + "╮"
    print(f"{GREY}{head}{RESET}")
    for line in lines:
        print(f"{GREY}│{RESET} {_pad(_clip(line, WIDTH - 3), WIDTH - 3)}"
              f"{GREY}│{RESET}")
    print(f"{GREY}╰{'─' * (WIDTH - 2)}╯{RESET}")


def header(made: int, cache_gb: float, state: str = "ready") -> None:
    dot = f"{GREEN}●{RESET}" if state == "ready" else f"{YELLOW}●{RESET}"
    panel("", [
        f"{BOLD}SHORTS BOT{RESET}",
        f"{dot} {state}   {made} made   cache {cache_gb:.1f} GB",
    ])


MENU = [
    ("1", "Paste links", "4", "Change clip"),
    ("2", "Use inbox folder", "5", "Recent activity"),
    ("3", "Open output", "Q", "Quit"),
]


def menu() -> None:
    print()
    for left_key, left, right_key, right in MENU:
        line = (f"  {BLUE}[{left_key}]{RESET} {_pad(left, 19)}"
                f"{BLUE}[{right_key}]{RESET} {right}")
        print(line)
    print()


# ---------------------------------------------------------------- badges


_BADGES = {
    "success": (GREEN, "SUCCESS"),
    "retry": (YELLOW, "RETRY  "),
    "skipped": (GREY, "SKIPPED"),
    "failed": (RED, "FAILED "),
    "parked": (RED, "PARKED "),
}


def badge(kind: str) -> str:
    colour, label = _BADGES.get(kind, (GREY, kind.upper()[:7].ljust(7)))
    return f"{colour}{label}{RESET}"


# ---------------------------------------------------------------- progress


PHASES = ("Download", "Splice", "Export")


class Progress:
    """Three phase bars for one video, redrawn in place.

    Falls back to a single printed line per state change when stdout is not a
    terminal, so piped output stays sane instead of filling with escapes.
    """

    BAR = 20

    def __init__(self, title: str, index: int, total: int) -> None:
        self.title = title
        self.index = index
        self.total = total
        self.pct: dict[str, float] = {p: 0.0 for p in PHASES}
        self.note: dict[str, str] = {p: "" for p in PHASES}
        self.state: dict[str, str] = {p: "waiting" for p in PHASES}
        # monotonic, not time.time(): durations must not be affected by the
        # clock being adjusted mid-render
        self.began: dict[str, float] = {}
        self.secs: dict[str, float] = {}
        self.live = COLOUR and sys.stdout.isatty()
        self.drawn = 0
        self._opened = False

    def elapsed(self, phase: str) -> float:
        """Seconds for a finished phase, or so far for a running one."""
        if phase in self.secs:
            return self.secs[phase]
        if phase in self.began:
            return time.monotonic() - self.began[phase]
        return 0.0

    def total_seconds(self) -> float:
        return sum(self.secs.values())

    def timings(self) -> dict[str, float]:
        return dict(self.secs)

    # -- drawing

    def _bar(self, phase: str) -> str:
        st = self.state[phase]
        pct = self.pct[phase]
        filled = int(self.BAR * min(1.0, max(0.0, pct)))
        if st == "done":
            body = f"{GREEN}{'█' * self.BAR}{RESET}"
            tail = f"{GREEN}done{RESET}"
        elif st == "failed":
            body = f"{RED}{'█' * filled}{RESET}{GREY}{'░' * (self.BAR - filled)}{RESET}"
            tail = f"{RED}failed{RESET}"
        elif st == "running":
            body = (f"{BLUE}{'█' * filled}{RESET}"
                    f"{GREY}{'░' * (self.BAR - filled)}{RESET}")
            tail = f"{BLUE}{pct * 100:3.0f}%{RESET}" if pct else f"{BLUE}...{RESET}"
        else:
            body = f"{GREY}{'░' * self.BAR}{RESET}"
            tail = f"{GREY}waiting{RESET}"
        secs = self.elapsed(phase)
        clock = f"{secs:6.2f}s" if (secs or st in ("done", "failed")) else "       "
        return (f"  {_pad(phase, 10)} {body} {_pad(tail, 8)}"
                f"{GREY}{clock}{self.note[phase]}{RESET}")

    def open(self) -> None:
        if self._opened:
            return
        self._opened = True
        print()
        panel(f"video {self.index} of {self.total}", [self.title[:WIDTH - 5]])
        if not self.live:
            return
        for phase in PHASES:
            print(self._bar(phase))
        self.drawn = len(PHASES)

    def _redraw(self) -> None:
        if not self.live:
            return
        sys.stdout.write(f"\033[{self.drawn}A")
        for phase in PHASES:
            sys.stdout.write("\033[2K" + self._bar(phase) + "\n")
        sys.stdout.flush()

    # -- state changes

    def start(self, phase: str, note: str = "") -> None:
        self.open()
        self.began[phase] = time.monotonic()
        self.state[phase] = "running"
        self.note[phase] = f"  {note}" if note else ""
        if self.live:
            self._redraw()
        else:
            print(f"  {phase}: started{(' ' + note) if note else ''}")

    def update(self, phase: str, fraction: float, note: str = "") -> None:
        self.pct[phase] = fraction
        if note:
            self.note[phase] = f"  {note}"
        if self.live:
            self._redraw()

    def finish(self, phase: str, note: str = "") -> None:
        if phase in self.began:
            self.secs[phase] = time.monotonic() - self.began[phase]
        self.state[phase] = "done"
        self.pct[phase] = 1.0
        if note:
            self.note[phase] = f"  {note}"
        if self.live:
            self._redraw()
        else:
            print(f"  {phase}: done in {self.elapsed(phase):.2f}s"
                  f"{(' · ' + note) if note else ''}")

    def fail(self, phase: str) -> None:
        if phase in self.began:
            self.secs[phase] = time.monotonic() - self.began[phase]
        self.state[phase] = "failed"
        if self.live:
            self._redraw()
        else:
            print(f"  {phase}: failed after {self.elapsed(phase):.2f}s")

    def close(self) -> None:
        if not self._opened:
            return
        parts = " · ".join(f"{p.lower()} {self.secs.get(p, 0.0):.2f}s"
                           for p in PHASES if p in self.secs)
        if parts:
            print(f"  {GREY}{parts}  →  total "
                  f"{BOLD}{self.total_seconds():.2f}s{RESET}")
        if self.live:
            sys.stdout.write("\n")
            sys.stdout.flush()


# ---------------------------------------------------------------- errors


def _first_line(text: str) -> str:
    for line in str(text).splitlines():
        line = line.strip()
        if line:
            return line
    return str(text)


# Ordered: the first pattern that matches wins, so put the specific ones first.
_EXPLANATIONS: list[tuple[str, tuple[str, str]]] = [
    (r"sign in to confirm|not a bot|confirm your age",
     ("YouTube is asking this download to prove it isn't a bot.",
      "Usually clears by itself within a few hours.")),
    (r"429|too many requests|rate.?limit",
     ("YouTube is asking for a slower pace.",
      "Too many downloads in a short time. It eases off on its own.")),
    (r"private video|video unavailable|has been removed|not available",
     ("That video isn't public any more, so it can't be downloaded.",
      "Check the link opens in your browser while signed out.")),
    (r"members-only|join this channel",
     ("That video is for channel members only.",
      "There's no way around this one. Try a different video.")),
    (r"no space left|disk full",
     ("Your disk is full.",
      "Free up space, then paste the link again.")),
    (r"could not read that link|is not a valid url|unsupported url",
     ("That link didn't look like a video page.",
      "Copy the address straight from the video and try again.")),
    (r"too short to split",
     ("That video is too short to cut in half.",
      "Anything under about a second has nowhere to put the interruption.")),
    (r"yt-dlp is not installed",
     ("The downloader is missing.",
      "Close this window and run the setup again.")),
    (r"no interruption clip",
     ("Your interruption clip is missing.",
      "Put it in the shorts_bot_data folder, named interruption.mp4.")),
    (r"cannot write into|permission denied|read-only",
     ("The bot can't write into its own folder.",
      "Check the Shorts Bot folder hasn't been moved or locked.")),
    (r"network|timed out|connection reset|temporary failure",
     ("The connection dropped partway through.",
      "Usually just the internet hiccuping. It will try again.")),
]


def explain(exc: BaseException | str) -> tuple[str, str]:
    """Turn a raw failure into something worth reading.

    Returns (what happened, what to do). Never leaks a command line: the raw
    text still goes to the log file for anyone who wants it.
    """
    raw = _first_line(exc if isinstance(exc, str) else str(exc)).lower()
    for pattern, message in _EXPLANATIONS:
        if re.search(pattern, raw):
            return message
    return ("Something went wrong while making this video.",
            "The details are in shorts_bot.log if you want to send them on.")


def show_error(exc: BaseException | str, retry_at: str | None = None,
               parked: bool = False) -> None:
    what, advice = explain(exc)
    print(f"  {RED}✕{RESET}  {what}")
    print(f"     {GREY}{advice}{RESET}")
    if parked:
        print(f"     {GREY}Tried several times now, so it's been set aside. "
              f"Paste it again to force another go.{RESET}")
    elif retry_at:
        print(f"     {GREY}Retrying automatically at {retry_at}. "
              f"Nothing for you to do.{RESET}")


# ---------------------------------------------------------------- summary


def summary(rows: list[tuple[str, str, str]]) -> None:
    """rows: (badge kind, title, trailing note)"""
    if not rows:
        return
    print()
    head = "╭─ batch finished " + "─" * max(0, WIDTH - 19) + "╮"
    print(f"{GREY}{head}{RESET}")
    print(f"{GREY}│{RESET}{' ' * (WIDTH - 2)}{GREY}│{RESET}")
    for kind, title, note in rows:
        # right-align the note and give the title whatever is left, otherwise
        # a long title runs straight into it with no gap
        inner = WIDTH - 3
        prefix = f"  {badge(kind)}  "
        room = inner - len(plain(prefix)) - len(note) - 3
        shown = title if len(title) <= room else title[:max(1, room - 1)] + "…"
        text = f"{prefix}{_pad(shown, room)}  {GREY}{note}{RESET}"
        print(f"{GREY}│{RESET} {_pad(text, inner)}{GREY}│{RESET}")
    print(f"{GREY}│{RESET}{' ' * (WIDTH - 2)}{GREY}│{RESET}")

    counts: dict[str, int] = {}
    for kind, _t, _n in rows:
        counts[kind] = counts.get(kind, 0) + 1
    bits = []
    for kind, word in (("success", "made"), ("retry", "retrying"),
                       ("skipped", "skipped"), ("failed", "failed"),
                       ("parked", "set aside")):
        if counts.get(kind):
            bits.append(f"{counts[kind]} {word}")
    line = "  " + " · ".join(bits)
    print(f"{GREY}│{RESET} {_pad(line, WIDTH - 3)}{GREY}│{RESET}")
    print(f"{GREY}╰{'─' * (WIDTH - 2)}╯{RESET}")


def profile(samples: list[dict[str, float]]) -> None:
    """Where the time went across a batch.

    samples: one dict per completed video, phase name -> seconds.
    """
    if not samples:
        return
    grand = sum(sum(s.values()) for s in samples)
    if grand <= 0:
        return

    print()
    head = "╭─ timings " + "─" * max(0, WIDTH - 12) + "╮"
    print(f"{GREY}{head}{RESET}")
    print(f"{GREY}│{RESET}{' ' * (WIDTH - 2)}{GREY}│{RESET}")

    for phase in PHASES:
        vals = [s[phase] for s in samples if phase in s]
        if not vals:
            continue
        total = sum(vals)
        share = total / grand
        bar = int(20 * share)
        line = (f"  {_pad(phase, 10)} {BLUE}{'█' * bar}{RESET}"
                f"{GREY}{'░' * (20 - bar)}{RESET} "
                f"{total:7.2f}s  {share * 100:3.0f}%")
        print(f"{GREY}│{RESET} {_pad(line, WIDTH - 3)}{GREY}│{RESET}")

    print(f"{GREY}│{RESET}{' ' * (WIDTH - 2)}{GREY}│{RESET}")
    n = len(samples)
    avg = grand / n
    for label, value in (("total", f"{grand:.2f}s over {n} video(s)"),
                         ("average", f"{avg:.2f}s each")):
        line = f"  {_pad(label, 10)} {value}"
        print(f"{GREY}│{RESET} {_pad(line, WIDTH - 3)}{GREY}│{RESET}")
    print(f"{GREY}╰{'─' * (WIDTH - 2)}╯{RESET}")


def clear() -> None:
    """Reset the screen without spawning a shell.

    os.system("clear") launched /bin/sh and depended on PATH resolving
    `clear`. These two escapes do the same job with neither.
    """
    if COLOUR and sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def term_width() -> int:
    return shutil.get_terminal_size((80, 24)).columns
