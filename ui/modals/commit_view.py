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

from core.models import ActionMenuItem, CommitViewModal, FileEntry, State
from core.workers import kick_off_add_tag, kick_off_load_commit_view

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
from ..tabs import cycle_tab, draw_tab_strip
from .diff_viewer import handle_diff_viewer_key, open_diff_viewer

_MODAL_W = 100
_PAD_X = 2
_PANE_TARGET_ROWS = 10  # max rows shown before scrolling
_TAB_IDS = ("changes", "reflog")
_VALID_TAG_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_./"
)


def _spinner_glyph(state: State) -> str:
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


# ---------- Action items list ---------------------------------------------


def _build_action_items() -> List[ActionMenuItem]:
    """The per-commit action list. Today this is a one-item list
    (`+ add tag`); future actions (cherry-pick, copy SHA, push tag…)
    slot in here so the modal stays the single home for "things you
    can do with this commit"."""
    return [
        ActionMenuItem(id="add_tag", label="+ add tag", enabled=True),
    ]


# ---------- Open ----------------------------------------------------------


def open_commit_view_modal(state: State, target_path,
                           target_label: str,
                           sha: str, subject: str = "") -> None:
    """Install the commit view modal and kick off the async loader.
    Caller passes the commit's short sha + the action-menu target's
    repo path/label so we don't have to re-derive them. Subject is
    pre-filled from the cached `CommitEntry` the caller already had
    on hand — the modal renders it instantly while the slower
    queries (full body, tags, files) land in the background."""
    if not sha or sha.startswith("-"):
        return
    modal = CommitViewModal(
        target_label=target_label,
        target_path=target_path,
        sha=sha,
        subject=subject,
    )
    state.commit_view_modal = modal
    kick_off_load_commit_view(state, modal)


# ---------- Layout helpers ------------------------------------------------


def _wrap_text(text: str, width: int) -> List[str]:
    """Greedy word-wrap, hard-breaking words longer than the row
    width. Used by the body renderer; same shape as the review
    pane's `_word_wrap` but local so this module stays free of
    review-pane imports."""
    if not text:
        return []
    if width <= 0:
        return []
    out: List[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            out.append("")
            continue
        words = paragraph.split(" ")
        current = ""
        for w in words:
            while len(w) > width:
                if current:
                    out.append(current)
                    current = ""
                out.append(w[:width])
                w = w[width:]
            candidate = w if not current else current + " " + w
            if len(candidate) <= width:
                current = candidate
            else:
                out.append(current)
                current = w
        if current:
            out.append(current)
    return out


def _flow_badges(tags: List[str], width: int,
                 max_lines: int = 2) -> List[List[str]]:
    """Pack tag names into rows of width-bounded lines. Each badge
    renders as `[name]` with a 1-cell gap; lines beyond `max_lines`
    are folded into a trailing "+N more" tail so a 50-tag commit
    doesn't blow up the modal height."""
    if not tags:
        return []
    rendered = [f"[{t}]" for t in tags]
    rows: List[List[str]] = [[]]
    cur_w = 0
    gap = 1
    for badge in rendered:
        bw = len(badge)
        if cur_w == 0:
            rows[-1].append(badge)
            cur_w = bw
        elif cur_w + gap + bw <= width:
            rows[-1].append(badge)
            cur_w += gap + bw
        else:
            if len(rows) >= max_lines:
                # Out of room — replace the last badge with a "+N more"
                # tail. Try to keep at least one badge visible.
                remaining = len(rendered) - rendered.index(badge)
                tail = f"+{remaining} more"
                while (rows[-1]
                       and (sum(len(b) + gap for b in rows[-1])
                            - gap + gap + len(tail) > width)):
                    rows[-1].pop()
                if not rows[-1]:
                    rows[-1] = [tail]
                else:
                    rows[-1].append(tail)
                return rows
            rows.append([badge])
            cur_w = bw
    return rows


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
    items = _build_action_items()
    hints: List[Hint] = [Hint(KEY_UP_DOWN, "navigate")]
    if 0 <= modal.action_selected < len(items):
        item = items[modal.action_selected]
        if item.id == "add_tag":
            hints.append(Hint(KEY_ENTER, "add tag"))
    hints.append(Hint(KEY_TAB, "close"))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


# ---------- Draw ----------------------------------------------------------


def _build_tab_header(modal: CommitViewModal, state: State) -> list:
    """Compose `(id, label, count_str)` tuples for the tab strip.
    While a loader is still in flight the count slot shows the
    spinner glyph instead of a stale `(0)`."""
    if modal.files_loading and not modal.files:
        changes_count = _spinner_glyph(state)
    else:
        changes_count = str(len(modal.files))
    if modal.reflog_loading and not modal.reflog_entries:
        reflog_count = _spinner_glyph(state)
    else:
        reflog_count = str(len(modal.reflog_entries))
    return [
        ("changes", "Changes", changes_count),
        ("reflog", "Reflog", reflog_count),
    ]


def _draw_changes_tab(stdscr, state: State, modal: CommitViewModal,
                      line: int, inner_x: int, inner_w: int,
                      pane_visible: int, pane_focused: bool,
                      sb: int) -> None:
    """Render the Changes tab — file rows with the same status
    glyphs / coloring as the action menu's working-tree pane.
    Caller has already advanced past the tab strip; this writes
    `pane_visible` rows starting at `line`."""
    if modal.files_loading and not modal.files:
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
    if modal.reflog_loading and not modal.reflog_entries:
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
    body_lines = _wrap_text(modal.body, target_inner_w) if modal.body else []
    subject_lines = (_wrap_text(modal.subject, target_inner_w)
                     if modal.subject else [])
    badge_rows = _flow_badges(modal.tags, target_inner_w, max_lines=2)
    if not modal.tags and modal.tags_loading:
        badge_rows = [["(loading tags…)"]]
    elif not modal.tags:
        badge_rows = [["(no tags on this commit)"]]
    action_items = _build_action_items()
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
    tabs = _build_tab_header(modal, state)
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


def _close_modal(state: State) -> None:
    modal = state.commit_view_modal
    if modal is None:
        return
    modal.cancel_event.set()
    state.commit_view_modal = None


def _begin_add_tag(modal: CommitViewModal) -> None:
    modal.edit_field = "add_tag"
    modal.edit_typed = ""


def _cancel_inline(modal: CommitViewModal) -> None:
    modal.edit_field = ""
    modal.edit_typed = ""


def _request_confirm(modal: CommitViewModal, message: str,
                     action: str, args: "dict[str, str]") -> None:
    modal.confirm_message = message
    modal.confirm_action = action
    modal.confirm_args = dict(args)


def _clear_confirm(modal: CommitViewModal) -> None:
    modal.confirm_message = ""
    modal.confirm_action = ""
    modal.confirm_args = {}


def _apply_pending(state: State, modal: CommitViewModal) -> None:
    if modal.confirm_action == "add_tag":
        name = modal.confirm_args.get("name", "")
        sha = modal.confirm_args.get("sha", "")
        kick_off_add_tag(
            state, target_label=modal.target_label,
            target_path=modal.target_path,
            target_repo=None, target_parent=None,
            name=name, sha=sha)
        # Optimistic update — the worker writes a ref, the next
        # state-load tick reads it back; in the meantime show the
        # tag in the badge row so the user sees it landed.
        if name and name not in modal.tags:
            modal.tags = list(modal.tags) + [name]
    _clear_confirm(modal)


def _handle_confirm(state: State, modal: CommitViewModal,
                    key: int) -> None:
    if key in (ord("y"), ord("Y")):
        _apply_pending(state, modal)
        return
    if key in (ord("n"), ord("N"), 27):
        _clear_confirm(modal)
        return


def _handle_inline_edit(state: State, modal: CommitViewModal,
                        key: int) -> None:
    if key == 27:
        _cancel_inline(modal)
        return
    if key in (10, 13, curses.KEY_ENTER):
        text = modal.edit_typed.strip()
        if not text or text.startswith("-"):
            return
        _cancel_inline(modal)
        _request_confirm(
            modal,
            f"Add tag {text} → {modal.sha[:8]}? [y/N]",
            "add_tag",
            {"name": text, "sha": modal.sha})
        return
    if key in (curses.KEY_BACKSPACE, 127, 8):
        modal.edit_typed = modal.edit_typed[:-1]
        return
    if 32 <= key < 127:
        ch = chr(key)
        if not modal.edit_typed and ch == "-":
            return
        if ch in _VALID_TAG_CHARS:
            modal.edit_typed += ch


def _open_diff_for_focused_file(state: State,
                                modal: CommitViewModal) -> None:
    if not modal.files:
        return
    if not (0 <= modal.file_selected < len(modal.files)):
        return
    fe = modal.files[modal.file_selected]
    open_diff_viewer(
        state,
        target_path=modal.target_path,
        label=f"{modal.target_label} · {modal.sha[:8]}",
        file_path=fe.path,
        untracked=False,
        commit_sha=modal.sha,
    )


def handle_commit_view_modal_key(state: State, key: int) -> None:
    modal = state.commit_view_modal
    if modal is None:
        return

    # Diff viewer is a sub-sub-modal of this dialog — when open it
    # owns every key and Tab/Esc both close it.
    if state.diff_viewer is not None:
        handle_diff_viewer_key(state, key)
        return

    # Confirm and inline-edit modes intercept before the global Esc
    # so their own cancel semantics work.
    if modal.confirm_message:
        _handle_confirm(state, modal, key)
        return
    if modal.edit_field:
        _handle_inline_edit(state, modal, key)
        return

    if key == 27:  # Esc — close the modal back to action menu
        _close_modal(state)
        return

    if modal.section == "actions":
        if key == 9:  # Tab in actions section closes the dialog
            _close_modal(state)
            return
        items = _build_action_items()
        n = len(items)
        if key == curses.KEY_UP and n > 0:
            modal.action_selected = max(0, modal.action_selected - 1)
            return
        if key == curses.KEY_DOWN and n > 0:
            if modal.action_selected >= n - 1:
                # Drop into the tabs pane, mirroring the action
                # menu's main → bottom-pane Down semantics.
                modal.section = "tabs"
                if modal.files and modal.file_selected >= len(modal.files):
                    modal.file_selected = 0
                return
            modal.action_selected += 1
            return
        if key in (10, 13, curses.KEY_ENTER) and n > 0:
            item = items[modal.action_selected]
            if item.id == "add_tag":
                _begin_add_tag(modal)
            return
        return

    # Tabs section — Changes drills via Tab; Reflog is read-only.
    if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
        tabs = [(t, t.title(), "") for t in _TAB_IDS]
        modal.active_tab = cycle_tab(
            tabs, modal.active_tab,
            -1 if key == curses.KEY_LEFT else 1)
        return
    if key == 9:  # Tab
        if modal.active_tab == "changes":
            _open_diff_for_focused_file(state, modal)
        else:
            _close_modal(state)
        return
    if key == curses.KEY_HOME:
        modal.section = "actions"
        modal.action_selected = 0
        return
    if modal.active_tab == "changes":
        if key == curses.KEY_UP:
            if modal.file_selected <= 0:
                modal.section = "actions"
                modal.file_selected = 0
                return
            modal.file_selected -= 1
            return
        if key == curses.KEY_DOWN:
            if (modal.files
                    and modal.file_selected < len(modal.files) - 1):
                modal.file_selected += 1
            return
        if key == curses.KEY_PPAGE:
            modal.file_selected = max(0, modal.file_selected - 10)
            return
        if key == curses.KEY_NPAGE:
            if modal.files:
                modal.file_selected = min(
                    len(modal.files) - 1, modal.file_selected + 10)
            return
    else:  # reflog tab — scroll-only
        if key == curses.KEY_UP:
            if modal.reflog_scroll <= 0:
                modal.section = "actions"
                return
            modal.reflog_scroll -= 1
            return
        if key == curses.KEY_DOWN:
            modal.reflog_scroll += 1  # clamped at draw time
            return
        if key == curses.KEY_PPAGE:
            modal.reflog_scroll = max(0, modal.reflog_scroll - 10)
            return
        if key == curses.KEY_NPAGE:
            modal.reflog_scroll += 10
            return


__all__ = [
    "open_commit_view_modal",
    "draw_commit_view_modal",
    "handle_commit_view_modal_key",
]
