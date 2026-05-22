"""Scrollable read-only viewer for a GitHub Actions run / job log.

Popped from the task-detail modal's `View log` action. Loads the log
in a background thread via `gh run view --log` (or `--log-failed`
when the focused task is in `fail` state) so a slow fetch doesn't
freeze the main UI. Esc / Enter close the modal; arrow / PgUp /
PgDn / Home / End scroll. Modeled on `diff_viewer` but single-tab
since there's only one log per task."""
from __future__ import annotations

import curses
import threading
from typing import List

from core.git_ops import fetch_run_log
from core.models import State, Task, TaskLogViewer

from ..colors import (
    PAIR_DLG_CYAN, PAIR_DLG_ERR, PAIR_DLG_FG, PAIR_DLG_OK, PAIR_DLG_WARN,
)
from ..geometry import (
    draw_modal_fill, draw_scroll_overflow, end_truncate, modal_geometry,
    safe_addstr, wrap_label_value,
)
from ..hints import KEY_ESC, KEY_UP_DOWN, Hint, render_hints
from ..sidebar import SPINNER_FRAMES


_STATUS_COLOURS = {
    "running": PAIR_DLG_CYAN,
    "pending": PAIR_DLG_CYAN,
    "ok": PAIR_DLG_OK,
    "fail": PAIR_DLG_ERR,
    "warn": PAIR_DLG_WARN,
}


def _status_label(task: Task) -> str:
    """Same glyph palette as task_detail._status_label — keep the two
    in lockstep so the indicator reads identically in both modals."""
    if task.status == "running":
        return "running"
    if task.status == "pending":
        return "pending"
    if task.status == "ok":
        return "✓ ok"
    if task.status == "fail":
        return "✗ failed"
    return "⚠ warn"


def _spinner_glyph(state: State) -> str:
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


# ---------- Open ----------------------------------------------------------


def open_task_log_viewer(state: State, task: Task) -> None:
    """Install the viewer onto state and spawn the loader thread.

    Caller is responsible for verifying the task has a `run_id`
    (the action menu only surfaces `View log` when it does).
    `only_failed` mirrors the focused task's status: failed runs get
    the `--log-failed` short-form (matches what gh CLI users reach
    for first), everything else gets the full `--log`. `job_id`
    scopes the fetch to a single job when the focused task is a job
    sub-task (Task metadata's `job_id` set by `_poll_run`)."""
    meta = state.tasks.get_meta(task)
    if meta is None or meta.run_id is None or not meta.slug:
        return
    only_failed = task.status == "fail"
    viewer = TaskLogViewer(
        task=task,
        slug=meta.slug,
        run_id=meta.run_id,
        job_id=meta.job_id,
        workflow_name=meta.workflow_name or "",
        only_failed=only_failed,
    )
    state.task_log_viewer = viewer
    threading.Thread(target=_load_log, args=(viewer,), daemon=True).start()


def _load_log(viewer: TaskLogViewer) -> None:
    """Background loader. Calls `fetch_run_log` and lands the result
    on the viewer under its lock. Bails on `cancel_event` so a user
    closing the modal mid-fetch doesn't stomp post-close state."""
    try:
        if viewer.cancel_event.is_set():
            return
        ok, lines, err = fetch_run_log(
            viewer.slug, viewer.run_id,
            job_id=viewer.job_id, only_failed=viewer.only_failed)
        if viewer.cancel_event.is_set():
            return
        with viewer.lock:
            if ok:
                viewer.lines = lines if lines else ["(no log output yet)"]
            else:
                viewer.error = err or "fetch failed"
                viewer.lines = []
            viewer.loading = False
    finally:
        if viewer.loading:
            with viewer.lock:
                viewer.loading = False


# ---------- Draw ----------------------------------------------------------


def _hints() -> List[Hint]:
    return [
        Hint(KEY_UP_DOWN, "scroll"),
        Hint(KEY_ESC, "close"),
    ]


def draw_task_log_viewer(stdscr, state: State, sidebar_x: int) -> None:
    viewer = state.task_log_viewer
    if viewer is None:
        return
    with viewer.lock:
        lines = list(viewer.lines)
        loading = viewer.loading
        error = viewer.error
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
    status_text = f"State: {_status_label(task)}"
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
        with viewer.lock:
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
    viewer = state.task_log_viewer
    if viewer is None:
        return
    # Enter / Esc / Tab all close — mirror diff_viewer's gesture set
    # so muscle memory carries over between the two scrollable panes.
    if key in (9, 10, 13, curses.KEY_ENTER, 27):
        viewer.cancel_event.set()
        state.task_log_viewer = None
        return
    with viewer.lock:
        cur = viewer.scroll
    if key == curses.KEY_UP:
        _set_scroll(viewer, max(0, cur - 1))
        return
    if key == curses.KEY_DOWN:
        # Clamped to max_scroll at draw time once body_h is known.
        _set_scroll(viewer, cur + 1)
        return
    if key == curses.KEY_PPAGE:
        _set_scroll(viewer, max(0, cur - 10))
        return
    if key == curses.KEY_NPAGE:
        _set_scroll(viewer, cur + 10)
        return
    if key == curses.KEY_HOME:
        _set_scroll(viewer, 0)
        return
    if key == curses.KEY_END:
        with viewer.lock:
            n = len(viewer.lines)
        _set_scroll(viewer, n)
        return


def _set_scroll(viewer: TaskLogViewer, value: int) -> None:
    with viewer.lock:
        viewer.scroll = value


__all__ = [
    "open_task_log_viewer",
    "draw_task_log_viewer",
    "handle_task_log_viewer_key",
]
