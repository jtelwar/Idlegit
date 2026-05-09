"""Workspace creator wizard — auto-shown on first run when there are no
configured workspaces, also reachable from the future "Add workspace"
action. Lets the user list one or more folder paths; each one becomes
a workspace named after its folder basename.

Live validation: as the user edits each row's path, a debounced worker
thread re-runs `discover_repos` and stamps a tick + repo count next to
the path. The tick is informational — workspaces with zero repos can
still be created, since the user may be pre-staging a folder where
they'll later clone things."""
from __future__ import annotations

import curses
import threading
from pathlib import Path
from typing import List

from core.git_ops import discover_repos
from core.models import State, Workspace, WorkspaceCreator, WorkspaceDraft

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


# Debounce window: only fire a discover_repos check after the path has
# been stable for this long. Keeps rapid keystrokes from spawning a
# storm of short-lived worker threads.
_DEBOUNCE_SECONDS = 0.25


# ---------- Open / commit -------------------------------------------------


def open_workspace_creator(state: State, *,
                           title: str = "Set up workspaces",
                           intro: str = "") -> None:
    """Install the workspace creator modal with one empty draft and the
    cursor on it. Defaults are tailored for the first-run path; callers
    that re-open the modal later (e.g. an "Add workspace" item) should
    customise `title` / `intro` to match the entry point."""
    if not intro:
        intro = ("Add folders to scan for git repos. Each becomes a "
                 "workspace named after the folder.")
    state.workspace_creator = WorkspaceCreator(
        drafts=[WorkspaceDraft()],
        title=title,
        intro=intro,
    )


def _drafts_to_workspaces(drafts: List[WorkspaceDraft]) -> List[Workspace]:
    """Convert the modal's drafts into Workspace objects, dropping
    empty rows. Workspace names default to the folder's basename; on
    name collisions we append `(2)`, `(3)`, … so each workspace stays
    uniquely identifiable in the title-row selector."""
    out: List[Workspace] = []
    seen_names: dict = {}
    for d in drafts:
        text = d.path_text.strip()
        if not text:
            continue
        try:
            resolved = Path(text).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        base = resolved.name or str(resolved) or "workspace"
        name = base
        n = seen_names.get(base, 0)
        if n:
            name = f"{base} ({n + 1})"
        seen_names[base] = n + 1
        out.append(Workspace(name=name, folders=[resolved]))
    return out


def commit_workspace_creator(state: State) -> None:
    """Snapshot the dialogue's current drafts onto `creator.result` so
    the main loop can pick the new workspace list up and switch into
    it. Closing the modal is left to the main loop after it consumes
    `result` — that way a stale modal can never linger after the swap."""
    creator = state.workspace_creator
    if creator is None:
        return
    creator.result = _drafts_to_workspaces(creator.drafts)


# ---------- Live repo-count check ----------------------------------------


def _kick_off_check(draft: WorkspaceDraft) -> None:
    """Spawn a daemon thread that resolves `draft.path_text`, runs
    `discover_repos`, and stamps the result back onto the draft. The
    thread reads `draft.path_text` once at start and only writes back if
    the text is still the same as what it checked — this is the lock-
    free anti-stale guard. `draft.last_checked` is set to the text we
    just attempted regardless of outcome so the redraw loop knows not
    to re-spawn until the user edits again."""
    text = draft.path_text.strip()
    if not text:
        # Empty input — clear any stale state and skip the worker.
        draft.last_checked = draft.path_text
        draft.repo_count = -1
        draft.error = ""
        draft.checking = False
        return
    target = draft.path_text
    draft.checking = True

    def worker() -> None:
        repo_count = -1
        error = ""
        try:
            p = Path(text).expanduser()
            if not p.is_absolute():
                p = p.resolve()
            if not p.exists():
                error = "(no such folder)"
            elif not p.is_dir():
                error = "(not a folder)"
            else:
                try:
                    repos = discover_repos(p)
                except OSError as e:
                    error = f"(error: {e.strerror or e})"
                else:
                    repo_count = len(repos)
        except (OSError, RuntimeError) as e:
            error = f"(error: {e})"
        # Only stamp if the user hasn't moved on to a different value.
        if draft.path_text == target:
            draft.repo_count = repo_count
            draft.error = error
            draft.last_checked = target
            draft.checking = False

    threading.Thread(target=worker, daemon=True).start()


def tick_creator_checks(state: State) -> bool:
    """Called from the main draw loop to (re)spawn discover_repos workers
    for any draft whose `path_text` has drifted from `last_checked`. Returns
    True if any draft is currently being checked, so the caller can keep
    the spinner ticking. Debouncing is implicit: each draft is checked
    once per stable text value, not once per keystroke."""
    creator = state.workspace_creator
    if creator is None:
        return False
    any_checking = False
    for draft in creator.drafts:
        if draft.path_text != draft.last_checked and not draft.checking:
            _kick_off_check(draft)
        if draft.checking:
            any_checking = True
    return any_checking


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


# ---------- Handle --------------------------------------------------------


def _focused_draft(creator: WorkspaceCreator) -> "WorkspaceDraft | None":
    if 0 <= creator.selected < len(creator.drafts):
        return creator.drafts[creator.selected]
    return None


def _on_done_row(creator: WorkspaceCreator) -> bool:
    return creator.selected == len(creator.drafts)


def _ensure_trailing_empty(creator: WorkspaceCreator) -> None:
    """Keep one blank draft pinned at the end so the user always has a
    place to add another path without a separate "+ new" command.
    No-op when the last row is already blank."""
    if not creator.drafts or creator.drafts[-1].path_text:
        creator.drafts.append(WorkspaceDraft())


def _move_to_field(creator: WorkspaceCreator, idx: int) -> None:
    """Snap selection to row `idx` and park the field cursor at the end
    of that row's path so the user can keep typing without first moving
    the caret. No-op when `idx` is out of range or already focused."""
    if idx < 0 or idx > len(creator.drafts):
        return
    creator.selected = idx
    if idx < len(creator.drafts):
        creator.field_cursor = len(creator.drafts[idx].path_text)
    else:
        creator.field_cursor = 0


def handle_workspace_creator_key(state: State, key: int) -> None:
    creator = state.workspace_creator
    if creator is None:
        return

    if key == 27:
        # Cancel — leave state.workspace_creator untouched (caller
        # checks `result is None` to distinguish cancel from commit).
        creator.result = []
        state.workspace_creator = None
        return

    if key == curses.KEY_UP:
        _move_to_field(creator, max(0, creator.selected - 1))
        return
    if key == curses.KEY_DOWN:
        max_idx = len(creator.drafts)  # Done row index
        # Stepping off the last (blank) draft jumps straight to Done.
        if (creator.selected < len(creator.drafts)
                and not creator.drafts[creator.selected].path_text):
            _move_to_field(creator, max_idx)
            return
        _move_to_field(creator, min(max_idx, creator.selected + 1))
        return

    if _on_done_row(creator):
        if key in (10, 13, curses.KEY_ENTER):
            nonempty = sum(1 for d in creator.drafts if d.path_text.strip())
            if nonempty == 0:
                # Nothing to commit — bounce back to the first row so the
                # user can type something.
                _move_to_field(creator, 0)
                return
            commit_workspace_creator(state)
            return
        return

    draft = _focused_draft(creator)
    if draft is None:
        return

    text = draft.path_text
    cur = max(0, min(creator.field_cursor, len(text)))

    if key in (10, 13, curses.KEY_ENTER, 9):  # Enter or Tab
        # Accept this row and advance. If we're on the trailing empty
        # row this also seeds a fresh blank for the next entry.
        if not text.strip():
            # Empty Enter on a blank row jumps to Done.
            _move_to_field(creator, len(creator.drafts))
            return
        _ensure_trailing_empty(creator)
        _move_to_field(creator, creator.selected + 1)
        return

    if key == curses.KEY_LEFT:
        creator.field_cursor = max(0, cur - 1)
        return
    if key == curses.KEY_RIGHT:
        creator.field_cursor = min(len(text), cur + 1)
        return
    if key == curses.KEY_HOME or key == 1:
        creator.field_cursor = 0
        return
    if key == curses.KEY_END or key == 5:
        creator.field_cursor = len(text)
        return

    if key in (curses.KEY_BACKSPACE, 127, 8):
        if cur > 0:
            draft.path_text = text[: cur - 1] + text[cur:]
            creator.field_cursor = cur - 1
            draft.last_checked = ""  # invalidate so the worker re-runs
        return
    if key == curses.KEY_DC:
        if cur < len(text):
            draft.path_text = text[:cur] + text[cur + 1:]
            draft.last_checked = ""
        return
    if 32 <= key < 127:
        draft.path_text = text[:cur] + chr(key) + text[cur:]
        creator.field_cursor = cur + 1
        draft.last_checked = ""
        # Make sure there's still a blank row at the end for the next
        # workspace the user might want to add.
        _ensure_trailing_empty(creator)
        return
