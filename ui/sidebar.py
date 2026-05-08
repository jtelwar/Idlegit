"""Right-hand task panel — `draw_sidebar` and the per-row geometry
helpers it needs. Keeps focus styling, scroll arrows, count display,
and the per-task status icons together so the panel stays a coherent
unit independent of the main screen."""
from __future__ import annotations

import curses
import time

from models import State
from git_ops import format_time_ago

from .colors import (
    PAIR_SB_CYAN, PAIR_SB_CYAN_ACTIVE,
    PAIR_SB_ERR, PAIR_SB_ERR_ACTIVE,
    PAIR_SB_FG, PAIR_SB_FG_ACTIVE,
    PAIR_SB_OK, PAIR_SB_OK_ACTIVE,
    PAIR_SB_WARN, PAIR_SB_WARN_ACTIVE,
)
from .geometry import safe_addstr
from .hints import KEY_CTRL_R, KEY_SHIFT_TAB, Hint, render_hints


SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _empty_panel_hints(focused: bool) -> list:
    """Hints shown beneath '(no tasks yet)' when the task panel is
    empty. Ctrl+R is what surfaces new tasks (kicks off a refresh that
    may produce them); Shift+Tab is offered only when this panel has
    focus, since that's when the user actually needs the way out."""
    hints = [Hint(KEY_CTRL_R, "refresh")]
    if focused:
        hints.append(Hint(KEY_SHIFT_TAB, "back to repos"))
    return hints


def _row_span(task) -> int:
    """How many vertical rows a task occupies in the sidebar — 2 if its
    fail/warn message gets a detail row, 1 otherwise."""
    return 2 if (task.message and task.status in ("fail", "warn")) else 1


def _measure_visible(items, start_idx: int, body_top: int,
                     body_max_y: int) -> int:
    """Return the index of the last task that fits in the panel when
    rendering from `start_idx`. Returns start_idx - 1 when nothing fits."""
    y = body_top
    last = start_idx - 1
    for i in range(start_idx, len(items)):
        span = _row_span(items[i])
        if y + span > body_max_y:
            break
        last = i
        y += span
    return last


def draw_sidebar(stdscr, state: State, x: int, w: int) -> None:
    if w <= 0:
        return
    h, _ = stdscr.getmaxyx()
    focused = state.focused_panel == "tasks"
    fill_attr = curses.color_pair(
        PAIR_SB_FG_ACTIVE if focused else PAIR_SB_FG)
    # When focused, every text colour pair must use sb_bg_active so icons
    # and labels don't punch holes in the lighter fill background.
    sb = fill_attr
    c_cyan = PAIR_SB_CYAN_ACTIVE if focused else PAIR_SB_CYAN
    c_ok = PAIR_SB_OK_ACTIVE if focused else PAIR_SB_OK
    c_err = PAIR_SB_ERR_ACTIVE if focused else PAIR_SB_ERR
    c_warn = PAIR_SB_WARN_ACTIVE if focused else PAIR_SB_WARN

    # Leave the very top row at the default terminal background so the
    # title row (`Idlegit · …`) reads as one continuous strip instead of
    # being clipped by the sidebar panel.
    fill = " " * w
    for y in range(1, h):
        safe_addstr(stdscr, y, x, fill, fill_attr)

    header_y = 2
    items = state.tasks.snapshot()

    # Header carries the panel's focus accent — bright cyan when
    # focused, dim white otherwise. Same treatment as the
    # "Repositories" header in `draw_main`.
    header_attr = (curses.color_pair(c_cyan) | curses.A_BOLD if focused
                   else sb | curses.A_DIM | curses.A_BOLD)

    if not items:
        safe_addstr(stdscr, header_y, x + 1, "Tasks", header_attr)
        safe_addstr(stdscr, header_y + 2, x + 1, "(no tasks yet)",
                    sb | curses.A_DIM)
        render_hints(stdscr, header_y + 3, x + 1, max(0, w - 2),
                     _empty_panel_hints(focused), attr=sb | curses.A_DIM)
        return

    sel_idx = state.task_selected if focused else -1

    body_top = header_y + 2
    # Reserve the last row for the "+N more" overflow hint when needed.
    body_max_y = h - 1

    # Auto-scroll so the selected task is visible while focused.
    state.task_scroll = max(0, min(state.task_scroll, max(0, len(items) - 1)))
    if focused:
        if state.task_selected < state.task_scroll:
            state.task_scroll = state.task_selected
        last_visible = _measure_visible(
            items, state.task_scroll, body_top, body_max_y)
        while (last_visible < state.task_selected
                and state.task_scroll < len(items) - 1):
            state.task_scroll += 1
            last_visible = _measure_visible(
                items, state.task_scroll, body_top, body_max_y)
    last_visible = _measure_visible(
        items, state.task_scroll, body_top, body_max_y)

    has_more_above = state.task_scroll > 0
    has_more_below = last_visible < len(items) - 1

    # Header: "Tasks" left, count + ↑ ↓ right. Arrows brighten when
    # there's content in that direction the user hasn't seen yet. The
    # header itself only takes the accent colour when the panel has
    # focus — see `header_attr` above.
    safe_addstr(stdscr, header_y, x + 1, "Tasks", header_attr)
    count_str = (f"{state.task_selected + 1}/{len(items)}"
                 if focused else str(len(items)))
    # Layout: "<count> ↑↓ " — single space before the up arrow, no gap
    # between ↑↓ (terminals don't do sub-cell spacing), then one cell
    # of breathing room before the panel's right edge.
    right_w = len(count_str) + 1 + 1 + 1 + 1  # count, sp, ↑, ↓, sp
    rx = x + w - right_w
    if rx >= x + 1 + len("Tasks") + 1:
        safe_addstr(stdscr, header_y, rx, count_str, sb | curses.A_DIM)
        up_x = rx + len(count_str) + 1
        down_x = up_x + 1
        on_attr = curses.color_pair(c_cyan) | curses.A_BOLD
        off_attr = sb | curses.A_DIM
        safe_addstr(stdscr, header_y, up_x, "↑",
                    on_attr if has_more_above else off_attr)
        safe_addstr(stdscr, header_y, down_x, "↓",
                    on_attr if has_more_below else off_attr)

    # Body: render visible window starting at task_scroll.
    spinner = SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]
    auto_remove = state.auto_remove_completed_after
    now = time.monotonic()
    y = body_top
    for i in range(state.task_scroll, len(items)):
        t = items[i]
        span = _row_span(t)
        if y + span > body_max_y:
            break
        if t.status == "running":
            icon, color = spinner, c_cyan
        elif t.status == "pending":
            # Clock face — task is queued, waiting on something else.
            icon, color = "◷", c_cyan
        elif t.status == "ok":
            icon, color = "✓", c_ok
        elif t.status == "fail":
            icon, color = "✗", c_err
        else:  # warn
            icon, color = "⚠", c_warn

        elapsed = max(0.0, now - t.started_at)
        time_tag = format_time_ago(elapsed)
        label_start = x + 3
        tag_x = x + w - len(time_tag) - 1
        max_label_w = max(0, tag_x - label_start - 1)

        is_selected = i == sel_idx
        # Suppress the fade overlay on the selected row so its colour
        # change isn't muddied by the progress-bar background.
        is_fading = (not is_selected
                     and auto_remove >= 0
                     and t.status in ("ok", "fail", "warn")
                     and t.finished_at is not None)

        if is_selected:
            safe_addstr(stdscr, y, x, "▸",
                        curses.color_pair(c_cyan) | curses.A_BOLD)
        safe_addstr(stdscr, y, x + 1, icon, curses.color_pair(color))
        if is_selected:
            label_attr = curses.color_pair(c_cyan) | curses.A_BOLD
        elif t.status == "pending":
            # Subtle cyan + dim so the "↪ then run: …" placeholder reads
            # as a chained follow-up rather than another regular running
            # row — matches the cyan icon in the gutter.
            label_attr = curses.color_pair(c_cyan) | curses.A_DIM
        elif t.status == "running":
            label_attr = sb
        else:
            label_attr = sb | curses.A_DIM
        safe_addstr(stdscr, y, label_start, t.label[:max_label_w], label_attr)
        if max_label_w > 0 and tag_x > label_start:
            safe_addstr(stdscr, y, tag_x, time_tag, sb | curses.A_DIM)

        if is_fading:
            if auto_remove > 0:
                since_done = max(0.0, now - t.finished_at)
                progress = min(1.0, since_done / auto_remove)
            else:
                progress = 1.0
            row_w = max(0, w - 1)
            fill_w = max(0, int(round((1.0 - progress) * row_w)))
            if fill_w > 0:
                try:
                    stdscr.chgat(y, x + 1, fill_w, sb | curses.A_REVERSE)
                except curses.error:
                    pass

        y += 1
        if span == 2:
            detail = t.message[: max(0, w - 6)]
            safe_addstr(stdscr, y, x + 5, detail,
                        curses.color_pair(color) | curses.A_DIM)
            y += 1

    if has_more_below:
        n_below = len(items) - 1 - last_visible
        safe_addstr(stdscr, h - 1, x + 1,
                    f"+{n_below} more (↓)",
                    sb | curses.A_DIM)
