"""Tab-on-task action menu — modal showing the focused task's detail
(label, status, durations, run id/url, sub-tasks) plus controls to
cancel a running workflow run, change/clear a chained then-run,
open the run in a browser, or remove a finished task from the panel."""
from __future__ import annotations

import curses
import subprocess
import sys
import threading
import time
from typing import List, Optional
from urllib.parse import urlparse

from models import State, Task, TaskActionMenu, TaskActionMenuItem
from git_ops import cancel_run

from ..colors import (
    PAIR_SB_CYAN, PAIR_SB_ERR,
    PAIR_SB_FG, PAIR_SB_OK, PAIR_SB_WARN,
)
from ..geometry import draw_modal_fill, modal_geometry, safe_addstr
from ..hints import (
    KEY_ENTER, KEY_ESC, KEY_UP_DOWN, Hint, render_hints,
)


def _hints_main(menu: TaskActionMenu) -> list:
    """Footer hints for the task-detail modal's action list. Enter
    adopts the focused item's own label so the user sees exactly which
    action is about to fire — and switches to a dim "(unavailable)"
    description for disabled rows."""
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
    """Footer hints for the inline workflow picker that surfaces inside
    the task-detail modal when the user runs `change_then_run`."""
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
    """Single call site keeps render_hints visibly used — sidesteps the
    autoformatter pruning the import on intermediate edits."""
    render_hints(stdscr, y, x, w, _hints_main(menu), attr=attr)


def _draw_sub_picker_hints(stdscr, menu: TaskActionMenu, y: int, x: int,
                           w: int, attr: int) -> None:
    """Counterpart for the sub-picker's footer."""
    render_hints(stdscr, y, x, w, _hints_sub_picker(menu), attr=attr)


# ---------- Helpers --------------------------------------------------------


def _dispatchable_targets(repo) -> List[str]:
    """Workflow names eligible as `then run` targets for this repo —
    dispatchable + not disabled-on-github."""
    if repo is None:
        return []
    return [w.name for w in repo.workflows
            if w.dispatchable and not w.state.startswith("disabled")]


def _format_duration(seconds: float) -> str:
    """Compact duration label for sub-task rows: 12s / 1m 45s / 2h 15m."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h {m}m"


def _is_terminal(status: str) -> bool:
    return status in ("ok", "fail", "warn")


def _is_pending_then_run(state: State, task: Task) -> bool:
    """A pending then-run placeholder is the only non-job task with a
    non-None `pending_target` in its metadata."""
    meta = state.tasks.get_meta(task)
    return meta is not None and bool(meta.pending_target)


# ---------- Open: build the action item list dynamically ------------------


def open_task_action_menu(state: State, task: Task) -> None:
    """Construct + install the task-detail modal for `task`. Items are
    derived from the task's status + metadata — a job sub-task gets
    "Cancel this run" because cancelling the parent gh run cancels all
    its jobs, while a plain bookkeeping row only gets "Close"."""
    meta = state.tasks.get_meta(task)
    items: List[TaskActionMenuItem] = []

    # Cancel this run — works for both run tasks and job sub-tasks
    # (both carry meta.run_id pointing at the same gh run).
    can_cancel = (meta is not None
                  and meta.run_id is not None
                  and meta.slug
                  and not _is_terminal(task.status))
    if can_cancel:
        items.append(TaskActionMenuItem(
            id="cancel_run", label="Cancel this run"))
    elif meta is not None and meta.run_id is not None:
        items.append(TaskActionMenuItem(
            id="cancel_run", label="Cancel this run",
            enabled=False,
            reason=("already finished" if _is_terminal(task.status)
                    else "no run id")))

    # Then-run management — both for the parent run task (whose pending
    # placeholder lives in its children) and for the placeholder itself.
    pending_child: Optional[Task] = None
    pending_workflow_name: Optional[str] = None
    if meta and meta.workflow_name and not _is_pending_then_run(state, task):
        # Parent run task: look for a child with status pending +
        # pending_target metadata.
        for child in state.tasks.children_of(task):
            cm = state.tasks.get_meta(child)
            if cm is not None and cm.pending_target:
                pending_child = child
                pending_workflow_name = meta.workflow_name
                break
    elif _is_pending_then_run(state, task):
        # The pending placeholder itself.
        pending_child = task
        pending_workflow_name = meta.pending_after_workflow if meta else None

    can_change = pending_child is not None and meta is not None
    if can_change:
        items.append(TaskActionMenuItem(
            id="change_then_run", label="Change then-run target"))
        items.append(TaskActionMenuItem(
            id="clear_then_run", label="Cancel then-run"))

    # Open in browser — workflow runs only.
    if meta is not None and meta.run_url:
        items.append(TaskActionMenuItem(
            id="open_in_browser", label="Open run in browser"))

    # Remove from list — completed tasks only.
    if _is_terminal(task.status):
        items.append(TaskActionMenuItem(
            id="remove", label="Remove from list"))

    items.append(TaskActionMenuItem(id="close", label="Close"))

    # Default cursor to first enabled item.
    initial = 0
    for i, it in enumerate(items):
        if it.enabled:
            initial = i
            break

    state.task_action_menu = TaskActionMenu(
        task=task, items=items, selected=initial,
    )
    # Cache pending-child info on the modal so the handler can find it
    # without re-walking the children list.
    state.task_action_menu._pending_child = pending_child  # type: ignore[attr-defined]
    state.task_action_menu._pending_workflow = pending_workflow_name  # type: ignore[attr-defined]


# ---------- Draw -----------------------------------------------------------


_STATUS_COLOURS = {
    "running": PAIR_SB_CYAN,
    "pending": PAIR_SB_CYAN,
    "ok": PAIR_SB_OK,
    "fail": PAIR_SB_ERR,
    "warn": PAIR_SB_WARN,
}


def _status_label(t: Task) -> str:
    if t.status == "running":
        return "running"
    if t.status == "pending":
        return "pending"
    if t.status == "ok":
        return "✓ ok"
    if t.status == "fail":
        return "✗ failed"
    return "⚠ warn"


def draw_task_action_menu(stdscr, state: State, sidebar_x: int) -> None:
    menu = state.task_action_menu
    if menu is None:
        return
    task = menu.task
    meta = state.tasks.get_meta(task)
    children = state.tasks.children_of(task)

    sb = curses.color_pair(PAIR_SB_FG)

    # Compute layout: a fixed header block, a variable sub-task list,
    # then the actions list. Cap heights so the modal stays readable
    # on small terminals.
    n_children = min(8, len(children))
    detail_lines = 6  # header + status + workflow + run id + then-run + spacer
    sub_section_h = (1 + max(1, n_children) + 1) if children else 0
    n_actions = len(menu.items) + 1  # +1 for "Actions:" header
    content_h = (
        1                # top blank
        + 1              # title row
        + 1              # blank
        + detail_lines
        + sub_section_h
        + n_actions
        + 1              # blank above hint
        + 1              # hint
        + 1              # blank bottom
    )
    x, y, w, h = modal_geometry(stdscr, sidebar_x, 80, content_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    line = y + 1
    safe_addstr(stdscr, line, inner_x, "Task detail",
                curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN))
    line += 2

    # Task label + status
    safe_addstr(stdscr, line, inner_x, task.label[: w - 4], sb)
    line += 1
    status_pair = _STATUS_COLOURS.get(task.status, PAIR_SB_FG)
    elapsed = max(0.0, time.monotonic() - task.started_at)
    if _is_terminal(task.status) and task.finished_at is not None:
        duration = task.finished_at - task.started_at
        suffix = f"finished, {_format_duration(duration)}"
    else:
        suffix = f"{_format_duration(elapsed)} elapsed"
    status_text = f"{_status_label(task)} · {suffix}"
    safe_addstr(stdscr, line, inner_x, status_text[: w - 4],
                curses.color_pair(status_pair))
    line += 1
    if task.message:
        safe_addstr(stdscr, line, inner_x,
                    task.message[: w - 4], sb | curses.A_DIM)
        line += 1
    else:
        line += 1

    # Workflow / run id
    if meta is not None:
        if meta.workflow_name:
            safe_addstr(stdscr, line, inner_x,
                        f"Workflow: {meta.workflow_name}"[: w - 4],
                        sb | curses.A_DIM)
            line += 1
        if meta.run_id is not None:
            safe_addstr(stdscr, line, inner_x,
                        f"Run id:   {meta.run_id}"[: w - 4],
                        sb | curses.A_DIM)
            line += 1
        # Pending then-run target — show the chained workflow name so
        # the user can confirm what they're about to cancel/change.
        chained = getattr(menu, "_pending_workflow", None)
        if chained is not None:
            for child in children:
                cm = state.tasks.get_meta(child)
                if cm is not None and cm.pending_target:
                    safe_addstr(stdscr, line, inner_x,
                                f"Then run: {cm.pending_target}"[: w - 4],
                                sb | curses.A_DIM)
                    line += 1
                    break

    # Sub-tasks (if any)
    if children:
        line += 1
        safe_addstr(stdscr, line, inner_x,
                    f"Sub-tasks ({len(children)}):", sb | curses.A_BOLD)
        line += 1
        now = time.monotonic()
        for child in children[:8]:
            cstatus = _status_label(child)
            cdur = ((child.finished_at or now) - child.started_at)
            cmsg = child.message[: 30] if child.message else ""
            cell = f"  {cstatus:<10} {child.label.strip()}"
            tail = f"{_format_duration(cdur)}"
            if cmsg and child.status in ("running", "pending"):
                tail += f" · {cmsg}"
            line_text = f"{cell} {tail}"
            safe_addstr(stdscr, line, inner_x, line_text[: w - 4],
                        curses.color_pair(
                            _STATUS_COLOURS.get(child.status, PAIR_SB_FG)))
            line += 1
        if len(children) > 8:
            safe_addstr(stdscr, line, inner_x,
                        f"  … and {len(children) - 8} more",
                        sb | curses.A_DIM)
            line += 1

    # Actions list
    safe_addstr(stdscr, line, inner_x, "Actions:", sb | curses.A_BOLD)
    line += 1
    for i, item in enumerate(menu.items):
        focused = (i == menu.selected)
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
                    (prefix + label).ljust(w - 4), attr)
        line += 1

    _draw_main_hints(stdscr, menu, y + h - 2, inner_x, w - 4,
                     sb | curses.A_DIM)

    # Optional embedded sub-picker (used by `change_then_run`).
    if menu.sub_picker_open and menu.sub_picker_options:
        _draw_sub_picker(stdscr, state, sidebar_x)


def _draw_sub_picker(stdscr, state: State, sidebar_x: int) -> None:
    """Mini workflow picker overlay used by `change_then_run`. Sits
    on top of the detail modal so the user can confirm without losing
    the detail context."""
    menu = state.task_action_menu
    if menu is None:
        return
    body_h = max(3, min(10, len(menu.sub_picker_options)))
    # +2 for blank rows above title and below the footer hint.
    content_h = 1 + 1 + body_h + 1 + 1 + 2
    x, y, w, h = modal_geometry(stdscr, sidebar_x, 60, content_h)
    sb = curses.color_pair(PAIR_SB_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)
    inner_x = x + 2

    safe_addstr(stdscr, y + 1, inner_x, "Pick then-run target",
                curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN))
    for i, name in enumerate(menu.sub_picker_options):
        focused = i == menu.sub_picker_selected
        prefix = "→ " if focused else "  "
        attr = sb | curses.A_REVERSE if focused else sb
        safe_addstr(stdscr, y + 3 + i, inner_x,
                    (prefix + name).ljust(w - 4), attr)
    _draw_sub_picker_hints(stdscr, menu, y + h - 2, inner_x, w - 4,
                           sb | curses.A_DIM)


# ---------- Handle ---------------------------------------------------------


def handle_task_action_menu_key(state: State, key: int) -> None:
    menu = state.task_action_menu
    if menu is None:
        return

    # Sub-picker takes priority when open.
    if menu.sub_picker_open:
        _handle_sub_picker_key(state, key)
        return

    if key == 27:
        state.task_action_menu = None
        return
    if key == curses.KEY_UP and menu.items:
        menu.selected = (menu.selected - 1) % len(menu.items)
        return
    if key == curses.KEY_DOWN and menu.items:
        menu.selected = (menu.selected + 1) % len(menu.items)
        return
    if key in (10, 13, curses.KEY_ENTER) and menu.items:
        item = menu.items[menu.selected]
        if not item.enabled:
            return
        _dispatch_action(state, item.id)


def _dispatch_action(state: State, item_id: str) -> None:
    menu = state.task_action_menu
    if menu is None:
        return
    task = menu.task
    meta = state.tasks.get_meta(task)

    if item_id == "close":
        state.task_action_menu = None
        return

    if item_id == "remove":
        state.tasks.remove(task)
        state.task_action_menu = None
        return

    if item_id == "open_in_browser" and meta is not None and meta.run_url:
        if _is_safe_browser_url(meta.run_url):
            _open_in_browser(state, meta.run_url)
        else:
            t = state.tasks.add("open run URL")
            state.tasks.update(t, "warn", "unsafe URL")
        # Modal stays open so user can pick another action afterwards.
        return

    if item_id == "cancel_run" and meta is not None and meta.run_id:
        slug = meta.slug or ""
        run_id = meta.run_id
        repo_label = state.task_repo_label(meta.repo) if meta.repo else "?"
        wf = meta.workflow_name or "?"

        def cancel_worker() -> None:
            t = state.tasks.add(f"⊘ {repo_label}: cancel {wf}")
            ok, msg = cancel_run(slug, run_id)
            state.tasks.update(t, "ok" if ok else "fail", msg)

        threading.Thread(target=cancel_worker, daemon=True).start()
        state.task_action_menu = None
        return

    if item_id == "change_then_run":
        repo = meta.repo if meta else None
        options = _dispatchable_targets(repo)
        if not options:
            return  # nothing to pick
        menu.sub_picker_options = options
        menu.sub_picker_selected = 0
        menu.sub_picker_open = True
        return

    if item_id == "clear_then_run":
        _clear_then_run(state)
        state.task_action_menu = None
        return


def _handle_sub_picker_key(state: State, key: int) -> None:
    menu = state.task_action_menu
    if menu is None:
        return
    if key == 27:
        menu.sub_picker_open = False
        return
    if not menu.sub_picker_options:
        menu.sub_picker_open = False
        return
    if key == curses.KEY_UP:
        menu.sub_picker_selected = max(0, menu.sub_picker_selected - 1)
        return
    if key == curses.KEY_DOWN:
        menu.sub_picker_selected = min(
            len(menu.sub_picker_options) - 1, menu.sub_picker_selected + 1)
        return
    if key in (10, 13, curses.KEY_ENTER):
        chosen = menu.sub_picker_options[menu.sub_picker_selected]
        _set_then_run(state, chosen)
        menu.sub_picker_open = False
        state.task_action_menu = None


def _pending_child_ref(state: State) -> Optional[Task]:
    """Return the pending then-run placeholder Task associated with the
    currently-open modal, whether the focused task is the parent run
    or the placeholder itself."""
    menu = state.task_action_menu
    if menu is None:
        return None
    return getattr(menu, "_pending_child", None)


def _set_then_run(state: State, target: str) -> None:
    """Wire the chosen workflow into the right `repo.then_run_after_*`
    slot and update the pending placeholder's label accordingly."""
    placeholder = _pending_child_ref(state)
    if placeholder is None:
        return
    meta = state.tasks.get_meta(placeholder)
    if meta is None or meta.repo is None:
        return
    parent_workflow = meta.pending_after_workflow or ""
    if parent_workflow:
        meta.repo.then_run_after_workflow[parent_workflow] = target
    else:
        meta.repo.then_run_after_push = target
    meta.pending_target = target
    state.tasks.set_label(placeholder, f"  ↪ then run: {target}")
    state.tasks.update(
        placeholder, "pending",
        f"waiting on {parent_workflow}" if parent_workflow else "")


def _clear_then_run(state: State) -> None:
    """Drop the pending placeholder's chained dispatch — clears the
    repo's then_run_after slot and marks the placeholder as warn so
    the user can see the chain was cancelled."""
    placeholder = _pending_child_ref(state)
    if placeholder is None:
        return
    meta = state.tasks.get_meta(placeholder)
    if meta is None or meta.repo is None:
        return
    parent_workflow = meta.pending_after_workflow or ""
    if parent_workflow:
        meta.repo.then_run_after_workflow.pop(parent_workflow, None)
    else:
        meta.repo.then_run_after_push = ""
    meta.pending_target = None
    state.tasks.update(placeholder, "warn", "cleared by user")


def _open_in_browser(state: State, url: str) -> None:
    """Spawn `open` (macOS) or `xdg-open` (linux) for `url`. Failures
    surface as a warn task in the panel rather than crashing or
    silently swallowing — the user expects feedback."""
    cmd = "open" if sys.platform == "darwin" else "xdg-open"

    def worker() -> None:
        t = state.tasks.add(f"open {url}"[:60])
        try:
            rc = subprocess.run(
                [cmd, url], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5).returncode
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            state.tasks.update(t, "warn", str(e))
            return
        if rc == 0:
            state.tasks.update(t, "ok", "opened")
        else:
            state.tasks.update(t, "warn", f"{cmd} returned {rc}")

    threading.Thread(target=worker, daemon=True).start()


def _is_safe_browser_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


__all__ = [
    "open_task_action_menu",
    "draw_task_action_menu",
    "handle_task_action_menu_key",
]
