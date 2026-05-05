"""File-diff viewer — pops from the review screen's right pane when
the user presses Enter on a focused file row. Loads `git diff HEAD --
<path>` (or the raw file contents for an untracked addition) in a
daemon thread; the modal opens instantly with a spinner and fills in
once the load lands. Enter or Esc closes; arrow / page keys scroll."""
from __future__ import annotations

import curses
import threading
from pathlib import Path
from typing import List

from models import DiffViewer, State
from git_ops import git_bounded_output

from ..colors import (
    PAIR_PASTEL_GREEN, PAIR_PASTEL_RED, PAIR_PASTEL_YELLOW,
    PAIR_SB_CYAN, PAIR_SB_FG,
)
from ..geometry import (
    draw_modal_fill, end_truncate, modal_geometry, safe_addstr,
    wrap_label_value,
)
from ..hints import (
    KEY_ESC, KEY_TAB, KEY_UP_DOWN, Hint, render_hints,
)
from ..sidebar import SPINNER_FRAMES


# Read cap for untracked file contents — anything larger and we
# truncate with a notice, so a stray 500MB binary doesn't blow up
# the modal's render path. Full diff output (which git itself would
# truncate or report binary on) gets the same cap as a defensive
# upper bound.
_MAX_DIFF_BYTES = 4 * 1024 * 1024
_MAX_DIFF_LINES = 50_000


def _spinner_glyph(state: State) -> str:
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


def open_diff_viewer(state: State, target_path: Path,
                     label: str, file_path: str,
                     untracked: bool) -> None:
    """Install the diff viewer onto state and kick off the loader.
    Caller passes the block's `target_path` (the repo / submodule
    working tree the diff runs in), the human label for the title,
    and the relative `file_path` to diff."""
    viewer = DiffViewer(
        file_path=file_path,
        target_path=target_path,
        label=label,
        untracked=untracked,
    )
    state.diff_viewer = viewer
    threading.Thread(
        target=_load_diff, args=(viewer,), daemon=True).start()


def _load_diff(viewer: DiffViewer) -> None:
    """Background loader. For tracked files, runs `git diff HEAD --
    <path>`; for untracked, reads the raw file contents and prepends
    a synthetic header so the modal still has something to show.
    Result lands in `viewer.lines`; `viewer.loading` flips False
    when done. Cancel-event short-circuits before mutating so a user
    closing the modal mid-load doesn't waste cycles."""
    try:
        if viewer.cancel_event.is_set():
            return
        if viewer.untracked:
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


def _diff_line_attr(line: str, sb: int) -> int:
    """Pastel paint for the four diff-line shapes:
      - hunk header  (@@ … @@)         → yellow + bold
      - removed line (- …)             → red
      - added line   (+ …)             → green
      - everything else                → default (sidebar fg)

    Headers (`diff --git`, `index …`, `--- …`, `+++ …`) keep the
    default attr so the structural lines read as quiet context."""
    if line.startswith("@@"):
        return curses.color_pair(PAIR_PASTEL_YELLOW) | curses.A_BOLD
    if line.startswith("+++") or line.startswith("---"):
        return sb | curses.A_DIM
    if line.startswith("+"):
        return curses.color_pair(PAIR_PASTEL_GREEN)
    if line.startswith("-"):
        return curses.color_pair(PAIR_PASTEL_RED)
    return sb


def _hints() -> List[Hint]:
    return [
        Hint(KEY_UP_DOWN, "scroll"),
        Hint(KEY_TAB, "close"),
        Hint(KEY_ESC, "close"),
    ]


def draw_diff_viewer(stdscr, state: State, sidebar_x: int = 0) -> None:
    viewer = state.diff_viewer
    if viewer is None:
        return
    with viewer.lock:
        lines = list(viewer.lines)
        loading = viewer.loading

    h, w = stdscr.getmaxyx()
    # Use the full available width (the review screen owns the whole
    # terminal — no sidebar to dodge). Cap at 120 cells for
    # readability on very wide windows.
    target_w = min(120, max(40, w - 4))
    target_h = max(10, h - 4)
    x, y, box_w, box_h = modal_geometry(
        stdscr, sidebar_x, target_w, target_h)

    sb = curses.color_pair(PAIR_SB_FG)
    draw_modal_fill(stdscr, x, y, box_w, box_h, sb)

    inner_x = x + 2
    inner_w = max(1, box_w - 4)
    pad_top = 1
    pad_bottom = 1

    # Title rows: "Diff: <repo-label>" on the first line, then the
    # file path on its own indented row via wrap_label_value so a
    # long path doesn't middle-truncate.
    title_rows = wrap_label_value("Diff", viewer.label, inner_w)
    line = y + pad_top
    for i, text in enumerate(title_rows):
        attr = (curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN)
                if i == 0 else sb)
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), attr)
        line += 1
    file_rows = wrap_label_value("File", viewer.file_path, inner_w)
    for text in file_rows:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(text, inner_w), sb | curses.A_DIM)
        line += 1
    line += 1  # blank between header and body

    hint_y = y + box_h - pad_bottom - 1
    body_h = max(1, hint_y - line - 1)

    if loading and not lines:
        safe_addstr(stdscr, line, inner_x,
                    f"{_spinner_glyph(state)} loading diff…",
                    sb | curses.A_DIM)
    else:
        # Clamp scroll to the visible window.
        max_scroll = max(0, len(lines) - body_h)
        if viewer.scroll > max_scroll:
            viewer.scroll = max_scroll
        if viewer.scroll < 0:
            viewer.scroll = 0
        for i in range(body_h):
            idx = viewer.scroll + i
            if idx >= len(lines):
                break
            row = lines[idx]
            safe_addstr(stdscr, line + i, inner_x,
                        end_truncate(row, inner_w),
                        _diff_line_attr(row, sb))
        # Above / below scroll affordances.
        if viewer.scroll > 0:
            msg = f"  ↑ {viewer.scroll} more above"
            safe_addstr(stdscr, line, inner_x + max(0, inner_w - len(msg) - 1),
                        msg, sb | curses.A_DIM)
        end = min(len(lines), viewer.scroll + body_h)
        if end < len(lines):
            below = len(lines) - end
            msg = f"  ↓ {below} more below"
            safe_addstr(stdscr, line + body_h - 1,
                        inner_x + max(0, inner_w - len(msg) - 1),
                        msg, sb | curses.A_DIM)

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
    if key == curses.KEY_UP:
        viewer.scroll = max(0, viewer.scroll - 1)
        return
    if key == curses.KEY_DOWN:
        viewer.scroll += 1  # clamped at draw time once we know body_h
        return
    if key == curses.KEY_PPAGE:
        viewer.scroll = max(0, viewer.scroll - 10)
        return
    if key == curses.KEY_NPAGE:
        viewer.scroll += 10
        return
    if key == curses.KEY_HOME:
        viewer.scroll = 0
        return
    if key == curses.KEY_END:
        # Big jump; draw clamps to max_scroll so we don't need to
        # know body_h here.
        viewer.scroll = len(viewer.lines)
        return


__all__ = [
    "open_diff_viewer",
    "draw_diff_viewer",
    "handle_diff_viewer_key",
]
