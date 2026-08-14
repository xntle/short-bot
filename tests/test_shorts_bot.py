"""Edge-case suite for shorts_bot.

Run from the Shorts Bot folder:

    python3 -m pytest tests/ -v

Tests that need real ffmpeg are marked `ffmpeg` and skip automatically when it
isn't installed:

    python3 -m pytest tests/ -v -m "not ffmpeg"

Nothing here touches the network. Every yt-dlp and ffmpeg call is stubbed
except in the handful of tests explicitly marked `ffmpeg`, which build tiny
synthetic clips locally.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shorts_bot as sb  # noqa: E402


HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def needs_ffmpeg(fn):
    """Tag the test AND skip it when ffmpeg is absent."""
    fn = pytest.mark.ffmpeg(fn)
    return pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")(fn)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_clip(path, seconds=4, w=360, h=640, audio=True, fps=30):
    """Build a tiny real video so ffmpeg-backed paths get exercised."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate={fps}:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i",
                f"sine=frequency=440:duration={seconds},aformat=channel_layouts=stereo"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def make_clip_gop(path, seconds=10, w=360, h=640, fps=30, gop=60, rate=44100):
    """A clip shaped like a real YouTube download: h264 + aac, regular GOP.

    The fixed GOP matters: the stream-copy path needs keyframes to cut on.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate={fps}:duration={seconds}",
         "-f", "lavfi", "-i",
         f"sine=frequency=440:duration={seconds},aformat=channel_layouts=stereo",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", str(gop),
         "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ar", str(rate), "-ac", "2", "-shortest", str(path)],
        check=True, capture_output=True)
    return path


@pytest.fixture
def root(tmp_path):
    """A data folder with an interruption clip already in place."""
    r = tmp_path / "data"
    (r / "inbox").mkdir(parents=True)
    (r / "downloads").mkdir()
    if HAVE_FFMPEG:
        make_clip(r / "interruption.mp4", seconds=2, w=320, h=320)
    else:
        (r / "interruption.mp4").write_bytes(b"stub")
    return r


@pytest.fixture
def offline(monkeypatch):
    """Block every subprocess call so a stray real invocation fails loudly."""
    def boom(cmd, **kw):
        raise AssertionError(f"unstubbed subprocess call: {cmd}")
    monkeypatch.setattr(sb, "run", boom)
    monkeypatch.setattr(sb, "have", lambda b: True)
    return monkeypatch


class FakeYtDlp:
    """Stands in for yt-dlp. Scriptable per video id."""

    def __init__(self, dest, fail_ids=None, exc=None, produce=True):
        self.dest = Path(dest)
        self.fail_ids = set(fail_ids or [])
        self.exc = exc or RuntimeError("yt-dlp failed")
        self.produce = produce
        self.calls = []

    def __call__(self, cmd, **kw):
        # recording lives here and only here, so a subclass can override the
        # behaviour without double-counting the call
        self.calls.append(cmd)
        return self._run(cmd, **kw)

    def _run(self, cmd, **kw):
        if cmd[0] != "yt-dlp":
            raise AssertionError(f"unexpected command {cmd[0]}")
        url = cmd[-1]
        vid = url.rstrip("/").rsplit("/", 1)[-1]
        if "--print" in cmd:
            if vid in self.fail_ids:
                raise self.exc
            return vid + "\n"
        if vid in self.fail_ids:
            raise self.exc
        if self.produce:
            (self.dest / f"{vid}.mp4").write_bytes(b"x" * 128)
        return ""


# ==========================================================================
# 1. invalid, null, empty and malformed input
# ==========================================================================


class TestMalformedInput:

    @pytest.mark.parametrize("lines,expected", [
        ([], []),
        ([""], []),
        (["   "], []),
        (["\n\t "], []),
        (["not a link"], []),
        (["ftp://example.com/x.mp4"], []),
        (["http://a"], ["http://a"]),
        (["HTTPS://A/B"], ["HTTPS://A/B"]),
        (['"https://a/1"'], ["https://a/1"]),
        (["<https://a/1>"], ["https://a/1"]),
        (["https://a/1, https://a/2"], ["https://a/1", "https://a/2"]),
        (["https://a/1 https://a/2"], ["https://a/1", "https://a/2"]),
        (["https://a/1", "https://a/1"], ["https://a/1"]),
        (["look: https://a/1 thanks"], ["https://a/1"]),
    ])
    def test_extract_urls_shapes(self, lines, expected):
        assert sb.extract_urls(lines) == expected

    def test_extract_urls_rejects_none_element(self):
        """A None in the list must not blow up the whole paste."""
        assert sb.extract_urls([None, "https://a/1"]) == ["https://a/1"]

    def test_extract_urls_rejects_none_argument(self):
        assert sb.extract_urls(None) == []

    def test_extract_urls_handles_bytes(self):
        assert sb.extract_urls([b"https://a/1"]) == []

    def test_slugify_empty_and_symbols(self):
        assert sb.slugify("") == "video"
        assert sb.slugify("///") == "video"
        assert sb.slugify(None) == "video"

    def test_slugify_length_capped(self):
        assert len(sb.slugify("a" * 500)) <= 60

    def test_slugify_unicode_survives(self):
        out = sb.slugify("Cà phê sữa đá")
        assert out and out != "video"

    def test_state_missing_file(self, tmp_path):
        s = sb.State(tmp_path / "nope.json")
        assert s.is_done("x") is False

    def test_state_corrupt_json(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("{not json at all")
        s = sb.State(p)
        assert s.is_done("x") is False

    def test_state_json_is_a_list(self, tmp_path):
        """A JSON array is valid JSON but the wrong shape."""
        p = tmp_path / "state.json"
        p.write_text('["a", "b"]')
        s = sb.State(p)
        assert s.is_done("a") is False
        s.mark("c")

    def test_state_json_is_a_scalar(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text('"hello"')
        s = sb.State(p)
        assert s.is_done("a") is False

    def test_state_done_key_wrong_type(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text('{"done": "not-a-list"}')
        s = sb.State(p)
        assert s.is_done("a") is False
        s.mark("a")
        assert s.is_done("a") is True

    def test_state_empty_file(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("")
        s = sb.State(p)
        assert s.is_done("x") is False

    @pytest.mark.parametrize("frac", [0.0, 1.0, -0.5, 1.5, 99.0])
    @needs_ffmpeg
    def test_split_out_of_range_is_rejected(self, tmp_path, frac):
        src = make_clip(tmp_path / "s.mp4", seconds=3)
        mid = make_clip(tmp_path / "m.mp4", seconds=1)
        work = tmp_path / "w"
        work.mkdir()
        with pytest.raises(RuntimeError):
            sb.splice(src, mid, tmp_path / "out.mp4", work, split_at=frac)

    @needs_ffmpeg
    def test_source_too_short_to_split(self, tmp_path):
        src = make_clip(tmp_path / "s.mp4", seconds=1)
        mid = make_clip(tmp_path / "m.mp4", seconds=1)
        work = tmp_path / "w"
        work.mkdir()
        with pytest.raises(RuntimeError, match="too short"):
            sb.splice(src, mid, tmp_path / "o.mp4", work, split_at=0.01)

    def test_duration_of_non_numeric_output(self, monkeypatch, tmp_path):
        """ffprobe reports N/A for some malformed containers."""
        monkeypatch.setattr(sb, "run", lambda *a, **k: "N/A\n")
        with pytest.raises(Exception):
            sb.duration_of(tmp_path / "x.mp4")

    @needs_ffmpeg
    def test_zero_byte_file_in_inbox_is_reported_not_fatal(self, root, offline):
        (root / "inbox" / "broken.mp4").write_bytes(b"")
        offline.setattr(sb, "run", sb.run.__wrapped__ if hasattr(sb.run, "__wrapped__") else _real_run)
        n = sb.process_all(root)
        assert n == 0
        assert (root / "inbox" / "broken.mp4").exists(), "bad file must stay put"

    def test_missing_interruption_returns_zero(self, tmp_path):
        r = tmp_path / "data"
        (r / "inbox").mkdir(parents=True)
        assert sb.process_all(r) == 0

    def test_no_jobs_returns_zero(self, root, offline):
        assert sb.process_all(root) == 0

    def test_directory_masquerading_as_video(self, root, offline):
        (root / "inbox" / "sneaky.mp4").mkdir()
        n = sb.process_all(root)
        assert n == 0

    def test_non_video_files_ignored(self, root, offline):
        for name in ("notes.txt", ".DS_Store", "cover.jpg", "archive.zip"):
            (root / "inbox" / name).write_text("x")
        assert sb.process_all(root) == 0


_real_run = sb.run


# ==========================================================================
# 2. network failures: timeouts, rate limits, dropped connections
# ==========================================================================


class TestNetworkFailures:
    """yt-dlp is the only network surface, so failures arrive as non-zero
    exits. These assert the failure is contained and legible."""

    @pytest.mark.parametrize("message", [
        "HTTP Error 429: Too Many Requests",
        "HTTP Error 500: Internal Server Error",
        "HTTP Error 503: Service Unavailable",
        "[Errno 60] Operation timed out",
        "Connection reset by peer",
        "unable to download video data: <urlopen error timed out>",
        "Video unavailable. This video is private",
        "Sign in to confirm you're not a bot",
    ])
    def test_download_failure_surfaces_as_runtime_error(
            self, root, monkeypatch, message):
        fake = FakeYtDlp(root / "downloads", fail_ids={"AAA"},
                         exc=RuntimeError(message))
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        with pytest.raises(RuntimeError):
            sb.download("https://x/AAA", root / "downloads")

    def test_id_lookup_failure_gives_human_message(self, root, monkeypatch):
        fake = FakeYtDlp(root / "downloads", fail_ids={"AAA"},
                         exc=RuntimeError("Unsupported URL"))
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        with pytest.raises(RuntimeError) as e:
            sb.download("https://x/AAA", root / "downloads")
        assert "could not read that link" in str(e.value)

    def test_rate_limit_is_not_reported_as_a_bad_link(self, root, monkeypatch):
        """429 during id lookup used to say "check it is a real video URL",
        which sends the user off to inspect a link that is perfectly fine."""
        fake = FakeYtDlp(root / "downloads", fail_ids={"AAA"},
                         exc=RuntimeError("HTTP Error 429"))
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        with pytest.raises(RuntimeError) as e:
            sb.download("https://x/AAA", root / "downloads")
        assert "could not read that link" not in str(e.value)
        assert "not a bot" in str(e.value)

    def test_yt_dlp_missing_is_explained(self, root, monkeypatch):
        monkeypatch.setattr(sb, "have", lambda b: False)
        with pytest.raises(RuntimeError, match="yt-dlp is not installed"):
            sb.download("https://x/AAA", root / "downloads")


class BotBlockingYtDlp(FakeYtDlp):
    """yt-dlp against a YouTube that refuses anonymous requests.

    Succeeds only when --cookies-from-browser names a browser in `accepts`.
    """

    def __init__(self, dest, accepts=(), unreadable=(), **kw):
        super().__init__(dest, **kw)
        self.accepts = set(accepts)
        self.unreadable = set(unreadable)

    def _run(self, cmd, **kw):
        browser = None
        if "--cookies-from-browser" in cmd:
            browser = cmd[cmd.index("--cookies-from-browser") + 1]
        if browser in self.unreadable:
            raise RuntimeError(f"could not find {browser} cookies database")
        if browser not in self.accepts:
            raise RuntimeError(
                "Sign in to confirm you're not a bot. Use --cookies-from-browser")
        return super()._run(cmd, **kw)


class TestCookieEscalation:
    """YouTube's bot check: the app must escalate to browser cookies on its
    own, and must not keep paying for attempts it already knows will fail."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        # module-level memo of which browser worked, must not leak across tests
        monkeypatch.setattr(sb, "_cookie_choice", None)
        monkeypatch.setattr(sb, "_cookie_dead", set())
        monkeypatch.setattr(sb, "COOKIES_FROM_BROWSER", None)
        monkeypatch.setattr(sb, "COOKIES_ENABLED", True)
        monkeypatch.setattr(sb, "have", lambda b: True)
        monkeypatch.setattr(sb, "MIN_DOWNLOAD_GAP", 0.0)
        monkeypatch.setattr(sb, "DOWNLOAD_JITTER", 0.0)

    def _cookies(self, browser):
        return [c for c in browser if c]

    def test_no_cookies_sent_when_youtube_is_happy(self, root, monkeypatch):
        """The common path must stay anonymous: no browser DB reads, no
        signed-in account attached to routine downloads."""
        fake = FakeYtDlp(root / "downloads")
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "installed_browsers", lambda: ["firefox"])
        sb.download("https://x/AAA", root / "downloads")
        assert all("--cookies-from-browser" not in c for c in fake.calls)

    def test_bot_block_retries_with_cookies(self, root, monkeypatch):
        fake = BotBlockingYtDlp(root / "downloads", accepts={"firefox"})
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "installed_browsers", lambda: ["firefox"])
        got = sb.download("https://x/AAA", root / "downloads")
        assert got.name == "AAA.mp4"
        assert any("--cookies-from-browser" in c for c in fake.calls)

    def test_falls_through_to_the_second_browser(self, root, monkeypatch):
        fake = BotBlockingYtDlp(root / "downloads", accepts={"chrome"},
                                unreadable={"firefox"})
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "installed_browsers",
                            lambda: ["firefox", "chrome"])
        assert sb.download("https://x/AAA", root / "downloads").name == "AAA.mp4"

    def test_working_browser_is_reused_for_the_rest_of_the_batch(
            self, root, monkeypatch):
        """Regression guard on cost: without the memo, every video in a batch
        pays a failed anonymous attempt before reaching the cookie retry."""
        fake = BotBlockingYtDlp(root / "downloads", accepts={"firefox"})
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "installed_browsers", lambda: ["firefox"])
        sb.download("https://x/AAA", root / "downloads")
        first = len(fake.calls)
        sb.download("https://x/BBB", root / "downloads")
        second = fake.calls[first:]
        assert all("--cookies-from-browser" in c for c in second), \
            "second video should go straight to cookies, not re-try anonymous"

    def test_unreadable_store_is_not_retried_per_video(self, root, monkeypatch):
        fake = BotBlockingYtDlp(root / "downloads", accepts={"chrome"},
                                unreadable={"firefox"})
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "installed_browsers",
                            lambda: ["firefox", "chrome"])
        sb.download("https://x/AAA", root / "downloads")
        first = len(fake.calls)
        sb.download("https://x/BBB", root / "downloads")
        used = [c for c in fake.calls[first:] if "--cookies-from-browser" in c]
        assert all("firefox" not in c for c in used)

    def test_no_cookies_flag_is_honoured(self, root, monkeypatch):
        monkeypatch.setattr(sb, "COOKIES_ENABLED", False)
        fake = BotBlockingYtDlp(root / "downloads", accepts={"firefox"})
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "installed_browsers", lambda: ["firefox"])
        with pytest.raises(RuntimeError, match="switched off"):
            sb.download("https://x/AAA", root / "downloads")
        assert all("--cookies-from-browser" not in c for c in fake.calls)

    def test_explicit_browser_overrides_detection(self, root, monkeypatch):
        monkeypatch.setattr(sb, "COOKIES_FROM_BROWSER", "brave")
        fake = BotBlockingYtDlp(root / "downloads", accepts={"brave"})
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "installed_browsers", lambda: ["firefox"])
        assert sb.download("https://x/AAA", root / "downloads").name == "AAA.mp4"

    def test_real_errors_are_not_retried_with_cookies(self, root, monkeypatch):
        """A private video is not a bot block. Retrying it against every
        installed browser is pure latency for a guaranteed failure."""
        fake = FakeYtDlp(root / "downloads", fail_ids={"AAA"},
                         exc=RuntimeError("Video unavailable. This video is private"))
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "installed_browsers",
                            lambda: ["firefox", "chrome"])
        with pytest.raises(RuntimeError, match="private"):
            sb.download("https://youtube.com/shorts/AAA", root / "downloads")
        assert len(fake.calls) == 1

    def test_message_names_a_fix_when_nothing_works(self, root, monkeypatch):
        fake = BotBlockingYtDlp(root / "downloads", accepts=set(),
                                unreadable={"firefox"})
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "installed_browsers", lambda: ["firefox"])
        with pytest.raises(RuntimeError) as e:
            sb.download("https://x/AAA", root / "downloads")
        assert "Full Disk Access" in str(e.value)

    def test_message_when_no_browser_is_installed(self, root, monkeypatch):
        fake = BotBlockingYtDlp(root / "downloads", accepts=set())
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "installed_browsers", lambda: [])
        with pytest.raises(RuntimeError) as e:
            sb.download("https://x/AAA", root / "downloads")
        assert "no browser profile" in str(e.value)

    def test_download_succeeds_but_writes_nothing(self, root, monkeypatch):
        """Dropped connection: exit 0, no file. Must not return someone
        else's video."""
        (root / "downloads" / "OTHER.mp4").write_bytes(b"x" * 10)
        fake = FakeYtDlp(root / "downloads", produce=False)
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        with pytest.raises(RuntimeError, match="no file appeared"):
            sb.download("https://x/AAA", root / "downloads")

    def test_partial_download_is_not_treated_as_complete(self, root, monkeypatch):
        """A dropped connection leaves a .part file, which must be ignored."""
        (root / "downloads" / "AAA.mp4.part").write_bytes(b"x" * 10)
        fake = FakeYtDlp(root / "downloads", produce=False)
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        with pytest.raises(RuntimeError):
            sb.download("https://x/AAA", root / "downloads")

    def test_zero_byte_cache_is_not_reused(self, root, monkeypatch):
        (root / "downloads" / "AAA.mp4").write_bytes(b"")
        fake = FakeYtDlp(root / "downloads")
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        got = sb.download("https://x/AAA", root / "downloads")
        assert got.stat().st_size > 0

    def test_cache_hit_returns_the_requested_video(self, root, monkeypatch):
        """Regression: used to return the most recently touched file."""
        d = root / "downloads"
        for vid in ("AAA", "BBB", "CCC"):
            (d / f"{vid}.mp4").write_bytes(b"x" * 10)
        import os
        os.utime(d / "CCC.mp4", (time.time(), time.time()))
        fake = FakeYtDlp(d)
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        assert sb.download("https://x/AAA", d).name == "AAA.mp4"

    def test_cache_hit_makes_no_download_call(self, root, monkeypatch):
        d = root / "downloads"
        (d / "AAA.mp4").write_bytes(b"x" * 10)
        fake = FakeYtDlp(d)
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        sb.download("https://x/AAA", d)
        downloads = [c for c in fake.calls if "--print" not in c]
        assert downloads == [], "cached video should not be re-fetched"

    @needs_ffmpeg
    def test_one_bad_link_does_not_abort_the_batch(self, root, monkeypatch):
        d = root / "downloads"
        for vid in ("AAA", "CCC"):
            make_clip(d / f"{vid}.mp4", seconds=4)
        real = sb.run
        fake = FakeYtDlp(d, fail_ids={"BBB"},
                         exc=RuntimeError("HTTP Error 429"))

        def router(cmd, **kw):
            return fake(cmd, **kw) if cmd[0] == "yt-dlp" else real(cmd, **kw)

        monkeypatch.setattr(sb, "run", router)
        monkeypatch.setattr(sb, "have", lambda b: True)
        n = sb.process_all(root, extra_urls=[
            "https://x/AAA", "https://x/BBB", "https://x/CCC"], force=True)
        assert n == 2
        # outputs are numbered, so the point here is that the good two both
        # landed and the failing one did not consume a number
        names = sorted((p.name for p in (root / "output").iterdir()),
                       key=lambda s: int(s.split(".")[0]))
        assert names == ["1.mp4", "2.mp4"]

    def test_failed_item_is_not_marked_done(self, root, monkeypatch):
        fake = FakeYtDlp(root / "downloads", fail_ids={"AAA"},
                         exc=RuntimeError("HTTP Error 500"))
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        sb.process_all(root, extra_urls=["https://x/AAA"])
        state = sb.State(root / "state.json")
        assert state.is_done("url:https://x/AAA") is False, \
            "a failure must stay retryable"

    def test_subprocess_stdin_is_detached(self, monkeypatch):
        """Regression: ffmpeg used to eat links queued in the terminal."""
        seen = {}

        def capture(cmd, **kw):
            seen.update(kw)
            class P:
                returncode = 0
                stdout = ""
                stderr = ""
            return P()

        monkeypatch.setattr(sb.subprocess, "run", capture)
        sb.run(["ffmpeg", "-version"])
        assert seen.get("stdin") is subprocess.DEVNULL

    def test_every_ffmpeg_invocation_passes_nostdin(self):
        """Count argv list literals only, not have("ffmpeg") probes."""
        src = Path(sb.__file__).read_text()
        calls = src.count('["ffmpeg"')
        guarded = src.count('["ffmpeg", "-nostdin"')
        assert calls > 0
        assert calls == guarded, f"{calls - guarded} ffmpeg call(s) missing -nostdin"


# ==========================================================================
# 3. concurrency and race conditions
# ==========================================================================


class TestConcurrency:

    def test_two_states_do_not_lose_writes(self, tmp_path):
        """Two runs open state.json at once. Neither may erase the other."""
        p = tmp_path / "state.json"
        a = sb.State(p)
        b = sb.State(p)
        a.mark("first")
        b.mark("second")
        final = sb.State(p)
        assert final.is_done("first"), "first write was lost"
        assert final.is_done("second"), "second write was lost"

    def test_threaded_marks_all_survive(self, tmp_path):
        p = tmp_path / "state.json"
        errors = []

        def worker(i):
            try:
                sb.State(p).mark(f"key-{i}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        final = sb.State(p)
        missing = [i for i in range(25) if not final.is_done(f"key-{i}")]
        assert not missing, f"lost {len(missing)} of 25 concurrent marks"

    def test_state_file_never_left_corrupt(self, tmp_path):
        """A reader must never catch a half-written file."""
        p = tmp_path / "state.json"
        sb.State(p).mark("seed")
        stop = threading.Event()
        bad = []

        def writer():
            i = 0
            while not stop.is_set():
                sb.State(p).mark(f"w{i}")
                i += 1

        def reader():
            while not stop.is_set():
                try:
                    text = p.read_text()
                    if text:
                        json.loads(text)
                except json.JSONDecodeError as e:
                    bad.append(str(e))
                except FileNotFoundError:
                    bad.append("file vanished")

        w = threading.Thread(target=writer)
        r = threading.Thread(target=reader)
        w.start(); r.start()
        time.sleep(0.6)
        stop.set()
        w.join(); r.join()
        assert not bad, f"state.json was readable in a broken state: {bad[:3]}"

    @needs_ffmpeg
    def test_concurrent_runs_do_not_overwrite_each_others_output(self, root):
        """Two runs splicing the same source must not collide on a filename."""
        src = make_clip(root / "inbox" / "same.mp4", seconds=4)
        results = []
        errors = []

        def worker():
            try:
                work = Path(sb.tempfile.mkdtemp())
                out = root / "output"
                out.mkdir(exist_ok=True)
                target = sb.reserve_output_path(out, "same")
                sb.splice(src, root / "interruption.mp4", target, work)
                results.append(target)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        assert len({p.name for p in results}) == 4, \
            f"filenames collided: {[p.name for p in results]}"
        for p in results:
            assert p.exists() and p.stat().st_size > 0

    def test_duplicate_links_in_one_paste_run_once(self, root, monkeypatch):
        fake = FakeYtDlp(root / "downloads")
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        urls = ["https://x/AAA"] * 5
        sb.process_all(root, extra_urls=urls, force=True)
        id_lookups = [c for c in fake.calls if "--print" in c]
        assert len(id_lookups) <= 1, "same link resolved more than once"

    def test_work_dirs_are_unique_per_job(self, monkeypatch):
        seen = set()
        for _ in range(50):
            d = sb.tempfile.mkdtemp(prefix="shortsbot-")
            assert d not in seen
            seen.add(d)
            shutil.rmtree(d, ignore_errors=True)


# ==========================================================================
# 4. large payloads and high volume
# ==========================================================================


class TestScale:

    def test_ten_thousand_urls_parse_quickly(self):
        lines = [f"https://x/v{i}" for i in range(10_000)]
        t0 = time.monotonic()
        out = sb.extract_urls(lines)
        elapsed = time.monotonic() - t0
        assert len(out) == 10_000
        assert elapsed < 5.0, f"parsing took {elapsed:.1f}s"

    def test_dedupe_of_many_duplicates_is_not_quadratic(self):
        lines = ["https://x/same"] * 20_000
        t0 = time.monotonic()
        out = sb.extract_urls(lines)
        elapsed = time.monotonic() - t0
        assert out == ["https://x/same"]
        assert elapsed < 5.0, f"dedupe took {elapsed:.1f}s"

    def test_large_state_lookup_is_not_quadratic(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text(json.dumps({"done": [f"url:https://x/v{i}"
                                          for i in range(20_000)]}))
        s = sb.State(p)
        t0 = time.monotonic()
        for i in range(0, 20_000, 20):
            s.is_done(f"url:https://x/v{i}")
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"1000 lookups over 20k entries took {elapsed:.1f}s"

    def test_marking_many_keys_is_not_quadratic(self, tmp_path):
        p = tmp_path / "state.json"
        s = sb.State(p)
        t0 = time.monotonic()
        for i in range(2_000):
            s.mark(f"key-{i}")
        elapsed = time.monotonic() - t0
        assert elapsed < 10.0, f"2000 marks took {elapsed:.1f}s"

    def test_very_long_urls_file(self, root, offline):
        big = "\n".join(f"# comment {i}" for i in range(50_000))
        (root / "urls.txt").write_text(big + "\n")
        assert sb.process_all(root) == 0

    def test_huge_inbox_listing(self, root, offline):
        for i in range(2_000):
            (root / "inbox" / f"note{i}.txt").write_text("x")
        t0 = time.monotonic()
        sb.process_all(root)
        assert time.monotonic() - t0 < 10.0

    def test_pathological_filename_lengths(self, root, offline):
        long_stem = "a" * 300
        assert len(sb.slugify(long_stem)) <= 60

    def test_two_long_names_sharing_a_prefix_get_distinct_outputs(self, tmp_path):
        out = tmp_path / "output"
        out.mkdir()
        stem = "x" * 80
        first = sb.reserve_output_path(out, sb.slugify(stem + "-one"))
        first.write_bytes(b"1")
        second = sb.reserve_output_path(out, sb.slugify(stem + "-two"))
        assert first != second, "long names collapsed onto one output file"

    def test_extract_urls_with_one_enormous_line(self):
        line = " ".join(f"https://x/v{i}" for i in range(5_000))
        out = sb.extract_urls([line])
        assert len(out) == 5_000

    def test_state_file_stays_valid_after_many_writes(self, tmp_path):
        p = tmp_path / "state.json"
        s = sb.State(p)
        for i in range(500):
            s.mark(f"k{i}")
        json.loads(p.read_text())
        assert sb.State(p).is_done("k499")


# ==========================================================================
# end-to-end sanity, guarding the fixes made so far
# ==========================================================================


class TestResilience:
    """24/7 behaviour: backoff, cache bounds, log structure."""

    def test_repeated_failure_backs_off_instead_of_hammering(
            self, root, monkeypatch):
        """The storm case: a 429ing host must not be hit every pass."""
        fake = FakeYtDlp(root / "downloads", fail_ids={"DEAD"},
                         exc=RuntimeError("HTTP Error 429"))
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        (root / "urls.txt").write_text("https://x/DEAD\n")

        for _ in range(10):                       # ten watch passes
            sb.process_all(root)

        attempts = [c for c in fake.calls if "--print" in c]
        assert len(attempts) == 1, \
            f"backoff not applied: {len(attempts)} attempts in 10 passes"

    def test_backoff_grows_exponentially(self, tmp_path):
        p = tmp_path / "state.json"
        s = sb.State(p)
        waits = []
        for _ in range(4):
            s.record_failure("k", "boom")
            waits.append(s.retry_wait("k"))
        assert waits == sorted(waits), f"delay did not grow: {waits}"
        assert waits[-1] > waits[0]

    def test_backoff_is_capped(self, tmp_path):
        p = tmp_path / "state.json"
        s = sb.State(p)
        for _ in range(3):
            s.record_failure("k", "boom")
        assert s.retry_wait("k") <= sb.RETRY_MAX_SECONDS

    def test_gives_up_after_the_limit(self, tmp_path):
        p = tmp_path / "state.json"
        s = sb.State(p)
        for _ in range(sb.RETRY_GIVE_UP_AFTER):
            s.record_failure("k", "boom")
        assert s.retry_wait("k") < 0, "should be parked, not retried forever"

    def test_success_clears_the_backoff(self, tmp_path):
        p = tmp_path / "state.json"
        s = sb.State(p)
        s.record_failure("k", "boom")
        assert s.retry_wait("k") > 0
        s.clear_failure("k")
        assert s.retry_wait("k") == 0

    def test_manual_paste_bypasses_backoff(self, root, monkeypatch):
        """force=True means a person asked for it right now."""
        fake = FakeYtDlp(root / "downloads", fail_ids={"DEAD"},
                         exc=RuntimeError("HTTP Error 429"))
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        for _ in range(3):
            sb.process_all(root, extra_urls=["https://x/DEAD"], force=True)
        attempts = [c for c in fake.calls if "--print" in c]
        assert len(attempts) == 3, "a human retry must always be honoured"

    def test_failure_reason_is_recorded(self, tmp_path):
        p = tmp_path / "state.json"
        s = sb.State(p)
        s.record_failure("k", "HTTP Error 429: Too Many Requests")
        saved = json.loads(p.read_text())
        assert "429" in saved["failures"]["k"]["reason"]

    def test_cache_pruned_by_age(self, tmp_path):
        d = tmp_path / "downloads"
        d.mkdir()
        import os
        old = d / "old.mp4"
        old.write_bytes(b"x" * 100)
        ancient = time.time() - 99 * 86400
        os.utime(old, (ancient, ancient))
        fresh = d / "fresh.mp4"
        fresh.write_bytes(b"x" * 100)

        sb.prune_cache(d, max_age_days=30, max_gb=100)
        assert not old.exists(), "stale cache entry survived"
        assert fresh.exists(), "fresh cache entry was wrongly removed"

    def test_cache_pruned_by_total_size(self, tmp_path):
        d = tmp_path / "downloads"
        d.mkdir()
        import os
        for i in range(5):
            f = d / f"v{i}.mp4"
            f.write_bytes(b"x" * 1000)
            t = time.time() - (5 - i) * 3600      # v0 oldest
            os.utime(f, (t, t))
        sb.prune_cache(d, max_age_days=999, max_gb=3000 / 1024 ** 3)
        left = sorted(p.name for p in d.iterdir())
        assert len(left) <= 3, f"size cap not enforced: {left}"
        assert "v4.mp4" in left, "newest should be kept"
        assert "v0.mp4" not in left, "oldest should go first"

    def test_prune_survives_unreadable_folder(self, tmp_path):
        assert sb.prune_cache(tmp_path / "does-not-exist") == 0

    def test_log_file_has_date_level_and_function(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb, "_log_configured", False)
        logger = sb._logger
        old_handlers = list(logger.handlers)
        for h in old_handlers:
            logger.removeHandler(h)
        try:
            path = sb.setup_logging(tmp_path)
            assert path is not None
            sb.log("normal thing")
            sb.warn("worrying thing")
            sb.error("broken thing")
            for h in logger.handlers:
                h.flush()
            text = path.read_text()
            assert "INFO" in text and "WARNING" in text and "ERROR" in text
            # full date, not just a clock time
            assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text)
            # the originating function, so a failure can be located
            assert "test_log_file" in text or "log" in text
        finally:
            for h in list(logger.handlers):
                logger.removeHandler(h)
            for h in old_handlers:
                logger.addHandler(h)

    def test_missing_interruption_warns_once_not_every_pass(
            self, tmp_path, monkeypatch):
        r = tmp_path / "data"
        (r / "inbox").mkdir(parents=True)
        monkeypatch.setattr(sb, "_warned_no_interruption", False)
        seen = []
        monkeypatch.setattr(sb, "warn", lambda m: seen.append(m))
        monkeypatch.setattr(sb, "debug", lambda m: None)
        for _ in range(5):
            sb.process_all(r)
        assert len(seen) == 1, \
            f"repeated the same warning {len(seen)} times in 5 passes"


class TestFastPath:
    """Stream-copy splicing, and the fallback when it isn't legal."""

    def test_youtube_id_read_from_url_without_network(self):
        cases = {
            "https://www.youtube.com/shorts/dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?feature=x&v=dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?t=5": "dQw4w9WgXcQ",
            "https://youtube.com/embed/dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "https://example.com/video/123": None,
            "not a url": None,
        }
        for url, expected in cases.items():
            assert sb.youtube_id(url) == expected, url

    def test_youtube_link_makes_no_id_lookup_request(self, root, monkeypatch):
        """Regression: every link used to cost an extra network round trip."""
        d = root / "downloads"
        fake = FakeYtDlp(d)
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        (d / "dQw4w9WgXcQ.mp4").write_bytes(b"x" * 10)
        sb.download("https://www.youtube.com/shorts/dQw4w9WgXcQ", d)
        assert fake.calls == [], "a YouTube link should need no network call when cached"

    @needs_ffmpeg
    def test_copy_path_produces_correct_duration(self, tmp_path):
        src = make_clip_gop(tmp_path / "src.mp4", seconds=20)
        mid = make_clip_gop(tmp_path / "interruption.mp4", seconds=3)
        out = tmp_path / "out.mp4"
        work = tmp_path / "w"
        work.mkdir()
        sb.splice(src, mid, out, work)
        total = sb.duration_of(out)
        assert abs(total - 23) < 0.5, f"expected ~23s, got {total:.2f}"

    @needs_ffmpeg
    def test_copy_path_is_lossless(self, tmp_path):
        """The source's own frames must come through untouched.

        A re-encode always loses data, so the giveaway is that the copied
        output keeps roughly the source's bitrate rather than shrinking.
        """
        src = make_clip_gop(tmp_path / "src.mp4", seconds=20)
        mid = make_clip_gop(tmp_path / "interruption.mp4", seconds=3)
        work = tmp_path / "w"
        work.mkdir()

        fast = tmp_path / "fast.mp4"
        sb.splice(src, mid, fast, work, allow_copy=True)
        shutil.rmtree(work)
        work.mkdir()
        slow = tmp_path / "slow.mp4"
        sb.splice(src, mid, slow, work, allow_copy=False)

        src_rate = src.stat().st_size / sb.duration_of(src)
        fast_rate = fast.stat().st_size / sb.duration_of(fast)
        assert fast_rate > src_rate * 0.8, \
            "fast path lost data, so it re-encoded rather than copied"

    @needs_ffmpeg
    def test_copy_path_is_faster_than_reencoding(self, tmp_path):
        src = make_clip_gop(tmp_path / "src.mp4", seconds=20)
        mid = make_clip_gop(tmp_path / "interruption.mp4", seconds=3)
        work = tmp_path / "w"

        work.mkdir()
        t0 = time.monotonic()
        sb.splice(src, mid, tmp_path / "fast.mp4", work, allow_copy=True)
        fast = time.monotonic() - t0
        shutil.rmtree(work)

        work.mkdir()
        t0 = time.monotonic()
        sb.splice(src, mid, tmp_path / "slow.mp4", work, allow_copy=False)
        slow = time.monotonic() - t0

        assert fast < slow, f"fast path ({fast:.2f}s) not faster than re-encode ({slow:.2f}s)"

    @needs_ffmpeg
    def test_cut_lands_exactly_on_a_keyframe(self, tmp_path):
        src = make_clip_gop(tmp_path / "src.mp4", seconds=20, gop=60)
        kfs = sb.keyframe_times(src)
        assert len(kfs) > 3
        cut, exact = sb.choose_cut(20.0, kfs)
        assert exact
        assert min(abs(cut - k) for k in kfs) < 0.001, \
            "copy cut must be keyframe exact or ffmpeg rewinds silently"

    def test_choose_cut_without_keyframes_falls_back(self):
        lo, hi = sb.SPLIT_RANGE
        cut, exact = sb.choose_cut(10.0, [])
        assert exact is False
        assert 10.0 * lo <= cut <= 10.0 * hi

    def test_choose_cut_stays_inside_split_range_when_possible(self):
        lo, hi = sb.SPLIT_RANGE
        kfs = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
        for _ in range(50):
            cut, exact = sb.choose_cut(10.0, kfs)
            assert exact
            assert 10.0 * lo <= cut <= 10.0 * hi, \
                f"cut {cut} left the {lo:.0%}-{hi:.0%} window"

    @needs_ffmpeg
    def test_non_h264_source_uses_the_safe_path(self, tmp_path):
        """A codec the copy path can't handle must still produce a video."""
        src = tmp_path / "src.webm"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc=size=320x568:rate=15:duration=8",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
             "-c:v", "libvpx-vp9", "-b:v", "300k", "-deadline", "realtime",
             "-cpu-used", "8", "-c:a", "libopus", "-shortest", str(src)],
            check=True, capture_output=True)
        assert sb.copy_is_viable(sb.probe_spec(src)) is False
        mid = make_clip_gop(tmp_path / "interruption.mp4", seconds=2)
        work = tmp_path / "w"
        work.mkdir()
        out = tmp_path / "out.mp4"
        sb.splice(src, mid, out, work)
        assert out.exists() and sb.duration_of(out) > 8

    @needs_ffmpeg
    def test_prepared_interruption_is_reused_not_rebuilt(self, tmp_path):
        src = make_clip_gop(tmp_path / "src.mp4", seconds=12)
        mid = make_clip_gop(tmp_path / "interruption.mp4", seconds=2)
        spec = sb.probe_spec(src)
        cache = tmp_path / ".prepared"

        first = sb.prepare_interruption(mid, spec, cache)
        assert first is not None
        stamp = first.stat().st_mtime_ns

        second = sb.prepare_interruption(mid, spec, cache)
        assert second == first
        assert second.stat().st_mtime_ns == stamp, "interruption was re-rendered"

    @needs_ffmpeg
    def test_prepared_interruption_rebuilds_when_source_changes(self, tmp_path):
        src = make_clip_gop(tmp_path / "src.mp4", seconds=12)
        mid = make_clip_gop(tmp_path / "interruption.mp4", seconds=2)
        spec = sb.probe_spec(src)
        cache = tmp_path / ".prepared"
        first = sb.prepare_interruption(mid, spec, cache)
        assert first is not None

        time.sleep(1.1)
        make_clip_gop(mid, seconds=3)                 # user swapped their clip
        rebuilt = sb.prepare_interruption(mid, spec, cache)
        assert rebuilt is not None
        assert abs(sb.duration_of(rebuilt) - 3) < 0.4, "stale interruption reused"

    def test_spec_signature_distinguishes_layouts(self):
        a = {"vcodec": "h264", "width": 1080, "height": 1920, "fps": "30/1",
             "pix_fmt": "yuv420p", "acodec": "aac", "sample_rate": "44100",
             "channels": 2}
        b = dict(a, height=1080)
        c = dict(a, sample_rate="48000")
        assert sb.spec_signature(a) != sb.spec_signature(b)
        assert sb.spec_signature(a) != sb.spec_signature(c)
        assert sb.spec_signature(a) == sb.spec_signature(dict(a))

    def test_encoder_detection_returns_something_usable(self):
        enc = sb.best_encoder()
        assert isinstance(enc, str) and enc
        args = sb.video_encode_args(enc)
        assert "-c:v" in args

    def test_encoder_args_honour_the_configured_preset(self):
        args = sb.video_encode_args("libx264")
        assert "-preset" in args
        assert args[args.index("-preset") + 1] == sb.X264_PRESET
        assert args[args.index("-crf") + 1] == str(sb.X264_CRF)

    @needs_ffmpeg
    def test_temp_files_are_all_cleaned_up(self, root, monkeypatch):
        """Cut files and the concat manifest must not survive the job."""
        make_clip_gop(root / "inbox" / "clip.mp4", seconds=10)
        created = []
        real_mkdtemp = sb.tempfile.mkdtemp

        def spy(*a, **k):
            d = real_mkdtemp(*a, **k)
            created.append(Path(d))
            return d

        monkeypatch.setattr(sb.tempfile, "mkdtemp", spy)
        sb.process_all(root)
        assert created, "no work dir was created"
        for d in created:
            assert not d.exists(), f"temp dir left behind: {d}"

    @needs_ffmpeg
    def test_temp_files_cleaned_up_even_when_the_job_fails(self, root, monkeypatch):
        make_clip_gop(root / "inbox" / "clip.mp4", seconds=10)
        created = []
        real_mkdtemp = sb.tempfile.mkdtemp

        def spy(*a, **k):
            d = real_mkdtemp(*a, **k)
            created.append(Path(d))
            return d

        monkeypatch.setattr(sb.tempfile, "mkdtemp", spy)
        monkeypatch.setattr(sb, "splice",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        sb.process_all(root)
        assert created
        for d in created:
            assert not d.exists(), f"temp dir survived a failure: {d}"

    @needs_ffmpeg
    def test_failed_job_leaves_no_placeholder_output(self, root, monkeypatch):
        make_clip_gop(root / "inbox" / "clip.mp4", seconds=10)
        monkeypatch.setattr(sb, "splice",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        sb.process_all(root)
        leftovers = list((root / "output").iterdir())
        assert leftovers == [], f"reserved output not cleaned up: {leftovers}"

    def test_prefetch_downloads_each_link_once(self, root, monkeypatch):
        d = root / "downloads"
        fake = FakeYtDlp(d)
        monkeypatch.setattr(sb, "run", fake)
        monkeypatch.setattr(sb, "have", lambda b: True)
        urls = [f"https://x/V{i}" for i in range(4)]
        sb.process_all(root, extra_urls=urls, force=True)
        fetches = [c for c in fake.calls if "--print" not in c]
        ids = [c[-1] for c in fetches]
        assert len(ids) == len(set(ids)), f"a link was downloaded twice: {ids}"

    def test_prefetch_pool_is_single_worker(self):
        """Parallel downloads are exactly what the backoff logic exists to avoid."""
        src = Path(sb.__file__).read_text()
        assert "max_workers=1" in src


class TestRegressions:

    def test_split_point_stays_in_middle_third(self):
        vals = [sb.pick_split() for _ in range(5_000)]
        assert min(vals) >= sb.SPLIT_RANGE[0]
        assert max(vals) <= sb.SPLIT_RANGE[1]
        assert len(set(vals)) > 4_000, "split point is not actually varying"

    def test_explicit_split_is_honoured(self):
        assert sb.pick_split(0.5) == 0.5

    @needs_ffmpeg
    def test_audio_is_not_attenuated(self, tmp_path):
        """Regression: mixing against silence cost 3dB and added a fade-in."""
        src = make_clip(tmp_path / "src.mp4", seconds=5)
        dst = tmp_path / "out.mp4"
        sb.normalize(src, dst)

        def mean_db(path, at):
            out = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "info", "-ss", str(at), "-t", "1",
                 "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                capture_output=True, text=True).stderr
            for line in out.splitlines():
                if "mean_volume:" in line:
                    return float(line.split("mean_volume:")[1].split("dB")[0])
            raise AssertionError("no volume reading")

        for t in (0, 1, 2, 3):
            assert abs(mean_db(dst, t) - mean_db(src, t)) < 0.5, \
                f"volume changed at t={t}s"

    @needs_ffmpeg
    def test_silent_source_gets_an_audio_track(self, tmp_path):
        src = make_clip(tmp_path / "mute.mp4", seconds=3, audio=False)
        dst = tmp_path / "out.mp4"
        sb.normalize(src, dst)
        assert sb.has_audio(dst)

    @needs_ffmpeg
    def test_output_geometry_is_normalized(self, tmp_path):
        src = make_clip(tmp_path / "wide.mp4", seconds=3, w=1920, h=1080, fps=24)
        dst = tmp_path / "out.mp4"
        sb.normalize(src, dst)
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(dst)],
            capture_output=True, text=True).stdout.strip()
        assert out == f"{sb.WIDTH},{sb.HEIGHT}"

    @needs_ffmpeg
    def test_spliced_duration_is_the_sum(self, root):
        src = make_clip(root / "inbox" / "clip.mp4", seconds=6)
        n = sb.process_all(root)
        assert n == 1
        out = next((root / "output").iterdir())
        total = sb.duration_of(out)
        expected = 6 + sb.duration_of(root / "interruption.mp4")
        assert abs(total - expected) < 0.5

    @needs_ffmpeg
    def test_processed_file_is_moved_out_of_inbox(self, root):
        make_clip(root / "inbox" / "clip.mp4", seconds=5)
        sb.process_all(root)
        assert not (root / "inbox" / "clip.mp4").exists()
        assert (root / "processed" / "clip.mp4").exists()


# ==========================================================================
# Numbered interruption clips
#
# Clips are versioned by number and the highest wins, so replacing one is a
# drag-and-drop. These pin that down, including the two ways it could quietly
# go wrong: picking by string order (so "10" loses to "2"), and reusing a
# cached render of the previous clip after a swap.
# ==========================================================================


class TestNumberedClips:

    @staticmethod
    def _folder(tmp_path, *names):
        for n in names:
            (tmp_path / n).write_bytes(b"not really a video")
        return tmp_path

    def test_single_numbered_clip_is_found(self, tmp_path):
        d = self._folder(tmp_path, "1.mov")
        assert sb.find_interruption(d).name == "1.mov"

    def test_highest_number_wins(self, tmp_path):
        d = self._folder(tmp_path, "1.mov", "2.mov", "3.mov")
        assert sb.find_interruption(d).name == "3.mov"

    def test_ordering_is_numeric_not_alphabetical(self, tmp_path):
        """The bug this guards: "10" sorts before "2" as a string."""
        d = self._folder(tmp_path, "2.mov", "10.mov")
        assert sb.find_interruption(d).name == "10.mov"

    def test_zero_padding_does_not_change_the_order(self, tmp_path):
        d = self._folder(tmp_path, "007.mp4", "8.mp4")
        assert sb.find_interruption(d).name == "8.mp4"

    def test_extension_is_a_deterministic_tie_break(self, tmp_path):
        """Same number twice must resolve identically on every run."""
        d = self._folder(tmp_path, "2.mov", "2.mp4")
        picked = {sb.find_interruption(d).name for _ in range(10)}
        assert picked == {"2.mp4"}

    def test_mixed_extensions_all_count(self, tmp_path):
        d = self._folder(tmp_path, "1.mp4", "2.webm", "3.mkv", "4.m4v", "5.avi")
        assert sb.find_interruption(d).name == "5.avi"

    def test_uppercase_extension_is_accepted(self, tmp_path):
        d = self._folder(tmp_path, "3.MOV")
        assert sb.find_interruption(d).name == "3.MOV"

    def test_numbers_beat_the_legacy_name(self, tmp_path):
        d = self._folder(tmp_path, "1.mov", "interruption.mp4")
        assert sb.find_interruption(d).name == "1.mov"

    def test_legacy_name_still_works_alone(self, tmp_path):
        d = self._folder(tmp_path, "interruption.mp4")
        assert sb.find_interruption(d).name == "interruption.mp4"

    def test_legacy_name_with_another_extension(self, tmp_path):
        d = self._folder(tmp_path, "interruption.mov")
        assert sb.find_interruption(d).name == "interruption.mov"

    def test_non_video_numbers_are_ignored(self, tmp_path):
        d = self._folder(tmp_path, "1.txt", "2.docx", "3.json")
        assert sb.find_interruption(d) is None

    def test_a_number_wins_over_a_non_video_higher_number(self, tmp_path):
        d = self._folder(tmp_path, "1.mov", "99.txt")
        assert sb.find_interruption(d).name == "1.mov"

    def test_loose_names_are_refused(self, tmp_path):
        """Strict on purpose: a stray video must not become the clip."""
        d = self._folder(tmp_path, "interuption v2.mov", "final cut 3.mp4",
                         "my clip.mov")
        assert sb.find_interruption(d) is None

    def test_empty_folder_gives_none(self, tmp_path):
        assert sb.find_interruption(tmp_path) is None

    def test_missing_folder_gives_none_not_an_error(self, tmp_path):
        assert sb.find_interruption(tmp_path / "does-not-exist") is None

    def test_directories_are_not_mistaken_for_clips(self, tmp_path):
        (tmp_path / "1.mov").mkdir()
        assert sb.find_interruption(tmp_path) is None

    def test_zero_is_a_valid_number(self, tmp_path):
        d = self._folder(tmp_path, "0.mov")
        assert sb.find_interruption(d).name == "0.mov"

    @pytest.mark.parametrize("name,want", [
        ("1.mov", 1), ("42.mp4", 42), ("007.mp4", 7), ("0.mov", 0),
        ("interruption.mp4", None), ("v2.mov", None), ("2 v1.mov", None),
        ("1.2.mov", None), ("-1.mov", None), ("1a.mov", None), ("", None),
    ])
    def test_clip_number_parsing(self, name, want):
        assert sb.clip_number(Path(name)) == want

    def test_process_all_reports_no_clip_when_folder_is_bare(self, tmp_path, capsys):
        sb._warned_no_interruption = False
        assert sb.process_all(tmp_path) == 0

    @needs_ffmpeg
    def test_swapping_clips_does_not_reuse_the_old_render(self, tmp_path):
        """The prep cache is keyed on the clip, not just the source layout.

        Keyed on layout alone, a newer clip whose mtime happened to be older
        (copying a file can preserve its timestamp) would silently reuse the
        previous clip's render, so a whole batch would go out with the wrong
        interruption in it.

        The sources here need a fixed GOP: prepare_interruption only runs on
        the stream-copy path, so a source without keyframes to cut on would
        take the re-encode path and never touch the cache at all, and this
        test would pass without proving anything.
        """
        for d in ("inbox", "output", "processed", "downloads"):
            (tmp_path / d).mkdir()

        def solid(path, colour, seconds):
            subprocess.run(
                ["ffmpeg", "-nostdin", "-y", "-v", "error",
                 "-f", "lavfi", "-i",
                 f"color=c={colour}:size=360x640:rate=30:duration={seconds}",
                 "-f", "lavfi", "-i", f"sine=frequency=800:duration={seconds}",
                 "-c:v", "libx264", "-g", "30", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest",
                 str(path)], check=True, capture_output=True)

        solid(tmp_path / "1.mp4", "blue", 3)
        make_clip_gop(tmp_path / "inbox" / "a.mp4", seconds=10)
        assert sb.process_all(tmp_path) == 1
        first = sb.duration_of(tmp_path / "output" / "1.mp4")
        assert abs(first - 13) < 0.6, f"clip 1 (3s) not used, got {first}s"

        cached = sorted(p.name for p in (tmp_path / ".prepared").glob("*.mp4"))
        assert cached, "the copy path never ran, so this test proves nothing"

        # clip 2 is a different length, and backdated so a layout-only cache
        # key would consider the existing render still fresh
        solid(tmp_path / "2.mp4", "red", 6)
        os.utime(tmp_path / "2.mp4", (0, 0))
        assert sb.find_interruption(tmp_path).name == "2.mp4"

        make_clip_gop(tmp_path / "inbox" / "b.mp4", seconds=10)
        assert sb.process_all(tmp_path) == 1
        second = sb.duration_of(tmp_path / "output" / "2.mp4")

        # 10 + 6 with clip 2, not 10 + 3 as it would be if clip 1 were reused
        assert abs(second - 16) < 0.6, \
            f"expected clip 2 (6s) to be used, output was {second}s"

        after = sorted(p.name for p in (tmp_path / ".prepared").glob("*.mp4"))
        assert len(after) == len(cached) + 1, \
            f"clip 2 should have been prepared separately, cache went {cached} -> {after}"


# ==========================================================================
# Numbered output files
#
# Exports are named 1.mp4, 2.mp4, 3.mp4 rather than after the source video.
# The things that could go wrong quietly: restarting at 1 and overwriting an
# earlier run, ordering by string so 10 lands before 2, and two concurrent
# runs claiming the same number.
# ==========================================================================


class TestNumberedOutputs:

    @staticmethod
    def _out(tmp_path, *names):
        d = tmp_path / "output"
        d.mkdir(exist_ok=True)
        for n in names:
            (d / n).write_bytes(b"x")
        return d

    def test_first_export_is_one(self, tmp_path):
        assert sb.reserve_numbered_output(self._out(tmp_path)).name == "1.mp4"

    def test_numbering_continues_across_runs(self, tmp_path):
        d = self._out(tmp_path, "1.mp4", "2.mp4")
        assert sb.reserve_numbered_output(d).name == "3.mp4"

    def test_does_not_restart_and_overwrite(self, tmp_path):
        """Restarting at 1 would destroy an earlier run's work."""
        d = self._out(tmp_path, "1.mp4", "2.mp4", "3.mp4")
        for _ in range(5):
            assert sb.reserve_numbered_output(d).name != "1.mp4"

    def test_ordering_is_numeric_not_alphabetical(self, tmp_path):
        d = self._out(tmp_path, "9.mp4", "10.mp4")
        assert sb.reserve_numbered_output(d).name == "11.mp4"

    def test_gaps_are_not_refilled(self, tmp_path):
        """Reusing 2.mp4 after deletion would make "2" mean two videos."""
        d = self._out(tmp_path, "1.mp4", "3.mp4")
        assert sb.reserve_numbered_output(d).name == "4.mp4"

    def test_legacy_named_files_do_not_block_numbering(self, tmp_path):
        d = self._out(tmp_path, "abc-spliced.mp4", "xyz-spliced.mp4")
        assert sb.reserve_numbered_output(d).name == "1.mp4"

    def test_mixed_old_and_new_names(self, tmp_path):
        d = self._out(tmp_path, "abc-spliced.mp4", "4.mp4")
        assert sb.reserve_numbered_output(d).name == "5.mp4"

    def test_non_mp4_numbers_are_ignored(self, tmp_path):
        d = self._out(tmp_path, "7.txt", "2.mp4")
        assert sb.reserve_numbered_output(d).name == "3.mp4"

    def test_the_reserved_file_exists_immediately(self, tmp_path):
        """It has to be on disk, or a second caller takes the same number."""
        d = self._out(tmp_path)
        p = sb.reserve_numbered_output(d)
        assert p.exists()
        assert sb.reserve_numbered_output(d).name == "2.mp4"

    def test_output_dir_is_created_if_missing(self, tmp_path):
        p = sb.reserve_numbered_output(tmp_path / "brand" / "new")
        assert p.name == "1.mp4" and p.parent.is_dir()

    def test_concurrent_runs_never_collide(self, tmp_path):
        d = self._out(tmp_path)
        got = []
        lock = threading.Lock()

        def claim():
            p = sb.reserve_numbered_output(d)
            with lock:
                got.append(p.name)

        threads = [threading.Thread(target=claim) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(got)) == 20, f"two runs claimed the same number: {got}"
        assert sorted(int(n.split(".")[0]) for n in got) == list(range(1, 21))

    @pytest.mark.parametrize("names,want", [
        ([], 0),
        (["1.mp4"], 1),
        (["1.mp4", "2.mp4", "3.mp4"], 3),
        (["9.mp4", "10.mp4"], 10),
        (["abc-spliced.mp4"], 0),
        (["5.txt"], 0),
        (["0.mp4"], 0),
    ])
    def test_highest_output_number(self, tmp_path, names, want):
        assert sb.highest_output_number(self._out(tmp_path, *names)) == want

    def test_highest_of_missing_folder_is_zero(self, tmp_path):
        assert sb.highest_output_number(tmp_path / "nope") == 0

    @needs_ffmpeg
    def test_a_real_run_produces_numbered_files(self, tmp_path):
        for d in ("inbox", "output", "processed", "downloads"):
            (tmp_path / d).mkdir()
        make_clip(tmp_path / "1.mp4", seconds=2, w=320, h=320)   # the clip
        make_clip(tmp_path / "inbox" / "first.mp4", seconds=5)
        make_clip(tmp_path / "inbox" / "second.mp4", seconds=5)
        assert sb.process_all(tmp_path) == 2
        names = sorted(p.name for p in (tmp_path / "output").glob("*.mp4"))
        assert names == ["1.mp4", "2.mp4"], names

        # a second run must carry on, not overwrite
        make_clip(tmp_path / "inbox" / "third.mp4", seconds=5)
        assert sb.process_all(tmp_path) == 1
        names = sorted((p.name for p in (tmp_path / "output").glob("*.mp4")),
                       key=lambda n: int(n.split(".")[0]))
        assert names == ["1.mp4", "2.mp4", "3.mp4"], names
