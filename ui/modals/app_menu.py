"""Global app menu — Tab-on-title-row modal. Two top-level sections:

  - APPLICATION: app name + version, Check for updates button, and
    (after a check) the latest-release tag plus an Update now button
    when the installed version is behind. The update check runs in
    a daemon worker so the menu stays responsive while the network
    call is in flight.
  - WORKSPACES: every configured workspace plus a trailing
    "+ Create new workspace…" sentinel that hands off to the
    workspace creator wizard. Enter on a workspace row switches the
    active workspace.

Rows are dynamically rebuilt when the update-check state changes,
the active workspace switches, or the workspaces list mutates."""
from __future__ import annotations

import curses
import os
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple

from core.config import APP_DISPLAY_NAME, VERSION
from core.models import AppMenu, AppMenuRow, State
from core.workers import kick_off_check_for_updates

from ..colors import (
    PAIR_DLG_CYAN, PAIR_DLG_MAGENTA, PAIR_DLG_OK, PAIR_DLG_FG, PAIR_DLG_FG_HINT_TEXT,
)
from ..geometry import (
    clamp_scroll, draw_modal_fill, draw_scroll_overflow, modal_geometry,
    safe_addstr, truncate,
)
from ..hints import (
    KEY_ENTER, KEY_ESC, KEY_UP_DOWN, Hint, render_hints,
)
from ..sidebar import SPINNER_FRAMES


# Action ids carried in `attr_name` on app_action rows. Centralised
# so build / dispatch / hint sites stay aligned via constants rather
# than free-form strings.
_ACTION_CHECK_FOR_UPDATES = "check_for_updates"
_ACTION_UPDATE_NOW = "update_now"


# Modal sizing.
MODAL_W = 70
BODY_TARGET_ROWS = 14  # max rows shown before scroll arrows kick in


# ---------- Version comparison + helpers --------------------------------


def _parse_version_tuple(text: str) -> Tuple[int, ...]:
    """Best-effort `vX.Y.Z[.…]` → tuple of ints. Strips a leading `v`
    and stops at the first non-version char so `1.2.3-rc1` reads as
    (1, 2, 3). Empty on failure — caller treats that as "can't tell"
    and skips the offer-to-update branch."""
    s = text.strip().lstrip("vV")
    head: list = []
    for ch in s:
        if ch.isdigit() or ch == ".":
            head.append(ch)
        else:
            break
    parts = "".join(head).split(".")
    return tuple(int(p) for p in parts if p)


def _is_update_available(installed: str, latest: str) -> bool:
    """True iff `latest` strictly outranks `installed` after parsing."""
    a = _parse_version_tuple(installed)
    b = _parse_version_tuple(latest)
    if not a or not b:
        return False
    return a < b


def _spinner_glyph(state: State) -> str:
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


# ---------- Row building -------------------------------------------------


def _app_section_rows(menu: AppMenu) -> "list[AppMenuRow]":
    """Build the update-check rows for the top of the menu. The app
    name + version live in the modal title (rendered separately by
    `draw_app_menu`), so this section only emits the action /
    info rows tied to the GitHub release fetch:
      - idle:        [Check for updates]
      - checking:    "Checking for updates…" (info)
      - no_releases: "No releases published yet" / [Check again]
      - failed:      error info / [Try again]
      - done:        latest-info /
                     [Update now] when behind, [Check again]
                     otherwise.
    Row count flexes deliberately — the rebuild logic re-anchors
    the cursor by (kind, attr_name) so the focused row stays sane."""
    rows: "list[AppMenuRow]" = []
    if menu.update_check == "idle":
        rows.append(AppMenuRow(
            label="Check for updates",
            attr_name=_ACTION_CHECK_FOR_UPDATES, kind="app_action"))
    elif menu.update_check == "checking":
        rows.append(AppMenuRow(
            label="Checking for updates…",
            attr_name="", kind="app_info"))
    elif menu.update_check == "no_releases":
        # GitHub returns 404 from /releases/latest when the repo
        # has no published releases yet. Surface this softly —
        # there's nothing wrong with the user's setup, the upstream
        # just hasn't cut a release.
        rows.append(AppMenuRow(
            label="No releases published yet",
            attr_name="", kind="app_info"))
        rows.append(AppMenuRow(
            label="Check again",
            attr_name=_ACTION_CHECK_FOR_UPDATES, kind="app_action"))
    elif menu.update_check == "failed":
        err = (menu.update_check_error or "unknown error").strip()
        rows.append(AppMenuRow(
            label=f"Check failed: {err}",
            attr_name="", kind="app_info"))
        rows.append(AppMenuRow(
            label="Try again",
            attr_name=_ACTION_CHECK_FOR_UPDATES, kind="app_action"))
    elif menu.update_check == "done":
        latest = menu.latest_version or "?"
        if _is_update_available(VERSION, latest):
            rows.append(AppMenuRow(
                label=f"Update available: {latest}",
                attr_name="", kind="app_info"))
            rows.append(AppMenuRow(
                label="Update now",
                attr_name=_ACTION_UPDATE_NOW, kind="app_action"))
        else:
            rows.append(AppMenuRow(
                label=f"Up to date (latest: {latest})",
                attr_name="", kind="app_info"))
            rows.append(AppMenuRow(
                label="Check again",
                attr_name=_ACTION_CHECK_FOR_UPDATES, kind="app_action"))
    return rows


def _workspaces_section_rows(state: State) -> "list[AppMenuRow]":
    """WORKSPACES section: header, one workspace row per configured
    workspace (the workspace's index lives in attr_name as a string
    so the dispatcher can switch by index), and a trailing
    "+ Create new workspace…" sentinel that opens the creator."""
    rows: "list[AppMenuRow]" = [
        AppMenuRow(label="WORKSPACES", attr_name="", kind="header"),
    ]
    for i, ws in enumerate(state.workspaces):
        rows.append(AppMenuRow(
            label=ws.name, attr_name=str(i), kind="workspace"))
    rows.append(AppMenuRow(
        label="+ Create new workspace…",
        attr_name="", kind="create_workspace"))
    return rows


def _build_rows(state: State, menu: AppMenu) -> "list[AppMenuRow]":
    """Compose the full row list — update-check rows at the top,
    one blank spacer row, then the WORKSPACES section. Both
    sections are rebuilt fresh on every call so changes to either
    side surface without side-effecting state mutations elsewhere."""
    rows = _app_section_rows(menu)
    if rows:
        rows.append(AppMenuRow(label="", attr_name="", kind="spacer"))
    rows.extend(_workspaces_section_rows(state))
    return rows


def _is_focusable(row: AppMenuRow) -> bool:
    """Headers, spacer rows, and app_info rows are read-only chrome;
    the cursor skips over them so navigation lands on something
    interactive."""
    return row.kind not in ("header", "spacer", "app_info")


def _first_focusable(rows: "list[AppMenuRow]") -> int:
    for i, row in enumerate(rows):
        if _is_focusable(row):
            return i
    return 0


def _rebuild_rows(state: State) -> None:
    """Re-derive the row list and keep the cursor on a sensible row.
    Tries to land on a row matching the previous (kind, attr_name)
    pair so the focused workspace stays focused after an update-
    check transition rebuilds the APPLICATION section above it.
    Falls back to the first focusable row when no match exists
    (e.g. a workspace was deleted between rebuilds)."""
    menu = state.app_menu
    if menu is None:
        return
    old_kind = ""
    old_attr = ""
    if 0 <= menu.selected < len(menu.rows):
        old_kind = menu.rows[menu.selected].kind
        old_attr = menu.rows[menu.selected].attr_name
    menu.rows = _build_rows(state, menu)
    new_idx = -1
    for i, row in enumerate(menu.rows):
        if row.kind == old_kind and row.attr_name == old_attr:
            new_idx = i
            break
    if new_idx == -1:
        new_idx = _first_focusable(menu.rows)
    menu.selected = new_idx


# ---------- Open ----------------------------------------------------------


def open_app_menu(state: State) -> None:
    """Install the global app menu. Cursor lands on the active
    workspace row so Enter is "stay" by default — the most common
    action (just glance at the menu and Esc) requires zero key
    movement."""
    if not state.workspaces:
        # Without any workspaces there's nothing to land on — defer
        # to the creator wizard directly.
        from .workspace_creator import open_workspace_creator
        open_workspace_creator(
            state, title="Add workspace",
            intro="Add folder paths to scan for git repos.")
        return
    menu = AppMenu()
    menu.rows = _build_rows(state, menu)
    # Default selection: the active workspace's row in the
    # WORKSPACES section so Enter is a no-op "stay".
    target_attr = str(state.active_workspace_index)
    selected = _first_focusable(menu.rows)
    for i, row in enumerate(menu.rows):
        if row.kind == "workspace" and row.attr_name == target_attr:
            selected = i
            break
    menu.selected = selected
    state.app_menu = menu


# ---------- Hints --------------------------------------------------------


def _hints(state) -> list:
    """Footer hints reflect the focused row's available actions."""
    menu = state.app_menu
    if menu is None:
        return []
    hints: list = [Hint(KEY_UP_DOWN, "select")]
    if 0 <= menu.selected < len(menu.rows):
        row = menu.rows[menu.selected]
        if row.kind == "app_action":
            if row.attr_name == _ACTION_UPDATE_NOW:
                hints.append(Hint(KEY_ENTER, "exit + run idlegit-update"))
            else:
                hints.append(Hint(KEY_ENTER, "check for updates"))
        elif row.kind == "workspace":
            try:
                ws_idx = int(row.attr_name)
            except ValueError:
                ws_idx = -1
            if ws_idx == state.active_workspace_index:
                hints.append(Hint(KEY_ENTER, "stay (already active)"))
            elif 0 <= ws_idx < len(state.workspaces):
                ws = state.workspaces[ws_idx]
                hints.append(Hint(KEY_ENTER, f"switch to {ws.name}"))
        elif row.kind == "create_workspace":
            hints.append(Hint(KEY_ENTER, "create new workspace…"))
    hints.append(Hint(KEY_ESC, "close"))
    return hints


# ---------- Update check tick + action dispatch -------------------------


def tick_app_menu_update_check(state: State) -> bool:
    """Sync the menu's row list with the latest update-check state.
    The fetch worker writes back onto the AppMenu directly; this
    tick rebuilds rows whenever `update_check` drifts from the
    value last baked in (`update_check_rendered`), so a
    `checking → done` transition surfaces immediately. Returns
    True iff the worker is still in flight, so the main loop keeps
    the spinner ticking."""
    menu = state.app_menu
    if menu is None:
        return False
    if menu.update_check != menu.update_check_rendered:
        menu.update_check_rendered = menu.update_check
        _rebuild_rows(state)
    return menu.update_check == "checking"


def _fire_app_action(state: State, action_id: str) -> None:
    """Dispatch on the focused `app_action` row's id. Centralised so
    the build / dispatch layers stay aligned via the `_ACTION_*`
    constants — adding a new action is one branch here plus a row
    in `_app_section_rows`."""
    menu = state.app_menu
    if menu is None:
        return
    if action_id == _ACTION_CHECK_FOR_UPDATES:
        kick_off_check_for_updates(menu)
        _rebuild_rows(state)
        return
    if action_id == _ACTION_UPDATE_NOW:
        _exec_idlegit_update()


def _exec_idlegit_update() -> None:
    """Tear down the curses session and replace this process with
    `idlegit-update -y`. Resolution order: PATH first, then
    `scripts/idlegit-update` next to argv[0] for the dev-tree case.
    Falls through silently if neither is found — caller redraws,
    so the user sees the same menu (no half-state limbo)."""
    target: Optional[str] = shutil.which("idlegit-update")
    if target is None:
        try:
            launcher_dir = Path(sys.argv[0]).resolve().parent
            candidate = launcher_dir / "scripts" / "idlegit-update"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                target = str(candidate)
        except OSError:
            pass
    if target is None:
        return
    try:
        curses.endwin()
    except curses.error:
        pass
    os.execvp(target, [target, "-y"])


# ---------- Draw ---------------------------------------------------------


def _draw_workspace_row(stdscr, line_y: int, inner_x: int, inner_w: int,
                        state: State, ws_idx: int, focused: bool,
                        sb: int) -> None:
    """One workspace row: name on the left, dim path summary in the
    middle, "active · N folder(s)" tag on the right. Mirrors how the
    old picker rendered each row so the visual layout the user is
    used to is preserved inside the new global menu."""
    ws = state.workspaces[ws_idx]
    is_active = (ws_idx == state.active_workspace_index)
    prefix = "→ " if focused else ("• " if is_active else "  ")
    name_w = max(12, inner_w // 3)
    name_text = truncate(ws.name, name_w, "end")
    n_folders = len(ws.folders)
    meta = (f"{n_folders} folder" if n_folders == 1
            else f"{n_folders} folders")
    if is_active:
        meta = "active · " + meta
    path_w = max(0, inner_w - len(prefix) - name_w - 2 - len(meta) - 1)
    first_path = (truncate(str(ws.folders[0]), path_w, "middle")
                  if ws.folders and path_w > 0 else "")
    line = f"{prefix}{name_text.ljust(name_w)}  {first_path.ljust(path_w)} {meta}"
    if focused:
        attr = sb | curses.A_REVERSE
    elif is_active:
        attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
    else:
        attr = sb
    safe_addstr(stdscr, line_y, inner_x,
                line.ljust(inner_w)[:inner_w], attr)
    # Overlay the meta segment in PAIR_DLG_OK when the row is the active
    # workspace and unfocused — the cyan-bold attr above colours the
    # whole line, but the active marker reads more clearly in green.
    if not focused and is_active:
        meta_x = inner_x + len(prefix) + name_w + 2 + path_w + 1
        safe_addstr(stdscr, line_y, meta_x, meta,
                    curses.color_pair(PAIR_DLG_OK))


def draw_app_menu(stdscr, state: State, sidebar_x: int) -> None:
    menu = state.app_menu
    if menu is None:
        return
    n_rows = len(menu.rows)
    body_h = max(3, min(BODY_TARGET_ROWS, n_rows))
    # blank-top (1) + title (1) + spacer/scroll-↑ (1)
    # + body + spacer/scroll-↓ (1) + footer (1) + blank-bottom (1).
    # The spacer-row directly under the title is also where the
    # scroll-↑ indicator lands; one row is enough separation.
    content_h = 1 + 1 + 1 + body_h + 1 + 1 + 1
    x, y, w, h = modal_geometry(stdscr, sidebar_x, MODAL_W, content_h)
    sb = curses.color_pair(PAIR_DLG_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    inner_w = w - 4

    # Title row: app name in magenta, " · vX.Y.Z" in dim cyan —
    # mirrors the main-screen title row so the modal reads as
    # "the same Idlegit, in menu mode" rather than a separate
    # surface with its own branding.
    safe_addstr(stdscr, y + 1, inner_x,
                APP_DISPLAY_NAME[:inner_w],
                curses.A_BOLD | curses.color_pair(PAIR_DLG_MAGENTA))
    name_w = min(len(APP_DISPLAY_NAME), inner_w)
    if name_w < inner_w:
        suffix = f"  v{VERSION}"
        safe_addstr(stdscr, y + 1, inner_x + name_w,
                    suffix[:inner_w - name_w],
                    curses.color_pair(PAIR_DLG_CYAN) | curses.A_DIM)

    menu.scroll = clamp_scroll(menu.selected, menu.scroll, n_rows, body_h)

    for i in range(body_h):
        idx = menu.scroll + i
        if idx >= n_rows:
            break
        row = menu.rows[idx]
        line_y = y + 3 + i
        focused = (idx == menu.selected)

        if row.kind == "header":
            safe_addstr(stdscr, line_y, inner_x,
                        row.label.ljust(inner_w)[:inner_w],
                        curses.color_pair(PAIR_DLG_CYAN) | curses.A_DIM)
            continue

        if row.kind == "spacer":
            # Visual breather between sections — same width as the
            # other rows but no glyphs. The modal background is
            # already painted by `draw_modal_fill`, so explicitly
            # writing nothing is enough.
            continue

        if row.kind == "app_info":
            label = row.label
            if (menu.update_check == "checking"
                    and label.startswith("Checking")):
                label = f"{_spinner_glyph(state)} {label}"
            safe_addstr(stdscr, line_y, inner_x,
                        ("  " + label).ljust(inner_w)[:inner_w],
                        curses.color_pair(PAIR_DLG_FG_HINT_TEXT))
            continue

        if row.kind == "app_action":
            prefix = "→ " if focused else "  "
            text = (prefix + row.label).ljust(inner_w)[:inner_w]
            attr = (curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
                    if focused else
                    curses.color_pair(PAIR_DLG_CYAN) | curses.A_DIM)
            if focused:
                attr |= curses.A_REVERSE
            safe_addstr(stdscr, line_y, inner_x, text, attr)
            continue

        if row.kind == "create_workspace":
            label = "  " + row.label
            if focused:
                attr = (curses.color_pair(PAIR_DLG_CYAN)
                        | curses.A_BOLD | curses.A_REVERSE)
            else:
                attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
            safe_addstr(stdscr, line_y, inner_x,
                        label.ljust(inner_w)[:inner_w], attr)
            continue

        if row.kind == "workspace":
            try:
                ws_idx = int(row.attr_name)
            except ValueError:
                continue
            if 0 <= ws_idx < len(state.workspaces):
                _draw_workspace_row(stdscr, line_y, inner_x, inner_w,
                                    state, ws_idx, focused, sb)
            continue

    if menu.scroll > 0:
        draw_scroll_overflow(stdscr, y + 2, inner_x, inner_w,
                             menu.scroll, "up", sb | curses.A_DIM)
    if menu.scroll + body_h < n_rows:
        below = n_rows - (menu.scroll + body_h)
        draw_scroll_overflow(stdscr, y + 3 + body_h, inner_x, inner_w,
                             below, "down", sb | curses.A_DIM)

    render_hints(stdscr, y + h - 2, inner_x, w - 4, _hints(state),
                 attr=sb | curses.A_DIM)


# ---------- Handle --------------------------------------------------------


def _move_selected(menu: AppMenu, direction: int) -> None:
    """Move `selected` by `direction` (±1, ±10) skipping over rows
    that aren't focusable. Stops at the ends without wrapping."""
    n = len(menu.rows)
    if n == 0:
        return
    new = menu.selected
    step = 1 if direction > 0 else -1
    remaining = abs(direction)
    while remaining > 0:
        candidate = new + step
        if not (0 <= candidate < n):
            break
        new = candidate
        if _is_focusable(menu.rows[new]):
            remaining -= 1
    menu.selected = new


def handle_app_menu_key(state: State, key: int) -> None:
    menu = state.app_menu
    if menu is None:
        return

    if key in (27, 9):  # Esc or Tab — close the modal
        state.app_menu = None
        return

    n = len(menu.rows)
    if n == 0:
        return

    if key == curses.KEY_UP:
        _move_selected(menu, -1)
        return
    if key == curses.KEY_DOWN:
        _move_selected(menu, +1)
        return
    if key == curses.KEY_PPAGE:
        _move_selected(menu, -10)
        return
    if key == curses.KEY_NPAGE:
        _move_selected(menu, +10)
        return
    if key == curses.KEY_HOME:
        menu.selected = _first_focusable(menu.rows)
        return
    if key == curses.KEY_END:
        # Walk back from the last row to the last focusable.
        for i in range(n - 1, -1, -1):
            if _is_focusable(menu.rows[i]):
                menu.selected = i
                break
        return

    if key in (10, 13, curses.KEY_ENTER, ord(" ")):
        if not (0 <= menu.selected < n):
            return
        row = menu.rows[menu.selected]
        if row.kind == "app_action":
            _fire_app_action(state, row.attr_name)
            return
        if row.kind == "create_workspace":
            from .workspace_creator import open_workspace_creator
            open_workspace_creator(
                state, title="Add workspace",
                intro="Add folder paths to scan for git repos.")
            return
        if row.kind == "workspace":
            try:
                target = int(row.attr_name)
            except ValueError:
                return
            state.app_menu = None
            if (0 <= target < len(state.workspaces)
                    and target != state.active_workspace_index):
                from core.workers import switch_workspace
                switch_workspace(state, target)
            return
