"""Unit tests for pure functions — no git, no curses, no temp dirs."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Bootstrap sys.path so we can import the package modules directly.
_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.git_ops import (  # noqa: E402
    _format_suggestion, _parse_on_block, canonicalize_url,
    derive_lfs_pattern, first_line, format_size, format_time_ago,
    parse_github_slug, would_run_on_push,
)
from core.models import FileChange, WorkflowInfo  # noqa: E402
from core.workers import (  # noqa: E402
    _current_step_label, _format_job_label, _format_run_label,
    _gh_run_status_to_task,
)


# ui.py imports curses at module load. We can't import it under unittest
# without a real terminal — guard with try/except so tests for the non-ui
# helpers still run on machines/CIs without a tty.
try:
    from ui import field_visible, truncate
    from ui.geometry import end_truncate, wrap_label_value
    UI_AVAILABLE = True
except Exception:  # pragma: no cover — only triggers when curses is unusable
    UI_AVAILABLE = False


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable (no curses)")
class TestTruncate(unittest.TestCase):
    def test_short_text_unchanged(self) -> None:
        self.assertEqual(truncate("hello", 20, "middle"), "hello")
        self.assertEqual(truncate("hello", 20, "start"), "hello")
        self.assertEqual(truncate("hello", 20, "end"), "hello")

    def test_max_zero_or_negative_disables(self) -> None:
        self.assertEqual(truncate("anything", 0, "middle"), "anything")
        self.assertEqual(truncate("anything", -5, "middle"), "anything")

    def test_max_one_is_just_ellipsis(self) -> None:
        self.assertEqual(truncate("longstring", 1, "middle"), "…")

    def test_end_truncation(self) -> None:
        # Keep head, drop tail. 12 chars total incl. ellipsis.
        out = truncate("upskill.health.vr", 12, "end")
        self.assertEqual(len(out), 12)
        self.assertTrue(out.endswith("…"))
        self.assertTrue(out.startswith("upskill.hea"))

    def test_start_truncation(self) -> None:
        out = truncate("upskill.health.vr", 12, "start")
        self.assertEqual(len(out), 12)
        self.assertTrue(out.startswith("…"))
        self.assertTrue(out.endswith("h.vr"))

    def test_middle_truncation_balances(self) -> None:
        out = truncate("Upskill.Health.Domain.Models", 20, "middle")
        self.assertEqual(len(out), 20)
        self.assertIn("…", out)
        head, tail = out.split("…")
        # The head and tail should each carry meaningful chars.
        self.assertTrue(head.startswith("Upskill"))
        self.assertTrue(tail.endswith("Models"))

    def test_unknown_mode_falls_back_to_middle(self) -> None:
        a = truncate("Upskill.Health.Domain.Models", 20, "garbage")
        b = truncate("Upskill.Health.Domain.Models", 20, "middle")
        self.assertEqual(a, b)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable (no curses)")
class TestEndTruncate(unittest.TestCase):
    def test_short_unchanged(self) -> None:
        self.assertEqual(end_truncate("hello", 20), "hello")

    def test_zero_or_negative_returns_empty(self) -> None:
        # Differs from `truncate(..., mode="end")` — modal callers
        # depend on this so a 0-cell column doesn't fall through to a
        # full-width string.
        self.assertEqual(end_truncate("anything", 0), "")
        self.assertEqual(end_truncate("anything", -3), "")

    def test_one_is_just_ellipsis(self) -> None:
        self.assertEqual(end_truncate("longstring", 1), "…")

    def test_keeps_head_drops_tail(self) -> None:
        out = end_truncate("Upskill.Health.Domain.Models", 12)
        self.assertEqual(len(out), 12)
        self.assertTrue(out.endswith("…"))
        self.assertTrue(out.startswith("Upskill.Hea"))


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable (no curses)")
class TestWrapLabelValue(unittest.TestCase):
    """Modal layout helper: keeps "label: value" on one line when it
    fits, otherwise splits the value onto its own indented line. End-
    truncation is the only allowed truncation — the head of a long
    repo name is what users actually recognise."""

    def test_one_liner_when_it_fits(self) -> None:
        self.assertEqual(
            wrap_label_value("Winner", "short-name", 30),
            ["Winner: short-name"])

    def test_splits_to_two_lines_when_value_fits_alone(self) -> None:
        # 14-char value, 20 max width → fits fine on its own indented line.
        out = wrap_label_value("Winner", "Upskill.Health", 20)
        self.assertEqual(out, ["Winner:", "  Upskill.Health"])

    def test_splits_to_two_lines_with_end_truncation(self) -> None:
        # 28-char value, 20 max width, 18 cells of value_room → trims
        # the tail with an ellipsis. Head ("Upskill.Health.D…") is what
        # the user reads.
        out = wrap_label_value("Winner", "Upskill.Health.Domain.Models", 20)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], "Winner:")
        self.assertEqual(len(out[1]), 20)  # padded indent + value
        self.assertTrue(out[1].startswith("  Upskill.Health"))
        self.assertTrue(out[1].endswith("…"))

    def test_end_truncates_when_value_overflows_alone(self) -> None:
        out = wrap_label_value("Syncing", "this-name-is-way-too-long-to-fit", 20)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], "Syncing:")
        self.assertTrue(out[1].endswith("…"))
        self.assertLessEqual(len(out[1]), 20)
        # Head of the name is preserved — that's the part users read.
        self.assertTrue(out[1].startswith("  this-name"))

    def test_empty_label_skips_label_row(self) -> None:
        # Used when laying out a continuation line ("to <repo>") —
        # callers pass an empty label and the helper still end-truncates
        # the value for them.
        self.assertEqual(
            wrap_label_value("", "short-value", 20),
            ["short-value"])
        self.assertEqual(
            wrap_label_value("", "this-name-is-way-too-long-to-fit", 15),
            ["this-name-is-w…"])

    def test_zero_width_returns_empty_list(self) -> None:
        self.assertEqual(wrap_label_value("Winner", "anything", 0), [])
        self.assertEqual(wrap_label_value("Winner", "anything", -3), [])


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable (no curses)")
class TestFieldVisible(unittest.TestCase):
    def test_short_message_returns_full(self) -> None:
        text, cur = field_visible("hello", 3, 20, focused=True)
        self.assertEqual(text, "hello")
        self.assertEqual(cur, 3)

    def test_long_unfocused_shows_tail(self) -> None:
        msg = "abcdefghijklmnopqrst"  # 20 chars
        text, _ = field_visible(msg, 0, 10, focused=False)
        self.assertEqual(text, "klmnopqrst")  # last 10

    def test_long_focused_centers_cursor(self) -> None:
        msg = "abcdefghijklmnopqrst"  # 20 chars, inner_w 10, half=5
        text, cur = field_visible(msg, 10, 10, focused=True)
        # cursor at offset 10, half=5 → start=5 → window "fghijklmno"
        self.assertEqual(len(text), 10)
        self.assertEqual(cur, 5)
        self.assertEqual(text[cur], msg[10])

    def test_cursor_at_start_clamps(self) -> None:
        msg = "abcdefghijklmnopqrst"
        text, cur = field_visible(msg, 0, 10, focused=True)
        # start clamps to 0
        self.assertEqual(text, msg[:10])
        self.assertEqual(cur, 0)

    def test_cursor_at_end_clamps(self) -> None:
        msg = "abcdefghijklmnopqrst"  # len 20
        text, cur = field_visible(msg, 20, 10, focused=True)
        # start = max(0, min(20-5, 20-10)) = 10 → window = msg[10:20]
        self.assertEqual(text, msg[10:20])
        self.assertEqual(cur, 10)


class TestCanonicalizeUrl(unittest.TestCase):
    def test_strips_dot_git_suffix(self) -> None:
        self.assertEqual(
            canonicalize_url("https://github.com/Foo/Bar.git"),
            "github.com/foo/bar")

    def test_lowercases(self) -> None:
        self.assertEqual(
            canonicalize_url("https://github.com/UpskillHealth/Foo"),
            "github.com/upskillhealth/foo")

    def test_strips_trailing_slash(self) -> None:
        self.assertEqual(
            canonicalize_url("https://github.com/foo/bar/"),
            "github.com/foo/bar")

    def test_ssh_form_to_canonical(self) -> None:
        self.assertEqual(
            canonicalize_url("git@github.com:foo/bar.git"),
            "github.com/foo/bar")

    def test_strips_user_at(self) -> None:
        self.assertEqual(
            canonicalize_url("https://user:token@github.com/foo/bar.git"),
            "github.com/foo/bar")

    def test_ssh_and_https_match(self) -> None:
        self.assertEqual(
            canonicalize_url("git@github.com:foo/bar.git"),
            canonicalize_url("https://github.com/foo/bar"))


class TestFormatSize(unittest.TestCase):
    def test_megabytes(self) -> None:
        self.assertEqual(format_size(150 * 1024 * 1024), "150.0 MB")
        self.assertEqual(format_size(1023 * 1024 * 1024 + 512 * 1024), "1023.5 MB")

    def test_gigabytes(self) -> None:
        self.assertTrue(format_size(2 * 1024 * 1024 * 1024).endswith(" GB"))
        out = format_size(int(1.5 * 1024 * 1024 * 1024))
        self.assertEqual(out, "1.50 GB")

    def test_zero(self) -> None:
        self.assertEqual(format_size(0), "0.0 MB")


class TestDeriveLfsPattern(unittest.TestCase):
    def test_extension(self) -> None:
        self.assertEqual(derive_lfs_pattern("Assets/Foo.png"), "*.png")
        self.assertEqual(derive_lfs_pattern("vendored/big.bin"), "*.bin")

    def test_no_extension_uses_basename(self) -> None:
        self.assertEqual(derive_lfs_pattern("dir/sub/datafile"), "datafile")

    def test_extension_with_only_dot(self) -> None:
        # ".." pathological — Path(".").suffix == "" so fall back to basename.
        self.assertEqual(derive_lfs_pattern("noext."), "noext.")


class TestFirstLine(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(first_line(""), "(no output)")

    def test_only_whitespace(self) -> None:
        self.assertEqual(first_line("   \n  \n"), "(no output)")

    def test_first_nonblank(self) -> None:
        self.assertEqual(first_line("\n\nfoo\nbar\n"), "foo")

    def test_strips_trailing_whitespace(self) -> None:
        self.assertEqual(first_line("  hello   \n"), "hello")


class TestFormatSuggestion(unittest.TestCase):
    def test_empty_changes(self) -> None:
        self.assertEqual(_format_suggestion([], 3, 3, 3), "")

    def test_single_category(self) -> None:
        changes = [
            FileChange(path="a.cs", kind="added", weight=100),
            FileChange(path="b.cs", kind="added", weight=50),
        ]
        out = _format_suggestion(changes, 3, 3, 3)
        self.assertEqual(out, "add: a.cs, b.cs")

    def test_three_categories_ordered(self) -> None:
        changes = [
            FileChange(path="new.cs", kind="added", weight=10),
            FileChange(path="edit.cs", kind="modified", weight=20),
            FileChange(path="gone.cs", kind="deleted", weight=0),
        ]
        out = _format_suggestion(changes, 3, 3, 3)
        # Categories joined with "; " in fixed order: add, update, remove —
        # imperative tense to match git's commit-message convention.
        self.assertEqual(out, "add: new.cs; update: edit.cs; remove: gone.cs")

    def test_caps_per_category(self) -> None:
        # 5 added files, only the top 2 by weight should appear when cap=2.
        changes = [
            FileChange(path=f"f{i}.bin", kind="added", weight=float(i))
            for i in range(5)
        ]
        out = _format_suggestion(changes, 2, 0, 0)
        # weight desc, so f4 then f3.
        self.assertEqual(out, "add: f4.bin, f3.bin")

    def test_cap_zero_hides_category(self) -> None:
        changes = [
            FileChange(path="a.cs", kind="added", weight=1),
            FileChange(path="b.cs", kind="modified", weight=1),
        ]
        out = _format_suggestion(changes, 0, 5, 0)
        self.assertEqual(out, "update: b.cs")

    def test_basenames_only(self) -> None:
        changes = [FileChange(path="deep/nested/a.cs", kind="added", weight=1)]
        out = _format_suggestion(changes, 3, 0, 0)
        self.assertEqual(out, "add: a.cs")


class TestFormatTimeAgo(unittest.TestCase):
    def test_under_a_second_is_now(self) -> None:
        self.assertEqual(format_time_ago(0), "now")
        self.assertEqual(format_time_ago(0.4), "now")

    def test_negative_clamped_to_now(self) -> None:
        self.assertEqual(format_time_ago(-5), "now")

    def test_seconds(self) -> None:
        self.assertEqual(format_time_ago(1), "1s ago")
        self.assertEqual(format_time_ago(45), "45s ago")
        self.assertEqual(format_time_ago(59.9), "59s ago")

    def test_minutes(self) -> None:
        self.assertEqual(format_time_ago(60), "1m ago")
        self.assertEqual(format_time_ago(125), "2m ago")
        self.assertEqual(format_time_ago(3599), "59m ago")

    def test_hours(self) -> None:
        self.assertEqual(format_time_ago(3600), "1h ago")
        self.assertEqual(format_time_ago(60 * 60 * 5), "5h ago")
        self.assertEqual(format_time_ago(86399), "23h ago")

    def test_days(self) -> None:
        self.assertEqual(format_time_ago(86400), "1d ago")
        self.assertEqual(format_time_ago(86400 * 3 + 60), "3d ago")


class TestParseGithubSlug(unittest.TestCase):
    def test_ssh_form(self) -> None:
        self.assertEqual(
            parse_github_slug("git@github.com:foo/bar.git"), "foo/bar")
        # No .git suffix.
        self.assertEqual(
            parse_github_slug("git@github.com:foo/bar"), "foo/bar")

    def test_https_form(self) -> None:
        self.assertEqual(
            parse_github_slug("https://github.com/foo/bar.git"), "foo/bar")
        self.assertEqual(
            parse_github_slug("http://github.com/foo/bar"), "foo/bar")

    def test_https_with_token(self) -> None:
        self.assertEqual(
            parse_github_slug("https://x-access-token:abc@github.com/foo/bar.git"),
            "foo/bar")

    def test_non_github(self) -> None:
        self.assertIsNone(parse_github_slug("git@gitlab.com:foo/bar.git"))
        self.assertIsNone(parse_github_slug("https://bitbucket.org/foo/bar"))

    def test_empty_or_none(self) -> None:
        self.assertIsNone(parse_github_slug(""))
        self.assertIsNone(parse_github_slug(None))


class TestGhRunStatusToTask(unittest.TestCase):
    def test_in_progress_stays_running(self) -> None:
        status, _ = _gh_run_status_to_task({"status": "in_progress"})
        self.assertEqual(status, "running")
        status, _ = _gh_run_status_to_task({"status": "queued"})
        self.assertEqual(status, "running")

    def test_completed_success_is_ok(self) -> None:
        status, _ = _gh_run_status_to_task(
            {"status": "completed", "conclusion": "success"})
        self.assertEqual(status, "ok")

    def test_completed_failure_is_fail(self) -> None:
        for c in ("failure", "timed_out", "startup_failure", "action_required"):
            status, msg = _gh_run_status_to_task(
                {"status": "completed", "conclusion": c})
            self.assertEqual(status, "fail", c)
            self.assertEqual(msg, c)

    def test_completed_skipped_or_cancelled_is_warn(self) -> None:
        for c in ("cancelled", "skipped", "neutral", "stale"):
            status, msg = _gh_run_status_to_task(
                {"status": "completed", "conclusion": c})
            self.assertEqual(status, "warn", c)

    def test_unknown_completed_conclusion(self) -> None:
        status, msg = _gh_run_status_to_task(
            {"status": "completed", "conclusion": "weird"})
        self.assertEqual(status, "warn")
        self.assertEqual(msg, "weird")


class TestCurrentStepLabel(unittest.TestCase):
    def test_no_steps_returns_empty(self) -> None:
        self.assertEqual(_current_step_label({"steps": []}), "")
        self.assertEqual(_current_step_label({}), "")

    def test_in_progress_step_takes_priority(self) -> None:
        job = {"steps": [
            {"name": "checkout", "status": "completed"},
            {"name": "build", "status": "in_progress"},
            {"name": "deploy", "status": "queued"},
        ]}
        self.assertEqual(_current_step_label(job), "build")

    def test_falls_back_to_latest_completed(self) -> None:
        job = {"steps": [
            {"name": "checkout", "status": "completed"},
            {"name": "build", "status": "completed"},
            {"name": "deploy", "status": "queued"},
        ]}
        self.assertEqual(_current_step_label(job), "build")

    def test_falls_back_to_first_when_none_started(self) -> None:
        job = {"steps": [
            {"name": "lint", "status": "queued"},
            {"name": "test", "status": "queued"},
        ]}
        self.assertEqual(_current_step_label(job), "lint")


class TestFormatRunAndJobLabels(unittest.TestCase):
    def test_run_label_with_step(self) -> None:
        self.assertEqual(
            _format_run_label("api", "CI", "compile"),
            "↗ api: CI — compile")

    def test_run_label_without_step(self) -> None:
        self.assertEqual(_format_run_label("api", "CI"), "↗ api: CI")

    def test_job_label_with_step(self) -> None:
        self.assertEqual(_format_job_label("build", "compile"),
                         "  ↳ build — compile")

    def test_job_label_without_step(self) -> None:
        self.assertEqual(_format_job_label("build"), "  ↳ build")


class TestParseOnBlock(unittest.TestCase):
    def test_no_on_block(self) -> None:
        out = _parse_on_block("name: foo\njobs: {}\n")
        self.assertFalse(out["push"])
        self.assertFalse(out["workflow_dispatch"])

    def test_scalar_on_push(self) -> None:
        out = _parse_on_block("on: push\njobs: {}\n")
        self.assertTrue(out["push"])
        self.assertFalse(out["workflow_dispatch"])
        self.assertEqual(out["push_branches"], [])

    def test_scalar_on_workflow_dispatch(self) -> None:
        out = _parse_on_block("on: workflow_dispatch\njobs: {}\n")
        self.assertFalse(out["push"])
        self.assertTrue(out["workflow_dispatch"])

    def test_flow_seq_multiple_triggers(self) -> None:
        out = _parse_on_block("on: [push, pull_request, workflow_dispatch]\n")
        self.assertTrue(out["push"])
        self.assertTrue(out["workflow_dispatch"])

    def test_mapping_with_inline_branches(self) -> None:
        text = "on:\n  push:\n    branches: [master, develop]\n"
        out = _parse_on_block(text)
        self.assertTrue(out["push"])
        self.assertEqual(out["push_branches"], ["master", "develop"])

    def test_mapping_with_block_list_branches_compact(self) -> None:
        # Block list at the same indent as `branches:` (the form GitHub
        # actually emits). Reproduces the user's API workflows.
        text = (
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "    - master\n"
            "    - develop\n"
        )
        out = _parse_on_block(text)
        self.assertTrue(out["push"])
        self.assertEqual(out["push_branches"], ["master", "develop"])

    def test_mapping_with_block_list_branches_indented(self) -> None:
        text = (
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - master\n"
            "      - develop\n"
        )
        out = _parse_on_block(text)
        self.assertEqual(out["push_branches"], ["master", "develop"])

    def test_branches_ignore(self) -> None:
        text = (
            "on:\n"
            "  push:\n"
            "    branches-ignore:\n"
            "    - hotfix/*\n"
        )
        out = _parse_on_block(text)
        self.assertTrue(out["push"])
        self.assertEqual(out["push_branches_ignore"], ["hotfix/*"])

    def test_workflow_dispatch_alongside_push(self) -> None:
        text = (
            "on:\n"
            "  push:\n"
            "    branches: [master]\n"
            "  workflow_dispatch:\n"
        )
        out = _parse_on_block(text)
        self.assertTrue(out["push"])
        self.assertTrue(out["workflow_dispatch"])
        self.assertEqual(out["push_branches"], ["master"])

    def test_pull_request_only(self) -> None:
        text = (
            "on:\n"
            "  pull_request:\n"
            "    branches: [master]\n"
        )
        out = _parse_on_block(text)
        self.assertFalse(out["push"])
        self.assertFalse(out["workflow_dispatch"])

    def test_inline_comments_stripped(self) -> None:
        text = (
            "on: # trigger config\n"
            "  push:  # only on push\n"
            "    branches: [master]  # production\n"
        )
        out = _parse_on_block(text)
        self.assertTrue(out["push"])
        self.assertEqual(out["push_branches"], ["master"])


class TestWouldRunOnPush(unittest.TestCase):
    def _wf(self, **kwargs) -> WorkflowInfo:
        defaults = dict(name="ci", path=".github/workflows/ci.yml",
                        triggers_push=True)
        defaults.update(kwargs)
        return WorkflowInfo(**defaults)

    def test_not_push_triggered_returns_false(self) -> None:
        wf = self._wf(triggers_push=False)
        self.assertFalse(would_run_on_push(wf, "master"))

    def test_no_branches_runs_on_any(self) -> None:
        wf = self._wf()
        self.assertTrue(would_run_on_push(wf, "master"))
        self.assertTrue(would_run_on_push(wf, "feature/x"))

    def test_branch_match(self) -> None:
        wf = self._wf(push_branches=["master"])
        self.assertTrue(would_run_on_push(wf, "master"))
        self.assertFalse(would_run_on_push(wf, "develop"))

    def test_branch_glob(self) -> None:
        wf = self._wf(push_branches=["release/*"])
        self.assertTrue(would_run_on_push(wf, "release/2026"))
        self.assertFalse(would_run_on_push(wf, "feature/x"))

    def test_branches_ignore_takes_priority(self) -> None:
        wf = self._wf(push_branches_ignore=["hotfix/*"])
        self.assertTrue(would_run_on_push(wf, "master"))
        self.assertFalse(would_run_on_push(wf, "hotfix/123"))

    def test_disabled_state_blocks_run(self) -> None:
        wf = self._wf(state="disabled_manually")
        self.assertFalse(would_run_on_push(wf, "master"))
        wf = self._wf(state="disabled_inactivity")
        self.assertFalse(would_run_on_push(wf, "master"))

    def test_active_state_runs(self) -> None:
        wf = self._wf(state="active")
        self.assertTrue(would_run_on_push(wf, "master"))

    def test_empty_state_treated_as_runnable(self) -> None:
        # Before remote merge, state is "". Treat as not-disabled so we
        # don't false-negative on workflows we haven't queried yet.
        wf = self._wf(state="")
        self.assertTrue(would_run_on_push(wf, "master"))


if __name__ == "__main__":
    unittest.main()
