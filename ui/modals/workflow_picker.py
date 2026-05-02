"""GitHub Actions workflow_dispatch picker — sub-modal of the action menu."""
from __future__ import annotations

import curses
from typing import Tuple

from models import State, WorkflowPicker
from workers import kick_off_manual_dispatch

from ..colors import PAIR_SB_CYAN, PAIR_SB_FG
from ..geometry import draw_modal_fill, modal_geometry, safe_addstr
from ..hints import (
    KEY_ENTER, KEY_ESC, KEY_UP_DOWN, Hint, render_hints,
)


def _hints(picker) -> list:
    """Footer hints for the workflow_dispatch picker. Enter is gated
    on the focused workflow being runnable — disabled / non-
    dispatchable rows show why instead of pretending Enter will fire."""
    if not picker.workflows:
        return [Hint(KEY_ESC, "back")]
    hints = [Hint(KEY_UP_DOWN, "select")]
    wf = picker.workflows[picker.selected]
    runnable, reason = _workflow_row_status(wf)
    if runnable:
        hints.append(Hint(KEY_ENTER, f"run on {picker.branch}"))
    else:
        hints.append(Hint(KEY_ENTER, f"unavailable {reason}".rstrip()))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


def _workflow_row_status(wf) -> Tuple[bool, str]:
    """Return (runnable, reason) for a workflow row in the picker.
    `runnable` gates Enter; `reason` is a short trailing tag shown after
    the name for any unrunnable row (empty for runnable rows)."""
    if wf.state.startswith("disabled"):
        return False, f"({wf.state.replace('_', ' ')})"
    if not wf.dispatchable:
        return False, "(no workflow_dispatch trigger)"
    return True, ""


def open_workflow_picker(state: State) -> None:
    """Open the workflow picker on the focused row's repo. Lists every
    workflow we discovered locally (including non-dispatchable + remotely
    disabled ones) so the user can see *why* a workflow can't be run from
    here. Enter on a runnable row triggers the dispatch + tracks the
    resulting run via kick_off_manual_dispatch; on disabled rows it's a
    no-op. Defaults the cursor to the first runnable row, falling back
    to row 0."""
    menu = state.action_menu
    if menu is None:
        return
    target_repo = menu.target_repo
    if target_repo is None and menu.target_child is not None:
        target_repo = menu.target_child.repo
    if target_repo is None:
        return
    workflows = list(target_repo.workflows)
    if not workflows:
        return
    initial = 0
    for i, wf in enumerate(workflows):
        if wf.dispatchable and not wf.state.startswith("disabled"):
            initial = i
            break
    state.workflow_picker = WorkflowPicker(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
        workflows=workflows,
        branch=menu.branch or target_repo.branch,
        selected=initial,
    )


def draw_workflow_picker(stdscr, state: State, sidebar_x: int) -> None:
    picker = state.workflow_picker
    if picker is None:
        return
    body_h = max(3, min(15, len(picker.workflows)))
    # +2 reserves a blank row above the title and below the footer
    # hint so the modal doesn't feel pasted against its own edges.
    content_h = 1 + 1 + body_h + 1 + 1 + 2
    x, y, w, h = modal_geometry(stdscr, sidebar_x, 70, content_h)
    sb = curses.color_pair(PAIR_SB_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    safe_addstr(stdscr, y + 1, inner_x,
                f"Run workflow on {picker.branch} — {picker.target_label}"
                [: w - 4],
                curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN))

    if not picker.workflows:
        safe_addstr(stdscr, y + 3, inner_x,
                    "(no workflows in this repo)", sb | curses.A_DIM)
        safe_addstr(stdscr, y + h - 2, inner_x,
                    "Esc back", sb | curses.A_DIM)
        return

    if picker.selected < picker.scroll:
        picker.scroll = picker.selected
    elif picker.selected >= picker.scroll + body_h:
        picker.scroll = picker.selected - body_h + 1
    picker.scroll = max(0, min(picker.scroll,
                               max(0, len(picker.workflows) - body_h)))

    for i in range(body_h):
        idx = picker.scroll + i
        if idx >= len(picker.workflows):
            break
        wf = picker.workflows[idx]
        focused = (idx == picker.selected)
        runnable, reason = _workflow_row_status(wf)
        prefix = "→ " if focused else "  "
        label = wf.name if runnable else f"{wf.name}  {reason}"
        text = (prefix + label).ljust(w - 4)
        if focused and runnable:
            attr = sb | curses.A_REVERSE
        elif focused:
            attr = sb | curses.A_REVERSE | curses.A_DIM
        elif not runnable:
            attr = sb | curses.A_DIM
        else:
            attr = sb
        safe_addstr(stdscr, y + 3 + i, inner_x, text, attr)

    if picker.scroll > 0:
        safe_addstr(stdscr, y + 2, inner_x,
                    f"↑ {picker.scroll} more above", sb | curses.A_DIM)
    if picker.scroll + body_h < len(picker.workflows):
        below = len(picker.workflows) - (picker.scroll + body_h)
        safe_addstr(stdscr, y + 3 + body_h, inner_x,
                    f"↓ {below} more below", sb | curses.A_DIM)

    render_hints(stdscr, y + h - 2, inner_x, w - 4, _hints(picker),
                 attr=sb | curses.A_DIM)


def handle_workflow_picker_key(state: State, key: int) -> None:
    picker = state.workflow_picker
    if picker is None:
        return
    if key == 27:
        state.workflow_picker = None
        return
    if not picker.workflows:
        return
    if key == curses.KEY_UP:
        picker.selected = max(0, picker.selected - 1)
        return
    if key == curses.KEY_DOWN:
        picker.selected = min(len(picker.workflows) - 1, picker.selected + 1)
        return
    if key == curses.KEY_PPAGE:
        picker.selected = max(0, picker.selected - 10)
        return
    if key == curses.KEY_NPAGE:
        picker.selected = min(len(picker.workflows) - 1, picker.selected + 10)
        return
    if key in (10, 13, curses.KEY_ENTER):
        wf = picker.workflows[picker.selected]
        runnable, _ = _workflow_row_status(wf)
        if not runnable:
            return  # silently no-op on disabled / non-dispatchable rows
        if picker.target_repo is not None:
            kick_off_manual_dispatch(
                state, picker.target_repo, wf.name, picker.branch)
        state.workflow_picker = None
        state.action_menu = None
