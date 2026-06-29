"""Workspace creator wizard — auto-shown on first run when there are no
configured workspaces, also reachable from the future "Add workspace"
action. Lets the user list one or more folder paths; each one becomes
a workspace named after its folder basename.

Live validation: as the user edits each row's path, a debounced worker-owned
job checks the folder and stamps a tick + repo count next to the
path. The tick is informational — workspaces with zero repos can
still be created, since the user may be pre-staging a folder where
they'll later clone things."""
from __future__ import annotations

import curses

from core.state.app import State
from core.state.workspaces import WorkspaceCreator, WorkspaceDraft
from features.workspace_creator.actions import (
    handle_workspace_creator_key as handle_workspace_creator_key_action,
)

from ..colors import (
    PAIR_DLG_OK, PAIR_DLG_CYAN, PAIR_DLG_FG, PAIR_DLG_WARN,
)
from ..geometry import draw_modal_fill, modal_geometry, safe_addstr
from ..hints import (
    KEY_ENTER, KEY_ESC, KEY_TAB, KEY_UP_DOWN, Hint, render_hints,
)


def _hints(creator: WorkspaceCreator) -> list:
    """Footer hints for the creator wizard. The Enter description
    swings between "accept this path", "finish + create N workspaces",
    and (when no rows have content yet) a hint that tells the user to
    type a path first."""
    nonempty = sum(1 for d in creator.drafts if d.path_text.strip())
    on_done = creator.selected == len(creator.drafts)
    hints = [Hint(KEY_UP_DOWN, "navigate")]
    if on_done:
        if nonempty == 0:
            hints.append(Hint(KEY_ENTER, "type a path above first"))
        elif nonempty == 1:
            hints.append(Hint(KEY_ENTER, "create 1 workspace"))
        else:
            hints.append(Hint(KEY_ENTER, f"create {nonempty} workspaces"))
    else:
        focused = (creator.drafts[creator.selected]
                   if 0 <= creator.selected < len(creator.drafts)
                   else None)
        if focused is not None and focused.path_text.strip():
            hints.append(Hint(KEY_ENTER, "accept · next row"))
            hints.append(Hint(KEY_TAB, "accept · next row"))
        else:
            hints.append(Hint(KEY_ENTER, "skip to finish"))
    hints.append(Hint(KEY_ESC, "cancel"))
    return hints


def _draw_creator_hints(stdscr, creator: WorkspaceCreator, y: int,
                        x: int, w: int, attr: int) -> None:
    render_hints(stdscr, y, x, w, _hints(creator), attr=attr)


# ---------- Draw ----------------------------------------------------------


def _row_status(draft: WorkspaceDraft) -> "tuple[str, int]":
    """Return (status_text, color_pair) for the right-hand status hint
    shown next to a draft's path field."""
    if draft.checking:
        return ("(checking…)", 0)
    text = draft.path_text.strip()
    if not text:
        return ("", 0)
    if draft.error:
        return (draft.error, PAIR_DLG_WARN)
    if draft.repo_count > 0:
        return (f"✓ {draft.repo_count} repo"
                f"{'s' if draft.repo_count != 1 else ''} found", PAIR_DLG_OK)
    if draft.repo_count == 0:
        return ("(no repos found)", PAIR_DLG_WARN)
    return ("", 0)


def draw_workspace_creator(stdscr, state: State, sidebar_x: int) -> None:
    creator = state.workspace_creator
    if creator is None:
        return

    n_drafts = len(creator.drafts)
    # blank-top (1) + title (1) + spacer (1) + intro (2) + spacer (1)
    # + drafts + spacer (1) + done row (1) + spacer (1) + footer (1)
    # + blank-bottom (1) + slack (1)
    content_h = (1 + 1 + 1 + 2 + 1 + max(1, n_drafts)
                 + 1 + 1 + 1 + 1 + 1 + 1)
    x, y, w, h = modal_geometry(stdscr, sidebar_x, 80, content_h)
    sb = curses.color_pair(PAIR_DLG_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    inner_w = w - 4

    safe_addstr(stdscr, y + 1, inner_x, creator.title[:inner_w],
                curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN))

    # Wrap the intro across two rows so a long sentence doesn't get
    # clipped to a single field's width.
    intro = creator.intro
    if intro:
        first = intro[:inner_w]
        rest = intro[inner_w: inner_w * 2]
        safe_addstr(stdscr, y + 3, inner_x, first, sb | curses.A_DIM)
        if rest:
            safe_addstr(stdscr, y + 4, inner_x, rest, sb | curses.A_DIM)

    # Field column widths: status text reserves 30 cells on the right,
    # leaving the rest for the path field. The "Path:" label sits at the
    # left, so account for it too.
    label = "Path: "
    status_w = 30
    field_w = max(20, inner_w - len(label) - status_w - 2)

    base_y = y + 6
    for i, draft in enumerate(creator.drafts):
        line_y = base_y + i
        focused = (creator.selected == i)
        prefix = "→ " if focused else "  "
        safe_addstr(stdscr, line_y, inner_x, prefix, sb | curses.A_BOLD)
        safe_addstr(stdscr, line_y, inner_x + 2, label, sb | curses.A_DIM)
        # Path field (visible window honours the cursor when the row is
        # focused so long paths still scroll under the user's caret).
        text = draft.path_text
        if focused:
            cur = max(0, min(creator.field_cursor, len(text)))
            if len(text) <= field_w - 1:
                visible = text
                cur_x = cur
            else:
                half = (field_w - 1) // 2
                start = max(0, min(cur - half, len(text) - (field_w - 1)))
                visible = text[start:start + field_w - 1]
                cur_x = cur - start
        else:
            visible = text if len(text) <= field_w - 1 else text[-(field_w - 1):]
            cur_x = len(visible)

        body = visible.ljust(field_w)
        field_attr = curses.A_UNDERLINE
        if focused:
            field_attr |= curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
        else:
            field_attr |= curses.A_DIM
        field_x = inner_x + 2 + len(label)
        safe_addstr(stdscr, line_y, field_x, body, field_attr)

        status_text, status_pair = _row_status(draft)
        if status_text:
            status_x = field_x + field_w + 1
            attr = (curses.color_pair(status_pair) if status_pair
                    else sb | curses.A_DIM)
            safe_addstr(stdscr, line_y, status_x,
                        status_text[:status_w], attr)

        # Show a soft hardware cursor on the focused field while the
        # modal is in path-edit mode.
        if focused and creator.selected < n_drafts:
            try:
                stdscr.move(line_y, field_x + cur_x)
                curses.curs_set(2)
            except curses.error:
                pass

    # "Done" pseudo-row.
    done_y = base_y + n_drafts + 1
    done_focused = (creator.selected == n_drafts)
    nonempty = sum(1 for d in creator.drafts if d.path_text.strip())
    if nonempty:
        text = (f"  Create {nonempty} workspace"
                f"{'s' if nonempty != 1 else ''}")
    else:
        text = "  (add at least one path above to continue)"
    if done_focused and nonempty:
        attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD | curses.A_REVERSE
    elif done_focused:
        attr = sb | curses.A_DIM | curses.A_REVERSE
    elif nonempty:
        attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
    else:
        attr = sb | curses.A_DIM
    safe_addstr(stdscr, done_y, inner_x, text.ljust(inner_w)[:inner_w], attr)

    if done_focused:
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    _draw_creator_hints(stdscr, creator, y + h - 2, inner_x, inner_w,
                        sb | curses.A_DIM)


def handle_workspace_creator_key(state: State, key: int) -> None:
    handle_workspace_creator_key_action(state, key)
