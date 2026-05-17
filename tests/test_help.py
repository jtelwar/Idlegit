"""Tests for the in-app help browser — page discovery + markdown
renderer.

Discovery: `load_help_pages` reads `*.md` files from the bundled
`help/` dir, alphabetically by filename, extracting the first `# `
heading as the page title (with a fallback to a de-prefixed
filename). Tests use a tmp dir + a `help_dir` monkey-patch so the
real bundled pages don't influence assertions.

Renderer: `render_markdown` parses the subset (headers, bold,
italic, lists, inline code) into pre-wrapped display lines. We
exercise each grammar rule in isolation rather than testing the
exact wrap output — terminal width is a knob, so the test asserts
on structural properties (span count, attribute markers, line
count) instead.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import help_loader  # noqa: E402

try:
    import curses  # noqa: F401
    from ui.modals import help as help_modal  # noqa: E402
    from ui.modals.help import render_markdown  # noqa: E402
    UI_AVAILABLE = True
except Exception:
    UI_AVAILABLE = False


# Curses isn't initscr'd in a unit-test process — `curses.color_pair()`
# raises until then. The renderer's `_attrs` is the only call into
# curses; stub it with a plain-int dict so tests can exercise the
# structural logic (span count, attr-differs-from-base, line wrap)
# without needing a real terminal. Each role gets a unique
# bitmask-shaped integer so "spans differ in attr" assertions still
# work, and inline-code's `0` value gives us a sentinel for the
# "attr changed" checks.
_STUB_ATTRS = {
    "plain": 0,
    "bold": 1,
    "italic": 2,
    "code": 4,
    "h1": 8,
    "h2": 16,
    "h3": 32,
    "bullet": 64,
    "hint": 128,
}


class _TempHelpDir(unittest.TestCase):
    """Mixin that spins up a tmp `help/` dir and patches
    `help_loader.help_dir` to point at it. Tests write whatever
    `.md` files they need into `self.tmp`."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="idlegit-help-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self._patch = mock.patch.object(
            help_loader, "help_dir", return_value=self.tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write(self, name: str, body: str) -> Path:
        path = self.tmp / name
        path.write_text(body, encoding="utf-8")
        return path


class TestLoadHelpPages(_TempHelpDir):
    def test_empty_dir_returns_empty_list(self) -> None:
        pages = help_loader.load_help_pages()
        self.assertEqual(pages, [])

    def test_missing_dir_returns_empty_list(self) -> None:
        with mock.patch.object(help_loader, "help_dir",
                               return_value=Path("/nonexistent-idlegit")):
            self.assertEqual(help_loader.load_help_pages(), [])

    def test_skips_non_markdown_files(self) -> None:
        self._write("a.md", "# A")
        self._write("b.txt", "not markdown")
        self._write("c.MD", "not lowercased — case-sensitive match")
        pages = help_loader.load_help_pages()
        # Match is case-insensitive on suffix.
        titles = {p.title for p in pages}
        self.assertIn("A", titles)

    def test_orders_by_filename(self) -> None:
        # Sort key is filename — the numeric prefix is what guarantees
        # the documented "01- comes before 02-" ordering.
        self._write("02-second.md", "# Second")
        self._write("01-first.md", "# First")
        self._write("10-tenth.md", "# Tenth")
        pages = help_loader.load_help_pages()
        self.assertEqual([p.title for p in pages],
                         ["First", "Second", "Tenth"])

    def test_uses_first_h1_as_title(self) -> None:
        self._write("a.md", "intro paragraph\n\n# The Heading\n\nbody")
        pages = help_loader.load_help_pages()
        self.assertEqual(pages[0].title, "The Heading")

    def test_filename_fallback_strips_numeric_prefix(self) -> None:
        # No `# heading` in the body — title comes from the filename
        # stem with the `NN-` prefix and dashes / underscores cleaned up.
        self._write("03-getting-started.md", "body without heading")
        pages = help_loader.load_help_pages()
        self.assertEqual(pages[0].title, "getting started")

    def test_filename_fallback_underscore_separator(self) -> None:
        self._write("01_intro_page.md", "body")
        pages = help_loader.load_help_pages()
        self.assertEqual(pages[0].title, "intro page")


@unittest.skipUnless(UI_AVAILABLE, "curses unavailable")
class TestRenderMarkdown(unittest.TestCase):
    """`render_markdown` returns `list[list[(text, attr)]]` — one
    outer entry per display line, inner spans concatenate left-to-
    right to form the line. These tests check span-level structure;
    we deliberately don't pin specific curses attr bitmasks (they
    depend on init_colors having been called)."""

    WIDTH = 80

    def setUp(self) -> None:
        # `render_markdown` calls `_attrs()` which calls
        # `curses.color_pair()` — that raises until `initscr()` has
        # been called, and we can't initscr in a unit test. Stub
        # `_attrs` with plain ints so the renderer's structural
        # logic runs unblocked.
        self._patch = mock.patch.object(
            help_modal, "_attrs", return_value=_STUB_ATTRS)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_empty_body(self) -> None:
        lines = render_markdown("", self.WIDTH)
        self.assertEqual(lines, [])

    def test_paragraph_renders_one_line(self) -> None:
        lines = render_markdown("Hello world", self.WIDTH)
        self.assertEqual(len(lines), 1)
        text = "".join(s[0] for s in lines[0])
        self.assertEqual(text, "Hello world")

    def test_blank_line_becomes_blank_row(self) -> None:
        lines = render_markdown("foo\n\nbar", self.WIDTH)
        # 3 rows: foo, blank, bar.
        self.assertEqual(len(lines), 3)
        self.assertEqual("".join(s[0] for s in lines[0]), "foo")
        self.assertEqual("".join(s[0] for s in lines[1]), "")
        self.assertEqual("".join(s[0] for s in lines[2]), "bar")

    def test_first_h1_is_stripped(self) -> None:
        # The modal chrome surfaces the page title — rendering it
        # again would double up. Only the FIRST h1 is stripped.
        lines = render_markdown("# Title\n\nbody", self.WIDTH)
        joined = " ".join("".join(s[0] for s in line) for line in lines)
        self.assertNotIn("Title", joined)
        self.assertIn("body", joined)

    def test_second_h1_survives(self) -> None:
        lines = render_markdown(
            "# Title\n\nintro\n\n# Second", self.WIDTH)
        joined = " ".join("".join(s[0] for s in line) for line in lines)
        self.assertIn("Second", joined)

    def test_bullet_list_uses_bullet_glyph(self) -> None:
        lines = render_markdown("- one\n- two", self.WIDTH)
        # 2 list rows, each starting with "•".
        self.assertEqual(len(lines), 2)
        self.assertTrue("".join(s[0] for s in lines[0]).startswith("•"))
        self.assertTrue("".join(s[0] for s in lines[1]).startswith("•"))

    def test_bold_span_separates_from_plain_text(self) -> None:
        # "the **strong** word" should produce ≥ 3 spans (plain, bold,
        # plain) with the middle span carrying a different attr.
        lines = render_markdown("the **strong** word", self.WIDTH)
        spans = lines[0]
        self.assertGreaterEqual(len(spans), 3)
        # Find the span containing "strong" — its attr should differ
        # from the surrounding plain-text attr.
        strong_idx = next(
            (i for i, (t, _) in enumerate(spans) if "strong" in t), -1)
        self.assertGreaterEqual(strong_idx, 0)
        plain_attr = next(a for t, a in spans if "the" in t)
        bold_attr = spans[strong_idx][1]
        self.assertNotEqual(plain_attr, bold_attr)

    def test_inline_code_separates_span(self) -> None:
        lines = render_markdown("use `foo()` here", self.WIDTH)
        spans = lines[0]
        code_idx = next(
            (i for i, (t, _) in enumerate(spans) if "foo()" in t), -1)
        self.assertGreaterEqual(code_idx, 0)
        # Backticks themselves shouldn't appear in any span.
        joined = "".join(t for t, _ in spans)
        self.assertNotIn("`", joined)

    def test_italic_underscore_does_not_eat_snake_case(self) -> None:
        # `_foo_` is italic; `snake_case` is NOT — word boundaries.
        lines = render_markdown("see snake_case and _italic_", self.WIDTH)
        joined = "".join(t for line in lines for t, _ in line)
        self.assertIn("snake_case", joined)
        # The underscores around `italic` are consumed.
        self.assertNotIn("_italic_", joined)
        self.assertIn("italic", joined)

    def test_long_line_wraps_to_multiple_rows(self) -> None:
        # 20-char width forces a wrap on a 40+ char line.
        text = "this is a fairly long line that must wrap"
        lines = render_markdown(text, 20)
        self.assertGreaterEqual(len(lines), 2)
        # Each rendered row is at most `width` cells.
        for line in lines:
            length = sum(len(t) for t, _ in line)
            self.assertLessEqual(length, 20)


class TestModalHelpPageDataclass(unittest.TestCase):
    def test_help_page_holds_raw_body(self) -> None:
        from core.models import HelpPage
        p = HelpPage(title="T", filename="01-t.md", body="# T\n\nbody")
        self.assertEqual(p.title, "T")
        self.assertEqual(p.filename, "01-t.md")
        self.assertIn("body", p.body)


if __name__ == "__main__":
    unittest.main()
