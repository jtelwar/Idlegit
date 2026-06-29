"""Safe submodule-parent propagation helpers."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from ..git_ops import (
    first_line,
    git,
    git_cancellable,
    is_safe_ref_arg,
    refresh_repo,
    submodule_pointer_change_paths,
)
from ..runtime.claims import WorkerClaim
from ..runtime.jobs import JobTaskBridge
from core.state.app import State
from ..state.repos import ChildRef, Repo


FindChildAt = Callable[[Optional[Repo], Path], Optional[ChildRef]]
PropagateParent = Callable[..., str]
FastForwardSubmodule = Callable[[Path, str, str], bool]
GitFn = Callable[[Path, List[str]], Tuple[int, str, str]]
GitCancellableFn = Callable[..., Tuple[int, str, str]]
RefreshFn = Callable[[Repo], None]
SubmodulePathsFn = Callable[[Path], List[str]]


def _repo_path_key(repo: Repo) -> str:
    try:
        return str(repo.path.resolve())
    except OSError:
        return str(repo.path)


def propagate_submodule_bump(
        state: State,
        parent: Repo,
        parent_label: str,
        *,
        push_timeout_seconds: float,
        task_bridge: Optional[JobTaskBridge] = None,
        cancel_event: Optional[threading.Event] = None,
        git_fn: GitFn = git,
        git_cancellable_fn: GitCancellableFn = git_cancellable,
        refresh_fn: RefreshFn = refresh_repo,
        submodule_paths_fn: SubmodulePathsFn = submodule_pointer_change_paths,
) -> str:
    """Commit and push a parent when its only dirt is submodule pointers."""
    tasks = task_bridge or JobTaskBridge(state.tasks)
    if cancel_event is not None and cancel_event.is_set():
        t = tasks.add(f"  ↳ propagate {parent_label}")
        tasks.update(t, "warn", "cancelled")
        return ""
    try:
        claim = WorkerClaim(state, repo=parent, acquire_repo=True, repo_timeout=5.0)
        claim.__enter__()
    except RuntimeError:
        t = tasks.add(f"  ↳ propagate {parent_label}")
        tasks.update(t, "warn", "skipped: parent refresh lock held by another op")
        return ""
    try:
        return _propagate_submodule_bump_inner(
            state, parent, parent_label,
            push_timeout_seconds=push_timeout_seconds,
            task_bridge=tasks,
            cancel_event=cancel_event,
            git_fn=git_fn,
            git_cancellable_fn=git_cancellable_fn,
            refresh_fn=refresh_fn,
            submodule_paths_fn=submodule_paths_fn,
        )
    finally:
        claim.__exit__(None, None, None)


def _propagate_submodule_bump_inner(
        state: State,
        parent: Repo,
        parent_label: str,
        *,
        push_timeout_seconds: float,
        task_bridge: JobTaskBridge,
        cancel_event: Optional[threading.Event],
        git_fn: GitFn,
        git_cancellable_fn: GitCancellableFn,
        refresh_fn: RefreshFn,
        submodule_paths_fn: SubmodulePathsFn,
) -> str:
    tasks = task_bridge
    refresh_fn(parent)
    if cancel_event is not None and cancel_event.is_set():
        t = tasks.add(f"  ↳ propagate {parent_label}")
        tasks.update(t, "warn", "cancelled")
        return ""
    if parent.error:
        return ""
    sub_paths = submodule_paths_fn(parent.path)
    if not sub_paths:
        t = tasks.add(f"  ↳ propagate {parent_label}")
        tasks.update(t, "warn", "skipped: parent has other dirty changes")
        return ""

    rc, out, _ = git_fn(parent.path, ["branch", "--show-current"])
    if rc != 0 or not out.strip():
        t = tasks.add(f"  ↳ propagate {parent_label}")
        tasks.update(t, "warn", "detached HEAD — no branch to commit on")
        return ""
    branch = out.strip()
    if not is_safe_ref_arg(branch):
        t = tasks.add(f"  ↳ propagate {parent_label}")
        tasks.update(t, "fail", "unsafe branch name")
        return ""

    if len(sub_paths) == 1:
        msg = f"bump submodule {sub_paths[0]}"
    elif sub_paths:
        joined = ", ".join(sub_paths)
        msg = f"bump submodules {joined}"
    else:
        msg = "bump submodule pointer(s)"

    t = tasks.add(f"  ↳ propagate {parent_label}: stage")
    rc, _, err = git_fn(parent.path, ["add", "--", *sub_paths])
    if rc != 0:
        tasks.update(t, "fail", first_line(err) or "git add failed")
        return ""
    tasks.update(t, "ok")

    if cancel_event is not None and cancel_event.is_set():
        return ""

    t = tasks.add(f"  ↳ propagate {parent_label}: commit")
    rc, _, err = git_fn(parent.path, ["commit", "-m", msg])
    if rc != 0:
        tasks.update(t, "fail", first_line(err))
        return ""
    tasks.update(t, "ok", msg)

    if cancel_event is not None and cancel_event.is_set():
        return ""

    t = tasks.add(f"  ↳ propagate {parent_label}: push")
    active_cancel_event = cancel_event or threading.Event()
    with WorkerClaim(
            state,
            repo=parent,
            task=t,
            mark_repo=False,
            claim_mutation=False):
        try:
            rc, _, err = git_cancellable_fn(
                parent.path,
                ["push"],
                cancel_event=active_cancel_event,
                timeout=push_timeout_seconds,
            )
            if rc != 0:
                if rc == 130:
                    tasks.update(t, "warn", "cancelled")
                    return ""
                if rc == 124:
                    tasks.update(t, "fail", first_line(err))
                    return ""
                rc, _, err = git_cancellable_fn(
                    parent.path,
                    ["push", "--set-upstream", "origin", branch],
                    cancel_event=active_cancel_event,
                    timeout=push_timeout_seconds,
                )
        except Exception as exc:  # noqa: BLE001
            tasks.update(t, "fail", first_line(str(exc)))
            return ""
        if rc != 0:
            if rc == 130:
                tasks.update(t, "warn", "cancelled")
            else:
                tasks.update(t, "fail", first_line(err))
            return ""
        tasks.update(t, "ok")

    rc, out, _ = git_fn(parent.path, ["rev-parse", "HEAD"])
    if rc != 0 or not out.strip():
        return ""
    return out.strip()


def ff_submodule_checkout_to(
        path: Path,
        branch: str,
        target_sha: str,
        *,
        git_fn: GitFn = git,
        submodule_paths_fn: SubmodulePathsFn = submodule_pointer_change_paths,
) -> bool:
    """Fast-forward a clean submodule checkout to a parent repo commit."""
    if not is_safe_ref_arg(branch):
        return False
    rc, _, _ = git_fn(path, ["fetch", "origin", branch])
    if rc != 0:
        return False
    rc, out, _ = git_fn(path, ["branch", "--show-current"])
    if rc != 0 or out.strip() != branch:
        return False
    rc, out, _ = git_fn(path, ["status", "--porcelain=v1"])
    if rc != 0:
        return False
    if out.strip() and not _pointer_only_dirt_matches_target(
            path,
            target_sha,
            git_fn=git_fn,
            submodule_paths_fn=submodule_paths_fn,
    ):
        return False
    rc, out, _ = git_fn(path, ["rev-parse", "HEAD"])
    if rc == 0 and out.strip() == target_sha:
        return True
    rc, _, _ = git_fn(path, ["merge-base", "--is-ancestor", "HEAD", target_sha])
    if rc != 0:
        return False
    rc, _, _ = git_fn(path, ["merge", "--ff-only", target_sha])
    return rc == 0


def _pointer_only_dirt_matches_target(
        path: Path,
        target_sha: str,
        *,
        git_fn: GitFn,
        submodule_paths_fn: SubmodulePathsFn,
) -> bool:
    sub_paths = submodule_paths_fn(path)
    if not sub_paths:
        return False
    for sub_path in sub_paths:
        rc, expected, _ = git_fn(path, ["rev-parse", f"{target_sha}:{sub_path}"])
        if rc != 0 or not expected.strip():
            return False
        rc, actual, _ = git_fn(path / sub_path, ["rev-parse", "HEAD"])
        if rc != 0 or actual.strip() != expected.strip():
            return False
    return True


def cascade_propagate_to_parents(
        state: State,
        canonicals_synced: List[Repo],
        *,
        find_child_at: FindChildAt,
        propagate_parent: PropagateParent,
        ff_submodule: FastForwardSubmodule,
        task_bridge: Optional[JobTaskBridge] = None,
        cancel_event: Optional[threading.Event] = None,
        git_fn: GitFn = git,
) -> None:
    """Cascade safe parent gitlink propagation upward through grandparents."""
    tasks = task_bridge or JobTaskBridge(state.tasks)
    visited: set[str] = set()
    queued: set[str] = set()
    pending: List[Repo] = []
    for canonical in canonicals_synced:
        for parent, _sub_path in canonical.siblings:
            if parent.synthetic:
                continue
            parent_key = _repo_path_key(parent)
            if parent_key not in queued:
                pending.append(parent)
                queued.add(parent_key)

    while pending:
        if cancel_event is not None and cancel_event.is_set():
            return
        parent = pending.pop(0)
        parent_key = _repo_path_key(parent)
        if parent_key in visited:
            continue
        visited.add(parent_key)

        parent_label = state.task_repo_label(parent)
        new_head = _call_propagate_parent(
            propagate_parent,
            state,
            parent,
            parent_label,
            tasks,
            cancel_event,
        )
        if not new_head:
            continue

        rc, branch_out, _ = git_fn(parent.path, ["branch", "--show-current"])
        if rc != 0 or not branch_out.strip():
            continue
        branch = branch_out.strip()
        for grandparent, sub_path in parent.siblings:
            if grandparent.synthetic:
                continue
            if cancel_event is not None and cancel_event.is_set():
                return
            grandparent_label = state.task_repo_label(grandparent)
            t = tasks.add(f"  ↳ propagate {parent_label}: align in {grandparent_label}")
            claim = WorkerClaim(
                state,
                child=find_child_at(grandparent, sub_path),
                acquire_child=True,
                child_timeout=5.0,
            )
            try:
                claim.__enter__()
            except RuntimeError:
                tasks.update(t, "warn", "skipped: child refresh lock held by another op")
                continue
            try:
                ff_ok = ff_submodule(sub_path, branch, new_head)
            except Exception as exc:  # noqa: BLE001
                tasks.update(t, "fail", first_line(str(exc)))
                continue
            finally:
                claim.__exit__(None, None, None)
            if ff_ok:
                tasks.update(t, "ok")
                grandparent_key = _repo_path_key(grandparent)
                if grandparent_key not in visited and grandparent_key not in queued:
                    pending.append(grandparent)
                    queued.add(grandparent_key)
            else:
                tasks.update(t, "warn", "skipped — non-FF or dirty checkout")


def _call_propagate_parent(
        propagate_parent: PropagateParent,
        state: State,
        parent: Repo,
        parent_label: str,
        task_bridge: JobTaskBridge,
        cancel_event: Optional[threading.Event],
) -> str:
    try:
        return propagate_parent(
            state,
            parent,
            parent_label,
            task_bridge=task_bridge,
            cancel_event=cancel_event,
        )
    except TypeError as exc:
        if "task_bridge" not in str(exc) and "cancel_event" not in str(exc):
            raise
        return propagate_parent(state, parent, parent_label)
