"""Task-detail modal rendering."""
from __future__ import annotations

import curses
import time

from core.state.app import State
from core.state.task_detail import TaskActionMenu
from core.runtime.tasks import Task
from features.task_detail.actions import handle_task_action_menu_key as handle_key
from features.task_detail.projection import (
    format_duration,
    is_terminal,
    is_safe_browser_url,
    status_label,
)
from features.task_log_viewer.session import open_task_log_viewer

from ..colors import (
    PAIR_DLG_CYAN, PAIR_DLG_ERR,
    PAIR_DLG_FG, PAIR_DLG_OK, PAIR_DLG_WARN,
)
from ..geometry import draw_modal_fill, modal_geometry, safe_addstr
from ..hints import (
    KEY_ENTER, KEY_ESC, KEY_UP_DOWN, Hint, render_hints,
)


def _hints_main(menu: TaskActionMenu) -> list:
    hints = [Hint(KEY_UP_DOWN, "select")]
    if 0 <= menu.selected < len(menu.items):
        item = menu.items[menu.selected]
        if item.enabled:
            hints.append(Hint(KEY_ENTER, item.label))
        else:
            reason = f" ({item.reason})" if item.reason else ""
            hints.append(Hint(KEY_ENTER, f"unavailable{reason}"))
    hints.append(Hint(KEY_ESC, "close"))
    return hints


def _hints_sub_picker(menu: TaskActionMenu) -> list:
    if not menu.sub_picker_options:
        return [Hint(KEY_ESC, "back")]
    name = menu.sub_picker_options[menu.sub_picker_selected]
    return [
        Hint(KEY_UP_DOWN, "select"),
        Hint(KEY_ENTER, f"chain to {name}"),
        Hint(KEY_ESC, "back"),
    ]


def _draw_main_hints(stdscr, menu: TaskActionMenu, y: int, x: int,
                     w: int, attr: int) -> None:
    render_hints(stdscr, y, x, w, _hints_main(menu), attr=attr)


def _draw_sub_picker_hints(stdscr, menu: TaskActionMenu, y: int, x: int,
                           w: int, attr: int) -> None:
    render_hints(stdscr, y, x, w, _hints_sub_picker(menu), attr=attr)


_STATUS_COLOURS = {
    "running": PAIR_DLG_CYAN,
    "pending": PAIR_DLG_CYAN,
    "ok": PAIR_DLG_OK,
    "fail": PAIR_DLG_ERR,
    "warn": PAIR_DLG_WARN,
}


def draw_task_action_menu(stdscr, state: State, sidebar_x: int) -> None:
    menu = state.task_action_menu
    if menu is None:
        return
    task = menu.task
    followup = state.workflow_followups.record_for_task(task)
    run_record = state.workflow_runs.record_for_task(task)
    children = state.tasks.children_of(task)

    sb = curses.color_pair(PAIR_DLG_FG)
    child_rows = min(8, len(children))
    detail_lines = 6
    sub_section_h = (1 + max(1, child_rows) + 1) if children else 0
    action_rows = len(menu.items) + 1
    content_h = (
        1 + 1 + 1 + detail_lines + sub_section_h + action_rows
        + 1 + 1 + 1
    )
    x, y, w, h = modal_geometry(stdscr, sidebar_x, 80, content_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    line = y + 1
    safe_addstr(stdscr, line, inner_x, "Task detail",
                curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN))
    line += 2

    line = _draw_task_summary(stdscr, task, line, inner_x, w, sb)
    line = _draw_run_detail(stdscr, state, menu, run_record, followup,
                            children, line, inner_x, w, sb)
    if children:
        line = _draw_children(stdscr, children, line, inner_x, w, sb)
    _draw_actions(stdscr, menu, line, inner_x, w, sb)

    _draw_main_hints(stdscr, menu, y + h - 2, inner_x, w - 4,
                     sb | curses.A_DIM)

    if menu.sub_picker_open and menu.sub_picker_options:
        _draw_sub_picker(stdscr, state, sidebar_x)


def _draw_task_summary(
    stdscr,
    task: Task,
    line: int,
    inner_x: int,
    width: int,
    sb: int,
) -> int:
    safe_addstr(stdscr, line, inner_x, task.label[: width - 4], sb)
    line += 1
    status_pair = _STATUS_COLOURS.get(task.status, PAIR_DLG_FG)
    elapsed = max(0.0, time.monotonic() - task.started_at)
    if is_terminal(task.status) and task.finished_at is not None:
        duration = task.finished_at - task.started_at
        suffix = f"finished, {format_duration(duration)}"
    else:
        suffix = f"{format_duration(elapsed)} elapsed"
    status_text = f"{status_label(task)} · {suffix}"
    safe_addstr(stdscr, line, inner_x, status_text[: width - 4],
                curses.color_pair(status_pair))
    line += 1
    if task.message:
        safe_addstr(stdscr, line, inner_x,
                    task.message[: width - 4], sb | curses.A_DIM)
    return line + 1


def _draw_run_detail(
    stdscr,
    state: State,
    menu: TaskActionMenu,
    run_record,
    followup,
    children: list[Task],
    line: int,
    inner_x: int,
    width: int,
    sb: int,
) -> int:
    if run_record is not None:
        if run_record.workflow_name:
            safe_addstr(stdscr, line, inner_x,
                        f"Workflow: {run_record.workflow_name}"[: width - 4],
                        sb | curses.A_DIM)
            line += 1
        if run_record.run_id is not None:
            safe_addstr(stdscr, line, inner_x,
                        f"Run id:   {run_record.run_id}"[: width - 4],
                        sb | curses.A_DIM)
            line += 1
    if followup is not None:
        chained = getattr(menu, "_pending_workflow", None)
        if chained is not None:
            for child in children:
                child_followup = state.workflow_followups.record_for_task(child)
                if child_followup is not None and child_followup.target:
                    safe_addstr(
                        stdscr,
                        line,
                        inner_x,
                        f"Then run: {child_followup.target}"[: width - 4],
                        sb | curses.A_DIM,
                    )
                    line += 1
                    break
    return line


def _draw_children(
    stdscr,
    children: list[Task],
    line: int,
    inner_x: int,
    width: int,
    sb: int,
) -> int:
    line += 1
    safe_addstr(stdscr, line, inner_x,
                f"Sub-tasks ({len(children)}):", sb | curses.A_BOLD)
    line += 1
    now = time.monotonic()
    for child in children[:8]:
        child_status = status_label(child)
        duration = ((child.finished_at or now) - child.started_at)
        message = child.message[: 30] if child.message else ""
        cell = f"  {child_status:<10} {child.label.strip()}"
        tail = f"{format_duration(duration)}"
        if message and child.status in ("running", "pending"):
            tail += f" · {message}"
        line_text = f"{cell} {tail}"
        safe_addstr(stdscr, line, inner_x, line_text[: width - 4],
                    curses.color_pair(
                        _STATUS_COLOURS.get(child.status, PAIR_DLG_FG)))
        line += 1
    if len(children) > 8:
        safe_addstr(stdscr, line, inner_x,
                    f"  … and {len(children) - 8} more",
                    sb | curses.A_DIM)
        line += 1
    return line


def _draw_actions(
    stdscr,
    menu: TaskActionMenu,
    line: int,
    inner_x: int,
    width: int,
    sb: int,
) -> None:
    safe_addstr(stdscr, line, inner_x, "Actions:", sb | curses.A_BOLD)
    line += 1
    for i, item in enumerate(menu.items):
        focused = i == menu.selected
        prefix = "→ " if focused else "  "
        label = item.label
        if not item.enabled and item.reason:
            label = f"{label}  ({item.reason})"
        if focused and item.enabled:
            attr = sb | curses.A_REVERSE
        elif focused:
            attr = sb | curses.A_REVERSE | curses.A_DIM
        elif not item.enabled:
            attr = sb | curses.A_DIM
        else:
            attr = sb
        safe_addstr(stdscr, line, inner_x,
                    (prefix + label).ljust(width - 4), attr)
        line += 1


def _draw_sub_picker(stdscr, state: State, sidebar_x: int) -> None:
    menu = state.task_action_menu
    if menu is None:
        return
    body_h = max(3, min(10, len(menu.sub_picker_options)))
    content_h = 1 + 1 + body_h + 1 + 1 + 2
    x, y, w, h = modal_geometry(stdscr, sidebar_x, 60, content_h)
    sb = curses.color_pair(PAIR_DLG_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)
    inner_x = x + 2

    safe_addstr(stdscr, y + 1, inner_x, "Pick then-run target",
                curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN))
    for i, name in enumerate(menu.sub_picker_options):
        focused = i == menu.sub_picker_selected
        prefix = "→ " if focused else "  "
        attr = sb | curses.A_REVERSE if focused else sb
        safe_addstr(stdscr, y + 3 + i, inner_x,
                    (prefix + name).ljust(w - 4), attr)
    _draw_sub_picker_hints(stdscr, menu, y + h - 2, inner_x, w - 4,
                           sb | curses.A_DIM)


def handle_task_action_menu_key(state: State, key: int) -> None:
    effect = handle_key(state, key)
    if effect.kind == "open_task_log" and effect.task is not None:
        open_task_log_viewer(state, effect.task)


_is_safe_browser_url = is_safe_browser_url
