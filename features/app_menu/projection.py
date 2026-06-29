"""Global app-menu row projection and focus helpers."""
from __future__ import annotations

from core.config import VERSION
from core.state.app import State
from core.state.app_menu import AppMenu, AppMenuRow

ACTION_CHECK_FOR_UPDATES = "check_for_updates"
ACTION_UPDATE_NOW = "update_now"
ACTION_OPEN_TASK_LOG = "open_task_log"
ACTION_CLEAR_TASK_LOG = "clear_task_log"
ACTION_TOGGLE_TASK_LOG = "toggle_task_log"
ACTION_TOGGLE_AUTO_REFRESH = "toggle_auto_refresh"
ACTION_CYCLE_DEBOUNCE = "cycle_auto_refresh_debounce"
ACTION_ADJUST_PERIODIC_REFRESH = "adjust_periodic_refresh"
ACTION_CYCLE_AUTO_REMOVE_COMPLETED = "cycle_auto_remove_completed"
ACTION_OPEN_HELP = "open_help"
ACTION_TOGGLE_SSH_AGENT = "toggle_ssh_agent"
ACTION_CREATE_SSH_KEY = "create_ssh_key"
ACTION_SSH_ADD_KEYS = "ssh_add_keys"

DEBOUNCE_PRESETS_MS = (200, 400, 800, 1500)
AUTO_REMOVE_COMPLETED_PRESETS = (-1.0, 3.0, 6.0, 10.0, 30.0)


def parse_version_tuple(text: str) -> tuple[int, ...]:
    s = text.strip().lstrip("vV")
    head: list[str] = []
    for ch in s:
        if ch.isdigit() or ch == ".":
            head.append(ch)
            continue
        break
    parts = "".join(head).split(".")
    return tuple(int(p) for p in parts if p)


def is_update_available(installed: str, latest: str) -> bool:
    current = parse_version_tuple(installed)
    remote = parse_version_tuple(latest)
    if not current or not remote:
        return False
    return current < remote


def format_auto_remove_completed(value: float) -> str:
    if value < 0:
        return "off"
    if float(value).is_integer():
        return f"{int(value)}s"
    return f"{value:g}s"


def format_periodic_refresh(value: float) -> str:
    if value < 1:
        return "0s (OFF)"
    if float(value).is_integer():
        return f"{int(value)}s"
    return f"{value:g}s"


def app_section_rows(menu: AppMenu) -> list[AppMenuRow]:
    rows: list[AppMenuRow] = []
    if menu.update_check == "idle":
        rows.append(AppMenuRow(
            label="Check for updates",
            attr_name=ACTION_CHECK_FOR_UPDATES,
            kind="app_action"))
    elif menu.update_check == "checking":
        rows.append(AppMenuRow(
            label="Checking for updates…",
            attr_name="",
            kind="app_info"))
    elif menu.update_check == "no_releases":
        rows.append(AppMenuRow(
            label="No releases published yet",
            attr_name="",
            kind="app_info"))
        rows.append(AppMenuRow(
            label="Check again",
            attr_name=ACTION_CHECK_FOR_UPDATES,
            kind="app_action"))
    elif menu.update_check == "failed":
        err = (menu.update_check_error or "unknown error").strip()
        rows.append(AppMenuRow(
            label=f"Check failed: {err}",
            attr_name="",
            kind="app_info"))
        rows.append(AppMenuRow(
            label="Try again",
            attr_name=ACTION_CHECK_FOR_UPDATES,
            kind="app_action"))
    elif menu.update_check == "done":
        latest = menu.latest_version or "?"
        if is_update_available(VERSION, latest):
            rows.append(AppMenuRow(
                label=f"Update available: {latest}",
                attr_name="",
                kind="app_info"))
            rows.append(AppMenuRow(
                label="Update now",
                attr_name=ACTION_UPDATE_NOW,
                kind="app_action"))
        else:
            rows.append(AppMenuRow(
                label=f"Up to date (latest: {latest})",
                attr_name="",
                kind="app_info"))
            rows.append(AppMenuRow(
                label="Check again",
                attr_name=ACTION_CHECK_FOR_UPDATES,
                kind="app_action"))
    rows.append(AppMenuRow(
        label="Help",
        attr_name=ACTION_OPEN_HELP,
        kind="app_action"))
    return rows


def auto_refresh_section_rows(state: State) -> list[AppMenuRow]:
    on = state.auto_refresh_on_fs_change
    toggle_label = (
        "Disable filesystem auto-refresh" if on
        else "Enable filesystem auto-refresh")
    rows = [
        AppMenuRow(label="AUTO REFRESH", attr_name="", kind="header"),
        AppMenuRow(
            label=toggle_label,
            attr_name=ACTION_TOGGLE_AUTO_REFRESH,
            kind="app_action"),
        AppMenuRow(
            label=f"Periodic refresh: {format_periodic_refresh(state.periodic_refresh_seconds)}",
            attr_name=ACTION_ADJUST_PERIODIC_REFRESH,
            kind="app_action"),
    ]
    if on:
        rows.append(AppMenuRow(
            label=f"Debounce: {state.auto_refresh_debounce_ms} ms",
            attr_name=ACTION_CYCLE_DEBOUNCE,
            kind="app_action"))
    return rows


def tasks_section_rows(state: State) -> list[AppMenuRow]:
    interval = format_auto_remove_completed(state.auto_remove_completed_after)
    return [
        AppMenuRow(label="TASKS", attr_name="", kind="header"),
        AppMenuRow(
            label=f"Remove successful tasks: {interval}",
            attr_name=ACTION_CYCLE_AUTO_REMOVE_COMPLETED,
            kind="app_action"),
    ]


def ssh_section_rows(state: State) -> list[AppMenuRow]:
    menu = state.app_menu
    on = state.auto_start_ssh_agent
    toggle_label = (
        "Disable auto-start ssh-agent" if on
        else "Enable auto-start ssh-agent")
    rows: list[AppMenuRow] = [
        AppMenuRow(label="SSH", attr_name="", kind="header"),
    ]
    missing = menu.ssh_tools_missing if menu is not None else []
    agent_label = menu.ssh_status if menu is not None else "checking"
    keys_label = menu.ssh_keys if menu is not None else "checking"
    if missing:
        rows.append(AppMenuRow(
            label=f"Missing on PATH: {', '.join(missing)}",
            attr_name="",
            kind="app_info"))
    rows.extend([
        AppMenuRow(
            label=f"Agent: {agent_label}",
            attr_name="",
            kind="app_info"),
        AppMenuRow(
            label=f"Keys: {keys_label}",
            attr_name="",
            kind="app_info"),
        AppMenuRow(
            label=toggle_label,
            attr_name=ACTION_TOGGLE_SSH_AGENT,
            kind="app_action"),
        AppMenuRow(
            label="Create GitHub SSH keypair…",
            attr_name=ACTION_CREATE_SSH_KEY,
            kind="app_action"),
        AppMenuRow(
            label="Load default keys into agent",
            attr_name=ACTION_SSH_ADD_KEYS,
            kind="app_action"),
    ])
    return rows


def task_logging_section_rows(state: State) -> list[AppMenuRow]:
    menu = state.app_menu
    path_text = str(state.task_log_path)
    size_text = menu.task_log_size if menu is not None else "checking"
    cap = state.task_log_max_lines
    cap_text = "unlimited" if cap <= 0 else f"{cap:,} lines"
    toggle_label = (
        "Disable task logging" if state.task_log_enabled
        else "Enable task logging")
    return [
        AppMenuRow(label="TASK LOGGING", attr_name="", kind="header"),
        AppMenuRow(
            label=toggle_label,
            attr_name=ACTION_TOGGLE_TASK_LOG,
            kind="app_action"),
        AppMenuRow(label=f"Path: {path_text}", attr_name="", kind="app_info"),
        AppMenuRow(label=f"Size: {size_text}", attr_name="", kind="app_info"),
        AppMenuRow(
            label=f"Max lines: {cap_text}",
            attr_name="",
            kind="app_info"),
        AppMenuRow(
            label="Open log file",
            attr_name=ACTION_OPEN_TASK_LOG,
            kind="app_action"),
        AppMenuRow(
            label="Clear log contents",
            attr_name=ACTION_CLEAR_TASK_LOG,
            kind="app_action"),
    ]


def workspaces_section_rows(state: State) -> list[AppMenuRow]:
    rows = [AppMenuRow(label="WORKSPACES", attr_name="", kind="header")]
    for i, ws in enumerate(state.workspaces):
        rows.append(AppMenuRow(
            label=ws.display_name,
            attr_name=str(i),
            kind="workspace"))
    rows.append(AppMenuRow(
        label="+ Create new workspace…",
        attr_name="",
        kind="create_workspace"))
    return rows


def build_app_menu_rows(state: State, menu: AppMenu) -> list[AppMenuRow]:
    rows = app_section_rows(menu)
    if rows:
        rows.append(AppMenuRow(label="", attr_name="", kind="spacer"))
    rows.extend(workspaces_section_rows(state))
    rows.append(AppMenuRow(label="", attr_name="", kind="spacer"))
    rows.extend(auto_refresh_section_rows(state))
    rows.append(AppMenuRow(label="", attr_name="", kind="spacer"))
    rows.extend(tasks_section_rows(state))
    rows.append(AppMenuRow(label="", attr_name="", kind="spacer"))
    rows.extend(ssh_section_rows(state))
    rows.append(AppMenuRow(label="", attr_name="", kind="spacer"))
    rows.extend(task_logging_section_rows(state))
    return rows


def is_focusable(row: AppMenuRow) -> bool:
    return row.kind not in ("header", "spacer", "app_info")


def first_focusable(rows: list[AppMenuRow]) -> int:
    for i, row in enumerate(rows):
        if is_focusable(row):
            return i
    return 0


def rebuild_app_menu_rows(state: State) -> None:
    menu = state.app_menu
    if menu is None:
        return
    old_kind = ""
    old_attr = ""
    if 0 <= menu.selected < len(menu.rows):
        old_kind = menu.rows[menu.selected].kind
        old_attr = menu.rows[menu.selected].attr_name
    menu.rows = build_app_menu_rows(state, menu)
    new_idx = -1
    for i, row in enumerate(menu.rows):
        if row.kind == old_kind and row.attr_name == old_attr:
            new_idx = i
            break
    if new_idx == -1:
        new_idx = first_focusable(menu.rows)
    menu.selected = new_idx
