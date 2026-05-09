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
import threading
from pathlib import Path
from typing import List

from core.models import DiffViewer, State
from core.git_ops import (
    git_bounded_output, query_file_blame, query_file_log,
)

from ..colors import (
    PAIR_DLG_PASTEL_BLUE, PAIR_DLG_PASTEL_GREEN, PAIR_DLG_PASTEL_RED,
    PAIR_DLG_PASTEL_YELLOW, PAIR_DLG_CYAN, PAIR_DLG_FG,
)
from ..geometry import (
    draw_modal_fill, draw_scroll_overflow, end_truncate, modal_geometry,
    safe_addstr, wrap_label_value,
)
from ..hints import (
    KEY_ESC, KEY_LEFT_RIGHT, KEY_TAB, KEY_UP_DOWN, Hint, render_hints,
)
from ..sidebar import SPINNER_FRAMES
from ..tabs import cycle_tab, draw_tab_strip


# Read cap for untracked file contents — anything larger and we
# truncate with a notice, so a stray 500MB binary doesn't blow up
# the modal's render path. Full diff output (which git itself would
# truncate or report binary on) gets the same cap as a defensive
# upper bound.
_MAX_DIFF_BYTES = 4 * 1024 * 1024
_MAX_DIFF_LINES = 50_000

_TAB_IDS = ("diff", "log", "blame")


def _spinner_glyph(state: State) -> str:
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


# ---------- Per-tab field accessors --------------------------------------
#
# Each tab carries its own (lines, loading, scroll). The diff tab
# keeps the original short field names so external animation hooks
# (`state.diff_viewer.loading`) keep working without touching the
# call sites — the helpers below just paper over the difference
# between the diff tab and the prefix-named log/blame tabs.


def _tab_lines(viewer: DiffViewer, tab: str) -> List[str]:
    if tab == "log":
        return viewer.log_lines
    if tab == "blame":
        return viewer.blame_lines
    return viewer.lines


def _tab_loading(viewer: DiffViewer, tab: str) -> bool:
    if tab == "log":
        return viewer.log_loading
    if tab == "blame":
        return viewer.blame_loading
    return viewer.loading


def _tab_scroll(viewer: DiffViewer, tab: str) -> int:
    if tab == "log":
        return viewer.log_scroll
    if tab == "blame":
        return viewer.blame_scroll
    return viewer.scroll


def _set_tab_scroll(viewer: DiffViewer, tab: str, value: int) -> None:
    if tab == "log":
        viewer.log_scroll = value
    elif tab == "blame":
        viewer.blame_scroll = value
    else:
        viewer.scroll = value


def _any_tab_loading(viewer: DiffViewer) -> bool:
    """Used by the spinner-tick driver in the main loop —
    `viewer.loading` only covers the diff tab, so we need a wider
    check to keep the global animation tick alive while log / blame
    are still working."""
    return viewer.loading or viewer.log_loading or viewer.blame_loading


# ---------- Open ---------------------------------------------------------


def open_diff_viewer(state: State, target_path: Path,
                     label: str, file_path: str,
                     untracked: bool,
                     commit_sha: str = "") -> None:
    """Install the diff viewer onto state and kick off all three tab
    loaders. Each runs in its own daemon thread so the user can
    switch tabs without waiting for a slow blame on a 50k-line
    file. `commit_sha` (when supplied) scopes diff + log + blame to
    that commit (`git show <sha>` / `git log <sha>` /
    `git blame <sha>`)."""
    viewer = DiffViewer(
        file_path=file_path,
        target_path=target_path,
        label=label,
        untracked=untracked,
        commit_sha=commit_sha,
    )
    state.diff_viewer = viewer
    threading.Thread(
        target=_load_diff, args=(viewer,), daemon=True).start()
    threading.Thread(
        target=_load_log, args=(viewer,), daemon=True).start()
    threading.Thread(
        target=_load_blame, args=(viewer,), daemon=True).start()


# ---------- Loaders ------------------------------------------------------


def _load_diff(viewer: DiffViewer) -> None:
    """Background loader for the diff tab. For tracked files runs
    `git diff HEAD -- <path>` (or `git show <sha> -- <path>` when
    `commit_sha` is set); for untracked, reads the raw file
    contents and prepends a synthetic header so the modal still
    has something to show. Result lands in `viewer.lines`;
    `viewer.loading` flips False when done. Cancel-event
    short-circuits before mutating so a user closing the modal
    mid-load doesn't waste cycles."""
    try:
        if viewer.cancel_event.is_set():
            return
        if viewer.commit_sha:
            sha = viewer.commit_sha
            if sha.startswith("-"):
                lines = ["(unsafe sha)"]
            else:
                rc, out, err, truncated = git_bounded_output(
                    viewer.target_path,
                    ["show", sha, "--", viewer.file_path],
                    _MAX_DIFF_BYTES)
                if rc != 0 and not out:
                    text = err.strip() or "(no diff available)"
                    lines = [text]
                else:
                    lines = out.splitlines() if out else ["(no diff)"]
                if truncated:
                    lines.append(
                        f"... (truncated at {_MAX_DIFF_BYTES} bytes)")
        elif viewer.untracked:
            full = viewer.target_path / viewer.file_path
            truncated = False
            try:
                with full.open("rb") as f:
                    raw = f.read(_MAX_DIFF_BYTES + 1)
                if len(raw) > _MAX_DIFF_BYTES:
                    raw = raw[:_MAX_DIFF_BYTES]
                    truncated = True
                text = raw.decode("utf-8", errors="replace")
            except OSError as e:
                text = f"(could not read file: {e})"
            lines = [
                f"diff --git a/{viewer.file_path} b/{viewer.file_path}",
                "new file (untracked)",
                "--- /dev/null",
                f"+++ b/{viewer.file_path}",
            ]
            for ln in text.splitlines():
                lines.append("+" + ln)
            if truncated:
                lines.append(f"... (truncated at {_MAX_DIFF_BYTES} bytes)")
        else:
            rc, out, err, truncated = git_bounded_output(
                viewer.target_path,
                ["diff", "HEAD", "--", viewer.file_path],
                _MAX_DIFF_BYTES)
            if rc != 0 and not out:
                text = err.strip() or "(no diff available)"
                lines = [text]
            else:
                lines = out.splitlines() if out else ["(no diff)"]
            if truncated:
                lines.append(f"... (truncated at {_MAX_DIFF_BYTES} bytes)")

        if len(lines) > _MAX_DIFF_LINES:
            lines = lines[:_MAX_DIFF_LINES]
            lines.append(
                f"... (truncated at {_MAX_DIFF_LINES} lines)")

        if viewer.cancel_event.is_set():
            return
        with viewer.lock:
            viewer.lines = lines
            viewer.loading = False
    finally:
        if viewer.loading:
            with viewer.lock:
                viewer.loading = False


def _load_log(viewer: DiffViewer) -> None:
    """Background loader for the log tab — `git log -- <path>` (or
    `git log <sha> -- <path>` when scoped to a commit). Cancel
    safe."""
    try:
        if viewer.cancel_event.is_set():
            return
        rows = query_file_log(
            viewer.target_path, viewer.file_path,
            sha=viewer.commit_sha)
        if viewer.cancel_event.is_set():
            return
        with viewer.lock:
            viewer.log_lines = rows or ["(no log available)"]
            viewer.log_loading = False
    finally:
        if viewer.log_loading:
            with viewer.lock:
                viewer.log_loading = False


def _load_blame(viewer: DiffViewer) -> None:
    """Background loader for the blame tab — `git blame -- <path>`
    (or `git blame <sha> -- <path>`). Untracked files have no
    history to blame; we land an explanatory line instead of an
    empty pane."""
    try:
        if viewer.cancel_event.is_set():
            return
        if viewer.untracked:
            with viewer.lock:
                viewer.blame_lines = [
                    "(untracked file — no blame history yet)"]
                viewer.blame_loading = False
            return
        rows = query_file_blame(
            viewer.target_path, viewer.file_path,
            sha=viewer.commit_sha)
        if viewer.cancel_event.is_set():
            return
        with viewer.lock:
            viewer.blame_lines = rows or ["(no blame output)"]
            viewer.blame_loading = False
    finally:
        if viewer.blame_loading:
            with viewer.lock:
                viewer.blame_loading = False


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
    return [
        Hint(KEY_LEFT_RIGHT, "switch tab"),
        Hint(KEY_UP_DOWN, "scroll"),
        Hint(KEY_TAB, "close"),
        Hint(KEY_ESC, "close"),
    ]


def _build_tabs(viewer: DiffViewer, state: State) -> list:
    """Compose the tab list with per-tab counts. Spinner glyph
    stands in for the count while a loader is still running so the
    header reads as live state rather than `(0)`."""
    def count(tab: str) -> str:
        if _tab_loading(viewer, tab):
            return _spinner_glyph(state)
        return str(len(_tab_lines(viewer, tab)))
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
    with viewer.lock:
        active = viewer.active_tab
        lines = list(_tab_lines(viewer, active))
        loading = _tab_loading(viewer, active)
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
    viewer = state.diff_viewer
    if viewer is None:
        return
    if key in (9, 10, 13, curses.KEY_ENTER, 27):
        viewer.cancel_event.set()
        state.diff_viewer = None
        return

    # ←/→ switches the active tab. Each tab keeps its own scroll
    # position so the user lands where they left off.
    if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
        tabs = [(t, t.title(), "") for t in _TAB_IDS]
        viewer.active_tab = cycle_tab(
            tabs, viewer.active_tab,
            -1 if key == curses.KEY_LEFT else 1)
        return

    # Scroll the active tab.
    active = viewer.active_tab
    cur = _tab_scroll(viewer, active)
    if key == curses.KEY_UP:
        _set_tab_scroll(viewer, active, max(0, cur - 1))
        return
    if key == curses.KEY_DOWN:
        # Clamped at draw time once we know body_h.
        _set_tab_scroll(viewer, active, cur + 1)
        return
    if key == curses.KEY_PPAGE:
        _set_tab_scroll(viewer, active, max(0, cur - 10))
        return
    if key == curses.KEY_NPAGE:
        _set_tab_scroll(viewer, active, cur + 10)
        return
    if key == curses.KEY_HOME:
        _set_tab_scroll(viewer, active, 0)
        return
    if key == curses.KEY_END:
        # Big jump; draw clamps to max_scroll so we don't need to
        # know body_h here.
        _set_tab_scroll(viewer, active, len(_tab_lines(viewer, active)))
        return


__all__ = [
    "open_diff_viewer",
    "draw_diff_viewer",
    "handle_diff_viewer_key",
    "_any_tab_loading",
]
