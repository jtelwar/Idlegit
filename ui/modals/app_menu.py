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
from typing import Optional

from core.config import APP_DISPLAY_NAME, VERSION
from core.state.app import State
from features.app_menu.actions import (
    AppMenuEffect,
    handle_app_menu_key as handle_app_menu_key_action,
)
from features.app_menu.projection import (
    ACTION_ADJUST_PERIODIC_REFRESH,
    ACTION_CLEAR_TASK_LOG,
    ACTION_CREATE_SSH_KEY,
    ACTION_CYCLE_AUTO_REMOVE_COMPLETED,
    ACTION_CYCLE_DEBOUNCE,
    ACTION_OPEN_HELP,
    ACTION_OPEN_TASK_LOG,
    ACTION_SSH_ADD_KEYS,
    ACTION_TOGGLE_AUTO_REFRESH,
    ACTION_TOGGLE_SSH_AGENT,
    ACTION_TOGGLE_TASK_LOG,
    ACTION_UPDATE_NOW,
)
from features.app_menu.session import (
    open_app_menu_session,
    tick_app_menu_update_check,  # noqa: F401
)

from ..colors import (
    PAIR_DLG_CYAN, PAIR_DLG_MAGENTA, PAIR_DLG_OK, PAIR_DLG_FG, PAIR_DLG_FG_HINT_TEXT,
)
from ..geometry import (
    clamp_scroll, draw_modal_fill, draw_scroll_overflow, modal_geometry,
    safe_addstr, truncate,
)
from ..hints import (
    KEY_ENTER, KEY_ESC, KEY_LEFT_RIGHT, KEY_UP_DOWN, Hint, render_hints,
)
from ..sidebar import SPINNER_FRAMES


# Modal sizing.
MODAL_W = 70
BODY_TARGET_ROWS = 14  # max rows shown before scroll arrows kick in
_PAD_TOP = 1
_PAD_BOTTOM = 1
_PAD_X = 2


def _spinner_glyph(state: State) -> str:
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


# ---------- Open ----------------------------------------------------------


def open_app_menu(state: State) -> None:
    """Install the global app menu. Cursor lands on the active
    workspace row so Enter is "stay" by default — the most common
    action (just glance at the menu and Esc) requires zero key
    movement."""
    result = open_app_menu_session(state)
    if result.open_workspace_creator:
        from features.workspace_creator.session import open_workspace_creator
        open_workspace_creator(
            state, title="Add workspace",
            intro="Add folder paths to scan for git repos.")


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
            if row.attr_name == ACTION_UPDATE_NOW:
                hints.append(Hint(KEY_ENTER, "exit + run idlegit-update"))
            elif row.attr_name == ACTION_OPEN_TASK_LOG:
                hints.append(Hint(KEY_ENTER, "open in default app"))
            elif row.attr_name == ACTION_CLEAR_TASK_LOG:
                hints.append(Hint(KEY_ENTER, "truncate log file"))
            elif row.attr_name == ACTION_OPEN_HELP:
                hints.append(Hint(KEY_ENTER, "open help"))
            elif row.attr_name == ACTION_TOGGLE_TASK_LOG:
                hints.append(Hint(
                    KEY_ENTER,
                    "disable + save" if state.task_log_enabled
                    else "enable + save"))
            elif row.attr_name == ACTION_TOGGLE_AUTO_REFRESH:
                hints.append(Hint(
                    KEY_ENTER,
                    "disable + save" if state.auto_refresh_on_fs_change
                    else "enable + save"))
            elif row.attr_name == ACTION_CYCLE_DEBOUNCE:
                hints.append(Hint(KEY_ENTER, "cycle preset + save"))
            elif row.attr_name == ACTION_ADJUST_PERIODIC_REFRESH:
                hints.append(Hint(KEY_LEFT_RIGHT, "adjust seconds"))
                hints.append(Hint(KEY_ENTER, "toggle off/default"))
            elif row.attr_name == ACTION_CYCLE_AUTO_REMOVE_COMPLETED:
                hints.append(Hint(KEY_ENTER, "cycle interval + save"))
            elif row.attr_name == ACTION_TOGGLE_SSH_AGENT:
                hints.append(Hint(
                    KEY_ENTER,
                    "disable + save" if state.auto_start_ssh_agent
                    else "enable + save"))
            elif row.attr_name == ACTION_CREATE_SSH_KEY:
                hints.append(Hint(KEY_ENTER, "create keypair"))
            elif row.attr_name == ACTION_SSH_ADD_KEYS:
                hints.append(Hint(KEY_ENTER, "ssh-add default keys"))
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
                hints.append(Hint(KEY_ENTER, f"switch to {ws.display_name}"))
        elif row.kind == "create_workspace":
            hints.append(Hint(KEY_ENTER, "create new workspace…"))
    hints.append(Hint(KEY_ESC, "close"))
    return hints


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
    """One workspace row: name on the left, first folder path on the
    right (middle-truncated when tight). Active workspaces get a •
    prefix and an ``active`` tag when there is room — no folder count."""
    ws = state.workspaces[ws_idx]
    is_active = (ws_idx == state.active_workspace_index)
    prefix = "→ " if focused else ("• " if is_active else "  ")
    active_tag = "active" if is_active else ""
    name_w = max(12, inner_w // 3)
    name_text = truncate(ws.display_name, name_w, "end")
    tag_w = len(active_tag) + (1 if active_tag else 0)
    path_w = max(0, inner_w - len(prefix) - name_w - 2 - tag_w)
    first_path = (truncate(str(ws.folders[0]), path_w, "middle")
                  if ws.folders and path_w > 0 else "")
    line = (f"{prefix}{name_text.ljust(name_w)}  "
            f"{first_path.ljust(path_w)}")
    if active_tag:
        line = f"{line.rstrip()} {active_tag}"
    if focused:
        attr = sb | curses.A_REVERSE
    elif is_active:
        attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
    else:
        attr = sb
    safe_addstr(stdscr, line_y, inner_x,
                line.ljust(inner_w)[:inner_w], attr)
    if not focused and is_active and active_tag:
        meta_x = inner_x + len(prefix) + name_w + 2 + path_w + 1
        safe_addstr(stdscr, line_y, meta_x, active_tag,
                    curses.color_pair(PAIR_DLG_OK))


def draw_app_menu(stdscr, state: State, sidebar_x: int) -> None:
    menu = state.app_menu
    if menu is None:
        return
    n_rows = len(menu.rows)
    title_rows = 1
    blank_after_title = 1
    blank_before_hints = 1
    hint_rows = 1
    desired_body = min(BODY_TARGET_ROWS, max(1, n_rows))
    desired_h = (
        _PAD_TOP + title_rows + blank_after_title + desired_body
        + blank_before_hints + hint_rows + _PAD_BOTTOM
    )
    x, y, w, h = modal_geometry(stdscr, sidebar_x, MODAL_W, desired_h)
    sb = curses.color_pair(PAIR_DLG_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + _PAD_X
    inner_w = max(1, w - 2 * _PAD_X)

    fixed_rows = (
        _PAD_TOP + title_rows + blank_after_title
        + blank_before_hints + hint_rows + _PAD_BOTTOM
    )
    visible_rows = max(1, h - fixed_rows)
    if n_rows > 0:
        visible_rows = min(visible_rows, n_rows)

    list_y = y + _PAD_TOP + title_rows + blank_after_title
    hint_y = y + h - _PAD_BOTTOM - hint_rows
    spacer_down_y = list_y + visible_rows
    scroll_up_y = y + _PAD_TOP + title_rows

    # Title row: app name in magenta, " · vX.Y.Z" in dim cyan —
    # mirrors the main-screen title row so the modal reads as
    # "the same Idlegit, in menu mode" rather than a separate
    # surface with its own branding.
    safe_addstr(stdscr, y + _PAD_TOP, inner_x,
                APP_DISPLAY_NAME[:inner_w],
                curses.A_BOLD | curses.color_pair(PAIR_DLG_MAGENTA))
    name_w = min(len(APP_DISPLAY_NAME), inner_w)
    if name_w < inner_w:
        suffix = f"  v{VERSION}"
        safe_addstr(stdscr, y + _PAD_TOP, inner_x + name_w,
                    suffix[:inner_w - name_w],
                    curses.color_pair(PAIR_DLG_CYAN) | curses.A_DIM)

    menu.scroll = clamp_scroll(menu.selected, menu.scroll, n_rows, visible_rows)

    if menu.scroll > 0:
        draw_scroll_overflow(stdscr, scroll_up_y, inner_x, inner_w,
                             menu.scroll, "up", sb | curses.A_DIM)

    for i in range(visible_rows):
        idx = menu.scroll + i
        if idx >= n_rows:
            break
        row = menu.rows[idx]
        line_y = list_y + i
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

    if menu.scroll + visible_rows < n_rows:
        below = n_rows - (menu.scroll + visible_rows)
        draw_scroll_overflow(stdscr, spacer_down_y, inner_x, inner_w,
                             below, "down", sb | curses.A_DIM)

    render_hints(stdscr, hint_y, inner_x, inner_w, _hints(state),
                 attr=sb | curses.A_DIM)


# ---------- Handle --------------------------------------------------------


def handle_app_menu_key(state: State, key: int) -> None:
    effect = handle_app_menu_key_action(state, key)
    _apply_app_menu_effect(state, effect)


def _apply_app_menu_effect(state: State, effect: AppMenuEffect) -> None:
    if effect.kind in ("none", "close"):
        return
    if effect.kind == "update_now":
        _exec_idlegit_update()
        return
    if effect.kind == "open_help":
        from .help import open_help_screen
        open_help_screen(state)
        return
    if effect.kind == "open_ssh_keygen":
        from features.ssh_keygen.session import open_ssh_keygen_modal
        open_ssh_keygen_modal(state)
        return
    if effect.kind == "open_workspace_creator":
        from features.workspace_creator.session import open_workspace_creator
        open_workspace_creator(
            state, title="Add workspace",
            intro="Add folder paths to scan for git repos.")
        return
