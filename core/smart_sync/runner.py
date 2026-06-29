"""Smart-sync threaded execution helper."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..runtime.jobs import (
    Job,
    JobSpec,
    JobStatus,
    JobTaskBridge,
    start_job_thread,
)
from core.state.app import State
from ..git_ops import canonicalize_url, _read_gitmodules_submodules
from ..state.repos import ChildRef, Repo
from ..reconcile import reconcile_repos_bounded
from ..state.workspaces import SubtreeSpec
from .lifecycle import SmartSyncLifecycle


AlignCanonical = Callable[..., Tuple[int, int]]
PropagateParents = Callable[..., None]
RefreshRepo = Callable[[Repo], None]
SyncSubtree = Callable[[Path, str, str, str], Tuple[bool, str]]
LinkSiblings = Callable[[List[Repo], Optional[List[SubtreeSpec]]], None]
FirstLine = Callable[[str], str]
NestedSubmodules = Callable[[Path], List[Tuple[str, Path]]]


@dataclass(frozen=True)
class SmartSyncWorkPlan:
    """Pure snapshot and ownership plan for one smart-sync run."""

    snapshot_repos: List[Repo]
    snapshot_subtrees: List[SubtreeSpec]
    canonicals: List[Repo]
    subtree_items: List[Tuple[Repo, ChildRef]]
    repo_keys: Tuple[str, ...]
    child_keys: Tuple[str, ...]

    @property
    def work_count(self) -> int:
        return len(self.canonicals) + len(self.subtree_items)


def build_smart_sync_work_plan(
        state: State,
        *,
        nested_submodules_fn: NestedSubmodules = _read_gitmodules_submodules,
) -> SmartSyncWorkPlan:
    """Snapshot smart-sync work and mutation targets before job start."""
    snapshot_repos = list(state.repos)
    snapshot_subtrees = list(state.subtrees)
    _add_recursive_submodule_siblings(
        snapshot_repos,
        nested_submodules_fn=nested_submodules_fn,
    )
    canonicals = [repo for repo in snapshot_repos if repo.siblings]
    subtree_items: List[Tuple[Repo, ChildRef]] = []
    repo_keys: List[str] = []
    child_keys: List[str] = []

    def add_repo(repo: Repo) -> None:
        key = str(repo.path)
        if key not in repo_keys:
            repo_keys.append(key)

    def add_child(path: Path) -> None:
        key = str(path)
        if key not in child_keys:
            child_keys.append(key)

    for canonical in canonicals:
        add_repo(canonical)
        for parent, nested_path in canonical.siblings:
            add_repo(parent)
            add_child(nested_path)

    for parent in snapshot_repos:
        for ref in parent.children:
            if ref.kind != "subtree":
                continue
            subtree_items.append((parent, ref))
            add_repo(parent)
            add_child(ref.nested_path)

    return SmartSyncWorkPlan(
        snapshot_repos=snapshot_repos,
        snapshot_subtrees=snapshot_subtrees,
        canonicals=canonicals,
        subtree_items=subtree_items,
        repo_keys=tuple(repo_keys),
        child_keys=tuple(child_keys),
    )


def _add_recursive_submodule_siblings(
        repos: List[Repo],
        *,
        nested_submodules_fn: NestedSubmodules = _read_gitmodules_submodules,
) -> None:
    """Add transient nested-submodule edges needed only by smart-sync.

    The main UI topology deliberately shows only top-level repo rows and their
    direct children. Smart-sync needs a deeper graph: if App contains SDK and
    SDK contains Models, then Models also has a checkout at
    ``App/vendor/SDK/vendor/Models`` that must be aligned before SDK can be
    propagated upward. These transient parent repos are synthetic and are not
    inserted into ``state.repos``.
    """
    url_to_repo: Dict[str, Repo] = {
        canonicalize_url(repo.remote_url): repo
        for repo in repos
        if repo.remote_url
    }
    for repo in repos:
        repo.siblings = [
            (parent, path)
            for parent, path in repo.siblings
            if not parent.synthetic
        ]
    parent_by_path: Dict[str, Repo] = {}
    queued: List[Tuple[Repo, Path]] = []
    seen_paths: set[str] = set()

    def path_key(path: Path) -> str:
        try:
            return str(path.resolve())
        except OSError:
            return str(path)

    def make_parent(repo: Repo, path: Path) -> Repo:
        key = path_key(path)
        existing = parent_by_path.get(key)
        if existing is not None:
            return existing
        parent = Repo(
            rel=f"{repo.rel}@{path.name}",
            path=path,
            branch=repo.branch,
            remote_url=repo.remote_url,
            remote_url_raw=repo.remote_url_raw,
            synthetic=True,
        )
        parent_by_path[key] = parent
        return parent

    def add_edge(target: Repo, parent: Repo, sub_path: Path) -> None:
        sub_key = path_key(sub_path)
        for _existing_parent, existing_path in target.siblings:
            if path_key(existing_path) == sub_key:
                return
        child = ChildRef(repo=target, nested_path=sub_path, kind="submodule")
        parent.children.append(child)
        target.siblings.append((parent, sub_path))

    for canonical in repos:
        for _parent, sibling_path in canonical.siblings:
            key = path_key(sibling_path)
            if key not in seen_paths:
                queued.append((canonical, sibling_path))
                seen_paths.add(key)

    while queued:
        checkout_repo, checkout_path = queued.pop(0)
        transient_parent = make_parent(checkout_repo, checkout_path)
        for url, sub_path in nested_submodules_fn(checkout_path):
            target = url_to_repo.get(canonicalize_url(url))
            if target is None or target is checkout_repo:
                continue
            add_edge(target, transient_parent, sub_path)
            sub_key = path_key(sub_path)
            if sub_key not in seen_paths:
                queued.append((target, sub_path))
                seen_paths.add(sub_key)


@dataclass(frozen=True)
class SmartSyncRunConfig:
    """Dependencies and snapshots required to execute one smart-sync job."""

    state: State
    snapshot_repos: List[Repo]
    snapshot_subtrees: List[SubtreeSpec]
    canonicals: List[Repo]
    subtree_items: List[Tuple[Repo, ChildRef]]
    lifecycle: SmartSyncLifecycle
    align_canonical: AlignCanonical
    propagate_parents: PropagateParents
    refresh_repo: RefreshRepo
    sync_subtree: SyncSubtree
    link_siblings: LinkSiblings
    first_line: FirstLine
    task_bridge: Optional[JobTaskBridge] = None


def run_smart_sync_job(job: Job, config: SmartSyncRunConfig) -> None:
    """Execute smart-sync work under an already-acquired lifecycle."""
    ok_total = 0
    fail_total = 0
    cancelled = False
    tasks = config.task_bridge or JobTaskBridge(config.state.tasks)
    try:
        for canonical in config.canonicals:
            if job.cancel_event.is_set():
                cancelled = True
                break
            try:
                ok, fail = _align_canonical(
                    config,
                    canonical,
                    tasks,
                    job.cancel_event,
                )
            except Exception as exc:  # noqa: BLE001
                t = tasks.add(
                    f"  ↳ align {config.state.task_repo_label(canonical)}")
                tasks.update(t, "fail", config.first_line(str(exc)))
                ok, fail = 0, 1
            finally:
                try:
                    config.refresh_repo(canonical)
                except Exception:  # noqa: BLE001
                    pass
                config.lifecycle.record_canonical_result(canonical, fail)
            if job.cancel_event.is_set():
                cancelled = True
                break
            ok_total += ok
            fail_total += fail

        if (
                not cancelled
                and config.state.auto_push_submodule_parent
                and config.canonicals
        ):
            try:
                _propagate_parents(
                    config,
                    tasks,
                    job.cancel_event,
                )
            except Exception as exc:  # noqa: BLE001
                t = tasks.add("  ↳ propagate to parents")
                tasks.update(t, "fail", config.first_line(str(exc)))
                fail_total += 1

        for parent, ref in config.subtree_items:
            if job.cancel_event.is_set():
                cancelled = True
                break
            t = tasks.add(
                f"  ⊕ {config.state.task_repo_label(ref.repo)} "
                f"in {config.state.task_repo_label(parent)}")
            ok_this = False
            try:
                try:
                    prefix = str(ref.nested_path.relative_to(parent.path))
                except ValueError:
                    prefix = ""
                ok, msg = config.sync_subtree(
                    parent.path,
                    prefix,
                    ref.repo.remote_url_raw or "",
                    ref.repo.branch,
                )
                tasks.update(t, "ok" if ok else "fail", msg)
                ok_this = ok
                if ok:
                    ok_total += 1
                else:
                    fail_total += 1
            finally:
                try:
                    config.refresh_repo(parent)
                except Exception:  # noqa: BLE001
                    pass
                config.lifecycle.record_subtree_result(ref, ok_this)
            if job.cancel_event.is_set():
                cancelled = True
                break
    finally:
        if cancelled or job.cancel_event.is_set():
            config.lifecycle.cancel(job)
        else:
            _run_final_cleanup(job, config, ok_total, fail_total)


def _propagate_parents(
        config: SmartSyncRunConfig,
        tasks: JobTaskBridge,
        cancel_event: threading.Event,
) -> None:
    try:
        config.propagate_parents(
            config.state,
            config.canonicals,
            task_bridge=tasks,
            cancel_event=cancel_event,
        )
    except TypeError as exc:
        if "task_bridge" not in str(exc) and "cancel_event" not in str(exc):
            raise
        config.propagate_parents(config.state, config.canonicals)


def _align_canonical(
        config: SmartSyncRunConfig,
        canonical: Repo,
        tasks: JobTaskBridge,
        cancel_event: threading.Event,
) -> Tuple[int, int]:
    try:
        return config.align_canonical(
            config.state,
            canonical,
            task_bridge=tasks,
            cancel_event=cancel_event,
        )
    except TypeError as exc:
        if "task_bridge" not in str(exc) and "cancel_event" not in str(exc):
            raise
        return config.align_canonical(config.state, canonical)


def _run_final_cleanup(
        job: Job,
        config: SmartSyncRunConfig,
        ok_total: int,
    fail_total: int,
) -> None:
    should_cleanup = ok_total + fail_total > 0
    config.lifecycle.finish(job, ok_total, fail_total)
    if should_cleanup:
        _kick_off_refresh_cleanup(config)


def _kick_off_refresh_cleanup(config: SmartSyncRunConfig) -> None:
    cleanup_job = config.state.job_registry.start(
        JobSpec(
            kind="smart-sync-cleanup",
            label="smart-sync refresh cleanup",
            local_mutation=False,
            repo_keys=tuple(str(repo.path) for repo in config.snapshot_repos),
        )
    )
    task_bridge = JobTaskBridge(
        config.state.tasks, config.state.job_registry, cleanup_job)
    cleanup_task = task_bridge.add("  ↳ smart-sync refresh cleanup")

    def worker(cleanup_job: Job) -> None:
        cleanup_result = reconcile_repos_bounded(
            config.snapshot_repos,
            config.snapshot_subtrees,
            refresh_fn=config.refresh_repo,
            link_fn=config.link_siblings,
            max_workers=1,
            should_stop=cleanup_job.cancel_event.is_set,
        )
        if cleanup_job.cancel_event.is_set():
            task_bridge.update(cleanup_task, "warn", "cancelled")
            return
        if cleanup_result.failed:
            parts: List[str] = []
            if cleanup_result.refresh.failed:
                parts.append(f"{cleanup_result.refresh.failed} refresh failed")
            if cleanup_result.link_error:
                parts.append("1 link failed")
            message = ", ".join(parts)
            task_bridge.update(cleanup_task, "warn", message)
            config.state.job_registry.finish(
                cleanup_job, JobStatus.WARN, message)
            return
        task_bridge.update(cleanup_task, "ok", "refreshed")

    thread = start_job_thread(config.state.job_registry, cleanup_job, worker)
    if thread is None:
        task_bridge.update(
            cleanup_task, "fail", config.first_line(cleanup_job.message))
