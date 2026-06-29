"""Scrollable read-only viewer for a GitHub Actions run / job log.

Popped from the task-detail modal's `View log` action. Loads the log
in a background thread via `gh run view --log` (or `--log-failed`
when the focused task is in `fail` state) so a slow fetch doesn't
freeze the main UI. Esc / Enter close the modal; arrow / PgUp /
PgDn / Home / End scroll. Modeled on `diff_viewer` but single-tab
since there's only one log per task."""
from __future__ import annotations

import curses
from typing import List

from core.state.app import State
from features.task_log_viewer.actions import (
    handle_task_log_viewer_key as handle_task_log_viewer_key_action,
)
from features.task_log_viewer.projection import (
    task_log_viewer_hint_specs,
    task_status_label,
)

from ..colors import (
    PAIR_DLG_CYAN, PAIR_DLG_ERR, PAIR_DLG_FG, PAIR_DLG_OK, PAIR_DLG_WARN,
)
from ..geometry import (
    draw_modal_fill, draw_scroll_overflow, end_truncate, modal_geometry,
    safe_addstr, wrap_label_value,
)
from ..hints import Hint, render_hints
from ..sidebar import SPINNER_FRAMES


_STATUS_COLOURS = {
    "running": PAIR_DLG_CYAN,
    "pending": PAIR_DLG_CYAN,
    "ok": PAIR_DLG_OK,
    "fail": PAIR_DLG_ERR,
    "warn": PAIR_DLG_WARN,
}


def _spinner_glyph(state: State) -> str:
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


# ---------- Draw ----------------------------------------------------------


def _hints() -> List[Hint]:
    return [Hint(keys, action)
            for keys, action in task_log_viewer_hint_specs()]


def draw_task_log_viewer(stdscr, state: State, sidebar_x: int) -> None:
    viewer = state.task_log_viewer
    if viewer is None:
        return
    lines, loading, error = state.view_loads.snapshot(viewer.load_id)
    scroll = viewer.scroll

    h, w = stdscr.getmaxyx()
    target_w = min(120, max(40, w - 4))
    target_h = max(12, h - 4)
    x, y, box_w, box_h = modal_geometry(
        stdscr, sidebar_x, target_w, target_h)

    sb = curses.color_pair(PAIR_DLG_FG)
    draw_modal_fill(stdscr, x, y, box_w, box_h, sb)

    inner_x = x + 2
    inner_w = max(1, box_w - 4)
    pad_top = 1
    pad_bottom = 1

    line = y + pad_top
    safe_addstr(stdscr, line, inner_x, "Run log",
                curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN))
    line += 2

    # Task label (may wrap to two lines for long workflow names).
    for i, text in enumerate(
            wrap_label_value("Task", viewer.task.label, inner_w)):
        attr = sb if i == 0 else sb | curses.A_DIM
        safe_addstr(stdscr, line, inner_x, end_truncate(text, inner_w), attr)
        line += 1

    # Run id row — flat label/value, no wrap (run ids are short).
    run_label = f"Run id: {viewer.run_id}"
    if viewer.job_id is not None:
        run_label += f"  ·  Job id: {viewer.job_id}"
    safe_addstr(stdscr, line, inner_x,
                end_truncate(run_label, inner_w), sb | curses.A_DIM)
    line += 1

    # Colored state pill — re-reads `viewer.task.status` so a task
    # that transitions while the modal is open updates the colour in
    # place (running → ok / fail).
    task = viewer.task
    status_pair = _STATUS_COLOURS.get(task.status, PAIR_DLG_FG)
    status_text = f"State: {task_status_label(task)}"
    if viewer.only_failed:
        status_text += "  ·  showing failed steps only"
    safe_addstr(stdscr, line, inner_x,
                end_truncate(status_text, inner_w),
                curses.color_pair(status_pair) | curses.A_BOLD)
    line += 2  # blank row before the body

    hint_y = y + box_h - pad_bottom - 1
    body_h = max(1, hint_y - line - 1)

    if loading and not lines:
        safe_addstr(stdscr, line, inner_x,
                    f"{_spinner_glyph(state)} loading log…",
                    sb | curses.A_DIM)
    elif error and not lines:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(f"could not load log: {error}", inner_w),
                    curses.color_pair(PAIR_DLG_ERR))
    else:
        max_scroll = max(0, len(lines) - body_h)
        if scroll > max_scroll:
            scroll = max_scroll
        if scroll < 0:
            scroll = 0
        viewer.scroll = scroll
        for i in range(body_h):
            idx = scroll + i
            if idx >= len(lines):
                break
            safe_addstr(stdscr, line + i, inner_x,
                        end_truncate(lines[idx], inner_w), sb)
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


# ---------- Handle --------------------------------------------------------


def handle_task_log_viewer_key(state: State, key: int) -> None:
    handle_task_log_viewer_key_action(state, key)


__all__ = [
    "draw_task_log_viewer",
    "handle_task_log_viewer_key",
]
