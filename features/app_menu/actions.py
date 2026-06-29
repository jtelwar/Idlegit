"""Global app-menu key handling and action dispatch."""
from __future__ import annotations

import curses
from dataclasses import dataclass

from core.state.app import State
from core.state.app_menu import AppMenu
from core.workers import (
    kick_off_auto_refresh_debounce_save,
    kick_off_auto_refresh_toggle,
    kick_off_auto_remove_completed_save,
    kick_off_check_for_updates,
    kick_off_clear_task_log,
    kick_off_open_task_log,
    kick_off_periodic_refresh_save,
    kick_off_ssh_add_keys,
    kick_off_ssh_agent_toggle,
    kick_off_task_log_toggle,
    switch_workspace,
)

from .projection import (
    ACTION_ADJUST_PERIODIC_REFRESH,
    ACTION_CHECK_FOR_UPDATES,
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
    AUTO_REMOVE_COMPLETED_PRESETS,
    DEBOUNCE_PRESETS_MS,
    first_focusable,
    format_auto_remove_completed,
    format_periodic_refresh,
    is_focusable,
    rebuild_app_menu_rows,
)


@dataclass(frozen=True)
class AppMenuEffect:
    kind: str = "none"


def close_effect() -> AppMenuEffect:
    return AppMenuEffect(kind="close")


def open_workspace_creator_effect() -> AppMenuEffect:
    return AppMenuEffect(kind="open_workspace_creator")


def open_help_effect() -> AppMenuEffect:
    return AppMenuEffect(kind="open_help")


def open_ssh_keygen_effect() -> AppMenuEffect:
    return AppMenuEffect(kind="open_ssh_keygen")


def update_now_effect() -> AppMenuEffect:
    return AppMenuEffect(kind="update_now")


def move_selected(menu: AppMenu, direction: int) -> None:
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
        if is_focusable(menu.rows[new]):
            remaining -= 1
    menu.selected = new


def handle_app_menu_key(state: State, key: int) -> AppMenuEffect:
    menu = state.app_menu
    if menu is None:
        return AppMenuEffect()

    if key in (27, 9):
        state.app_menu = None
        return close_effect()

    n = len(menu.rows)
    if n == 0:
        return AppMenuEffect()

    focused_row = menu.rows[menu.selected] if 0 <= menu.selected < n else None
    if (focused_row is not None
            and focused_row.kind == "app_action"
            and focused_row.attr_name == ACTION_ADJUST_PERIODIC_REFRESH):
        if key in (curses.KEY_LEFT, ord("-")):
            step_periodic_refresh(state, -1)
            return AppMenuEffect()
        if key in (curses.KEY_RIGHT, ord("+"), ord("=")):
            step_periodic_refresh(state, +1)
            return AppMenuEffect()

    if key == curses.KEY_UP:
        move_selected(menu, -1)
        return AppMenuEffect()
    if key == curses.KEY_DOWN:
        move_selected(menu, +1)
        return AppMenuEffect()
    if key == curses.KEY_PPAGE:
        move_selected(menu, -10)
        return AppMenuEffect()
    if key == curses.KEY_NPAGE:
        move_selected(menu, +10)
        return AppMenuEffect()
    if key == curses.KEY_HOME:
        menu.selected = first_focusable(menu.rows)
        return AppMenuEffect()
    if key == curses.KEY_END:
        for i in range(n - 1, -1, -1):
            if is_focusable(menu.rows[i]):
                menu.selected = i
                break
        return AppMenuEffect()

    if key not in (10, 13, curses.KEY_ENTER, ord(" ")):
        return AppMenuEffect()

    if not (0 <= menu.selected < n):
        return AppMenuEffect()
    row = menu.rows[menu.selected]
    if row.kind == "app_action":
        return fire_app_action(state, row.attr_name)
    if row.kind == "create_workspace":
        return open_workspace_creator_effect()
    if row.kind != "workspace":
        return AppMenuEffect()

    try:
        target = int(row.attr_name)
    except ValueError:
        return AppMenuEffect()
    state.app_menu = None
    if 0 <= target < len(state.workspaces) and target != state.active_workspace_index:
        switch_workspace(state, target)
    return close_effect()


def fire_app_action(state: State, action_id: str) -> AppMenuEffect:
    menu = state.app_menu
    if menu is None:
        return AppMenuEffect()
    if action_id == ACTION_CHECK_FOR_UPDATES:
        kick_off_check_for_updates(state, menu)
        rebuild_app_menu_rows(state)
        return AppMenuEffect()
    if action_id == ACTION_UPDATE_NOW:
        return update_now_effect()
    if action_id == ACTION_OPEN_TASK_LOG:
        kick_off_open_task_log(state)
        return AppMenuEffect()
    if action_id == ACTION_CLEAR_TASK_LOG:
        kick_off_clear_task_log(state)
        return AppMenuEffect()
    if action_id == ACTION_TOGGLE_TASK_LOG:
        toggle_task_log(state)
        return AppMenuEffect()
    if action_id == ACTION_TOGGLE_AUTO_REFRESH:
        toggle_auto_refresh(state)
        return AppMenuEffect()
    if action_id == ACTION_CYCLE_DEBOUNCE:
        cycle_debounce(state)
        return AppMenuEffect()
    if action_id == ACTION_ADJUST_PERIODIC_REFRESH:
        toggle_periodic_refresh(state)
        return AppMenuEffect()
    if action_id == ACTION_CYCLE_AUTO_REMOVE_COMPLETED:
        cycle_auto_remove_completed(state)
        return AppMenuEffect()
    if action_id == ACTION_TOGGLE_SSH_AGENT:
        toggle_ssh_agent(state)
        return AppMenuEffect()
    if action_id == ACTION_CREATE_SSH_KEY:
        return open_ssh_keygen_effect()
    if action_id == ACTION_SSH_ADD_KEYS:
        kick_off_ssh_add_keys(state)
        return AppMenuEffect()
    if action_id == ACTION_OPEN_HELP:
        state.app_menu = None
        return open_help_effect()
    return AppMenuEffect()


def toggle_task_log(state: State) -> None:
    new_enabled = not state.task_log_enabled
    state.task_log_enabled = new_enabled
    rebuild_app_menu_rows(state)
    kick_off_task_log_toggle(state, new_enabled)


def toggle_ssh_agent(state: State) -> None:
    new_enabled = not state.auto_start_ssh_agent
    state.auto_start_ssh_agent = new_enabled
    rebuild_app_menu_rows(state)
    kick_off_ssh_agent_toggle(state, new_enabled)


def toggle_auto_refresh(state: State) -> None:
    new_enabled = not state.auto_refresh_on_fs_change
    state.auto_refresh_on_fs_change = new_enabled
    rebuild_app_menu_rows(state)
    kick_off_auto_refresh_toggle(state, new_enabled)


def cycle_debounce(state: State) -> None:
    current = state.auto_refresh_debounce_ms
    if current in DEBOUNCE_PRESETS_MS:
        idx = DEBOUNCE_PRESETS_MS.index(current)
        new_value = DEBOUNCE_PRESETS_MS[
            (idx + 1) % len(DEBOUNCE_PRESETS_MS)]
    else:
        new_value = next(
            (p for p in DEBOUNCE_PRESETS_MS if p > current),
            DEBOUNCE_PRESETS_MS[0])
    state.auto_refresh_debounce_ms = new_value
    rebuild_app_menu_rows(state)
    kick_off_auto_refresh_debounce_save(state, new_value)


def save_periodic_refresh(state: State, new_value: float) -> None:
    state.periodic_refresh_seconds = new_value
    value_text = format_periodic_refresh(new_value)
    rebuild_app_menu_rows(state)
    kick_off_periodic_refresh_save(state, new_value, value_text)


def toggle_periodic_refresh(state: State) -> None:
    new_value = 60.0 if state.periodic_refresh_seconds < 1 else 0.0
    save_periodic_refresh(state, new_value)


def step_periodic_refresh(state: State, delta: int) -> None:
    current = state.periodic_refresh_seconds
    if delta > 0:
        new_value = 1.0 if current < 1 else current + 1
    else:
        new_value = 0.0 if current <= 1 else current - 1
    save_periodic_refresh(state, new_value)


def cycle_auto_remove_completed(state: State) -> None:
    current = state.auto_remove_completed_after
    if current in AUTO_REMOVE_COMPLETED_PRESETS:
        idx = AUTO_REMOVE_COMPLETED_PRESETS.index(current)
        new_value = AUTO_REMOVE_COMPLETED_PRESETS[
            (idx + 1) % len(AUTO_REMOVE_COMPLETED_PRESETS)]
    else:
        new_value = next(
            (p for p in AUTO_REMOVE_COMPLETED_PRESETS if p > current),
            AUTO_REMOVE_COMPLETED_PRESETS[0])
    state.auto_remove_completed_after = new_value
    value_text = format_auto_remove_completed(new_value)
    rebuild_app_menu_rows(state)
    kick_off_auto_remove_completed_save(state, new_value, value_text)
