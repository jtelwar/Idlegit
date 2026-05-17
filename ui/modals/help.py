"""In-app help browser.

Two-pane modal opened from the app menu (Tab on title row → Help).
Left pane lists every `.md` file in `<install_dir>/help/`; right pane
renders the selected page's content. Left/Right cycles focus between
the panes; Up/Down navigates within the focused pane (page selection
on the left, content scroll on the right). Esc closes.

The markdown subset supports headers (`#`, `##`, `###`), bold
(`**text**`), italic (`*text*` / `_text_`), unordered lists
(`- item` / `* item`), and inline code (`` `code` ``). Everything
else passes through as plain text. The renderer is deliberately
small + dependency-free — `wcwidth` is still the only runtime dep."""
from __future__ import annotations

import curses
import re
from typing import List, Tuple

from core.config import APP_DISPLAY_NAME, VERSION
from core.help_loader import load_help_pages
from core.models import HelpScreen, State

from ..colors import (
    PAIR_BRANCH, PAIR_DLG_CYAN, PAIR_DLG_FG, PAIR_DLG_FG_HINT_TEXT,
    PAIR_DLG_MAGENTA,
)
from ..geometry import (
    clamp_scroll, draw_modal_fill, draw_scroll_overflow, modal_geometry,
    safe_addstr, truncate,
)
from ..hints import (
    KEY_ENTER, KEY_ESC, KEY_LEFT_RIGHT, KEY_UP_DOWN, Hint, render_hints,
)


# Modal sizing.
MODAL_W = 100
BODY_TARGET_ROWS = 24


# ---------- Markdown subset renderer ------------------------------------


# A rendered span is (text, attribute-bitmask). A rendered line is a
# list of spans. The renderer returns `list[list[(text, attr)]]` —
# one entry per display row, already word-wrapped to fit the pane
# width, so the draw routine just paints what it gets back.
RenderedSpan = Tuple[str, int]
RenderedLine = List[RenderedSpan]


# Header glyph attributes are applied by the renderer to the WHOLE
# logical line (after stripping the leading `#`s). Inline emphasis
# inside headers is intentionally NOT recursively parsed — keeping
# the subset small.
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_STAR_RE = re.compile(r"(?<![*])\*(?!\*)(.+?)(?<![*])\*(?!\*)")
_ITALIC_UNDER_RE = re.compile(r"(?<![A-Za-z0-9_])_(.+?)_(?![A-Za-z0-9_])")
_CODE_RE = re.compile(r"`([^`]+?)`")


def _attrs() -> dict:
    """Curses attribute bitmasks for each markup role, computed lazily
    so import-time has no curses dependency. Italic falls back to
    A_DIM when the terminal's terminfo doesn't ship A_ITALIC (older
    terminfo entries don't, and curses raises AttributeError)."""
    italic = getattr(curses, "A_ITALIC", 0) or curses.A_DIM
    bold = curses.A_BOLD
    fg = curses.color_pair(PAIR_DLG_FG)
    cyan = curses.color_pair(PAIR_DLG_CYAN)
    code = curses.color_pair(PAIR_BRANCH)
    return {
        "plain": fg,
        "bold": fg | bold,
        "italic": fg | italic,
        "code": code,
        # Headers all share the cyan accent — H1 + H2 are bold,
        # H3 keeps the colour without the weight so it reads as a
        # finer-grained subsection rather than a peer of H1/H2.
        "h1": cyan | bold,
        "h2": cyan | bold,
        "h3": cyan,
        "bullet": fg | bold,
        "hint": curses.color_pair(PAIR_DLG_FG_HINT_TEXT),
    }


def _parse_inline(text: str, base_attr: int, attrs: dict) -> RenderedLine:
    """Parse inline markup (bold / italic / code) within a single
    paragraph line. Returns a list of (text, attr) spans in left-to-
    right order. Non-overlapping greedy match — `**foo**` wins over
    `*foo*`, and code blocks (`` `…` ``) take priority over both so
    a literal `**` inside backticks survives intact."""
    # Code first — the literal-pass-through rule means we tokenise
    # those regions and leave them untouched while inline emphasis is
    # applied to the gaps between them.
    spans: RenderedLine = []
    pos = 0
    for m in _CODE_RE.finditer(text):
        if m.start() > pos:
            spans.extend(_parse_emphasis(text[pos:m.start()], base_attr, attrs))
        spans.append((m.group(1), attrs["code"]))
        pos = m.end()
    if pos < len(text):
        spans.extend(_parse_emphasis(text[pos:], base_attr, attrs))
    return spans or [("", base_attr)]


def _parse_emphasis(text: str, base_attr: int,
                    attrs: dict) -> RenderedLine:
    """Replace `**…**` with bold spans and `*…*` / `_…_` with italic.
    Bold runs first so the italic pass doesn't snag the inner asterisks.
    Anything not matched stays in `base_attr`."""
    spans: RenderedLine = []
    pos = 0
    for m in _BOLD_RE.finditer(text):
        if m.start() > pos:
            spans.extend(_parse_italic(text[pos:m.start()], base_attr, attrs))
        spans.append((m.group(1), attrs["bold"]))
        pos = m.end()
    if pos < len(text):
        spans.extend(_parse_italic(text[pos:], base_attr, attrs))
    return spans


def _parse_italic(text: str, base_attr: int,
                  attrs: dict) -> RenderedLine:
    """Replace `*…*` / `_…_` with italic spans. The underscore form's
    word-boundary lookarounds prevent eating `snake_case` identifiers
    inside prose."""
    spans: RenderedLine = []
    pos = 0
    pattern = re.compile(
        rf"{_ITALIC_STAR_RE.pattern}|{_ITALIC_UNDER_RE.pattern}")
    for m in pattern.finditer(text):
        if m.start() > pos:
            spans.append((text[pos:m.start()], base_attr))
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        spans.append((inner, attrs["italic"]))
        pos = m.end()
    if pos < len(text):
        spans.append((text[pos:], base_attr))
    return spans


def _wrap_spans(spans: RenderedLine, width: int,
                indent: str = "") -> List[RenderedLine]:
    """Soft-wrap a span sequence into multiple display lines, each
    at most `width` cells. Splits on whitespace boundaries when
    possible; falls back to a hard cut on long unbroken tokens
    (paths, shas). Continuation lines keep `indent` so a bulleted
    item's subsequent rows align under the text, not the bullet."""
    if width <= 0:
        return [spans]
    out: List[RenderedLine] = []
    current: RenderedLine = []
    current_len = 0
    for text, attr in spans:
        words = re.split(r"(\s+)", text)
        for token in words:
            if not token:
                continue
            tok_len = len(token)
            if current_len + tok_len <= width:
                current.append((token, attr))
                current_len += tok_len
                continue
            # Token doesn't fit — flush, then handle long tokens by
            # splitting them mid-character.
            if current:
                out.append(current)
            if tok_len > width:
                # Hard-wrap a long unbreakable token.
                i = 0
                while i < tok_len:
                    out.append([(token[i: i + width], attr)])
                    i += width
                current = []
                current_len = 0
            else:
                # Drop a leading whitespace token at the start of a
                # new line — visual indent comes from `indent` below.
                if token.isspace():
                    current = []
                    current_len = 0
                else:
                    current = [(token, attr)]
                    current_len = tok_len
    if current:
        out.append(current)
    if not out:
        out = [[("", 0)]]
    # Apply the per-line `indent` prefix to continuation lines so
    # wrapped list items align under the body, not the bullet.
    if indent:
        attrs = _attrs()
        for i in range(1, len(out)):
            out[i] = [(indent, attrs["plain"])] + out[i]
    return out


def render_markdown(body: str, width: int) -> List[RenderedLine]:
    """Convert a markdown body to a flat list of pre-wrapped display
    lines. The first H1 (the page title) is intentionally STRIPPED
    here — the modal header surfaces the page title above the content
    pane already, so rendering it again is redundant.

    Caller passes the content-pane width; the wrap re-runs on every
    redraw, so a terminal resize naturally reflows the page."""
    attrs = _attrs()
    lines = body.splitlines()
    rendered: List[RenderedLine] = []
    seen_title = False
    for raw in lines:
        line = raw.rstrip()
        # Skip the first H1 (page title rendered separately by the
        # modal chrome) but keep subsequent H1s — multi-H1 docs are
        # unusual but we don't want to silently eat them.
        if not seen_title and line.startswith("# "):
            seen_title = True
            continue
        if not line.strip():
            rendered.append([("", attrs["plain"])])
            continue
        # Headers.
        if line.startswith("### "):
            spans = [(line[4:], attrs["h3"])]
            rendered.extend(_wrap_spans(spans, width))
            continue
        if line.startswith("## "):
            spans = [(line[3:], attrs["h2"])]
            rendered.extend(_wrap_spans(spans, width))
            continue
        if line.startswith("# "):
            spans = [(line[2:], attrs["h1"])]
            rendered.extend(_wrap_spans(spans, width))
            continue
        # Unordered list. The leading bullet glyph is rendered in
        # bold; the rest of the line gets inline emphasis parsing.
        m = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if m:
            indent_text = m.group(1)
            spans: RenderedLine = [
                (indent_text + "• ", attrs["bullet"]),
            ]
            spans.extend(_parse_inline(m.group(2), attrs["plain"], attrs))
            rendered.extend(_wrap_spans(
                spans, width, indent=indent_text + "  "))
            continue
        # Plain paragraph line.
        spans = _parse_inline(line, attrs["plain"], attrs)
        rendered.extend(_wrap_spans(spans, width))
    return rendered


# ---------- Open helper -------------------------------------------------


def open_help_screen(state: State) -> None:
    """Install the help-screen modal on `state.help_screen`. Always
    succeeds — even when the bundled `help/` directory is missing
    or empty, the modal opens with a single placeholder page so the
    user gets a clear "no help available" message rather than a
    silent no-op."""
    pages = load_help_pages()
    if not pages:
        from core.models import HelpPage
        pages = [HelpPage(
            title="(no help available)",
            filename="",
            body=(
                "# No help available\n\n"
                "The bundled `help/` directory could not be found.\n\n"
                "This usually means the install didn't ship the help "
                "pages, or the directory was deleted post-install. "
                "Reinstall via `idlegit-update` to refresh the bundled "
                "documentation."
            ),
        )]
    state.help_screen = HelpScreen(pages=pages)


# ---------- Hints --------------------------------------------------------


def _hints(state: State) -> list:
    screen = state.help_screen
    if screen is None:
        return []
    hints: list = [Hint(KEY_UP_DOWN, "navigate")]
    if len(screen.pages) > 1:
        hints.append(Hint(KEY_LEFT_RIGHT, "switch pane"))
    hints.append(Hint(KEY_ENTER, "open page"))
    hints.append(Hint(KEY_ESC, "close"))
    return hints


# ---------- Handle -------------------------------------------------------


def handle_help_screen_key(state: State, key: int) -> None:
    """Dispatch a keypress against the help screen. Left/Right toggles
    focus between the page list and the content pane; Up/Down then
    moves within the focused pane (selection vs scroll). Esc closes.
    Enter on the list pane jumps focus to the content pane so a one-
    handed reader can move "select page → start reading" without
    reaching for the arrow keys."""
    screen = state.help_screen
    if screen is None:
        return

    if key == 27:  # Esc — close
        state.help_screen = None
        return

    if not screen.pages:
        return

    if key == curses.KEY_LEFT:
        screen.focused_pane = "list"
        return
    if key == curses.KEY_RIGHT:
        screen.focused_pane = "content"
        return

    if screen.focused_pane == "list":
        if key == curses.KEY_UP:
            screen.selected_page = max(0, screen.selected_page - 1)
            screen.content_scroll = 0
            return
        if key == curses.KEY_DOWN:
            screen.selected_page = min(len(screen.pages) - 1,
                                       screen.selected_page + 1)
            screen.content_scroll = 0
            return
        if key in (10, 13, curses.KEY_ENTER):
            # Enter on the list jumps focus to the content pane so the
            # user can immediately start scrolling the selected page.
            screen.focused_pane = "content"
            return
        return

    # Content pane is focused — Up/Down scroll a single row, Page
    # Up/Down move a screenful. Bounds are enforced by `clamp_scroll`
    # inside the draw routine.
    if key == curses.KEY_UP:
        screen.content_scroll = max(0, screen.content_scroll - 1)
        return
    if key == curses.KEY_DOWN:
        screen.content_scroll += 1  # clamped in draw
        return
    if key == curses.KEY_PPAGE:
        screen.content_scroll = max(0, screen.content_scroll - 10)
        return
    if key == curses.KEY_NPAGE:
        screen.content_scroll += 10  # clamped in draw
        return
    if key == curses.KEY_HOME:
        screen.content_scroll = 0
        return
    if key == curses.KEY_END:
        # Caller doesn't know the rendered height yet — pick a big
        # number and let the draw routine clamp. Cheaper than re-
        # rendering here just to compute the max.
        screen.content_scroll = 10_000
        return


# ---------- Draw ---------------------------------------------------------


def draw_help_screen(stdscr, state: State, sidebar_x: int) -> None:
    """Two-pane help browser. Left column is the page list; the bulk
    of the width is the rendered content. A vertical divider sits at
    the boundary so the two panes read as separate surfaces without
    needing different background colours."""
    screen = state.help_screen
    if screen is None:
        return

    # blank-top (1) + title (1) + blank (1) + pane-header (1)
    # + body (body_h) + blank (1) + footer (1) + blank-bottom (1).
    # Seven rows of chrome — everything else is body.
    chrome_h = 7
    requested_body = max(8, BODY_TARGET_ROWS)
    requested_h = chrome_h + requested_body
    x, y, w, h = modal_geometry(stdscr, sidebar_x, MODAL_W, requested_h)
    # `modal_geometry` clamps `h` to `terminal_rows - 2`, so on
    # short terminals the returned box is smaller than what we
    # asked for. Re-derive body_h from the actual height so the
    # content + scroll-overflow indicators stay inside the box —
    # without this, a clamped modal paints body rows past its own
    # bottom edge, overlapping the footer / surrounding panel.
    body_h = max(1, h - chrome_h)
    sb = curses.color_pair(PAIR_DLG_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    inner_w = w - 4

    # ---- Title row: "Idlegit vX.Y.Z Help" ----
    # Matches the app-menu title styling: magenta app name, dim
    # cyan version suffix, then "Help" in bold cyan so the modal's
    # purpose reads as the rightmost segment.
    safe_addstr(stdscr, y + 1, inner_x,
                APP_DISPLAY_NAME[:inner_w],
                curses.A_BOLD | curses.color_pair(PAIR_DLG_MAGENTA))
    title_col = min(len(APP_DISPLAY_NAME), inner_w)
    if title_col < inner_w:
        version_suffix = f" v{VERSION}"
        safe_addstr(stdscr, y + 1, inner_x + title_col,
                    version_suffix[:inner_w - title_col],
                    curses.color_pair(PAIR_DLG_CYAN) | curses.A_DIM)
        title_col += min(len(version_suffix), inner_w - title_col)
    if title_col < inner_w:
        help_suffix = " Help"
        safe_addstr(stdscr, y + 1, inner_x + title_col,
                    help_suffix[:inner_w - title_col],
                    curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN))

    # ---- Body: list pane | content pane ----
    list_w = max(18, min(28, inner_w // 4))
    divider_x = inner_x + list_w + 1  # one cell of padding between
    content_x = divider_x + 2
    content_w = max(10, inner_w - (content_x - inner_x))
    # Pane headers ("Index" / "<filename>") live one row above the
    # body content; the body starts a row lower than the old layout.
    pane_header_y = y + 3
    body_y0 = y + 4

    list_focused = (screen.focused_pane == "list")
    content_focused = (screen.focused_pane == "content")
    active_attr = curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN)
    inactive_attr = curses.color_pair(PAIR_DLG_FG_HINT_TEXT)
    # Left pane header: "Index"
    index_label = "Index"
    safe_addstr(stdscr, pane_header_y, inner_x,
                index_label[:list_w].ljust(list_w),
                active_attr if list_focused else inactive_attr)
    # Right pane header: the current page's parsed title (the first
    # `# heading` in the body, with a de-prefixed filename fallback —
    # same string the index lists). Truncated to the content-pane
    # width so a long title can't bleed past the right border.
    page = screen.pages[screen.selected_page] if screen.pages else None
    page_title = page.title if page and page.title else ""
    if page_title:
        safe_addstr(stdscr, pane_header_y, content_x,
                    truncate(page_title, content_w, "middle"),
                    active_attr if content_focused else inactive_attr)

    # Page list.
    n_pages = len(screen.pages)
    list_scroll = clamp_scroll(
        screen.selected_page, 0, n_pages, body_h)
    list_focused = (screen.focused_pane == "list")
    for i in range(body_h):
        idx = list_scroll + i
        if idx >= n_pages:
            break
        row_y = body_y0 + i
        page = screen.pages[idx]
        is_sel = (idx == screen.selected_page)
        prefix = "→ " if (is_sel and list_focused) else (
            "• " if is_sel else "  ")
        label = truncate(page.title, list_w - len(prefix), "end")
        text = (prefix + label).ljust(list_w)[:list_w]
        if is_sel and list_focused:
            attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD | curses.A_REVERSE
        elif is_sel:
            attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
        else:
            attr = sb
        safe_addstr(stdscr, row_y, inner_x, text, attr)

    # Vertical divider.
    for i in range(body_h):
        safe_addstr(stdscr, body_y0 + i, divider_x, "│",
                    curses.color_pair(PAIR_DLG_FG_HINT_TEXT))

    # Content pane.
    page = screen.pages[screen.selected_page]
    rendered = render_markdown(page.body, content_w)
    n_lines = len(rendered)
    # Clamp scroll AFTER the render so resize / page-switch corrects
    # for an out-of-range scroll without leaving the pane blank.
    screen.content_scroll = max(
        0, min(screen.content_scroll, max(0, n_lines - body_h)))
    for i in range(body_h):
        idx = screen.content_scroll + i
        if idx >= n_lines:
            break
        row_y = body_y0 + i
        # Each line is a list of (text, attr) spans — paint them in
        # order across the row, then ljust the remainder with the
        # panel bg so any prior content is cleared.
        col = content_x
        printed = 0
        for span_text, attr in rendered[idx]:
            if printed >= content_w:
                break
            allowed = content_w - printed
            chunk = span_text[:allowed]
            safe_addstr(stdscr, row_y, col, chunk, attr)
            col += len(chunk)
            printed += len(chunk)
        # Fill the trailing cells with panel bg so the previous
        # frame's text doesn't ghost through.
        if printed < content_w:
            safe_addstr(stdscr, row_y, col,
                        " " * (content_w - printed), sb)

    # Scroll-overflow indicators on the content pane.
    if screen.content_scroll > 0:
        draw_scroll_overflow(stdscr, body_y0 - 1, content_x, content_w,
                             screen.content_scroll, "up",
                             sb | curses.A_DIM)
    if screen.content_scroll + body_h < n_lines:
        below = n_lines - (screen.content_scroll + body_h)
        draw_scroll_overflow(stdscr, body_y0 + body_h, content_x,
                             content_w, below, "down",
                             sb | curses.A_DIM)

    # ---- Footer hints ----
    render_hints(stdscr, y + h - 2, inner_x, inner_w, _hints(state),
                 attr=sb | curses.A_DIM)
