"""Diff viewer session lifecycle."""
from __future__ import annotations

from pathlib import Path

from core.state.app import State
from core.state.views import DiffViewer
from core.workers import kick_off_diff_viewer_loads


def open_diff_viewer(
        state: State,
        target_path: Path,
        label: str,
        file_path: str,
        untracked: bool,
        commit_sha: str = "",
) -> None:
    viewer = DiffViewer(
        file_path=file_path,
        target_path=target_path,
        label=label,
        untracked=untracked,
        commit_sha=commit_sha,
        diff_load_id=f"diff-viewer:{id(state)}:{id(file_path)}:diff",
        log_load_id=f"diff-viewer:{id(state)}:{id(file_path)}:log",
        blame_load_id=f"diff-viewer:{id(state)}:{id(file_path)}:blame",
    )
    state.diff_viewer = viewer
    kick_off_diff_viewer_loads(state, viewer)


def close_diff_viewer(state: State) -> None:
    viewer = state.diff_viewer
    if viewer is None:
        return
    state.view_loads.remove_many([
        viewer.diff_load_id,
        viewer.log_load_id,
        viewer.blame_load_id,
    ])
    state.diff_viewer = None
