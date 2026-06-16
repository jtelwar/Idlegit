"""Keyboard routing for the main surface and the review (confirm) sub-loop."""
from __future__ import annotations

import curses
from typing import Optional

from core.models import State
from core.workers import (
    _build_recovery_prompt,
    execute_detached_recovery,
    kick_off_bulk_suggest,
    kick_off_safe_merge_confirm,
    kick_off_safe_merge_finalize,
    kick_off_suggest_for,
    kick_off_workers,
    refresh_repo_with_remote_state,
    safe_merge_abort,
)
from .colors import PAIR_WARN
from .geometry import safe_addstr
from .main_screen import _focused_message_holder, draw_main
from .modals import (
    draw_diff_viewer,
    handle_detached_recovery_prompt_key,
    handle_diff_viewer_key,
    open_action_menu,
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
    kick_off_review_files_load,
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
    state.field_cursor = len(holder.message) if holder is not None else 0


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

    if key == 18:  # Ctrl+R — refresh
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
            if t.status not in ("running", "pending"):
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

    if key == 18:  # Ctrl+R — refresh state, prune tasks
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
            from .modals.workspace_switcher import open_workspace_switcher
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
        cur = state.current_repo
        cur_child = state.current_child
        if (cur is not None and cur.refreshing) or \
                (cur_child is not None and cur_child[1].refreshing):
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
        if target_message_holder is not None and target_message_holder.refreshing:
            return "confirm-quit" if state.has_messages else "quit"
        if target_message_holder is not None and target_message_holder.message:
            target_message_holder.message = ""
            state.field_cursor = 0
            return None
        return "confirm-quit" if state.has_messages else "quit"

    if target_message_holder is None:
        return None  # subtree row or otherwise non-editable

    if target_message_holder.refreshing:
        return None  # message retained but not editable until refresh finishes

    msg = target_message_holder.message
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
            target_message_holder.message = msg[: cur - 1] + msg[cur:]
            state.field_cursor = cur - 1
        return None
    if key == curses.KEY_DC:  # forward delete
        if cur < len(msg):
            target_message_holder.message = msg[:cur] + msg[cur + 1:]
        return None
    if 32 <= key < 127:
        target_message_holder.message = msg[:cur] + chr(key) + msg[cur:]
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
    draw_main(stdscr, state)
    h, _ = stdscr.getmaxyx()
    n = sum(1 for r in state.repos if r.message.strip())
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
    """Pop the recovery modal for every detached-HEAD repo / submodule
    child that has a queued commit message, BEFORE the review screen
    draws. Returns True when the review can proceed (every detached
    target either got fast-forwarded or wasn't on the commit list);
    False if the user cancelled any prompt — in which case the review
    is aborted and the cursor goes back to the main panel.

    Without this preflight, a detached canonical row got past review
    all the way to `commit_worker` before the recovery modal popped,
    which felt like idlegit was "about to push on origin/(detached)"
    even though the commit_worker guard would still have caught it.
    Surfacing the modal at review time matches the user's mental
    model of "I just told it to commit; ask now"."""
    while True:
        target = _next_detached_review_target(state)
        if target is None:
            return True
        path, label = target
        prompt = _build_recovery_prompt(path, label)
        if prompt is None:
            # No recovery branch available — surface a one-shot warn
            # task and abort the review so commit_worker doesn't try
            # to push a (detached) refspec.
            t = state.tasks.add(f"{label}: cannot commit")
            state.tasks.update(
                t, "fail",
                "detached HEAD with no recoverable target branch")
            return False
        state.detached_recovery_prompt = prompt
        if not _drive_modal_until_closed(stdscr, state,
                                         "detached_recovery_prompt"):
            return False
        if prompt.chosen_action != "ff":
            return False
        ok, msg = execute_detached_recovery(path, prompt.target_branch)
        if not ok:
            t = state.tasks.add(f"{label}: cannot commit")
            state.tasks.update(t, "fail", msg or "recovery failed")
            return False
        # Refresh the in-memory Repo so build_review_blocks sees the
        # real branch name instead of the stale "(detached)" sentinel.
        for repo in state.repos:
            if repo.path == path:
                refresh_repo_with_remote_state(repo)
                break
            for ref in repo.children:
                if ref.kind == "submodule" and ref.nested_path == path:
                    refresh_repo_with_remote_state(ref.repo)
                    break
        # Loop back — the next iteration finds the next detached
        # target (if any) and runs the same flow.


def _next_detached_review_target(state: State):
    """Return `(path, label)` for the next detached commit target with
    a queued message, or None when none remain. Walks top-level repos
    first, then submodule children — so the modal sequence is stable
    and predictable."""
    from core.git_ops import git
    for repo in state.repos:
        if not repo.message.strip():
            continue
        rc, out, _ = git(repo.path, ["branch", "--show-current"])
        if rc == 0 and not out.strip():
            return repo.path, repo.display_name
    for parent in state.repos:
        for child in parent.children:
            if child.kind != "submodule" or not child.message.strip():
                continue
            rc, out, _ = git(child.nested_path, ["branch", "--show-current"])
            if rc == 0 and not out.strip():
                label = (f"↳ {child.repo.display_name} "
                         f"in {parent.display_name}")
                return child.nested_path, label
    return None


def _drive_modal_until_closed(stdscr, state: State, slot: str) -> bool:
    """Inner event loop that draws the main UI plus whichever modal
    `state.<slot>` is set to, dispatching keys to the matching
    handler until the modal clears its slot. Used by the review-
    screen preflight to surface a `DetachedRecoveryPrompt` from the
    main thread (workers use `result_event` instead).

    Returns True when the modal closed normally; False on a Ctrl+C
    interrupt (caller treats this as a cancel)."""
    handler = {
        "detached_recovery_prompt": handle_detached_recovery_prompt_key,
    }[slot]
    while getattr(state, slot) is not None:
        draw_main(stdscr, state)
        stdscr.refresh()
        try:
            key = read_key(stdscr)
        except KeyboardInterrupt:
            setattr(state, slot, None)
            return False
        if key == -1:
            continue
        if key == curses.KEY_RESIZE:
            continue
        handler(state, key)
    return True


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
    kick_off_review_files_load(blocks)

    focusables = _collect_review_focusables(blocks)
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
            anim = any(b.files_loading or b.suggesting for b in blocks) or (
                state.diff_viewer is not None
                and (state.diff_viewer.loading
                     or state.diff_viewer.log_loading
                     or state.diff_viewer.blame_loading))
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
                # Manual reset escape hatch for the case where the
                # spec's "queue → forget" auto-clear hasn't fired
                # yet (after-workflow chains stay until `_poll_run`
                # pops them; the user may want to reset before that
                # lands). Wipes every Repo's then-run state plus
                # `track_workflow` so the next review starts clean.
                # Doesn't touch commit messages or staged-paths.
                for r in state.repos:
                    r.track_workflow.clear()
                    r.then_run_after_push = ""
                    r.then_run_params_after_push.clear()
                    r.then_run_after_workflow.clear()
                    r.then_run_params_after_workflow.clear()
                continue
            if key in (10, 13, curses.KEY_ENTER):
                if panel_focus == "left":
                    # Don't fire commits until every block has finished
                    # loading its files — without files we'd race the
                    # staging step and bail with "nothing staged". The
                    # left-pane Enter hint is hidden in the same
                    # condition so this is just defence in depth.
                    if any(b.files_loading for b in blocks):
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
                        fire_toolbar_action(block, block.toolbar_focus)
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
                    if (not block.files_loading and block.files
                            and 0 <= block.file_selected < len(block.files)):
                        fe = block.files[block.file_selected]
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
                    spec = _find_param_spec(sel, param_name)
                    if spec is None:
                        continue
                    if key in (curses.KEY_BACKSPACE, 127, 8):
                        cur = _then_run_param_value(sel, param_name)
                        _set_then_run_param_value(
                            sel, param_name, cur[:-1])
                        continue
                    if 32 <= key < 127:
                        ch = chr(key)
                        cur = _then_run_param_value(sel, param_name)
                        if (not cur and ch == "-"
                                and spec.refuse_leading_dash):
                            continue
                        if ch in spec.valid_chars:
                            _set_then_run_param_value(
                                sel, param_name, cur + ch)
                        continue
                    # Fall through for ↑/↓ (handled below) and any
                    # other gesture that should still navigate.
                if key == ord(" ") and 0 <= focus < len(focusables):
                    _, kind, obj = focusables[focus]
                    if kind == "lfs":
                        obj.track = not obj.track
                    elif kind == "toggle":
                        on = obj.repo.track_workflow.get(
                            obj.workflow_name, False)
                        obj.repo.track_workflow[obj.workflow_name] = not on
                        # Tracking an action shows its then-run chain;
                        # untracking hides it — so rebuild the focusables
                        # list and clamp focus, same as the push toggle.
                        focusables = _collect_review_focusables(blocks)
                        if focus >= len(focusables):
                            focus = max(0, len(focusables) - 1)
                    elif kind == "push":
                        # Flip this block's per-commit push. Turning it
                        # off hides the push-only rows (workflow
                        # tracking, then-run-after-push), so rebuild the
                        # focusables list and clamp focus — same dance
                        # as cycling a then-run target below.
                        obj.push = not obj.push
                        focusables = _collect_review_focusables(blocks)
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
                            obj, -1 if key == curses.KEY_LEFT else 1)
                        # Cycling can add or remove the tag_input
                        # row that follows this selector — rebuild
                        # the focusables list so Down lands on the
                        # newly-injected row (or skips one that
                        # just disappeared). Clamp focus so an
                        # entry that was at the tail doesn't fall
                        # past the end after the rebuild.
                        focusables = _collect_review_focusables(blocks)
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
                    n = len(block.files)
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
                                block, block.toolbar_focus)
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
                            # Pipeline reads block.staged_paths at
                            # commit dispatch.
                            fe = block.files[block.file_selected]
                            cur = block.staged_paths.get(fe.path, False)
                            block.staged_paths[fe.path] = not cur
    finally:
        for b in blocks:
            b.cancel_event.set()


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
