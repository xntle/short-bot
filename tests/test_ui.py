"""Tests for the terminal presentation layer.

The UI has no business breaking a batch, so these check two things above all:
that every panel stays inside its own borders, and that a raw failure never
reaches the screen verbatim.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ui  # noqa: E402
import shorts_bot as sb  # noqa: E402


def widths(capsys):
    out = capsys.readouterr().out
    return [len(ui.plain(line)) for line in out.splitlines() if line.strip()]


class TestLayout:

    def test_panel_lines_are_all_the_same_width(self, capsys):
        ui.panel("a title", ["short", "a considerably longer line of text"])
        assert set(widths(capsys)) == {ui.WIDTH}

    def test_panel_with_no_title(self, capsys):
        ui.panel("", ["body"])
        assert set(widths(capsys)) == {ui.WIDTH}

    def test_over_long_content_does_not_break_the_box(self, capsys):
        ui.panel("t", ["x" * 500])
        w = widths(capsys)
        assert max(w) <= ui.WIDTH + 1, f"panel burst its border: {max(w)}"

    def test_header_fits(self, capsys):
        ui.header(made=99999, cache_gb=123.456)
        assert set(widths(capsys)) == {ui.WIDTH}

    def test_summary_rows_stay_inside_the_border(self, capsys):
        ui.summary([
            ("success", "a" * 200, "3.4s"),
            ("skipped", "short", "already made"),
            ("parked", "b" * 60, "set aside"),
        ])
        w = widths(capsys)
        assert len(set(w)) == 1, f"ragged summary widths: {sorted(set(w))}"

    def test_summary_long_title_is_truncated_not_wrapped(self, capsys):
        ui.summary([("success", "z" * 300, "9.9s")])
        out = capsys.readouterr().out
        assert "9.9s" in out
        assert len(out.strip().splitlines()) == 6

    def test_summary_of_nothing_prints_nothing(self, capsys):
        ui.summary([])
        assert capsys.readouterr().out == ""

    def test_summary_counts_each_kind(self, capsys):
        ui.summary([
            ("success", "a", ""), ("success", "b", ""),
            ("retry", "c", ""), ("skipped", "d", ""),
        ])
        out = ui.plain(capsys.readouterr().out)
        assert "2 made" in out and "1 retrying" in out and "1 skipped" in out

    def test_menu_renders_every_option(self, capsys):
        ui.menu()
        out = ui.plain(capsys.readouterr().out)
        for key in ("[1]", "[2]", "[3]", "[4]", "[5]", "[Q]"):
            assert key in out

    def test_badge_is_fixed_width(self):
        widths_seen = {len(ui.plain(ui.badge(k)))
                       for k in ("success", "retry", "skipped", "failed", "parked")}
        assert len(widths_seen) == 1, "badges must align in a column"

    def test_unknown_badge_does_not_crash(self):
        assert ui.plain(ui.badge("weird-state"))


class TestProgress:

    def test_non_tty_prints_plain_lines(self, capsys):
        p = ui.Progress("a video", 1, 3)
        p.start("Download")
        p.finish("Download", "4.2 MB")
        out = capsys.readouterr().out
        assert "\033[" not in out or not sys.stdout.isatty()
        assert "Download" in out

    def test_phases_track_state(self):
        p = ui.Progress("v", 1, 1)
        assert p.state["Splice"] == "waiting"
        p.start("Splice")
        assert p.state["Splice"] == "running"
        p.update("Splice", 0.5)
        assert p.pct["Splice"] == 0.5
        p.finish("Splice")
        assert p.state["Splice"] == "done" and p.pct["Splice"] == 1.0

    def test_fail_marks_only_that_phase(self):
        p = ui.Progress("v", 1, 1)
        p.start("Download")
        p.fail("Download")
        assert p.state["Download"] == "failed"
        assert p.state["Splice"] == "waiting"

    @pytest.mark.parametrize("fraction", [-5.0, 0.0, 0.5, 1.0, 99.0])
    def test_bar_never_overflows(self, fraction):
        p = ui.Progress("v", 1, 1)
        p.start("Splice")
        p.update("Splice", fraction)
        bar = ui.plain(p._bar("Splice"))
        assert bar.count("█") <= p.BAR
        assert bar.count("█") + bar.count("░") == p.BAR

    def test_open_is_idempotent(self, capsys):
        p = ui.Progress("v", 1, 1)
        p.open()
        first = capsys.readouterr().out
        p.open()
        assert capsys.readouterr().out == "", "panel drawn twice"
        assert first


class TestTiming:

    def test_elapsed_measures_a_finished_phase(self):
        p = ui.Progress("v", 1, 1)
        p.start("Splice")
        time.sleep(0.05)
        p.finish("Splice")
        assert 0.04 <= p.elapsed("Splice") < 1.0

    def test_elapsed_reports_a_running_phase_so_far(self):
        p = ui.Progress("v", 1, 1)
        p.start("Download")
        time.sleep(0.03)
        first = p.elapsed("Download")
        time.sleep(0.03)
        assert p.elapsed("Download") > first

    def test_a_phase_that_never_ran_reports_zero(self):
        p = ui.Progress("v", 1, 1)
        assert p.elapsed("Export") == 0.0
        assert "Export" not in p.timings()

    def test_finishing_freezes_the_clock(self):
        p = ui.Progress("v", 1, 1)
        p.start("Export")
        p.finish("Export")
        settled = p.elapsed("Export")
        time.sleep(0.05)
        assert p.elapsed("Export") == settled

    def test_a_failed_phase_is_still_timed(self):
        p = ui.Progress("v", 1, 1)
        p.start("Download")
        time.sleep(0.03)
        p.fail("Download")
        assert p.elapsed("Download") > 0
        assert "Download" in p.timings()

    def test_total_is_the_sum_of_the_phases(self):
        p = ui.Progress("v", 1, 1)
        for phase in ("Download", "Splice", "Export"):
            p.start(phase)
            time.sleep(0.02)
            p.finish(phase)
        assert abs(p.total_seconds() - sum(p.timings().values())) < 1e-9
        assert p.total_seconds() >= 0.05

    def test_uses_a_monotonic_clock(self):
        """The wall clock jumps when the system clock is adjusted, so a
        duration measured with it can come out negative. Parse for real call
        sites rather than grepping, which matches the comments explaining this.
        """
        import ast
        tree = ast.parse(Path(ui.__file__).read_text())
        calls = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and getattr(node.func.value, "id", "") == "time"):
                calls.append(node.func.attr)
        assert "monotonic" in calls
        assert "time" not in calls, "wall clock used to measure a duration"

    def test_close_prints_the_per_video_breakdown(self, capsys):
        p = ui.Progress("a video", 1, 1)
        p.start("Splice")
        p.finish("Splice")
        p.close()
        out = ui.plain(capsys.readouterr().out)
        assert "splice" in out and "total" in out

    def test_profile_panel_shows_every_phase_and_a_share(self, capsys):
        ui.profile([
            {"Download": 4.0, "Splice": 4.0, "Export": 2.0},
            {"Download": 2.0, "Splice": 6.0, "Export": 2.0},
        ])
        out = ui.plain(capsys.readouterr().out)
        assert "Download" in out and "Splice" in out and "Export" in out
        assert "6.00s" in out          # download total
        assert "10.00s" in out         # splice total
        assert "20.00s" in out         # grand total
        assert "10.00s each" in out    # average

    def test_profile_widths_stay_inside_the_border(self, capsys):
        ui.profile([{"Download": 3600.0, "Splice": 0.001, "Export": 12.5}])
        w = widths(capsys)
        assert len(set(w)) == 1, f"ragged timing panel: {sorted(set(w))}"

    def test_profile_of_nothing_prints_nothing(self, capsys):
        ui.profile([])
        assert capsys.readouterr().out == ""

    def test_profile_ignores_zero_length_runs(self, capsys):
        ui.profile([{"Splice": 0.0}])
        assert capsys.readouterr().out == ""

    def test_reporter_collects_a_sample_per_video(self):
        rep = sb.Reporter()
        p = ui.Progress("v", 1, 1)
        p.start("Splice")
        p.finish("Splice")
        rep.current = p
        rep.record_timings("v")
        assert len(rep.samples) == 1
        assert "Splice" in rep.samples[0]

    def test_reporter_ignores_a_video_with_no_timings(self):
        rep = sb.Reporter()
        rep.current = ui.Progress("v", 1, 1)
        rep.record_timings("v")
        assert rep.samples == []

    def test_flush_clears_the_samples(self, capsys):
        rep = sb.Reporter()
        p = ui.Progress("v", 1, 1)
        p.start("Export")
        p.finish("Export")
        rep.current = p
        rep.record_timings("v")
        rep.flush()
        capsys.readouterr()
        assert rep.samples == []


class TestErrorMessages:

    @pytest.mark.parametrize("raw,expect", [
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "bot"),
        ("ERROR: HTTP Error 429: Too Many Requests", "slower pace"),
        ("ERROR: Private video. Sign in if you've been granted access", "public"),
        ("OSError: [Errno 28] No space left on device", "disk is full"),
        ("source too short to split (0.40s)", "too short"),
        ("yt-dlp is not installed", "downloader is missing"),
        ("cannot write into /x: Permission denied", "can't write"),
        ("urlopen error timed out", "connection dropped"),
        ("could not read that link", "didn't look like a video"),
    ])
    def test_known_failures_get_plain_english(self, raw, expect):
        what, advice = ui.explain(raw)
        assert expect in what.lower() or expect in advice.lower()
        assert advice, "every message needs an action"

    def test_unknown_failure_still_gets_a_useful_message(self):
        what, advice = ui.explain("kernel panic in the flux capacitor")
        assert what and "log" in advice

    def test_no_command_line_ever_reaches_the_screen(self, capsys):
        raw = ("command failed: yt-dlp -f bv*[ext=mp4]+ba[ext=m4a] "
               "--merge-output-format mp4 -o /Users/x/%(id)s.%(ext)s\n"
               "ERROR: HTTP Error 429: Too Many Requests")
        ui.show_error(raw, retry_at="14:32")
        out = ui.plain(capsys.readouterr().out)
        for leak in ("yt-dlp", "--merge-output-format", "%(id)s", "ERROR:"):
            assert leak not in out, f"leaked {leak!r} to the user"
        assert "14:32" in out

    def test_parked_message_tells_you_how_to_force_a_retry(self, capsys):
        ui.show_error("HTTP Error 429", parked=True)
        out = ui.plain(capsys.readouterr().out).lower()
        assert "paste it again" in out

    def test_explain_accepts_an_exception_object(self):
        what, _ = ui.explain(RuntimeError("HTTP Error 429: Too Many Requests"))
        assert "slower pace" in what

    def test_explain_survives_empty_input(self):
        what, advice = ui.explain("")
        assert what and advice


class TestReporter:

    def test_titles_are_readable_not_raw(self):
        assert "dQw4w9WgXcQ" in sb._pretty(
            "https://www.youtube.com/shorts/dQw4w9WgXcQ")
        pretty = sb._pretty(
            "YTDown.com_Shorts_The-Forgotten-Corn-Method_Media_1hn_001_1080p")
        assert "YTDown" not in pretty
        assert "Forgotten Corn" in pretty

    def test_pretty_never_returns_empty(self):
        for raw in ("", "___", "https://x/", "a"):
            assert sb._pretty(raw) != ""

    def test_reporter_collects_rows_for_the_summary(self):
        rep = sb.Reporter()
        rep.result("success", "one", "3s")
        rep.result("skipped", "two", "already made")
        assert [r[0] for r in rep.rows] == ["success", "skipped"]

    def test_reporter_failure_classifies_correctly(self, capsys):
        rep = sb.Reporter()
        rep.failed("a", RuntimeError("429"), retry_at="14:32", parked=False)
        rep.failed("b", RuntimeError("429"), retry_at=None, parked=True)
        rep.failed("c", RuntimeError("boom"), retry_at=None, parked=False)
        capsys.readouterr()
        assert [r[0] for r in rep.rows] == ["retry", "parked", "failed"]

    def test_flush_empties_the_rows(self, capsys):
        rep = sb.Reporter()
        rep.result("success", "one", "3s")
        rep.flush()
        capsys.readouterr()
        assert rep.rows == []


class TestDegradation:

    def test_no_color_env_disables_escapes(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        assert ui._colour_ok() is False

    def test_force_color_env_enables_escapes(self, monkeypatch):
        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert ui._colour_ok() is True

    def test_plain_strips_every_escape(self):
        assert ui.plain("\033[32mgreen\033[0m and \033[1mbold\033[0m") == \
            "green and bold"

    def test_progress_bars_do_not_redraw_when_piped(self, capsys):
        p = ui.Progress("v", 1, 1)
        p.live = False
        p.start("Download")
        p.update("Download", 0.4)
        p.finish("Download")
        out = capsys.readouterr().out
        assert "\033[" not in out, "cursor movement leaked into piped output"
