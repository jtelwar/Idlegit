"""Tab-on-row action menu — fetch/pull/push/switch-branch/soft-reset/
run-a-workflow against the focused repo or submodule child.

The bottom half of the modal is a tabbed pane: a working-tree view
(every changed/untracked file with status + ins/del counts) and a
commits view (paged in lazily as the user scrolls back). Each pane
has its own filter row at the top. Down off the last action item
moves focus into the pane; Up off the filter row moves it back.
Home jumps to the first action item from anywhere."""
from __future__ import annotations

import curses
from typing import Optional

from features.action_menu.actions import (
    ActionMenuEffect,
    handle_action_menu_key_intent,
)
from features.action_menu.loaders import (
    kick_off_action_menu_commits_page as _kick_off_commits_page,
)
from features.action_menu.projection import (
    breadcrumb_segments as _breadcrumb_segments,
    build_actions_items as _build_actions_items,
    build_branch_items as _build_branch_items,
    build_remotes_items as _build_remotes_items,
    build_stashes_items as _build_stashes_items,
    current_items as _current_items,
    current_selected as _current_selected,
    filtered_commits as _filtered_commits,
    filtered_tree as _filtered_tree,
    in_submenu as _in_submenu,
)
from features.action_menu.session import close_action_menu
from core.state.app import State
from core.state.action_menu import (
    ActionMenu, CommitEntry, FileEntry,
)

from ..colors import (
    PAIR_DLG_PASTEL_BLUE, PAIR_DLG_PASTEL_GREEN, PAIR_DLG_PASTEL_RED, PAIR_DLG_PASTEL_YELLOW,
    PAIR_DLG_CYAN, PAIR_DLG_FG,
)
from ..geometry import (
    draw_modal_fill, end_truncate, modal_geometry, safe_addstr, truncate,
)
from ..hints import (
    KEY_DOWN, KEY_ENTER, KEY_ESC, KEY_HOME, KEY_LEFT, KEY_LEFT_RIGHT,
    KEY_RIGHT, KEY_TAB, KEY_UP_DOWN, Hint, render_hints,
)
from ..sidebar import SPINNER_FRAMES
from features.diff_viewer.session import open_diff_viewer
from .diff_viewer import handle_diff_viewer_key


def _spinner_glyph(state: State) -> str:
    """Current spinner frame, picked from the same global tick the
    sidebar uses so every animated indicator in the app stays in sync."""
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


def _is_load_running(state: State, load_id: str) -> bool:
    if not load_id:
        return False
    record = state.view_loads.get(load_id)
    return bool(record is not None and record.loading)


def _state_loading(state: State, menu: ActionMenu) -> bool:
    return _is_load_running(state, menu.state_load_id)


def _inventory_loading(state: State, menu: ActionMenu) -> bool:
    return _is_load_running(state, menu.inventory_load_id)


def _tree_loading(state: State, menu: ActionMenu) -> bool:
    return _is_load_running(state, menu.tree_load_id)


def _commits_loading(state: State, menu: ActionMenu) -> bool:
    return _is_load_running(state, menu.commits_load_id)


def _hints_action_focus(menu: ActionMenu) -> list:
    """Footer hints when the action items list has focus. Enter's
    description names the focused item; disabled items show why.
    Submenu rows show Right/Left navigation hints (Down-into-pane is
    swapped out when in a submenu since the bottom pane is owned by
    the top level only)."""
    items = _current_items(menu)
    selected = _current_selected(menu)
    hints = [Hint(KEY_UP_DOWN, "select")]
    if 0 <= selected < len(items):
        item = items[selected]
        if item.is_back:
            parent = "main menu"
            if len(menu.submenu_stack) >= 2:
                parent = menu.submenu_stack[-2].label
            hints.append(Hint(KEY_ENTER, f"back to {parent}"))
        elif item.id.startswith("remote:"):
            # Remote rows have three actions on shortcut keys —
            # Enter (set URL) is the most common, R renames, D
            # deletes (with confirm).
            hints.append(Hint(KEY_ENTER, "edit url"))
            hints.append(Hint("r", "rename"))
            hints.append(Hint("d", "delete"))
        elif item.has_submenu:
            hints.append(Hint(KEY_RIGHT, f"open {item.label} menu"))
            hints.append(Hint(KEY_ENTER, f"open {item.label} menu"))
        elif item.enabled:
            hints.append(Hint(KEY_ENTER, item.label))
        else:
            reason = f" ({item.reason})" if item.reason else ""
            hints.append(Hint(KEY_ENTER, f"unavailable{reason}"))
    if _in_submenu(menu):
        parent = (menu.submenu_stack[-2].label
                  if len(menu.submenu_stack) >= 2 else "main menu")
        hints.append(Hint(KEY_LEFT, f"back to {parent}"))
    else:
        hints.append(Hint(KEY_DOWN, "into pane"))
    hints.append(Hint(KEY_TAB, "close"))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


def _hints_pane_focus(menu: ActionMenu) -> list:
    """Footer hints when the bottom pane (working tree / commits) has
    focus. Tab swap is presented as ←/→ to match how the existing UI
    handles the same physical motion."""
    other_tab = "commits" if menu.pane_tab == "tree" else "working tree"
    hints = [
        Hint(KEY_UP_DOWN, "select"),
        Hint(KEY_LEFT_RIGHT, f"switch to {other_tab}"),
    ]
    if menu.pane_tab == "tree" and menu.tree_selected > 0:
        hints.append(Hint(KEY_TAB, "view diff"))
    hints.append(Hint(KEY_HOME, "back to actions"))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


def _draw_action_hints(stdscr, menu: ActionMenu, y: int, x: int,
                       w: int, attr: int) -> None:
    """Single call site keeps render_hints visibly used so the
    autoformatter doesn't strip it from the import block on
    subsequent edits. Confirm prompts and inline edit modes paint
    their own footer text instead of the regular hint list."""
    if menu.confirm_message:
        # Bold yellow strip — same treatment the standalone remotes
        # modal's confirm row uses, so the confirm UX reads the same
        # everywhere.
        from ..colors import PAIR_DLG_WARN
        text = menu.confirm_message
        safe_addstr(stdscr, y, x,
                    text[:max(0, w)],
                    curses.color_pair(PAIR_DLG_WARN) | curses.A_BOLD)
        return
    if menu.edit_field:
        from ..hints import KEY_BACKSPACE
        if menu.edit_field == "rename_remote":
            verb = "rename"
        elif menu.edit_field == "set_url_remote":
            verb = "set url"
        elif menu.edit_field == "add_remote_name":
            verb = "next: enter URL"
        elif menu.edit_field == "add_remote_url":
            verb = "add remote"
        else:
            verb = "save"
        hints = [
            Hint("type", "edit"),
            Hint(KEY_BACKSPACE, "delete char"),
            Hint(KEY_ENTER, verb),
            Hint(KEY_ESC, "cancel"),
        ]
        render_hints(stdscr, y, x, w, hints, attr=attr)
        return
    hints = (_hints_pane_focus(menu) if menu.pane_focus
             else _hints_action_focus(menu))
    render_hints(stdscr, y, x, w, hints, attr=attr)


# Map a porcelain XY status pair to a pastel colour pair. Ordered so
# the most-impactful change (delete > add > rename > modify) wins —
# a "DM" file (deleted-staged, then re-modified in-tree) reads as a
# delete first, which matches what the user is going to commit.
def _file_status_pair(x: str, y: str) -> Optional[int]:
    pair = (x, y)
    if "U" in pair or pair == ("A", "A") or pair == ("D", "D"):
        return PAIR_DLG_PASTEL_RED
    if "D" in pair:
        return PAIR_DLG_PASTEL_RED
    if "A" in pair:
        return PAIR_DLG_PASTEL_GREEN
    if "R" in pair:
        return PAIR_DLG_PASTEL_BLUE
    if "M" in pair:
        return PAIR_DLG_PASTEL_YELLOW
    return None


# Modal sizing.
MODAL_W = 90
PANE_TARGET_ROWS = 12  # rows visible in the bottom pane (cap)


# ---------- Draw ----------------------------------------------------------


def _scroll_for_cursor(text: str, cur: int,
                       width: int) -> "tuple[str, int]":
    """Crop `text` to `width` so the cursor stays visible. When the
    buffer fits, returns it unchanged with the cursor at its natural
    offset; when it doesn't, slides a window over the buffer and
    returns the cursor offset within that window. Mirrors the
    workspace_menu inline-edit helper so paste-of-long-URLs into the
    name/url cells doesn't push the cursor off-screen."""
    cur = max(0, min(cur, len(text)))
    width = max(1, width)
    if len(text) <= width - 1 or width <= 1:
        return text[: width], cur
    half = (width - 1) // 2
    start = max(0, min(cur - half, len(text) - (width - 1)))
    return text[start:start + width - 1], cur - start


def _place_inline_cursor(stdscr, y: int, x: int) -> None:
    """Move the terminal cursor to (y, x) and make it visible. Wrapped
    in try/except since `move` raises on out-of-bounds, which can
    happen during a resize race."""
    try:
        stdscr.move(y, x)
        curses.curs_set(2)
    except curses.error:
        pass


def draw_action_menu(stdscr, state: State, sidebar_x: int) -> None:
    menu = state.action_menu
    if menu is None:
        return

    items = _current_items(menu)
    selected = _current_selected(menu)
    # Reserve enough action rows to fit whichever list is longest
    # across all menu levels so the modal doesn't shrink/grow
    # visibly when the user enters or exits a submenu. Stash entries
    # plus the back/new-stash/separator chrome can outsize the main
    # menu, so include the cached stash count too.
    main_count = len(menu.items)
    branch_count = len(_build_branch_items(menu.cached_meta or {}))
    actions_count = len(_build_actions_items(menu.cached_meta or {}))
    stashes_count = len(_build_stashes_items(menu.stashes))
    remotes_count = len(_build_remotes_items(menu.remotes_list))
    n_items = max(main_count, branch_count, actions_count,
                  stashes_count, remotes_count)
    # Header (title + spacer + branch + upstream + sep) = 5 rows;
    # breadcrumb row (always reserved) = 1; actions = n_items rows;
    # separator = 1; tab header = 1; filter = 1; pane list = up to
    # PANE_TARGET_ROWS; footer hint = 1; padding = 2. Trailing +1
    # reserves a blank row below the footer for visual breathing —
    # the existing layout already has a blank above the title via
    # the leading "1" component.
    content_h = (1 + 1 + 1 + 1 + 1
                 + 1
                 + n_items + 1
                 + 1 + 1
                 + PANE_TARGET_ROWS
                 + 1 + 1
                 + 1)
    x, y, w, h = modal_geometry(stdscr, sidebar_x, MODAL_W, content_h)
    sb = curses.color_pair(PAIR_DLG_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    inner_w = w - 4

    # Title row: repo name (cyan-bold) + middle-truncated full path
    # in brackets (dim) so users with multiple repository_folders can
    # tell which on-disk location this menu targets at a glance.
    # The repo name uses end-only truncation (no middle-truncation of
    # repo names — modal-wide rule); only the on-disk path keeps the
    # middle-truncation since the leaf folder is what users recognise.
    name = menu.target_label
    path_str = str(menu.target_path)
    name_clip = end_truncate(name, inner_w)
    safe_addstr(stdscr, y + 1, inner_x, name_clip,
                curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN))
    # Reserve room for "  [<path>]" — 4 cells of fixed chrome around
    # the truncated path. min length 3 so we never collapse to "[…]".
    avail = inner_w - len(name_clip) - 4
    if avail >= 3:
        path_trunc = truncate(path_str, avail, mode="middle")
        safe_addstr(stdscr, y + 1, inner_x + len(name_clip),
                    f"  [{path_trunc}]", sb | curses.A_DIM)

    line = y + 3
    branch_label = menu.branch or "(loading…)"
    branch_str = f"[{branch_label}]"
    safe_addstr(stdscr, line, inner_x, branch_str,
                curses.color_pair(PAIR_DLG_CYAN))
    if _state_loading(state, menu):
        # Spinner + neutral "checking…" badge while query_target_state
        # is in flight. Matches the sidebar spinner so the user reads
        # the modal-level loading indicator the same way as everywhere
        # else in the app.
        spin = _spinner_glyph(state)
        safe_addstr(stdscr, line, inner_x + len(branch_str) + 1,
                    f"{spin} checking…", sb | curses.A_DIM)
    else:
        state_attr = (curses.color_pair(menu.state_pair)
                      if menu.state_pair else (sb | curses.A_DIM))
        safe_addstr(stdscr, line, inner_x + len(branch_str) + 1,
                    f"● {menu.state_label}", state_attr)

    line += 1
    if menu.upstream:
        meta = (f"upstream: {menu.upstream}  ·  "
                f"ahead {menu.ahead} / behind {menu.behind}")
    else:
        meta = "no upstream"
    safe_addstr(stdscr, line, inner_x, meta[:inner_w], sb | curses.A_DIM)

    line += 1
    safe_addstr(stdscr, line, inner_x, "─" * inner_w, sb | curses.A_DIM)

    # Breadcrumb header — `repo › branch › Stashes`. Earlier
    # segments dim, current segment in accent cyan-bold so the user
    # always sees where they are. The row is always reserved (kept
    # blank on the main menu) so the modal layout doesn't shift when
    # the user pushes into a submenu.
    line += 1
    if menu.submenu_stack:
        segs = _breadcrumb_segments(menu)
        cx = inner_x
        sep = " › "
        for i, seg in enumerate(segs):
            is_last = (i == len(segs) - 1)
            if is_last:
                seg_attr = (curses.color_pair(PAIR_DLG_CYAN)
                            | curses.A_BOLD)
            else:
                seg_attr = sb | curses.A_DIM
            text = seg
            if cx + len(text) > inner_x + inner_w:
                text = end_truncate(text, inner_x + inner_w - cx)
            safe_addstr(stdscr, line, cx, text, seg_attr)
            cx += len(text)
            if not is_last:
                if cx + len(sep) > inner_x + inner_w:
                    break
                safe_addstr(stdscr, line, cx, sep, sb | curses.A_DIM)
                cx += len(sep)
    line += 1

    # Action items — `items` and `selected` come from the
    # main-or-submenu helpers so the same render path serves both.
    # The caret column 0 is reserved across every row: rows that
    # open a submenu paint a `›` here (always visible, dim when not
    # focused, bright when focused); regular focused rows paint the
    # focus arrow `→`; everything else gets blank padding so labels
    # align across the list. Separators render as a dim hairline.
    rendered = 0
    for i, item in enumerate(items):
        focused = (i == selected and not menu.pane_focus
                   and not item.is_separator)
        if item.is_separator:
            safe_addstr(stdscr, line, inner_x,
                        ("  " + "─" * max(1, inner_w - 4)
                         + "  ").ljust(inner_w),
                        sb | curses.A_DIM)
            line += 1
            rendered += 1
            continue
        # Column 0 caret / focus arrow.
        if item.has_submenu:
            col0 = "› "
        elif focused:
            col0 = "→ "
        else:
            col0 = "  "
        label = item.label
        if not item.enabled and item.reason:
            label = f"{label}  ({item.reason})"
        # Attribute selection — back rows render as dim cyan
        # breadcrumb-style, regardless of state.
        if item.is_back:
            attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_DIM
            if focused:
                attr |= curses.A_REVERSE
        elif focused and item.enabled:
            attr = sb | curses.A_REVERSE
        elif focused:
            attr = sb | curses.A_REVERSE | curses.A_DIM
        elif not item.enabled:
            attr = sb | curses.A_DIM
        else:
            attr = sb
        inline_editing = (focused and bool(menu.edit_field)
                          and item.id == menu.edit_target_id)
        is_remote_row = item.id.startswith("remote:")
        if is_remote_row:
            # Two-column render: name on the left, URL on the right.
            # Width auto-fits the longest cached remote name (capped
            # at half the row); the URL column takes the rest.
            remote_name = item.id.split(":", 1)[1]
            remote_url = ""
            for n, u in menu.remotes_list:
                if n == remote_name:
                    remote_url = u
                    break
            name_w = max(8, max(
                (len(n) for n, _ in menu.remotes_list), default=8))
            name_w = min(name_w, max(8, (inner_w - 4) // 2))
            sep = "  "
            name_x = inner_x + len(col0)
            url_x = name_x + name_w + len(sep)
            url_w = max(1, inner_w - (url_x - inner_x))
            # Background paint for the whole row first so attrs
            # stay contiguous when reverse-video kicks in.
            safe_addstr(stdscr, line, inner_x, " " * inner_w, attr)
            safe_addstr(stdscr, line, inner_x, col0, attr)
            # Determine per-column display + per-column attrs based
            # on which (if any) field is being edited inline. The
            # actively-edited cell carries a real terminal cursor —
            # see _place_inline_cursor below for placement.
            edit_cell_x = -1
            if inline_editing and menu.edit_field == "rename_remote":
                name_text = menu.edit_typed
                name_attr = sb | curses.A_REVERSE
                url_text = remote_url
                url_attr = attr
                edit_cell_x = name_x
            elif inline_editing and menu.edit_field == "set_url_remote":
                name_text = remote_name
                name_attr = attr
                url_text = menu.edit_typed
                url_attr = sb | curses.A_REVERSE
                edit_cell_x = url_x
            else:
                name_text = remote_name
                url_text = remote_url
                name_attr = attr
                url_attr = attr
            name_render = (name_text if inline_editing
                           and edit_cell_x == name_x
                           else end_truncate(name_text, name_w))
            url_render = (url_text if inline_editing
                          and edit_cell_x == url_x
                          else end_truncate(url_text, url_w))
            # Apply scroll-offset truncation on the active edit cell so
            # the cursor stays visible when the buffer outgrows the
            # column width.
            if inline_editing and edit_cell_x == name_x:
                name_render, edit_cur_off = _scroll_for_cursor(
                    name_text, menu.edit_cursor, name_w)
            elif inline_editing and edit_cell_x == url_x:
                url_render, edit_cur_off = _scroll_for_cursor(
                    url_text, menu.edit_cursor, url_w)
            else:
                edit_cur_off = 0
            safe_addstr(stdscr, line, name_x,
                        name_render.ljust(name_w), name_attr)
            safe_addstr(stdscr, line, url_x,
                        url_render.ljust(url_w), url_attr)
            if inline_editing:
                _place_inline_cursor(
                    stdscr, line, edit_cell_x + edit_cur_off)
        elif inline_editing:
            # Non-remote inline edits (add_remote_name /
            # add_remote_url) replace the whole row label with the
            # editable buffer cell. A real terminal cursor lands at
            # menu.edit_cursor — see _place_inline_cursor.
            if menu.edit_field == "add_remote_name":
                prefix_label = "name: "
            elif menu.edit_field == "add_remote_url":
                prefix_label = "url: "
            else:
                prefix_label = ""
            buf_w = max(1, inner_w - len(col0) - len(prefix_label))
            visible, cur_off = _scroll_for_cursor(
                menu.edit_typed, menu.edit_cursor, buf_w)
            cell = prefix_label + visible
            full = (col0 + cell).ljust(inner_w)
            safe_addstr(stdscr, line, inner_x, full[:inner_w],
                        sb | curses.A_REVERSE)
            _place_inline_cursor(
                stdscr, line,
                inner_x + len(col0) + len(prefix_label) + cur_off)
        else:
            # Whole-row paint so reverse-video stays contiguous,
            # then overlay the caret in its own attr when the row
            # isn't focused — caret stays dim cyan against the
            # dim/normal row, and the focused row's reverse-video
            # still reads cleanly.
            full = (col0 + label).ljust(inner_w)
            safe_addstr(stdscr, line, inner_x, full[:inner_w], attr)
            if item.has_submenu and not focused:
                caret_attr = (curses.color_pair(PAIR_DLG_CYAN)
                              | curses.A_DIM)
                safe_addstr(stdscr, line, inner_x, "›", caret_attr)
        line += 1
        rendered += 1
    # Pad any remaining reserved rows so the layout below the action
    # items doesn't shift between main and submenu views.
    for _ in range(n_items - rendered):
        safe_addstr(stdscr, line, inner_x, " " * inner_w, sb)
        line += 1

    # Bottom-pane separator
    safe_addstr(stdscr, line, inner_x, "─" * inner_w, sb | curses.A_DIM)
    line += 1

    # Tab header
    _draw_tab_header(stdscr, line, inner_x, inner_w, menu, state, sb)
    line += 1

    # Compute pane size: whatever's left between current line and the
    # footer hint row, capped at PANE_TARGET_ROWS + 1 (filter row).
    footer_y = y + h - 2
    pane_total = max(2, footer_y - line - 1)
    list_rows = pane_total - 1  # filter takes the first row

    _draw_pane(stdscr, line, inner_x, inner_w, list_rows, menu, state, sb)

    _draw_action_hints(stdscr, menu, footer_y, inner_x, inner_w,
                       sb | curses.A_DIM)

    # No inline edit active → make sure the terminal cursor stays
    # hidden. _place_inline_cursor turns it back on (and positions
    # it) inside the per-item draw when an edit is in progress.
    if not menu.edit_field:
        try:
            curses.curs_set(0)
        except curses.error:
            pass


def _draw_tab_header(stdscr, line: int, inner_x: int, inner_w: int,
                     menu: ActionMenu, state: State, sb: int) -> None:
    """Render the [ Working tree ] [ Recent commits ] tabs. Active tab
    gets cyan + bold; inactive is dim. When `pane_focus` is False the
    whole header drops a tone so the user can tell the action items
    have focus, not the pane.

    While the initial query for a tab is still in flight, that tab's
    count column shows a spinner instead of a number — keeps the
    label stable but tells the user the figure isn't final yet."""
    tree_count = (_spinner_glyph(state) if _tree_loading(state, menu)
                  else str(len(menu.tree_files)))
    commits_count = (_spinner_glyph(state)
                     if (_commits_loading(state, menu)
                         and not menu.commits_full)
                     else str(len(menu.commits_full)))
    tabs = [("tree", "Working tree", tree_count),
            ("commits", "Recent commits", commits_count)]
    cur_x = inner_x
    for tid, label, count in tabs:
        active = (menu.pane_tab == tid)
        text = f" {label} ({count}) "
        if active and menu.pane_focus:
            attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
        elif active:
            attr = sb | curses.A_BOLD
        else:
            attr = sb | curses.A_DIM
        safe_addstr(stdscr, line, cur_x, text, attr)
        cur_x += len(text) + 1
        if cur_x >= inner_x + inner_w:
            break
    # Loading hint for commits paging — only when we're paging on top
    # of an already-populated list. The empty-list case shows its own
    # "loading…" centered in the pane via _draw_commits_pane.
    if _commits_loading(state, menu) and menu.commits_full:
        msg = f"  {_spinner_glyph(state)} loading more"
        safe_addstr(stdscr, line, inner_x + inner_w - len(msg),
                    msg, sb | curses.A_DIM)


def _draw_pane(stdscr, line: int, inner_x: int, inner_w: int,
               list_rows: int, menu: ActionMenu, state: State,
               sb: int) -> None:
    """Render the filter row + filtered list for the active tab."""
    if menu.pane_tab == "tree":
        _draw_tree_pane(stdscr, line, inner_x, inner_w, list_rows,
                        menu, state, sb)
    else:
        _draw_commits_pane(stdscr, line, inner_x, inner_w, list_rows,
                           menu, state, sb)


def _draw_filter_row(stdscr, line: int, inner_x: int, inner_w: int,
                     filter_text: str, focused: bool, sb: int) -> None:
    """One-line filter input prefixed with a magnifier glyph."""
    icon = "🔍 "
    typed = filter_text or ""
    if focused:
        # Reverse-video the filter row + show a `_` cursor.
        body = (typed + "_").ljust(inner_w - len(icon))
        attr = sb | curses.A_REVERSE
    elif typed:
        body = typed.ljust(inner_w - len(icon))
        attr = sb
    else:
        body = "filter…".ljust(inner_w - len(icon))
        attr = sb | curses.A_DIM
    safe_addstr(stdscr, line, inner_x, icon, sb | curses.A_DIM)
    safe_addstr(stdscr, line, inner_x + len(icon), body[:inner_w - len(icon)], attr)


def _draw_tree_pane(stdscr, line: int, inner_x: int, inner_w: int,
                    list_rows: int, menu: ActionMenu, state: State,
                    sb: int) -> None:
    filter_focused = menu.pane_focus and menu.tree_selected == 0
    _draw_filter_row(stdscr, line, inner_x, inner_w,
                     menu.tree_filter, filter_focused, sb)
    line += 1

    # Initial-load placeholder. tree_loading stays True until
    # query_working_tree finishes; until then we show a spinner +
    # "loading working tree…" centred on the first list row so the
    # user sees Tab landed and the data is on its way.
    if _tree_loading(state, menu) and not menu.tree_files:
        safe_addstr(stdscr, line, inner_x + 2,
                    f"{_spinner_glyph(state)} loading working tree…",
                    sb | curses.A_DIM)
        return

    files = _filtered_tree(menu)
    if not files:
        msg = "(no changes)" if not menu.tree_filter else "(no matches)"
        safe_addstr(stdscr, line, inner_x + 2, msg, sb | curses.A_DIM)
        return

    # Selection-aware scroll: keep the selected row in view.
    sel_in_list = max(0, menu.tree_selected - 1)
    if sel_in_list < menu.tree_scroll:
        menu.tree_scroll = sel_in_list
    elif sel_in_list >= menu.tree_scroll + list_rows:
        menu.tree_scroll = sel_in_list - list_rows + 1
    if menu.tree_scroll > max(0, len(files) - list_rows):
        menu.tree_scroll = max(0, len(files) - list_rows)

    visible = files[menu.tree_scroll:menu.tree_scroll + list_rows]
    for i, fe in enumerate(visible):
        idx = menu.tree_scroll + i
        focused = (menu.pane_focus and menu.tree_selected == idx + 1)
        _draw_tree_row(stdscr, line + i, inner_x, inner_w, fe, focused, sb)


def _draw_tree_row(stdscr, y: int, x: int, inner_w: int, fe: FileEntry,
                   focused: bool, sb: int) -> None:
    """Render one working-tree row with pastel overlays on the status
    code and the +ins / -del numbers. The row is laid down first as a
    single-attr base (so reverse-video for the focused row stays
    contiguous), then the colored segments are over-painted in place
    when not focused."""
    code = "??" if fe.untracked else f"{fe.x}{fe.y}"
    stat_ins = f"+{fe.inserted}" if (fe.inserted or fe.deleted) else ""
    stat_del = f"-{fe.deleted}" if (fe.inserted or fe.deleted) else ""
    stat = f"{stat_ins} {stat_del}".strip()
    left = f" {code}  "                       # 5 chars: " XY  "
    pad = max(1, inner_w - len(left) - len(stat) - 1)
    name = fe.path
    if len(name) > pad:
        name = name[: pad - 1] + "…"
    name = name.ljust(pad)
    full = f"{left}{name} {stat}"

    if focused:
        safe_addstr(stdscr, y, x, full, sb | curses.A_REVERSE)
        return

    base = sb | curses.A_DIM if fe.untracked else sb
    safe_addstr(stdscr, y, x, full, base)

    # Overlay the status code. Untracked stays dim cyan-ish via the
    # base attr; everything else picks a per-status pastel pair.
    if not fe.untracked:
        pair_id = _file_status_pair(fe.x, fe.y)
        if pair_id is not None:
            safe_addstr(stdscr, y, x + 1, code, curses.color_pair(pair_id))

    # Overlay the diff stats — green for "+N", red for "-M".
    if stat:
        stat_x = x + len(left) + pad + 1
        safe_addstr(stdscr, y, stat_x, stat_ins,
                    curses.color_pair(PAIR_DLG_PASTEL_GREEN))
        safe_addstr(stdscr, y, stat_x + len(stat_ins) + 1, stat_del,
                    curses.color_pair(PAIR_DLG_PASTEL_RED))


def _draw_commits_pane(stdscr, line: int, inner_x: int, inner_w: int,
                       list_rows: int, menu: ActionMenu, state: State,
                       sb: int) -> None:
    filter_focused = menu.pane_focus and menu.commits_selected == 0
    _draw_filter_row(stdscr, line, inner_x, inner_w,
                     menu.commits_filter, filter_focused, sb)
    line += 1

    # Initial-load placeholder for commits. commits_loading is reused
    # across initial-load and pagination — the empty-list case here
    # implies the first page is still in flight (paginated loading
    # always has something already in commits_full).
    if _commits_loading(state, menu) and not menu.commits_full:
        safe_addstr(stdscr, line, inner_x + 2,
                    f"{_spinner_glyph(state)} loading commits…",
                    sb | curses.A_DIM)
        return

    commits = _filtered_commits(menu)
    if not commits:
        msg = ("(no commits on this branch yet)"
               if not menu.commits_filter else "(no matches)")
        safe_addstr(stdscr, line, inner_x + 2, msg, sb | curses.A_DIM)
        return

    sel_in_list = max(0, menu.commits_selected - 1)
    if sel_in_list < menu.commits_scroll:
        menu.commits_scroll = sel_in_list
    elif sel_in_list >= menu.commits_scroll + list_rows:
        menu.commits_scroll = sel_in_list - list_rows + 1
    if menu.commits_scroll > max(0, len(commits) - list_rows):
        menu.commits_scroll = max(0, len(commits) - list_rows)

    visible = commits[menu.commits_scroll:menu.commits_scroll + list_rows]
    for i, c in enumerate(visible):
        idx = menu.commits_scroll + i
        focused = (menu.pane_focus and menu.commits_selected == idx + 1)
        _draw_commit_row(stdscr, line + i, inner_x, inner_w, c, focused, sb)

    # Trailing footer when we've walked all the way back.
    if (menu.commits_exhausted and len(visible) < list_rows
            and not menu.commits_filter):
        tail_y = line + len(visible)
        safe_addstr(stdscr, tail_y, inner_x + 2,
                    "(reached root commit)", sb | curses.A_DIM)


def _draw_commit_row(stdscr, y: int, x: int, inner_w: int,
                     c: CommitEntry, focused: bool, sb: int) -> None:
    """Render one commit row with the SHA in pastel-yellow (matching
    git's own --abbrev colour) and the relative date in pastel-blue.
    The subject keeps default fg so it's the visual focus of the row."""
    sha = c.sha
    rel = f"({c.relative})" if c.relative else ""
    head = f"  {sha}  "
    pad = max(1, inner_w - len(head) - len(rel) - 1)
    subj = c.subject
    if len(subj) > pad:
        subj = subj[: pad - 1] + "…"
    subj = subj.ljust(pad)
    full = f"{head}{subj} {rel}"

    if focused:
        safe_addstr(stdscr, y, x, full, sb | curses.A_REVERSE)
        return

    safe_addstr(stdscr, y, x, full, sb)
    # Overlay the SHA (positions 2..2+len(sha)).
    safe_addstr(stdscr, y, x + 2, sha,
                curses.color_pair(PAIR_DLG_PASTEL_YELLOW))
    if rel:
        rel_x = x + len(head) + pad + 1
        safe_addstr(stdscr, y, rel_x, rel,
                    curses.color_pair(PAIR_DLG_PASTEL_BLUE))


# ---------- Handle --------------------------------------------------------


def handle_action_menu_key(state: State, key: int) -> None:
    menu = state.action_menu
    if menu is None:
        return

    # Diff viewer is a sub-modal of the action menu — route all keys to
    # it while it's open; Tab and Esc both close it.
    if state.diff_viewer is not None:
        handle_diff_viewer_key(state, key)
        return

    _apply_action_effect(
        state,
        handle_action_menu_key_intent(state, menu, key),
    )


def _apply_action_effect(state: State, effect: ActionMenuEffect) -> None:
    if effect.kind == "none":
        return
    if effect.kind == "close":
        close_action_menu(state)
        return
    if effect.kind == "branch_picker":
        from features.branch_picker.session import open_branch_picker
        open_branch_picker(state, mode=effect.mode or "switch")
        return
    if effect.kind == "remote_branch_picker":
        from features.remote_branch_picker.session import open_remote_branch_picker
        open_remote_branch_picker(state)
        return
    if effect.kind == "branch_name_prompt":
        from features.branch_name_prompt.session import open_branch_name_prompt
        if effect.mode:
            open_branch_name_prompt(state, mode=effect.mode)
        else:
            open_branch_name_prompt(state)
        return
    if effect.kind == "reset_prompt":
        from features.reset_prompt.session import open_reset_prompt
        open_reset_prompt(state)
        return
    if effect.kind == "workflow_picker":
        from features.workflow_picker.session import open_workflow_picker
        open_workflow_picker(state)
        return
    if effect.kind == "prefetch_commits":
        menu = state.action_menu
        if menu is not None:
            _kick_off_commits_page(state, menu)
        return
    if effect.kind == "diff_viewer":
        menu = state.action_menu
        if menu is not None:
            open_diff_viewer(
                state,
                target_path=menu.target_path,
                label=menu.target_label,
                file_path=effect.file_path,
                untracked=effect.untracked,
            )
        return
    if effect.kind == "commit_view":
        from features.commit_view.session import open_commit_view_modal
        menu = state.action_menu
        if menu is not None:
            open_commit_view_modal(
                state,
                target_path=menu.target_path,
                target_label=menu.target_label,
                sha=effect.sha,
                subject=effect.subject,
            )
        return
