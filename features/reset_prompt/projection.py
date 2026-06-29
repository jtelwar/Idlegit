"""Soft-reset prompt projection helpers."""
from __future__ import annotations

from core.state.prompts import ResetPrompt

KEY_BACKSPACE_LABEL = "Backspace"
KEY_ENTER_LABEL = "Enter"
KEY_ESC_LABEL = "Esc"


def reset_prompt_hint_specs(prompt: ResetPrompt) -> list[tuple[str, str]]:
    hints: list[tuple[str, str]] = []
    if prompt.typed:
        count = reset_count_from_typed(prompt.typed)
        hints.append(("0-9", "edit count"))
        hints.append((KEY_BACKSPACE_LABEL, "delete digit"))
        if count == 0:
            hints.append((KEY_ENTER_LABEL, "wipe ALL unpushed"))
        else:
            plural = "s" if count != 1 else ""
            hints.append((KEY_ENTER_LABEL, f"reset {count} commit{plural}"))
    else:
        hints.append(("0-9", "type count"))
        hints.append((KEY_ENTER_LABEL, "type 0 to wipe all"))
    hints.append((KEY_ESC_LABEL, "back"))
    return hints


def reset_prompt_title(_prompt: ResetPrompt) -> str:
    return "Soft reset"


def reset_count_from_typed(text: str) -> int:
    try:
        return max(0, int(text))
    except ValueError:
        return 0
