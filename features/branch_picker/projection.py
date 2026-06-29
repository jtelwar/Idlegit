"""Branch-picker projection helpers."""
from __future__ import annotations

from core.state.app import State
from core.state.pickers import BranchPicker

VALID_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_./"
)


def has_create_row(picker: BranchPicker) -> bool:
    return picker.mode == "switch"


def picker_branches(
        state: State,
        picker: BranchPicker,
) -> tuple[list[str], str, bool]:
    branches, loading, _error = state.view_loads.snapshot(picker.load_id)
    current = state.view_loads.details(picker.load_id).get("current", "")
    return branches, current, loading


def title_label(picker: BranchPicker) -> str:
    if picker.mode == "safe_merge":
        return "Safe-merge in branch"
    if picker.mode == "merge":
        return "Merge in branch"
    if picker.mode == "set_upstream":
        return "Set upstream"
    return "Switch branch"
