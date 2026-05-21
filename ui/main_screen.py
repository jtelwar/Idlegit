"""Main full-screen layout — workspace header, repo list, legend, modals, sidebar."""
from __future__ import annotations

import curses
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.models import ChildRef, Repo, State
from core.config import APP_DISPLAY_NAME, VERSION, WORKSPACES_FILE
from .colors import (
    PAIR_AHEAD, PAIR_BEHIND, PAIR_BRANCH, PAIR_DIRTY, PAIR_ERR, PAIR_HEADER,
    PAIR_OK, child_state_color, state_color,
)
from .geometry import (
    clamp_scroll, draw_scroll_overflow, field_visible, safe_addstr, truncate,
)
from .hints import (
    KEY_CTRL_P, KEY_CTRL_R, KEY_CTRL_S, KEY_ENTER, KEY_ESC, KEY_LEFT,
    KEY_LEFT_RIGHT, KEY_RIGHT, KEY_SHIFT_TAB, KEY_TAB, KEY_UP_DOWN, Hint,
    render_hints,
)
from .modals import (
    draw_action_menu, draw_align_heads_prompt, draw_branch_name_prompt,
    draw_branch_picker, draw_remote_branch_picker,
    draw_clone_modal, draw_commit_msg_editor,
    draw_commit_view_modal, draw_detached_recovery_prompt,
    draw_diff_viewer, draw_help_screen, draw_remotes_modal, draw_reset_prompt,
    draw_ssh_keygen_modal,
    draw_task_action_menu, draw_workflow_picker,
    draw_workspace_creator, draw_workspace_menu, draw_app_menu,
    draw_workspace_switcher,
)
from .sidebar import SPINNER_FRAMES, draw_sidebar

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
    state.body_scroll = clamp_scroll(body_idx, state.body_scroll,
                                     total_body, body_h)


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


def _focused_message_holder(state: State):
    """Return the Repo or ChildRef whose message field is currently
    editable, or None for the title / workspace pseudo-rows, subtree
    rows, or any other non-editable focus."""
    if state.on_title_row or state.on_workspace_row:
        return None
    if state.current_repo is not None:
        return state.current_repo
    cur_child = state.current_child
    if cur_child is not None and cur_child[1].kind == "submodule":
        return cur_child[1]
    return None


# ---------- Main-screen hints registry -----------------------------------


def _esc_hint(state: State) -> Hint:
    """Esc means three different things on the main screen — pick the
    one that's actually about to fire so the footer doesn't lie."""
    if state.focused_panel == "tasks":
        return Hint(KEY_ESC, "back to repos")
    holder = _focused_message_holder(state)
    if holder is not None and holder.message:
        return Hint(KEY_ESC, "clear msg")
    if state.has_messages:
        return Hint(KEY_ESC, "discard + quit")
    return Hint(KEY_ESC, "quit")


def _title_row_hints(state: State) -> List[Hint]:
    """Hints for the Idlegit title row — Enter opens the app menu
    (matches the row's underline affordance — Tab works too but
    isn't advertised, since one canonical hint per action keeps
    the status line readable). Up/Down navigate."""
    hints = [Hint(KEY_UP_DOWN, "navigate")]
    hints.append(Hint(KEY_ENTER, "menu…"))
    return hints


def _workspace_row_hints(state: State) -> List[Hint]:
    hints = [Hint(KEY_UP_DOWN, "navigate")]
    if len(state.workspaces) > 1:
        hints.append(Hint(KEY_LEFT_RIGHT, "cycle workspaces"))
    hints.append(Hint(KEY_ENTER, "switch workspace…"))
    hints.append(Hint(KEY_TAB, "settings…"))
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
        # Editable row: typing edits the message inline. Suggest /
        # suggest-all only make sense on an empty field; the larger
        # editor is always available (compose-from-scratch when empty,
        # edit-in-place when there's content). Both Shift hints are
        # gated to `holder is not None` so they never appear on a
        # subtree row or a clean repo with no message holder.
        if not holder.message:
            hints.append(Hint(KEY_LEFT, "suggest"))
            hints.append(Hint(f"Shift+{KEY_LEFT}", "suggest all"))
        hints.append(Hint(f"Shift+{KEY_RIGHT}", "edit msg"))
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
    if state.on_title_row:
        return _title_row_hints(state)
    if state.on_workspace_row:
        return _workspace_row_hints(state)
    return _body_row_hints(state)


def _main_hints_global(state: State) -> List[Hint]:
    """Second footer line — always-applicable shortcuts. Shift+Tab,
    Ctrl+R / Ctrl+S, and the context-aware Esc, in that order."""
    if state.focused_panel == "tasks":
        panel_hint = Hint(KEY_SHIFT_TAB, "<- repos")
    else:
        panel_hint = Hint(KEY_SHIFT_TAB, "-> tasks")
    return [
        panel_hint,
        Hint(KEY_CTRL_R, "refresh"),
        Hint(KEY_CTRL_S, "smart-sync"),
        Hint(KEY_CTRL_P, "pull all"),
        _esc_hint(state),
    ]


def draw_main(stdscr, state: State) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    body_h = _body_height_for(state, h)

    # Row 0 — Idlegit title (selectable; Enter or Tab opens the app
    # menu) followed by a muted version label where the workspace
    # name used to live. Focus is signalled with an underline rather
    # than a colour shift: every "shade brighter than bold magenta"
    # in the xterm-256 palette ends up reading as pink/orchid on
    # the terminals we tested, so the underline keeps the hue
    # consistent and the focus state still unambiguous. No chevrons
    # here — those are reserved for ←/→ switchers like the workspace
    # selector below.
    title_focused = (state.on_title_row
                     and state.focused_panel == "repos")
    title_attr = curses.A_BOLD | curses.color_pair(PAIR_HEADER)
    if title_focused:
        title_attr |= curses.A_UNDERLINE
    safe_addstr(stdscr, 0, 0, APP_DISPLAY_NAME, title_attr)
    x = len(APP_DISPLAY_NAME)
    safe_addstr(stdscr, 0, x, " · ", curses.A_DIM)
    x += 3
    safe_addstr(stdscr, 0, x, f"v{VERSION}",
                curses.color_pair(PAIR_BRANCH) | curses.A_DIM)

    toggle_y = 2
    # Row 2 — workspace switcher (Enter opens the workspace picker).
    # The name is anchored at column 2 regardless of focus so the row
    # never jumps as focus moves; the dim "‹ X ›" chevrons that signal
    # ←/→ cycling slot into the reserved column-0 / trailing space
    # only when this row has focus.
    #
    # Color carries a second signal: cyan when the repos panel owns
    # focus, dim grey when the user has Shift+Tab'd over to the tasks
    # panel — that way the cyan accent always tracks the active pane,
    # matching how the Tasks-panel header in the sidebar lights up.
    if state.workspace_name:
        repos_panel_active = state.focused_panel == "repos"
        ws_focused = state.on_workspace_row and repos_panel_active
        name_x = 2
        if ws_focused:
            safe_addstr(stdscr, toggle_y, 0, "‹ ", curses.A_DIM)
        if repos_panel_active:
            ws_attr = curses.A_BOLD | curses.color_pair(PAIR_BRANCH)
        else:
            ws_attr = curses.A_BOLD | curses.A_DIM
        safe_addstr(stdscr, toggle_y, name_x, state.workspace_name, ws_attr)
        end_x = name_x + len(state.workspace_name)
        if ws_focused:
            safe_addstr(stdscr, toggle_y, end_x, " ›", curses.A_DIM)

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
    # Empty-repo fallback: keep the name column wide enough to fit the
    # workspace name (which now lives on row 2 where "Repositories"
    # used to). Prevents a visual collapse when state.repos is empty.
    name_lengths = [len(truncate(r.display_name, nm, nmode))
                    for r in state.repos] or [
                        max(8, len(state.workspace_name))]
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
    _main_tasks_gap = 1
    field_w, sidebar_w = _split_remaining_width(
        max(0, remaining_w - _main_tasks_gap),
        state.tasks_min_width_percent,
        state.tasks_max_width_percent)
    sidebar_x = field_x + field_w + _main_tasks_gap
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
        draw_scroll_overflow(stdscr, base_y - 1, 2, main_w - 2,
                             visible_start, "up", curses.A_DIM)
    if visible_end < len(body_rows):
        below = len(body_rows) - visible_end
        draw_scroll_overflow(stdscr, base_y + body_h, 2, main_w - 2,
                             below, "down", curses.A_DIM)

    # Subtle focus marker at column 0 of the active body row. Skipped
    # on the workspace row (selected = -1) since the chevrons around
    # the workspace name already advertise focus and an extra glyph at
    # column 0 would clobber the first letter of the app title. Also skipped when
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
                    or state.remote_branch_picker is not None
                    or state.branch_name_prompt is not None
                    or state.detached_recovery_prompt is not None
                    or state.reset_prompt is not None
                    or state.workflow_picker is not None
                    or state.align_heads_prompt is not None
                    or state.task_action_menu is not None
                    or state.workspace_menu is not None
                    or state.app_menu is not None
                    or state.workspace_creator is not None
                    or state.diff_viewer is not None
                    or state.remotes_modal is not None
                    or state.clone_modal is not None
                    or state.commit_view_modal is not None
                    or state.commit_msg_editor is not None
                    or state.help_screen is not None
                    or state.ssh_keygen_modal is not None
                    or state.workspace_switcher is not None)
    if state.action_menu is not None:
        draw_action_menu(stdscr, state, sidebar_x)
    if state.branch_picker is not None:
        draw_branch_picker(stdscr, state, sidebar_x)
    if state.remote_branch_picker is not None:
        draw_remote_branch_picker(stdscr, state, sidebar_x)
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
    if state.app_menu is not None:
        draw_app_menu(stdscr, state, sidebar_x)
    if state.workspace_switcher is not None:
        draw_workspace_switcher(stdscr, state, sidebar_x)
    if state.workspace_creator is not None:
        draw_workspace_creator(stdscr, state, sidebar_x)
    # Sub-modals of action_menu (remotes, commit view) and
    # workspace_menu (clone) paint last so they layer on top of
    # their parents. The commit view goes above remotes/clone since
    # it can be opened on top of the action menu's commits pane.
    if state.remotes_modal is not None:
        draw_remotes_modal(stdscr, state, sidebar_x)
    if state.clone_modal is not None:
        draw_clone_modal(stdscr, state, sidebar_x)
    if state.commit_view_modal is not None:
        draw_commit_view_modal(stdscr, state, sidebar_x)
    if state.diff_viewer is not None:
        draw_diff_viewer(stdscr, state, sidebar_x)
    # Commit-message editor paints last so it sits on top of every
    # other modal. It's typically opened from the bare main screen,
    # but layering it on top means a stray open while another modal
    # is up doesn't render half-buried.
    if state.commit_msg_editor is not None:
        draw_commit_msg_editor(stdscr, state, sidebar_x)
    # Help screen is a peer of the app menu — opened from it but
    # paints on top so the user can read the docs without seeing the
    # menu chrome underneath.
    if state.help_screen is not None:
        draw_help_screen(stdscr, state, sidebar_x)
    if state.ssh_keygen_modal is not None:
        draw_ssh_keygen_modal(stdscr, state, sidebar_x)

    # Sidebar drawn LAST so it's always the freshest paint on screen —
    # avoids the resize artifacts where stale cells from the old layout
    # bleed through under the panel.
    if sidebar_w > 0:
        draw_sidebar(stdscr, state, sidebar_x, sidebar_w)

    # The hardware cursor on the focused commit-message field is only
    # advertised when the repos panel itself owns focus. While the user
    # is over on the tasks panel, state.selected still names a body row
    # but drawing a blinking cursor there would suggest both panels are
    # accepting input at once. We don't touch state.selected, so the
    # cursor reappears on the same column when focus returns.
    cursor_set = False
    # Commit-message editor owns the cursor while it's open — paint the
    # caret last, AFTER sidebar redraw, so the modal's textarea has a
    # visible cursor even though `modal_active` is True.
    if state.commit_msg_editor is not None:
        from .modals.commit_msg_editor import apply_commit_msg_editor_cursor
        if apply_commit_msg_editor_cursor(stdscr, state):
            cursor_set = True
    if (not cursor_set and not modal_active and state.selected >= 0
            and state.focused_panel == "repos"):
        body_idx = state.selected
        if 0 <= body_idx < len(body_rows) and body_idx in y_for_body:
            row = body_rows[body_idx]
            target = None
            if row[0] == "repo":
                r = row[1]
                target = r if ((r.is_dirty or r.message)
                               and not r.refreshing) else None
            elif row[0] == "child" and row[2].kind == "submodule":
                ch = row[2]
                target = ch if ((ch.dirty or ch.message)
                                and not ch.refreshing) else None
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

    if not repo.refreshing:
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
    name_attr = curses.A_BOLD if focused else curses.A_DIM
    # Submodule glyph is a composite "needs your attention?" indicator:
    #   pink   — out of sync vs canonical (drift takes precedence — the
    #            nested checkout is on the wrong commit, fixing that is
    #            the bigger problem)
    #   yellow — in sync with canonical but the working tree is dirty
    #            (uncommitted edits — easy to miss when scanning if the
    #            glyph were green)
    #   green  — in sync AND clean (truly nothing to do)
    # Subtree rows use ⊕ in the normal name attribute unless refreshing.
    # While refreshing, the glyph uses the same spinner as repo rows /
    # the state column so in-flight work is obvious at a glance.
    if child.refreshing:
        glyph = spinner_char
        glyph_attr = curses.color_pair(PAIR_BRANCH)
        if focused:
            glyph_attr |= curses.A_BOLD
    else:
        glyph = "↳" if child.kind == "submodule" else "⊕"
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
        if not child.refreshing:
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

def show_no_repos_message(stdscr, workspace: Path) -> None:
    """Used at startup if discovery finds no git repos under workspace."""
    safe_addstr(stdscr, 0, 0,
                f"no git repos found under {workspace}",
                curses.color_pair(PAIR_ERR))
    safe_addstr(stdscr, 2, 0,
                f"edit workspace folders in {WORKSPACES_FILE}, then re-run.",
                curses.A_DIM)
    stdscr.refresh()
    stdscr.timeout(-1)
    stdscr.getch()
