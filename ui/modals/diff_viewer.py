"""File-context viewer with three tabs: diff, log, blame.

Diff is the original tab — runs `git diff HEAD -- <path>` (or
`git show <sha> -- <path>` when the modal was opened from the
commit-view). Log lists commits that touched the file
(`git log -- <path>`); Blame shows line-by-line authorship
(`git blame -- <path>`). Each tab has its own scroll position so
switching back lands where you left.

Pop'd from:
  * the review screen's right pane (Tab on a focused file row)
  * the action menu's working-tree pane (Tab on a focused file)
  * the commit view's Changes tab (Tab on a focused file row)

Key map (when the viewer is open):
  ←/→     switch active tab
  ↑/↓     scroll the active tab
  PgUp/Dn scroll bigger
  Home/End jump to the start/end of the active tab
  Tab/Esc close the modal back to the parent
"""
from __future__ import annotations

import curses
from typing import List

from core.state.app import State
from core.state.views import DiffViewer
from features.diff_viewer.actions import (
    handle_diff_viewer_key as handle_diff_viewer_key_action,
)
from features.diff_viewer.projection import (
    diff_viewer_hint_specs,
    set_tab_scroll,
    tab_lines,
    tab_loading,
    tab_scroll,
)

from ..colors import (
    PAIR_DLG_PASTEL_BLUE, PAIR_DLG_PASTEL_GREEN, PAIR_DLG_PASTEL_RED,
    PAIR_DLG_PASTEL_YELLOW, PAIR_DLG_CYAN, PAIR_DLG_FG,
)
from ..geometry import (
    draw_modal_fill, draw_scroll_overflow, end_truncate, modal_geometry,
    safe_addstr, wrap_label_value,
)
from ..hints import Hint, render_hints
from ..sidebar import SPINNER_FRAMES
from ..tabs import draw_tab_strip


def _spinner_glyph(state: State) -> str:
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


def _tab_lines(state: State, viewer: DiffViewer, tab: str) -> List[str]:
    return tab_lines(state, viewer, tab)


def _tab_loading(state: State, viewer: DiffViewer, tab: str) -> bool:
    return tab_loading(state, viewer, tab)


def _tab_scroll(viewer: DiffViewer, tab: str) -> int:
    return tab_scroll(viewer, tab)


def _set_tab_scroll(viewer: DiffViewer, tab: str, value: int) -> None:
    set_tab_scroll(viewer, tab, value)


# ---------- Per-line color attrs -----------------------------------------


def _diff_line_attr(line: str, sb: int) -> int:
    """Pastel paint for the four diff-line shapes:
      - hunk header  (@@ … @@)         → yellow + bold
      - removed line (- …)             → red
      - added line   (+ …)             → green
      - everything else                → default (sidebar fg)

    Headers (`diff --git`, `index …`, `--- …`, `+++ …`) keep the
    default attr so the structural lines read as quiet context."""
    if line.startswith("@@"):
        return curses.color_pair(PAIR_DLG_PASTEL_YELLOW) | curses.A_BOLD
    if line.startswith("+++") or line.startswith("---"):
        return sb | curses.A_DIM
    if line.startswith("+"):
        return curses.color_pair(PAIR_DLG_PASTEL_GREEN)
    if line.startswith("-"):
        return curses.color_pair(PAIR_DLG_PASTEL_RED)
    return sb


def _draw_log_row(stdscr, y: int, x: int, w: int, row: str,
                  sb: int) -> None:
    """Log row layout is `<short-sha> <YYYY-MM-DD> <subject>` —
    paint the sha column pastel yellow (matching the action
    menu's commit pane) and the date pastel blue, leaving the
    subject in default fg."""
    text = end_truncate(row, w)
    safe_addstr(stdscr, y, x, text, sb)
    parts = text.split(" ", 2)
    if not parts:
        return
    sha = parts[0]
    safe_addstr(stdscr, y, x, sha,
                curses.color_pair(PAIR_DLG_PASTEL_YELLOW))
    if len(parts) >= 2:
        date = parts[1]
        safe_addstr(stdscr, y, x + len(sha) + 1, date,
                    curses.color_pair(PAIR_DLG_PASTEL_BLUE))


def _draw_blame_row(stdscr, y: int, x: int, w: int, row: str,
                    sb: int) -> None:
    """Blame row layout (with `--abbrev=8`) is
    `<short-sha> (<author> <date> <line-num>) <content>` — pastel
    the sha column the same way log does, dim the parenthesized
    metadata so the actual code stands out."""
    text = end_truncate(row, w)
    safe_addstr(stdscr, y, x, text, sb)
    # First token is the sha (8 hex chars typically). Re-paint it.
    if len(text) >= 8 and text[8:9] in (" ", ""):
        safe_addstr(stdscr, y, x, text[:8],
                    curses.color_pair(PAIR_DLG_PASTEL_YELLOW))
    # Find the matching `)` after the metadata block to dim it.
    open_paren = text.find("(")
    close_paren = text.find(")", open_paren) if open_paren != -1 else -1
    if open_paren != -1 and close_paren != -1:
        meta_x = x + open_paren
        meta_text = text[open_paren:close_paren + 1]
        safe_addstr(stdscr, y, meta_x, meta_text, sb | curses.A_DIM)


# ---------- Hints --------------------------------------------------------


def _hints() -> List[Hint]:
    return [Hint(keys, action)
            for keys, action in diff_viewer_hint_specs()]


def _build_tabs(viewer: DiffViewer, state: State) -> list:
    """Compose the tab list with per-tab counts. Spinner glyph
    stands in for the count while a loader is still running so the
    header reads as live state rather than `(0)`."""
    def count(tab: str) -> str:
        if _tab_loading(state, viewer, tab):
            return _spinner_glyph(state)
        return str(len(_tab_lines(state, viewer, tab)))
    return [
        ("diff", "Diff", count("diff")),
        ("log", "Log", count("log")),
        ("blame", "Blame", count("blame")),
    ]


# ---------- Draw ---------------------------------------------------------


def draw_diff_viewer(stdscr, state: State, sidebar_x: int = 0) -> None:
    viewer = state.diff_viewer
    if viewer is None:
        return
    active = viewer.active_tab
    lines = list(_tab_lines(state, viewer, active))
    loading = _tab_loading(state, viewer, active)
    scroll = _tab_scroll(viewer, active)

    h, w = stdscr.getmaxyx()
    # Use the full available width (the review screen owns the whole
    # terminal — no sidebar to dodge). Cap at 120 cells for
    # readability on very wide windows.
    target_w = min(120, max(40, w - 4))
    target_h = max(10, h - 4)
    x, y, box_w, box_h = modal_geometry(
        stdscr, sidebar_x, target_w, target_h)

    sb = curses.color_pair(PAIR_DLG_FG)
    draw_modal_fill(stdscr, x, y, box_w, box_h, sb)

    inner_x = x + 2
    inner_w = max(1, box_w - 4)
    pad_top = 1
    pad_bottom = 1

    # Title rows: repo / block label first, file path indented
    # below — same shape as before, just no per-tab "Diff:" prefix
    # since the tab strip below already names the active view.
    title_rows = wrap_label_value("View", viewer.label, inner_w)
    line = y + pad_top
    for i, text in enumerate(title_rows):
        attr = (curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN)
                if i == 0 else sb)
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), attr)
        line += 1
    file_rows = wrap_label_value("File", viewer.file_path, inner_w)
    for text in file_rows:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), sb | curses.A_DIM)
        line += 1
    line += 1  # blank between header and tabs

    tabs = _build_tabs(viewer, state)
    draw_tab_strip(stdscr, line, inner_x, inner_w,
                   tabs, active_id=active, focused=True,
                   base_attr=sb)
    line += 1
    line += 1  # blank between tab strip and body

    hint_y = y + box_h - pad_bottom - 1
    body_h = max(1, hint_y - line - 1)

    if loading and not lines:
        safe_addstr(stdscr, line, inner_x,
                    f"{_spinner_glyph(state)} loading {active}…",
                    sb | curses.A_DIM)
    else:
        # Clamp scroll to the visible window.
        max_scroll = max(0, len(lines) - body_h)
        if scroll > max_scroll:
            scroll = max_scroll
        if scroll < 0:
            scroll = 0
        _set_tab_scroll(viewer, active, scroll)
        for i in range(body_h):
            idx = scroll + i
            if idx >= len(lines):
                break
            row = lines[idx]
            if active == "diff":
                safe_addstr(stdscr, line + i, inner_x,
                            end_truncate(row, inner_w),
                            _diff_line_attr(row, sb))
            elif active == "log":
                _draw_log_row(stdscr, line + i, inner_x,
                              inner_w, row, sb)
            else:  # blame
                _draw_blame_row(stdscr, line + i, inner_x,
                                inner_w, row, sb)
        # Above / below scroll affordances.
        if scroll > 0:
            draw_scroll_overflow(stdscr, line, inner_x, inner_w,
                                 scroll, "up", sb | curses.A_DIM)
        end = min(len(lines), scroll + body_h)
        if end < len(lines):
            below = len(lines) - end
            draw_scroll_overflow(stdscr, line + body_h - 1,
                                 inner_x, inner_w, below, "down",
                                 sb | curses.A_DIM)

    render_hints(stdscr, hint_y, inner_x, inner_w,
                 _hints(), attr=sb | curses.A_DIM)


def handle_diff_viewer_key(state: State, key: int) -> None:
    handle_diff_viewer_key_action(state, key)


__all__ = [
    "draw_diff_viewer",
    "handle_diff_viewer_key",
]
