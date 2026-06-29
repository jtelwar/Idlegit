"""Small job registry and runner primitives for background work."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .tasks import Task, Tasks
from .threads import create_job_thread


def _monotonic() -> float:
    """Indirected so tests can stub time."""
    return time.monotonic()


class JobStatus(str, Enum):
    """Lifecycle status for a background job."""

    RUNNING = "running"
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class JobSpec:
    """Static information needed to register a background job."""

    kind: str
    label: str
    local_mutation: bool = False
    repo_keys: Tuple[str, ...] = ()
    child_keys: Tuple[str, ...] = ()
    stale_after_seconds: float = 0.0


@dataclass
class Job:
    """Runtime state for one registered job."""

    job_id: int
    spec: JobSpec
    status: JobStatus = JobStatus.RUNNING
    started_at: float = field(default_factory=_monotonic)
    finished_at: Optional[float] = None
    message: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    task_keys: Tuple[int, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.status in (
            JobStatus.OK, JobStatus.WARN, JobStatus.FAIL,
            JobStatus.CANCELLED)

    def age_seconds(self, now: Optional[float] = None) -> float:
        """Return monotonic age for active diagnostics."""
        current = _monotonic() if now is None else now
        return max(0.0, current - self.started_at)

    def is_stale(
            self,
            *,
            now: Optional[float] = None,
            default_stale_after_seconds: float = 0.0,
    ) -> bool:
        """Return whether this active job has exceeded its stale threshold."""
        if self.terminal:
            return False
        threshold = (
            self.spec.stale_after_seconds
            if self.spec.stale_after_seconds > 0
            else default_stale_after_seconds
        )
        if threshold <= 0:
            return False
        return self.age_seconds(now) >= threshold


@dataclass(frozen=True)
class JobTaskOutcome:
    """Terminal worker outcome observed while publishing task rows."""

    status: Optional[JobStatus] = None
    message: str = ""


class JobTaskBridge:
    """Publish task rows while reporting worker outcome to the job registry.

    Task rows are presentation. The bridge observes status writes at the
    worker boundary so a job can finish as warn/fail without scanning rendered
    task rows back into control state.
    """

    def __init__(
            self,
            tasks: Tasks,
            registry: Optional["JobRegistry"] = None,
            job: Optional[Job] = None,
    ) -> None:
        if (registry is None) != (job is None):
            raise ValueError("registry and job must be supplied together")
        self._tasks = tasks
        self._registry = registry
        self._job = job
        self._outcome = JobTaskOutcome()
        self._lock = threading.Lock()

    def add(self, label: str, parent: Optional[Task] = None) -> Task:
        task = self._tasks.add(label, parent=parent)
        self.attach(task)
        return task

    def attach(self, task: Task) -> bool:
        """Link an existing presentation row to this bridge's job."""
        if self._registry is None or self._job is None:
            return False
        return self._registry.link_task(
            self._job, task, allow_terminal=True)

    def update(self, task: Task, status: str, message: str = "") -> None:
        self._tasks.update(task, status, message)
        self._record_status(status, message)

    def set_label(self, task: Task, label: str) -> None:
        self._tasks.set_label(task, label)

    def clear_message(self, task: Task) -> None:
        self._tasks.clear_message(task)

    def outcome(self) -> JobTaskOutcome:
        with self._lock:
            return self._outcome

    def finish_failed_or_warned_job(
            self,
            registry: "JobRegistry",
            job: Job,
    ) -> bool:
        outcome = self.outcome()
        if outcome.status is None:
            return False
        return registry.finish(job, outcome.status, outcome.message)

    def _record_status(self, status: str, message: str) -> None:
        if status == "fail":
            self._set_outcome(JobStatus.FAIL, message)
        elif status == "warn":
            self._set_outcome(JobStatus.WARN, message)

    def _set_outcome(self, status: JobStatus, message: str) -> None:
        with self._lock:
            current = self._outcome.status
            if current == JobStatus.FAIL:
                return
            if current == status and self._outcome.message:
                return
            if current == JobStatus.WARN and status != JobStatus.FAIL:
                return
            self._outcome = JobTaskOutcome(status=status, message=message)


class JobRegistry:
    """Thread-safe owner of background job lifecycle state."""

    def __init__(self, on_change: Optional[Callable[[], None]] = None) -> None:
        self._lock = threading.Lock()
        self._next_job_id = 1
        self._jobs: List[Job] = []
        self.on_change = on_change

    def start(self, spec: JobSpec) -> Job:
        with self._lock:
            job = Job(job_id=self._next_job_id, spec=spec)
            self._next_job_id += 1
            self._jobs.append(job)
        self._notify_change()
        return job

    def snapshot(self) -> List[Job]:
        with self._lock:
            return list(self._jobs)

    def get(self, job_id: int) -> Optional[Job]:
        with self._lock:
            for job in self._jobs:
                if job.job_id == job_id:
                    return job
        return None

    def link_task(
            self,
            job: Job,
            task: object,
            *,
            allow_terminal: bool = False,
    ) -> bool:
        task_key = id(task)
        with self._lock:
            if job.terminal and not allow_terminal:
                return False
            if task_key in job.task_keys:
                return True
            job.task_keys = (*job.task_keys, task_key)
        self._notify_change()
        return True

    def link_task_by_id(self, job_id: int, task: object) -> bool:
        with self._lock:
            for job in self._jobs:
                if job.job_id == job_id:
                    break
            else:
                return False
        return self.link_task(job, task)

    def job_for_task(self, task: object) -> Optional[Job]:
        task_key = id(task)
        with self._lock:
            matched: List[Job] = []
            for job in self._jobs:
                if task_key in job.task_keys:
                    matched.append(job)
            for job in reversed(matched):
                if not job.terminal:
                    return job
            if matched:
                return matched[-1]
            return None

    def finish(self, job: Job, status: JobStatus,
               message: str = "") -> bool:
        if status == JobStatus.RUNNING:
            raise ValueError("cannot finish job as running")
        with self._lock:
            if job.terminal:
                return False
            job.status = status
            job.message = message
            job.finished_at = _monotonic()
        self._notify_change()
        return True

    def request_cancel(self, job: Job) -> bool:
        with self._lock:
            if job.terminal:
                return False
            job.cancel_event.set()
        self._notify_change()
        return True

    def request_cancel_by_id(self, job_id: int) -> bool:
        with self._lock:
            for job in self._jobs:
                if job.job_id != job_id:
                    continue
                if job.terminal:
                    return False
                job.cancel_event.set()
                break
            else:
                return False
        self._notify_change()
        return True

    def has_active_local_mutation(self) -> bool:
        with self._lock:
            return any(
                j.spec.local_mutation and not j.terminal
                for j in self._jobs)

    def has_active_local_mutation_for(
            self,
            *,
            repo_keys: Tuple[str, ...] = (),
            child_keys: Tuple[str, ...] = (),
    ) -> bool:
        return bool(self.active_local_mutation_jobs_for(
            repo_keys=repo_keys, child_keys=child_keys))

    def active_local_mutation_jobs(self) -> List[Job]:
        with self._lock:
            return [
                j for j in self._jobs
                if j.spec.local_mutation and not j.terminal
            ]

    def active_local_mutation_jobs_for(
            self,
            *,
            repo_keys: Tuple[str, ...] = (),
            child_keys: Tuple[str, ...] = (),
    ) -> List[Job]:
        with self._lock:
            return [
                j for j in self._jobs
                if j.spec.local_mutation
                and not j.terminal
                and _job_targets_overlap(j.spec, repo_keys, child_keys)
            ]

    def stale_jobs(
            self,
            *,
            now: Optional[float] = None,
            default_stale_after_seconds: float = 0.0,
    ) -> List[Job]:
        """Return active jobs that have exceeded their stale threshold."""
        with self._lock:
            return [
                job for job in self._jobs
                if job.is_stale(
                    now=now,
                    default_stale_after_seconds=default_stale_after_seconds,
                )
            ]

    def _notify_change(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                pass


def _job_targets_overlap(
        spec: JobSpec,
        repo_keys: Tuple[str, ...],
        child_keys: Tuple[str, ...],
) -> bool:
    if not spec.repo_keys and not spec.child_keys:
        return True
    if not repo_keys and not child_keys:
        return True
    job_repos = set(spec.repo_keys)
    job_children = set(spec.child_keys)
    query_repos = set(repo_keys)
    query_children = set(child_keys)
    if job_repos & query_repos:
        return True
    if job_children & query_children:
        return True
    for query_repo in query_repos:
        if any(_path_is_same_or_inside(job_child, query_repo)
               for job_child in job_children):
            return True
    for query_child in query_children:
        if any(_path_is_same_or_inside(query_child, job_repo)
               for job_repo in job_repos):
            return True
    return False


def _path_is_same_or_inside(path: str, parent: str) -> bool:
    try:
        path_obj = Path(path).resolve()
        parent_obj = Path(parent).resolve()
    except OSError:
        path_obj = Path(path)
        parent_obj = Path(parent)
    return path_obj == parent_obj or parent_obj in path_obj.parents


ThreadFactory = Callable[[Callable[[], None], str], threading.Thread]


def submit_job(
        registry: JobRegistry,
        spec: JobSpec,
        target: Callable[[Job], None],
        *,
        thread_factory: Optional[ThreadFactory] = None,
) -> Tuple[Job, Optional[threading.Thread]]:
    """Register and run a job in a daemon thread.

    The wrapper guarantees that an uncaught worker exception becomes a terminal
    failed job, and that thread-start failure also terminalizes the job before
    returning.
    """
    job = registry.start(spec)
    thread = start_job_thread(
        registry, job, target, thread_factory=thread_factory)
    return job, thread


def start_job_thread(
        registry: JobRegistry,
        job: Job,
        target: Callable[[Job], None],
        *,
        thread_factory: Optional[ThreadFactory] = None,
) -> Optional[threading.Thread]:
    """Run an already-registered job in a daemon thread."""
    factory = thread_factory or _default_thread_factory

    def wrapped() -> None:
        try:
            target(job)
        except Exception as e:  # noqa: BLE001
            registry.finish(job, JobStatus.FAIL, str(e))
            return
        if job.cancel_event.is_set():
            registry.finish(job, JobStatus.CANCELLED)
            return
        registry.finish(job, JobStatus.OK)

    try:
        thread = factory(wrapped, f"idlegit-job-{job.job_id}")
        thread.daemon = True
        thread.start()
    except Exception as e:  # noqa: BLE001
        registry.finish(job, JobStatus.FAIL, str(e))
        return None
    return thread


def _default_thread_factory(
        target: Callable[[], None], name: str) -> threading.Thread:
    return create_job_thread(target, name)
