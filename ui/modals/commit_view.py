"""Commit view dialog — sub-modal of the action menu's commits pane.

Tab on a focused commit row in the recent-commits tab pops this
dialog, which renders the commit's metadata (author, date,
subject, body), tag badges in a flow layout, an action list (with
`+ add tag` as the only entry today), and the per-file changes
the commit introduced. Tab semantics mirror the rest of the app:
in the actions section it closes the modal back to the action
menu, in the files section it opens / closes the diff viewer
scoped to this commit (`git show <sha> -- <path>`)."""
from __future__ import annotations

import curses
from typing import List

from core.state.app import State
from core.state.action_menu import FileEntry
from core.state.views import CommitViewModal
from features.commit_view.actions import handle_commit_view_modal_key as handle_key
from features.commit_view.projection import (
    MODAL_W as _MODAL_W,
    PAD_X as _PAD_X,
    PANE_TARGET_ROWS as _PANE_TARGET_ROWS,
    build_action_items,
    build_tab_header,
    files_loading,
    flow_badges,
    reflog_loading,
    tags_loading,
    wrap_text,
)
from features.diff_viewer.session import open_diff_viewer

from ..colors import (
    PAIR_DLG_OK, PAIR_DLG_PASTEL_GREEN, PAIR_DLG_PASTEL_GREEN_ACTIVE,
    PAIR_DLG_PASTEL_RED, PAIR_DLG_PASTEL_RED_ACTIVE, PAIR_DLG_PASTEL_YELLOW,
    PAIR_DLG_CYAN, PAIR_DLG_FG, PAIR_DLG_FG_ACTIVE, PAIR_DLG_WARN,
)
from ..geometry import (
    clamp_scroll, draw_modal_fill, draw_scroll_overflow, end_truncate,
    modal_geometry, safe_addstr,
)
from ..hints import (
    KEY_BACKSPACE, KEY_ENTER, KEY_ESC, KEY_LEFT_RIGHT, KEY_TAB,
    KEY_UP_DOWN, Hint, render_hints,
)
from ..sidebar import SPINNER_FRAMES
from ..tabs import draw_tab_strip
from .diff_viewer import handle_diff_viewer_key

# ---------- File row renderer ---------------------------------------------


def _file_status_pair(status: str) -> int:
    """Map `git show --name-status` letter to a pastel pair so the
    delete / add / rename / modify visual hierarchy matches the
    review screen and the action menu's working-tree pane."""
    if status == "A":
        return PAIR_DLG_PASTEL_GREEN
    if status == "D":
        return PAIR_DLG_PASTEL_RED
    if status == "R":
        return PAIR_DLG_CYAN
    if status == "M":
        return PAIR_DLG_PASTEL_YELLOW
    return PAIR_DLG_FG


def _spinner_glyph(state: State) -> str:
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


def _draw_file_row(stdscr, y: int, x: int, w: int, fe: FileEntry,
                   focused: bool) -> None:
    """Single file row in the commit-view files pane. Status code
    stays in its own pastel column; +ins / -del numbers in
    green / red. Reverse-video on the focused row."""
    code = fe.x or " "
    code = code.ljust(2)[:2]
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
    fill = curses.color_pair(PAIR_DLG_FG_ACTIVE if focused else PAIR_DLG_FG)
    if focused:
        safe_addstr(stdscr, y, x, full, fill | curses.A_REVERSE)
        return
    safe_addstr(stdscr, y, x, full, fill)
    pair_id = _file_status_pair(fe.x.strip())
    if pair_id != PAIR_DLG_FG:
        safe_addstr(stdscr, y, x + 1, code.strip(),
                    curses.color_pair(pair_id))
    if stat:
        stat_x = x + len(left) + pad + 1
        safe_addstr(stdscr, y, stat_x, stat_ins,
                    curses.color_pair(PAIR_DLG_PASTEL_GREEN_ACTIVE
                                      if focused
                                      else PAIR_DLG_PASTEL_GREEN))
        safe_addstr(stdscr, y, stat_x + len(stat_ins) + 1, stat_del,
                    curses.color_pair(PAIR_DLG_PASTEL_RED_ACTIVE
                                      if focused
                                      else PAIR_DLG_PASTEL_RED))


# ---------- Hints ---------------------------------------------------------


def _hints(modal: CommitViewModal) -> List[Hint]:
    if modal.confirm_message:
        return [
            Hint("y", "apply"),
            Hint("n", "cancel"),
            Hint(KEY_ESC, "cancel"),
        ]
    if modal.edit_field:
        return [
            Hint("type", "edit tag name"),
            Hint(KEY_BACKSPACE, "delete char"),
            Hint(KEY_ENTER, "confirm"),
            Hint(KEY_ESC, "cancel"),
        ]
    if modal.section == "tabs":
        hints: List[Hint] = [Hint(KEY_LEFT_RIGHT, "switch tab")]
        if modal.active_tab == "changes":
            if not modal.files:
                hints.append(Hint("(no files)", ""))
            else:
                hints.append(Hint(KEY_UP_DOWN, "select file"))
                hints.append(Hint(KEY_TAB, "view diff"))
        else:  # reflog
            if not modal.reflog_entries:
                hints.append(Hint("(no reflog hits)", ""))
            else:
                hints.append(Hint(KEY_UP_DOWN, "scroll"))
        hints.append(Hint(KEY_ESC, "back"))
        return hints
    # Actions section
    items = build_action_items()
    hints: List[Hint] = [Hint(KEY_UP_DOWN, "navigate")]
    if 0 <= modal.action_selected < len(items):
        item = items[modal.action_selected]
        if item.id == "add_tag":
            hints.append(Hint(KEY_ENTER, "add tag"))
    hints.append(Hint(KEY_TAB, "close"))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


# ---------- Draw ----------------------------------------------------------


def _draw_changes_tab(stdscr, state: State, modal: CommitViewModal,
                      line: int, inner_x: int, inner_w: int,
                      pane_visible: int, pane_focused: bool,
                      sb: int) -> None:
    """Render the Changes tab — file rows with the same status
    glyphs / coloring as the action menu's working-tree pane.
    Caller has already advanced past the tab strip; this writes
    `pane_visible` rows starting at `line`."""
    if files_loading(state, modal) and not modal.files:
        safe_addstr(stdscr, line, inner_x + 2,
                    f"{_spinner_glyph(state)} loading files…",
                    sb | curses.A_DIM)
        return
    if not modal.files:
        safe_addstr(stdscr, line, inner_x + 2,
                    "(no file changes)", sb | curses.A_DIM)
        return
    n = len(modal.files)
    modal.file_scroll = clamp_scroll(
        modal.file_selected, modal.file_scroll, n, pane_visible)
    for slot in range(pane_visible):
        idx = modal.file_scroll + slot
        if idx >= n:
            break
        fe = modal.files[idx]
        focused = pane_focused and idx == modal.file_selected
        _draw_file_row(stdscr, line + slot, inner_x, inner_w,
                       fe, focused)
    if modal.file_scroll > 0:
        draw_scroll_overflow(stdscr, line, inner_x, inner_w,
                             modal.file_scroll, "up", sb | curses.A_DIM)
    end = min(n, modal.file_scroll + pane_visible)
    if end < n:
        below = n - end
        draw_scroll_overflow(stdscr, line + pane_visible - 1,
                             inner_x, inner_w, below, "down",
                             sb | curses.A_DIM)


def _draw_reflog_tab(stdscr, state: State, modal: CommitViewModal,
                     line: int, inner_x: int, inner_w: int,
                     pane_visible: int, sb: int) -> None:
    """Render the Reflog tab — text rows of the form
    `<short-sha> HEAD@{N} <reflog-message>`. Read-only, scroll-only
    (no per-row drilldown today). Caller has already advanced past
    the tab strip."""
    if reflog_loading(state, modal) and not modal.reflog_entries:
        safe_addstr(stdscr, line, inner_x + 2,
                    f"{_spinner_glyph(state)} loading reflog…",
                    sb | curses.A_DIM)
        return
    if not modal.reflog_entries:
        safe_addstr(stdscr, line, inner_x + 2,
                    "(no reflog entries mention this commit)",
                    sb | curses.A_DIM)
        return
    n = len(modal.reflog_entries)
    max_scroll = max(0, n - pane_visible)
    if modal.reflog_scroll > max_scroll:
        modal.reflog_scroll = max_scroll
    if modal.reflog_scroll < 0:
        modal.reflog_scroll = 0
    for slot in range(pane_visible):
        idx = modal.reflog_scroll + slot
        if idx >= n:
            break
        row = modal.reflog_entries[idx]
        # Highlight the leading short-sha (first whitespace-separated
        # token) so the eye lands on the commit ref first; the rest
        # of the line renders normal-weight.
        head, _, rest = row.partition(" ")
        head_clip = end_truncate(head, inner_w)
        safe_addstr(stdscr, line + slot, inner_x, head_clip,
                    curses.color_pair(PAIR_DLG_PASTEL_YELLOW))
        if rest:
            rest_x = inner_x + len(head_clip) + 1
            rest_w = max(0, inner_w - (rest_x - inner_x))
            if rest_w > 0:
                safe_addstr(stdscr, line + slot, rest_x,
                            end_truncate(rest, rest_w), sb)
    if modal.reflog_scroll > 0:
        draw_scroll_overflow(stdscr, line, inner_x, inner_w,
                             modal.reflog_scroll, "up",
                             sb | curses.A_DIM)
    end = min(n, modal.reflog_scroll + pane_visible)
    if end < n:
        below = n - end
        draw_scroll_overflow(stdscr, line + pane_visible - 1,
                             inner_x, inner_w, below, "down",
                             sb | curses.A_DIM)


def draw_commit_view_modal(stdscr, state: State, sidebar_x: int) -> None:
    modal = state.commit_view_modal
    if modal is None:
        return

    sb = curses.color_pair(PAIR_DLG_FG)
    target_inner_w = max(20, _MODAL_W - 2 * _PAD_X)

    # Pre-compute the variable-height blocks so the modal can size
    # itself just-large-enough for the content.
    short_sha = (modal.sha[:8] if len(modal.sha) >= 8
                 else modal.sha or "(no sha)")
    title_line = f"Commit {short_sha}"
    subtitle_parts = []
    if modal.author:
        subtitle_parts.append(modal.author)
    if modal.date:
        subtitle_parts.append(modal.date)
    subtitle = " · ".join(subtitle_parts) if subtitle_parts else ""
    body_lines = wrap_text(modal.body, target_inner_w) if modal.body else []
    subject_lines = (wrap_text(modal.subject, target_inner_w)
                     if modal.subject else [])
    badge_rows = flow_badges(modal.tags, target_inner_w, max_lines=2)
    if not modal.tags and tags_loading(state, modal):
        badge_rows = [["(loading tags…)"]]
    elif not modal.tags:
        badge_rows = [["(no tags on this commit)"]]
    action_items = build_action_items()
    # Bottom pane sizes itself to whichever tab needs the most rows
    # so swapping tabs doesn't resize the dialog.
    pane_rows = max(len(modal.files), len(modal.reflog_entries), 1)
    pane_visible = min(_PANE_TARGET_ROWS, pane_rows)

    body_h = (
        1                            # title row
        + (1 if subtitle else 0)
        + len(subject_lines)
        + (1 if body_lines else 0)   # spacer above body
        + len(body_lines)
        + 1                          # spacer
        + len(badge_rows)
        + 1                          # separator
        + len(action_items)
        + 1                          # separator
        + 1                          # tab strip
        + pane_visible
        + 1                          # spacer above hints
        + 1                          # hints
    )
    pad_top = 1
    pad_bottom = 1
    desired_h = pad_top + body_h + pad_bottom
    x, y, w, h = modal_geometry(stdscr, sidebar_x, _MODAL_W, desired_h)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + _PAD_X
    inner_w = max(1, w - 2 * _PAD_X)
    line = y + pad_top

    # Title (cyan bold) + subtitle (dim).
    safe_addstr(stdscr, line, inner_x,
                end_truncate(title_line, inner_w),
                curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN))
    line += 1
    if subtitle:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(subtitle, inner_w),
                    sb | curses.A_DIM)
        line += 1
    for sl in subject_lines:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(sl, inner_w), sb)
        line += 1
    if body_lines:
        line += 1
        for bl in body_lines:
            safe_addstr(stdscr, line, inner_x,
                        end_truncate(bl, inner_w), sb | curses.A_DIM)
            line += 1
    line += 1  # spacer above badges

    # Tag badges. Real tags get the cyan-bold pill treatment; the
    # placeholder rows ("loading", "no tags") render dim so they
    # read as informational rather than as an actual badge.
    is_placeholder = (not modal.tags)
    badge_attr = (sb | curses.A_DIM if is_placeholder
                  else (curses.color_pair(PAIR_DLG_OK) | curses.A_BOLD))
    for row in badge_rows:
        rendered = " ".join(row)
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(rendered, inner_w), badge_attr)
        line += 1

    # Separator above actions.
    safe_addstr(stdscr, line, inner_x, "─" * inner_w, sb | curses.A_DIM)
    line += 1

    # Action items.
    for i, item in enumerate(action_items):
        focused = (modal.section == "actions"
                   and modal.action_selected == i
                   and not modal.confirm_message
                   and not modal.edit_field)
        col0 = "→ " if focused else "  "
        if (modal.edit_field == "add_tag"
                and modal.action_selected == i):
            cell = f"{modal.edit_typed}_"
            safe_addstr(stdscr, line, inner_x,
                        (col0 + "tag: " + cell).ljust(inner_w)[:inner_w],
                        sb | curses.A_REVERSE)
        else:
            attr = sb | curses.A_REVERSE if focused else sb
            text = (col0 + item.label).ljust(inner_w)[:inner_w]
            safe_addstr(stdscr, line, inner_x, text, attr)
        line += 1

    # Separator above tabs.
    safe_addstr(stdscr, line, inner_x, "─" * inner_w, sb | curses.A_DIM)
    line += 1

    # Tab strip — Changes / Reflog. Spinner glyph stands in for the
    # count while a loader is still running so the header reads as
    # live state rather than a stale `(0)`.
    pane_focused = modal.section == "tabs"
    tabs = build_tab_header(modal, state, _spinner_glyph(state))
    draw_tab_strip(stdscr, line, inner_x, inner_w,
                   tabs, active_id=modal.active_tab,
                   focused=pane_focused, base_attr=sb)
    line += 1

    # Active tab content — Changes (file rows, drillable) or Reflog
    # (text rows, scroll only). Both share `pane_visible` height so
    # tab switching doesn't change dialog size.
    if modal.active_tab == "changes":
        _draw_changes_tab(stdscr, state, modal, line, inner_x,
                          inner_w, pane_visible, pane_focused, sb)
    else:
        _draw_reflog_tab(stdscr, state, modal, line, inner_x,
                         inner_w, pane_visible, sb)
    line += pane_visible

    line += 1  # spacer above hints

    # Confirm prompt overrides the hint row when set; same yellow
    # bold as the action menu's remote-edit confirm strip.
    if modal.confirm_message:
        safe_addstr(stdscr, line, inner_x,
                    end_truncate(modal.confirm_message, inner_w),
                    curses.color_pair(PAIR_DLG_WARN) | curses.A_BOLD)
    else:
        render_hints(stdscr, line, inner_x, inner_w,
                     _hints(modal), attr=sb | curses.A_DIM)


# ---------- Key handling --------------------------------------------------


def handle_commit_view_modal_key(state: State, key: int) -> None:
    if state.diff_viewer is not None:
        handle_diff_viewer_key(state, key)
        return
    effect = handle_key(state, key)
    if effect.kind == "open_diff":
        open_diff_viewer(
            state,
            target_path=effect.target_path,
            label=effect.label,
            file_path=effect.file_path,
            untracked=False,
            commit_sha=effect.commit_sha,
        )


__all__ = [
    "draw_commit_view_modal",
    "handle_commit_view_modal_key",
]
