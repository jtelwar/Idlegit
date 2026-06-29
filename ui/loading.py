"""Parallel workspace refresh and startup loading screen."""
from __future__ import annotations

import curses
from typing import List, Tuple

from core.state.repos import Repo
from core.config import APP_DISPLAY_NAME, DEFAULT_TRUNCATION_MODE, VERSION
from core.workers import kick_off_startup_refresh

from .colors import PAIR_BRANCH, PAIR_HEADER, PAIR_OK
from .geometry import safe_addstr
from .sidebar import SPINNER_FRAMES


# Loading-screen poll interval. The pre-Esc-cancel version called
# `curses.napms(80)` between frames, which blocked the input buffer
# entirely so a press during load only registered after every refresh
# had landed (felt like a frozen app). Switching to a `getch` with
# the same effective frame budget keeps the spinner animating AND
# lets us react to Esc immediately.
_FRAME_POLL_MS = 80


def refresh_all_workspaces(stdscr,
                           workspace_repos: List[Tuple[str, List[Repo], object]],
                           name_max: int,
                           name_mode: str = DEFAULT_TRUNCATION_MODE,
                           active_index: int = 0) -> bool:
    """Refresh the active workspace for startup loading.

    `workspace_repos` is a list of (workspace_name, repos, subtrees)
    triples — typically built from `state.workspaces` plus a per-
    workspace `discover_repos`. `active_index` is the workspace the
    main UI will land in once loading completes; it gets an "(active)"
    marker on its header row. Inactive workspaces are intentionally left
    untouched and refresh on entry via switch_workspace.

    Returns True on a clean completion, False when the user pressed
    Esc while the active workspace is loading."""
    all_repos: List[Repo] = [r for _, repos, _ in workspace_repos
                             for r in repos]
    if not all_repos:
        return True
    # Track completion by repo identity so concurrent threads can flip
    # their own bit without coordinating on a shared index.
    done: dict = {id(r): False for r in all_repos}

    def mark_done(r: Repo) -> None:
        done[id(r)] = True

    active_repos: List[Repo] = []
    active_subtrees = []
    if 0 <= active_index < len(workspace_repos):
        active_repos = list(workspace_repos[active_index][1])
        active_subtrees = workspace_repos[active_index][2]
    else:
        active_repos = list(all_repos)

    if not kick_off_startup_refresh(active_repos, active_subtrees, mark_done):
        return True

    curses.curs_set(0)
    stdscr.timeout(_FRAME_POLL_MS)
    frame = 0
    cancelled = False
    while not all(done.get(id(r), False) for r in active_repos):
        draw_workspace_loading(
            stdscr, workspace_repos, done, name_max, name_mode,
            active_index, SPINNER_FRAMES[frame % len(SPINNER_FRAMES)])
        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            cancelled = True
            break
        if key == 27:  # Esc — cancel load and signal "quit" to caller
            cancelled = True
            break
        # KEY_RESIZE / -1 (timeout) / any other key falls through and
        # the next iteration redraws + polls again.
        frame += 1

    if cancelled:
        return False

    draw_workspace_loading(
        stdscr, workspace_repos, done, name_max, name_mode,
        active_index, SPINNER_FRAMES[frame % len(SPINNER_FRAMES)])
    curses.napms(120)
    return True


def draw_workspace_loading(stdscr,
                           workspace_repos: List[Tuple[str, List[Repo], object]],
                           done: dict, name_max: int, name_mode: str,
                           active_index: int, spinner: str) -> None:
    """Render the loading screen as one row per workspace — no expanded
    repo lists. A workspace's row ticks ✓ once every one of its repos
    finishes refreshing; until then the spinner glyph spins next to it.
    The active workspace gets a bold "(active)" tag so the user can see
    where they'll land."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    n_ws = len(workspace_repos)
    if n_ws == 0:
        return

    # Per-workspace completion: a workspace is "done" only when every
    # one of its repos has refreshed. Empty workspaces (no repos found)
    # are considered done immediately.
    ws_done = []
    for _, repos, _ in workspace_repos:
        if not repos:
            ws_done.append(True)
        else:
            ws_done.append(all(done.get(id(r), False) for r in repos))

    title = APP_DISPLAY_NAME
    ver_suffix = f"  v{VERSION}"
    completed_ws = sum(1 for d in ws_done if d)
    if n_ws == 1:
        summary = f"{spinner}  loading workspace"
    else:
        summary = (f"{spinner}  loading workspaces "
                   f"({completed_ws}/{n_ws})")

    # Layout: title (1) + spacer (1) + summary (1) + spacer (1) + N
    # workspace rows.
    block_h = 4 + n_ws
    name_w = max(
        max((len(ws_name) for ws_name, _, _ in workspace_repos), default=0),
        len(" (active)") + 8,
    )
    top = max(1, (h - block_h) // 2)
    cx = w // 2
    list_left = max(0, cx - (name_w + 8) // 2)

    title_x = max(0, cx - (len(title) + len(ver_suffix)) // 2)
    safe_addstr(stdscr, top, title_x, title,
                curses.A_BOLD | curses.color_pair(PAIR_HEADER))
    safe_addstr(stdscr, top, title_x + len(title), ver_suffix,
                curses.color_pair(PAIR_BRANCH) | curses.A_DIM)
    safe_addstr(stdscr, top + 2, max(0, cx - len(summary) // 2),
                summary, curses.color_pair(PAIR_BRANCH))

    y = top + 4
    for i, (ws_name, _repos, _) in enumerate(workspace_repos):
        is_active = (i == active_index)
        if ws_done[i]:
            mark, mark_attr = "✓", curses.color_pair(PAIR_OK)
        else:
            mark, mark_attr = spinner, curses.A_DIM
        # Glyph + workspace name on a single line. (active) suffix in a
        # dimmer attribute keeps the visual hierarchy: name first, then
        # the marker tag.
        safe_addstr(stdscr, y, list_left, f"  {mark}  ", mark_attr)
        name_attr = (curses.color_pair(PAIR_BRANCH) | curses.A_BOLD
                     if is_active
                     else curses.color_pair(PAIR_BRANCH))
        safe_addstr(stdscr, y, list_left + 5, ws_name, name_attr)
        if is_active:
            safe_addstr(stdscr, y, list_left + 5 + len(ws_name),
                        " (active)", curses.A_DIM)
        y += 1

    stdscr.refresh()
