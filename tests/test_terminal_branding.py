"""Offline terminal contracts; no gateway, model, or network access."""
from contextlib import redirect_stdout
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import unittest
from unittest.mock import Mock, patch

import branding
import triglav_art as art

ROOT = Path(__file__).resolve().parents[1]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
CONFIG = {"roles": {"code": "code-alias", "critic": "critic-alias", "review": "review-alias"},
          "model_metadata": {"code-alias": {"display_name": "Qwen"},
                             "critic-alias": {"display_name": "Nemotron"},
                             "review-alias": {"display_name": "GPT-OSS"}}}


class Terminal(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True

    def fileno(self):
        return 2


class BrandingTests(unittest.TestCase):
    def picture(self, **kwargs):
        return branding.render(CONFIG, "topic/example", "abcdef0", width=100, height=40,
                               color=True, **kwargs)

    def test_original_artwork_bytes_preserved(self):
        self.assertEqual(hashlib.sha256((ROOT / "triglav_art.py").read_bytes()).hexdigest(),
                         "3bfc4f36f8c9837aae26f9016ffc8f1460dbe79f8e4579a00f7700994cd74c80")

    def test_palette_and_all_pixels_valid(self):
        self.assertRegex(art.PALETTE, r"^[0-9a-f]{192}$")
        self.assertEqual(len(art.ROWS), 62)
        for row in art.ROWS:
            self.assertEqual(len(row), 40)
            self.assertFalse(set(row) - set(art._ALPHA))
        for char in art._ALPHA:
            self.assertTrue(all(0 <= value <= 255 for value in art._rgb(char)))

    def test_every_half_block_has_correct_rgb_and_reset(self):
        lines = art.art_lines()
        self.assertEqual(len(lines), 31)
        for y, line in enumerate(lines):
            expected = ""
            for top, bottom in zip(art.ROWS[2*y], art.ROWS[2*y+1]):
                fg = ";".join(map(str, art._rgb(top)))
                bg = ";".join(map(str, art._rgb(bottom)))
                expected += f"\x1b[38;2;{fg}m\x1b[48;2;{bg}m\u2580"
            self.assertEqual(line, expected + branding.RESET)
            self.assertEqual(ANSI.sub("", line), "\u2580" * 40)

    def test_portrait_alignment_and_final_reset(self):
        output = self.picture(critic=True, reviewer=True)
        lines = ANSI.sub("", output).splitlines()
        self.assertEqual(len(lines), 31)
        self.assertTrue(all(len(line) <= 99 for line in lines))
        self.assertEqual(lines[0], "\u2580" * 40 + "   TRIGLAV")
        self.assertEqual(lines[4][43:], "BRANCH      topic/example")
        self.assertEqual(lines[8][43:], "EXECUTOR    Qwen")
        self.assertTrue(output.endswith(branding.RESET + "\n"))

    def test_status_is_only_starting_or_dry_run(self):
        self.assertIn("STARTING", self.picture())
        self.assertIn("DRY RUN", self.picture(dry_run=True))
        for word in ("READY", "VERIFIED", "SUCCESS"):
            self.assertNotIn(word, self.picture())

    def test_single_model_does_not_advertise_disabled_roles(self):
        output = ANSI.sub("", self.picture())
        self.assertIn("SINGLE-MODEL", output)
        self.assertIn("CRITIC      OFF", output)
        self.assertIn("REVIEWER    OFF", output)
        self.assertNotIn("Nemotron", output)
        self.assertNotIn("GPT-OSS", output)

    def test_three_model_and_two_model_follow_enabled_roles(self):
        for critic, reviewer, mode in ((True, True, "THREE-MODEL"),
                                       (True, False, "TWO-MODEL"),
                                       (False, True, "TWO-MODEL")):
            output = self.picture(critic=critic, reviewer=reviewer)
            self.assertIn(mode, output)
            self.assertEqual("Nemotron" in output, critic)
            self.assertEqual("GPT-OSS" in output, reviewer)

    def test_configured_alias_and_metadata_are_dynamic(self):
        config = {"roles": {"code": "replacement"}}
        self.assertEqual(branding.runtime_info(config)[1]["code"], "replacement")
        self.assertEqual(branding.runtime_info({})[0], "UNCONFIGURED")

    def test_small_viewports_never_wrap_or_overflow(self):
        for width, height in ((77, 40), (100, 31), (20, 10), (1, 1), (0, 0), (5, 2)):
            with self.subTest(width=width, height=height):
                output = branding.render(CONFIG, "branch", "abcdef0", width=width,
                                         height=height, color=True)
                self.assertNotIn("\x1b", output)
                self.assertNotIn("\u2580", output)
                self.assertLessEqual(len(output.splitlines()), max(0, height-1))
                self.assertTrue(all(len(line) <= max(0, width-1) for line in output.splitlines()))

    def test_minimum_graphical_viewport_and_long_metadata(self):
        config = {"roles": {"code": "x" * 200}}
        output = branding.render(config, "b" * 200, "c" * 200, width=78, height=32, color=True)
        self.assertIn("\u2580", output)
        self.assertIn("...", output)
        self.assertTrue(all(len(line) <= 77 for line in ANSI.sub("", output).splitlines()))

    def test_terminal_control_and_unicode_metadata_are_sanitized(self):
        config = {"roles": {"code": "bad\x1b[2J\n\t\u202e"}}
        output = ANSI.sub("", branding.render(config, "\x1b]0;bad\x07", "\rcommit",
                                              width=100, height=40, color=True))
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\r", output)
        self.assertEqual(len(output.splitlines()), 31)

    def test_windows_terminal_and_vscode_truecolor(self):
        for env in ({"WT_SESSION": "id"}, {"TERM_PROGRAM": "vscode"},
                    {"COLORTERM": "truecolor"}, {"COLORTERM": "24bit"}):
            self.assertTrue(branding.truecolor_terminal(Terminal(), env))

    def test_no_color_even_empty_and_dumb_terminal(self):
        for env in ({"NO_COLOR": "", "WT_SESSION": "id"},
                    {"TERM": "dumb", "TERM_PROGRAM": "vscode"}, {}):
            self.assertFalse(branding.truecolor_terminal(Terminal(), env))

    def test_encoding_limitations_fall_back(self):
        for encoding in ("ascii", "cp1252", "unknown-codec", None):
            stream = Mock(encoding=encoding)
            self.assertFalse(branding.truecolor_terminal(stream, {"WT_SESSION": "id"}))

    def test_non_tty_and_quiet_do_no_git_or_render_work(self):
        for stream, quiet in ((io.StringIO(), False), (Terminal(), True)):
            with patch.object(branding, "git_identity") as git:
                branding.show_splash(ROOT, CONFIG, quiet=quiet, stream=stream)
                git.assert_not_called()
                self.assertEqual(stream.getvalue(), "")

    def test_single_print_to_terminal_only(self):
        stream, output = Terminal(), io.StringIO()
        with patch.object(branding.os, "get_terminal_size", return_value=os.terminal_size((100, 40))), \
                patch.object(branding, "git_identity", return_value=("other-branch", "7654321")), \
                patch.dict(os.environ, {"WT_SESSION": "id"}, clear=True), redirect_stdout(output):
            branding.show_splash(ROOT, CONFIG, stream=stream)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(stream.getvalue().count("TRIGLAV"), 1)
        self.assertIn("other-branch", stream.getvalue())
        self.assertIn("7654321", stream.getvalue())

    def test_no_color_output_is_clean_ascii(self):
        stream = Terminal()
        with patch.dict(os.environ, {"NO_COLOR": "", "WT_SESSION": "id"}, clear=True), \
                patch.object(branding, "git_identity", return_value=("main", "abcdef0")):
            branding.show_splash(ROOT, CONFIG, stream=stream)
        self.assertNotIn("\x1b", stream.getvalue())
        self.assertTrue(stream.getvalue().isascii())

    def test_missing_dimensions_use_compact_fallback(self):
        stream = Terminal()
        with patch.object(branding.os, "get_terminal_size", side_effect=OSError), \
                patch.object(branding, "git_identity", return_value=("main", "abcdef0")):
            branding.show_splash(ROOT, CONFIG, stream=stream)
        self.assertNotIn("\u2580", stream.getvalue())

    def test_broken_terminal_and_render_failures_are_cosmetic(self):
        for failure in (BrokenPipeError, UnicodeEncodeError("ascii", "x", 0, 1, "bad"), OSError):
            stream = Mock(encoding="utf-8")
            stream.write.side_effect = failure
            with patch.object(branding, "git_identity", return_value=("main", "abcdef0")):
                branding.show_splash(ROOT, CONFIG, stream=stream)

    def test_git_queries_use_target_repo_and_are_bounded(self):
        with patch.object(branding.subprocess, "run", side_effect=[Mock(stdout="target\n"), Mock(stdout="1234567\n")]) as run:
            self.assertEqual(branding.git_identity("target repo"), ("target", "1234567"))
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3], ["git", "-C", "target repo"])
            self.assertEqual(call.kwargs["timeout"], 1)

    def test_missing_git_and_detached_head_are_graceful(self):
        with patch.object(branding.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(branding.git_identity(ROOT)[1], "unknown")
        with patch.object(branding.subprocess, "run", side_effect=[subprocess.CalledProcessError(1, "git"), Mock(stdout="1234567")]):
            self.assertEqual(branding.git_identity(ROOT), ("detached", "1234567"))

    def test_preview_never_opens_config_when_captured_or_quiet(self):
        for stream, quiet in ((io.StringIO(), []), (Terminal(), ["--quiet"])):
            with patch.object(branding.sys, "stderr", stream), patch.object(Path, "read_text") as read:
                self.assertEqual(branding.main(["--repo", ".", "--config", "absent", *quiet]), 0)
                read.assert_not_called()

    def test_preview_malformed_config_returns_zero(self):
        with patch.object(branding.sys, "stderr", Terminal()), \
                patch.object(Path, "read_text", return_value="invalid json"):
            self.assertEqual(branding.main(["--repo", ".", "--config", "absent"]), 0)

    def test_interactive_preview_reads_config_and_forwards_active_roles(self):
        stream = Terminal()
        with patch.object(branding.sys, "stderr", stream), \
                patch.object(Path, "read_text", return_value='{"roles": {"code": "custom"}}') as read, \
                patch.object(branding, "show_splash") as show:
            self.assertEqual(branding.main(["--repo", "target", "--config", "chosen.json",
                                           "--critic", "--reviewer", "--dry-run"]), 0)
        read.assert_called_once_with(encoding="utf-8-sig")
        show.assert_called_once_with(Path("target"), {"roles": {"code": "custom"}},
                                     critic=True, reviewer=True, dry_run=True, quiet=False)

    def test_art_and_branding_do_not_enter_controller_or_records(self):
        # Branding has one owner: the operator, not controller imports or log writers.
        for name in ("harness.py", "manager.py", "durable.py", "terminal_run.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("import branding", source)
            self.assertNotIn("triglav_art", source)
        source = (ROOT / "scripts/Invoke-Triglav.ps1").read_text(encoding="utf-8")
        self.assertEqual(source.count("& python $splashArgs"), 1)
        self.assertNotIn("RedirectStandard", source[source.index("$splashExit ="):source.index("# Build CLI argument")])


if __name__ == "__main__":
    unittest.main()
