"""Global app-menu session lifecycle and status refresh coordination."""
from __future__ import annotations

from dataclasses import dataclass

from core.state.app import State
from core.state.app_menu import AppMenu
from core.workers import kick_off_app_menu_status_refresh

from .projection import build_app_menu_rows, first_focusable, rebuild_app_menu_rows


@dataclass(frozen=True)
class OpenAppMenuResult:
    open_workspace_creator: bool = False


def open_app_menu_session(state: State) -> OpenAppMenuResult:
    if not state.workspaces:
        return OpenAppMenuResult(open_workspace_creator=True)

    menu = AppMenu()
    menu.rows = build_app_menu_rows(state, menu)
    target_attr = str(state.active_workspace_index)
    selected = first_focusable(menu.rows)
    for i, row in enumerate(menu.rows):
        if row.kind == "workspace" and row.attr_name == target_attr:
            selected = i
            break
    menu.selected = selected
    state.app_menu = menu
    kick_off_app_menu_status_refresh(state, menu)
    return OpenAppMenuResult()


def app_menu_status_needs_refresh(menu: AppMenu) -> bool:
    return (
        menu.ssh_status == "checking"
        and not menu.ssh_status_checking
    ) or (
        menu.task_log_size == "checking"
        and not menu.task_log_checking
    )


def tick_app_menu_update_check(state: State) -> bool:
    menu = state.app_menu
    if menu is None:
        return False
    if app_menu_status_needs_refresh(menu):
        kick_off_app_menu_status_refresh(state, menu)
    if menu.ssh_status != "checking" and not any(
            row.label == f"Agent: {menu.ssh_status}" for row in menu.rows):
        rebuild_app_menu_rows(state)
    if menu.task_log_size != "checking" and not any(
            row.label == f"Size: {menu.task_log_size}" for row in menu.rows):
        rebuild_app_menu_rows(state)
    if menu.update_check != menu.update_check_rendered:
        menu.update_check_rendered = menu.update_check
        rebuild_app_menu_rows(state)
    return (
        menu.update_check == "checking"
        or menu.ssh_status_checking
        or menu.task_log_checking
    )

