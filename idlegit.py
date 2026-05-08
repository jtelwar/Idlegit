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

import curses
import sys
import termios
import time

from config import (
    APP_DISPLAY_NAME,
    apply_workspace_overrides,
    load_config,
    load_workspaces,
    save_workspaces,
    get_load_warnings,
)
from git_ops import discover_repos
from models import State
from ui import (
    SPINNER_FRAMES,
    confirm_quit,
    draw_main,
    handle_action_menu_key,
    handle_align_heads_prompt_key,
    handle_branch_name_prompt_key,
    handle_branch_picker_key,
    handle_clone_modal_key,
    handle_confirm,
    handle_detached_recovery_prompt_key,
    handle_remotes_modal_key,
    handle_main_key,
    handle_reset_prompt_key,
    handle_task_action_menu_key,
    handle_workflow_picker_key,
    handle_workspace_creator_key,
    handle_workspace_menu_key,
    handle_workspaces_picker_key,
    init_colors,
    refresh_all_workspaces,
    safe_addstr,
    tick_creator_checks,
    tick_menu_path_checks,
)
from workers import (
    kick_off_inline_refresh,
    kick_off_sync_siblings,
    switch_workspace,
)


def _disable_flow_control() -> None:
    """Stop the tty from eating Ctrl+S (XOFF) and Ctrl+Q (XON). Curses
    enables cbreak by default, which leaves the kernel-level
    software flow control on — Ctrl+S then pauses output and never
    reaches our getch loop. Clearing IXON/IXOFF on stdin makes both
    keys deliverable like any other control char. No-op if stdin
    isn't a real tty (CI, pipes, ...) or if termios is unavailable
    (Windows; we're already curses so this should never fire there)."""
    if not sys.stdin.isatty():
        return
    try:
        attrs = termios.tcgetattr(sys.stdin)
        attrs[0] &= ~(termios.IXON | termios.IXOFF)
        termios.tcsetattr(sys.stdin, termios.TCSANOW, attrs)
    except termios.error:
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
    user either commits at least one workspace (returns the list, also
    persisted via save_workspaces) or cancels with Esc (returns [])."""
    from models import State
    from ui import (
        PAIR_HEADER, draw_workspace_creator,
        open_workspace_creator,
        sidebar_geometry,
    )
    state = State(
        repos=[], workspace_name="", workspaces=[], base_config=cfg,
    )
    intro = ("No workspaces are configured yet. Add one or more folder "
             "paths to scan for git repos.")
    if startup_warnings:
        intro = (f"{startup_warnings[0]}\n\n"
                 "Add one or more folder paths to scan for git repos.")
    open_workspace_creator(
        state,
        title="Welcome to Idlegit",
        intro=intro)
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
        safe_addstr(stdscr, 1, max(2, (w - len(title)) // 2), title,
                    curses.A_BOLD | curses.color_pair(PAIR_HEADER))
        sidebar_x, _ = sidebar_geometry(w)
        draw_workspace_creator(stdscr, state, sidebar_x)
        stdscr.refresh()
        try:
            key = stdscr.getch()
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


def run(stdscr, cfg, workspaces, initial_active_idx: int = 0,
        startup_warnings=None) -> None:
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        pass
    curses.curs_set(0)
    init_colors()
    _disable_flow_control()
    stdscr.keypad(True)
    stdscr.timeout(100)  # non-blocking getch — drives sidebar animation.

    # First-run path: idlegit.workspaces missing or empty drops us into
    # the creator wizard before the main UI gets to draw. The wizard
    # blocks until the user commits at least one workspace (then we
    # persist it) or cancels with Esc (we exit cleanly).
    if not workspaces:
        workspaces = _run_workspace_creator_subloop(
            stdscr, cfg, startup_warnings=startup_warnings)
        if not workspaces:
            return
        try:
            save_workspaces(workspaces, active_index=0)
        except OSError:
            # Persistence failure is non-fatal — the user can re-create
            # the workspaces next run; right now just keep going.
            pass
        active_idx = 0
    else:
        # Honour the persisted last-active index from idlegit.workspaces
        # so the user lands back where they left off. main() already
        # validated this against the post-filter workspace list.
        active_idx = max(0, min(initial_active_idx, len(workspaces) - 1))
    active_ws = workspaces[active_idx]
    # Discover repos for EVERY configured workspace up front, then
    # refresh them all in parallel. Loading every workspace at startup
    # makes ←/→ workspace switching instant (the lists are already
    # there, no async fetch race), and surfaces every workspace's
    # repos on the loading screen so the user can see the whole
    # picture instead of just the active workspace's contents.
    workspace_repos: list = []
    for ws in workspaces:
        ws_repos = _discover_workspace_repos(ws.folders)
        # Stash the discovered list onto the Workspace so switching
        # later just reuses these refreshed Repo objects — no re-
        # discovery, no async refresh race when the user rapid-fires
        # ←/→ between workspaces.
        ws.cached_repos = ws_repos
        workspace_repos.append((ws.name, ws_repos, ws.subtrees))
    repos = workspace_repos[active_idx][1]
    refresh_all_workspaces(stdscr, workspace_repos, cfg.name_display_max,
                           cfg.name_truncation, active_index=active_idx)

    title = f"Idlegit · {active_ws.name}" if active_ws.name else "Idlegit"
    # Re-emit immediately after curses owns the terminal: VS Code's
    # integrated terminal (and a few others) overwrite the title with
    # the running-process name once the alt-screen comes up.
    _set_terminal_title(title)
    last_title_emitted = title
    last_title_at = time.monotonic()
    state = State(
        repos=repos,
        workspace_name=active_ws.name,
        workspaces=workspaces,
        active_workspace_index=active_idx,
        base_config=cfg,
    )
    for warning in startup_warnings or []:
        t = state.tasks.add("startup warning")
        state.tasks.update(t, "warn", warning)
    # Layer base-config defaults + this workspace's overrides on top of
    # the freshly-built State so the same code path runs on startup and
    # later workspace switches.
    apply_workspace_overrides(state, cfg, active_ws)

    while True:
        now = time.monotonic()
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
            tick_creator_checks(state)
            if state.workspace_creator is not None else False)
        # Same idea for the workspace settings modal — its folder rows
        # carry per-path drafts that need re-checking when text edits
        # land. Bundling both into anim_running keeps the spinner
        # ticking through any in-flight discover_repos work.
        menu_checking = (
            tick_menu_path_checks(state)
            if state.workspace_menu is not None else False)
        # Runtime commit: if the creator finished while open as a
        # modal (e.g. via the "+ Create new workspace" picker entry),
        # consume its result here — append the new workspaces, persist,
        # close both modals, and switch to the first new workspace so
        # the user lands inside what they just created.
        if (state.workspace_creator is not None
                and state.workspace_creator.result is not None):
            new_ws = state.workspace_creator.result
            state.workspace_creator = None
            if new_ws:
                first_new_index = len(state.workspaces)
                state.workspaces = list(state.workspaces) + list(new_ws)
                state.workspaces_picker = None
                switch_workspace(state, first_new_index)
                # switch_workspace now persists the post-switch active
                # index; mirror it here for the append+save case so the
                # newly-created workspace is also written to disk.
                try:
                    save_workspaces(state.workspaces,
                                    state.active_workspace_index)
                except OSError:
                    pass
                ws = state.active_workspace
                if ws is not None:
                    title = (f"Idlegit · {ws.name}"
                             if ws.name else "Idlegit")
        action_menu_loading = (
            state.action_menu is not None
            and (state.action_menu.state_loading
                 or state.action_menu.tree_loading
                 or state.action_menu.commits_loading))
        anim_running = (state.tasks.has_running()
                        or state.tasks.has_pending_auto_remove(
                            state.auto_remove_completed_after)
                        or any(r.suggesting or r.refreshing for r in state.repos)
                        or any(c.suggesting
                               for r in state.repos for c in r.children)
                        or creator_checking
                        or menu_checking
                        or action_menu_loading
                        or (state.diff_viewer is not None
                            and state.diff_viewer.loading))
        if anim_running:
            state.spinner_frame = (state.spinner_frame + 1) % len(SPINNER_FRAMES)

        # Dynamic getch timeout: snappy 100ms while anything is animating
        # so the spinner / fade-outs run smoothly, lazy 1s when truly
        # idle. The loop still wakes once a second to refresh relative
        # time tags ("2m ago") and to re-assert the terminal title; any
        # background worker that adds a task will set anim_running on
        # the next iteration, snapping us back to 10 Hz automatically.
        stdscr.timeout(100 if anim_running else 1000)

        try:
            key = stdscr.getch()
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
        if state.reset_prompt is not None:
            handle_reset_prompt_key(state, key)
            continue
        if state.workflow_picker is not None:
            handle_workflow_picker_key(state, key)
            continue
        if state.align_heads_prompt is not None:
            handle_align_heads_prompt_key(state, key)
            continue
        if state.task_action_menu is not None:
            handle_task_action_menu_key(state, key)
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
        # Sub-modal of action_menu — must dispatch before action_menu so
        # the modal on top owns key handling while it's open.
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
        if state.workspaces_picker is not None:
            handle_workspaces_picker_key(state, key)
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
            kick_off_inline_refresh(state)
            continue
        if action == "sync":
            kick_off_sync_siblings(state)
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
            try:
                handle_confirm(stdscr, state)
            finally:
                stdscr.timeout(100)


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
    # Remember the last-active workspace name BEFORE filtering — so if
    # we drop the entry whose folders no longer exist, we can still try
    # to honour the user's previous choice in the resulting list (or
    # fall back to 0 cleanly).
    remembered_name = (workspaces[persisted_active_idx].name
                       if 0 <= persisted_active_idx < len(workspaces)
                       else "")
    # Drop folders that no longer exist on disk; if a workspace loses
    # all its folders, drop the workspace entirely. The in-app creator
    # wizard fires when run() sees an empty list.
    for ws in workspaces:
        kept = []
        for f in ws.folders:
            if f.is_dir():
                kept.append(f)
            else:
                startup_warnings.append(
                    f"{ws.name}: workspace folder unavailable: {f}")
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

    title_name = (workspaces[initial_active_idx].name
                  if workspaces else "")
    _set_terminal_title(
        f"{APP_DISPLAY_NAME} · {title_name}" if title_name else APP_DISPLAY_NAME)
    try:
        curses.wrapper(
            lambda stdscr: run(
                stdscr, cfg, workspaces, initial_active_idx,
                startup_warnings=startup_warnings))
    except KeyboardInterrupt:
        pass
    finally:
        # Blank the title on exit. Most shells will set a fresh one on
        # their next prompt; if not, an empty title beats a stale one.
        _set_terminal_title("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
