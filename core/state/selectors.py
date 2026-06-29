"""Read-only selectors for UI and worker gating decisions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple

from core.state.app import State
from .ids import ChildId
from .repos import ChildRef, Repo


@dataclass(frozen=True)
class RowDisplayState:
    """Presentation facts for one repo or child row."""

    busy: bool
    dirty: bool
    editable: bool
    show_spinner: bool
    show_message_field: bool
    suggesting: bool
    message: str


def active_workspace_repo_rows(state: State) -> List[Repo]:
    """Return active workspace repo rows from store membership."""
    workspace_id = state.store.active_workspace_id
    if workspace_id is None:
        return []
    return [
        record.repo
        for record in state.store.repo_records_for_workspace(workspace_id)
    ]


def active_workspace_child_rows(state: State) -> List[Tuple[Repo, ChildRef]]:
    """Return active workspace child rows as (parent repo, child row)."""
    workspace_id = state.store.active_workspace_id
    if workspace_id is None:
        return []
    rows: List[Tuple[Repo, ChildRef]] = []
    for repo_record in state.store.repo_records_for_workspace(workspace_id):
        parent = repo_record.repo
        for child_record in state.store.child_records_for_repo(
                repo_record.repo_id):
            rows.append((parent, child_record.child))
    return rows


def repo_row_state(state: State, repo: Repo) -> RowDisplayState:
    """Return the display/editability state for a repo row."""
    busy = (
        state.store.repo_busy(repo)
        or local_mutation_active_for(state, repos=[repo])
    )
    status = state.store.repo_status(repo)
    if status is None:
        raise RuntimeError("repo row is not registered in state store")
    dirty = status.dirty
    return RowDisplayState(
        busy=busy,
        dirty=dirty,
        editable=(dirty or bool(status.message)) and not busy,
        show_spinner=busy and not (status.error or status.merging),
        show_message_field=(dirty or bool(status.message)) and not busy,
        suggesting=state.store.repo_suggesting(repo),
        message=status.message,
    )


def child_row_state(state: State, child: ChildRef) -> RowDisplayState:
    """Return the display/editability state for a child row."""
    child_id_value = state.store.child_id_for(child)
    if child_id_value is None:
        raise RuntimeError("child row is not registered in state store")
    return _child_row_state_for_id(state, child, child_id_value)


def child_row_state_for_parent(
        state: State,
        parent: Repo,
        child: ChildRef,
) -> RowDisplayState:
    """Return display state for a child row captured during rendering."""
    child_id_value = state.store.child_id_for_parent_child(parent, child)
    if child_id_value is None:
        raise RuntimeError("child row is not registered in state store")
    return _child_row_state_for_id(state, child, child_id_value)


def _child_row_state_for_id(
        state: State,
        child: ChildRef,
        child_id_value: ChildId,
) -> RowDisplayState:
    status = state.store.child_status_by_id(child_id_value)
    if status is None:
        raise RuntimeError("child row is not registered in state store")
    busy = (
        state.store.child_busy_by_id(child_id_value)
        or local_mutation_active_for(state, children=[child])
        or state.leases.has_lease_for(
            children=[child],
            child_ids=[child_id_value],
        )
    )
    dirty = status.dirty
    return RowDisplayState(
        busy=busy,
        dirty=dirty,
        editable=(
            status.kind == "submodule"
            and (dirty or bool(status.message))
            and not busy
        ),
        show_spinner=busy and not (status.error or status.merging),
        show_message_field=(
            status.kind == "submodule"
            and (dirty or bool(status.message))
            and not busy
        ),
        suggesting=state.store.child_suggesting_by_id(child_id_value),
        message=status.message,
    )


def selectable_body_rows(state: State) -> List[Tuple[str, Repo, Optional[ChildRef]]]:
    """Return body rows from store membership and registered child records."""
    rows: List[Tuple[str, Repo, Optional[ChildRef]]] = []
    for repo in active_workspace_repo_rows(state):
        rows.append(("repo", repo, None))
        repo_id = state.store.repo_id_for(repo)
        if repo_id is None:
            continue
        for child_record in state.store.child_records_for_repo(repo_id):
            rows.append(("child", repo, child_record.child))
    return rows


def focused_body_row(
        state: State) -> Optional[Tuple[str, Repo, Optional[ChildRef]]]:
    """Return the selected body row, or None for pseudo/out-of-range rows."""
    if state.selected < 0:
        return None
    rows = selectable_body_rows(state)
    if state.selected >= len(rows):
        return None
    return rows[state.selected]


def focused_repo(state: State) -> Optional[Repo]:
    """Return the selected repo row if focus is on a repo row."""
    row = focused_body_row(state)
    if row is None or row[0] != "repo":
        return None
    return row[1]


def focused_child(state: State) -> Optional[Tuple[Repo, ChildRef]]:
    """Return the selected child row as (parent, child)."""
    row = focused_body_row(state)
    if row is None or row[0] != "child" or row[2] is None:
        return None
    return row[1], row[2]


def total_body_rows(state: State) -> int:
    """Return the number of selectable body rows."""
    return len(selectable_body_rows(state))


def has_commit_messages(state: State) -> bool:
    """Return whether any store-owned row status has a nonblank message."""
    return commit_message_count(state) > 0


def commit_message_count(state: State) -> int:
    """Return the number of rows with nonblank store-owned messages."""
    count = 0
    for kind, repo, child in selectable_body_rows(state):
        if kind == "repo":
            status = state.store.repo_status(repo)
        elif child is not None:
            status = state.store.child_status(child)
        else:
            status = None
        if status is not None and status.message.strip():
            count += 1
    return count


def read_only_child_busy(state: State, child: ChildRef) -> bool:
    """Return store-owned read-only busy state for one child row."""
    return state.store.child_busy(child)


def read_only_child_busy_predicate(state: State) -> Callable[[ChildRef], bool]:
    """Return a relink predicate backed by selector-owned child busy state."""
    return lambda child: read_only_child_busy(state, child)


def view_load_activity_active(state: State) -> bool:
    """Return whether any active modal/view-load record is still loading."""
    load_ids = []
    if state.action_menu is not None:
        load_ids.extend([
            state.action_menu.state_load_id,
            state.action_menu.inventory_load_id,
            state.action_menu.tree_load_id,
            state.action_menu.commits_load_id,
        ])
    if state.commit_view_modal is not None:
        load_ids.extend([
            state.commit_view_modal.tags_load_id,
            state.commit_view_modal.details_load_id,
            state.commit_view_modal.files_load_id,
            state.commit_view_modal.reflog_load_id,
        ])
    if state.diff_viewer is not None:
        load_ids.extend([
            state.diff_viewer.diff_load_id,
            state.diff_viewer.log_load_id,
            state.diff_viewer.blame_load_id,
        ])
    if state.task_log_viewer is not None:
        load_ids.append(state.task_log_viewer.load_id)
    if state.branch_picker is not None:
        load_ids.append(state.branch_picker.load_id)
    if state.remote_branch_picker is not None:
        load_ids.append(state.remote_branch_picker.load_id)
    return state.view_loads.any_loading([load_id for load_id in load_ids if load_id])


def local_mutation_active_for(
        state: State,
        *,
        repos: Optional[Iterable[Repo]] = None,
        children: Optional[Iterable[ChildRef]] = None,
        include_repo_children: bool = False,
) -> bool:
    """Return whether local mutation ownership overlaps the targets."""
    repo_list = list(repos or ())
    child_list = list(children or ())
    if include_repo_children:
        known_child_ids = {
            child_id_value
            for child_id_value in (
                state.store.child_id_for(child) for child in child_list
            )
            if child_id_value is not None
        }
        for repo in repo_list:
            repo_id = state.store.repo_id_for(repo)
            if repo_id is None:
                continue
            for child_record in state.store.child_records_for_repo(repo_id):
                if child_record.child_id in known_child_ids:
                    continue
                child_list.append(child_record.child)
                known_child_ids.add(child_record.child_id)
    repo_keys = tuple(str(r.path) for r in repo_list)
    child_keys = tuple(str(c.nested_path) for c in child_list)
    repo_ids = [
        repo_id for repo_id in (
            state.store.repo_id_for(repo) for repo in repo_list
        )
        if repo_id is not None
    ]
    child_ids = [
        child_id for child_id in (
            state.store.child_id_for(child) for child in child_list
        )
        if child_id is not None
    ]
    if state.job_registry.has_active_local_mutation_for(
            repo_keys=repo_keys,
            child_keys=child_keys):
        return True
    return state.leases.has_lease_for(
        repos=repo_list,
        children=child_list,
        repo_ids=repo_ids,
        child_ids=child_ids,
    )


def any_local_mutation_active(state: State) -> bool:
    """Return whether any local mutation lease or job is active."""
    return (
        state.job_registry.has_active_local_mutation()
        or state.leases.has_leases()
    )


def read_only_row_busy_active(
        state: State,
        repos: Optional[Iterable[Repo]] = None,
) -> bool:
    """Return whether store-owned read-only row busy state is active."""
    repo_list = active_workspace_repo_rows(state) if repos is None else list(repos)
    if any(state.store.repo_busy(repo) for repo in repo_list):
        return True
    for repo in repo_list:
        repo_id = state.store.repo_id_for(repo)
        if repo_id is None:
            continue
        for child_record in state.store.child_records_for_repo(repo_id):
            if read_only_child_busy(state, child_record.child):
                return True
    return False
