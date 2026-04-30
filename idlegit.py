#!/usr/bin/env python3
"""
idlegit — interactive git multi-repo manager.

Scans a workspace for git repos (the workspace itself if it is one, plus
immediate child folders that contain .git) and lets you commit/push them
from a single screen.

This file is just the entry point — the real code lives in:
    config.py    Config + load_config + DEFAULT_* constants
    models.py    dataclasses (Repo, ChildRef, State, Task, Tasks, ActionMenu, …)
    git_ops.py   git subprocess wrappers, discovery, sync, suggest, LFS
    workers.py   background pipelines (kick_off_*) for commits, sync, refresh
    ui.py        curses rendering, modal openers/handlers, main key handler

See idlegit.conf for runtime configuration; see README.md for the keymap.
"""
from __future__ import annotations

import curses
import sys
import termios
import time

from config import CONFIG_FILE, load_config
from git_ops import discover_repos
from models import State
from ui import (
    SPINNER_FRAMES,
    confirm_quit,
    draw_main,
    handle_action_menu_key,
    handle_align_heads_prompt_key,
    handle_branch_picker_key,
    handle_confirm,
    handle_main_key,
    handle_reset_prompt_key,
    handle_task_action_menu_key,
    handle_workflow_picker_key,
    init_colors,
    refresh_all,
    safe_addstr,
    PAIR_ERR,
)
from workers import kick_off_inline_refresh, kick_off_sync_siblings


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


def run(stdscr, cfg) -> None:
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        pass
    curses.curs_set(0)
    init_colors()
    _disable_flow_control()
    stdscr.keypad(True)
    stdscr.timeout(100)  # non-blocking getch — drives sidebar animation.

    # Walk every configured folder, collect repos, dedupe by absolute
    # path, and present them sorted by display name (case-insensitive)
    # so the order is stable across resizes and refreshes regardless
    # of which folder a repo came from.
    repos = []
    seen: set = set()
    for folder in cfg.repository_folders:
        for r in discover_repos(folder):
            if r.path in seen:
                continue
            seen.add(r.path)
            repos.append(r)
    repos.sort(key=lambda r: r.display_name.lower())

    if not repos:
        folders_str = ", ".join(str(f) for f in cfg.repository_folders)
        safe_addstr(stdscr, 0, 0,
                    f"no git repos found under {folders_str}",
                    curses.color_pair(PAIR_ERR))
        safe_addstr(stdscr, 2, 0,
                    f"edit {CONFIG_FILE.name} to point 'repository_folders' "
                    "at one or more valid paths, then re-run.",
                    curses.A_DIM)
        stdscr.refresh()
        stdscr.timeout(-1)
        stdscr.getch()
        return

    refresh_all(stdscr, repos, cfg.name_display_max,
                cfg.name_truncation, cfg.subtrees)

    # Title uses the first folder's name when there's only one;
    # multiple folders show as "idlegit · N folders" since we can't
    # cram every name into a window title.
    if len(cfg.repository_folders) == 1:
        first = cfg.repository_folders[0]
        workspace_name = first.name or str(first)
    else:
        workspace_name = f"{len(cfg.repository_folders)} folders"
    title = f"idlegit · {workspace_name}" if workspace_name else "idlegit"
    # Re-emit immediately after curses owns the terminal: VS Code's
    # integrated terminal (and a few others) overwrite the title with
    # the running-process name once the alt-screen comes up.
    _set_terminal_title(title)
    last_title_emitted = title
    last_title_at = time.monotonic()
    state = State(
        repos=repos,
        workspace_name=workspace_name,
        suggest_added=cfg.suggest_added,
        suggest_updated=cfg.suggest_updated,
        suggest_deleted=cfg.suggest_deleted,
        lfs_warn_bytes=cfg.lfs_warn_bytes,
        branch_display_max=cfg.branch_display_max,
        name_display_max=cfg.name_display_max,
        task_name_display_max=cfg.task_name_display_max,
        name_truncation=cfg.name_truncation,
        branch_truncation=cfg.branch_truncation,
        task_name_truncation=cfg.task_name_truncation,
        max_visible_repo_rows=cfg.max_visible_repo_rows,
        subtrees=cfg.subtrees,
        track_actions_default=cfg.track_actions_default,
        actions_poll_seconds=cfg.actions_poll_seconds,
        auto_remove_completed_after=cfg.auto_remove_completed_after,
        auto_stage=cfg.default_auto_stage,
        auto_push=cfg.default_auto_push,
    )

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
        anim_running = (state.tasks.has_running()
                        or state.tasks.has_pending_auto_remove(
                            state.auto_remove_completed_after)
                        or any(r.suggesting or r.refreshing for r in state.repos)
                        or any(c.suggesting
                               for r in state.repos for c in r.children))
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
        if state.action_menu is not None:
            handle_action_menu_key(state, key)
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
    valid = [f for f in cfg.repository_folders if f.is_dir()]
    if not valid:
        listed = ", ".join(str(f) for f in cfg.repository_folders) or "(none)"
        print(f"error: no valid repository folders. Tried: {listed}",
              file=sys.stderr)
        print(f"edit {CONFIG_FILE} to set 'repository_folders' to one or "
              "more existing directories (comma-separated).",
              file=sys.stderr)
        return 1
    if len(valid) < len(cfg.repository_folders):
        # Drop missing entries silently after warning so the rest of
        # the run still works against whatever's actually on disk.
        missing = [str(f) for f in cfg.repository_folders if not f.is_dir()]
        print(f"warning: skipping missing folders: {', '.join(missing)}",
              file=sys.stderr)
        cfg.repository_folders = valid
    if len(valid) == 1:
        first = valid[0]
        title_name = first.name or str(first)
    else:
        title_name = f"{len(valid)} folders"
    _set_terminal_title(f"idlegit · {title_name}" if title_name else "idlegit")
    try:
        curses.wrapper(lambda stdscr: run(stdscr, cfg))
    except KeyboardInterrupt:
        pass
    finally:
        # Blank the title on exit. Most shells will set a fresh one on
        # their next prompt; if not, an empty title beats a stale one.
        _set_terminal_title("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
