"""Diff viewer projection helpers."""
from __future__ import annotations

from core.state.app import State
from core.state.views import DiffViewer

TAB_IDS = ("diff", "log", "blame")

KEY_ESC_LABEL = "Esc"
KEY_LEFT_RIGHT_LABEL = "←/→"
KEY_TAB_LABEL = "Tab"
KEY_UP_DOWN_LABEL = "↑/↓"


def diff_viewer_hint_specs() -> list[tuple[str, str]]:
    return [
        (KEY_LEFT_RIGHT_LABEL, "switch tab"),
        (KEY_UP_DOWN_LABEL, "scroll"),
        (KEY_TAB_LABEL, "close"),
        (KEY_ESC_LABEL, "close"),
    ]


def tab_load_id(viewer: DiffViewer, tab: str) -> str:
    if tab == "log":
        return viewer.log_load_id
    if tab == "blame":
        return viewer.blame_load_id
    return viewer.diff_load_id


def tab_lines(state: State, viewer: DiffViewer, tab: str) -> list[str]:
    lines, _loading, _error = state.view_loads.snapshot(
        tab_load_id(viewer, tab))
    return lines


def tab_loading(state: State, viewer: DiffViewer, tab: str) -> bool:
    _lines, loading, _error = state.view_loads.snapshot(
        tab_load_id(viewer, tab))
    return loading


def tab_scroll(viewer: DiffViewer, tab: str) -> int:
    if tab == "log":
        return viewer.log_scroll
    if tab == "blame":
        return viewer.blame_scroll
    return viewer.scroll


def set_tab_scroll(viewer: DiffViewer, tab: str, value: int) -> None:
    if tab == "log":
        viewer.log_scroll = value
    elif tab == "blame":
        viewer.blame_scroll = value
    else:
        viewer.scroll = value


def any_tab_loading(state: State, viewer: DiffViewer) -> bool:
    return state.view_loads.any_loading([
        viewer.diff_load_id,
        viewer.log_load_id,
        viewer.blame_load_id,
    ])
