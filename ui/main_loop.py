"""Keyboard routing for the main surface and the review (confirm) sub-loop."""
from __future__ import annotations

import curses
from typing import Optional

from core.runtime.jobs import JobStatus
from core.runtime.task_actions import task_can_remove
from core.state.app import State
from core.state.selectors import (
    RowDisplayState,
    child_row_state,
    commit_message_count,
    repo_row_state,
)
from features.action_menu.session import open_action_menu
from core.workers import (
    cancel_review_file_loads,
    kick_off_detached_review_preflight,
    kick_off_bulk_suggest,
    kick_off_review_files_load,
    kick_off_safe_merge_confirm,
    kick_off_safe_merge_finalize,
    kick_off_suggest_for,
    kick_off_workers,
    safe_merge_abort,
)
from .colors import PAIR_WARN
from .geometry import safe_addstr
from .main_screen import _focused_message_holder, draw_main
from .modals import (
    _any_tab_loading,
    draw_diff_viewer,
    handle_detached_recovery_prompt_key,
    handle_diff_viewer_key,
    open_commit_msg_editor,
    open_diff_viewer,
    open_task_action_menu,
    open_workspace_menu,
    open_app_menu,
)
from .review import (
    _collect_review_focusables,
    _focused_block_idx,
    build_review_blocks,
    cycle_then_run,
    draw_review,
    fire_toolbar_action,
    is_toolbar_toggle,
)
from .safe_merge import (
    all_decided,
    draw_safe_merge,
    focus_move,
    focus_next_undecided,
    has_manual,
    set_choice,
)
from .mouse import ALT_M, ALT_S, read_key
from .sidebar import SPINNER_FRAMES


def _reset_field_cursor(state: State) -> None:
    """Park the cursor at the end of the focused row's message — runs after
    every selection change so each field starts in a familiar place."""
    holder = _focused_message_holder(state)
    state.field_cursor = (
        len(_holder_message(state, holder)) if holder is not None else 0
    )


def _holder_message(state: State, holder) -> str:
    return "" if holder is None else state.store.row_message(holder)


def _set_holder_message(state: State, holder, message: str) -> None:
    state.store.set_row_message(holder, message)


def _focused_row_state(state: State) -> Optional[RowDisplayState]:
    """Return selector state for the currently focused editable row."""
    cur = state.current_repo
    if cur is not None:
        return repo_row_state(state, cur)
    cur_child = state.current_child
    if cur_child is not None:
        return child_row_state(state, cur_child[1])
    return None


def _focused_row_is_busy(state: State) -> bool:
    row_state = _focused_row_state(state)
    return bool(row_state is not None and row_state.busy)


def _clamp_task_selection(state: State) -> None:
    """Keep state.task_selected within the current task list and within
    the visible window. Called after navigation + after the task list
    mutates (additions, removals, prunes)."""
    n = len(state.tasks.snapshot())
    if n == 0:
        state.task_selected = 0
        state.task_scroll = 0
        return
    state.task_selected = max(0, min(state.task_selected, n - 1))


def handle_task_panel_key(state: State, key: int) -> Optional[str]:
    """Key handling while the task panel has focus. Returns the same
    action sentinels as handle_main_key so the main loop's outer dispatch
    keeps working without special cases."""
    items = state.tasks.snapshot()
    n = len(items)

    if key == curses.KEY_BTAB or key == 27:
        # Shift+Tab toggles back; Esc also returns focus to the repo list
        # rather than triggering a quit.
        state.focused_panel = "repos"
        return None

    if key == 18 or key == curses.KEY_F5:  # Ctrl+R / F5 — refresh
        return "refresh"
    if key == 19:
        return "sync"
    if key == 16:  # Ctrl+P — pull all
        return "pull-all"

    if n == 0:
        return None

    if key == curses.KEY_UP:
        state.task_selected = max(0, state.task_selected - 1)
        return None
    if key == curses.KEY_DOWN:
        state.task_selected = min(n - 1, state.task_selected + 1)
        return None
    if key == curses.KEY_PPAGE:
        state.task_selected = max(0, state.task_selected - 10)
        return None
    if key == curses.KEY_NPAGE:
        state.task_selected = min(n - 1, state.task_selected + 10)
        return None
    if key == curses.KEY_HOME:
        state.task_selected = 0
        return None
    if key == curses.KEY_END:
        state.task_selected = n - 1
        return None

    if key == 9:  # Tab — open the task-detail modal on the focused row
        if 0 <= state.task_selected < n:
            open_task_action_menu(state, items[state.task_selected])
        return None

    if key in (10, 13, curses.KEY_ENTER):
        # Enter on a finished task removes it. `running` AND `pending`
        # rows are both kept so the user can't accidentally drop
        # something mid-flight — `pending` is the chained-then-run
        # placeholder waiting on a parent run to land, and dropping it
        # would silently cancel the queued follow-up.
        if 0 <= state.task_selected < n:
            t = items[state.task_selected]
            if task_can_remove(state, t):
                state.tasks.remove(t)
                _clamp_task_selection(state)
        return None
    return None


def _cycle_workspace(state: State, direction: int) -> Optional[str]:
    """Cycle the active workspace by `direction` (+1 / -1) and trigger
    the synchronous discover + apply-overrides + async-refresh switch.
    Returns "switch-workspace" so the main loop can re-derive the OSC
    terminal title; returns None when there are fewer than two
    workspaces (cycling would be a no-op)."""
    if len(state.workspaces) < 2:
        return None
    n = len(state.workspaces)
    new_idx = (state.active_workspace_index + direction) % n
    # Imported lazily — workers depends on git_ops which is fine at
    # module load, but keeping the import local mirrors how other key
    # handlers in this file pull worker entry points on demand.
    from core.workers import switch_workspace
    switch_workspace(state, new_idx)
    return "switch-workspace"


def handle_main_key(state: State, key: int) -> Optional[str]:
    if key == curses.KEY_RESIZE:
        return None

    # Shift+Tab toggles between repo list and task panel. We handle it
    # before the focus dispatch below so it works from either side.
    if key == curses.KEY_BTAB:
        state.focused_panel = (
            "tasks" if state.focused_panel == "repos" else "repos")
        if state.focused_panel == "tasks":
            _clamp_task_selection(state)
        return None

    if state.focused_panel == "tasks":
        return handle_task_panel_key(state, key)

    if key == 18 or key == curses.KEY_F5:  # Ctrl+R / F5 — refresh state
        return "refresh"
    if key == 19:  # Ctrl+S — fetch + checkout every tracked sibling
        return "sync"
    if key == 16:  # Ctrl+P — ff-only pull every repo with an upstream
        return "pull-all"

    # Title row navigation (selected = -2). Enter (matches the
    # row's underline affordance) and Tab both open the app menu;
    # ←/→ are no-ops here since cycling belongs to the workspace
    # switcher row below.
    if state.on_title_row:
        if key == curses.KEY_UP:
            # Wrap to the bottom of the body — same feel as the
            # workspace row used to have before it had a row above it.
            state.selected = max(-2, state.total_rows - 1)
            _reset_field_cursor(state)
            return None
        if key == curses.KEY_DOWN:
            state.selected = -1
            _reset_field_cursor(state)
            return None
        if key == 9 or key in (10, 13, curses.KEY_ENTER):
            open_app_menu(state)
            return None
        if key == 27:
            return "confirm-quit" if state.has_messages else "quit"
        return None

    # Workspace switcher row navigation (selected = -1). ←/→ cycles
    # workspaces; Enter opens the workspace picker; Tab opens settings.
    if state.on_workspace_row:
        if key == curses.KEY_UP:
            state.selected = -2  # up to the title row
            _reset_field_cursor(state)
            return None
        if key == curses.KEY_DOWN:
            state.selected = 0
            _reset_field_cursor(state)
            return None
        if key == curses.KEY_LEFT:
            return _cycle_workspace(state, -1)
        if key == curses.KEY_RIGHT:
            return _cycle_workspace(state, +1)
        if key in (10, 13, curses.KEY_ENTER):
            from features.workspace_switcher.session import open_workspace_switcher
            open_workspace_switcher(state)
            return None
        if key == 9:  # Tab — opens the workspace settings modal
            open_workspace_menu(state)
            return None
        if key == 27:
            return "confirm-quit" if state.has_messages else "quit"
        return None

    if key == curses.KEY_UP:
        if state.selected == 0:
            # Up from the first body row lands on the workspace row.
            state.selected = -1
        else:
            state.selected = (state.selected - 1) % state.total_rows
        _reset_field_cursor(state)
        return None
    if key == curses.KEY_DOWN:
        state.selected = (state.selected + 1) % state.total_rows
        _reset_field_cursor(state)
        return None

    if key in (10, 13, curses.KEY_ENTER):
        if state.has_messages:
            return "confirm"
        return None

    if key == 9:  # Tab — open per-row action menu
        if _focused_row_is_busy(state):
            return None  # action in flight — ignore until lock releases
        open_action_menu(state)
        return None

    if key == curses.KEY_SRIGHT or key == ALT_M:  # Shift+Right / Alt+M — large commit-msg editor
        # Silently ignored when the focused row isn't an editable
        # commit-message holder; same gating logic as the inline field
        # below — opens only on dirty repos / dirty submodule rows.
        open_commit_msg_editor(state)
        return None

    target_message_holder = _focused_message_holder(state)

    if key == 27:
        if target_message_holder is not None and _focused_row_is_busy(state):
            return "confirm-quit" if state.has_messages else "quit"
        if target_message_holder is not None and _holder_message(
                state, target_message_holder):
            _set_holder_message(state, target_message_holder, "")
            state.field_cursor = 0
            return None
        return "confirm-quit" if state.has_messages else "quit"

    if target_message_holder is None:
        return None  # subtree row or otherwise non-editable

    if _focused_row_is_busy(state):
        return None  # message retained but not editable until refresh finishes

    msg = _holder_message(state, target_message_holder)
    cur = max(0, min(state.field_cursor, len(msg)))

    if key == curses.KEY_LEFT:
        if not msg:
            kick_off_suggest_for(state, target_message_holder)
            return None
        state.field_cursor = max(0, cur - 1)
        return None
    if (key == curses.KEY_SLEFT or key == ALT_S) and not msg:
        kick_off_bulk_suggest(state)
        return None

    if key == curses.KEY_RIGHT:
        state.field_cursor = min(len(msg), cur + 1)
        return None
    if key == curses.KEY_HOME or key == 1:  # Home or Ctrl+A
        state.field_cursor = 0
        return None
    if key == curses.KEY_END or key == 5:  # End or Ctrl+E
        state.field_cursor = len(msg)
        return None

    if key in (curses.KEY_BACKSPACE, 127, 8):
        if cur > 0:
            _set_holder_message(
                state,
                target_message_holder,
                msg[: cur - 1] + msg[cur:],
            )
            state.field_cursor = cur - 1
        return None
    if key == curses.KEY_DC:  # forward delete
        if cur < len(msg):
            _set_holder_message(
                state,
                target_message_holder,
                msg[:cur] + msg[cur + 1:],
            )
        return None
    if 32 <= key < 127:
        _set_holder_message(
            state,
            target_message_holder,
            msg[:cur] + chr(key) + msg[cur:],
        )
        state.field_cursor = cur + 1
        return None
    return None


# ---------- Confirm sub-loop + quit confirmation --------------------------


def ensure_cursor_visible(line_index: int, scroll: int, body_h: int) -> int:
    """Return a new scroll value that keeps line_index on-screen."""
    if line_index < scroll:
        return line_index
    if line_index >= scroll + body_h:
        return max(0, line_index - body_h + 1)
    return scroll


def confirm_quit(stdscr, state: State) -> bool:
    """Show a 'Quit and discard N message(s)? [y/N]' prompt at the bottom of
    the main screen. Returns True if the user confirms, False to cancel."""
    stdscr.timeout(100)
    draw_main(stdscr, state)
    h, _ = stdscr.getmaxyx()
    n = commit_message_count(state)
    plural = "" if n == 1 else "s"
    prompt = f"Quit and discard {n} commit message{plural}? [y/N]"
    try:
        stdscr.move(h - 1, 0)
        stdscr.clrtoeol()
    except curses.error:
        pass
    safe_addstr(stdscr, h - 1, 2, prompt,
                curses.color_pair(PAIR_WARN) | curses.A_BOLD)
    curses.curs_set(0)
    stdscr.refresh()
    while True:
        try:
            key = read_key(stdscr)
        except KeyboardInterrupt:
            return True
        if key == -1:
            continue
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), 27, 10, 13, curses.KEY_ENTER):
            return False


def _detached_review_preflight(stdscr, state: State) -> bool:
    """Run detached-HEAD review recovery as a worker-owned preflight.

    The UI loop only draws and routes the recovery prompt while the worker
    performs git probing, safe recovery, and refresh. This keeps review entry
    from blocking the input thread before a task/job exists."""
    job = kick_off_detached_review_preflight(state)
    if job is None:
        return True
    if job.terminal:
        return job.status in (JobStatus.OK, JobStatus.WARN)
    stdscr.timeout(100)
    while not job.terminal:
        draw_main(stdscr, state)
        stdscr.refresh()
        try:
            key = read_key(stdscr)
        except KeyboardInterrupt:
            prompt = state.detached_recovery_prompt
            if prompt is not None:
                prompt.chosen_action = "cancel"
                prompt.result_event.set()
                state.detached_recovery_prompt = None
            state.job_registry.request_cancel(job)
            return False
        if key == -1:
            continue
        if key == curses.KEY_RESIZE:
            continue
        if state.detached_recovery_prompt is not None:
            handle_detached_recovery_prompt_key(state, key)
    return job.status in (JobStatus.OK, JobStatus.WARN)


def handle_confirm(stdscr, state: State) -> None:
    """Two-panel review screen.

    Left pane = per-target blocks (header + message + push summary +
    LFS warnings + workflow toggles + then-runs). ↑/↓ navigates
    across blocks; Space toggles LFS / workflow rows; ←/→ cycles a
    then-run target.

    Right pane = working-tree files for the block of the currently-
    focused row (loaded asynchronously, with a spinner placeholder
    until `query_working_tree` lands). Shift+Tab toggles focus
    between the panes; in the right pane ↑/↓ navigates the file
    list and Enter opens the diff modal — a sub-modal of this inner
    loop, drawn on top of the review screen with its keys handled
    here so Enter / Esc close it without leaving review.

    Enter from the left pane runs the commits — the async pipeline
    takes over the sidebar from there. Esc backs out without
    committing."""
    if not _detached_review_preflight(stdscr, state):
        return
    blocks = build_review_blocks(state)
    if not blocks:
        return
    kick_off_review_files_load(state, blocks)

    focusables = _collect_review_focusables(state, blocks)
    # Land on the first message (suggest) row — the top of each block and
    # the natural starting point; ↓ steps down through LFS, the push
    # toggle, and the on-push actions in pipeline order. Falls back to
    # the first focusable when there's no suggest row (all mid-merge).
    focus = next((i for i, f in enumerate(focusables) if f[1] == "suggest"),
                 0 if focusables else -1)
    panel_focus = "left"
    scroll = 0

    try:
        while True:
            anim = any(
                state.review_drafts.get_or_create(b.draft_id).files_loading
                or state.review_drafts.get_or_create(
                    b.draft_id).suggesting for b in blocks) or (
                state.diff_viewer is not None
                and _any_tab_loading(state, state.diff_viewer))
            stdscr.timeout(100 if anim else 1000)
            scroll = draw_review(stdscr, state, blocks, focusables,
                                 focus, panel_focus, scroll)
            if state.diff_viewer is not None:
                draw_diff_viewer(stdscr, state)
                stdscr.refresh()
            try:
                key = read_key(stdscr)
            except KeyboardInterrupt:
                return
            if key == -1:
                if anim:
                    state.spinner_frame = (
                        state.spinner_frame + 1) % len(SPINNER_FRAMES)
                continue
            if key == curses.KEY_RESIZE:
                continue

            # Diff modal owns key handling while it's open. Enter / Esc
            # both close it (per the user-specified gesture); arrow /
            # page keys scroll the diff body.
            if state.diff_viewer is not None:
                handle_diff_viewer_key(state, key)
                continue

            if key == 27:  # Esc
                return
            if key == 11:  # Ctrl+K — clear all then-run chains
                # Manual reset escape hatch for review-owned chains.
                # Doesn't touch commit messages or staged-paths.
                for b in blocks:
                    state.review_drafts.clear_workflow_intent(b.draft_id)
                continue
            if key in (10, 13, curses.KEY_ENTER):
                if panel_focus == "left":
                    # Don't fire commits until every block has finished
                    # loading its files — without files we'd race the
                    # staging step and bail with "nothing staged". The
                    # left-pane Enter hint is hidden in the same
                    # condition so this is just defence in depth.
                    if any(
                            state.review_drafts.get_or_create(
                                b.draft_id).files_loading
                            for b in blocks):
                        continue
                    kick_off_workers(state, blocks)
                    return  # async pipeline takes over the sidebar
                # Right pane: Enter fires the focused toolbar button
                # when the toolbar has focus; otherwise it's a no-op
                # (file rows don't have an Enter action).
                bi = _focused_block_idx(focusables, focus)
                if 0 <= bi < len(blocks):
                    block = blocks[bi]
                    if block.toolbar_focus >= 0:
                        fire_toolbar_action(state, block, block.toolbar_focus)
                continue
            if key == 9:  # Tab — only meaningful in the right (Changes) pane
                # No-op when the user is on the left review pane: there's
                # no file selection there, so opening the diff viewer
                # would either pick a stale right-pane row or land on an
                # empty list. The hint footer only advertises Tab on the
                # right pane, but a stray Tab from the left used to fire
                # this branch and pop a half-empty viewer.
                if panel_focus != "right":
                    continue
                bi = _focused_block_idx(focusables, focus)
                if 0 <= bi < len(blocks):
                    block = blocks[bi]
                    draft = state.review_drafts.get_or_create(block.draft_id)
                    if (not draft.files_loading and draft.files
                            and 0 <= block.file_selected < len(draft.files)):
                        fe = draft.files[block.file_selected]
                        open_diff_viewer(
                            state,
                            target_path=block.target_path,
                            label=block.label,
                            file_path=fe.path,
                            untracked=fe.untracked,
                        )
                continue
            if key == curses.KEY_BTAB:
                panel_focus = "right" if panel_focus == "left" else "left"
                continue

            if panel_focus == "left":
                # Param-input rows own typing / backspace before the
                # generic Space / arrow handlers, so the user can put
                # any allowed char into a parameter buffer without
                # those keys leaking out to navigation. Up and Down
                # still fall through (they're not printable), and the
                # row is auto-removed from the focusables list when
                # the parent then-run is cycled away from a
                # parameterised action — no separate exit gesture
                # required. The validator (allowed-chars +
                # leading-dash rule) comes from the row's ParamSpec.
                if (0 <= focus < len(focusables)
                        and focusables[focus][1] == "param_input"):
                    from .review import (
                        _find_param_spec,
                        _set_then_run_param_value,
                        _then_run_param_value,
                    )
                    sel, param_name = focusables[focus][2]
                    spec = _find_param_spec(state, sel, param_name)
                    if spec is None:
                        continue
                    if key in (curses.KEY_BACKSPACE, 127, 8):
                        cur = _then_run_param_value(state, sel, param_name)
                        _set_then_run_param_value(
                            state, sel, param_name, cur[:-1])
                        continue
                    if 32 <= key < 127:
                        ch = chr(key)
                        cur = _then_run_param_value(state, sel, param_name)
                        if (not cur and ch == "-"
                                and spec.refuse_leading_dash):
                            continue
                        if ch in spec.valid_chars:
                            _set_then_run_param_value(
                                state, sel, param_name, cur + ch)
                        continue
                    # Fall through for ↑/↓ (handled below) and any
                    # other gesture that should still navigate.
                if key == ord(" ") and 0 <= focus < len(focusables):
                    _, kind, obj = focusables[focus]
                    if kind == "lfs":
                        obj.track = not obj.track
                    elif kind == "toggle":
                        draft = state.review_drafts.get_or_create(
                            obj.draft_id)
                        on = draft.track_workflow.get(
                            obj.workflow_name, False)
                        state.review_drafts.set_track_workflow(
                            obj.draft_id, obj.workflow_name, not on)
                        # Tracking an action shows its then-run chain;
                        # untracking hides it — so rebuild the focusables
                        # list and clamp focus, same as the push toggle.
                        focusables = _collect_review_focusables(state, blocks)
                        if focus >= len(focusables):
                            focus = max(0, len(focusables) - 1)
                    elif kind == "push":
                        # Flip this block's per-commit push. Turning it
                        # off hides the push-only rows (workflow
                        # tracking, then-run-after-push), so rebuild the
                        # focusables list and clamp focus — same dance
                        # as cycling a then-run target below.
                        draft = state.review_drafts.get_or_create(obj.draft_id)
                        state.review_drafts.set_push(obj.draft_id, not draft.push)
                        focusables = _collect_review_focusables(state, blocks)
                        if focus >= len(focusables):
                            focus = max(0, len(focusables) - 1)
                    # Space on a suggest / then-run row is a no-op
                    # (use ← for suggest, ←/→ for then-run).
                    continue
                if (key in (curses.KEY_LEFT, curses.KEY_RIGHT)
                        and 0 <= focus < len(focusables)):
                    _, kind, obj = focusables[focus]
                    if kind == "then_run":
                        cycle_then_run(
                            state, obj,
                            -1 if key == curses.KEY_LEFT else 1)
                        # Cycling can add or remove the tag_input
                        # row that follows this selector — rebuild
                        # the focusables list so Down lands on the
                        # newly-injected row (or skips one that
                        # just disappeared). Clamp focus so an
                        # entry that was at the tail doesn't fall
                        # past the end after the rebuild.
                        focusables = _collect_review_focusables(state, blocks)
                        if focus >= len(focusables):
                            focus = max(0, len(focusables) - 1)
                    elif kind == "suggest" and key == curses.KEY_LEFT:
                        # Re-suggest the commit message scoped to the
                        # block's currently-checked files. Lazy import
                        # to keep workers off the main_loop import
                        # cycle.
                        from core.workers import kick_off_review_suggest
                        kick_off_review_suggest(state, obj)
                    continue
                if key == curses.KEY_UP and focus > 0:
                    focus -= 1
                elif (key == curses.KEY_DOWN
                        and focus < len(focusables) - 1):
                    focus += 1
            else:  # panel_focus == "right"
                bi = _focused_block_idx(focusables, focus)
                if 0 <= bi < len(blocks):
                    block = blocks[bi]
                    draft = state.review_drafts.get_or_create(block.draft_id)
                    n = len(draft.files)
                    if block.toolbar_focus >= 0:
                        # Toolbar focus mode — Up is a no-op (already
                        # at the top of the pane), Down drops back to
                        # the file list, Left/Right cycle between the
                        # buttons. Enter is fired by the top-level
                        # Enter handler (so it isn't swallowed by the
                        # earlier `continue`); Space here only fires
                        # toggle buttons (e.g. amend) so an accidental
                        # Space on `[ stage all ]` doesn't trigger a
                        # bulk-stage the user didn't intend.
                        if key == curses.KEY_DOWN:
                            block.toolbar_focus = -1
                            if n > 0 and block.file_selected >= n:
                                block.file_selected = 0
                        elif key == curses.KEY_LEFT:
                            block.toolbar_focus = max(
                                0, block.toolbar_focus - 1)
                        elif key == curses.KEY_RIGHT:
                            block.toolbar_focus = min(
                                2, block.toolbar_focus + 1)
                        elif key == ord(" ") and is_toolbar_toggle(
                                block.toolbar_focus):
                            fire_toolbar_action(
                                state, block, block.toolbar_focus)
                    else:
                        if key == curses.KEY_UP and block.file_selected > 0:
                            block.file_selected -= 1
                        elif key == curses.KEY_UP:
                            # At file 0 — Up lifts focus to the
                            # toolbar. Default to "stage all" so the
                            # most-common follow-up gesture is one
                            # keystroke away.
                            block.toolbar_focus = 0
                        elif (key == curses.KEY_DOWN
                                and block.file_selected < n - 1):
                            block.file_selected += 1
                        elif key == ord(" ") and 0 <= block.file_selected < n:
                            # Toggle the staged-for-commit checkbox.
                            # Pipeline reads review_drafts at commit
                            # dispatch.
                            fe = draft.files[block.file_selected]
                            cur = draft.staged_paths.get(fe.path, False)
                            state.review_drafts.set_staged(
                                block.draft_id, fe.path, not cur)
    finally:
        cancel_review_file_loads(state, blocks)


def handle_safe_merge(stdscr, state: State) -> None:
    """Full-screen safe-merge sub-loop. Drives the conflict resolver
    (`ui/safe_merge.py`) through its phases:

      preparing → resolve → committing → confirm → confirming → done

    Worker phases animate a spinner and ignore input (a daemon worker owns
    the git work); the idle phases (resolve / confirm / error) take blocking
    input. Esc never runs `git merge --abort` — it leaves the merge in
    place (Cardinal Rule), preserving the backup stash and any conflicts so
    the user can finish by hand or re-open."""
    screen = state.safe_merge
    if screen is None:
        return
    worker_phases = ("preparing", "committing", "confirming")
    while True:
        if screen.phase == "done":
            return
        anim = screen.phase in worker_phases
        stdscr.timeout(100 if anim else -1)
        draw_safe_merge(stdscr, state)
        try:
            key = read_key(stdscr)
        except KeyboardInterrupt:
            safe_merge_abort(state, screen)
            return
        if key == -1:
            if anim:
                state.spinner_frame = (
                    state.spinner_frame + 1) % len(SPINNER_FRAMES)
            continue
        if key == curses.KEY_RESIZE:
            continue

        phase = screen.phase
        if phase in worker_phases:
            continue  # worker owns the git work — swallow input

        if phase == "error":
            if key in (27, 10, 13, curses.KEY_ENTER):
                safe_merge_abort(state, screen)
                return
            continue

        if phase == "resolve":
            if key == 27:  # Esc — leave the merge in progress
                safe_merge_abort(state, screen)
                return
            if key == curses.KEY_UP:
                focus_move(screen, -1)
            elif key == curses.KEY_DOWN:
                focus_move(screen, 1)
            elif key == curses.KEY_PPAGE:
                focus_move(screen, -5)
            elif key == curses.KEY_NPAGE:
                focus_move(screen, 5)
            elif key == curses.KEY_LEFT:
                set_choice(screen, "ours")
            elif key == curses.KEY_RIGHT:
                set_choice(screen, "theirs")
            elif key in (ord("b"), ord("B")):
                set_choice(screen, "both")
            elif key == ord(" "):
                # Cycle the focused pick ours → theirs → ours.
                if screen.decisions:
                    fi, hi = screen.decisions[screen.focus]
                    cur = (screen.files[fi].whole_choice if hi < 0
                           else screen.files[fi].hunks[hi].choice)
                    set_choice(screen,
                               "theirs" if cur == "ours" else "ours")
            elif key in (10, 13, curses.KEY_ENTER):
                if not screen.decisions:
                    screen.status_note = (
                        "nothing to resolve here — Esc to leave the merge")
                elif not all_decided(screen):
                    focus_next_undecided(screen)
                    screen.status_note = "every conflict needs a choice first"
                elif has_manual(screen):
                    screen.status_note = (
                        "manual conflict(s) remain — resolve them outside "
                        "idlegit, then re-open safe-merge")
                else:
                    kick_off_safe_merge_finalize(state, screen)
            continue

        if phase == "confirm":
            if key == 27:  # Esc — keep the merge commit, skip push/sync
                safe_merge_abort(state, screen)
                return
            if key == curses.KEY_UP:
                screen.confirm_focus = max(0, screen.confirm_focus - 1)
            elif key == curses.KEY_DOWN:
                screen.confirm_focus = min(2, screen.confirm_focus + 1)
            elif key == ord(" "):
                _safe_merge_toggle_confirm(screen)
            elif key in (10, 13, curses.KEY_ENTER):
                if screen.confirm_focus == 2:
                    kick_off_safe_merge_confirm(state, screen)
                else:
                    _safe_merge_toggle_confirm(screen)
            continue


def _safe_merge_toggle_confirm(screen) -> None:
    """Flip the checkbox the confirm screen's cursor is on."""
    if screen.confirm_focus == 0:
        screen.confirm_push = not screen.confirm_push
    elif screen.confirm_focus == 1 and screen.backup_stash_name:
        screen.confirm_remove_stash = not screen.confirm_remove_stash
