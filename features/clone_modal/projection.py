"""Clone modal projection helpers."""
from __future__ import annotations

from core.state.clone import CloneModal

FIELD_URL = 0
FIELD_DEST = 1
FIELD_BRANCH = 2
FIELD_RECURSE = 3
FIELD_BUTTON = 4
N_FIELDS = 5

KEY_BACKSPACE_LABEL = "Backspace"
KEY_ENTER_LABEL = "Enter"
KEY_ESC_LABEL = "Esc"
KEY_SPACE_LABEL = "Space"
KEY_TAB_LABEL = "Tab"
KEY_UP_DOWN_LABEL = "↑/↓"

BRANCH_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_./"
)


def name_from_url(url: str) -> str:
    if not url:
        return ""
    last = url.rstrip("/").rsplit("/", 1)[-1]
    last = last.rstrip()
    if last.endswith(".git"):
        last = last[:-4]
    return last or "repo"


def default_dest(modal: CloneModal) -> str:
    if not modal.workspace_folders:
        return ""
    name = name_from_url(modal.url)
    if not name:
        return str(modal.workspace_folders[0])
    return str(modal.workspace_folders[0] / name)


def can_clone(modal: CloneModal) -> bool:
    if modal.cloning:
        return False
    if not modal.url or modal.url.startswith("-"):
        return False
    if not modal.dest_text.strip():
        return False
    return True


def clone_modal_hint_specs(modal: CloneModal) -> list[tuple[str, str]]:
    if modal.edit_field:
        return [
            ("type", f"edit {modal.edit_field}"),
            (KEY_BACKSPACE_LABEL, "delete char"),
            (KEY_ENTER_LABEL, "save"),
            (KEY_ESC_LABEL, "cancel edit"),
        ]
    hints: list[tuple[str, str]] = [(KEY_UP_DOWN_LABEL, "select")]
    selected = modal.selected
    if selected == FIELD_URL:
        hints.append((KEY_ENTER_LABEL, "edit url"))
    elif selected == FIELD_DEST:
        hints.append((KEY_ENTER_LABEL, "edit path"))
    elif selected == FIELD_BRANCH:
        hints.append((KEY_ENTER_LABEL, "edit branch"))
    elif selected == FIELD_RECURSE:
        if modal.recurse_submodules:
            hints.append((KEY_SPACE_LABEL, "init submodules off"))
        else:
            hints.append((KEY_SPACE_LABEL, "init submodules on"))
    elif selected == FIELD_BUTTON:
        if can_clone(modal):
            hints.append((KEY_ENTER_LABEL, "run clone"))
        else:
            hints.append((KEY_ENTER_LABEL, "(fill url + path first)"))
    hints.append((KEY_TAB_LABEL, "close"))
    hints.append((KEY_ESC_LABEL, "close"))
    return hints

