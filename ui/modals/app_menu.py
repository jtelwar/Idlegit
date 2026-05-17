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
_ACTION_OPEN_TASK_LOG = "open_task_log"
_ACTION_CLEAR_TASK_LOG = "clear_task_log"
_ACTION_TOGGLE_TASK_LOG = "toggle_task_log"
_ACTION_TOGGLE_AUTO_REFRESH = "toggle_auto_refresh"
_ACTION_CYCLE_DEBOUNCE = "cycle_auto_refresh_debounce"
_ACTION_OPEN_HELP = "open_help"

# Preset debounce values the menu cycles through. The default config
# value (400 ms) sits in the middle; jumping to a longer setting helps
# on noisy trees with heavy build artifact churn, jumping shorter buys
# snappier feedback on quiet repos.
_DEBOUNCE_PRESETS_MS = (200, 400, 800, 1500)


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
    # "Help" sits at the bottom of the APPLICATION section regardless
    # of the update-check state — it's always available, and grouping
    # it here keeps the related "application-level" rows together
    # before the spacer + WORKSPACES section.
    rows.append(AppMenuRow(
        label="Help",
        attr_name=_ACTION_OPEN_HELP, kind="app_action"))
    return rows


def _auto_refresh_section_rows(state: State) -> "list[AppMenuRow]":
    """AUTO REFRESH section: toggle row for the fs-watch feature plus a
    debounce-cycle row when the feature is on. The debounce row hides
    when the feature is off so the user isn't tweaking a parameter that
    doesn't apply — re-enabling brings it back."""
    on = state.auto_refresh_on_fs_change
    toggle_label = (
        "Disable filesystem auto-refresh" if on
        else "Enable filesystem auto-refresh")
    rows: "list[AppMenuRow]" = [
        AppMenuRow(label="AUTO REFRESH", attr_name="", kind="header"),
        AppMenuRow(label=toggle_label,
                   attr_name=_ACTION_TOGGLE_AUTO_REFRESH, kind="app_action"),
    ]
    if on:
        rows.append(AppMenuRow(
            label=f"Debounce: {state.auto_refresh_debounce_ms} ms",
            attr_name=_ACTION_CYCLE_DEBOUNCE, kind="app_action"))
    return rows


def _task_logging_section_rows(state: State) -> "list[AppMenuRow]":
    """TASK LOGGING section: read-only status / path / size rows plus
    Open + Clear actions. Path editing is intentionally not surfaced
    here — the user edits `task_log_path` in idlegit.conf and restarts
    (the loader path is one-shot at startup, mirroring the rest of the
    global config). Size is computed on each build_rows so the value
    stays current as workers write more lines."""
    from core.task_log import (
        format_size, task_log_line_count, task_log_size_bytes,
    )

    path_text = str(state.task_log_path)
    size_bytes = task_log_size_bytes(state.task_log_path)
    if size_bytes <= 0:
        size_text = "0 B (empty)"
    else:
        lines = task_log_line_count(state.task_log_path)
        size_text = f"{format_size(size_bytes)} ({lines:,} lines)"
    cap = state.task_log_max_lines
    cap_text = "unlimited" if cap <= 0 else f"{cap:,} lines"
    # The toggle row is the section's primary action — the label
    # describes the next state the action lands in ("Enable…" when
    # off, "Disable…" when on), so a dedicated "Enabled: yes/no"
    # info row would just duplicate the same signal.
    toggle_label = (
        "Disable task logging" if state.task_log_enabled
        else "Enable task logging")
    rows: "list[AppMenuRow]" = [
        AppMenuRow(label="TASK LOGGING", attr_name="", kind="header"),
        AppMenuRow(label=toggle_label,
                   attr_name=_ACTION_TOGGLE_TASK_LOG, kind="app_action"),
        AppMenuRow(label=f"Path: {path_text}",
                   attr_name="", kind="app_info"),
        AppMenuRow(label=f"Size: {size_text}",
                   attr_name="", kind="app_info"),
        AppMenuRow(label=f"Max lines: {cap_text}",
                   attr_name="", kind="app_info"),
        AppMenuRow(label="Open log file",
                   attr_name=_ACTION_OPEN_TASK_LOG, kind="app_action"),
        AppMenuRow(label="Clear log contents",
                   attr_name=_ACTION_CLEAR_TASK_LOG, kind="app_action"),
    ]
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
            label=ws.display_name, attr_name=str(i), kind="workspace"))
    rows.append(AppMenuRow(
        label="+ Create new workspace…",
        attr_name="", kind="create_workspace"))
    return rows


def _build_rows(state: State, menu: AppMenu) -> "list[AppMenuRow]":
    """Compose the full row list — update-check rows at the top, then
    WORKSPACES (the main use-case for opening this menu), then
    TASK LOGGING at the bottom (occasional diagnostic surface). Each
    section is rebuilt fresh on every call so changes (update-check
    transitions, workspace add/remove, log size growth) surface
    without side-effecting state mutations elsewhere."""
    rows = _app_section_rows(menu)
    if rows:
        rows.append(AppMenuRow(label="", attr_name="", kind="spacer"))
    rows.extend(_workspaces_section_rows(state))
    rows.append(AppMenuRow(label="", attr_name="", kind="spacer"))
    rows.extend(_auto_refresh_section_rows(state))
    rows.append(AppMenuRow(label="", attr_name="", kind="spacer"))
    rows.extend(_task_logging_section_rows(state))
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
            elif row.attr_name == _ACTION_OPEN_TASK_LOG:
                hints.append(Hint(KEY_ENTER, "open in default app"))
            elif row.attr_name == _ACTION_CLEAR_TASK_LOG:
                hints.append(Hint(KEY_ENTER, "truncate log file"))
            elif row.attr_name == _ACTION_OPEN_HELP:
                hints.append(Hint(KEY_ENTER, "open help"))
            elif row.attr_name == _ACTION_TOGGLE_TASK_LOG:
                hints.append(Hint(
                    KEY_ENTER,
                    "disable + save" if state.task_log_enabled
                    else "enable + save"))
            elif row.attr_name == _ACTION_TOGGLE_AUTO_REFRESH:
                hints.append(Hint(
                    KEY_ENTER,
                    "disable + save" if state.auto_refresh_on_fs_change
                    else "enable + save"))
            elif row.attr_name == _ACTION_CYCLE_DEBOUNCE:
                hints.append(Hint(KEY_ENTER, "cycle preset + save"))
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
        return
    if action_id == _ACTION_OPEN_TASK_LOG:
        _fire_open_task_log(state)
        return
    if action_id == _ACTION_CLEAR_TASK_LOG:
        _fire_clear_task_log(state)
        return
    if action_id == _ACTION_TOGGLE_TASK_LOG:
        _fire_toggle_task_log(state)
        return
    if action_id == _ACTION_TOGGLE_AUTO_REFRESH:
        _fire_toggle_auto_refresh(state)
        return
    if action_id == _ACTION_CYCLE_DEBOUNCE:
        _fire_cycle_debounce(state)
        return
    if action_id == _ACTION_OPEN_HELP:
        # Close the app menu first so the help screen owns the modal
        # stack — opening it on top of the menu would leave the
        # menu's row chrome bleeding through underneath the help
        # panes on tight terminal geometries.
        from .help import open_help_screen
        state.app_menu = None
        open_help_screen(state)
        return


def _fire_toggle_task_log(state: State) -> None:
    """Flip `state.task_log_enabled` and persist the change to
    idlegit.conf so it survives a restart. Wires / unwires the sink on
    `state.tasks.on_finished` to match — enabling also touches the log
    file so the very next "Open log file" lands on something real,
    rather than reporting "does not exist yet" until the first task
    finishes."""
    from core.config import set_conf_value
    from core.task_log import unwire_task_log, wire_task_log

    new_enabled = not state.task_log_enabled
    state.task_log_enabled = new_enabled
    if new_enabled:
        wire_task_log(state)
    else:
        unwire_task_log(state)

    t = state.tasks.add(
        "enable task logging" if new_enabled else "disable task logging")
    if set_conf_value("task_log_enabled",
                       "true" if new_enabled else "false"):
        state.tasks.update(
            t, "ok",
            "logging on" if new_enabled else "logging off")
    else:
        state.tasks.update(
            t, "warn",
            "applied but conf write failed — won't persist across restart")
    _rebuild_rows(state)


def _fire_toggle_auto_refresh(state: State) -> None:
    """Flip `state.auto_refresh_on_fs_change`, persist it, and reconcile
    watchers so the change lands immediately (stopping the Observer on
    disable, starting it on enable). Mirrors `_fire_toggle_task_log`'s
    "apply + save + surface a task" pattern so the user gets one-row
    feedback on whether the conf write actually persisted."""
    from core.config import set_conf_value
    from core.fs_watcher import reconcile_repo_watchers

    new_enabled = not state.auto_refresh_on_fs_change
    state.auto_refresh_on_fs_change = new_enabled
    reconcile_repo_watchers(state)

    t = state.tasks.add(
        "enable auto-refresh" if new_enabled else "disable auto-refresh")
    if set_conf_value("auto_refresh_on_fs_change",
                      "true" if new_enabled else "false"):
        state.tasks.update(
            t, "ok",
            "watching files" if new_enabled else "Ctrl+R only")
    else:
        state.tasks.update(
            t, "warn",
            "applied but conf write failed — won't persist across restart")
    _rebuild_rows(state)


def _fire_cycle_debounce(state: State) -> None:
    """Advance `state.auto_refresh_debounce_ms` to the next entry in
    `_DEBOUNCE_PRESETS_MS`, wrapping at the end. If the current value
    isn't in the preset list (e.g. user hand-edited idlegit.conf to a
    custom number), pick the first preset that's >= current, falling
    back to the first preset. Persists the new value and surfaces a
    one-shot task confirming the write landed."""
    from core.config import set_conf_value

    current = state.auto_refresh_debounce_ms
    if current in _DEBOUNCE_PRESETS_MS:
        idx = _DEBOUNCE_PRESETS_MS.index(current)
        new_value = _DEBOUNCE_PRESETS_MS[
            (idx + 1) % len(_DEBOUNCE_PRESETS_MS)]
    else:
        new_value = next(
            (p for p in _DEBOUNCE_PRESETS_MS if p > current),
            _DEBOUNCE_PRESETS_MS[0])
    state.auto_refresh_debounce_ms = new_value

    t = state.tasks.add(f"debounce → {new_value} ms")
    if set_conf_value("auto_refresh_debounce_ms", str(new_value)):
        state.tasks.update(t, "ok", "saved")
    else:
        state.tasks.update(
            t, "warn",
            "applied but conf write failed — won't persist across restart")
    _rebuild_rows(state)


def _fire_open_task_log(state: State) -> None:
    """Hand the log file off to the platform's default opener via the
    same `webbrowser` mechanism used for run URLs. Failures surface as
    a warn task in the panel so the user gets feedback instead of a
    silent no-op. Spawns a daemon thread because the dispatch can be
    slow on some platforms (Linux desktops in particular)."""
    import threading
    from core.task_log import open_task_log

    path = state.task_log_path

    def worker() -> None:
        t = state.tasks.add(f"open {path.name}")
        # Create-if-missing so opening an "empty log" lands on a real
        # file instead of "does not exist yet". The installer normally
        # pre-creates this, but older installs / non-installed runs /
        # exotic config dirs may not have it yet.
        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            except OSError as e:
                state.tasks.update(
                    t, "fail", f"could not create log: {e}")
                return
        if open_task_log(path):
            state.tasks.update(t, "ok", "opened")
        else:
            state.tasks.update(t, "warn", "no opener available")

    threading.Thread(target=worker, daemon=True).start()


def _fire_clear_task_log(state: State) -> None:
    """Truncate the log file to zero bytes. Reports the result as a
    task row so the user can see it landed (or didn't). Rebuilds the
    menu rows so the size display refreshes immediately."""
    from core.task_log import clear_task_log

    t = state.tasks.add(f"clear {state.task_log_path.name}")
    if clear_task_log(state.task_log_path):
        state.tasks.update(t, "ok", "log cleared")
    else:
        state.tasks.update(t, "fail", "could not write log file")
    _rebuild_rows(state)


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
    name_text = truncate(ws.display_name, name_w, "end")
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
