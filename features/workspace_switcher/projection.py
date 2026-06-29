"""Workspace switcher projection helpers."""
from __future__ import annotations

from core.state.app import State
from core.state.workspaces import WorkspaceSwitcher

KEY_ENTER_LABEL = "Enter"
KEY_ESC_LABEL = "Esc"
KEY_UP_DOWN_LABEL = "↑/↓"


def clamped_active_workspace_index(state: State) -> int:
    return max(0, min(state.active_workspace_index, len(state.workspaces) - 1))


def workspace_switcher_hint_specs(
        state: State,
        switcher: WorkspaceSwitcher,
) -> list[tuple[str, str]]:
    hints = [(KEY_UP_DOWN_LABEL, "select")]
    workspace_count = len(state.workspaces)
    if 0 <= switcher.selected < workspace_count:
        workspace = state.workspaces[switcher.selected]
        if switcher.selected == state.active_workspace_index:
            hints.append((KEY_ENTER_LABEL, "stay (already active)"))
        else:
            hints.append((KEY_ENTER_LABEL, f"switch to {workspace.display_name}"))
    hints.append((KEY_ESC_LABEL, "close"))
    return hints
