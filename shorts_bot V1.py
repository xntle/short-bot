#!/usr/bin/env python3
"""
shorts_bot - download short videos, splice an interruption clip into the middle, export.

Run once:      python3 shorts_bot.py
Run forever:   python3 shorts_bot.py --watch

Folder layout (created on first run, next to this script unless --root is given):

    shorts_bot_data/
        interruption.mp4   <- YOUR clip. put it here. required.
        urls.txt           <- one YouTube/other URL per line. lines starting with # ignored.
        inbox/             <- drop local video files here instead of using urls.txt
        downloads/         <- cache of fetched sources
        output/            <- finished videos land here
        processed/         <- local inbox files moved here after processing
        state.json         <- remembers what's already been done

Only process content you own or are licensed to reuse.
"""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import random
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import ui
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor

# ---------------------------------------------------------------- config

WIDTH = 1080
HEIGHT = 1920
FPS = 30
AUDIO_RATE = 44100
# Where the interruption gets spliced in, as a fraction of the source. Late by
# design: the cut lands in the last quarter, so the viewer is already invested
# before being interrupted. Randomised within the range so a batch doesn't come
# out with the interruption in the identical spot every time. Pass
# --split-at 0.5 for an exact midpoint instead.
# The tests and --help read this, so changing it here changes them too.
SPLIT_RANGE: tuple[float, float] = (0.75, 0.90)
SPLIT_AT: float | None = None   # None = random within SPLIT_RANGE
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
# Interruption clips are versioned by number (1.mov, 2.mp4, ...) and the
# highest number is the one used. This extension is only for what the bot
# suggests in messages; any VIDEO_EXTS extension is accepted.
DEFAULT_CLIP_EXT = ".mp4"
POLL_SECONDS = 60       # how often --watch rescans

# Retry policy. Without this, a link that fails is retried on every single
# watch pass forever, which against a host returning 429 is indistinguishable
# from hammering it. Failures back off exponentially and eventually park.
RETRY_BASE_SECONDS = 300          # 5 min after the first failure
RETRY_MAX_SECONDS = 6 * 3600      # never wait longer than 6 hours
RETRY_GIVE_UP_AFTER = 6           # then stop trying and say so, once

# Download cache. Unbounded, this reaches tens of GB within a year of daily use.
CACHE_MAX_AGE_DAYS = 30
CACHE_MAX_GB = 20.0

# Log file. Rotated so it cannot grow without limit either.
LOG_MAX_BYTES = 2_000_000
LOG_BACKUPS = 3

# Encoding, used only on the fallback path. The fast path re-encodes nothing.
X264_PRESET = "superfast"
X264_CRF = 18
HW_BITRATE = "12M"          # hardware encoders take a bitrate, not a CRF

# Stream-copy splicing only works if the cut lands exactly on a keyframe, and
# only for codecs the mp4 container and concat demuxer handle cleanly.
COPY_SAFE_VCODECS = {"h264"}
COPY_SAFE_ACODECS = {"aac"}
PREFETCH = True             # download the next link while the current encodes
# Seconds between downloads, so a batch stays polite. Raised from 1.5 after
# YouTube began answering batches with 429 on the very first request of each
# video: 1.5s was fast enough to look automated from an IP it had already
# noticed. Jitter is added on top so a batch doesn't tick like a metronome.
MIN_DOWNLOAD_GAP = 4.0
DOWNLOAD_JITTER = 2.0       # extra random 0..n seconds on each gap

# ---------------------------------------------------------------- helpers


def run(cmd: Sequence[str | Path], **kw: Any) -> str:
    """Run a command, raise with readable output on failure.

    stdin is always detached. ffmpeg reads stdin for its interactive keys and
    will happily eat whatever the user has queued up in the terminal, which
    silently swallowed pasted links that had not been consumed yet.
    """
    kw.setdefault("stdin", subprocess.DEVNULL)
    kw.setdefault("check", False)
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        tail = "\n".join((p.stderr or p.stdout).strip().splitlines()[-15:])
        raise RuntimeError(f"command failed: {' '.join(map(str, cmd))}\n{tail}")
    return str(p.stdout)


def run_progress(cmd: Sequence[str | Path],
                 on_line: Callable[[str], None],
                 **kw: Any) -> str:
    """Like run(), but hands each output line over as it arrives.

    Needed because run() buffers everything until the process exits, which is
    useless for a progress bar on a job that takes a minute.
    """
    kw.setdefault("stdin", subprocess.DEVNULL)
    collected: list[str] = []
    proc = subprocess.Popen(
        [str(c) for c in cmd], stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1, **kw)
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            collected.append(line)
            try:
                on_line(line.rstrip("\n"))
            except Exception:  # noqa: BLE001 - a broken bar must not kill the job
                pass
    finally:
        proc.stdout.close()
        proc.wait()

    if proc.returncode != 0:
        tail = "\n".join("".join(collected).strip().splitlines()[-15:])
        raise RuntimeError(f"command failed: {' '.join(map(str, cmd))}\n{tail}")
    return "".join(collected)


_last_download = 0.0
_download_gate = threading.Lock()


def throttle_downloads(gap: float = MIN_DOWNLOAD_GAP) -> None:
    """Hold a minimum interval between download starts.

    The backoff logic only reacts after a refusal. This is the proactive half:
    a batch of forty links would otherwise open forty requests back to back as
    fast as the encoder finishes, which is what earns a rate limit in the
    first place.
    """
    global _last_download
    with _download_gate:
        wait = gap - (time.monotonic() - _last_download)
        if DOWNLOAD_JITTER:
            wait += random.uniform(0, DOWNLOAD_JITTER)
        if wait > 0:
            time.sleep(wait)
        _last_download = time.monotonic()


_YT_PCT = re.compile(r"\[download\]\s+([\d.]+)%")
_FF_TIME = re.compile(r"out_time_ms=(\d+)")


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def duration_of(path: str | Path) -> float:
    out = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    return float(out.strip())


def slugify(text: object, limit: int = 60) -> str:
    if text is None:
        return "video"
    text = re.sub(r"[^\w\s-]", "", str(text), flags=re.UNICODE).strip()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:limit].strip("-") or "video"


# ---------------------------------------------------------------- logging

# Two destinations on purpose. The terminal stays friendly for a person
# watching it work; the file is what you grep at 2am when something broke
# three days ago, so it carries a full date, a severity and the function name.

_logger = logging.getLogger("shorts_bot")
_log_configured = False


def setup_logging(root: Path | None = None, verbose: bool = False) -> Path | None:
    """Attach a console handler and a rotating file handler. Idempotent."""
    global _log_configured
    if _log_configured:
        return None
    _log_configured = True

    _logger.setLevel(logging.DEBUG)
    _logger.propagate = False

    class ConsoleFormatter(logging.Formatter):
        """Same message, but never the traceback.

        A stack trace on screen tells a non-technical user nothing and buries
        the one line that matters. The full trace still goes to the file.
        """

        def format(self, record: logging.LogRecord) -> str:
            saved_exc, saved_text = record.exc_info, record.exc_text
            record.exc_info, record.exc_text = None, None
            try:
                return super().format(record)
            finally:
                record.exc_info, record.exc_text = saved_exc, saved_text

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(ConsoleFormatter("[%(asctime)s] %(message)s",
                                          datefmt="%H:%M:%S"))
    _logger.addHandler(console)

    if root is None:
        return None

    log_path = root / "shorts_bot.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_path, maxBytes=LOG_MAX_BYTES,
                                 backupCount=LOG_BACKUPS, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(funcName)-16s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        _logger.addHandler(fh)
    except OSError as e:
        # a read-only folder must not stop the bot working
        _logger.warning("could not open log file (%s), console only", e)
        return None
    return log_path


def set_console_level(level: int) -> None:
    """Quieten the screen without touching the log file.

    The UI draws its own panels and redraws progress bars in place, so stray
    log lines would tear straight through them. The file handler keeps
    recording everything at DEBUG regardless.
    """
    for h in _logger.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(
                h, RotatingFileHandler):
            h.setLevel(level)


def log(msg: str) -> None:
    """Normal progress, shown to the user."""
    if not _log_configured:
        setup_logging()
    _logger.info(msg, stacklevel=2)


def warn(msg: str) -> None:
    if not _log_configured:
        setup_logging()
    _logger.warning(msg, stacklevel=2)


def error(msg: str, exc: BaseException | None = None) -> None:
    """A failure. The file handler also records the traceback."""
    if not _log_configured:
        setup_logging()
    _logger.error(msg, exc_info=exc, stacklevel=2)


def debug(msg: str) -> None:
    if not _log_configured:
        setup_logging()
    _logger.debug(msg, stacklevel=2)


# ---------------------------------------------------------------- state


_STATE_LOCK = threading.Lock()

# watch mode re-runs forever, so a persistent condition must not reprint
# the same warning every single pass
_warned_no_interruption = False


class State:
    """Remembers what has already been processed.

    Written to survive two runs at once, which is a real situation here: watch
    mode and the interactive prompt can both be open. The naive version held
    the list in memory and wrote it whole, so whichever run saved last erased
    the other's work, and a reader could catch a half-written file.

    So: every write re-reads the file under an exclusive lock, merges, and
    lands atomically via os.replace.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data = self._read()
        self._seen = set(self.data["done"])

    def _read(self) -> dict[str, Any]:
        """Load the file, coercing anything unexpected into a usable shape."""
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            raw = None                      # first run, nothing to say
        except (OSError, ValueError) as e:
            # Do not fail silently here. Resetting the memory file means work
            # may be redone, and the user deserves to know why.
            log(f"!! {self.path.name} was unreadable ({e}), starting a fresh one")
            raw = None
        if not isinstance(raw, dict):
            raw = {}
        done = raw.get("done")
        if not isinstance(done, list):
            done = []
        raw["done"] = [d for d in done if isinstance(d, str)]
        result: dict[str, Any] = raw
        return result

    def _write_atomic(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_name(self.path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)      # atomic: readers see old or new, never half

    def is_done(self, key: str) -> bool:
        return key in self._seen

    # ---- failure tracking -------------------------------------------------
    # A link that fails is not "done", so without this it would be retried on
    # every watch pass forever. Against a host answering 429 that is just
    # hammering it, and is how an IP gets blocked.

    def _failures(self) -> dict[str, Any]:
        f = self.data.get("failures")
        if not isinstance(f, dict):
            f = {}
            self.data["failures"] = f
        return f

    def retry_wait(self, key: str) -> float:
        """Seconds still to wait before this key may be attempted again.

        0 means go ahead. A negative sentinel means give up entirely.
        """
        rec = self._failures().get(key)
        if not isinstance(rec, dict):
            return 0.0
        count = int(rec.get("count", 0))
        if count >= RETRY_GIVE_UP_AFTER:
            return -1.0
        delay = min(RETRY_BASE_SECONDS * (2 ** max(0, count - 1)),
                    RETRY_MAX_SECONDS)
        elapsed = time.time() - float(rec.get("last", 0))
        return float(max(0.0, delay - elapsed))

    def record_failure(self, key: str, reason: str) -> int:
        with _STATE_LOCK, open(self._lock_path(), "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                latest = self._read()
                fails = latest.setdefault("failures", {})
                if not isinstance(fails, dict):
                    fails = {}
                    latest["failures"] = fails
                rec = fails.get(key) if isinstance(fails.get(key), dict) else {}
                rec["count"] = int(rec.get("count", 0)) + 1
                rec["last"] = time.time()
                rec["reason"] = reason[:300]
                fails[key] = rec
                self._write_atomic(latest)
                self.data = latest
                return int(rec["count"])
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def clear_failure(self, key: str) -> None:
        if key not in self._failures():
            return
        with _STATE_LOCK, open(self._lock_path(), "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                latest = self._read()
                fails = latest.get("failures")
                if isinstance(fails, dict) and key in fails:
                    del fails[key]
                    self._write_atomic(latest)
                    self.data = latest
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def mark(self, key: str) -> None:
        if key in self._seen:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_path()

        # _STATE_LOCK guards threads in this process, flock guards other
        # processes. Both are needed: flock is per-open-file-description.
        with _STATE_LOCK, open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                latest = self._read()   # someone else may have written
                merged = list(latest["done"])
                known = set(merged)
                for k in self.data["done"] + [key]:
                    if k not in known:
                        known.add(k)
                        merged.append(k)
                latest["done"] = merged
                self._write_atomic(latest)
                self.data = latest
                self._seen = known
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


def prune_cache(downloads: Path,
                max_age_days: float = CACHE_MAX_AGE_DAYS,
                max_gb: float = CACHE_MAX_GB) -> int:
    """Keep the download cache bounded by age, then by total size.

    Left alone this folder grows forever. At ~13MB per short and 50 a day it
    passes 200GB inside a year, and the user is the one who runs out of disk.
    """
    try:
        files = [p for p in downloads.iterdir()
                 if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    except OSError:
        return 0

    removed = 0
    cutoff = time.time() - max_age_days * 86400

    def drop(p: Path) -> None:
        nonlocal removed
        try:
            p.unlink()
            removed += 1
            debug(f"pruned cached download {p.name}")
        except OSError as e:
            debug(f"could not prune {p.name}: {e}")

    survivors = []
    for p in files:
        try:
            stat = p.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            drop(p)
        else:
            survivors.append((stat.st_mtime, stat.st_size, p))

    # then trim the oldest until the folder fits under the size cap
    budget = max_gb * 1024 ** 3
    total = sum(size for _, size, _ in survivors)
    for _, size, p in sorted(survivors):
        if total <= budget:
            break
        drop(p)
        total -= size

    if removed:
        log(f"tidied the download cache, removed {removed} old file(s)")
    return removed


def reserve_output_path(out_dir: str | Path, stem: str,
                        suffix: str = ".mp4") -> Path:
    """Claim a free output filename, atomically.

    Checking `exists()` and then writing is a race: two runs splicing the same
    source both see the name free and one silently overwrites the other. Creating
    the file with O_EXCL means exactly one caller can win each name.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    while True:
        name = f"{stem}-spliced{suffix}" if n == 0 else f"{stem}-spliced-{n}{suffix}"
        candidate = out_dir / name
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return candidate
        except FileExistsError:
            n += 1
        except OSError as e:
            raise RuntimeError(f"cannot write into {out_dir}: {e}") from e


def highest_output_number(out_dir: str | Path, suffix: str = ".mp4") -> int:
    """Largest number already used in the output folder, or 0 if none.

    Only whole-number stems count, so anything else sitting in the folder is
    ignored rather than being misread as a number.
    """
    try:
        entries = list(Path(out_dir).iterdir())
    except OSError:
        return 0
    numbers = [int(p.stem) for p in entries
               if p.is_file() and p.suffix.lower() == suffix and p.stem.isdigit()]
    return max(numbers, default=0)


def reserve_numbered_output(out_dir: str | Path, suffix: str = ".mp4") -> Path:
    """Claim the next free number in the output folder: 1.mp4, 2.mp4, ...

    Numbering continues from the highest already there rather than restarting,
    so a later run does not overwrite an earlier one's work. Gaps left by
    deleting files are not refilled - going back and reusing 3.mp4 after it was
    deleted would make "3" mean two different videos across a set of uploads.

    Same O_EXCL reservation as reserve_output_path: two runs racing for the
    same number means one of them loses the create and simply takes the next.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = highest_output_number(out_dir, suffix) + 1
    while True:
        candidate = out_dir / f"{n}{suffix}"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return candidate
        except FileExistsError:
            n += 1
        except OSError as e:
            raise RuntimeError(f"cannot write into {out_dir}: {e}") from e


# ---------------------------------------------------------------- ffmpeg work

# Scale to fit inside the target frame, pad the rest with black, force a
# consistent fps / pixel format / audio layout. Doing this to *every* segment is
# what makes the concat reliable no matter what the sources look like.
VF = (
    f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
    f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
    f"setsar=1,fps={FPS},format=yuv420p"
)

ENCODE = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE), "-ac", "2",
]


# ------------------------------------------------- capability detection


_encoder_cache: str | None = None


def _encoder_usable(name: str) -> bool:
    """Presence in `-encoders` does not mean it works. Actually encode a frame."""
    try:
        run(["ffmpeg", "-nostdin", "-v", "error",
             "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
             "-c:v", name, "-frames:v", "1", "-f", "null", "-"])
    except RuntimeError:
        return False
    return True


def best_encoder() -> str:
    """Hardware encoder if one genuinely works here, else libx264.

    Only matters on the fallback path; the fast path encodes nothing.
    """
    global _encoder_cache
    if _encoder_cache:
        return _encoder_cache

    candidates = (["h264_videotoolbox"] if sys.platform == "darwin"
                  else ["h264_nvenc", "h264_qsv", "h264_vaapi"])
    try:
        listed = run(["ffmpeg", "-nostdin", "-v", "error", "-hide_banner", "-encoders"])
    except RuntimeError:
        listed = ""

    for name in candidates:
        if name in listed and _encoder_usable(name):
            debug(f"hardware encoder available: {name}")
            _encoder_cache = name
            return name

    debug("no usable hardware encoder, using libx264")
    _encoder_cache = "libx264"
    return _encoder_cache


def video_encode_args(encoder: str | None = None) -> list[str]:
    enc = encoder or best_encoder()
    if enc == "libx264":
        return ["-c:v", "libx264", "-preset", X264_PRESET,
                "-crf", str(X264_CRF), "-pix_fmt", "yuv420p"]
    # hardware encoders have no CRF equivalent; a generous bitrate keeps
    # quality close enough that the difference is not visible on a short
    return ["-c:v", enc, "-b:v", HW_BITRATE, "-pix_fmt", "yuv420p"]


# ------------------------------------------------- stream inspection


def _timescale(time_base: str | None) -> int | None:
    """'1/15360' -> 15360.

    This is the one that bites. The concat demuxer will happily join clips
    whose video timescales differ and produce a file whose frames are correct
    but whose timestamps are stretched: 690 frames reported as 88 seconds
    instead of 23. So a prepared clip must adopt the source's timescale.
    """
    if not time_base or "/" not in time_base:
        return None
    try:
        return int(time_base.split("/", 1)[1])
    except ValueError:
        return None


def probe_spec(path: str | Path) -> dict[str, Any]:
    """Everything that has to match for a stream copy to be legal."""
    out = run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)])
    data = json.loads(out)
    spec: dict[str, Any] = {
        "vcodec": None, "width": None, "height": None, "fps": None,
        "pix_fmt": None, "acodec": None, "sample_rate": None, "channels": None,
        "timescale": None,
    }
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and spec["vcodec"] is None:
            spec.update(vcodec=st.get("codec_name"),
                        width=st.get("width"), height=st.get("height"),
                        pix_fmt=st.get("pix_fmt"),
                        fps=st.get("r_frame_rate"),
                        timescale=_timescale(st.get("time_base")))
        elif st.get("codec_type") == "audio" and spec["acodec"] is None:
            spec.update(acodec=st.get("codec_name"),
                        sample_rate=st.get("sample_rate"),
                        channels=st.get("channels"))
    return spec


def spec_signature(spec: dict[str, Any]) -> str:
    """Short stable id for a stream layout, used to cache prepared clips."""
    key = "|".join(str(spec.get(k)) for k in
                   ("vcodec", "width", "height", "fps", "pix_fmt",
                    "acodec", "sample_rate", "channels", "timescale"))
    return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:12]


def copy_is_viable(spec: dict[str, Any]) -> bool:
    return (spec.get("vcodec") in COPY_SAFE_VCODECS
            and spec.get("acodec") in COPY_SAFE_ACODECS
            and bool(spec.get("width")) and bool(spec.get("height"))
            and bool(spec.get("timescale")))


def keyframe_times(path: str | Path) -> list[float]:
    """Keyframe timestamps, read from packet flags so nothing is decoded."""
    try:
        out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "packet=pts_time,flags",
                   "-of", "csv=p=0", str(path)])
    except RuntimeError:
        return []
    times = []
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                times.append(float(parts[0]))
            except ValueError:
                continue
    return sorted(times)


def choose_cut(total: float, keyframes: list[float],
               split_at: float | None = None) -> tuple[float, bool]:
    """Where to cut, and whether that point is keyframe-exact.

    A copy cut has to land on a keyframe or ffmpeg silently rewinds to the
    previous one and emits negative timestamps. So the random point inside
    SPLIT_RANGE is snapped to the nearest keyframe in that window. The
    placement stays varied; it just lands on legal boundaries.
    """
    target = total * pick_split(split_at)
    if not keyframes:
        return target, False

    lo, hi = total * SPLIT_RANGE[0], total * SPLIT_RANGE[1]
    inside = [k for k in keyframes if lo <= k <= hi and k > 0.05]
    pool = inside or [k for k in keyframes if 0.05 < k < total - 0.05]
    if not pool:
        return target, False

    best = min(pool, key=lambda k: abs(k - target))
    return best, True


def prepare_interruption(interruption: Path, spec: dict[str, Any],
                         cache_dir: Path) -> Path | None:
    """Render the interruption once per distinct source layout, then reuse it.

    This is the single biggest saving in a batch: without it the interruption
    is re-encoded from scratch for every video processed.

    The cache name carries which clip it came from as well as the source
    layout. Keying on layout alone was unsafe once clips became numbered:
    swapping 1.mov for a 2.mov whose mtime happened to be older (copying a
    file can preserve its timestamp) would silently reuse clip 1's render.
    """
    sig = spec_signature(spec)
    cache_dir.mkdir(parents=True, exist_ok=True)
    who = re.sub(r"[^A-Za-z0-9]+", "-", interruption.name).strip("-") or "clip"
    dst = cache_dir / f"{who}-{sig}.mp4"

    try:
        if dst.exists() and dst.stat().st_mtime >= interruption.stat().st_mtime:
            debug(f"reusing prepared interruption {dst.name}")
            return dst
    except OSError:
        pass

    w, h, fps = spec["width"], spec["height"], spec["fps"]
    rate = spec.get("sample_rate") or AUDIO_RATE
    ch = spec.get("channels") or 2

    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
          f"setsar=1,fps={fps},format={spec.get('pix_fmt') or 'yuv420p'}")

    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(interruption)]
    if not has_audio(interruption):
        cmd += ["-f", "lavfi", "-i",
                f"anullsrc=channel_layout=stereo:sample_rate={rate}", "-shortest",
                "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    cmd += ["-vf", vf,
            # must match the source's GOP style so the join is clean
            "-c:v", "libx264", "-preset", X264_PRESET, "-crf", str(X264_CRF),
            "-pix_fmt", spec.get("pix_fmt") or "yuv420p",
            "-c:a", "aac", "-ar", str(rate), "-ac", str(ch)]
    # Must match the source exactly or concat stretches the timestamps.
    if spec.get("timescale"):
        cmd += ["-video_track_timescale", str(spec["timescale"])]
    cmd += [str(dst)]
    try:
        log("    preparing interruption for this format (one time only)")
        run(cmd)
    except RuntimeError as e:
        debug(f"could not prepare interruption for copy path: {e}")
        dst.unlink(missing_ok=True)
        return None
    return dst


def has_audio(path: str | Path) -> bool:
    """True if the file carries at least one audio stream."""
    out = run([
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        str(path),
    ])
    return bool(out.strip())


def normalize(src: str | Path, dst: str | Path,
              start: float | None = None, end: float | None = None,
              on_progress: Callable[[float], None] | None = None,
              expect: float = 0.0) -> None:
    """Re-encode a (portion of a) file to the canonical format.

    If the source has no audio at all, a silent track is generated, because
    concat refuses to join a video-only clip to clips that have sound.

    The source's own audio is passed through untouched. An earlier version
    mixed it against a silent track to cover both cases in one command, which
    quietly cost 3dB of volume and put a two-second fade-in on the front of
    every segment. Never mix here.
    """
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-to", f"{end:.3f}"]
    cmd += ["-i", str(src)]

    if has_audio(src):
        cmd += ["-vf", VF, "-map", "0:v:0", "-map", "0:a:0"]
    else:
        cmd += [
            "-f", "lavfi", "-i",
            f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}",
            "-shortest",
            "-vf", VF,
            "-map", "0:v:0", "-map", "1:a:0",
        ]

    cmd += ENCODE + [str(dst)]
    if on_progress is None or expect <= 0:
        run(cmd)
        return

    def relay(line: str) -> None:
        m = _FF_TIME.search(line)
        if m:
            on_progress(min(1.0, int(m.group(1)) / 1_000_000.0 / expect))

    run_progress([*cmd[:1], "-progress", "pipe:1", "-nostats", *cmd[1:]], relay)


def pick_split(split_at: float | None = None) -> float:
    """Fraction of the way through the source to make the cut."""
    if split_at is None:
        return random.uniform(*SPLIT_RANGE)  # noqa: S311 - variety, not crypto
    return split_at


def concat_files(parts: Sequence[Path], out_path: Path, work: Path,
                 reencode: bool = False) -> None:
    """Join parts via the concat demuxer. Written once, used by both paths."""
    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts))

    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error",
           "-f", "concat", "-safe", "0", "-i", str(listing.resolve())]
    cmd += (["-c", "copy"] if not reencode
            else [*video_encode_args(), "-c:a", "aac", "-b:a", "192k"])
    cmd += ["-movflags", "+faststart", str(Path(out_path).resolve())]

    # cwd=work so the bare filenames in concat.txt resolve; everything handed
    # to ffmpeg must therefore be absolute.
    run(cmd, cwd=str(work.resolve()))


def copy_splice(source: Path, prepared_mid: Path, out_path: Path,
                work: Path, cut: float,
                on_export: Callable[[], None] | None = None) -> None:
    """Cut, insert and join without re-encoding a single frame of the source.

    Only legal because `cut` is a real keyframe timestamp and `prepared_mid`
    was rendered to the source's exact stream layout.
    """
    part_a = work / "a.mp4"
    part_b = work / "b.mp4"
    mid_local = work / "mid.mp4"
    shutil.copy2(prepared_mid, mid_local)

    run(["ffmpeg", "-nostdin", "-y", "-v", "error",
         "-t", f"{cut:.6f}", "-i", str(source),
         "-c", "copy", "-avoid_negative_ts", "make_zero", str(part_a)])
    run(["ffmpeg", "-nostdin", "-y", "-v", "error",
         "-ss", f"{cut:.6f}", "-i", str(source),
         "-c", "copy", "-avoid_negative_ts", "make_zero", str(part_b)])

    if on_export:
        on_export()
    concat_files([part_a, mid_local, part_b], out_path, work)


def reencode_splice(source: Path, interruption: Path, out_path: Path,
                    work: Path, cut: float, total: float,
                    on_progress: Callable[[float], None] | None = None,
                    on_export: Callable[[], None] | None = None) -> None:
    """The safe path: normalise all three segments, then join."""
    part_a = work / "a.mp4"
    part_b = work / "b.mp4"
    mid = work / "mid.mp4"

    # one bar across three encodes: weight each by how much footage it covers
    tail = total - cut
    mid_len = 2.0
    whole = max(0.001, cut + mid_len + tail)

    def stage(offset: float, span: float) -> Callable[[float], None] | None:
        if on_progress is None:
            return None
        return lambda f: on_progress((offset + f * span) / whole)

    log(f"    re-encoding part 1 (0 - {cut:.1f}s)")
    normalize(source, part_a, start=0, end=cut,
              on_progress=stage(0.0, cut), expect=cut)
    log("    re-encoding interruption")
    normalize(interruption, mid,
              on_progress=stage(cut, mid_len), expect=mid_len)
    log(f"    re-encoding part 2 ({cut:.1f} - {total:.1f}s)")
    normalize(source, part_b, start=cut,
              on_progress=stage(cut + mid_len, tail), expect=tail)

    if on_export:
        on_export()
    concat_files([part_a, mid, part_b], out_path, work)


def splice(source: Path, interruption: Path, out_path: Path, work: Path,
           split_at: float | None = SPLIT_AT,
           allow_copy: bool = True,
           on_progress: Callable[[float], None] | None = None,
           on_method: Callable[[str], None] | None = None,
           on_export: Callable[[], None] | None = None) -> None:
    """source -> [first part][interruption][second part] -> out_path

    Two strategies. The fast one copies the source's video and audio packets
    untouched, which is both far quicker and lossless, but demands that the
    cut sit on a keyframe and that every segment share an identical stream
    layout. When any of that does not hold, it falls back to re-encoding
    everything, which always works.
    """
    total = duration_of(source)
    if total < 0.8:
        raise RuntimeError(f"source too short to split ({total:.2f}s)")

    spec = {}
    cut = total * pick_split(split_at)
    exact = False

    if allow_copy:
        try:
            spec = probe_spec(source)
            if copy_is_viable(spec):
                cut, exact = choose_cut(total, keyframe_times(source), split_at)
        except (RuntimeError, ValueError, json.JSONDecodeError) as e:
            debug(f"could not inspect source, using the safe path: {e}")

    if cut < 0.3 or (total - cut) < 0.3:
        raise RuntimeError(f"source too short to split ({total:.2f}s)")

    log(f"    cutting at {cut / total * 100:.0f}% ({cut:.1f}s of {total:.1f}s)")

    if allow_copy and exact and copy_is_viable(spec):
        cache = interruption.parent / ".prepared"
        prepared = prepare_interruption(interruption, spec, cache)
        if prepared is not None:
            try:
                log("    joining without re-encoding (fast path)")
                if on_method:
                    on_method("fast path")
                copy_splice(source, prepared, out_path, work, cut, on_export)
                if on_progress:
                    on_progress(1.0)
                return
            except RuntimeError as e:
                # any copy failure is recoverable: throw the partial output
                # away and do it the slow, certain way
                debug(f"fast path failed ({e}), falling back to re-encode")
                out_path.unlink(missing_ok=True)

    if on_method:
        on_method("re-encoding")
    reencode_splice(source, interruption, out_path, work, cut, total,
                    on_progress, on_export)


# ---------------------------------------------------------------- sources


_YT_PATTERNS = (
    re.compile(r"youtube\.com/shorts/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/watch\?(?:.*&)?v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/embed/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
)


_SAFE_URL = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


def is_safe_url(candidate: object) -> bool:
    """Reject anything that isn't plainly an http(s) URL.

    This is a security boundary, not tidiness. Every URL ends up as an argv
    entry for yt-dlp, and yt-dlp has flags that execute shell commands
    (--exec among them). A line reading `--exec=...` in urls.txt was being
    handed straight over as a flag. Requiring an http(s) scheme means a
    string can never be mistaken for an option.
    """
    if not isinstance(candidate, str):
        return False
    text = candidate.strip()
    if not text or text.startswith("-"):
        return False
    return bool(_SAFE_URL.match(text))


def youtube_id(url: str) -> str | None:
    """Read the video id straight out of the URL, no network call needed."""
    for pat in _YT_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------- cookies

# YouTube now answers a large share of anonymous requests with 429 and then
# "Sign in to confirm you're not a bot". The remedy it names is cookies from a
# browser that is already signed in.
#
# Cookies are deliberately NOT sent by default. A plain request is cheaper, it
# does not touch the browser's cookie store, and it keeps a real account out of
# this entirely for as long as that keeps working. They are brought in only
# after a refusal. Once a browser works, it is remembered for the rest of the
# run so the batch does not burn one doomed attempt per video.

COOKIES_FROM_BROWSER: str | None = None   # forced choice, from --cookies-from
COOKIES_ENABLED = True                    # cleared by --no-cookies

# Ordered by how reliably yt-dlp can actually READ the store, which is not the
# same as how popular the browser is. Firefox keeps cookies.sqlite unencrypted.
# Chromium browsers encrypt them, and recent macOS/Windows builds use
# app-bound encryption that frequently refuses to open at all. Safari needs
# Full Disk Access granted to the terminal. So: Firefox first, Safari last.
_BROWSER_PROFILES: dict[str, tuple[str, ...]] = {
    "firefox": ("~/Library/Application Support/Firefox",
                "~/.mozilla/firefox",
                "~/AppData/Roaming/Mozilla/Firefox"),
    "chrome": ("~/Library/Application Support/Google/Chrome",
               "~/.config/google-chrome",
               "~/AppData/Local/Google/Chrome/User Data"),
    "brave": ("~/Library/Application Support/BraveSoftware/Brave-Browser",
              "~/.config/BraveSoftware/Brave-Browser",
              "~/AppData/Local/BraveSoftware/Brave-Browser/User Data"),
    "edge": ("~/Library/Application Support/Microsoft Edge",
             "~/.config/microsoft-edge",
             "~/AppData/Local/Microsoft/Edge/User Data"),
    "chromium": ("~/Library/Application Support/Chromium",
                 "~/.config/chromium"),
    "vivaldi": ("~/Library/Application Support/Vivaldi",
                "~/.config/vivaldi"),
    "opera": ("~/Library/Application Support/com.operasoftware.Opera",
              "~/.config/opera"),
    "safari": ("~/Library/Cookies",),
}

_cookie_gate = threading.Lock()
_cookie_choice: str | None = None       # browser that last worked
_cookie_dead: set[str] = set()          # stores we established we cannot read

# "Refused because we look like a robot." Retrying these WITH cookies is
# worth a shot.
_BOT_BLOCK = re.compile(
    r"sign in to confirm|not a bot|--cookies|po token|"
    r"429|too many requests|"
    r"this helps protect our community",
    re.I)

# "Cookies themselves could not be read." Retrying the same browser is
# pointless; it will fail identically on every video.
_COOKIE_UNREADABLE = re.compile(
    r"could not (find|copy|read|open)[^\n]{0,60}cookie|"
    r"unable to (open|read)[^\n]{0,60}(database|cookie)|"
    r"permission denied|operation not permitted|"
    r"failed to decrypt|dpapi|"
    r"cookie database[^\n]{0,40}(locked|in use)|"
    r"(unsupported|invalid) browser|"
    r"no [^\n]{0,20}(cookies|profile)[^\n]{0,20}found",
    re.I)


def looks_like_bot_block(err: object) -> bool:
    return bool(_BOT_BLOCK.search(str(err)))


def looks_like_cookie_failure(err: object) -> bool:
    return bool(_COOKIE_UNREADABLE.search(str(err)))


def installed_browsers() -> list[str]:
    """Browsers with a profile directory on this machine, best-readable first."""
    return [name for name, paths in _BROWSER_PROFILES.items()
            if any(Path(p).expanduser().is_dir() for p in paths)]


def cookie_candidates() -> list[str]:
    """Browsers still worth trying, the one already known to work first."""
    if not COOKIES_ENABLED:
        return []
    with _cookie_gate:
        preferred, dead = _cookie_choice, set(_cookie_dead)
    if COOKIES_FROM_BROWSER:
        # An explicit choice is honoured as given and never second-guessed:
        # yt-dlp also accepts "chrome:Profile 2" and keyring suffixes, which
        # will not match anything in _BROWSER_PROFILES.
        return [] if COOKIES_FROM_BROWSER in dead else [COOKIES_FROM_BROWSER]
    order = [b for b in installed_browsers() if b not in dead]
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    return order


def cookie_args(browser: str | None) -> list[str]:
    return ["--cookies-from-browser", browser] if browser else []


def run_yt_dlp(build: Callable[[list[str]], list[str]],
               relay: Callable[[str], None] | None = None) -> str:
    """Run a yt-dlp command, escalating to browser cookies if YouTube refuses.

    build() is handed the cookie flags to splice in, and returns the full
    argv. Attempts stop at the first success, and the browser that succeeded
    becomes the default for the rest of the run.
    """
    global _cookie_choice

    with _cookie_gate:
        first = _cookie_choice          # None on a fresh run: anonymous first
    attempts: list[str | None] = [first]
    attempts += [b for b in cookie_candidates() if b != first]

    last: RuntimeError | None = None
    blocked = False          # did ANY attempt come back as "you're a robot"
    for browser in attempts:
        cmd = build(cookie_args(browser))
        try:
            if relay is None:
                out = run(cmd)
            else:
                # --newline stops yt-dlp rewriting one line with \r, which we
                # cannot parse incrementally
                out = run_progress([*cmd[:1], "--newline", *cmd[1:]], relay)
        except RuntimeError as e:
            last = e
            blocked = blocked or looks_like_bot_block(e)
            if browser and looks_like_cookie_failure(e):
                with _cookie_gate:
                    _cookie_dead.add(browser)
                    if _cookie_choice == browser:
                        _cookie_choice = None
                debug(f"cannot read {browser} cookies, skipping it from now on")
                continue
            if looks_like_bot_block(e):
                debug("refused as a bot, retrying with browser cookies")
                continue
            raise                        # a real error: private, 404, disk full
        else:
            if browser:
                with _cookie_gate:
                    if _cookie_choice != browser:
                        _cookie_choice = browser
                        log(f"    (signed in via {browser} cookies)")
            return out

    assert last is not None
    raise _explain_block(last, blocked)


def _explain_block(err: RuntimeError, blocked: bool = False) -> RuntimeError:
    """Turn yt-dlp's wall of text into something actionable.

    `blocked` carries whether the ORIGINAL refusal was a bot check. The last
    exception often isn't: a bot block followed by an unreadable cookie store
    ends on "could not find firefox cookies database", and diagnosing from
    that alone loses the reason we reached for cookies in the first place.
    """
    if not (blocked or looks_like_bot_block(err)):
        return err

    tried = [b for b in installed_browsers() if b in _cookie_dead]
    if not COOKIES_ENABLED:
        hint = "cookies are switched off (--no-cookies). Drop that flag."
    elif not installed_browsers():
        hint = ("no browser profile was found to take cookies from. "
                "Sign in to YouTube in Firefox or Chrome, then run again.")
    elif tried and not cookie_candidates():
        hint = (f"tried cookies from {', '.join(tried)} but the cookie store "
                "could not be read. On macOS, System Settings > Privacy & "
                "Security > Full Disk Access, add your terminal, and restart "
                "it. Quitting the browser first also helps.")
    else:
        hint = ("cookies were sent and YouTube still refused. The IP is most "
                "likely rate limited: wait an hour or two, or switch off a "
                "VPN if one is on.")

    return RuntimeError(
        "YouTube refused the download and asked us to prove we're not a bot.\n"
        f"  {hint}\n"
        "  Also worth doing first: pip install -U yt-dlp (fixes ship weekly).\n"
        f"\n{err}")


def download(url: str, dest_dir: Path,
             on_progress: Callable[[float], None] | None = None) -> Path:
    """Fetch a URL with yt-dlp. Returns the path of THIS video.

    The video's own id is resolved first and the file is then located by that
    id. Matching on "whatever appeared in the folder" is not safe: when the
    video is already cached yt-dlp writes nothing, and the fallback used to
    hand back the most recently touched file in the folder, which is a
    different video entirely.
    """
    if not is_safe_url(url):
        raise RuntimeError(
            "that link didn't look like a video address. "
            "it must start with http:// or https://")
    if not have("yt-dlp"):
        raise RuntimeError("yt-dlp is not installed (pip install -U yt-dlp)")

    # A YouTube URL already contains the id, so resolving it over the network
    # was a wasted round trip on every single link, and one more request
    # against a host that rate limits. Only unusual URLs need asking.
    vid = youtube_id(url)
    if vid is None:
        def build_probe(cookies: list[str]) -> list[str]:
            return ["yt-dlp", *cookies,
                    "--no-playlist", "--skip-download", "--print", "id",
                    "--", url]

        try:
            vid = run_yt_dlp(build_probe).strip().splitlines()[0].strip()
        except (RuntimeError, IndexError) as e:
            # a bot block already carries its own instructions; replacing it
            # with "check the link is real" would send the user the wrong way
            if isinstance(e, RuntimeError) and looks_like_bot_block(e):
                raise
            raise RuntimeError(
                "could not read that link. check it is a real video URL, "
                "and that the video is not private or region locked"
            ) from e

    def by_id() -> list[Path]:
        return [p for p in dest_dir.glob(f"{vid}.*")
                if p.suffix.lower() in VIDEO_EXTS and p.stat().st_size > 0]

    cached = by_id()
    if cached:
        log(f"    already downloaded ({cached[0].name}), reusing")
        return cached[0]

    throttle_downloads()

    def build_fetch(cookies: list[str]) -> list[str]:
        return [
            "yt-dlp",
            *cookies,
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "-o", str(dest_dir / "%(id)s.%(ext)s"),
            "--",                  # nothing after this can be read as a flag
            url,
        ]

    def relay(line: str) -> None:
        m = _YT_PCT.search(line)
        if m and on_progress is not None:
            on_progress(float(m.group(1)) / 100.0)

    # no callback means no need for line-by-line reading, which lets the
    # simpler run() path handle it
    run_yt_dlp(build_fetch, relay if on_progress is not None else None)

    got = by_id()
    if got:
        return got[0]
    raise RuntimeError(f"download finished but no file appeared for id {vid}")


# ------------------------------------------------- picking the clip


def clip_number(path: Path) -> int | None:
    """The version number in a clip filename, or None if it isn't numbered.

    Deliberately strict: the whole stem has to be digits. Anything looser
    (pulling the first number out of "final v2 export") would let a stray
    video in the folder be mistaken for the interruption clip.
    """
    stem = path.stem.strip()
    return int(stem) if stem.isdigit() else None


def find_interruption(root: Path) -> Path | None:
    """The clip to splice in: the highest-numbered one in the data folder.

    Clips are versioned by number - 1.mov, 2.mp4, 3.mov - and the highest
    number is the live one. Older numbers stay put as history and are ignored,
    so replacing the clip is a drag-and-drop with nothing to rename or delete.

    The legacy "interruption.mp4" name still works, so an older setup that
    never switched to numbers keeps running untouched.
    """
    try:
        entries = list(root.iterdir())
    except OSError:
        return None

    numbered: list[tuple[int, Path]] = []
    for p in entries:
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        n = clip_number(p)
        if n is not None:
            numbered.append((n, p))

    if numbered:
        # Highest number wins. Extension is the tie-break, alphabetically last,
        # which lands on .mp4 over .mov - the format that needs the least work
        # downstream. The point of tie-breaking at all is that a folder holding
        # both 2.mov and 2.mp4 resolves identically on every run, rather than
        # following whatever order the filesystem happens to hand back.
        return max(numbered, key=lambda np: (np[0], np[1].suffix.lower()))[1]

    legacy = root / f"interruption{DEFAULT_CLIP_EXT}"
    if legacy.is_file():
        return legacy
    alts = sorted(p for p in entries
                  if p.is_file() and p.stem == "interruption"
                  and p.suffix.lower() in VIDEO_EXTS)
    return alts[0] if alts else None


# ---------------------------------------------------------------- main pass


def process_all(root: Path, split_at: float | None = SPLIT_AT,
                keep_downloads: bool = True,
                extra_urls: Sequence[str] | None = None,
                force: bool = False,
                reporter: Any = None) -> int:
    global _warned_no_interruption

    dirs = {name: root / name for name in
            ("inbox", "downloads", "output", "processed")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    prune_cache(dirs["downloads"])

    interruption = find_interruption(root)
    if interruption is None:
        # in watch mode this condition persists, so say it loudly once
        # rather than repeating the same line every minute forever
        if not _warned_no_interruption:
            _warned_no_interruption = True
            warn(f"no interruption clip found. put one in {root} "
                 f"named 1{DEFAULT_CLIP_EXT} (then 2, 3, ... for later versions)")
        else:
            debug("still no interruption clip")
        return 0
    # worth a normal log line, not a debug one: silently splicing the wrong
    # version into a whole batch is expensive to discover later
    log(f"interruption clip: {interruption.name}")
    _warned_no_interruption = False

    state = State(root / "state.json")
    urls_file = root / "urls.txt"
    if not urls_file.exists():
        urls_file.write_text("# one URL per line\n")

    # (state key, label shown to the user, either a URL string or a local Path)
    jobs: list[tuple[str, str, str | Path]] = []

    # links passed on the command line jump the queue
    for u in (extra_urls or []):
        u = u.strip()
        if not u:
            continue
        if not is_safe_url(u):
            warn(f"ignoring something that isn't a web address: {u[:60]}")
            continue
        jobs.append(("url:" + u, u, u))

    for line in urls_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not is_safe_url(line):
            warn(f"ignoring a line in urls.txt that isn't a web address: "
                 f"{line[:60]}")
            continue
        jobs.append(("url:" + line, line, line))

    for f in sorted(dirs["inbox"].iterdir()):
        if f.suffix.lower() in VIDEO_EXTS:
            # resolve() guarantees a leading "/", so ffmpeg cannot mistake a
            # file called "-loglevel.mp4" for a command line option
            jobs.append(("file:" + f.name, f.name, f.resolve()))

    if not jobs:
        return 0

    # Dedupe up front rather than inside the loop, so the prefetch below can
    # look ahead at real work instead of re-fetching a link twice.
    unique: list[tuple[str, str, str | Path]] = []
    seen: set[str] = set()
    for job in jobs:
        if job[0] not in seen:
            seen.add(job[0])
            unique.append(job)
    jobs = unique

    # Overlap the next download with the current encode. Deliberately ONE
    # worker: parallel downloads against a host that rate limits is the exact
    # pattern the backoff logic exists to avoid. This hides download latency
    # without raising the request rate at all.
    pool: ThreadPoolExecutor | None = None
    pending: dict[str, Future[Path]] = {}

    def prefetch(after: int) -> None:
        if pool is None:
            return
        for k, _lbl, tgt in jobs[after + 1:after + 2]:
            if (isinstance(tgt, str) and k not in pending
                    and not (state.is_done(k) and not force)):
                pending[k] = pool.submit(download, tgt, dirs["downloads"])

    url_jobs = sum(1 for _k, _l, t in jobs if isinstance(t, str))
    if PREFETCH and url_jobs > 1:
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fetch")

    count = 0
    for index, (key, label, target) in enumerate(jobs):
        if state.is_done(key) and not force:
            debug(f"already done, skipping: {label}")
            if reporter:
                reporter.result("skipped", label, "already made")
            continue

        # A link the user just pasted is always attempted; force means "I am
        # asking for this now". Only the unattended queue backs off.
        if not force:
            wait = state.retry_wait(key)
            if wait < 0:
                debug(f"parked after {RETRY_GIVE_UP_AFTER} failures: {label}")
                if reporter:
                    reporter.result("parked", label, "set aside")
                continue
            if wait > 0:
                debug(f"backing off {wait / 60:.0f} more min before retrying {label}")
                if reporter:
                    due = time.strftime("%H:%M", time.localtime(time.time() + wait))
                    reporter.result("retry", label, due)
                continue

        log(f"-> {label}")
        # scratch space lives in the system temp dir, not the data folder,
        # so cloud-synced or network-mounted data folders can't trip it up
        work = Path(tempfile.mkdtemp(prefix="shortsbot-"))
        prog = reporter.begin(index + 1, len(jobs), label) if reporter else None

        try:
            if isinstance(target, Path):
                source = target
            else:
                fut = pending.pop(key, None)
                if prog:
                    prog.start("Download")
                if fut is not None:
                    log("    downloading (already started in the background)")
                    source = fut.result()
                else:
                    log("    downloading")
                    source = download(
                        target, dirs["downloads"],
                        on_progress=(lambda f: prog.update("Download", f))
                        if prog else None)
                if prog:
                    prog.finish("Download", f"{source.stat().st_size / 1e6:.1f} MB")
                prefetch(index)

            out_path = reserve_numbered_output(dirs["output"])

            if prog and isinstance(target, Path):
                prog.finish("Download", "local file")

            try:
                if prog:
                    prog.start("Splice")
                def began_export() -> None:
                    # the join IS the export, so the boundary belongs here
                    # rather than after splice() has already returned
                    if prog:
                        prog.finish("Splice")
                        prog.start("Export")

                splice(source, interruption, out_path, work, split_at,
                       on_progress=(lambda f: prog.update("Splice", f))
                       if prog else None,
                       on_method=(lambda m: prog.update("Splice", 0.0, m))
                       if prog else None,
                       on_export=began_export if prog else None)
            except Exception:
                # don't leave the reserved placeholder behind on failure
                out_path.unlink(missing_ok=True)
                raise

            if isinstance(target, Path):
                shutil.move(str(target), str(dirs["processed"] / target.name))
            if not keep_downloads and not isinstance(target, Path):
                source.unlink(missing_ok=True)

            state.mark(key)
            state.clear_failure(key)     # a success wipes the backoff history
            count += 1
            secs = duration_of(out_path)
            log(f"   done -> {out_path.name} ({secs:.1f}s)")
            if prog:
                prog.finish("Export", f"{out_path.stat().st_size / 1e6:.1f} MB")
                reporter.record_timings(label)
                prog.close()
                reporter.result("success", label,
                                f"{prog.total_seconds():.1f}s")
        except Exception as e:  # noqa: BLE001
            # Deliberately broad: one bad video (corrupt file, 429, disk full)
            # must not take down the rest of the batch. The item is left
            # unmarked so it can be retried, but under a growing backoff.
            n_fail = state.record_failure(key, str(e))
            error(f"   FAILED ({n_fail}x): {e}", exc=e)
            parked = n_fail >= RETRY_GIVE_UP_AFTER
            when: str | None = None
            if not parked and not force:
                delay = min(RETRY_BASE_SECONDS * 2 ** (n_fail - 1),
                            RETRY_MAX_SECONDS)
                when = time.strftime("%H:%M", time.localtime(time.time() + delay))
            if prog:
                for phase in ("Download", "Splice", "Export"):
                    if prog.state[phase] == "running":
                        prog.fail(phase)
                reporter.record_timings(label)
                prog.close()
                reporter.failed(label, e, when, parked)
            elif parked:
                warn(f"   giving up on {label} after {n_fail} tries.")
            elif when:
                log(f"   will try again around {when}")
        finally:
            # temp cut files and the concat manifest live in here, so this one
            # line disposes of everything the job created, on every exit path
            shutil.rmtree(work, ignore_errors=True)

    if pool is not None:
        # a prefetch may still be in flight if the loop ended early; let it
        # finish rather than leaving a half-written file in the cache
        for fut in pending.values():
            fut.cancel()
        pool.shutdown(wait=True)

    return count


# ---------------------------------------------------------------- interactive


def read_pasted_lines(prompt: str) -> list[str] | None:
    """Read a line, then grab any further lines already sitting in the buffer.

    A multi-line paste lands in the terminal all at once, so after the first
    line the rest are immediately readable. select() picks them up with no
    wait; typing by hand finds nothing extra and carries on.

    This deliberately lives in Python rather than the shell. The shell version
    used `read -t 0.4`, and macOS ships bash 3.2, which rejects fractional
    timeouts outright, so only the first line was ever captured.
    """
    try:
        first = input(prompt)
    except (EOFError, KeyboardInterrupt):
        # Ctrl-D or Ctrl-C at the prompt is how a person quits. Returning None
        # ends the loop cleanly instead of dumping a traceback at them.
        print()
        return None

    lines = [first]
    try:
        while select.select([sys.stdin], [], [], 0.35)[0]:
            nxt = sys.stdin.readline()
            if not nxt:
                break
            lines.append(nxt.rstrip("\n"))
    except (OSError, ValueError):
        # stdin closed or not selectable (piped input, odd terminal).
        # Whatever was already read is still usable.
        pass
    return lines


def extract_urls(lines: object) -> list[str]:
    """Pull real links out of a messy paste, in order, without duplicates.

    Tolerant by design. A paste can contain anything, including nothing.
    """
    if not lines:
        return []
    if isinstance(lines, str):
        lines = [lines]
    if not isinstance(lines, Iterable):
        return []

    urls: list[str] = []
    seen = set()          # a set, not a scan of `urls`, so a huge paste
    for line in lines:    # stays linear rather than quadratic
        if not isinstance(line, str):
            continue
        for token in re.split(r"[\s,]+", line.strip()):
            token = token.strip("\"'<>")
            if token.lower().startswith("http") and token not in seen:
                seen.add(token)
                urls.append(token)
    return urls


class Reporter:
    """Bridges the engine's callbacks to the terminal UI.

    The engine knows nothing about panels or colour; it just calls these.
    Swapping this out is how you'd drive the same pipeline from somewhere else.
    """

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.current: Any = None
        self.samples: list[dict[str, float]] = []

    def begin(self, index: int, total: int, title: str) -> Any:
        self.current = ui.Progress(_pretty(title), index, total)
        self.current.open()
        return self.current

    def result(self, kind: str, title: str, note: str = "") -> None:
        self.rows.append((kind, _pretty(title), note))

    def failed(self, title: str, exc: BaseException,
               retry_at: str | None, parked: bool) -> None:
        print()
        ui.show_error(exc, retry_at=retry_at, parked=parked)
        kind = "parked" if parked else ("retry" if retry_at else "failed")
        self.rows.append((kind, _pretty(title), retry_at or ""))

    def record_timings(self, label: str) -> None:
        """Keep the per-phase seconds, and write one greppable log line."""
        if self.current is None:
            return
        t = self.current.timings()
        if not t:
            return
        self.samples.append(t)
        log("timing "
            + " ".join(f"{k.lower()}={v:.3f}" for k, v in t.items())
            + f" total={sum(t.values()):.3f} video={_pretty(label)!r}")

    def flush(self) -> None:
        ui.summary(self.rows)
        ui.profile(self.samples)
        self.rows = []
        self.samples = []


def _pretty(label: str) -> str:
    """Turn a URL or filename into something worth reading in a panel."""
    if label.startswith("http"):
        vid = youtube_id(label)
        return f"youtube.com/…/{vid}" if vid else label

    name = label
    # Only strip a real video extension. Path().stem cuts at the LAST dot,
    # which mangles names like "YTDown.com_Shorts_..." down to "YTDown".
    suffix = Path(name).suffix.lower()
    if suffix in VIDEO_EXTS:
        name = name[:-len(suffix)]

    name = re.sub(r"^YTDown\.com_Shorts_", "", name)
    name = re.sub(r"_Media_.*$", "", name)
    name = re.sub(r"[-_]+", " ", name).strip()
    return name or label or "video"


def _folder_size_gb(path: Path) -> float:
    try:
        return sum(p.stat().st_size for p in path.glob("*") if p.is_file()) / 1024 ** 3
    except OSError:
        return 0.0


def _run_batch(root: Path, split_at: float | None,
               urls: Sequence[str] | None = None) -> None:
    rep = Reporter()
    n = process_all(root, split_at, extra_urls=urls,
                    force=bool(urls), reporter=rep)
    rep.flush()
    if n:
        subprocess.run(["open", str(root / "output")], check=False)


def interactive(root: Path, split_at: float | None) -> None:
    state = State(root / "state.json")

    while True:
        ui.clear()
        ui.header(made=len(state.data.get("done", [])),
                  cache_gb=_folder_size_gb(root / "downloads"))
        ui.menu()

        lines = read_pasted_lines("  choose ›  ")
        if lines is None:
            return
        choice = lines[0].strip().lower()

        if choice in ("q", "quit", "exit", "5q"):
            print("\n  Bye.\n")
            return

        if choice in ("1", "paste", "links") or choice.startswith("http"):
            # a link pasted straight at the menu skips the extra step
            urls = extract_urls(lines)
            if not urls:
                print()
                print("  Paste your link(s) now, then press Enter.")
                more = read_pasted_lines("  › ")
                if more is None:
                    return
                urls = extract_urls(more)
            if not urls:
                _pause("  No links found in that. They need to start with http.")
                continue
            print()
            print(f"  {len(urls)} link{'s' if len(urls) != 1 else ''} queued.")
            _run_batch(root, split_at, urls)
            state = State(root / "state.json")
            _pause()

        elif choice in ("2", "folder", "inbox"):
            inbox = list((root / "inbox").glob("*"))
            videos = [p for p in inbox if p.suffix.lower() in VIDEO_EXTS]
            if not videos:
                _pause("  The inbox folder is empty. Drop some videos in first.")
                subprocess.run(["open", str(root / "inbox")], check=False)
                continue
            print()
            print(f"  {len(videos)} video{'s' if len(videos) != 1 else ''} in the inbox.")
            _run_batch(root, split_at)
            state = State(root / "state.json")
            _pause()

        elif choice in ("3", "open", "output"):
            subprocess.run(["open", str(root / "output")], check=False)

        elif choice in ("4", "clip", "change"):
            print()
            print("  Your interruption clip is the one inserted into every video.")
            print("  Replace the file named interruption.mp4, keeping the name.")
            subprocess.run(["open", str(root)], check=False)
            _pause()

        elif choice in ("5", "recent", "activity", "log"):
            _recent(root)
            _pause()

        else:
            _pause("  Pick a number from the list, or Q to quit.")


def _pause(message: str = "") -> None:
    if message:
        print()
        print(message)
    print()
    try:
        input("  press Enter to go back  ")
    except (EOFError, KeyboardInterrupt):
        print()


def _recent(root: Path, limit: int = 8) -> None:
    out = root / "output"
    try:
        files = sorted((p for p in out.glob("*.mp4")),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    except OSError:
        files = []
    if not files:
        print("\n  Nothing made yet.")
        return
    rows = []
    for f in files:
        when = time.strftime("%d %b %H:%M", time.localtime(f.stat().st_mtime))
        # numbered outputs read better with a "#", and older "<id>-spliced"
        # names from before the switch still need their suffix trimmed
        label = f"#{f.stem}" if f.stem.isdigit() else f.stem.replace("-spliced", "")
        rows.append(("success", label,
                     f"{when}  {f.stat().st_size / 1e6:.1f} MB"))
    ui.summary(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None, help="data folder (default: ./shorts_bot_data)")
    ap.add_argument("--watch", action="store_true", help="keep running, rescan periodically")
    ap.add_argument("--interval", type=int, default=POLL_SECONDS, help="seconds between scans")
    ap.add_argument("--split-at", type=float, default=SPLIT_AT,
                    help="where to cut, 0-1 (0.5 = exact midpoint). "
                         # no literal "%" here: argparse runs its own
                         # %-interpolation over help text and would raise
                         "omit for a random point inside SPLIT_RANGE "
                         f"(currently {SPLIT_RANGE[0]:.2f}-{SPLIT_RANGE[1]:.2f})")
    ap.add_argument("--no-keep", action="store_true", help="delete downloads after export")
    ap.add_argument("--url", nargs="*", default=None,
                    help="process these link(s) right now")
    ap.add_argument("--force", action="store_true",
                    help="process even if it was done before")
    ap.add_argument("--open", action="store_true",
                    help="open the output folder when finished")
    ap.add_argument("--interactive", action="store_true",
                    help="prompt for links in a loop")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="show debug detail on screen (always in the log file)")
    ap.add_argument("--cookies-from", default=None, metavar="BROWSER",
                    help="take cookies from this browser when YouTube asks us "
                         "to prove we're not a bot (firefox, chrome, brave, "
                         "edge, safari...). default: try installed browsers")
    ap.add_argument("--no-cookies", action="store_true",
                    help="never read browser cookies, even if YouTube refuses")
    args = ap.parse_args()

    global COOKIES_FROM_BROWSER, COOKIES_ENABLED
    COOKIES_FROM_BROWSER = args.cookies_from
    COOKIES_ENABLED = not args.no_cookies

    if not have("ffmpeg") or not have("ffprobe"):
        sys.exit("ffmpeg/ffprobe not found. Install ffmpeg first (brew install ffmpeg).")

    root = (Path(args.root).resolve() if args.root
            else Path(__file__).resolve().parent / "shorts_bot_data")
    root.mkdir(parents=True, exist_ok=True)
    log_path = setup_logging(root, verbose=args.verbose)
    if log_path:
        debug(f"logging to {log_path}")

    if args.interactive:
        if not args.verbose:
            set_console_level(logging.WARNING)
        interactive(root, args.split_at)
        return

    log(f"data folder: {root}")

    if not args.watch:
        n = process_all(root, args.split_at, not args.no_keep,
                        extra_urls=args.url, force=args.force)
        log(f"finished, {n} video(s) exported")
        if args.open and n:
            subprocess.run(["open", str(root / "output")], check=False)
        return

    log(f"watching (every {args.interval}s), ctrl-c to stop")
    while True:
        try:
            n = process_all(root, args.split_at, not args.no_keep)
            if n:
                log(f"exported {n}")
            # the sleep has to sit inside the try, otherwise ctrl-c during the
            # wait (which is where the process spends nearly all its time)
            # escapes as a traceback instead of stopping cleanly
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log("stopped")
            return
        except Exception as e:  # noqa: BLE001 - one bad pass must not end the watch
            log(f"pass failed: {e}")
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(130)   # conventional exit code for interrupted-by-ctrl-c
