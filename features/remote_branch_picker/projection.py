"""Remote-branch-picker projection helpers."""
from __future__ import annotations

from core.state.app import State
from core.state.pickers import RemoteBranchPicker


def picker_refs(
        state: State,
        picker: RemoteBranchPicker,
) -> tuple[list[str], bool]:
    refs, loading, _error = state.view_loads.snapshot(picker.load_id)
    return refs, loading


def tracking_label(ref: str) -> str:
    if "/" not in ref:
        return f"checkout {ref}"
    short = ref.split("/", 1)[1]
    return f"checkout {short} (track {ref})"


def title_label(_picker: RemoteBranchPicker) -> str:
    return "Checkout remote branch"
