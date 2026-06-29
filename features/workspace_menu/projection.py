"""Workspace menu row projection and effective-value helpers."""
from __future__ import annotations

from typing import Tuple

from core.config import (
    WORKSPACE_OVERRIDE_TARGETS,
    base_value_for_override,
    state_attr_value_from_override,
)
from core.state.app import State
from core.state.workspaces import (
    Workspace,
    WorkspaceDraft,
    WorkspaceMenu,
    WorkspaceMenuRow,
)


OVERRIDE_ROWS: Tuple[WorkspaceMenuRow, ...] = (
    WorkspaceMenuRow(label="COMMIT", attr_name="", kind="header"),
    WorkspaceMenuRow(
        label="Auto-stage first",
        attr_name="default_auto_stage", kind="bool",
        hint_text="stage all changes before committing (default: on)"),
    WorkspaceMenuRow(
        label="Auto-push after",
        attr_name="default_auto_push", kind="bool",
        hint_text="push to upstream after a successful commit (default: on)"),
    WorkspaceMenuRow(
        label="Track actions by default",
        attr_name="track_actions_default", kind="bool",
        hint_text="poll GitHub Actions on new repos (default: on)"),
    WorkspaceMenuRow(label="SMART-SYNC", attr_name="", kind="header"),
    WorkspaceMenuRow(
        label="Align heads",
        attr_name="default_align_heads", kind="bool",
        hint_text="reattach a detached HEAD onto its branch before sync (default: on)"),
    WorkspaceMenuRow(
        label="Auto-FF",
        attr_name="default_auto_ff", kind="bool",
        hint_text="fast-forward the loser side of a sync when safe (default: on)"),
    WorkspaceMenuRow(
        label="Prompt for branch",
        attr_name="default_prompt_for_branch", kind="bool",
        hint_text="ask which branch a detached winner pushes to (default: on)"),
    WorkspaceMenuRow(
        label="Prevent silent merge (sync)",
        attr_name="default_prevent_smart_sync_silent_merge", kind="bool",
        hint_text="smart-sync: FF-only for losers — no auto merge commits (default: off)"),
    WorkspaceMenuRow(
        label="Auto-push submodule parent",
        attr_name="default_auto_push_submodule_parent", kind="bool",
        hint_text="after sync, commit+push any parent whose only dirt is the bumped submodule (default: on)"),
    WorkspaceMenuRow(
        label="Fetch on manual refresh",
        attr_name="fetch_on_manual_refresh", kind="bool",
        hint_text="Ctrl+R does `git fetch --all` per repo so ahead/behind is fresh; off keeps Ctrl+R instant + offline (default: off)"),
    WorkspaceMenuRow(label="DISPLAY", attr_name="", kind="header"),
    WorkspaceMenuRow(
        label="Name truncation",
        attr_name="name_truncation", kind="trunc_mode",
        hint_text="where to drop characters when a repo name overflows (default: middle)"),
    WorkspaceMenuRow(
        label="Branch truncation",
        attr_name="branch_truncation", kind="trunc_mode",
        hint_text="where to drop characters when a branch name overflows (default: middle)"),
    WorkspaceMenuRow(
        label="Task name truncation",
        attr_name="task_name_truncation", kind="trunc_mode",
        hint_text="where to drop characters when a task name overflows (default: middle)"),
    WorkspaceMenuRow(
        label="Name display max",
        attr_name="name_display_max", kind="int",
        min_value=0, max_value=200, step=2,
        hint_text="max width of the repo name column, in cells (default: 40)"),
    WorkspaceMenuRow(
        label="Child name display max",
        attr_name="child_name_display_max", kind="int",
        min_value=-1, max_value=200, step=2,
        hint_text="max width of sub-repo name column, -1 = inherit parent (default: -1)"),
    WorkspaceMenuRow(
        label="Branch display max",
        attr_name="branch_display_max", kind="int",
        min_value=0, max_value=80, step=1,
        hint_text="max width of the branch column, in cells (default: 12)"),
    WorkspaceMenuRow(
        label="Task name display max",
        attr_name="task_name_display_max", kind="int",
        min_value=0, max_value=80, step=1,
        hint_text="max width of the task-name column, in cells (default: 16)"),
    WorkspaceMenuRow(
        label="Max visible repo rows",
        attr_name="max_visible_repo_rows", kind="int",
        min_value=0, max_value=200, step=1,
        hint_text="how many repo rows to show, 0 = use full available height (default: 0)"),
    WorkspaceMenuRow(label="COMMIT SUGGESTIONS", attr_name="", kind="header"),
    WorkspaceMenuRow(
        label="Suggest added",
        attr_name="suggest_added", kind="int",
        min_value=-1, max_value=99, step=1,
        hint_text="added files to suggest, -1 = unlimited, 0 = hidden (default: 5)"),
    WorkspaceMenuRow(
        label="Suggest updated",
        attr_name="suggest_updated", kind="int",
        min_value=-1, max_value=99, step=1,
        hint_text="updated files to suggest, -1 = unlimited, 0 = hidden (default: 5)"),
    WorkspaceMenuRow(
        label="Suggest deleted",
        attr_name="suggest_deleted", kind="int",
        min_value=-1, max_value=99, step=1,
        hint_text="deleted files to suggest, -1 = unlimited, 0 = hidden (default: 5)"),
)


def build_rows(ws: Workspace) -> list[WorkspaceMenuRow]:
    rows: list[WorkspaceMenuRow] = []
    if ws.ephemeral:
        rows.append(WorkspaceMenuRow(
            label="EPHEMERAL WORKSPACE", attr_name="", kind="header"))
        rows.append(WorkspaceMenuRow(
            label="+ Save as workspace…",
            attr_name="", kind="save_ephemeral"))
    rows.append(WorkspaceMenuRow(
        label="FOLDERS", attr_name="", kind="header"))
    for i in range(len(ws.folders)):
        rows.append(WorkspaceMenuRow(
            label=f"folder {i + 1}", attr_name=str(i), kind="folder"))
    rows.append(WorkspaceMenuRow(
        label="+ Add folder…", attr_name="", kind="add_folder"))
    rows.append(WorkspaceMenuRow(
        label="+ Clone repository…", attr_name="", kind="clone"))
    rows.append(WorkspaceMenuRow(
        label="FILE WATCH IGNORE", attr_name="", kind="header"))
    for i in range(len(ws.fs_watch_ignore)):
        rows.append(WorkspaceMenuRow(
            label=f"pattern {i + 1}", attr_name=str(i),
            kind="ignore_pattern"))
    rows.append(WorkspaceMenuRow(
        label="+ Add pattern…", attr_name="",
        kind="add_ignore_pattern",
        hint_text="gitignore syntax — *.log, build/**, !keep.log, "
                  "leading / anchors, trailing / for dirs"))
    rows.extend(OVERRIDE_ROWS)
    return rows


def is_focusable(row: WorkspaceMenuRow) -> bool:
    return row.kind != "header"


def focused_row(menu: WorkspaceMenu) -> WorkspaceMenuRow | None:
    if 0 <= menu.selected < len(menu.rows):
        return menu.rows[menu.selected]
    return None


def draft_for_row(
    menu: WorkspaceMenu,
    row: WorkspaceMenuRow,
) -> WorkspaceDraft | None:
    if row.kind != "folder":
        return None
    try:
        idx = int(row.attr_name)
    except ValueError:
        return None
    if 0 <= idx < len(menu.path_drafts):
        return menu.path_drafts[idx]
    return None


def rebuild_rows(state: State) -> None:
    menu = state.workspace_menu
    ws = state.active_workspace
    if menu is None or ws is None:
        return
    old_kind = ""
    old_attr = ""
    if 0 <= menu.selected < len(menu.rows):
        old_kind = menu.rows[menu.selected].kind
        old_attr = menu.rows[menu.selected].attr_name
    menu.rows = build_rows(ws)
    new_idx = -1
    for i, row in enumerate(menu.rows):
        if row.kind == old_kind and row.attr_name == old_attr:
            new_idx = i
            break
    if new_idx == -1:
        for i, row in enumerate(menu.rows):
            if is_focusable(row):
                new_idx = i
                break
    menu.selected = new_idx if new_idx != -1 else 0


def state_attr_for(row: WorkspaceMenuRow) -> str:
    return WORKSPACE_OVERRIDE_TARGETS.get(row.attr_name, row.attr_name)


def read_value(state: State, row: WorkspaceMenuRow):
    return getattr(state, state_attr_for(row), None)


def apply_base_value(state: State, row: WorkspaceMenuRow) -> None:
    cfg = state.base_config
    if cfg is None:
        return
    base = base_value_for_override(cfg, row.attr_name)
    if base is not None:
        setattr(state, state_attr_for(row),
                state_attr_value_from_override(row.attr_name, base))


def is_overridden(state: State, row: WorkspaceMenuRow) -> bool:
    ws = state.active_workspace
    return ws is not None and row.attr_name in ws.overrides


def format_value(state: State, row: WorkspaceMenuRow) -> str:
    cur = read_value(state, row)
    if row.kind == "bool":
        return "[x]" if cur else "[ ]"
    if row.kind == "trunc_mode":
        return f"‹ {cur or '?'} ›"
    if row.kind == "int":
        try:
            return f"‹ {int(cur)} ›"
        except (TypeError, ValueError):
            return "‹ ? ›"
    return str(cur)
