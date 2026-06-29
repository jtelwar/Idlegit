"""Authoritative runtime index for workspace repo state."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

from .ids import ChildId, RepoId, WorkspaceId, child_id, repo_id, workspace_id


T = TypeVar("T")


@dataclass(frozen=True)
class RepoRecord:
    """A repo row registered in the runtime store."""

    repo_id: RepoId
    workspace_id: WorkspaceId
    rel: str
    path: Path
    repo: object


@dataclass(frozen=True)
class RepoStatusSnapshot:
    """Store-owned status facts for one repo row."""

    branch: str = ""
    head: str = ""
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    dirty: bool = False
    message: str = ""
    error: str = ""
    merging: bool = False


@dataclass(frozen=True)
class ChildRecord:
    """A nested repo row registered in the runtime store."""

    child_id: ChildId
    parent_repo_id: RepoId
    repo_id: RepoId
    kind: str
    nested_path: Path
    child: object


@dataclass(frozen=True)
class ChildStatusSnapshot:
    """Store-owned status facts for one nested repo row."""

    kind: str = ""
    branch: str = ""
    head: str = ""
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    dirty: bool = False
    message: str = ""
    error: str = ""
    merging: bool = False
    in_sync: bool = True


@dataclass(frozen=True)
class WorkflowIntentSnapshot:
    """Store-owned post-push workflow intent for one canonical repo."""

    track_workflow: Dict[str, bool] = field(default_factory=dict)
    then_run_after_push: str = ""
    then_run_params_after_push: Dict[str, str] = field(default_factory=dict)
    then_run_after_workflow: Dict[str, str] = field(default_factory=dict)
    then_run_params_after_workflow: Dict[str, Dict[str, str]] = field(
        default_factory=dict)

    @property
    def empty(self) -> bool:
        return (
            not self.track_workflow
            and not self.then_run_after_push
            and not self.then_run_params_after_push
            and not self.then_run_after_workflow
            and not self.then_run_params_after_workflow
        )


@dataclass(frozen=True)
class ChildTopologySnapshot:
    """Store-ready nested repo membership plus its status facts."""

    parent_repo: object
    child: object
    status: ChildStatusSnapshot


@dataclass(frozen=True)
class WorkspaceRecord:
    """Workspace row membership registered in the runtime store."""

    workspace_id: WorkspaceId
    name: str
    folders: Tuple[Path, ...]
    repo_ids: Tuple[RepoId, ...]


class StateStore:
    """Thread-safe single index for live workspace, repo, and child objects."""

    def __init__(self, on_change: Optional[Callable[[], None]] = None) -> None:
        self._lock = threading.Lock()
        self._workspaces: Dict[WorkspaceId, WorkspaceRecord] = {}
        self._repos: Dict[RepoId, RepoRecord] = {}
        self._children: Dict[ChildId, ChildRecord] = {}
        self._repo_statuses: Dict[RepoId, RepoStatusSnapshot] = {}
        self._child_statuses: Dict[ChildId, ChildStatusSnapshot] = {}
        self._repo_workflow_intents: Dict[RepoId, WorkflowIntentSnapshot] = {}
        self._repo_by_object: Dict[int, RepoId] = {}
        self._child_by_object: Dict[int, ChildId] = {}
        self._repo_refresh_locks: Dict[RepoId, threading.Lock] = {}
        self._child_refresh_locks: Dict[ChildId, threading.Lock] = {}
        self._busy_repo_ids: Dict[RepoId, int] = {}
        self._busy_child_ids: Dict[ChildId, int] = {}
        self._suggesting_repo_ids: set[RepoId] = set()
        self._suggesting_child_ids: set[ChildId] = set()
        self._active_workspace_id: Optional[WorkspaceId] = None
        self.on_change = on_change

    def replace_workspace(
            self,
            *,
            name: str,
            folders: Iterable[Path],
            repos: Iterable[object],
            notify: bool = True,
    ) -> WorkspaceId:
        """Replace a workspace's repo/child membership atomically."""
        repo_list = list(repos)
        children = [
            ChildTopologySnapshot(
                parent_repo=repo,
                child=child,
                status=_child_status_snapshot(child),
            )
            for repo in repo_list
            for child in _children(repo)
        ]
        return self.replace_workspace_topology(
            name=name,
            folders=folders,
            repos=repo_list,
            children=children,
            notify=notify,
        )

    def replace_workspace_topology(
            self,
            *,
            name: str,
            folders: Iterable[Path],
            repos: Iterable[object],
            children: Iterable[ChildTopologySnapshot],
            notify: bool = True,
            activate: bool = True,
    ) -> WorkspaceId:
        """Replace workspace membership from explicit repo/child snapshots."""
        workspace = workspace_id(name)
        repo_list = list(repos)
        child_list = list(children)
        folder_tuple = tuple(Path(folder) for folder in folders)
        next_repos: Dict[RepoId, RepoRecord] = {}
        next_children: Dict[ChildId, ChildRecord] = {}
        next_repo_statuses: Dict[RepoId, RepoStatusSnapshot] = {}
        next_child_statuses: Dict[ChildId, ChildStatusSnapshot] = {}
        next_repo_objects: Dict[int, RepoId] = {}
        next_child_objects: Dict[int, ChildId] = {}
        repo_ids: List[RepoId] = []

        for repo in repo_list:
            rid = repo_id(workspace, _repo_path(repo))
            repo_status = _repo_status_snapshot(repo)
            with self._lock:
                previous_status = self._repo_statuses.get(rid)
            if previous_status is not None:
                repo_status = replace(repo_status, message=previous_status.message)
            repo_ids.append(rid)
            next_repos[rid] = RepoRecord(
                repo_id=rid,
                workspace_id=workspace,
                rel=str(getattr(repo, "rel", "")),
                path=_repo_path(repo),
                repo=repo,
            )
            self._ensure_repo_refresh_lock(rid)
            next_repo_statuses[rid] = repo_status
            next_repo_objects[id(repo)] = rid

        for child_snapshot in child_list:
            parent = child_snapshot.parent_repo
            child = child_snapshot.child
            parent_id = repo_id(workspace, _repo_path(parent))
            canonical = getattr(child, "repo", None)
            canonical_id = (
                repo_id(workspace, _repo_path(canonical))
                if canonical is not None else parent_id
            )
            cid = child_id(
                parent_id,
                _child_path(child),
                str(getattr(child, "kind", "")),
            )
            next_children[cid] = ChildRecord(
                child_id=cid,
                parent_repo_id=parent_id,
                repo_id=canonical_id,
                kind=str(getattr(child, "kind", "")),
                nested_path=_child_path(child),
                child=child,
            )
            self._ensure_child_refresh_lock(cid)
            child_status = child_snapshot.status
            with self._lock:
                previous_child_status = self._child_statuses.get(cid)
            if previous_child_status is not None:
                child_status = replace(
                    child_status,
                    message=previous_child_status.message,
                )
            next_child_statuses[cid] = child_status
            next_child_objects[id(child)] = cid

        with self._lock:
            self._workspaces[workspace] = WorkspaceRecord(
                workspace_id=workspace,
                name=name,
                folders=folder_tuple,
                repo_ids=tuple(repo_ids),
            )
            self._drop_workspace_locked(
                workspace,
                keep_repo_ids=set(next_repos),
                keep_child_ids=set(next_children),
            )
            self._repos.update(next_repos)
            self._children.update(next_children)
            self._repo_statuses.update(next_repo_statuses)
            self._child_statuses.update(next_child_statuses)
            self._repo_by_object = _without_values(
                self._repo_by_object, set(repo_ids))
            self._repo_by_object.update(next_repo_objects)
            self._child_by_object = _without_values(
                self._child_by_object, set(next_children))
            self._child_by_object.update(next_child_objects)
            self._busy_repo_ids = {
                rid: count for rid, count in self._busy_repo_ids.items()
                if rid in self._repos and count > 0
            }
            self._busy_child_ids = {
                cid: count for cid, count in self._busy_child_ids.items()
                if cid in self._children and count > 0
            }
            self._suggesting_repo_ids &= set(self._repos)
            self._suggesting_child_ids &= set(self._children)
            if activate:
                self._active_workspace_id = workspace
        if notify:
            self._notify_change()
        return workspace

    @property
    def active_workspace_id(self) -> Optional[WorkspaceId]:
        with self._lock:
            return self._active_workspace_id

    def workspace_record(
            self, workspace: WorkspaceId) -> Optional[WorkspaceRecord]:
        with self._lock:
            return self._workspaces.get(workspace)

    def repo_record(self, rid: RepoId) -> Optional[RepoRecord]:
        with self._lock:
            return self._repos.get(rid)

    def child_record(self, cid: ChildId) -> Optional[ChildRecord]:
        with self._lock:
            return self._children.get(cid)

    def repo_id_for(self, repo: object) -> Optional[RepoId]:
        with self._lock:
            return self._repo_by_object.get(id(repo))

    def child_id_for(self, child: object) -> Optional[ChildId]:
        with self._lock:
            return self._child_by_object.get(id(child))

    def child_id_for_parent_child(
            self,
            parent: object,
            child: object,
    ) -> Optional[ChildId]:
        """Return a child id by object identity, then stable row key.

        Rendering can hold a child object captured just before a topology
        replacement. The replacement may remap the same stable row id to a
        fresh ChildRef object before the row is drawn, so object identity alone
        is too narrow for that path.
        """
        with self._lock:
            child_object_id = self._child_by_object.get(id(child))
            if child_object_id is not None and child_object_id in self._children:
                return child_object_id
            parent_id = self._repo_by_object.get(id(parent))
            if parent_id is None:
                return None
            stable_child_id = child_id(
                parent_id,
                _child_path(child),
                str(getattr(child, "kind", "")),
            )
            if stable_child_id not in self._children:
                return None
            return stable_child_id

    def repo_records_for_workspace(
            self, workspace: WorkspaceId) -> List[RepoRecord]:
        with self._lock:
            record = self._workspaces.get(workspace)
            if record is None:
                return []
            return [
                self._repos[rid]
                for rid in record.repo_ids
                if rid in self._repos
            ]

    def child_records_for_repo(self, rid: RepoId) -> List[ChildRecord]:
        with self._lock:
            return [
                record for record in self._children.values()
                if record.parent_repo_id == rid
            ]

    def publish_repo_status(self, repo: object) -> None:
        """Publish the current repo object status into the store."""
        rid = self.repo_id_for(repo)
        if rid is None:
            return
        with self._lock:
            self._publish_repo_status_locked(rid, repo)
        self._notify_change()

    def publish_repo_status_snapshot(
            self,
            repo: object,
            snapshot: RepoStatusSnapshot,
    ) -> None:
        """Publish already-computed status facts for a repo row.

        This is the Phase 1B path: refresh producers return typed snapshots and
        the store applies them without rereading mutable row-object fields.
        """
        rid = self.repo_id_for(repo)
        if rid is None:
            return
        self.publish_repo_status_snapshot_by_id(rid, snapshot)

    def publish_repo_status_snapshot_by_id(
            self,
            rid: RepoId,
            snapshot: RepoStatusSnapshot,
    ) -> None:
        """Publish already-computed status facts for one repo id."""
        with self._lock:
            if rid not in self._repos:
                return
            self._repo_statuses[rid] = snapshot
        self._notify_change()

    def publish_child_status(self, child: object) -> None:
        """Publish the current nested row status into the store."""
        cid = self.child_id_for(child)
        if cid is None:
            return
        with self._lock:
            self._publish_child_status_locked(cid, child)
        self._notify_change()

    def publish_child_status_snapshot(
            self,
            child: object,
            snapshot: ChildStatusSnapshot,
    ) -> None:
        """Publish already-computed status facts for a nested row."""
        cid = self.child_id_for(child)
        if cid is None:
            return
        self.publish_child_status_snapshot_by_id(cid, snapshot)

    def publish_child_status_snapshot_by_id(
            self,
            cid: ChildId,
            snapshot: ChildStatusSnapshot,
    ) -> None:
        """Publish already-computed status facts for one child id."""
        with self._lock:
            if cid not in self._children:
                return
            self._child_statuses[cid] = snapshot
        self._notify_change()

    def publish_row_status(self, row: object) -> None:
        """Publish status for a repo or nested row object."""
        with self._lock:
            rid = self._repo_by_object.get(id(row))
            if rid is not None:
                self._publish_repo_status_locked(rid, row)
                changed = True
            else:
                changed = False
            cid = self._child_by_object.get(id(row))
            if cid is not None:
                self._publish_child_status_locked(cid, row)
                changed = True
        if changed:
            self._notify_change()

    def row_message(self, row: object) -> str:
        """Return the store-owned commit message for a repo or nested row."""
        with self._lock:
            rid = self._repo_by_object.get(id(row))
            if rid is not None:
                status = self._repo_statuses.get(rid)
                return "" if status is None else status.message
            cid = self._child_by_object.get(id(row))
            if cid is not None:
                status = self._child_statuses.get(cid)
                return "" if status is None else status.message
        return ""

    def set_row_message(self, row: object, message: str) -> None:
        """Set the store-owned commit message for a repo or nested row."""
        value = str(message)
        changed = False
        with self._lock:
            rid = self._repo_by_object.get(id(row))
            if rid is not None:
                status = self._repo_statuses.get(rid)
                if status is None:
                    self._publish_repo_status_locked(rid, row)
                else:
                    self._repo_statuses[rid] = replace(
                        status, message=value)
                changed = True
            cid = self._child_by_object.get(id(row))
            if cid is not None:
                status = self._child_statuses.get(cid)
                if status is None:
                    self._publish_child_status_locked(cid, row)
                else:
                    self._child_statuses[cid] = replace(
                        status, message=value)
                changed = True
        if changed:
            self._notify_change()

    def repo_workflow_intent(self, repo: object) -> WorkflowIntentSnapshot:
        """Return store-owned workflow intent for a canonical repo row."""
        rid = self.repo_id_for(repo)
        if rid is None:
            return WorkflowIntentSnapshot()
        return self.repo_workflow_intent_by_id(rid)

    def repo_workflow_intent_by_id(
            self, rid: RepoId) -> WorkflowIntentSnapshot:
        """Return store-owned workflow intent for a stable repo id."""
        with self._lock:
            return _copy_workflow_intent(
                self._repo_workflow_intents.get(rid))

    def set_repo_workflow_intent(
            self,
            repo: object,
            intent: WorkflowIntentSnapshot,
    ) -> None:
        """Replace store-owned workflow intent for a canonical repo row."""
        rid = self.repo_id_for(repo)
        if rid is None:
            return
        self.set_repo_workflow_intent_by_id(rid, intent)

    def set_repo_workflow_intent_by_id(
            self,
            rid: RepoId,
            intent: WorkflowIntentSnapshot,
    ) -> None:
        """Replace store-owned workflow intent for a stable repo id."""
        stored = _copy_workflow_intent(intent)
        changed = False
        with self._lock:
            if rid not in self._repos:
                return
            if stored.empty:
                changed = rid in self._repo_workflow_intents
                self._repo_workflow_intents.pop(rid, None)
            else:
                changed = self._repo_workflow_intents.get(rid) != stored
                self._repo_workflow_intents[rid] = stored
        if changed:
            self._notify_change()

    def take_repo_workflow_intent(self, repo: object) -> WorkflowIntentSnapshot:
        """Return and clear store-owned workflow intent for one repo row."""
        rid = self.repo_id_for(repo)
        if rid is None:
            return WorkflowIntentSnapshot()
        with self._lock:
            stored = self._repo_workflow_intents.pop(rid, None)
        if stored is not None:
            self._notify_change()
        return _copy_workflow_intent(stored)

    def pop_repo_then_run_after_workflow(
            self,
            repo: object,
            workflow_name: str,
    ) -> Tuple[str, Dict[str, str]]:
        """Return and clear one workflow-completion follow-up slot."""
        rid = self.repo_id_for(repo)
        if rid is None:
            return "", {}
        changed = False
        with self._lock:
            intent = _copy_workflow_intent(
                self._repo_workflow_intents.get(rid))
            target = intent.then_run_after_workflow.pop(workflow_name, "")
            params = intent.then_run_params_after_workflow.pop(
                workflow_name, {})
            if target or params:
                changed = True
            if changed:
                if intent.empty:
                    self._repo_workflow_intents.pop(rid, None)
                else:
                    self._repo_workflow_intents[rid] = intent
        if changed:
            self._notify_change()
        return target, dict(params)

    def publish_workspace_statuses(self, repos: Iterable[object]) -> None:
        """Publish status facts for a workspace repo snapshot."""
        repo_list = list(repos)
        with self._lock:
            for repo in repo_list:
                rid = self._repo_by_object.get(id(repo))
                if rid is not None:
                    self._publish_repo_status_locked(rid, repo)
                for child in _children(repo):
                    cid = self._child_by_object.get(id(child))
                    if cid is not None:
                        self._publish_child_status_locked(cid, child)
        self._notify_change()

    def repo_status(self, repo: object) -> Optional[RepoStatusSnapshot]:
        """Return store-owned status facts for one repo object."""
        rid = self.repo_id_for(repo)
        if rid is None:
            return None
        return self.repo_status_by_id(rid)

    def repo_status_by_id(
            self, rid: RepoId) -> Optional[RepoStatusSnapshot]:
        """Return store-owned status facts for one repo id."""
        with self._lock:
            return self._repo_statuses.get(rid)

    def child_status(self, child: object) -> Optional[ChildStatusSnapshot]:
        """Return store-owned status facts for one nested row object."""
        cid = self.child_id_for(child)
        if cid is None:
            return None
        return self.child_status_by_id(cid)

    def child_status_by_id(
            self, cid: ChildId) -> Optional[ChildStatusSnapshot]:
        """Return store-owned status facts for one child id."""
        with self._lock:
            return self._child_statuses.get(cid)

    def acquire_repo_refresh(
            self,
            repo: object,
            *,
            timeout: Optional[float] = None,
    ) -> Tuple[bool, Optional[RepoId]]:
        """Acquire the Store-owned refresh mutex for a repo row."""
        rid = self.repo_id_for(repo)
        if rid is None:
            return False, None
        return self.acquire_repo_refresh_by_id(rid, timeout=timeout), rid

    def acquire_child_refresh(
            self,
            child: object,
            *,
            timeout: Optional[float] = None,
    ) -> Tuple[bool, Optional[ChildId]]:
        """Acquire the Store-owned refresh mutex for a child row."""
        cid = self.child_id_for(child)
        if cid is None:
            return False, None
        return self.acquire_child_refresh_by_id(cid, timeout=timeout), cid

    def acquire_repo_refresh_by_id(
            self,
            rid: RepoId,
            *,
            timeout: Optional[float] = None,
    ) -> bool:
        lock = self._repo_refresh_lock_for(rid)
        return _acquire_lock(lock, timeout)

    def acquire_child_refresh_by_id(
            self,
            cid: ChildId,
            *,
            timeout: Optional[float] = None,
    ) -> bool:
        lock = self._child_refresh_lock_for(cid)
        return _acquire_lock(lock, timeout)

    def release_repo_refresh_by_id(self, rid: Optional[RepoId]) -> None:
        if rid is None:
            return
        lock = self._repo_refresh_lock_for(rid)
        _release_lock(lock)

    def release_child_refresh_by_id(self, cid: Optional[ChildId]) -> None:
        if cid is None:
            return
        lock = self._child_refresh_lock_for(cid)
        _release_lock(lock)

    def set_repo_busy(self, repo: object, value: bool) -> None:
        """Set read-only row-busy state for a registered repo object."""
        rid = self.repo_id_for(repo)
        if rid is None:
            return
        changed = self.set_repo_busy_by_id(rid, value)
        if changed:
            self._notify_change()

    def set_child_busy(self, child: object, value: bool) -> None:
        """Set read-only row-busy state for a registered child object."""
        cid = self.child_id_for(child)
        if cid is None:
            return
        changed = self.set_child_busy_by_id(cid, value)
        if changed:
            self._notify_change()

    def set_repo_busy_by_id(self, rid: RepoId, value: bool) -> bool:
        """Set read-only row-busy state by stable repo id."""
        with self._lock:
            if value:
                previous = self._busy_repo_ids.get(rid, 0)
                self._busy_repo_ids[rid] = previous + 1
                return previous == 0
            previous = self._busy_repo_ids.get(rid, 0)
            if previous <= 0:
                return False
            if previous == 1:
                self._busy_repo_ids.pop(rid, None)
                return True
            self._busy_repo_ids[rid] = previous - 1
            return False

    def set_child_busy_by_id(self, cid: ChildId, value: bool) -> bool:
        """Set read-only row-busy state by stable child id."""
        with self._lock:
            if value:
                previous = self._busy_child_ids.get(cid, 0)
                self._busy_child_ids[cid] = previous + 1
                return previous == 0
            previous = self._busy_child_ids.get(cid, 0)
            if previous <= 0:
                return False
            if previous == 1:
                self._busy_child_ids.pop(cid, None)
                return True
            self._busy_child_ids[cid] = previous - 1
            return False

    def set_busy_snapshot_by_id(
            self,
            rid: Optional[RepoId],
            child_ids: Iterable[ChildId],
            value: bool,
    ) -> None:
        """Set one repo and a captured set of child ids busy together."""
        changed = False
        with self._lock:
            if rid is not None and rid in self._repos:
                changed = self._set_repo_busy_by_id_locked(rid, value) or changed
            for cid in child_ids:
                if cid in self._children:
                    changed = self._set_child_busy_by_id_locked(
                        cid, value) or changed
        if changed:
            self._notify_change()

    def repo_busy(self, repo: object) -> bool:
        """Return read-only row-busy state for a registered repo object."""
        rid = self.repo_id_for(repo)
        if rid is None:
            return False
        with self._lock:
            return self._busy_repo_ids.get(rid, 0) > 0

    def child_busy(self, child: object) -> bool:
        """Return read-only row-busy state for a registered child object."""
        cid = self.child_id_for(child)
        if cid is None:
            return False
        return self.child_busy_by_id(cid)

    def child_busy_by_id(self, cid: ChildId) -> bool:
        """Return read-only row-busy state for one child id."""
        with self._lock:
            return self._busy_child_ids.get(cid, 0) > 0

    def set_repo_suggesting(self, repo: object, value: bool) -> None:
        """Set background suggestion state for a registered repo row."""
        rid = self.repo_id_for(repo)
        if rid is None:
            return
        changed = self.set_repo_suggesting_by_id(rid, value)
        if changed:
            self._notify_change()

    def set_child_suggesting(self, child: object, value: bool) -> None:
        """Set background suggestion state for a registered child row."""
        cid = self.child_id_for(child)
        if cid is None:
            return
        changed = self.set_child_suggesting_by_id(cid, value)
        if changed:
            self._notify_change()

    def set_repo_suggesting_by_id(self, rid: RepoId, value: bool) -> bool:
        """Set background suggestion state by stable repo id."""
        with self._lock:
            if value:
                if rid in self._suggesting_repo_ids:
                    return False
                self._suggesting_repo_ids.add(rid)
                return True
            if rid not in self._suggesting_repo_ids:
                return False
            self._suggesting_repo_ids.remove(rid)
            return True

    def set_child_suggesting_by_id(self, cid: ChildId, value: bool) -> bool:
        """Set background suggestion state by stable child id."""
        with self._lock:
            if value:
                if cid in self._suggesting_child_ids:
                    return False
                self._suggesting_child_ids.add(cid)
                return True
            if cid not in self._suggesting_child_ids:
                return False
            self._suggesting_child_ids.remove(cid)
            return True

    def repo_suggesting(self, repo: object) -> bool:
        """Return background suggestion state for a registered repo row."""
        rid = self.repo_id_for(repo)
        if rid is None:
            return False
        with self._lock:
            return rid in self._suggesting_repo_ids

    def child_suggesting(self, child: object) -> bool:
        """Return background suggestion state for a registered child row."""
        cid = self.child_id_for(child)
        if cid is None:
            return False
        return self.child_suggesting_by_id(cid)

    def child_suggesting_by_id(self, cid: ChildId) -> bool:
        """Return background suggestion state for one child id."""
        with self._lock:
            return cid in self._suggesting_child_ids

    def _drop_workspace_locked(
            self,
            workspace: WorkspaceId,
            *,
            keep_repo_ids: Optional[set[RepoId]] = None,
            keep_child_ids: Optional[set[ChildId]] = None,
    ) -> None:
        keep_repo_ids = keep_repo_ids or set()
        keep_child_ids = keep_child_ids or set()
        stale_repo_ids = [
            rid for rid, record in self._repos.items()
            if record.workspace_id == workspace
        ]
        stale_repo_set = set(stale_repo_ids)
        stale_child_ids = [
            cid for cid, record in self._children.items()
            if record.parent_repo_id in stale_repo_set
        ]
        for rid in stale_repo_ids:
            self._repos.pop(rid, None)
        for cid in stale_child_ids:
            self._children.pop(cid, None)
        self._repo_by_object = _without_values(
            self._repo_by_object, stale_repo_set)
        self._child_by_object = _without_values(
            self._child_by_object, set(stale_child_ids))
        stale_repo_drop = stale_repo_set - keep_repo_ids
        stale_child_drop = set(stale_child_ids) - keep_child_ids
        for rid in stale_repo_drop:
            self._busy_repo_ids.pop(rid, None)
            self._repo_statuses.pop(rid, None)
            self._repo_workflow_intents.pop(rid, None)
        for cid in stale_child_drop:
            self._busy_child_ids.pop(cid, None)
            self._child_statuses.pop(cid, None)

    def _publish_repo_status_locked(
            self,
            rid: RepoId,
            repo: object,
    ) -> None:
        self._repo_statuses[rid] = _repo_status_snapshot(repo)

    def _publish_child_status_locked(
            self,
            cid: ChildId,
            child: object,
    ) -> None:
        self._child_statuses[cid] = _child_status_snapshot(child)

    def _ensure_repo_refresh_lock(self, rid: RepoId) -> None:
        with self._lock:
            self._repo_refresh_locks.setdefault(rid, threading.Lock())

    def _ensure_child_refresh_lock(self, cid: ChildId) -> None:
        with self._lock:
            self._child_refresh_locks.setdefault(cid, threading.Lock())

    def _repo_refresh_lock_for(self, rid: RepoId) -> threading.Lock:
        with self._lock:
            return self._repo_refresh_locks.setdefault(rid, threading.Lock())

    def _child_refresh_lock_for(self, cid: ChildId) -> threading.Lock:
        with self._lock:
            return self._child_refresh_locks.setdefault(cid, threading.Lock())

    def _set_repo_busy_by_id_locked(self, rid: RepoId, value: bool) -> bool:
        if value:
            previous = self._busy_repo_ids.get(rid, 0)
            self._busy_repo_ids[rid] = previous + 1
            return previous == 0
        previous = self._busy_repo_ids.get(rid, 0)
        if previous <= 0:
            return False
        if previous == 1:
            self._busy_repo_ids.pop(rid, None)
            return True
        self._busy_repo_ids[rid] = previous - 1
        return False

    def _set_child_busy_by_id_locked(self, cid: ChildId, value: bool) -> bool:
        if value:
            previous = self._busy_child_ids.get(cid, 0)
            self._busy_child_ids[cid] = previous + 1
            return previous == 0
        previous = self._busy_child_ids.get(cid, 0)
        if previous <= 0:
            return False
        if previous == 1:
            self._busy_child_ids.pop(cid, None)
            return True
        self._busy_child_ids[cid] = previous - 1
        return False

    def _notify_change(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception:  # noqa: BLE001
            pass


def _repo_path(repo: object) -> Path:
    path = getattr(repo, "path", None)
    if path is None:
        return Path("")
    return Path(path)


def _child_path(child: object) -> Path:
    path = getattr(child, "nested_path", None)
    if path is None:
        return Path("")
    return Path(path)


def _children(repo: object) -> List[object]:
    value = getattr(repo, "children", [])
    return list(value)


def _repo_status_snapshot(repo: object) -> RepoStatusSnapshot:
    staged = list(getattr(repo, "staged", []))
    unstaged = list(getattr(repo, "unstaged", []))
    untracked = list(getattr(repo, "untracked", []))
    return RepoStatusSnapshot(
        branch=str(getattr(repo, "branch", "")),
        head=str(getattr(repo, "head", "")),
        upstream=getattr(repo, "upstream", None),
        ahead=int(getattr(repo, "ahead", 0) or 0),
        behind=int(getattr(repo, "behind", 0) or 0),
        dirty=bool(staged or unstaged or untracked),
        message="",
        error=str(getattr(repo, "error", "")),
        merging=bool(getattr(repo, "merging", False)),
    )


def _child_status_snapshot(child: object) -> ChildStatusSnapshot:
    return ChildStatusSnapshot(
        kind=str(getattr(child, "kind", "")),
        branch=str(getattr(child, "branch", "")),
        head=str(getattr(child, "head", "")),
        upstream=getattr(child, "upstream", None),
        ahead=int(getattr(child, "ahead", 0) or 0),
        behind=int(getattr(child, "behind", 0) or 0),
        dirty=bool(getattr(child, "dirty", False)),
        message="",
        error=str(getattr(child, "error", "")),
        merging=bool(getattr(child, "merging", False)),
        in_sync=bool(getattr(child, "in_sync", True)),
    )


def _copy_workflow_intent(
        intent: Optional[WorkflowIntentSnapshot]) -> WorkflowIntentSnapshot:
    if intent is None:
        return WorkflowIntentSnapshot()
    return WorkflowIntentSnapshot(
        track_workflow=dict(intent.track_workflow),
        then_run_after_push=str(intent.then_run_after_push),
        then_run_params_after_push=dict(intent.then_run_params_after_push),
        then_run_after_workflow=dict(intent.then_run_after_workflow),
        then_run_params_after_workflow={
            key: dict(value)
            for key, value in intent.then_run_params_after_workflow.items()
        },
    )


def _acquire_lock(lock: threading.Lock, timeout: Optional[float]) -> bool:
    if timeout is None:
        return lock.acquire(blocking=False)
    return lock.acquire(timeout=timeout)


def _release_lock(lock: threading.Lock) -> None:
    try:
        lock.release()
    except RuntimeError:
        return


def _without_values(mapping: Dict[int, T], stale_values: set[T]) -> Dict[int, T]:
    if not stale_values:
        return dict(mapping)
    return {
        key: value
        for key, value in mapping.items()
        if value not in stale_values
    }
