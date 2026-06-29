#!/usr/bin/env python3
"""
Idlegit — interactive git multi-repo manager.

Scans a workspace for git repos (the workspace itself if it is one, plus
immediate child folders that contain .git) and lets you commit/push them
from a single screen.

This file is just the entry point — the real code lives in:
    config.py    Config + load_config + DEFAULT_* constants
    models.py    dataclasses (Repo, ChildRef, State, Task, Tasks, ActionMenu, …)
    git_ops.py   git subprocess wrappers, discovery, sync, suggest, LFS
    workers.py   background pipelines (kick_off_*) for commits, sync, refresh
    ui/          curses UI (package: main_screen, review, main_loop, modals, …)

See README.md for config locations and the keymap.
"""

from __future__ import annotations

import contextlib
import curses
import os
import sys
import time

from core.config import (
    APP_DISPLAY_NAME,
    apply_workspace_overrides,
    load_config,
    load_workspaces,
    get_load_warnings,
)
from core.git_ops import discover_repos
from core.state.app import State
from ui import (
    SPINNER_FRAMES,
    confirm_quit,
    draw_main,
    handle_action_menu_key,
    handle_align_heads_prompt_key,
    handle_branch_name_prompt_key,
    handle_branch_picker_key,
    handle_remote_branch_picker_key,
    handle_clone_modal_key,
    handle_commit_msg_editor_key,
    handle_commit_view_modal_key,
    handle_confirm,
    handle_safe_merge,
    handle_detached_recovery_prompt_key,
    handle_help_screen_key,
    handle_ssh_keygen_modal_key,
    handle_remotes_modal_key,
    handle_main_key,
    handle_reset_prompt_key,
    handle_task_action_menu_key,
    handle_task_log_viewer_key,
    handle_workflow_picker_key,
    handle_workspace_creator_key,
    handle_workspace_menu_key,
    handle_app_menu_key,
    handle_workspace_switcher_key,
    init_colors,
    refresh_all_workspaces,
    safe_addstr,
    tick_app_menu_update_check,
    tick_creator_checks,
    tick_menu_path_checks,
)
from core.workers import (
    kick_off_inline_refresh,
    kick_off_pull_all,
    kick_off_sync_siblings,
    kick_off_workspace_settings_save,
    switch_workspace,
)
from core.state.selectors import view_load_activity_active


@contextlib.contextmanager
def _silenced_stderr_fd():
    """Redirect file descriptor 2 (stderr) to /dev/null for the
    duration of the with-block, restoring the original fd on exit.

    Why this is fd-level rather than `sys.stderr =` Python-level:
    `logging.StreamHandler` (and the C-level OpenSSL stack invoked
    by urllib's TLS) cache the original stderr fd at handler /
    library setup time, so swapping `sys.stderr` afterward doesn't
    redirect their output. Curses owns the screen during the
    session and any stderr write would corrupt the layout
    (pyenv's broken-OpenSSL `code for hash blake2b was not found`
    spam from `hashlib`'s logging is the canonical example), so
    silencing fd 2 wholesale is the safe default. Real fatal
    errors that need user attention are surfaced via curses
    modals, not stderr."""
    if not hasattr(os, "dup") or not hasattr(os, "dup2"):
        yield
        return
    devnull = None
    saved = None
    try:
        saved = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        yield
    finally:
        if saved is not None:
            try:
                os.dup2(saved, 2)
            except OSError:
                pass
            try:
                os.close(saved)
            except OSError:
                pass
        if devnull is not None:
            try:
                os.close(devnull)
            except OSError:
                pass


def _disable_flow_control() -> None:
    """Stop the tty from eating Ctrl+S (XOFF) and Ctrl+Q (XON). Curses
    enables cbreak by default, which leaves the kernel-level
    software flow control on — Ctrl+S then pauses output and never
    reaches our getch loop. Clearing IXON/IXOFF on every plausible
    controlling-tty fd makes both keys deliverable like any other
    control char. No-op if no real tty is available (CI, pipes, ...)
    or if termios is unavailable
    (Windows: XON/XOFF is a POSIX terminal-driver concept; there's
    nothing to clear)."""
    if sys.platform == "win32":
        return
    import termios

    candidates = []
    seen = set()

    def add_fd(fd: int, close_after: bool = False) -> None:
        if fd < 0 or fd in seen:
            if close_after:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return
        seen.add(fd)
        candidates.append((fd, close_after))

    add_fd(0)
    try:
        add_fd(sys.stdin.fileno())
    except (AttributeError, OSError):
        pass
    try:
        flags = os.O_RDWR
        if hasattr(os, "O_NOCTTY"):
            flags |= os.O_NOCTTY
        add_fd(os.open("/dev/tty", flags), close_after=True)
    except (termios.error, OSError):
        pass

    for fd, close_after in candidates:
        try:
            if os.isatty(fd):
                attrs = termios.tcgetattr(fd)
                attrs[0] &= ~(termios.IXON | termios.IXOFF)
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except (termios.error, OSError):
            pass
        finally:
            if close_after:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _discover_workspace_repos(folders) -> list:
    """Walk every folder configured for a workspace, collect repos, dedupe
    by absolute path, and return them sorted by display name. Shared by
    startup and the runtime workspace-switch helper so both paths see the
    same dedupe order regardless of which folder a repo came from."""
    repos = []
    seen: set = set()
    for folder in folders:
        for r in discover_repos(folder):
            if r.path in seen:
                continue
            seen.add(r.path)
            repos.append(r)
    repos.sort(key=lambda r: r.display_name.lower())
    return repos


def _run_workspace_creator_subloop(stdscr, cfg, startup_warnings=None):
    """Run the workspace creator as a self-contained sub-loop. Used at
    first launch when idlegit.workspaces is empty/missing — drives a
    blank welcome backdrop with the creator modal overlaid until the
    user either commits at least one workspace (returns the list) or
    cancels with Esc (returns [])."""
    from core.state.app import State
    from ui import (
        PAIR_HEADER,
        draw_workspace_creator,
        open_workspace_creator,
        sidebar_geometry,
    )

    state = State(
        repos=[],
        workspace_name="",
        workspaces=[],
        base_config=cfg,
    )
    intro = "No workspaces are configured yet. Add one or more folder paths to scan for git repos."
    if startup_warnings:
        intro = f"{startup_warnings[0]}\n\nAdd one or more folder paths to scan for git repos."
    open_workspace_creator(state, title="Welcome to Idlegit", intro=intro)
    from ui.mouse import enable_mouse, read_key

    enable_mouse()
    stdscr.timeout(100)
    while state.workspace_creator is not None:
        if state.workspace_creator.result is not None:
            break
        # Spawn / track live discover_repos checks for any draft whose
        # path text drifted from what's been validated.
        tick_creator_checks(state)
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        title = "Idlegit"
        safe_addstr(
            stdscr,
            1,
            max(2, (w - len(title)) // 2),
            title,
            curses.A_BOLD | curses.color_pair(PAIR_HEADER),
        )
        sidebar_x, _ = sidebar_geometry(w)
        draw_workspace_creator(stdscr, state, sidebar_x)
        stdscr.refresh()
        try:
            key = read_key(stdscr)
        except KeyboardInterrupt:
            return []
        if key == -1:
            continue
        if key == curses.KEY_RESIZE:
            try:
                curses.update_lines_cols()
            except (AttributeError, curses.error):
                pass
            try:
                stdscr.clear()
            except curses.error:
                pass
            continue
        handle_workspace_creator_key(state, key)
    creator = state.workspace_creator
    result = list(creator.result) if creator and creator.result else []
    state.workspace_creator = None
    return result


def run(stdscr, cfg, workspaces, initial_active_idx: int = 0, startup_warnings=None) -> None:
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        pass
    curses.curs_set(0)
    init_colors()
    _disable_flow_control()
    stdscr.keypad(True)
    from ui.mouse import enable_mouse

    enable_mouse()
    stdscr.timeout(100)  # non-blocking getch — drives sidebar animation.

    first_run_created_workspaces = False
    # First-run path: idlegit.workspaces missing or empty drops us into
    # the creator wizard before the main UI gets to draw. The wizard
    # blocks until the user commits at least one workspace or cancels
    # with Esc (we exit cleanly). Persistence is scheduled once the
    # main State exists so the same worker-owned writer handles first
    # run and runtime creation.
    if not workspaces:
        workspaces = _run_workspace_creator_subloop(stdscr, cfg, startup_warnings=startup_warnings)
        if not workspaces:
            return
        first_run_created_workspaces = True
        active_idx = 0
    else:
        # Honour the persisted last-active index from idlegit.workspaces
        # so the user lands back where they left off. main() already
        # validated this against the post-filter workspace list.
        active_idx = max(0, min(initial_active_idx, len(workspaces) - 1))
    active_ws = workspaces[active_idx]
    # Discover and refresh only the active workspace at startup, but pass
    # every workspace through to the loading screen so the user sees the
    # whole configured set. Inactive workspaces keep any cached rows they
    # already have and refresh on entry via switch_workspace.
    active_repos = _discover_workspace_repos(active_ws.folders)
    active_ws.cached_repos = active_repos
    workspace_repos = []
    for i, ws in enumerate(workspaces):
        repos_for_row = active_repos if i == active_idx else list(ws.cached_repos)
        workspace_repos.append((ws.display_name, repos_for_row, ws.subtrees))
    repos = active_repos
    # Esc during the initial refresh aborts the load and quits — the
    # only "quit" gesture available before the main loop dispatches keys.
    if not refresh_all_workspaces(
        stdscr, workspace_repos, cfg.name_display_max, cfg.name_truncation, active_index=active_idx
    ):
        return

    title = f"Idlegit · {active_ws.display_name}" if active_ws.display_name else "Idlegit"
    # Re-emit immediately after curses owns the terminal: VS Code's
    # integrated terminal (and a few others) overwrite the title with
    # the running-process name once the alt-screen comes up.
    _set_terminal_title(title)
    state = State(
        repos=repos,
        workspace_name=active_ws.display_name,
        workspaces=workspaces,
        active_workspace_index=active_idx,
        base_config=cfg,
    )
    state.replace_repos(active_repos, workspace=active_ws, notify=False)
    if first_run_created_workspaces:
        kick_off_workspace_settings_save(
            state,
            label="save new workspaces",
            success_message="workspaces saved",
        )
    # Task logging is global (not workspace-scoped) so it's wired from
    # the loaded Config directly, not from apply_workspace_overrides.
    # When enabled, every terminal task transition appends a line to
    # `task_log_path`; the cap rotates oldest-first when exceeded.
    from core.task_log import resolve_task_log_path, wire_task_log

    state.task_log_enabled = cfg.task_log_enabled
    state.task_log_path = resolve_task_log_path(cfg.task_log_path)
    state.task_log_max_lines = cfg.task_log_max_lines
    if state.task_log_enabled:
        wire_task_log(state)
    for warning in startup_warnings or []:
        t = state.tasks.add("startup warning")
        state.tasks.update(t, "warn", warning)
    # Layer base-config defaults + this workspace's overrides on top of
    # the freshly-built State so the same code path runs on startup and
    # later workspace switches.
    apply_workspace_overrides(state, cfg, active_ws)

    # Stand up filesystem watchers for the active workspace's repos so
    # external edits propagate without a Ctrl+R. The reconcile is a
    # no-op (and tears down any leftover observer) when the config flag
    # is off; the manager handles per-repo schedule + debounce.
    from core.fs_watcher import (
        reconcile_repo_watchers,
        stop_repo_watchers,
    )

    reconcile_repo_watchers(state)

    try:
        _run_main_loop(stdscr, state, title)
    finally:
        # Polite teardown — daemon Observer thread would exit with the
        # process anyway, but stopping it here lets the platform release
        # its inotify/FSEvents handles immediately.
        stop_repo_watchers()


def _periodic_refresh_interval(state) -> float:
    interval = getattr(state, "periodic_refresh_seconds", 0)
    return interval if interval >= 1 else 0.0


def _local_mutation_active(state) -> bool:
    from core.state.selectors import any_local_mutation_active
    return any_local_mutation_active(state)


def _row_activity_active(state) -> bool:
    from core.state.selectors import (
        active_workspace_child_rows,
        active_workspace_repo_rows,
        child_row_state,
        read_only_row_busy_active,
        repo_row_state,
    )
    if read_only_row_busy_active(state):
        return True
    for repo in active_workspace_repo_rows(state):
        if repo_row_state(state, repo).suggesting:
            return True
    for _parent, child in active_workspace_child_rows(state):
        if child_row_state(state, child).suggesting:
            return True
    return False


def _periodic_refresh_idle(state) -> bool:
    if state.in_review or state.in_safe_merge:
        return False
    if _local_mutation_active(state):
        return False
    from core.state.selectors import read_only_row_busy_active
    if read_only_row_busy_active(state):
        return False
    return True


def _main_loop_timeout_ms(
        state,
        *,
        anim_running: bool,
        mutation_jobs_just_drained: bool,
        periodic_refresh_fired: bool,
) -> int:
    ui_event_pending = state.ui_events.drain()
    if anim_running or mutation_jobs_just_drained or periodic_refresh_fired or ui_event_pending:
        return 100
    return 1000


def _run_main_loop(stdscr, state, title):
    from core.fs_watcher import drain_pending_refreshes
    from ui.mouse import read_key

    last_title_emitted = title
    last_title_at = time.monotonic()
    last_periodic_workspace_index = state.active_workspace_index
    last_periodic_interval = _periodic_refresh_interval(state)
    next_periodic_refresh_at = (
        last_title_at + last_periodic_interval if last_periodic_interval > 0 else None
    )
    # Track local mutation jobs across iterations so we can detect the
    # transition from "actions mutating repos" → "idle" and drain queued
    # fs-watch events at that boundary. Without this, a sync (which
    # writes to multiple working trees as it goes) would queue events
    # in `_pending` but never fire — the user would see the action
    # complete but the repo rows stay stale until they hit Ctrl+R.
    prev_mutation_jobs_running = False
    while True:
        now = time.monotonic()
        current_interval = _periodic_refresh_interval(state)
        if (
            current_interval != last_periodic_interval
            or state.active_workspace_index != last_periodic_workspace_index
        ):
            last_periodic_workspace_index = state.active_workspace_index
            last_periodic_interval = current_interval
            next_periodic_refresh_at = now + current_interval if current_interval > 0 else None
        # Prune any completed tasks that have aged past the auto-remove
        # window. Negative interval is a no-op (legacy never-prune mode).
        state.tasks.prune_aged(state.auto_remove_completed_after)
        # Re-assert the terminal title every ~2s, but only emit when the
        # candidate string actually changed. Some terminals (VS Code's
        # integrated, iTerm2 with shell integration) periodically auto-
        # detect the foreground process and overwrite our OSC; this
        # loop wins the fight without saturating stdout.
        if now - last_title_at > 2.0:
            if title != last_title_emitted:
                _set_terminal_title(title)
                last_title_emitted = title
            last_title_at = now

        # Safe-merge sub-loop: a worker (the branch-picker merge route or
        # the action menu's "Safe merge…" entry) sets state.safe_merge to
        # hand the keyboard to the full-screen conflict resolver. Hold
        # in_safe_merge for its duration so the fs-watcher queues events
        # instead of refreshing the repo the user is mid-resolving — the
        # same contract as in_review around the review screen.
        if state.safe_merge is not None and not state.in_safe_merge:
            state.in_safe_merge = True
            try:
                handle_safe_merge(stdscr, state)
            finally:
                state.in_safe_merge = False
                state.safe_merge = None
                stdscr.timeout(100)
                drain_pending_refreshes()
            continue

        draw_main(stdscr, state)
        # Tick the spinner whenever any background work is in flight so
        # animations (sidebar tasks, in-field suggest, in-row refresh) play.
        # Also keep ticking while a finished task is fading out so the
        # progress-bar countdown actually animates.
        # While the workspace creator is open, kick off / track the
        # background discover_repos checks for any draft whose path
        # text changed since its last check. The tick returns True
        # while any worker is in flight so the spinner keeps animating.
        creator_checking = (
            tick_creator_checks(state) if state.workspace_creator is not None else False
        )
        # Same idea for the workspace settings modal — its folder rows
        # carry per-path drafts that need re-checking when text edits
        # land. Bundling both into anim_running keeps the spinner
        # ticking through any in-flight discover_repos work.
        menu_checking = tick_menu_path_checks(state) if state.workspace_menu is not None else False
        # The global app menu's APPLICATION section runs an async
        # GitHub Releases query when the user fires "Check for
        # updates". The tick rebuilds rows on `checking → done /
        # failed` transitions and reports True while the worker is
        # still in flight so the spinner glyph keeps animating.
        app_menu_checking = (
            tick_app_menu_update_check(state) if state.app_menu is not None else False
        )
        # Runtime commit: if the creator finished while open as a
        # modal (e.g. via the "+ Create new workspace" picker entry),
        # consume its result here — append the new workspaces, persist,
        # close both modals, and switch to the first new workspace so
        # the user lands inside what they just created.
        if state.workspace_creator is not None and state.workspace_creator.result is not None:
            new_ws = state.workspace_creator.result
            state.workspace_creator = None
            if new_ws:
                first_new_index = len(state.workspaces)
                state.workspaces = list(state.workspaces) + list(new_ws)
                state.app_menu = None
                switch_workspace(state, first_new_index)
                ws = state.active_workspace
                if ws is not None:
                    title = f"Idlegit · {ws.name}" if ws.name else "Idlegit"
        local_mutation_active = _local_mutation_active(state)
        anim_running = (
            state.tasks.has_visible_activity()
            or state.tasks.has_pending_auto_remove(state.auto_remove_completed_after)
            or local_mutation_active
            or _row_activity_active(state)
            or creator_checking
            or menu_checking
            or app_menu_checking
            or view_load_activity_active(state)
        )
        if anim_running:
            state.spinner_frame = (state.spinner_frame + 1) % len(SPINNER_FRAMES)

        # Tasks-drained transition: when the last running task finishes
        # we fire one auto-refresh per repo that had a suppressed fs
        # event during the action. This is what gives the user the
        # post-sync / post-commit "everything's now up to date"
        # snapshot without thrashing the spinner during the action
        # itself. `drain_pending_refreshes` is a no-op when nothing
        # was queued.
        cur_mutation_jobs_running = local_mutation_active
        mutation_jobs_just_drained = prev_mutation_jobs_running and not cur_mutation_jobs_running
        if mutation_jobs_just_drained:
            drain_pending_refreshes()
        prev_mutation_jobs_running = cur_mutation_jobs_running

        periodic_refresh_fired = False
        if (
            next_periodic_refresh_at is not None
            and now >= next_periodic_refresh_at
            and last_periodic_interval > 0
            and _periodic_refresh_idle(state)
        ):
            kick_off_inline_refresh(state)
            next_periodic_refresh_at = now + last_periodic_interval
            periodic_refresh_fired = True

        # Dynamic getch timeout: snappy 100ms while anything is animating
        # so the spinner / fade-outs run smoothly, lazy 1s when truly
        # idle. The loop still wakes once a second to refresh relative
        # time tags ("2m ago") and to re-assert the terminal title; any
        # background worker that adds a task will set anim_running on
        # the next iteration, snapping us back to 10 Hz automatically.
        #
        # `mutation_jobs_just_drained` forces one extra snappy tick on the
        # mutation-active→idle edge: a worker (smart-sync especially) lands its
        # final `refresh_repo` + `link_siblings` and only THEN clears its
        # spinner flags / marks the header task terminal, so on this very
        # iteration `anim_running` can already be False. Without the
        # nudge the loop would commit to the 1s timeout and the
        # freshly-refreshed repo icons wouldn't repaint until the next
        # idle tick (or a keypress) — the "I have to refresh to see the
        # true state after a sync" symptom. The commit path dodged this
        # because its working-tree writes trigger an fs-watcher refresh
        # that keeps the loop awake; an align-only sync touches no
        # watched file, so it needs this explicit edge.
        stdscr.timeout(_main_loop_timeout_ms(
            state,
            anim_running=anim_running,
            mutation_jobs_just_drained=mutation_jobs_just_drained,
            periodic_refresh_fired=periodic_refresh_fired,
        ))

        try:
            key = read_key(stdscr)
        except KeyboardInterrupt:
            return
        if key == -1:
            continue  # tick — loop back to redraw and animate
        if key == curses.KEY_RESIZE:
            # Sync curses' cached LINES/COLS to the new terminal size and
            # force a full repaint instead of an incremental diff. Without
            # this, cells from the old layout (most visibly: black bars
            # in the task panel) survive into the new draw because curses
            # only writes cells it thinks have changed.
            try:
                curses.update_lines_cols()
            except (AttributeError, curses.error):
                pass
            try:
                stdscr.clear()
            except curses.error:
                pass
            continue

        # Modal dispatch (deepest first). Each modal owns its key handling
        # and may close itself by clearing its slot on state.
        # Commit-msg editor goes first — it's the only modal that owns
        # the hardware cursor while open, so any other modal stacked
        # on top would still want this one to swallow its own keys.
        if state.commit_msg_editor is not None:
            handle_commit_msg_editor_key(state, key)
            # When it closes, restore the hidden-cursor default the
            # main screen relies on (the modal turned it back on to
            # show the caret over the textarea).
            if state.commit_msg_editor is None:
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
            continue
        if state.help_screen is not None:
            handle_help_screen_key(state, key)
            continue
        if state.ssh_keygen_modal is not None:
            handle_ssh_keygen_modal_key(state, key)
            continue
        if state.reset_prompt is not None:
            handle_reset_prompt_key(state, key)
            continue
        if state.workflow_picker is not None:
            handle_workflow_picker_key(state, key)
            continue
        if state.align_heads_prompt is not None:
            handle_align_heads_prompt_key(state, key)
            continue
        # Log viewer dispatches BEFORE task_action_menu — it's
        # opened from the detail modal and layers on top, so it
        # owns the keyboard while it's up.
        if state.task_log_viewer is not None:
            handle_task_log_viewer_key(state, key)
            continue
        if state.task_action_menu is not None:
            handle_task_action_menu_key(state, key)
            continue
        if state.remote_branch_picker is not None:
            handle_remote_branch_picker_key(state, key)
            continue
        if state.branch_picker is not None:
            handle_branch_picker_key(state, key)
            continue
        if state.branch_name_prompt is not None:
            handle_branch_name_prompt_key(state, key)
            continue
        if state.detached_recovery_prompt is not None:
            handle_detached_recovery_prompt_key(state, key)
            continue
        # Sub-modals of action_menu — must dispatch before action_menu
        # so whichever modal is on top owns key handling. The commit
        # view (popped from the commits pane) and the remotes editor
        # are siblings; only one can be open at a time.
        if state.commit_view_modal is not None:
            handle_commit_view_modal_key(state, key)
            continue
        if state.remotes_modal is not None:
            handle_remotes_modal_key(state, key)
            continue
        if state.action_menu is not None:
            handle_action_menu_key(state, key)
            continue
        # Sub-modal of workspace_menu — same precedence rule as remotes
        # vs action_menu.
        if state.clone_modal is not None:
            handle_clone_modal_key(state, key)
            continue
        if state.workspace_menu is not None:
            handle_workspace_menu_key(state, key)
            continue
        # Creator dispatched before the picker so a creator opened from
        # the picker's "+ Create new workspace" entry receives keys
        # while the picker is still alive underneath.
        if state.workspace_creator is not None:
            handle_workspace_creator_key(state, key)
            continue
        if state.app_menu is not None:
            handle_app_menu_key(state, key)
            continue
        if state.workspace_switcher is not None:
            ws_action = handle_workspace_switcher_key(state, key)
            if ws_action == "switch-workspace":
                ws = state.active_workspace
                if ws is not None:
                    title = f"Idlegit · {ws.name}" if ws.name else "Idlegit"
            continue

        action = handle_main_key(state, key)
        if action == "quit":
            return
        if action == "confirm-quit":
            if confirm_quit(stdscr, state):
                return
            continue
        if action == "refresh":
            state.tasks.prune_completed()
            kick_off_inline_refresh(state, manual=True)
            continue
        if action == "sync":
            kick_off_sync_siblings(state)
            continue
        if action == "pull-all":
            kick_off_pull_all(state)
            continue
        if action == "switch-workspace":
            # Re-derive the title once the active workspace changes, so the
            # OSC re-emit loop above picks the new name up next tick.
            ws = state.active_workspace
            if ws is not None:
                title = f"Idlegit · {ws.name}" if ws.name else "Idlegit"
            continue
        if action == "confirm":
            stdscr.timeout(-1)  # confirm sub-loop wants blocking input
            # Hold `in_review` for the duration of the confirm sub-loop
            # so the fs-watcher queues any events that fire under us
            # rather than refreshing rows the user is mid-deciding on.
            # Cleared in `finally` so an exception in `handle_confirm`
            # can't leave us stuck in review-mode forever; the drain
            # then fires any events that landed during the window.
            state.in_review = True
            try:
                handle_confirm(stdscr, state)
            finally:
                state.in_review = False
                stdscr.timeout(100)
                drain_pending_refreshes()


def _set_terminal_title(title: str) -> None:
    """Emit the OSC 0/2 escape so the host terminal renames its window
    while idlegit is running. No-op if stdout isn't a terminal."""
    if not sys.stdout.isatty():
        return
    try:
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()
    except OSError:
        pass


def main() -> int:
    cfg = load_config()
    workspaces, persisted_active_idx = load_workspaces()
    startup_warnings = get_load_warnings()
    from core.ssh import ensure_ssh_agent, ssh_tools_status

    tools = ssh_tools_status()
    if cfg.auto_start_ssh_agent and not tools.has_ssh_agent:
        startup_warnings.append("SSH: ssh-agent not on PATH — auto-start skipped (install OpenSSH)")
    else:
        ssh_status, ssh_warn = ensure_ssh_agent(cfg.auto_start_ssh_agent)
        if ssh_status == "started":
            startup_warnings.append("Started ssh-agent for this session")
        elif ssh_status == "failed" and ssh_warn:
            startup_warnings.append(f"ssh-agent: {ssh_warn}")
    # Remember the last-active workspace name BEFORE filtering — so if
    # we drop the entry whose folders no longer exist, we can still try
    # to honour the user's previous choice in the resulting list (or
    # fall back to 0 cleanly).
    remembered_name = (
        workspaces[persisted_active_idx].name if 0 <= persisted_active_idx < len(workspaces) else ""
    )
    # Drop folders that no longer exist on disk; if a workspace loses
    # all its folders, drop the workspace entirely. The in-app creator
    # wizard fires when run() sees an empty list.
    for ws in workspaces:
        kept = []
        for f in ws.folders:
            if f.is_dir():
                kept.append(f)
            else:
                startup_warnings.append(f"{ws.name}: workspace folder unavailable: {f}")
        ws.folders = kept
    workspaces = [ws for ws in workspaces if ws.folders]
    # Re-resolve the remembered active index against the post-filter
    # list. If the remembered workspace was dropped (its folders all
    # vanished), default to 0 so the user lands somewhere valid.
    initial_active_idx = 0
    for i, ws in enumerate(workspaces):
        if ws.name == remembered_name:
            initial_active_idx = i
            break

    # Ephemeral-workspace detection: when launched from inside a git
    # repo not already covered by a configured workspace folder,
    # mint a transient workspace pointing at the detected repo and
    # make it the active one. The user lands in something useful
    # without first configuring a workspace. If the detected repo
    # IS already covered, activate that covering workspace instead
    # (still honours "the launch location wins over the persisted
    # active workspace" — just avoids a duplicate entry).
    from core.ephemeral import (
        build_ephemeral_workspace,
        find_git_repo_root,
        repo_covered_by_workspace,
    )

    detected_repo = find_git_repo_root()
    if detected_repo is not None:
        covering = repo_covered_by_workspace(detected_repo, workspaces)
        if covering is not None:
            initial_active_idx = workspaces.index(covering)
        else:
            workspaces.insert(0, build_ephemeral_workspace(detected_repo))
            initial_active_idx = 0

    title_name = workspaces[initial_active_idx].display_name if workspaces else ""
    _set_terminal_title(f"{APP_DISPLAY_NAME} · {title_name}" if title_name else APP_DISPLAY_NAME)
    try:
        # _silenced_stderr_fd swallows interpreter / library noise
        # (broken-OpenSSL hashlib spam, etc.) for the lifetime of
        # the curses session — see the helper's docstring.
        with _silenced_stderr_fd():
            curses.wrapper(
                lambda stdscr: run(
                    stdscr, cfg, workspaces, initial_active_idx, startup_warnings=startup_warnings
                )
            )
    except KeyboardInterrupt:
        pass
    finally:
        # Blank the title on exit. Most shells will set a fresh one on
        # their next prompt; if not, an empty title beats a stale one.
        _set_terminal_title("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
