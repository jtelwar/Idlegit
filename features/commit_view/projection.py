"""Commit-view projection helpers."""
from __future__ import annotations

from typing import List

from core.state.app import State
from core.state.action_menu import ActionMenuItem
from core.state.views import CommitViewModal


MODAL_W = 100
PAD_X = 2
PANE_TARGET_ROWS = 10
TAB_IDS = ("changes", "reflog")
VALID_TAG_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_./"
)


def commit_view_load_ids(modal: CommitViewModal) -> list[str]:
    return [
        modal.tags_load_id,
        modal.details_load_id,
        modal.files_load_id,
        modal.reflog_load_id,
    ]


def is_loading(state: State, load_id: str) -> bool:
    if not load_id:
        return True
    _lines, loading, _error = state.view_loads.snapshot(load_id)
    return loading


def tags_loading(state: State, modal: CommitViewModal) -> bool:
    return is_loading(state, modal.tags_load_id)


def files_loading(state: State, modal: CommitViewModal) -> bool:
    return is_loading(state, modal.files_load_id)


def reflog_loading(state: State, modal: CommitViewModal) -> bool:
    return is_loading(state, modal.reflog_load_id)


def build_action_items() -> List[ActionMenuItem]:
    return [
        ActionMenuItem(id="add_tag", label="+ add tag", enabled=True),
    ]


def wrap_text(text: str, width: int) -> List[str]:
    if not text or width <= 0:
        return []
    out: List[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            out.append("")
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            while len(word) > width:
                if current:
                    out.append(current)
                    current = ""
                out.append(word[:width])
                word = word[width:]
            candidate = word if not current else current + " " + word
            if len(candidate) <= width:
                current = candidate
            else:
                out.append(current)
                current = word
        if current:
            out.append(current)
    return out


def flow_badges(
    tags: List[str],
    width: int,
    max_lines: int = 2,
) -> List[List[str]]:
    if not tags:
        return []
    rendered = [f"[{tag}]" for tag in tags]
    rows: List[List[str]] = [[]]
    current_width = 0
    gap = 1
    for index, badge in enumerate(rendered):
        badge_width = len(badge)
        if current_width == 0:
            rows[-1].append(badge)
            current_width = badge_width
        elif current_width + gap + badge_width <= width:
            rows[-1].append(badge)
            current_width += gap + badge_width
        else:
            if len(rows) >= max_lines:
                remaining = len(rendered) - index
                tail = f"+{remaining} more"
                while (rows[-1]
                       and (sum(len(item) + gap for item in rows[-1])
                            - gap + gap + len(tail) > width)):
                    rows[-1].pop()
                if not rows[-1]:
                    rows[-1] = [tail]
                else:
                    rows[-1].append(tail)
                return rows
            rows.append([badge])
            current_width = badge_width
    return rows


def build_tab_header(
    modal: CommitViewModal,
    state: State,
    spinner: str,
) -> list:
    if files_loading(state, modal) and not modal.files:
        changes_count = spinner
    else:
        changes_count = str(len(modal.files))
    if reflog_loading(state, modal) and not modal.reflog_entries:
        reflog_count = spinner
    else:
        reflog_count = str(len(modal.reflog_entries))
    return [
        ("changes", "Changes", changes_count),
        ("reflog", "Reflog", reflog_count),
    ]


def cycle_tab_id(active_id: str, direction: int) -> str:
    if len(TAB_IDS) <= 1:
        return active_id
    try:
        index = TAB_IDS.index(active_id)
    except ValueError:
        return active_id
    return TAB_IDS[(index + direction) % len(TAB_IDS)]


_commit_view_load_ids = commit_view_load_ids
