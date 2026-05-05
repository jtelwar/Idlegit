"""All curses rendering, modal openers, and keyboard handlers. Every
function here is called from the main thread; nothing in this file
blocks on git or kicks off workers directly — the workers module owns
the background pipeline."""
from __future__ import annotations

import curses
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from models import (
    ChildRef, FileEntry, LFSCandidate, Repo, ReviewBlock, State,
    ThenRunSelector, WorkflowToggle,
)
from config import CONFIG_FILE, DEFAULT_TRUNCATION_MODE, VERSION
from git_ops import (
    find_lfs_warnings, gh_available, link_siblings, MAX_PARALLEL_GIT_JOBS,
    parse_github_slug, query_working_tree, would_run_on_push,
)
from workers import (
    _build_recovery_prompt, execute_detached_recovery,
    kick_off_bulk_suggest, kick_off_suggest_for, kick_off_workers,
    refresh_repo_with_remote_state,
)
# Color palette + state-dot helpers live in ui.colors so they can be
# imported from leaf modules (modals, sidebar) without pulling in the
# rest of this monolith. Re-exported here so external callers keep
# `from ui import PAIR_…, state_color, init_colors`-style imports.
from .colors import (  # noqa: F401  (re-exported public API)
    PAIR_AHEAD, PAIR_BEHIND, PAIR_BRANCH, PAIR_DIRTY, PAIR_ERR,
    PAIR_HEADER, PAIR_HINT, PAIR_OK,
    PAIR_PASTEL_BLUE, PAIR_PASTEL_BLUE_ACTIVE,
    PAIR_PASTEL_GREEN, PAIR_PASTEL_GREEN_ACTIVE,
    PAIR_PASTEL_RED, PAIR_PASTEL_RED_ACTIVE,
    PAIR_PASTEL_YELLOW, PAIR_PASTEL_YELLOW_ACTIVE,
    PAIR_SB_CYAN, PAIR_SB_CYAN_ACTIVE, PAIR_SB_ERR, PAIR_SB_FG,
    PAIR_SB_FG_ACTIVE,
    PAIR_SB_OK, PAIR_SB_WARN, PAIR_TOGGLE_OFF, PAIR_TOGGLE_ON, PAIR_WARN,
    _state_color, child_state_color, init_colors, state_color,
)
from .geometry import (  # noqa: F401  (re-exported public API)
    SIDEBAR_W, SIDEBAR_W_NARROW, draw_modal_fill, field_visible,
    modal_geometry, safe_addstr, sidebar_geometry, truncate,
)
from .hints import (  # noqa: F401  (re-exported public API)
    KEY_BACKSPACE, KEY_CTRL_R, KEY_CTRL_S, KEY_DOWN, KEY_END, KEY_ENTER,
    KEY_ESC, KEY_HOME, KEY_LEFT, KEY_LEFT_RIGHT, KEY_RIGHT, KEY_SHIFT_TAB,
    KEY_SPACE, KEY_TAB, KEY_UP, KEY_UP_DOWN, Hint, fit_hints,
    render_hint, render_hints,
)
# Modals — each one is self-contained in ui.modals.<name>; the package
# re-exports the public open/draw/handle trio. Imported here so callers
# of the package don't have to know which submodule owns which modal.
from .modals import (  # noqa: F401  (re-exported public API)
    commit_workspace_creator,
    draw_action_menu, draw_align_heads_prompt, draw_branch_name_prompt,
    draw_branch_picker, draw_detached_recovery_prompt, draw_diff_viewer,
    draw_reset_prompt, draw_task_action_menu, draw_workflow_picker,
    draw_workspace_creator, draw_workspace_menu, draw_workspaces_picker,
    handle_action_menu_key, handle_align_heads_prompt_key,
    handle_branch_name_prompt_key, handle_branch_picker_key,
    handle_detached_recovery_prompt_key, handle_diff_viewer_key,
    handle_reset_prompt_key, handle_task_action_menu_key,
    handle_workflow_picker_key, handle_workspace_creator_key,
    handle_workspace_menu_key, handle_workspaces_picker_key,
    open_action_menu, open_align_heads_prompt, open_branch_name_prompt,
    open_branch_picker, open_detached_recovery_prompt, open_diff_viewer,
    open_reset_prompt, open_task_action_menu, open_workflow_picker,
    open_workspace_creator, open_workspace_menu, open_workspaces_picker,
    tick_creator_checks, tick_menu_path_checks,
)
# Right-hand task panel.
from .sidebar import (  # noqa: F401  (re-exported public API)
    SPINNER_FRAMES, draw_sidebar,
)


# ---------- Loading screen (startup only) ---------------------------------


def refresh_all_workspaces(stdscr,
                           workspace_repos: List[Tuple[str, List[Repo], object]],
                           name_max: int,
                           name_mode: str = DEFAULT_TRUNCATION_MODE,
                           active_index: int = 0) -> None:
    """Refresh every repo across every configured workspace in parallel,
    rendering a grouped loading screen so the user sees all workspaces
    at once instead of one in isolation.

    `workspace_repos` is a list of (workspace_name, repos, subtrees)
    triples — typically built from `state.workspaces` plus a per-
    workspace `discover_repos`. `active_index` is the workspace the
    main UI will land in once loading completes; it gets an "(active)"
    marker on its header row. Total work is parallel across all repos
    in all workspaces, so a 3-workspace startup completes in roughly
    the same wall-clock time as a single-workspace one."""
    all_repos: List[Repo] = [r for _, repos, _ in workspace_repos
                             for r in repos]
    if not all_repos:
        return
    # Track completion by repo identity so concurrent threads can flip
    # their own bit without coordinating on a shared index.
    done: dict = {id(r): False for r in all_repos}

    def work(r: Repo) -> None:
        refresh_repo_with_remote_state(r)
        done[id(r)] = True

    curses.curs_set(0)
    max_workers = max(1, min(len(all_repos), MAX_PARALLEL_GIT_JOBS))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(work, r) for r in all_repos]
        frame = 0
        while not all(f.done() for f in futures):
            draw_workspace_loading(
                stdscr, workspace_repos, done, name_max, name_mode,
                active_index, SPINNER_FRAMES[frame % len(SPINNER_FRAMES)])
            curses.napms(80)
            frame += 1
        draw_workspace_loading(
            stdscr, workspace_repos, done, name_max, name_mode,
            active_index, "✓")
        curses.napms(120)
        for f in futures:
            f.result()  # surface any thread exception
    # Link siblings within each workspace independently — a submodule
    # inside one workspace shouldn't be linked to a same-URL repo in
    # a different workspace.
    for _, repos, subtrees in workspace_repos:
        link_siblings(repos, subtrees)


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

    title = "idlegit"
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
    for i, (ws_name, repos, _) in enumerate(workspace_repos):
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




# ---------- Main screen ---------------------------------------------------


def _body_height_for(state: State, h: int) -> int:
    """Height (in rows) available for the repo body. Reserves space for the
    title (1), toggles row (1) + blank (1), one blank line before hints,
    two hint lines, and the state legend (1) — 7 rows of chrome total."""
    chrome = 7
    avail = h - chrome
    if avail < 1:
        return 1
    if state.max_visible_repo_rows > 0:
        avail = min(avail, state.max_visible_repo_rows)
    return max(1, avail)


def _ensure_focused_visible(state: State, body_h: int, total_body: int) -> None:
    """Adjust state.body_scroll so the focused body row is on-screen.
    Workspace row (selected = -1) is rendered on the title line and
    isn't part of the body, so it doesn't move scroll."""
    body_idx = state.selected
    if body_idx < 0 or body_idx >= total_body:
        return
    if body_idx < state.body_scroll:
        state.body_scroll = body_idx
    elif body_idx >= state.body_scroll + body_h:
        state.body_scroll = body_idx - body_h + 1
    state.body_scroll = max(0, min(state.body_scroll, max(0, total_body - body_h)))


def _split_remaining_width(remaining_w: int, tasks_min_pct: float,
                           tasks_max_pct: float) -> Tuple[int, int]:
    """Split width after fixed repo columns into (message_w, tasks_w).

    The task panel starts from an even split, then clamps to the
    configured percentage band. Percentages are of `remaining_w`, not
    of the full terminal width."""
    if remaining_w <= 0:
        return 0, 0
    min_pct = max(0.0, min(1.0, tasks_min_pct))
    max_pct = max(min_pct, max(0.0, min(1.0, tasks_max_pct)))
    min_w = int(remaining_w * min_pct)
    max_w = int(remaining_w * max_pct)
    ideal_w = remaining_w // 2
    tasks_w = max(min_w, min(max_w, ideal_w))
    if remaining_w >= 2 and tasks_w >= remaining_w:
        tasks_w = remaining_w - 1
    return remaining_w - tasks_w, tasks_w


# ---------- Main-screen hints registry -----------------------------------


def _esc_hint(state: State) -> Hint:
    """Esc means three different things on the main screen — pick the
    one that's actually about to fire so the footer doesn't lie."""
    if state.focused_panel == "tasks":
        return Hint(KEY_ESC, "back to repos")
    holder = _focused_message_holder(state)
    if holder is not None and holder.message:
        return Hint(KEY_ESC, "clear message")
    if state.has_messages:
        return Hint(KEY_ESC, "discard + quit")
    return Hint(KEY_ESC, "quit")


def _workspace_row_hints(state: State) -> List[Hint]:
    hints = [Hint(KEY_UP_DOWN, "navigate")]
    if len(state.workspaces) > 1:
        hints.append(Hint(KEY_LEFT_RIGHT, "cycle workspaces"))
    hints.append(Hint(KEY_TAB, "workspaces…"))
    hints.append(Hint(KEY_ENTER, "settings…"))
    return hints


def _body_row_hints(state: State) -> List[Hint]:
    """Hints for repo / submodule-child rows. Reflects whether the
    focused row has an editable message field, whether suggest is
    available, and whether Enter would actually launch the review."""
    hints: List[Hint] = [Hint(KEY_UP_DOWN, "navigate")]
    holder = _focused_message_holder(state)
    cur_repo = state.current_repo
    cur_child = state.current_child

    # Tab opens the per-row action menu for repos and submodule
    # children; a subtree child has no actions menu, so we omit it.
    if cur_repo is not None or (cur_child is not None
                                and cur_child[1].kind == "submodule"):
        hints.append(Hint(KEY_TAB, "actions…"))

    if holder is not None:
        # Editable row: typing edits the message inline. Surface
        # "Enter commit + push" only when there's something to commit
        # so the user isn't promised an action that won't run.
        if not holder.message:
            hints.append(Hint(KEY_LEFT, "suggest"))
            hints.append(Hint(f"Shift+{KEY_LEFT}", "suggest all"))
        if state.has_messages:
            hints.append(Hint(KEY_ENTER, "review + commit"))
    else:
        # Subtree row or otherwise non-editable. Enter still triggers
        # review iff some other row already carries a message.
        if state.has_messages:
            hints.append(Hint(KEY_ENTER, "review + commit"))

    return hints


def _task_panel_hints(state: State) -> List[Hint]:
    items = state.tasks.snapshot()
    n = len(items)
    hints: List[Hint] = []
    if n > 0:
        hints.append(Hint(KEY_UP_DOWN, "navigate"))
        hints.append(Hint(KEY_TAB, "task detail…"))
        if 0 <= state.task_selected < n:
            t = items[state.task_selected]
            if t.status != "running":
                hints.append(Hint(KEY_ENTER, "remove task"))
    return hints


def _main_hints_primary(state: State) -> List[Hint]:
    """First footer line — context-specific. Picks the hint set for
    whichever zone of the main UI currently has focus. The toggle
    row is gone now — every body index lands on a repo / child."""
    if state.focused_panel == "tasks":
        return _task_panel_hints(state)
    if state.on_workspace_row:
        return _workspace_row_hints(state)
    return _body_row_hints(state)


def _main_hints_global(state: State) -> List[Hint]:
    """Second footer line — always-applicable shortcuts. Shift+Tab,
    Ctrl+R / Ctrl+S, and the context-aware Esc, in that order."""
    if state.focused_panel == "tasks":
        panel_hint = Hint(KEY_SHIFT_TAB, "back to repos")
    else:
        panel_hint = Hint(KEY_SHIFT_TAB, "tasks panel")
    return [
        panel_hint,
        Hint(KEY_CTRL_R, "refresh"),
        Hint(KEY_CTRL_S, "smart-sync"),
        _esc_hint(state),
    ]


def draw_main(stdscr, state: State) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    body_h = _body_height_for(state, h)

    safe_addstr(stdscr, 0, 0, "idlegit",
                curses.A_BOLD | curses.color_pair(PAIR_HEADER))
    if state.workspace_name:
        safe_addstr(stdscr, 0, len("idlegit"), " · ", curses.A_DIM)
        # Workspace selector. When the title row has focus the name is
        # wrapped in muted chevrons (advertising ←/→ as cycle keys; the
        # same "‹ X ›" convention used by the then-run line). The
        # chevrons stay dim so the workspace name itself reads as the
        # primary content; with a single workspace they're cosmetic
        # (cycling is a no-op) but the visual cue still tells the user
        # "this row is focused".
        # Chevrons only render when the workspace row is BOTH selected
        # AND the repos panel itself is focused — otherwise they stay
        # lit when the user has tabbed over to the task panel, which
        # reads as both panels claiming focus simultaneously.
        ws_focused = (state.on_workspace_row
                      and state.focused_panel == "repos")
        x = len("idlegit") + 3
        if ws_focused:
            safe_addstr(stdscr, 0, x, "‹ ", curses.A_DIM)
            x += 2
        ws_attr = curses.A_BOLD | curses.color_pair(PAIR_BRANCH)
        safe_addstr(stdscr, 0, x, state.workspace_name, ws_attr)
        x += len(state.workspace_name)
        if ws_focused:
            safe_addstr(stdscr, 0, x, " ›", curses.A_DIM)

    toggle_y = 2
    # "Repositories" header on the left of the toggles row, mirroring
    # the "Tasks" header in the sidebar. The accent (cyan) only lights
    # up when this panel has focus; otherwise it dims to match the
    # sidebar's inactive header.
    repos_active = state.focused_panel == "repos"
    # Active = cyan accent (matches the Tasks-panel header in the
    # sidebar). The magenta PAIR_HEADER is reserved for the title.
    repos_header_attr = (
        curses.color_pair(PAIR_BRANCH) | curses.A_BOLD if repos_active
        else curses.A_DIM | curses.A_BOLD)
    safe_addstr(stdscr, toggle_y, 2, "Repositories", repos_header_attr)

    # The three commit/sync toggles that used to live on this row
    # (auto-stage, auto-push, align-heads) moved into the workspace
    # menu's COMMIT and SMART-SYNC sections — the main panel just
    # displays the "Repositories" header here now.

    nm = state.name_display_max
    # Children share the parent's name cap by default (-1 sentinel);
    # a positive value lets the user truncate submodule + subtree
    # rows tighter without affecting parent rows.
    cnm = state.child_name_display_max
    if cnm < 0:
        cnm = nm
    bm = state.branch_display_max
    nmode = state.name_truncation
    bmode = state.branch_truncation
    # Column widths must accommodate every visible row, including
    # submodule children. Children render at column 4 with a "↳ " glyph
    # (2 cells) so they need 4 extra cells of name budget compared to
    # parent rows. Without this allowance, the branch column overwrites
    # the tail of long child names and the configured truncation policy
    # never fires (it just looks like end-truncation by clipping).
    name_lengths = [len(truncate(r.display_name, nm, nmode))
                    for r in state.repos] or [len("Repositories")]
    branch_lengths = [len(f"[{truncate(r.branch, bm, bmode)}]")
                      for r in state.repos] or [0]
    for parent in state.repos:
        for ch in parent.children:
            name_lengths.append(
                4 + len(truncate(ch.repo.display_name, cnm, nmode)))
            if ch.branch:
                branch_lengths.append(
                    len(f"[{truncate(ch.branch, bm, bmode)}]"))
    name_w = max(name_lengths) + 2
    branch_w = max(branch_lengths) + 2
    marker_w = 3
    field_x = 2 + name_w + branch_w + marker_w
    remaining_w = max(0, w - field_x - 1)
    field_w, sidebar_w = _split_remaining_width(
        remaining_w,
        state.tasks_min_width_percent,
        state.tasks_max_width_percent)
    sidebar_x = field_x + field_w
    main_w = sidebar_x

    if field_w < 1 or h < 8:
        safe_addstr(stdscr, 0, 0, "terminal too small — resize and try again",
                    curses.color_pair(PAIR_ERR))
        stdscr.refresh()
        return

    base_y = 4
    body_rows = state.selectable_rows()
    _ensure_focused_visible(state, body_h, len(body_rows))
    visible_start = state.body_scroll
    visible_end = min(len(body_rows), visible_start + body_h)

    spinner_char = SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]
    y_for_body: Dict[int, int] = {}
    # The repos panel only shows its focus arrow when it's the active
    # panel. If the user has Shift+Tab'd over to the task panel,
    # `state.selected` still names a repo row but it shouldn't be
    # highlighted as focused on the repos side — otherwise both panels
    # appear to have focus simultaneously, which reads as confusing.
    repos_panel_active = state.focused_panel == "repos"
    for screen_i, body_idx in enumerate(range(visible_start, visible_end)):
        row = body_rows[body_idx]
        y = base_y + screen_i
        y_for_body[body_idx] = y
        focused = repos_panel_active and (state.selected == body_idx)
        if row[0] == "repo":
            row_cursor = state.field_cursor if focused else 0
            draw_repo_row(stdscr, y, row[1], focused,
                          name_w, branch_w, field_x, field_w,
                          nm, bm, nmode, bmode, row_cursor, spinner_char)
        else:  # child
            row_cursor = state.field_cursor if focused else 0
            draw_child_row(stdscr, y, row[2], focused,
                           name_w, branch_w, field_x, field_w,
                           cnm, bm, nmode, bmode,
                           row_cursor, spinner_char)

    if visible_start > 0:
        safe_addstr(stdscr, base_y - 1, 2,
                    f"↑ {visible_start} more above", curses.A_DIM)
    if visible_end < len(body_rows):
        below = len(body_rows) - visible_end
        safe_addstr(stdscr, base_y + body_h, 2,
                    f"↓ {below} more below", curses.A_DIM)

    # Subtle focus marker at column 0 of the active body row. Skipped
    # on the workspace row (selected = -1) since the chevrons around
    # the workspace name already advertise focus and an extra glyph at
    # column 0 would clobber the "i" of "idlegit". Also skipped when
    # the user has tabbed over to the task panel — having the marker
    # lit on both sides reads as "both panels are active".
    focus_y: Optional[int] = None
    if repos_panel_active and state.selected >= 0:
        focus_y = y_for_body.get(state.selected)
    if focus_y is not None:
        safe_addstr(stdscr, focus_y, 0, "›",
                    curses.color_pair(PAIR_BRANCH) | curses.A_BOLD)

    hint_y = base_y + body_h + 1
    hint_max_w = max(0, main_w - 4)
    render_hints(stdscr, hint_y, 2, hint_max_w,
                 _main_hints_primary(state), attr=curses.A_DIM)
    render_hints(stdscr, hint_y + 1, 2, hint_max_w,
                 _main_hints_global(state), attr=curses.A_DIM)
    draw_state_legend(stdscr, hint_y + 2, 2)

    modal_active = (state.action_menu is not None
                    or state.branch_picker is not None
                    or state.branch_name_prompt is not None
                    or state.detached_recovery_prompt is not None
                    or state.reset_prompt is not None
                    or state.workflow_picker is not None
                    or state.align_heads_prompt is not None
                    or state.task_action_menu is not None
                    or state.workspace_menu is not None
                    or state.workspaces_picker is not None
                    or state.workspace_creator is not None
                    or state.diff_viewer is not None)
    if state.action_menu is not None:
        draw_action_menu(stdscr, state, sidebar_x)
    if state.branch_picker is not None:
        draw_branch_picker(stdscr, state, sidebar_x)
    if state.branch_name_prompt is not None:
        draw_branch_name_prompt(stdscr, state, sidebar_x)
    if state.detached_recovery_prompt is not None:
        draw_detached_recovery_prompt(stdscr, state, sidebar_x)
    if state.reset_prompt is not None:
        draw_reset_prompt(stdscr, state, sidebar_x)
    if state.workflow_picker is not None:
        draw_workflow_picker(stdscr, state, sidebar_x)
    if state.align_heads_prompt is not None:
        draw_align_heads_prompt(stdscr, state, sidebar_x)
    if state.task_action_menu is not None:
        draw_task_action_menu(stdscr, state, sidebar_x)
    if state.workspace_menu is not None:
        draw_workspace_menu(stdscr, state, sidebar_x)
    # Picker drawn before creator so the creator (when both are open)
    # paints on top — common during the "Create new workspace" flow.
    if state.workspaces_picker is not None:
        draw_workspaces_picker(stdscr, state, sidebar_x)
    if state.workspace_creator is not None:
        draw_workspace_creator(stdscr, state, sidebar_x)
    if state.diff_viewer is not None:
        draw_diff_viewer(stdscr, state, sidebar_x)

    # Sidebar drawn LAST so it's always the freshest paint on screen —
    # avoids the resize artifacts where stale cells from the old layout
    # bleed through under the panel.
    if sidebar_w > 0:
        draw_sidebar(stdscr, state, sidebar_x, sidebar_w)

    cursor_set = False
    if not modal_active and state.selected >= 0:
        body_idx = state.selected
        if 0 <= body_idx < len(body_rows) and body_idx in y_for_body:
            row = body_rows[body_idx]
            target = None
            if row[0] == "repo":
                target = row[1] if (row[1].is_dirty or row[1].message) else None
            elif row[0] == "child" and row[2].kind == "submodule":
                target = row[2] if (row[2].dirty or row[2].message) else None
            if target is not None:
                # field_w-1 leaves a single trailing cell as an
                # end-of-field cap; the message itself starts at
                # field_x so the cursor's home is the first
                # character (no inert leading column).
                inner_w = field_w - 1
                cur = max(0, min(state.field_cursor, len(target.message)))
                _, cur_in_visible = field_visible(
                    target.message, cur, inner_w, True)
                cur_x = field_x + cur_in_visible
                cur_y = y_for_body[body_idx]
                # Ask for a "very visible" hardware cursor — without the
                # extra cell-attribute overlay, which produced too much
                # contrast against the reversed-white field.
                try:
                    stdscr.move(cur_y, cur_x)
                    curses.curs_set(2)
                    cursor_set = True
                except curses.error:
                    pass
    if not cursor_set:
        curses.curs_set(0)

    stdscr.refresh()


def draw_state_legend(stdscr, y: int, x: int) -> None:
    items = [
        ("clean", curses.color_pair(PAIR_OK)),
        ("dirty", curses.color_pair(PAIR_DIRTY)),
        ("merging", curses.color_pair(PAIR_ERR)),
        ("ahead", curses.color_pair(PAIR_AHEAD)),
        ("behind", curses.color_pair(PAIR_BEHIND)),
        ("no upstream", curses.A_DIM),
        ("error", curses.color_pair(PAIR_ERR)),
    ]
    cur = x
    for label, attr in items:
        safe_addstr(stdscr, y, cur, "●", attr)
        safe_addstr(stdscr, y, cur + 2, label, curses.A_DIM)
        cur += 2 + len(label) + 2


def draw_repo_row(stdscr, y: int, repo: Repo, focused: bool,
                  name_w: int, branch_w: int, field_x: int, field_w: int,
                  name_max: int, branch_max: int,
                  name_mode: str, branch_mode: str,
                  field_cursor: int = 0,
                  spinner_char: str = " ") -> None:
    name_attr = curses.A_BOLD if focused else 0
    safe_addstr(stdscr, y, 2,
                truncate(repo.display_name, name_max, name_mode).ljust(name_w),
                name_attr)

    branch_str = f"[{truncate(repo.branch, branch_max, branch_mode)}]".ljust(branch_w)
    safe_addstr(stdscr, y, 2 + name_w, branch_str,
                curses.color_pair(PAIR_BRANCH))

    if repo.refreshing:
        safe_addstr(stdscr, y, 2 + name_w + branch_w,
                    f" {spinner_char} ", curses.color_pair(PAIR_BRANCH))
    else:
        _, state_attr = state_color(repo)
        safe_addstr(stdscr, y, 2 + name_w + branch_w, " ● ", state_attr)

    if repo.suggesting and not repo.message:
        inner_w = field_w - 1
        text = (f"{spinner_char} generating…").ljust(inner_w + 1)
        safe_addstr(stdscr, y, field_x, text,
                    curses.color_pair(PAIR_BRANCH) | curses.A_DIM)
    elif repo.is_dirty or repo.message:
        inner_w = field_w - 1
        visible, _ = field_visible(repo.message, field_cursor, inner_w, focused)
        field_text = visible.ljust(inner_w) + " "
        # Outline-only field styling: leaves the terminal background
        # untouched (so the hardware cursor stays readable on both light
        # and dark themes) and relies on a colored underline + the row's
        # focus arrow / bold name to signal which row is active.
        if focused:
            field_attr = (curses.color_pair(PAIR_BRANCH)
                          | curses.A_UNDERLINE | curses.A_BOLD)
        else:
            field_attr = curses.A_UNDERLINE | curses.A_DIM
        safe_addstr(stdscr, y, field_x, field_text, field_attr)


def draw_child_row(stdscr, y: int, child: ChildRef, focused: bool,
                   name_w: int, branch_w: int, field_x: int, field_w: int,
                   name_max: int, branch_max: int,
                   name_mode: str, branch_mode: str,
                   field_cursor: int = 0,
                   spinner_char: str = " ") -> None:
    glyph = "↳" if child.kind == "submodule" else "⊕"
    name_attr = curses.A_BOLD if focused else curses.A_DIM
    # Submodule glyph is a composite "needs your attention?" indicator:
    #   pink   — out of sync vs canonical (drift takes precedence — the
    #            nested checkout is on the wrong commit, fixing that is
    #            the bigger problem)
    #   yellow — in sync with canonical but the working tree is dirty
    #            (uncommitted edits — easy to miss when scanning if the
    #            glyph were green)
    #   green  — in sync AND clean (truly nothing to do)
    # Subtree rows have no canonical relationship, so the glyph stays
    # in the row's normal name attribute.
    if child.kind == "submodule":
        if not child.in_sync:
            glyph_pair = PAIR_BEHIND
        elif child.dirty:
            glyph_pair = PAIR_DIRTY
        else:
            glyph_pair = PAIR_OK
        glyph_attr = curses.color_pair(glyph_pair)
        if focused:
            glyph_attr |= curses.A_BOLD
    else:
        glyph_attr = name_attr
    safe_addstr(stdscr, y, 4, glyph, glyph_attr)
    safe_addstr(stdscr, y, 6,
                truncate(child.repo.display_name, name_max, name_mode),
                name_attr)
    if child.kind == "submodule":
        # Branch label in the same column as parent rows, but a dimmer
        # cyan to keep the visual hierarchy obvious at a glance.
        if child.branch:
            branch_str = (
                f"[{truncate(child.branch, branch_max, branch_mode)}]"
                .ljust(branch_w))
            safe_addstr(stdscr, y, 2 + name_w, branch_str,
                        curses.color_pair(PAIR_BRANCH) | curses.A_DIM)
        # Main state dot — same precedence as a top-level repo. While
        # the child is mid-action / mid-refresh, swap the dot for the
        # global spinner so the row is obviously in-flight instead of
        # carrying a stale state colour.
        if child.refreshing:
            safe_addstr(stdscr, y, 2 + name_w + branch_w,
                        f" {spinner_char} ", curses.color_pair(PAIR_BRANCH))
        else:
            _, state_attr = child_state_color(child)
            safe_addstr(stdscr, y, 2 + name_w + branch_w, " ● ", state_attr)
        if child.suggesting and not child.message:
            inner_w = field_w - 1
            text = (f"{spinner_char} generating…").ljust(inner_w + 1)
            safe_addstr(stdscr, y, field_x, text,
                        curses.color_pair(PAIR_BRANCH) | curses.A_DIM)
        elif child.dirty or child.message:
            inner_w = field_w - 1
            visible, _ = field_visible(
                child.message, field_cursor, inner_w, focused)
            field_text = visible.ljust(inner_w) + " "
            # Outline-only field styling: leaves the terminal background
            # untouched (so the hardware cursor stays readable on both
            # light and dark themes) and relies on a colored underline +
            # the row's focus arrow / bold name to signal active rows.
            if focused:
                field_attr = (curses.color_pair(PAIR_BRANCH)
                              | curses.A_UNDERLINE | curses.A_BOLD)
            else:
                field_attr = curses.A_UNDERLINE | curses.A_DIM
            safe_addstr(stdscr, y, field_x, field_text, field_attr)




# ---------- Confirm screen -------------------------------------------------


def _block_for_repo(state: State, repo: Repo) -> ReviewBlock:
    """Build the per-repo review block for a top-level commit target.
    Picks up its LFS warnings, workflow toggles, and then-run
    selectors — same focusables the old single-list review surfaced
    — so the two-panel layout has all of them grouped under this
    repo's header instead of mixed with other repos'."""
    threshold_mb = state.lfs_warn_bytes // (1024 * 1024)
    block = ReviewBlock(
        label=repo.display_name,
        branch=repo.branch,
        target_path=repo.path,
        target_repo=repo,
        message=repo.message.strip(),
        merging=repo.merging,
        conflict_paths=list(repo.conflict_paths),
        has_origin=bool(repo.remote_url),
        upstream=repo.upstream,
        siblings_summary=", ".join(s[0].display_name for s in repo.siblings),
        auto_stage=state.auto_stage,
        auto_push=state.auto_push,
        threshold_mb=threshold_mb,
    )
    if state.auto_push:
        if repo.upstream:
            block.push_summary = f"push: yes → {repo.upstream}"
        else:
            block.push_summary = (
                f"push: yes (sets upstream → origin/{repo.branch})")
    else:
        block.push_summary = "push: no"
    if not repo.merging:
        warnings = find_lfs_warnings(
            repo, state.auto_stage, state.lfs_warn_bytes)
        for path, size in warnings:
            block.lfs_candidates.append(LFSCandidate(
                repo=repo, path=path, size_str=size))
        if (state.auto_push and gh_available() and repo.workflows
                and parse_github_slug(repo.remote_url_raw)):
            dispatchable_options = [
                w.name for w in repo.workflows
                if w.dispatchable and not w.state.startswith("disabled")
            ]
            for wf in repo.workflows:
                if not would_run_on_push(wf, repo.branch):
                    continue
                if wf.state.startswith("disabled"):
                    continue
                if wf.name not in repo.track_workflow:
                    repo.track_workflow[wf.name] = state.track_actions_default
                block.workflow_toggles.append(WorkflowToggle(
                    repo=repo, workflow_name=wf.name))
                if dispatchable_options:
                    block.then_run_items.append(ThenRunSelector(
                        repo=repo, after_workflow=wf.name))
            if dispatchable_options:
                block.then_run_items.append(ThenRunSelector(
                    repo=repo, after_workflow=""))
    return block


def _block_for_child(state: State,
                     parent: Repo, ref: ChildRef) -> ReviewBlock:
    """Build the per-child review block for a nested submodule
    commit target. Children don't carry their own workflow toggles —
    those live on the canonical's top-level row — so this block is a
    simpler header + message + push-summary shape."""
    label = f"↳ {ref.repo.display_name} in {parent.display_name}"
    block = ReviewBlock(
        label=label,
        branch=ref.branch,
        target_path=ref.nested_path,
        target_parent=parent,
        target_child=ref,
        message=ref.message.strip(),
        is_child=True,
        auto_stage=state.auto_stage,
        auto_push=state.auto_push,
        threshold_mb=state.lfs_warn_bytes // (1024 * 1024),
    )
    if state.auto_push:
        targets = [ref.repo.display_name + " (top-level)"]
        for other_parent, other_path in ref.repo.siblings:
            if other_path != ref.nested_path:
                targets.append(
                    f"{ref.repo.display_name} in {other_parent.display_name}")
        block.siblings_summary = ", ".join(targets)
        block.push_summary = "push: yes (from nested checkout)"
    else:
        block.push_summary = "push: no"
    return block


def build_review_blocks(state: State) -> List[ReviewBlock]:
    """Per-repo / per-child review blocks for the two-panel review
    screen. Top-level repos with a queued message come first (in
    state.repos order), then submodule children (parent-by-parent).
    Empty when nothing has a message — the caller treats that as
    "nothing to review, just bail"."""
    blocks: List[ReviewBlock] = []
    for repo in state.repos:
        if repo.message.strip():
            blocks.append(_block_for_repo(state, repo))
    for parent in state.repos:
        for ref in parent.children:
            if ref.kind == "submodule" and ref.message.strip():
                blocks.append(_block_for_child(state, parent, ref))
    return blocks


def kick_off_review_files_load(blocks: List[ReviewBlock]) -> None:
    """Spawn one daemon thread per block to populate `block.files`
    via `query_working_tree`. Non-blocking — the review screen draws
    immediately with `files_loading=True` placeholders, and each pane
    fills in as its worker completes. Each worker checks
    `block.cancel_event` before mutating so closing the review
    mid-load drops the result on the floor."""
    import threading

    def loader(block: ReviewBlock) -> None:
        try:
            if block.cancel_event.is_set():
                return
            files: List[FileEntry] = query_working_tree(block.target_path)
            if block.cancel_event.is_set():
                return
            block.files = files
        finally:
            block.files_loading = False

    for block in blocks:
        threading.Thread(target=loader, args=(block,), daemon=True).start()


def _then_run_options(repo: Repo) -> List[str]:
    """Workflow names eligible as 'then run' targets for this repo —
    dispatchable + not disabled-on-github. Returned in the same order
    as repo.workflows so left/right cycling stays stable."""
    return [w.name for w in repo.workflows
            if w.dispatchable and not w.state.startswith("disabled")]


def _then_run_current(selector: ThenRunSelector) -> str:
    """Read the current then-run selection from the repo's memory dict."""
    if selector.after_workflow:
        return selector.repo.then_run_after_workflow.get(
            selector.after_workflow, "")
    return selector.repo.then_run_after_push


def _then_run_set(selector: ThenRunSelector, value: str) -> None:
    """Persist a then-run selection. Empty string means '(none)'."""
    if selector.after_workflow:
        if value:
            selector.repo.then_run_after_workflow[
                selector.after_workflow] = value
        else:
            selector.repo.then_run_after_workflow.pop(
                selector.after_workflow, None)
    else:
        selector.repo.then_run_after_push = value


def cycle_then_run(selector: ThenRunSelector, direction: int) -> None:
    """Cycle the selector's choice through the repo's dispatchable
    workflows + a '(none)' slot. `direction` is +1 (right arrow) or
    -1 (left arrow)."""
    options = _then_run_options(selector.repo)
    if not options:
        _then_run_set(selector, "")
        return
    wheel = [""] + options
    current = _then_run_current(selector)
    try:
        i = wheel.index(current)
    except ValueError:
        i = 0
    i = (i + direction) % len(wheel)
    _then_run_set(selector, wheel[i])


# ---------- Two-panel review screen --------------------------------------


def _file_status_pair(x: str, y: str, pane_focused: bool = False) -> Optional[int]:
    """Map an XY porcelain status pair to a pastel colour pair,
    matching the action-menu's tree pane (delete > add > rename >
    modify). Returns None for plain rows that don't need an overlay."""
    pair = (x, y)
    if "U" in pair or pair == ("A", "A") or pair == ("D", "D"):
        return PAIR_PASTEL_RED_ACTIVE if pane_focused else PAIR_PASTEL_RED
    if "D" in pair:
        return PAIR_PASTEL_RED_ACTIVE if pane_focused else PAIR_PASTEL_RED
    if "A" in pair:
        return PAIR_PASTEL_GREEN_ACTIVE if pane_focused else PAIR_PASTEL_GREEN
    if "R" in pair:
        return PAIR_PASTEL_BLUE_ACTIVE if pane_focused else PAIR_PASTEL_BLUE
    if "M" in pair:
        return PAIR_PASTEL_YELLOW_ACTIVE if pane_focused else PAIR_PASTEL_YELLOW
    return None


def _review_spinner(state: State) -> str:
    """Same spinner glyph the sidebar / action-menu animations use,
    so every animated indicator on screen ticks in lockstep."""
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


def _collect_review_focusables(
    blocks: List[ReviewBlock],
) -> List[Tuple[int, str, object]]:
    """Flatten every block's interactive items into one ordered list
    of `(block_idx, kind, item)`. `kind` is "lfs", "toggle", or
    "then_run". Up/Down on the left pane navigates this list;
    `block_idx` says which block's files the right pane should
    show. The list intentionally omits headers / message / push
    summary lines — those are display-only context."""
    out: List[Tuple[int, str, object]] = []
    for bi, block in enumerate(blocks):
        for c in block.lfs_candidates:
            out.append((bi, "lfs", c))
        for tog in block.workflow_toggles:
            out.append((bi, "toggle", tog))
        for sel in block.then_run_items:
            out.append((bi, "then_run", sel))
    return out


def _focused_block_idx(focusables: List[Tuple[int, str, object]],
                       focus: int, default: int = 0) -> int:
    if focus < 0 or focus >= len(focusables):
        return default
    return focusables[focus][0]


def _word_wrap(text: str, first_w: int, cont_w: int) -> List[str]:
    """Greedy word-wrap, breaking on whitespace. Words longer than a
    row are hard-broken at the row boundary so a 200-char URL doesn't
    silently truncate. Returns the list of wrapped lines, each at
    most `first_w` (line 0) or `cont_w` (lines 1+) chars wide."""
    if not text:
        return []
    if first_w <= 0:
        first_w = 1
    if cont_w <= 0:
        cont_w = 1
    words = text.split(" ")
    lines: List[str] = []
    current = ""
    cap = first_w
    for w in words:
        # Hard-break a word that's longer than the available width.
        while len(w) > cap:
            if current:
                lines.append(current)
                current = ""
                cap = cont_w
            lines.append(w[:cap])
            w = w[cap:]
        candidate = w if not current else current + " " + w
        if len(candidate) <= cap:
            current = candidate
        else:
            lines.append(current)
            current = w
            cap = cont_w
    if current:
        lines.append(current)
    return lines


def _wrap_message_lines(message: str, cap: int, max_w: int
                        ) -> List[str]:
    """Lay out a commit message across as many rows as needed for
    the review screen's left pane. End-truncates the FULL message
    when `cap > 0` and `len(message) > cap` (cap=0 disables the
    cap entirely). Continuation lines align under the opening
    quote; the closing quote sits on the last line, on its own
    line if the last chunk would otherwise overflow `max_w`."""
    if cap > 0 and len(message) > cap:
        message = message[: max(0, cap - 1)] + "…"
    if not message:
        return ['  message: ""']
    prefix = '  message: "'
    cont_indent = " " * len(prefix)
    text = message.replace("\n", " ").replace("\r", "")
    first_w = max(1, max_w - len(prefix))
    cont_w = max(1, max_w - len(cont_indent))
    chunks = _word_wrap(text, first_w, cont_w)
    if not chunks:
        return [prefix + '"']
    lines = [prefix + chunks[0]]
    for chunk in chunks[1:]:
        lines.append(cont_indent + chunk)
    last = lines[-1]
    if len(last) + 1 > max_w:
        # Pushing the closing quote here would overflow — drop it on
        # its own indented row instead. Reads as "open quote, body,
        # close quote on its own line".
        lines.append(cont_indent + '"')
    else:
        lines[-1] = last + '"'
    return lines


def _block_left_rows(
    block: ReviewBlock,
    focusables: List[Tuple[int, str, object]],
    focus: int, panel_focus: str, block_idx: int,
    inner_w: int, message_cap: int,
) -> List[Tuple[str, int, bool]]:
    """Build the (text, attr, is_focused) tuples for ONE block on
    the left pane. Focus highlighting only kicks in when the left
    pane has the active focus — when the user has Shift+Tab'd over
    to the right pane, the rows render in their resting style so
    both panels can't claim focus at the same time. `inner_w` is
    the available pane width used to wrap multi-line content (the
    commit message); `message_cap` end-truncates the full message
    before wrapping (0 disables)."""
    rows: List[Tuple[str, int, bool]] = []
    header_attr = curses.A_BOLD | curses.color_pair(PAIR_BRANCH)
    rows.append((f"{block.label}  [{block.branch}]", header_attr, False))

    if block.merging:
        rows.append((
            "  ⚠ merge / rebase in progress — commit will be skipped",
            curses.color_pair(PAIR_ERR), False))
        for cp in block.conflict_paths:
            rows.append((f"      {cp}",
                         curses.color_pair(PAIR_ERR), False))
        return rows

    if block.message:
        for line in _wrap_message_lines(block.message, message_cap, inner_w):
            rows.append((line, 0, False))
    push_line = f"  {block.push_summary}"
    arrow = push_line.rfind("→ ")
    if arrow != -1 and "yes" in push_line:
        val_attr = curses.color_pair(PAIR_BRANCH) | curses.A_DIM
        rows.append(([
            (push_line[:arrow + 2], curses.A_DIM),
            (push_line[arrow + 2:], val_attr),
        ], curses.A_DIM, False))
    else:
        rows.append((push_line, curses.A_DIM, False))
    if block.siblings_summary:
        val_attr = curses.color_pair(PAIR_BRANCH) | curses.A_DIM
        rows.append(([
            ("  sync: ", curses.A_DIM),
            (block.siblings_summary, val_attr),
        ], curses.A_DIM, False))

    if block.lfs_candidates:
        rows.append((
            f"  ⚠ files ≥{block.threshold_mb} MB not LFS-tracked — "
            "push will fail:",
            curses.color_pair(PAIR_ERR), False))
        for cand in block.lfs_candidates:
            is_focused = (panel_focus == "left" and focus >= 0
                          and focusables[focus] == (block_idx, "lfs", cand))
            check = "[x]" if cand.track else "[ ]"
            text = f"      {check}  {cand.path}  ({cand.size_str})"
            base = PAIR_OK if cand.track else PAIR_ERR
            attr = curses.color_pair(base)
            if is_focused:
                attr |= curses.A_REVERSE
            rows.append((text, attr, is_focused))

    def append_then_run(sel) -> None:
        is_focused = (panel_focus == "left" and focus >= 0
                      and focusables[focus] == (block_idx, "then_run", sel))
        indent = "        " if sel.after_workflow else "  "
        label = "then run:" if sel.after_workflow else "then run after push:"
        current = _then_run_current(sel) or "(none)"
        text = f"{indent}{label} ‹ {current} ›"
        if is_focused:
            attr = curses.color_pair(PAIR_BRANCH) | curses.A_BOLD
        else:
            attr = curses.A_DIM
        rows.append((text, attr, is_focused))

    then_runs_by_wf = {
        sel.after_workflow: sel
        for sel in block.then_run_items if sel.after_workflow
    }
    for tog in block.workflow_toggles:
        is_focused = (panel_focus == "left" and focus >= 0
                      and focusables[focus] == (block_idx, "toggle", tog))
        on = tog.repo.track_workflow.get(tog.workflow_name, False)
        check = "[x]" if on else "[ ]"
        text = f"  {check}  track action: {tog.workflow_name}"
        if on:
            attr = curses.color_pair(PAIR_OK)
        else:
            attr = curses.color_pair(PAIR_HEADER) | curses.A_DIM
        if is_focused:
            attr |= curses.A_REVERSE
        rows.append((text, attr, is_focused))
        sel = then_runs_by_wf.get(tog.workflow_name)
        if sel is not None:
            append_then_run(sel)

    for sel in block.then_run_items:
        if not sel.after_workflow:
            append_then_run(sel)
    return rows


def _build_left_pane_rows(
    blocks: List[ReviewBlock],
    focusables: List[Tuple[int, str, object]],
    focus: int, panel_focus: str, inner_w: int,
    message_cap: int,
) -> Tuple[List[Tuple[str, int]], int]:
    """Concatenate every block's rows into one flat (text, attr)
    list, with subtle divider lines between blocks. Returns
    (rows, focused_row_index) — the second value tells the caller
    which row index to keep visible when adjusting scroll.

    `inner_w` is the available pane width (commit messages wrap to
    fit it); `message_cap` end-truncates the full message before
    wrapping (0 disables the cap)."""
    rows: List[Tuple[str, int]] = []
    focused_row_idx = -1
    for bi, block in enumerate(blocks):
        block_rows = _block_left_rows(
            block, focusables, focus, panel_focus, bi,
            inner_w, message_cap)
        for text, attr, is_focused in block_rows:
            if is_focused:
                focused_row_idx = len(rows)
            rows.append((text, attr))
        if bi < len(blocks) - 1:
            rows.append(("─" * max(1, inner_w - 2), curses.A_DIM))
    return rows, focused_row_idx


def _draw_left_pane(stdscr, x: int, y: int, w: int, h: int,
                    rows: List[Tuple[str, int]], scroll: int) -> None:
    for i in range(h):
        idx = scroll + i
        if idx >= len(rows):
            break
        text, attr = rows[idx]
        if isinstance(text, list):
            cx = x
            for seg_text, seg_attr in text:
                avail = max(0, w - (cx - x))
                if avail <= 0:
                    break
                safe_addstr(stdscr, y + i, cx, seg_text[:avail], seg_attr)
                cx += len(seg_text)
        else:
            safe_addstr(stdscr, y + i, x, text[:w], attr)


def _render_review_file_row(stdscr, y: int, x: int, w: int,
                            fe: FileEntry, focused: bool,
                            pane_focused: bool = False) -> None:
    """Same shape as the action-menu's tree-row renderer — pastel
    overlay on the status code, green/red on the +ins/-del numbers."""
    p_green = PAIR_PASTEL_GREEN_ACTIVE if pane_focused else PAIR_PASTEL_GREEN
    p_red   = PAIR_PASTEL_RED_ACTIVE   if pane_focused else PAIR_PASTEL_RED
    code = "??" if fe.untracked else f"{fe.x}{fe.y}"
    stat_ins = f"+{fe.inserted}" if (fe.inserted or fe.deleted) else ""
    stat_del = f"-{fe.deleted}" if (fe.inserted or fe.deleted) else ""
    stat = f"{stat_ins} {stat_del}".strip()
    left = f" {code}  "
    pad = max(1, w - len(left) - len(stat) - 1)
    name = fe.path
    if len(name) > pad:
        name = name[: pad - 1] + "…"
    name = name.ljust(pad)
    full = f"{left}{name} {stat}"
    fill_attr = curses.color_pair(
        PAIR_SB_FG_ACTIVE if pane_focused else PAIR_SB_FG)
    if focused:
        safe_addstr(stdscr, y, x, full, fill_attr | curses.A_REVERSE)
        return
    base = fill_attr | curses.A_DIM if fe.untracked else fill_attr
    safe_addstr(stdscr, y, x, full, base)
    if not fe.untracked:
        pair_id = _file_status_pair(fe.x, fe.y, pane_focused)
        if pair_id is not None:
            safe_addstr(stdscr, y, x + 1, code, curses.color_pair(pair_id))
    if stat:
        stat_x = x + len(left) + pad + 1
        safe_addstr(stdscr, y, stat_x, stat_ins,
                    curses.color_pair(p_green))
        safe_addstr(stdscr, y, stat_x + len(stat_ins) + 1, stat_del,
                    curses.color_pair(p_red))


def _draw_right_pane(stdscr, x: int, y: int, w: int, h: int,
                     block: Optional[ReviewBlock],
                     panel_focus: str, state: State) -> None:
    """Right pane = working-tree files for the focused block. Header
    accents bright when the right pane has focus, dims otherwise so
    the user can see at a glance which side ↑/↓ steers."""
    if block is None or w <= 0 or h <= 0:
        return
    pane_focused = panel_focus == "right"
    fill_pair = PAIR_SB_FG_ACTIVE if pane_focused else PAIR_SB_FG
    fill_attr = curses.color_pair(fill_pair)
    dim_attr = fill_attr | curses.A_DIM
    fill = " " * w
    scr_h, _ = stdscr.getmaxyx()
    for fy in range(y, min(y + h, scr_h)):
        safe_addstr(stdscr, fy, x, fill, fill_attr)
    if pane_focused:
        header_attr = curses.color_pair(PAIR_SB_CYAN_ACTIVE) | curses.A_BOLD
    else:
        header_attr = fill_attr | curses.A_BOLD | curses.A_DIM
    if block.files_loading and not block.files:
        count_str = _review_spinner(state)
    else:
        count_str = str(len(block.files))
    header = f"{block.label}: {count_str} file(s)"
    safe_addstr(stdscr, y, x, header[:w], header_attr)

    line = y + 2
    list_h = max(0, h - (line - y))
    if list_h <= 0:
        return

    if block.files_loading and not block.files:
        safe_addstr(stdscr, line, x + 2,
                    f"{_review_spinner(state)} loading files…",
                    dim_attr)
        return
    if not block.files:
        safe_addstr(stdscr, line, x + 2, "(no changes)", dim_attr)
        return

    sel = block.file_selected
    if sel < block.file_scroll:
        block.file_scroll = sel
    elif sel >= block.file_scroll + list_h:
        block.file_scroll = sel - list_h + 1
    block.file_scroll = max(0, min(
        block.file_scroll, max(0, len(block.files) - list_h)))

    for slot in range(list_h):
        idx = block.file_scroll + slot
        if idx >= len(block.files):
            break
        fe = block.files[idx]
        focused = pane_focused and idx == sel
        _render_review_file_row(stdscr, line + slot, x, w, fe, focused,
                                pane_focused)
    if block.file_scroll > 0:
        msg = f"  ↑ {block.file_scroll} more above"
        safe_addstr(stdscr, line, x + max(0, w - len(msg) - 1),
                    msg, dim_attr)
    end = min(len(block.files), block.file_scroll + list_h)
    if end < len(block.files):
        below = len(block.files) - end
        msg = f"  ↓ {below} more below"
        safe_addstr(stdscr, line + list_h - 1, x + max(0, w - len(msg) - 1),
                    msg, dim_attr)


def _review_hints(focusables: List[Tuple[int, str, object]],
                  focus: int, panel_focus: str) -> List[Hint]:
    hints: List[Hint] = []
    if panel_focus == "left":
        hints.append(Hint(KEY_UP_DOWN, "select"))
        if 0 <= focus < len(focusables):
            _, kind, obj = focusables[focus]
            if kind == "lfs":
                hints.append(Hint(
                    KEY_SPACE,
                    "stop tracking" if obj.track else "track with LFS"))
            elif kind == "toggle":
                on = obj.repo.track_workflow.get(obj.workflow_name, False)
                hints.append(Hint(
                    KEY_SPACE,
                    "untrack workflow" if on else "track workflow"))
            else:  # then_run
                hints.append(Hint(KEY_LEFT_RIGHT,
                                  "cycle then-run target"))
        hints.append(Hint(KEY_SHIFT_TAB, "files panel"))
        hints.append(Hint(KEY_ENTER, "execute commits"))
    else:  # right
        hints.append(Hint(KEY_UP_DOWN, "select file"))
        hints.append(Hint(KEY_TAB, "view diff"))
        hints.append(Hint(KEY_SHIFT_TAB, "back to repos"))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


def draw_review(stdscr, state: State, blocks: List[ReviewBlock],
                focusables: List[Tuple[int, str, object]],
                focus: int, panel_focus: str,
                scroll: int) -> int:
    """Draw the two-panel review screen and return the (clamped)
    left-pane scroll the caller should keep going forward.

    Layout:
        ┌─ Review · N targets · auto-stage on · auto-push on ─┐
        │                                                       │
        │  block A header   │   block A files (header)          │
        │  ...              │   working-tree rows               │
        │  ── divider ──    │                                   │
        │  block B header   │                                   │
        │  ...              │                                   │
        │                                                       │
        │  hint line                                            │
        └───────────────────────────────────────────────────────┘
    """
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    # Workspace title bar — same shape as the main screen's row 0
    # (`idlegit · <workspace name>`), MINUS the focus chevrons. The
    # review screen's workspace selector isn't navigable, just a
    # label, so the chevrons would be misleading.
    safe_addstr(stdscr, 0, 0, "idlegit",
                curses.A_BOLD | curses.color_pair(PAIR_HEADER))
    if state.workspace_name:
        safe_addstr(stdscr, 0, len("idlegit"), " · ", curses.A_DIM)
        ws_attr = curses.A_BOLD | curses.color_pair(PAIR_BRANCH)
        safe_addstr(stdscr, 0, len("idlegit") + 3,
                    state.workspace_name, ws_attr)

    body_top = 4
    body_h = max(1, h - body_top - 2)
    left_w = max(40, int(w * 0.55))
    if left_w >= w - 12:
        left_w = max(20, w - 12)
    right_x = left_w + 1
    right_w = max(10, w - right_x - 1)

    # Panel title row — "Review" on the left, "Changes" on the right,
    # each cyan when its pane has focus and dim when it doesn't, matching
    # the "Repositories" / "Tasks" header treatment on the main screen.
    left_focused = panel_focus == "left"
    right_focused = panel_focus == "right"
    left_title_attr = (curses.color_pair(PAIR_BRANCH) | curses.A_BOLD
                       if left_focused else curses.A_DIM | curses.A_BOLD)
    right_title_attr = (curses.color_pair(PAIR_BRANCH) | curses.A_BOLD
                        if right_focused else curses.A_DIM | curses.A_BOLD)
    safe_addstr(stdscr, 2, 0, "Review", left_title_attr)
    sub = (f"{len(blocks)} target(s)  ·  "
           f"auto-stage: {'on' if state.auto_stage else 'off'}  ·  "
           f"auto-push: {'on' if state.auto_push else 'off'}")
    safe_addstr(stdscr, 2, len("Review") + 3, sub, curses.A_DIM)
    safe_addstr(stdscr, 2, right_x + 1, "Changes", right_title_attr)

    rows, focused_row = _build_left_pane_rows(
        blocks, focusables, focus, panel_focus, left_w,
        state.max_commit_message_length_in_review)
    if focused_row >= 0:
        if focused_row < scroll:
            scroll = focused_row
        elif focused_row >= scroll + body_h:
            scroll = focused_row - body_h + 1
    max_scroll = max(0, len(rows) - body_h)
    scroll = max(0, min(scroll, max_scroll))
    _draw_left_pane(stdscr, 0, body_top, left_w, body_h, rows, scroll)

    for row in range(body_h):
        safe_addstr(stdscr, body_top + row, left_w, "│", curses.A_DIM)

    block = blocks[_focused_block_idx(focusables, focus)] if blocks else None
    _draw_right_pane(stdscr, right_x + 1, body_top, right_w, body_h,
                     block, panel_focus, state)

    render_hints(stdscr, h - 1, 0, max(0, w - 1),
                 _review_hints(focusables, focus, panel_focus),
                 attr=curses.A_DIM)
    curses.curs_set(0)
    stdscr.refresh()
    return scroll





# ---------- Main key handler ----------------------------------------------


def _focused_message_holder(state: State):
    """Return the Repo or ChildRef whose message field is currently
    editable, or None for the workspace row, subtree rows, or any
    other non-editable focus."""
    if state.on_workspace_row:
        return None
    if state.current_repo is not None:
        return state.current_repo
    cur_child = state.current_child
    if cur_child is not None and cur_child[1].kind == "submodule":
        return cur_child[1]
    return None


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

    if key in (18, curses.KEY_F5):
        return "refresh"
    if key == 19:
        return "sync"

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
    from workers import switch_workspace
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

    if key in (18, curses.KEY_F5):  # Ctrl+R or F5 — refresh state, prune tasks
        return "refresh"
    if key == 19:  # Ctrl+S — fetch + checkout every tracked sibling
        return "sync"

    # Workspace title-row navigation. `selected = -1` is a sentinel for
    # "the workspace selector on the title row"; Up from the top body
    # row lands here, Down from here returns to the first body row.
    # ←/→ cycles workspaces; Space/Enter opens the workspace-overrides
    # modal.
    if state.on_workspace_row:
        if key == curses.KEY_UP:
            # Wrap to the bottom of the body (preserves the existing
            # "Up from the top wraps to the last row" feel).
            state.selected = max(-1, state.total_rows - 1)
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
        if key == 9:  # Tab — opens the workspaces picker
            open_workspaces_picker(state)
            return None
        if key in (ord(" "), 10, 13, curses.KEY_ENTER):
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

    target_message_holder = _focused_message_holder(state)

    if key == 27:
        if target_message_holder is not None and target_message_holder.message:
            target_message_holder.message = ""
            state.field_cursor = 0
            return None
        return "confirm-quit" if state.has_messages else "quit"

    if target_message_holder is None:
        return None  # subtree row or otherwise non-editable

    msg = target_message_holder.message
    cur = max(0, min(state.field_cursor, len(msg)))

    if key == curses.KEY_LEFT:
        if not msg:
            kick_off_suggest_for(state, target_message_holder)
            return None
        state.field_cursor = max(0, cur - 1)
        return None
    if key == curses.KEY_SLEFT and not msg:
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
            key = stdscr.getch()
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
    from git_ops import git
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
            key = stdscr.getch()
        except KeyboardInterrupt:
            setattr(state, slot, None)
            return False
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
    focus = 0 if focusables else -1
    panel_focus = "left"
    scroll = 0

    try:
        while True:
            anim = any(b.files_loading for b in blocks) or (
                state.diff_viewer is not None and state.diff_viewer.loading)
            stdscr.timeout(100 if anim else 1000)
            scroll = draw_review(stdscr, state, blocks, focusables,
                                 focus, panel_focus, scroll)
            if state.diff_viewer is not None:
                draw_diff_viewer(stdscr, state)
                stdscr.refresh()
            try:
                key = stdscr.getch()
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
            if key in (10, 13, curses.KEY_ENTER):
                if panel_focus == "left":
                    cands = [c for b in blocks for c in b.lfs_candidates]
                    kick_off_workers(state, cands)
                    return  # async pipeline takes over the sidebar
                continue
            if key == 9:  # Tab — open diff viewer on the focused file row
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
                if key == ord(" ") and 0 <= focus < len(focusables):
                    _, kind, obj = focusables[focus]
                    if kind == "lfs":
                        obj.track = not obj.track
                    elif kind == "toggle":
                        on = obj.repo.track_workflow.get(
                            obj.workflow_name, False)
                        obj.repo.track_workflow[obj.workflow_name] = not on
                    # Space on a then-run row is a no-op (use ←/→).
                    continue
                if (key in (curses.KEY_LEFT, curses.KEY_RIGHT)
                        and 0 <= focus < len(focusables)):
                    _, kind, obj = focusables[focus]
                    if kind == "then_run":
                        cycle_then_run(
                            obj, -1 if key == curses.KEY_LEFT else 1)
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
                    if key == curses.KEY_UP and block.file_selected > 0:
                        block.file_selected -= 1
                    elif (key == curses.KEY_DOWN
                            and block.file_selected < n - 1):
                        block.file_selected += 1
    finally:
        for b in blocks:
            b.cancel_event.set()


# ---------- Initial empty-repo screen helper ------------------------------


def show_no_repos_message(stdscr, workspace: Path) -> None:
    """Used at startup if discovery finds no git repos under workspace."""
    safe_addstr(stdscr, 0, 0,
                f"no git repos found under {workspace}",
                curses.color_pair(PAIR_ERR))
    safe_addstr(stdscr, 2, 0,
                f"edit {CONFIG_FILE.name} to point at a different root, then re-run.",
                curses.A_DIM)
    stdscr.refresh()
    stdscr.timeout(-1)
    stdscr.getch()
