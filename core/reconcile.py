"""Shared bounded refresh/reconciliation helpers."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional

from .git_ops import MAX_PARALLEL_GIT_JOBS, first_line, link_siblings, refresh_repo
from .state.repos import Repo
from .state.workspaces import SubtreeSpec


@dataclass(frozen=True)
class RefreshFailure:
    """One repo refresh failure captured by a bounded refresh batch."""

    repo: Repo
    message: str


@dataclass
class RefreshBatchResult:
    """Structured outcome for a bounded repo refresh batch."""

    refreshed: int = 0
    failures: List[RefreshFailure] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return len(self.failures)


@dataclass
class ReconcileResult:
    """Structured outcome for a bounded refresh plus sibling relink."""

    refresh: RefreshBatchResult = field(default_factory=RefreshBatchResult)
    link_error: str = ""
    link_skipped: bool = False

    @property
    def failed(self) -> int:
        return self.refresh.failed + (1 if self.link_error else 0)


def refresh_repos_bounded(
        repos: Iterable[Repo],
        *,
        refresh_fn: Callable[[Repo], None] = refresh_repo,
        max_workers: Optional[int] = None,
        on_done: Optional[Callable[[Repo], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
) -> RefreshBatchResult:
    """Refresh repos concurrently and convert per-repo exceptions to data."""
    repo_list = list(repos)
    result = RefreshBatchResult()
    if not repo_list:
        return result
    workers = max_workers or MAX_PARALLEL_GIT_JOBS
    workers = max(1, min(len(repo_list), workers))
    lock = threading.Lock()

    def run(repo: Repo) -> None:
        if should_stop is not None and should_stop():
            return
        try:
            refresh_fn(repo)
            with lock:
                result.refreshed += 1
        except Exception as e:  # noqa: BLE001
            with lock:
                result.failures.append(
                    RefreshFailure(repo=repo, message=first_line(str(e))))
        finally:
            if on_done is not None:
                on_done(repo)

    if workers == 1:
        for repo in repo_list:
            run(repo)
        return result
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(run, repo_list))
    return result


def reconcile_repos_bounded(
        repos: Iterable[Repo],
        subtrees: Optional[Iterable[SubtreeSpec]] = None,
        *,
        link_repos: Optional[Iterable[Repo]] = None,
        refresh_fn: Callable[[Repo], None] = refresh_repo,
        link_fn: Callable[[List[Repo], Optional[List[SubtreeSpec]]], None] = link_siblings,
        max_workers: Optional[int] = None,
        on_done: Optional[Callable[[Repo], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        should_link: Optional[Callable[[], bool]] = None,
) -> ReconcileResult:
    """Refresh repos through the bounded helper, then relink sibling rows."""
    repo_list = list(repos)
    link_repo_list = list(link_repos) if link_repos is not None else repo_list
    subtree_list = list(subtrees) if subtrees is not None else None
    result = ReconcileResult(
        refresh=refresh_repos_bounded(
            repo_list,
            refresh_fn=refresh_fn,
            max_workers=max_workers,
            on_done=on_done,
            should_stop=should_stop,
        )
    )
    if should_stop is not None and should_stop():
        return result
    if should_link is not None and not should_link():
        result.link_skipped = True
        return result
    try:
        link_fn(link_repo_list, subtree_list)
    except Exception as e:  # noqa: BLE001
        result.link_error = first_line(str(e))
    return result
