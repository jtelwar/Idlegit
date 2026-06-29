"""Background worker functions: every git action that takes more than a
moment runs in a daemon thread out of here, publishing progress to the
sidebar via state.tasks. None of these touch curses."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .runtime.jobs import (
    Job,
    JobRegistry,
    JobSpec,
    JobStatus,
    JobTaskBridge,
    JobTaskOutcome,
    start_job_thread,
    submit_job,
)
from .runtime.claims import RefreshClaim, WorkerClaim
from .reconcile import ReconcileResult, reconcile_repos_bounded
from .refresh_queue import InlineRefreshQueue
from .refresh_scope import WorkspaceRefreshScope
from .smart_sync.executor import CanonicalExecutionDeps, execute_canonical_plan
from .smart_sync.lifecycle import SmartSyncLifecycle
from .smart_sync.planner import plan_canonical_alignment
from .smart_sync.propagation import (
    cascade_propagate_to_parents,
    ff_submodule_checkout_to,
    propagate_submodule_bump,
)
from .smart_sync.runner import (
    SmartSyncRunConfig,
    SmartSyncWorkPlan,
    build_smart_sync_work_plan,
    run_smart_sync_job,
)
from .smart_sync.types import (
    CanonicalPlan,
    CanonicalPlanStatus,
    CheckoutSnapshot,
    SmartSyncSettings,
    SyncStep,
    SyncStepKind,
)
from .state.selectors import (
    active_workspace_child_rows,
    active_workspace_repo_rows,
    child_row_state,
    local_mutation_active_for,
    read_only_child_busy_predicate,
    read_only_row_busy_active,
    repo_row_state,
)
from .runtime.threads import (
    ThreadGroup,
    create_job_thread,
    create_worker_thread,
)
from core.state.app import State
from core.state.app_menu import AppMenu
from core.state.ssh_keygen import SshKeygenModal
from core.state.store import (
    ChildStatusSnapshot,
    ChildTopologySnapshot,
    WorkflowIntentSnapshot,
)
from .state.action_menu import FileEntry
from .state.prompts import AlignHeadsPrompt, DetachedRecoveryPrompt
from .state.pickers import BranchPicker, RemoteBranchPicker
from .state.smart_sync import SmartSyncCheckout
from .state.review import LFSCandidate, ReviewBlock
from .state.repos import ChildRef, Repo
from .state.safe_merge import SafeMergeScreen
from .runtime.tasks import Task, Tasks
from .state.views import DiffViewer, TaskLogViewer
from .state.workspaces import SubtreeSpec, Workspace, WorkspaceDraft
from .git_ops import (
    apply_lfs_tracking,
    begin_safe_merge,
    complete_safe_merge_commit,
    create_named_stash,
    describe_merge_side,
    discover_repos,
    dispatch_workflow,
    drop_named_stash,
    first_line,
    fetch_run_log,
    get_run_view,
    gh_available,
    git,
    git_bounded_output,
    git_cancellable,
    head_short_info,
    link_siblings,
    list_branches,
    list_remote_tracking_refs,
    list_recent_runs,
    merge_head_sha,
    merge_remote_workflow_states,
    parse_github_slug,
    parse_safe_merge_conflicts,
    query_file_blame,
    query_file_log,
    query_working_tree,
    apply_link_siblings_snapshot,
    apply_repo_refresh_snapshot,
    read_link_siblings_snapshot,
    read_repo_refresh_snapshot,
    refresh_repo,
    remaining_conflict_paths,
    safe_stage_all,
    signature_mtime,
    submodule_pointer_change_paths,
    suggest_commit_message,
    suggest_commit_message_at,
    suggest_commit_message_for_paths,
    sync_sibling,
    sync_subtree,
    is_safe_ref_arg,
    working_tree_signature,
    write_conflict_resolution,
    MAX_PARALLEL_GIT_JOBS,
)

PROMPT_WAIT_SECONDS = 15 * 60
MIN_ACTION_REFRESH_SECONDS = 0.35
USER_PUSH_TIMEOUT_SECONDS = 60 * 60
PROPAGATE_PUSH_TIMEOUT_SECONDS = 30.0
POST_PUSH_RUN_DISCOVERY_TIMEOUT_SECONDS = 120.0
_detached_recovery_prompt_lock = threading.Lock()
_align_heads_prompt_lock = threading.Lock()


def kick_off_workspace_path_check(
        state: State,
        draft: WorkspaceDraft,
        *,
        kind: str = "workspace-path-check",
) -> None:
    """Check one workspace path draft in a read-only worker job."""
    text = draft.path_text.strip()
    if not text:
        draft.last_checked = draft.path_text
        draft.repo_count = -1
        draft.error = ""
        draft.checking = False
        return
    target = draft.path_text
    draft.checking = True

    def worker(_job: Job) -> None:
        repo_count = -1
        error = ""
        try:
            path = Path(text).expanduser()
            if not path.is_absolute():
                path = path.resolve()
            if not path.exists():
                error = "(no such folder)"
            elif not path.is_dir():
                error = "(not a folder)"
            else:
                try:
                    repos = discover_repos(path)
                except OSError as exc:
                    error = f"(error: {exc.strerror or exc})"
                else:
                    repo_count = len(repos)
        except (OSError, RuntimeError) as exc:
            error = f"(error: {exc})"
        if draft.path_text == target:
            draft.repo_count = repo_count
            draft.error = error
            draft.last_checked = target
            draft.checking = False

    def thread_factory(target_fn, thread_name):
        return create_job_thread(target_fn, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind=kind,
            label=f"check {target}",
            local_mutation=False,
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None and draft.path_text == target:
        draft.repo_count = -1
        draft.error = f"(error: {job.message})"
        draft.last_checked = target
        draft.checking = False


def kick_off_workspace_settings_save(
        state: State,
        *,
        label: str = "save workspace settings",
        success_message: str = "saved",
        on_failure: Optional[Callable[[str], None]] = None,
) -> None:
    """Persist workspace settings from a worker-owned read-only job."""
    workspaces_snapshot = _snapshot_workspace_settings(state.workspaces)
    active_index_snapshot = state.active_workspace_index

    def worker(
            _job: Job,
            task: Task,
            bridge: JobTaskBridge,
    ) -> None:
        from .config import save_workspaces

        try:
            save_workspaces(workspaces_snapshot, active_index_snapshot)
        except OSError as exc:
            message = f"could not write: {exc}"
            if on_failure is not None:
                on_failure(message)
            bridge.update(task, "fail", message)
            return
        bridge.update(task, "ok", success_message)

    _kick_off_read_only_task(
        state,
        kind="workspace-settings-save",
        label=label,
        worker=worker,
        on_start_failure=on_failure,
    )


def _snapshot_workspace_settings(workspaces: List[Workspace]) -> List[Workspace]:
    """Copy persisted workspace metadata for a background settings save."""
    snapshot: List[Workspace] = []
    for ws in workspaces:
        snapshot.append(Workspace(
            name=ws.name,
            folders=list(ws.folders),
            overrides=dict(ws.overrides),
            subtrees=[
                SubtreeSpec(
                    name=sub.name,
                    parent=sub.parent,
                    source=sub.source,
                    prefix=sub.prefix,
                )
                for sub in ws.subtrees
            ],
            fs_watch_ignore=list(ws.fs_watch_ignore),
            ephemeral=ws.ephemeral,
        ))
    return snapshot


def kick_off_startup_refresh(
        active_repos: List[Repo],
        subtrees: object,
        mark_done: Callable[[Repo], None],
) -> bool:
    """Refresh and relink the startup workspace from a worker-owned job.

    Returns True when a worker was started. On thread-start failure the repos
    are marked done and the returned value is False, allowing the loading screen
    to stop polling without owning job/thread primitives.
    """
    active_snapshot = list(active_repos)
    subtree_snapshot = list(subtrees) if subtrees is not None else []
    if not active_snapshot:
        return True

    def worker(job: Job) -> None:
        try:
            reconcile_repos_bounded(
                active_snapshot,
                subtree_snapshot,
                link_repos=active_snapshot,
                refresh_fn=refresh_repo,
                link_fn=link_siblings,
                should_stop=job.cancel_event.is_set,
            )
        finally:
            for repo in active_snapshot:
                mark_done(repo)

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    registry = JobRegistry()
    job, thread = submit_job(
        registry,
        JobSpec(
            kind="startup-refresh",
            label="startup refresh",
            local_mutation=False,
            repo_keys=tuple(sorted(str(repo.path) for repo in active_snapshot)),
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        for repo in active_snapshot:
            mark_done(repo)
        return False
    return True


def _review_files_load_id(block: ReviewBlock) -> str:
    return f"review-files:{block.draft_id}"


def ensure_review_files_load_id(state: State, block: ReviewBlock) -> str:
    draft = state.review_drafts.get_or_create(block.draft_id)
    if draft.files_load_id:
        return draft.files_load_id
    load_id = _review_files_load_id(block)
    state.review_drafts.set_file_load_id(block.draft_id, load_id)
    return load_id


def cancel_review_file_loads(state: State, blocks: List[ReviewBlock]) -> None:
    state.view_loads.remove_many([
        ensure_review_files_load_id(state, block)
        for block in blocks
    ])


def kick_off_review_files_load(state: State, blocks: List[ReviewBlock]) -> None:
    """Populate review draft files through worker-owned read-only jobs."""

    def loader(_job: Job, block: ReviewBlock, load_id: str) -> None:
        try:
            if state.view_loads.is_cancelled(load_id):
                return
            files: List[FileEntry] = query_working_tree(block.target_path)
            if state.view_loads.is_cancelled(load_id):
                return
            if block.auto_stage:
                staged_paths = {fe.path: True for fe in files}
            else:
                staged_paths = {
                    fe.path: (fe.x != " " and not fe.untracked)
                    for fe in files
                }
            state.review_drafts.set_files(
                block.draft_id, files, staged_paths, loading=False)
        finally:
            state.review_drafts.set_loading(block.draft_id, False)
            state.view_loads.finish(load_id, [])

    for block in blocks:
        load_id = ensure_review_files_load_id(state, block)
        state.view_loads.create(load_id)

        def worker(job: Job, current_block=block,
                   current_load_id=load_id) -> None:
            loader(job, current_block, current_load_id)

        def thread_factory(target, thread_name):
            return create_job_thread(target, thread_name)

        job, thread = submit_job(
            state.job_registry,
            JobSpec(
                kind="review-files-load",
                label=f"{block.label}: load review files",
                local_mutation=False,
            ),
            worker,
            thread_factory=thread_factory,
        )
        if thread is None:
            state.review_drafts.set_loading(block.draft_id, False)
            state.view_loads.fail(load_id, job.message)


def kick_off_task_log_load(
        state: State,
        viewer: TaskLogViewer,
        label: str,
) -> None:
    """Fetch a workflow run log for a task-log viewer in a read-only job."""

    def load_log() -> Tuple[List[str], str]:
        if state.view_loads.is_cancelled(viewer.load_id):
            return [], ""
        ok, lines, err = fetch_run_log(
            viewer.slug,
            viewer.run_id,
            job_id=viewer.job_id,
            only_failed=viewer.only_failed,
        )
        if state.view_loads.is_cancelled(viewer.load_id):
            return [], ""
        if ok:
            return lines if lines else ["(no log output yet)"], ""
        return [], err or "fetch failed"

    def worker(_job: Job) -> None:
        try:
            lines, error = load_log()
            if state.view_loads.is_cancelled(viewer.load_id):
                return
            state.view_loads.finish(viewer.load_id, lines, error=error)
        except Exception as exc:
            state.view_loads.finish(
                viewer.load_id, [], error=first_line(str(exc)))
            raise

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    _job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="task-log-load",
            label=label,
            local_mutation=False,
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        state.view_loads.finish(
            viewer.load_id, [], error="thread start failed")


def _initial_branch_selection(
        mode: str,
        branches: List[str],
        current: str,
) -> int:
    initial = 0
    if mode == "set_upstream":
        guess = f"origin/{current}" if current else ""
        for i, branch in enumerate(branches):
            if branch == guess:
                return i
        return initial
    if mode in ("merge", "safe_merge"):
        for i, branch in enumerate(branches):
            if branch != current:
                return i
        return initial
    for i, branch in enumerate(branches):
        if branch == current:
            return i
    return initial


def kick_off_branch_picker_load(state: State, picker: BranchPicker) -> None:
    """Populate local or upstream branch picker rows from a read-only job."""
    path = picker.target_path
    mode = picker.mode
    state.view_loads.create(picker.load_id)

    def worker(_job: Job) -> None:
        if state.view_loads.is_cancelled(picker.load_id):
            return
        if mode == "set_upstream":
            branches = list_remote_tracking_refs(path)
            rc, current_out, _ = git(path, ["branch", "--show-current"])
            current = current_out.strip() if rc == 0 else ""
        else:
            branches, current = list_branches(path)
        if state.view_loads.is_cancelled(picker.load_id):
            return
        picker.selected = _initial_branch_selection(mode, branches, current)
        state.view_loads.finish(
            picker.load_id, branches, details={"current": current})

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="branch-picker-load",
            label=f"{picker.target_label}: branches",
            local_mutation=False,
            repo_keys=(str(picker.target_path),),
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        state.view_loads.fail(picker.load_id, job.message)


def kick_off_remote_branch_picker_load(
        state: State,
        picker: RemoteBranchPicker,
) -> None:
    """Populate remote branch picker rows from a read-only job."""
    path = picker.target_path
    state.view_loads.create(picker.load_id)

    def worker(_job: Job) -> None:
        if state.view_loads.is_cancelled(picker.load_id):
            return
        refs = list_remote_tracking_refs(path)
        if state.view_loads.is_cancelled(picker.load_id):
            return
        state.view_loads.finish(picker.load_id, refs)
        if refs:
            picker.selected = 0

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="remote-branch-load",
            label=f"{picker.target_label}: remote branches",
            local_mutation=False,
            repo_keys=(str(picker.target_path),),
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        state.view_loads.fail(picker.load_id, job.message)


_DIFF_VIEWER_MAX_BYTES = 4 * 1024 * 1024
_DIFF_VIEWER_MAX_LINES = 50_000


def _diff_viewer_tab_load_id(viewer: DiffViewer, tab: str) -> str:
    if tab == "log":
        return viewer.log_load_id
    if tab == "blame":
        return viewer.blame_load_id
    return viewer.diff_load_id


def kick_off_diff_viewer_loads(state: State, viewer: DiffViewer) -> None:
    """Start worker-owned read-only jobs for all diff-viewer tabs."""
    _kick_off_diff_viewer_tab_load(
        state, viewer, "diff-viewer-diff", "diff", _load_diff_viewer_diff)
    _kick_off_diff_viewer_tab_load(
        state, viewer, "diff-viewer-log", "log", _load_diff_viewer_log)
    _kick_off_diff_viewer_tab_load(
        state, viewer, "diff-viewer-blame", "blame", _load_diff_viewer_blame)


def _kick_off_diff_viewer_tab_load(
        state: State,
        viewer: DiffViewer,
        kind: str,
        label: str,
        loader: Callable[[State, DiffViewer, str], List[str]],
) -> None:
    load_id = _diff_viewer_tab_load_id(viewer, label)
    state.view_loads.create(load_id)

    def worker(_job: Job) -> None:
        try:
            if state.view_loads.is_cancelled(load_id):
                return
            lines = loader(state, viewer, load_id)
            if state.view_loads.is_cancelled(load_id):
                return
            state.view_loads.finish(load_id, lines)
        except Exception as exc:
            state.view_loads.fail(load_id, first_line(str(exc)))
            raise

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind=kind,
            label=f"{viewer.label}: {label}",
            local_mutation=False,
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is not None:
        return
    state.view_loads.fail(load_id, job.message)


def _load_diff_viewer_diff(
        state: State, viewer: DiffViewer, load_id: str) -> List[str]:
    if state.view_loads.is_cancelled(load_id):
        return []

    if viewer.commit_sha:
        sha = viewer.commit_sha
        if sha.startswith("-"):
            lines = ["(unsafe sha)"]
        else:
            rc, out, err, truncated = git_bounded_output(
                viewer.target_path,
                ["show", sha, "--", viewer.file_path],
                _DIFF_VIEWER_MAX_BYTES)
            if rc != 0 and not out:
                text = err.strip() or "(no diff available)"
                lines = [text]
            else:
                lines = out.splitlines() if out else ["(no diff)"]
            if truncated:
                lines.append(
                    f"... (truncated at {_DIFF_VIEWER_MAX_BYTES} bytes)")
    elif viewer.untracked:
        full = viewer.target_path / viewer.file_path
        truncated = False
        try:
            with full.open("rb") as file_handle:
                raw = file_handle.read(_DIFF_VIEWER_MAX_BYTES + 1)
            if len(raw) > _DIFF_VIEWER_MAX_BYTES:
                raw = raw[:_DIFF_VIEWER_MAX_BYTES]
                truncated = True
            text = raw.decode("utf-8", errors="replace")
        except OSError as exc:
            text = f"(could not read file: {exc})"
        lines = [
            f"diff --git a/{viewer.file_path} b/{viewer.file_path}",
            "new file (untracked)",
            "--- /dev/null",
            f"+++ b/{viewer.file_path}",
        ]
        for line in text.splitlines():
            lines.append("+" + line)
        if truncated:
            lines.append(
                f"... (truncated at {_DIFF_VIEWER_MAX_BYTES} bytes)")
    else:
        rc, out, err, truncated = git_bounded_output(
            viewer.target_path,
            ["diff", "HEAD", "--", viewer.file_path],
            _DIFF_VIEWER_MAX_BYTES)
        if rc != 0 and not out:
            text = err.strip() or "(no diff available)"
            lines = [text]
        else:
            lines = out.splitlines() if out else ["(no diff)"]
        if truncated:
            lines.append(
                f"... (truncated at {_DIFF_VIEWER_MAX_BYTES} bytes)")

    if len(lines) > _DIFF_VIEWER_MAX_LINES:
        lines = lines[:_DIFF_VIEWER_MAX_LINES]
        lines.append(f"... (truncated at {_DIFF_VIEWER_MAX_LINES} lines)")
    return lines


def _load_diff_viewer_log(
        state: State, viewer: DiffViewer, load_id: str) -> List[str]:
    if state.view_loads.is_cancelled(load_id):
        return []
    rows = query_file_log(
        viewer.target_path, viewer.file_path,
        sha=viewer.commit_sha)
    if state.view_loads.is_cancelled(load_id):
        return []
    return rows or ["(no log available)"]


def _load_diff_viewer_blame(
        state: State, viewer: DiffViewer, load_id: str) -> List[str]:
    if state.view_loads.is_cancelled(load_id):
        return []
    if viewer.untracked:
        return ["(untracked file - no blame history yet)"]
    rows = query_file_blame(
        viewer.target_path, viewer.file_path,
        sha=viewer.commit_sha)
    if state.view_loads.is_cancelled(load_id):
        return []
    return rows or ["(no blame output)"]


def _sync_sibling_safe(
    target_path: Path, branch: str, *, parent_path: Optional[Path] = None
) -> Tuple[bool, str]:
    """Run sync_sibling and convert unexpected exceptions to task text."""
    try:
        return sync_sibling(target_path, branch, parent_path=parent_path)
    except Exception as e:  # noqa: BLE001
        return False, first_line(str(e)) or "sync failed"


def _pull_prefer_ff_then_merge(
    path: Path,
    tasks: Tasks,
    name: str,
    *,
    allow_merge_fallback: bool,
    parent_task: "Optional[Task]" = None,
    cancel_event: "Optional[threading.Event]" = None,
) -> bool:
    """`git pull --ff-only`, then optionally `git pull --no-rebase --no-edit`
    when the caller allows merge commits. No rebase / no force. Returns
    False on failure (a task row records the error). On success: when HEAD
    moved, adds an ok task; when already up to date, no task (matches the
    commit pipeline's historical no-noise behaviour).

    Never passes `--recurse-submodules`: that flag re-checkouts each
    submodule to the parent's recorded gitlink, which silently
    orphans local-only submodule commits ahead of the gitlink. Use
    Ctrl+S (smart-sync) to align submodule checkouts safely via
    `sync_sibling`, which refuses to orphan commits.

    `parent_task` (default None) — when supplied, every child task row
    added by this call gets parented under it via `tasks.add(..., parent=...)`.
    Used by `kick_off_pull_all` to nest per-repo task rows beneath a
    single workspace-wide "pull all" parent so the user can see at a
    glance which gesture they're looking at — without changing the
    no-noise behaviour for "already up to date" repos (those still
    add no task at all)."""
    _, head_before, _ = git(path, ["rev-parse", "HEAD"])
    pull_args = ["pull", "--ff-only"]
    try:
        rc, _, err = git_cancellable(path, pull_args, cancel_event=cancel_event)
    except Exception as e:  # noqa: BLE001
        t = tasks.add(f"{name}: pull --ff-only", parent=parent_task)
        tasks.update(t, "fail", first_line(str(e)))
        return False
    _, head_after, _ = git(path, ["rev-parse", "HEAD"])
    if rc == 0:
        if head_before.strip() != head_after.strip():
            t = tasks.add(f"{name}: pull", parent=parent_task)
            tasks.update(t, "ok")
        return True
    if rc == 130:
        # User cancelled — surface a warn row and bail without
        # trying the merge fallback (that'd also block on network).
        t = tasks.add(f"{name}: pull", parent=parent_task)
        tasks.update(t, "warn", "cancelled")
        return False
    if not allow_merge_fallback:
        t = tasks.add(f"{name}: pull --ff-only", parent=parent_task)
        tasks.update(t, "fail", first_line(err) or "cannot fast-forward")
        return False
    merge_args = ["pull", "--no-rebase", "--no-edit"]
    try:
        rc2, _, err2 = git_cancellable(path, merge_args, cancel_event=cancel_event)
    except Exception as e:  # noqa: BLE001
        t = tasks.add(f"{name}: pull", parent=parent_task)
        tasks.update(t, "fail", first_line(str(e)))
        return False
    _, head_after2, _ = git(path, ["rev-parse", "HEAD"])
    if rc2 == 130:
        t = tasks.add(f"{name}: pull", parent=parent_task)
        tasks.update(t, "warn", "cancelled")
        return False
    if rc2 != 0:
        t = tasks.add(f"{name}: pull", parent=parent_task)
        tasks.update(t, "fail", first_line(err2) or first_line(err) or "pull failed")
        return False
    t = tasks.add(f"{name}: pull", parent=parent_task)
    detail = ""
    if head_before.strip() != head_after2.strip():
        detail = "merged upstream"
    tasks.update(t, "ok", detail)
    return True


def refresh_repo_with_remote_state(repo: Repo) -> None:
    """`refresh_repo` plus a best-effort workflow-state hydration.

    Most refresh callers should use `refresh_repo` directly. This wrapper is
    reserved for places that explicitly need GitHub-side workflow state."""
    apply_repo_refresh_snapshot(
        repo,
        read_repo_refresh_snapshot(repo, message=repo.message),
    )
    if not gh_available() or not repo.workflows:
        return
    slug = parse_github_slug(repo.remote_url_raw)
    if slug:
        merge_remote_workflow_states(repo.workflows, slug)
        repo.workflow_states_hydrated = True


# ---------- GitHub Actions tracking ---------------------------------------


def _gh_run_status_to_task(run: dict) -> Tuple[str, str]:
    """Map a `gh` run/job JSON object to an idlegit (status, message). Runs
    that are still queued / in_progress stay "running"; completed runs
    collapse to ok / fail / warn based on their conclusion."""
    status = (run.get("status") or "").lower()
    conclusion = (run.get("conclusion") or "").lower()
    if status != "completed":
        return "running", ""
    if conclusion == "success":
        return "ok", ""
    if conclusion in ("failure", "timed_out", "startup_failure", "action_required"):
        return "fail", conclusion or "failed"
    if conclusion in ("cancelled", "skipped", "neutral", "stale"):
        return "warn", conclusion or "skipped"
    return "warn", conclusion or "unknown"


def _workflow_poll_outcome(status: str, message: str = "") -> JobTaskOutcome:
    if status == "fail":
        return JobTaskOutcome(JobStatus.FAIL, message)
    if status == "warn":
        return JobTaskOutcome(JobStatus.WARN, message)
    return JobTaskOutcome(JobStatus.OK, message)


def _merge_job_task_outcome(
        current: JobTaskOutcome,
        candidate: JobTaskOutcome,
) -> JobTaskOutcome:
    if candidate.status is None:
        return current
    if current.status == JobStatus.FAIL:
        return current
    if candidate.status == JobStatus.FAIL:
        return candidate
    if current.status == JobStatus.WARN:
        return current
    return candidate


def _current_step_label(job: dict) -> str:
    """Pick the step within a job that best represents its progress: the
    one that's currently in_progress, else the most recently completed one,
    else the first declared step. Empty string if the job has no steps."""
    steps = job.get("steps") or []
    if not steps:
        return ""
    for s in steps:
        if (s.get("status") or "").lower() == "in_progress":
            return s.get("name", "") or ""
    completed = [s for s in steps if (s.get("status") or "").lower() == "completed"]
    if completed:
        return completed[-1].get("name", "") or ""
    return steps[0].get("name", "") or ""


def _format_run_label(repo_label: str, workflow_name: str, current_step: str = "") -> str:
    base = f"↗ {repo_label}: {workflow_name}"
    if current_step:
        return f"{base} — {current_step}"
    return base


def _format_job_label(job_name: str, current_step: str = "") -> str:
    if current_step:
        return f"  ↳ {job_name} — {current_step}"
    return f"  ↳ {job_name}"


def _create_and_push_tag(tasks, task_obj, repo_path: Path, tag_name: str, sha: str) -> None:
    """Create `tag_name` at `sha` in `repo_path`, then push it to
    origin. Updates `task_obj` to ok/fail in place. Used by both the
    after-push and after-workflow `__add_tag__` then-run handlers —
    the chain's whole purpose is to land a tag upstream (so a
    tag-triggered workflow like release.yml can fire), so an unpushed
    local tag isn't a useful end-state. Push failure leaves the
    local tag in place; the user can rerun the push manually after
    fixing the cause (auth, network, etc.)."""
    if not tag_name or tag_name.startswith("-"):
        tasks.update(task_obj, "fail", "tag name empty or unsafe")
        return
    if not sha:
        tasks.update(task_obj, "fail", "no sha to tag")
        return
    rc_t, _, err_t = git(repo_path, ["tag", tag_name, sha])
    if rc_t != 0:
        tasks.update(task_obj, "fail", first_line(err_t) or "git tag failed")
        return
    rc_p, _, err_p = git(repo_path, ["push", "origin", tag_name])
    if rc_p != 0:
        tasks.update(task_obj, "fail", f"push: {first_line(err_p) or 'failed'}")
        return
    tasks.update(task_obj, "ok")


def _workflow_intent_from_draft(draft) -> WorkflowIntentSnapshot:
    return WorkflowIntentSnapshot(
        track_workflow=dict(draft.track_workflow),
        then_run_after_push=draft.then_run_after_push,
        then_run_params_after_push=dict(draft.then_run_params_after_push),
        then_run_after_workflow=dict(draft.then_run_after_workflow),
        then_run_params_after_workflow={
            key: dict(value)
            for key, value in draft.then_run_params_after_workflow.items()
        },
    )


def _poll_run(
    state: State,
    slug: str,
    run_id: int,
    repo: Repo,
    workflow_name: str,
    run_task: Task,
    pending_task: Optional[Task] = None,
    pushed_sha: str = "",
    then_run_after_workflow: Optional["dict[str, str]"] = None,
    then_run_params_after_workflow: Optional["dict[str, dict[str, str]]"] = None,
    task_bridge: Optional[JobTaskBridge] = None,
) -> JobTaskOutcome:
    """Poll one run's detailed view until it terminates, mirroring its
    progress to the sidebar. Each job materialises as its own indented
    sub-task, and the parent task's label refreshes with the current step
    of whichever job is most active. When the run finishes successfully,
    fire the repo's "then run after <workflow>" chain — if any.

    `pending_task`, when set, is a `pending`-status placeholder row
    that the caller already inserted directly under `run_task` (so the
    "↪ then run: X" line stays adjacent to its parent even when other
    workflow runs land in the panel between this call and the first
    poll). On success the placeholder transforms (via `existing_task=`
    on `kick_off_manual_dispatch`) into the dispatch step rather than
    leaving a duplicate row behind; on fail/warn it short-circuits to
    a "skipped" warn."""
    base_interval = max(0.5, state.actions_poll_seconds)
    poll_interval = base_interval
    # Cap the backed-off interval — long-queued runs still want to be
    # detected within a minute of transitioning. 60s is the upper end
    # of "still feels live" for a TUI dashboard.
    max_interval = max(60.0, base_interval * 12)
    # Abandon the run after this many consecutive failures (gh broken,
    # network down, run deleted). At base_interval=5s this is ~1 min
    # of tolerance, which beats hanging the thread forever.
    failure_budget = 12
    consecutive_failures = 0
    last_raw_status: Optional[str] = None
    job_tasks: "dict[int, Task]" = {}
    repo_label = state.task_repo_label(repo)
    tasks = task_bridge or JobTaskBridge(state.tasks)
    tasks.attach(run_task)
    if pending_task is not None:
        tasks.attach(pending_task)

    # Stash workflow-tracking data in the workflow-run registry so task
    # rows remain presentation while detail/log actions still have a stable
    # run handle.
    run_record = state.workflow_runs.create_for_task(
        run_task,
        repo=repo,
        slug=slug,
        run_id=run_id,
        workflow_name=workflow_name,
    )

    while True:
        view = get_run_view(slug, run_id)
        if view is None:
            consecutive_failures += 1
            if consecutive_failures >= failure_budget:
                tasks.update(run_task, "warn", "polling abandoned — gh unreachable")
                if pending_task is not None:
                    tasks.update(pending_task, "warn", "skipped — polling abandoned")
                return JobTaskOutcome(
                    JobStatus.WARN, "polling abandoned — gh unreachable")
            time.sleep(poll_interval)
            continue
        consecutive_failures = 0

        # Capture the run-level URL once we have it, so the detail
        # modal's "Open in browser" item works even if later polls
        # fail or return None.
        url = view.get("url") or view.get("html_url") or ""
        state.workflow_runs.update(
            run_record.record_id, latest_view=view, run_url=url)

        jobs = view.get("jobs") or []
        for job in jobs:
            jid = job.get("databaseId") or job.get("id")
            if not isinstance(jid, int):
                continue
            jname = job.get("name") or "job"
            step_label = _current_step_label(job)
            label = _format_job_label(jname, step_label)
            if jid not in job_tasks:
                job_tasks[jid] = tasks.add(label, parent=run_task)
                # Job rows share the parent run's slug + run_id so a cancel
                # or log view from a job row still hits the right run.
                state.workflow_runs.create_for_task(
                    job_tasks[jid],
                    repo=repo,
                    slug=slug,
                    run_id=run_id,
                    workflow_name=workflow_name,
                    job_id=jid,
                )
            else:
                tasks.set_label(job_tasks[jid], label)
            jstatus, jmsg = _gh_run_status_to_task(job)
            tasks.update(job_tasks[jid], jstatus, jmsg)

        active = next(
            (j for j in jobs if (j.get("status") or "").lower() == "in_progress"),
            None,
        )
        if active is None:
            active = next(
                (j for j in jobs if (j.get("status") or "").lower() != "completed"),
                None,
            )
        focus_step = _current_step_label(active) if active else ""
        tasks.set_label(run_task, _format_run_label(repo_label, workflow_name, focus_step))

        rstatus, rmsg = _gh_run_status_to_task(view)
        if rstatus != "running":
            tasks.update(run_task, rstatus, rmsg)
            # Drop the heavy `latest_view` JSON now that the run is
            # done — terminal tasks may sit in the panel for a long
            # time (or indefinitely when auto-remove is off), and
            # the snapshot can be 10–100 KB per run. The run id / url
            # / workflow_name fields stay so the detail modal still
            # works for "Open in browser".
            state.workflow_runs.update(run_record.record_id, latest_view=None)
            # "Then run after <workflow>" chain — only fire on a clean
            # success. We pop the entry so a later push doesn't double-
            # fire (the user reset their selection on the next review).
            # Two shapes:
            #   * a workflow name → manual dispatch (existing path)
            #   * "__add_tag__"   → tag the commit that triggered
            #     this run with the buffered name. Falls back to
            #     `git rev-parse HEAD` when no pushed_sha was
            #     captured (manual-dispatch chain).
            if rstatus == "ok":
                if then_run_after_workflow is not None:
                    next_target = then_run_after_workflow.pop(
                        workflow_name, "")
                    params = (
                        then_run_params_after_workflow.pop(workflow_name, {})
                        if then_run_params_after_workflow is not None
                        else {}
                    )
                elif pending_task is not None:
                    followup = state.workflow_followups.record_for_task(
                        pending_task)
                    next_target = "" if followup is None else followup.target
                    params = {}
                else:
                    next_target, params = (
                        state.store.pop_repo_then_run_after_workflow(
                            repo, workflow_name))
                if next_target == "__add_tag__":
                    # Pop the slot's parameter bucket — currently
                    # only "tag" lives in it, but the dict shape
                    # leaves room for more inputs (e.g.
                    # workflow_dispatch fields wired through the
                    # same ParamSpec pattern in the future).
                    tag_name = params.get("tag", "").strip()
                    sha = pushed_sha
                    if not sha:
                        rc_h, head_out, _ = git(repo.path, ["rev-parse", "HEAD"])
                        sha = head_out.strip() if rc_h == 0 else ""
                    tag_label = f"  ↪ tag {tag_name}" if tag_name else "  ↪ tag (empty name)"
                    if pending_task is not None:
                        tasks.set_label(pending_task, tag_label)
                        tasks.clear_message(pending_task)
                        tasks.update(pending_task, "running", "")
                        tag_task = pending_task
                    else:
                        tag_task = tasks.add(tag_label, parent=run_task)
                    _create_and_push_tag(tasks, tag_task, repo.path, tag_name, sha)
                elif next_target:
                    branch = repo.branch or "main"
                    # Pop the per-workflow input buffer the same
                    # way the add-tag branch above pops its tag
                    # buffer — keeps a follow-up run from
                    # re-dispatching with stale -F values.
                    kick_off_manual_dispatch(
                        state,
                        repo,
                        next_target,
                        branch,
                        existing_task=pending_task,
                        inputs=params,
                    )
            elif pending_task is not None:
                # Parent didn't succeed — the chain is dead. Mark the
                # placeholder so the user can see the chain was skipped
                # rather than leave a stuck "pending" row.
                tasks.update(pending_task, "warn", "skipped — parent didn't succeed")
            return _workflow_poll_outcome(rstatus, rmsg)
        tasks.update(run_task, "running")
        # Geometric backoff: while the run-level status field is
        # unchanged (e.g. stuck in "queued" or steady "in_progress"),
        # widen the poll interval. As soon as it transitions, snap
        # back to base so the user sees step-level changes promptly.
        raw_status = (view.get("status") or "").lower()
        if last_raw_status is None or last_raw_status != raw_status:
            poll_interval = base_interval
            last_raw_status = raw_status
        else:
            poll_interval = min(max_interval, poll_interval * 1.5)
        time.sleep(poll_interval)


def kick_off_workflow_tracking(
    state: State,
    slug: str,
    run: dict,
    repo: Repo,
    pushed_sha: str = "",
    then_run_after_workflow: Optional["dict[str, str]"] = None,
    then_run_params_after_workflow: Optional["dict[str, dict[str, str]]"] = None,
) -> Optional[Task]:
    """Add a sidebar task for a known GitHub Actions run and spawn a daemon
    that updates it (and its job sub-tasks) until completion. Returns the
    parent task on success, or None when the run dict is unusable.

    `pushed_sha` (when supplied) is the commit that triggered this
    run — used by `_poll_run` to tag the right commit if the
    repo has wired up an "__add_tag__" then-run for this workflow.
    Empty when called from a manual dispatch path; the tag fallback
    in `_poll_run` reads HEAD at that moment in that case.

    If the repo has a chained then-run wired up for `workflow_name`,
    we insert its `pending`-status placeholder row SYNCHRONOUSLY here,
    immediately after the run task — that way the "↪ then run: …"
    line stays adjacent to its parent even when another tracked run
    lands in the panel before this run's poller has had a chance to
    start. The placeholder is then handed to `_poll_run` so it can
    transform / fail it as the parent run progresses."""
    workflow_name = run.get("workflowName") or run.get("name") or "workflow"
    repo_label = state.task_repo_label(repo)
    run_id = run.get("databaseId")
    if not isinstance(run_id, int):
        t = state.tasks.add(_format_run_label(repo_label, workflow_name))
        state.tasks.update(t, "fail", "no run id")
        return None
    job = state.job_registry.start(
        JobSpec(
            kind="workflow-poll",
            label=_format_run_label(repo_label, workflow_name),
            local_mutation=False,
            repo_keys=(str(repo.path),),
        )
    )
    task_bridge = JobTaskBridge(state.tasks, state.job_registry, job)
    t = task_bridge.add(_format_run_label(repo_label, workflow_name))
    state.workflow_runs.create_for_task(
        t,
        repo=repo,
        slug=slug,
        run_id=run_id,
        workflow_name=workflow_name,
    )

    fallback_intent = (
        WorkflowIntentSnapshot()
        if then_run_after_workflow is not None
        else state.store.take_repo_workflow_intent(repo)
    )
    effective_then_run_after_workflow = (
        then_run_after_workflow
        if then_run_after_workflow is not None
        else dict(fallback_intent.then_run_after_workflow)
    )
    effective_then_run_params_after_workflow = (
        then_run_params_after_workflow
        if then_run_params_after_workflow is not None
        else {
            key: dict(value)
            for key, value in
            fallback_intent.then_run_params_after_workflow.items()
        }
    )
    pending_source = (
        effective_then_run_after_workflow
    )
    pending_params_source = (
        effective_then_run_params_after_workflow
    )
    pending_then_run = pending_source.get(workflow_name, "")
    pending_task: Optional[Task] = None
    if pending_then_run:
        # Pretty up the placeholder when the chain is "add tag" —
        # the user-facing label says "tag <name>" so the row reads
        # like a real action rather than the sentinel.
        if pending_then_run == "__add_tag__":
            tag_name = pending_params_source.get(
                workflow_name, {}).get("tag", "")
            placeholder_label = (
                f"  ↪ then run: tag {tag_name}" if tag_name else "  ↪ then run: tag (name unset)"
            )
        else:
            placeholder_label = f"  ↪ then run: {pending_then_run}"
        pending_task = task_bridge.add(placeholder_label, parent=t)
        task_bridge.update(pending_task, "pending", f"waiting on {workflow_name}")
        state.workflow_followups.create_for_task(
            pending_task,
            repo=repo,
            parent_workflow=workflow_name,
            target=pending_then_run,
        )

    def worker(job: Job) -> None:
        poll_kwargs = {}
        if then_run_after_workflow is not None:
            poll_kwargs["then_run_after_workflow"] = then_run_after_workflow
        if then_run_params_after_workflow is not None:
            poll_kwargs[
                "then_run_params_after_workflow"
            ] = then_run_params_after_workflow
        outcome = _poll_run(
            state, slug, run_id, repo, workflow_name, t, pending_task,
            pushed_sha, task_bridge=task_bridge, **poll_kwargs)
        if outcome.status is not None:
            state.job_registry.finish(job, outcome.status, outcome.message)

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    thread = start_job_thread(
        state.job_registry,
        job,
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        message = first_line(job.message)
        task_bridge.update(t, "fail", message)
        if pending_task is not None:
            task_bridge.update(pending_task, "warn", "skipped — workflow polling failed")
    return t


def kick_off_post_push_run_tracking(
    state: State,
    repo: Repo,
    branch: str,
    sha: str,
    tracked_names: Iterable[str],
    then_run_after_workflow: Optional["dict[str, str]"] = None,
    then_run_params_after_workflow: Optional["dict[str, dict[str, str]]"] = None,
) -> None:
    """After a successful push, watch for the GitHub Actions runs triggered
    by that push on `branch`@`sha`. Each run whose workflowName is in
    `tracked_names` becomes its own sidebar task + indented sub-tasks via
    kick_off_workflow_tracking. Stops after a 2-min window so a paths-ignore
    miss doesn't keep us polling forever."""
    wanted = {n for n in tracked_names if n}
    if not wanted:
        return
    slug = parse_github_slug(repo.remote_url_raw)
    if not slug or not gh_available() or not branch or not sha:
        return

    repo_label = state.task_repo_label(repo)
    poll_interval = max(0.5, state.actions_poll_seconds)

    def watcher(job: Job) -> None:
        deadline = time.monotonic() + POST_PUSH_RUN_DISCOVERY_TIMEOUT_SECONDS
        seen: set = set()
        remaining = set(wanted)
        while remaining and time.monotonic() < deadline:
            for run in list_recent_runs(slug, branch, sha, limit=20):
                rid = run.get("databaseId")
                if not isinstance(rid, int) or rid in seen:
                    continue
                wf = run.get("workflowName") or run.get("name") or ""
                if wf not in remaining:
                    continue
                seen.add(rid)
                remaining.discard(wf)
                tracking_kwargs = {"pushed_sha": sha}
                if then_run_after_workflow is not None:
                    tracking_kwargs[
                        "then_run_after_workflow"
                    ] = then_run_after_workflow
                if then_run_params_after_workflow is not None:
                    tracking_kwargs[
                        "then_run_params_after_workflow"
                    ] = then_run_params_after_workflow
                kick_off_workflow_tracking(
                    state, slug, run, repo, **tracking_kwargs)
            if remaining:
                time.sleep(poll_interval)
        for wf in remaining:
            t = state.tasks.add(f"↗ {repo_label}: {wf}")
            state.tasks.update(t, "warn", "no run triggered within 2 min")
        if remaining:
            state.job_registry.finish(job, JobStatus.WARN, "some workflow runs did not appear")

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="workflow-poll-discovery",
            label=f"{repo_label}: discover workflow runs",
            local_mutation=False,
            repo_keys=(str(repo.path),),
        ),
        watcher,
        thread_factory=thread_factory,
    )
    if thread is None:
        t = state.tasks.add(f"↗ {repo_label}: discover workflow runs")
        state.tasks.update(t, "fail", first_line(job.message))


def kick_off_manual_dispatch(
    state: State,
    target_repo: Repo,
    workflow_name: str,
    ref: str,
    *,
    existing_task: Optional[Task] = None,
    inputs: "Optional[dict[str, str]]" = None,
) -> None:
    """Fire `gh workflow run` for `workflow_name` against `ref`, then poll
    for the resulting run id and hand off to kick_off_workflow_tracking.
    workflow_dispatch runs don't carry a commit filter, so we identify the
    new run by tracking which run ids existed before dispatch and looking
    for a fresh one with a matching workflowName.

    `existing_task` lets a chained then-run reuse the placeholder row
    that `_poll_run` added in `pending` state, so the dispatch step
    transforms in place rather than spawning a fresh row alongside it.

    `inputs` (when supplied) maps `workflow_dispatch.inputs` names
    to the values the user typed in the review screen's inline
    param rows; non-empty entries are forwarded as `-F name=value`
    by `dispatch_workflow`. Empty / missing keys leave the
    workflow's declared default in place (or raise the canonical
    "required input missing" gh error for required-but-blank
    fields)."""
    slug = parse_github_slug(target_repo.remote_url_raw)
    repo_label = state.task_repo_label(target_repo)
    if not slug or not gh_available():
        if existing_task is not None:
            state.tasks.update(existing_task, "fail", "gh CLI / github remote unavailable")
        else:
            t = state.tasks.add(f"↗ {repo_label}: {workflow_name}")
            state.tasks.update(t, "fail", "gh CLI / github remote unavailable")
        return

    job = state.job_registry.start(
        JobSpec(
            kind="workflow-dispatch",
            label=f"{repo_label}: dispatch {workflow_name}",
            local_mutation=False,
            repo_keys=(str(target_repo.path),),
        )
    )
    task_bridge = JobTaskBridge(state.tasks, state.job_registry, job)

    if existing_task is not None:
        dispatch_task = existing_task
        task_bridge.attach(dispatch_task)
        # Keep the indented "↪ …" prefix so the row stays visually
        # nested under the parent that triggered it.
        task_bridge.set_label(dispatch_task, f"  ↪ dispatch {workflow_name}")
        task_bridge.update(dispatch_task, "running", "")
    else:
        dispatch_task = task_bridge.add(f"↗ {repo_label}: dispatch {workflow_name}")

    def worker(job: Job) -> None:
        ok, msg = dispatch_workflow(slug, workflow_name, ref, inputs=inputs)
        if not ok:
            task_bridge.update(dispatch_task, "fail", msg)
            state.job_registry.finish(job, JobStatus.FAIL, msg)
            return
        task_bridge.update(dispatch_task, "ok", msg)

        before: set = set()
        for r in list_recent_runs(slug, ref, "", limit=20):
            rid = r.get("databaseId")
            if isinstance(rid, int):
                before.add(rid)

        deadline = time.monotonic() + 30.0
        poll_interval = max(0.5, state.actions_poll_seconds)
        while time.monotonic() < deadline:
            for r in list_recent_runs(slug, ref, "", limit=20):
                rid = r.get("databaseId")
                wf = r.get("workflowName") or r.get("name") or ""
                if isinstance(rid, int) and rid not in before and wf == workflow_name:
                    kick_off_workflow_tracking(state, slug, r, target_repo)
                    return
            time.sleep(poll_interval)
        t = task_bridge.add(f"↗ {repo_label}: {workflow_name}")
        task_bridge.update(t, "warn", "dispatched but no run appeared in 30s")
        state.job_registry.finish(job, JobStatus.WARN, "dispatched but no run appeared in 30s")

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    thread = start_job_thread(
        state.job_registry,
        job,
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        task_bridge.update(dispatch_task, "fail", first_line(job.message))


# ---------- Single-repo refresh after an action ---------------------------


def _state_link_siblings(state: State, repos: List[Repo],
                         subtrees: Optional[List[SubtreeSpec]]) -> None:
    snapshot = read_link_siblings_snapshot(
        repos,
        subtrees,
        busy_child_predicate=read_only_child_busy_predicate(state),
        child_message_lookup=state.store.row_message,
    )
    workspace = _workspace_for_repo_snapshot(state, repos)
    name = workspace.name if workspace is not None else state.workspace_name
    folders = workspace.folders if workspace is not None else state.active_folders
    activate = workspace is state.active_workspace
    state.store.replace_workspace_topology(
        name=name,
        folders=folders,
        repos=repos,
        children=[
            _child_topology_snapshot(parent, child)
            for parent, child in snapshot.parent_child_pairs()
        ],
        activate=activate,
    )
    apply_link_siblings_snapshot(snapshot)


def _workspace_for_repo_snapshot(
        state: State,
        repos: List[Repo],
) -> Optional[Workspace]:
    snapshot_ids = tuple(id(repo) for repo in repos)
    for workspace in state.workspaces:
        if tuple(id(repo) for repo in workspace.cached_repos) == snapshot_ids:
            return workspace
    if tuple(id(repo) for repo in state.repos) == snapshot_ids:
        return state.active_workspace
    return state.active_workspace


def _child_topology_snapshot(
        parent: Repo,
        child: ChildRef,
) -> ChildTopologySnapshot:
    return ChildTopologySnapshot(
        parent_repo=parent,
        child=child,
        status=ChildStatusSnapshot(
            kind=child.kind,
            branch=child.branch,
            head=child.head,
            upstream=child.upstream,
            ahead=child.ahead,
            behind=child.behind,
            dirty=child.dirty,
            message=child.message,
            error=child.error,
            merging=child.merging,
            in_sync=child.in_sync,
        ),
    )


def _refresh_repo_snapshot_into_state(state: State, repo: Repo) -> None:
    """Refresh one repo through a typed snapshot and publish store state first."""
    snapshot = read_repo_refresh_snapshot(
        repo,
        message=state.store.row_message(repo),
    )
    state.store.publish_repo_status_snapshot(repo, snapshot.status_snapshot())
    apply_repo_refresh_snapshot(repo, snapshot)


def _refresh_target_state(
    state: State,
    target_repo: Optional[Repo],
    target_parent: Optional[Repo],
    snapshot_repos: Optional[List[Repo]] = None,
    snapshot_subtrees: Optional[List[SubtreeSpec]] = None,
) -> ReconcileResult:
    """Re-fetch state for just one row's repo. For top-level rows we
    refresh the Repo itself; for submodule child rows we refresh the
    parent (its dirty state changes when the nested checkout moves) and
    then re-link siblings so the child's HEAD/in_sync/dirty fields catch
    up."""
    refresh_targets: List[Repo] = []
    if target_repo is not None:
        refresh_targets.append(target_repo)
    if target_parent is not None and target_parent is not target_repo:
        refresh_targets.append(target_parent)
    repos = snapshot_repos if snapshot_repos is not None else state.repos
    subtrees = snapshot_subtrees if snapshot_subtrees is not None else state.subtrees
    refresh_scope = (
        WorkspaceRefreshScope.capture(state)
        if snapshot_repos is None and snapshot_subtrees is None
        else None
    )
    return reconcile_repos_bounded(
        refresh_targets,
        subtrees,
        link_repos=repos,
        refresh_fn=lambda repo: _refresh_repo_snapshot_into_state(state, repo),
        link_fn=lambda link_repos, link_subtrees: _state_link_siblings(
            state, link_repos, link_subtrees),
        should_link=(
            None if refresh_scope is None
            else lambda: refresh_scope.is_active_current(state)
        ),
    )


# ---------- Single git-action launcher ------------------------------------


def _find_child_at(parent: Optional[Repo], path: Path) -> Optional[ChildRef]:
    """Locate the ChildRef inside `parent` whose nested checkout lives
    at `path`. Used by `kick_off_action` to flip the row's refreshing
    spinner while an action runs against a nested submodule child."""
    if parent is None:
        return None
    for child in parent.children:
        if child.nested_path == path:
            return child
    return None


def kick_off_action(
    state: State,
    action_id: str,
    *,
    target_label: str,
    target_path: Path,
    target_repo: Optional[Repo],
    target_parent: Optional[Repo],
    branch_arg: str = "",
    reset_count: int = 0,
) -> None:
    """Spawn a daemon worker that runs one git action against `target_path`,
    publishes its progress to the sidebar, and quietly re-queries that one
    repo's state when it finishes. Returns immediately so the UI is free.

    The targeted row's `refreshing` flag is held high from the moment
    the action is submitted until the post-action refresh completes —
    its state dot renders as the global spinner glyph during that
    window, so it's obvious the row's state is in transition rather
    than the user wondering whether their keystroke registered."""
    known_actions = {
        "fetch",
        "pull",
        "push",
        "soft_reset",
        "switch_branch",
        "checkout_remote_branch",
        "branch_from_head",
        "create_branch",
        "ff_merge",
        "rename_branch",
        "set_upstream",
        "stash_create",
        "stash_apply",
    }
    should_refresh = action_id in known_actions
    snapshot_repos = list(state.repos)
    snapshot_subtrees = list(state.subtrees)
    target_child = _find_child_at(target_parent, target_path)
    # Claim the refresh slot SYNCHRONOUSLY before returning so the
    # very next redraw shows the spinner — the daemon worker may not
    # run for a tick, and even a 100ms gap reads as "did anything
    # happen?". WorkerClaim owns both the parent repo and child claim
    # so a child-claim failure cannot strand the parent lock.
    claim = WorkerClaim(
        state,
        repo=target_repo,
        child=target_child,
        acquire_repo=target_repo is not None,
        acquire_child=target_child is not None,
    )
    try:
        claim.__enter__()
    except RuntimeError:
        t = state.tasks.add(f"{target_label}: skipped")
        state.tasks.update(t, "warn", "refresh in progress — try again")
        return

    def worker(job: Job) -> None:
        started_at = time.monotonic()
        task_bridge = JobTaskBridge(state.tasks, state.job_registry, job)
        try:
            if action_id == "fetch":
                t = task_bridge.add(f"{target_label}: fetch")
                rc, _, err = git(target_path, ["fetch", "--all"])
                task_bridge.update(
                    t, "ok" if rc == 0 else "fail", "" if rc == 0 else first_line(err)
                )
            elif action_id == "pull":
                ok = _pull_prefer_ff_then_merge(
                    target_path, task_bridge, target_label, allow_merge_fallback=True
                )
                if not ok:
                    return
            elif action_id == "push":
                t = task_bridge.add(f"{target_label}: push")
                rc_b, b_out, _ = git(target_path, ["branch", "--show-current"])
                cur_branch = b_out.strip() if rc_b == 0 else ""
                if cur_branch and not is_safe_ref_arg(cur_branch):
                    task_bridge.update(t, "fail", "unsafe current branch name")
                    return
                rc_u, u_out, _ = git(
                    target_path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
                )
                has_upstream = rc_u == 0 and bool(u_out.strip())
                if has_upstream:
                    ok_pull = _pull_prefer_ff_then_merge(
                        target_path, task_bridge, target_label, allow_merge_fallback=True
                    )
                    if not ok_pull:
                        task_bridge.update(t, "fail", "skipped: cannot pull")
                        return
                    rc, _, err = git(
                        target_path, ["push"], timeout=USER_PUSH_TIMEOUT_SECONDS)
                    task_bridge.update(
                        t, "ok" if rc == 0 else "fail", "" if rc == 0 else first_line(err)
                    )
                elif cur_branch:
                    rc, _, err = git(
                        target_path,
                        ["push", "--set-upstream", "origin", cur_branch],
                        timeout=USER_PUSH_TIMEOUT_SECONDS,
                    )
                    task_bridge.update(
                        t, "ok" if rc == 0 else "fail", "" if rc == 0 else first_line(err)
                    )
                else:
                    task_bridge.update(t, "fail", "no current branch")
            elif action_id == "soft_reset":
                if reset_count <= 0:
                    t = task_bridge.add(f"{target_label}: soft reset all unpushed (to @{{u}})")
                    rc, _, err = git(target_path, ["reset", "--soft", "@{u}"])
                else:
                    t = task_bridge.add(f"{target_label}: soft reset HEAD~{reset_count}")
                    rc, _, err = git(target_path, ["reset", "--soft", f"HEAD~{reset_count}"])
                task_bridge.update(
                    t, "ok" if rc == 0 else "fail", "" if rc == 0 else first_line(err)
                )
            elif action_id == "switch_branch":
                t = task_bridge.add(f"{target_label}: checkout {branch_arg}")
                if not is_safe_ref_arg(branch_arg):
                    task_bridge.update(t, "fail", "unsafe branch name")
                    return
                # Refuse the switch if HEAD has commits not on the chosen
                # branch — git would otherwise silently orphan them and
                # files unique to those commits would vanish from WT.
                # The user picked the branch via the menu, but they may
                # not realise their HEAD is detached with unpushed work.
                if not _head_is_ancestor_of(target_path, branch_arg):
                    task_bridge.update(
                        t,
                        "warn",
                        f"HEAD has commits not on {branch_arg} — would orphan "
                        "them; manual: `git checkout -b <name>` to keep them",
                    )
                else:
                    rc, _, err = git(target_path, ["checkout", branch_arg])
                    task_bridge.update(
                        t, "ok" if rc == 0 else "fail", "" if rc == 0 else first_line(err)
                    )
            elif action_id == "checkout_remote_branch":
                t = task_bridge.add(f"{target_label}: checkout remote {branch_arg}")
                if not is_safe_ref_arg(branch_arg) or "/" not in branch_arg:
                    task_bridge.update(t, "fail", "unsafe remote ref")
                    return
                short = branch_arg.split("/", 1)[1]
                if not short or not is_safe_ref_arg(short):
                    task_bridge.update(t, "fail", "unsafe branch name")
                    return
                rc, out, _ = git(target_path, ["branch", "--list", short])
                local_exists = rc == 0 and bool(out.strip())
                checkout_ref = short if local_exists else branch_arg
                if not _head_is_ancestor_of(target_path, checkout_ref):
                    task_bridge.update(
                        t,
                        "warn",
                        f"HEAD has commits not on {checkout_ref} — would orphan "
                        "them; manual: `git checkout -b <name>` to keep them",
                    )
                elif local_exists:
                    rc, _, err = git(target_path, ["checkout", short])
                    task_bridge.update(
                        t, "ok" if rc == 0 else "fail", "" if rc == 0 else first_line(err)
                    )
                else:
                    rc, _, err = git(target_path, ["checkout", "-b", short, branch_arg])
                    task_bridge.update(
                        t, "ok" if rc == 0 else "fail", "" if rc == 0 else first_line(err)
                    )
            elif action_id == "branch_from_head":
                # Save a detached HEAD's commits onto a fresh branch.
                # `git checkout -b <name>` only creates a ref + flips HEAD
                # to it — non-destructive (cardinal-rule safe). The new
                # branch points at the SAME commit HEAD was on, so every
                # unique commit is now reachable from a named branch and
                # `merge-base --is-ancestor` checks elsewhere will treat
                # the work as no longer at risk of being orphaned.
                t = task_bridge.add(f"{target_label}: branch HEAD as {branch_arg}")
                if not is_safe_ref_arg(branch_arg):
                    task_bridge.update(t, "fail", "unsafe branch name")
                    return
                rc, _, err = git(target_path, ["checkout", "-b", branch_arg])
                task_bridge.update(
                    t, "ok" if rc == 0 else "fail", "" if rc == 0 else first_line(err)
                )
            elif action_id == "create_branch":
                # Create a new branch off the current HEAD and switch to
                # it. Same `git checkout -b <name>` plumbing as
                # branch_from_head — distinct action_id so the task label
                # reads naturally when the user wasn't actually detached.
                t = task_bridge.add(f"{target_label}: create branch {branch_arg}")
                if not is_safe_ref_arg(branch_arg):
                    task_bridge.update(t, "fail", "unsafe branch name")
                    return
                rc, _, err = git(target_path, ["checkout", "-b", branch_arg])
                task_bridge.update(
                    t, "ok" if rc == 0 else "fail", "" if rc == 0 else first_line(err)
                )
            elif action_id == "ff_merge":
                # Fast-forward-only merge: refuses on its own if a real
                # merge commit would be needed, so divergent histories
                # never silently get a merge commit the user didn't ask
                # for. The lack of `--no-ff` etc. keeps this strict.
                t = task_bridge.add(f"{target_label}: merge --ff-only {branch_arg}")
                if not is_safe_ref_arg(branch_arg):
                    task_bridge.update(t, "fail", "unsafe branch name")
                    return
                rc, _, err = git(target_path, ["merge", "--ff-only", branch_arg])
                if rc == 0:
                    task_bridge.update(t, "ok")
                else:
                    task_bridge.update(t, "fail", first_line(err) or "not a fast-forward")
            elif action_id == "rename_branch":
                # `git branch -m <newname>` renames the *current* branch in
                # place — only touches refs, no commits orphaned. Refuses
                # on detached HEAD via git's own error. Cardinal-rule safe.
                t = task_bridge.add(f"{target_label}: rename branch → {branch_arg}")
                if not is_safe_ref_arg(branch_arg):
                    task_bridge.update(t, "fail", "unsafe branch name")
                    return
                rc, _, err = git(target_path, ["branch", "-m", branch_arg])
                task_bridge.update(
                    t, "ok" if rc == 0 else "fail", "" if rc == 0 else first_line(err)
                )
            elif action_id == "set_upstream":
                # `git branch --set-upstream-to=<ref>` only edits config,
                # never touches refs or commits. `branch_arg` is the fully
                # qualified remote-tracking ref (e.g. origin/main).
                t = task_bridge.add(f"{target_label}: upstream → {branch_arg}")
                if not is_safe_ref_arg(branch_arg):
                    task_bridge.update(t, "fail", "unsafe ref name")
                    return
                rc, _, err = git(target_path, ["branch", f"--set-upstream-to={branch_arg}"])
                task_bridge.update(
                    t, "ok" if rc == 0 else "fail", "" if rc == 0 else first_line(err)
                )
            elif action_id == "stash_create":
                # `git stash push` saves working-tree changes to a new
                # stash entry. Cardinal-rule safe: the entry preserves
                # both index and worktree state, and our pipeline never
                # calls `stash drop` / `pop` so nothing is destroyed.
                t = task_bridge.add(f"{target_label}: stash push")
                rc, _, err = git(target_path, ["stash", "push"])
                if rc != 0:
                    task_bridge.update(t, "fail", first_line(err))
                else:
                    task_bridge.update(t, "ok", "")
            elif action_id == "stash_apply":
                # `git stash apply <ref>` reapplies the stash without
                # dropping it — the entry stays around, so an apply that
                # silently drops content can be re-attempted. Pop (apply
                # + drop) is intentionally NOT supported here; that's a
                # cardinal-rule violation.
                t = task_bridge.add(f"{target_label}: stash apply {branch_arg}")
                # branch_arg is `stash@{N}` — protect against shell-style
                # tricks even though git's argv parsing makes them moot.
                if not branch_arg or branch_arg.startswith("-"):
                    task_bridge.update(t, "fail", "unsafe stash ref")
                    return
                rc, _, err = git(target_path, ["stash", "apply", "--", branch_arg])
                task_bridge.update(
                    t, "ok" if rc == 0 else "fail", "" if rc == 0 else first_line(err)
                )
            else:
                return  # unknown action — nothing to do
        except Exception as e:
            t = task_bridge.add(f"{target_label}: failed")
            task_bridge.update(t, "fail", first_line(str(e)))
        finally:
            if should_refresh:
                try:
                    reconcile_result = _refresh_target_state(
                        state, target_repo, target_parent, snapshot_repos, snapshot_subtrees
                    )
                    if isinstance(reconcile_result, ReconcileResult):
                        for failure in reconcile_result.refresh.failures:
                            t = task_bridge.add(f"{target_label}: refresh")
                            task_bridge.update(t, "fail", failure.message or "refresh failed")
                        if reconcile_result.link_error:
                            t = task_bridge.add(f"{target_label}: refresh")
                            task_bridge.update(t, "fail", reconcile_result.link_error)
                except Exception as e:
                    t = task_bridge.add(f"{target_label}: refresh")
                    task_bridge.update(t, "fail", first_line(str(e)))
                remaining = MIN_ACTION_REFRESH_SECONDS - (time.monotonic() - started_at)
                if remaining > 0:
                    time.sleep(remaining)
            task_bridge.finish_failed_or_warned_job(state.job_registry, job)
            # Always release the refresh slot — even on early-return /
            # exception paths — so a row never gets stuck spinning and the
            # underlying lock is freed for fs_watcher / Ctrl+R.
            claim.__exit__(None, None, None)

    def thread_factory(target, name):
        return create_job_thread(target, name)

    repo_keys = (str(target_repo.path),) if target_repo is not None else ()
    child_keys = (str(target_child.nested_path),) if target_child is not None else ()
    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="action",
            label=f"{target_label}: {action_id}",
            local_mutation=should_refresh,
            repo_keys=repo_keys,
            child_keys=child_keys,
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        task_bridge = JobTaskBridge(state.tasks, state.job_registry, job)
        t = task_bridge.add(f"{target_label}: failed")
        task_bridge.update(t, "fail", first_line(job.message))
        claim.__exit__(None, None, None)


# ---------- Remotes batch-apply ------------------------------------------


def _compute_remote_ops(rows) -> List[Tuple]:
    """Diff a RemotesModal.rows list against original_* and return a
    flat list of git-remote sub-operations to run, in safe order:
    removes first (so a rename can reuse the freed name), then renames
    (so set-url can target the new name), then set-urls, then adds.

    Each tuple is shape-tagged: ("remove", name), ("rename", old, new),
    ("set_url", name, new_url), ("add", name, url)."""
    ops: List[Tuple] = []
    for row in rows:
        if row.is_new:
            if row.to_delete:
                continue  # cancelled before apply
            if row.name and row.url:
                ops.append(("add", row.name, row.url))
            continue
        if row.to_delete:
            ops.append(("remove", row.original_name))
            continue
        if row.name and row.name != row.original_name:
            ops.append(("rename", row.original_name, row.name))
        # After rename, set-url targets the new name; both rename and
        # set-url emitted means the set-url runs against the renamed
        # remote, which is what `git remote rename` leaves on disk.
        if row.url and row.url != row.original_url:
            ops.append(("set_url", row.name or row.original_name, row.url))
    return _order_remote_ops(ops)


def _order_remote_ops(ops: List[Tuple]) -> List[Tuple]:
    """Group by op kind so removes free names before renames, renames
    settle before set-urls, and adds run last (lowest risk of
    colliding with anything else)."""
    by_kind = {"remove": [], "rename": [], "set_url": [], "add": []}
    for op in ops:
        by_kind.setdefault(op[0], []).append(op)
    return by_kind["remove"] + by_kind["rename"] + by_kind["set_url"] + by_kind["add"]


def kick_off_remote_changes(
    state: State, modal_rows, target_label: str, target_path: Path, target_repo: Optional[Repo]
) -> int:
    """Apply pending remote changes from the modal as a single batched
    sidebar task. Returns the number of operations dispatched (0 means
    "nothing to do" — caller can skip the confirmation prompt). Each
    op runs sequentially in the same daemon thread so a rename
    completes before its follow-up set-url fires."""
    ops = _compute_remote_ops(modal_rows)
    if not ops:
        return 0

    plural = "" if len(ops) == 1 else "s"
    label = f"{target_label}: applying {len(ops)} remote change{plural}"
    repo_keys = (str(target_repo.path),) if target_repo is not None else ()
    job = state.job_registry.start(JobSpec(
        kind="remote-edit",
        label=label,
        local_mutation=True,
        repo_keys=repo_keys,
    ))
    task_bridge = JobTaskBridge(state.tasks, state.job_registry, job)
    t = task_bridge.add(label)
    claim = WorkerClaim(state, repo=target_repo, task=t, acquire_repo=target_repo is not None)
    try:
        claim.__enter__()
    except RuntimeError:
        task_bridge.update(t, "warn", "repo busy — remote changes skipped")
        state.job_registry.finish(job, JobStatus.WARN, "repo busy")
        return 0

    def worker(job: Job) -> None:
        def fail_job(message: str) -> None:
            state.job_registry.finish(job, JobStatus.FAIL, message)

        try:
            for op in ops:
                if op[0] == "remove":
                    _, name = op
                    if not is_safe_ref_arg(name):
                        msg = f"unsafe remote name: {name}"
                        task_bridge.update(t, "fail", msg)
                        fail_job(msg)
                        return
                    rc, _, err = git(target_path, ["remote", "remove", name])
                elif op[0] == "rename":
                    _, old, new = op
                    if not is_safe_ref_arg(old) or not is_safe_ref_arg(new):
                        msg = f"unsafe remote name: {old}/{new}"
                        task_bridge.update(t, "fail", msg)
                        fail_job(msg)
                        return
                    rc, _, err = git(target_path, ["remote", "rename", old, new])
                elif op[0] == "set_url":
                    _, name, url = op
                    if not is_safe_ref_arg(name) or not url or url.startswith("-"):
                        msg = f"unsafe url for {name}"
                        task_bridge.update(t, "fail", msg)
                        fail_job(msg)
                        return
                    rc, _, err = git(target_path, ["remote", "set-url", name, url])
                elif op[0] == "add":
                    _, name, url = op
                    if not is_safe_ref_arg(name) or not url or url.startswith("-"):
                        msg = f"unsafe url for {name}"
                        task_bridge.update(t, "fail", msg)
                        fail_job(msg)
                        return
                    rc, _, err = git(target_path, ["remote", "add", name, url])
                else:
                    continue
                if rc != 0:
                    msg = first_line(err)
                    task_bridge.update(t, "fail", msg)
                    fail_job(msg)
                    return
            task_bridge.update(t, "ok", "")
        finally:
            # Re-query the repo so its remote_url cache reflects the
            # new origin URL (if origin was touched). Best-effort.
            try:
                if target_repo is not None:
                    refresh_repo_with_remote_state(target_repo)
            finally:
                claim.__exit__(None, None, None)

    def thread_factory(target, name):
        return create_job_thread(target, name)

    thread = start_job_thread(
        state.job_registry,
        job,
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        message = first_line(job.message)
        task_bridge.update(t, "fail", message)
        claim.__exit__(None, None, None)
        return 0
    return len(ops)


# ---------- Clone --------------------------------------------------------


def kick_off_load_commit_view(state: State, modal) -> None:
    """Load a commit view modal's async fields (tags list, full
    message body, file changes, HEAD reflog hits) in a single daemon
    thread so the modal can render immediately with spinner
    placeholders. Each field flips its respective `*_loading` flag
    False as it lands; `cancel_event` short-circuits if the user
    closes the modal while the queries are in flight."""
    from .git_ops import (
        get_commit_details,
        list_tags_at,
        query_commit_files,
        query_commit_reflog,
    )

    if not modal.tags_load_id:
        base_load_id = f"commit-view:{id(state)}:{id(modal.target_path)}:{modal.sha}"
        modal.tags_load_id = f"{base_load_id}:tags"
        modal.details_load_id = f"{base_load_id}:details"
        modal.files_load_id = f"{base_load_id}:files"
        modal.reflog_load_id = f"{base_load_id}:reflog"

    load_ids = [
        modal.tags_load_id,
        modal.details_load_id,
        modal.files_load_id,
        modal.reflog_load_id,
    ]
    for load_id in load_ids:
        state.view_loads.create(load_id)

    def cancelled() -> bool:
        return any(state.view_loads.is_cancelled(load_id)
                   for load_id in load_ids)

    def finish_remaining() -> None:
        state.view_loads.finish(modal.tags_load_id, modal.tags)
        state.view_loads.finish(modal.details_load_id, [])
        state.view_loads.finish(modal.files_load_id, [])
        state.view_loads.finish(modal.reflog_load_id, modal.reflog_entries)

    def worker(_job: Job) -> None:
        try:
            if cancelled():
                return
            modal.tags = list_tags_at(modal.target_path, modal.sha)
            state.view_loads.finish(modal.tags_load_id, modal.tags)
            if cancelled():
                return
            author, date, subject, body = get_commit_details(modal.target_path, modal.sha)
            modal.author = author
            modal.date = date
            # Subject was already populated from the CommitEntry the
            # caller had on hand; only overwrite if the on-disk show
            # disagrees (rare — mostly when the commit moved).
            if subject and not modal.subject:
                modal.subject = subject
            modal.body = body
            state.view_loads.finish(modal.details_load_id, [])
            if cancelled():
                return
            modal.files = query_commit_files(modal.target_path, modal.sha)
            state.view_loads.finish(modal.files_load_id, [])
            if cancelled():
                return
            modal.reflog_entries = query_commit_reflog(modal.target_path, modal.sha)
        finally:
            finish_remaining()

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    _job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="commit-view-load",
            label=f"{modal.target_label}: commit view",
            local_mutation=False,
            repo_keys=(str(modal.target_path),),
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        for load_id in load_ids:
            state.view_loads.fail(load_id, _job.message)


def kick_off_add_tag(
    state: State,
    target_label: str,
    target_path: Path,
    target_repo: Optional[Repo],
    target_parent: Optional[Repo],
    name: str,
    sha: str,
) -> None:
    """Create a lightweight tag pointing at `sha`, and push it iff
    the commit is already reachable from some `origin/*` ref.

    Why conditional push: `git push origin <tag>` happily ships
    orphan commit objects along with the tag ref when the commit
    isn't on origin, leaving the new commit reachable on origin
    only via the tag (not via any branch). That surprises the user
    — their tag-triggered release workflow fires on a commit that
    isn't on master. So if the commit isn't on origin yet, we
    create the tag locally only and surface a warn task pointing
    at the missing branch push.

    Refuses unsafe name / sha (defence-in-depth). Cardinal-rule
    safe: tags only add refs, never rewrite history; the push is a
    plain `push origin <tag>` with no `--force`."""
    target_child = _find_child_at(target_parent, target_path)
    # Same claim semantics as kick_off_action — a tag write is a fast
    # ref-only op, but it still mutates `.git/` and would race a
    # concurrent refresh on the row's flags.
    claim = WorkerClaim(
        state,
        repo=target_repo,
        child=target_child,
        acquire_repo=target_repo is not None,
        acquire_child=target_child is not None,
    )
    try:
        claim.__enter__()
    except RuntimeError:
        t = state.tasks.add(f"{target_label}: skipped")
        state.tasks.update(t, "warn", "refresh in progress — try again")
        return

    def worker(job: Job) -> None:
        def finish_failed(message: str) -> None:
            state.job_registry.finish(job, JobStatus.FAIL, message)

        def finish_warn(message: str) -> None:
            state.job_registry.finish(job, JobStatus.WARN, message)

        try:
            t = state.tasks.add(f"{target_label}: tag {name}")
            if not is_safe_ref_arg(name):
                msg = "unsafe tag name"
                state.tasks.update(t, "fail", msg)
                finish_failed(msg)
                return
            if not sha or sha.startswith("-"):
                msg = "unsafe sha"
                state.tasks.update(t, "fail", msg)
                finish_failed(msg)
                return

            # 1) Create the tag locally.
            rc, _, err = git(target_path, ["tag", name, sha])
            if rc != 0:
                msg = first_line(err) or "git tag failed"
                state.tasks.update(t, "fail", msg)
                finish_failed(msg)
                return

            # 2) Check whether the commit is reachable from any
            # `refs/remotes/origin/*` ref. `for-each-ref --contains`
            # walks the named refs and returns those whose tip is a
            # descendant (or equal) of `sha`. Empty output means
            # "no origin ref reaches this commit yet" — push the
            # branch first, otherwise we'd be carrying the commit
            # to origin via the tag.
            rc, out, _ = git(
                target_path,
                ["for-each-ref", "--contains", sha, "--format=%(refname)", "refs/remotes/origin/"],
            )
            on_origin = rc == 0 and bool(out.strip())
            if not on_origin:
                msg = (
                    "tagged locally — commit not on origin yet; "
                    "push the branch first, then re-add the tag"
                )
                state.tasks.update(t, "warn", msg)
                finish_warn(msg)
                return

            # 3) Push the tag — safe ref-only operation since the
            # commit objects it points at are already on origin.
            rc, _, err = git(target_path, ["push", "origin", name])
            if rc != 0:
                msg = f"push: {first_line(err) or 'failed'}"
                state.tasks.update(t, "fail", msg)
                finish_failed(msg)
                return
            state.tasks.update(t, "ok")
        finally:
            claim.__exit__(None, None, None)

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    repo_keys = (str(target_repo.path),) if target_repo is not None else ()
    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="tag",
            label=f"{target_label}: tag {name}",
            local_mutation=True,
            repo_keys=repo_keys,
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        t = state.tasks.add(f"{target_label}: tag {name}")
        state.tasks.update(t, "fail", first_line(job.message))
        claim.__exit__(None, None, None)


def kick_off_clone(
    state: State, url: str, dest: Path, branch: str, recurse_submodules: bool, on_done=None
) -> None:
    """Run `git clone` in a daemon thread, publishing progress to the
    sidebar. `on_done` is called with `(ok, message)` once the clone
    settles, on the worker thread — caller wires it up to refresh the
    workspace's repo list and close the modal."""
    from .git_ops import clone_repo

    label = dest.name or "clone"
    t = state.tasks.add(f"{label}: clone")

    def worker(job: Job) -> None:
        ok, msg = clone_repo(url, dest, branch=branch, recurse_submodules=recurse_submodules)
        state.tasks.update(t, "ok" if ok else "fail", msg)
        if not ok:
            state.job_registry.finish(job, JobStatus.FAIL, msg)
        if on_done is not None:
            try:
                on_done(ok, msg)
            except Exception:  # noqa: BLE001
                # Caller-supplied callback is best-effort; never let a
                # bad sink (e.g. a UI hook that races a teardown) crash
                # the daemon thread. Failure here is invisible to the
                # main loop, but the task row already records the real
                # ok/fail outcome a few lines above.
                pass

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="clone",
            label=t.label,
            local_mutation=True,
            repo_keys=(str(dest),),
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        message = first_line(job.message)
        state.tasks.update(t, "fail", message)
        if on_done is not None:
            try:
                on_done(False, message)
            except Exception:  # noqa: BLE001
                pass


# ---------- Async commit-message suggestion -------------------------------


def _suggest_into_repo(state: State, repo: Repo) -> None:
    state.store.set_repo_suggesting(repo, True)
    try:
        result = suggest_commit_message(
            repo,
            max_added=state.suggest_added,
            max_updated=state.suggest_updated,
            max_deleted=state.suggest_deleted,
            auto_stage=state.auto_stage,
        )
        if result and not repo_row_state(state, repo).busy:
            state.store.set_row_message(repo, result)
    finally:
        state.store.set_repo_suggesting(repo, False)


def _suggest_into_child(state: State, child: ChildRef) -> None:
    state.store.set_child_suggesting(child, True)
    try:
        result = suggest_commit_message_at(
            child.nested_path,
            max_added=state.suggest_added,
            max_updated=state.suggest_updated,
            max_deleted=state.suggest_deleted,
            auto_stage=state.auto_stage,
        )
        if result and not child_row_state(state, child).busy:
            state.store.set_row_message(child, result)
    finally:
        state.store.set_child_suggesting(child, False)


def kick_off_suggest_for(state: State, target) -> None:
    """Run a single suggestion in a background thread; UI shows a spinner
    from store-owned row suggestion state."""
    if isinstance(target, Repo):
        repo = target
        if repo_row_state(state, repo).busy:
            return
        if state.store.repo_suggesting(repo):
            return

        def worker(_job: Job) -> None:
            _suggest_into_repo(state, repo)

        _job, thread = submit_job(
            state.job_registry,
            JobSpec(
                kind="suggest",
                label=f"{repo.rel}: suggest",
                local_mutation=False,
                repo_keys=(str(repo.path),),
            ),
            worker,
        )
        if thread is None:
            state.store.set_repo_suggesting(repo, False)
    else:  # ChildRef
        child = target
        if child_row_state(state, child).busy:
            return
        if state.store.child_suggesting(child):
            return

        def worker(_job: Job) -> None:
            _suggest_into_child(state, child)

        _job, thread = submit_job(
            state.job_registry,
            JobSpec(
                kind="suggest",
                label=f"{child.repo.rel}: suggest",
                local_mutation=False,
                child_keys=(str(child.nested_path),),
            ),
            worker,
        )
        if thread is None:
            state.store.set_child_suggesting(child, False)


def kick_off_bulk_suggest(state: State) -> None:
    """For every dirty row with an empty message, kick off a background
    suggestion. Each row animates independently."""
    for repo in active_workspace_repo_rows(state):
        row_state = repo_row_state(state, repo)
        if (
            row_state.dirty
            and not row_state.message.strip()
            and not state.store.repo_suggesting(repo)
            and not row_state.busy
        ):
            kick_off_suggest_for(state, repo)
    for _, child in active_workspace_child_rows(state):
        status = state.store.child_status(child)
        row_state = child_row_state(state, child)
        if (
            status is not None
            and status.kind == "submodule"
            and row_state.dirty
            and not row_state.message.strip()
            and not state.store.child_suggesting(child)
            and not row_state.busy
        ):
            kick_off_suggest_for(state, child)


# ---------- Commit pipelines ----------------------------------------------


def _apply_staging_plan(target_path: Path, staged_paths: "dict[str, bool]") -> Tuple[bool, str]:
    """Bring the index in line with the user's per-file checkbox state.
    `git add -A` for paths checked True (so deletions land too — plain
    `git add` skips removed files); `git restore --staged` for paths
    checked False. Stale entries — paths the user checked in the
    review pane but that have since gone away (e.g. an external
    `git rm` removed the file from both working tree and index) —
    are filtered out by intersecting with current `git status` so
    they don't trigger `pathspec did not match any files`. Returns
    (ok, error_msg)."""
    from .git_ops import _iter_porcelain_z_entries  # local: avoid circ

    if not staged_paths:
        return True, ""
    rc, status_out, _ = git(target_path, ["status", "--porcelain=v1", "-z"])
    # Map of path → porcelain XY code so we can tell "already fully
    # staged" (X non-space, Y space) apart from "has unstaged work
    # left to capture". Empty when the status read fails — we play
    # it safe and skip everything in that case.
    current_status: "dict[str, str]" = {}
    if rc == 0:
        for xy, p in _iter_porcelain_z_entries(status_out):
            current_status[p] = xy

    def _needs_add(xy: str) -> bool:
        """True when there's anything for `git add -A` to do for the
        path. `X != ' '` and `Y == ' '` means the change is already
        staged — a re-add is a no-op for modifications and an
        outright error ("pathspec did not match") for staged
        deletions, since the path is gone from both the working
        tree and the index. Skipping these keeps the staging step
        from blowing up on already-correct rows."""
        return not (xy[0] != " " and xy[1] == " ")

    to_stage = sorted(
        p
        for p, on in staged_paths.items()
        if on and p in current_status and _needs_add(current_status[p])
    )
    to_unstage = sorted(p for p, on in staged_paths.items() if not on and p in current_status)
    if to_unstage:
        rc, _, err = git(target_path, ["restore", "--staged", "--"] + to_unstage)
        if rc != 0:
            return False, first_line(err)
    if to_stage:
        # `-A` so a path the user checked that's been deleted from
        # the working tree (` D` in porcelain) still gets its
        # deletion staged — plain `git add` skips removed files.
        rc, _, err = git(target_path, ["add", "-A", "--"] + to_stage)
        if rc != 0:
            return False, first_line(err)
    return True, ""


def kick_off_review_suggest(state: State, block: ReviewBlock) -> None:
    """Spawn a daemon worker that re-runs commit-message suggestion for
    a review block, scoped to the files the user has currently checked
    in the right pane. Result is written to BOTH the review draft
    (so the review screen shows it immediately) and the underlying
    repo / child's message (so backing out of the review preserves it,
    matching the main-screen suggest semantics)."""
    draft = state.review_drafts.get_or_create(block.draft_id)
    if draft.suggesting or block.merging:
        return
    state.review_drafts.set_suggesting(block.draft_id, True)

    def worker(_job: Job) -> None:
        try:
            staged_paths = state.review_drafts.snapshot_staged(block.draft_id)
            paths = [p for p, on in staged_paths.items() if on]
            if not paths:
                return
            result = suggest_commit_message_for_paths(
                block.target_path,
                paths,
                max_added=state.suggest_added,
                max_updated=state.suggest_updated,
                max_deleted=state.suggest_deleted,
            )
            if not result:
                return
            state.review_drafts.set_message(block.draft_id, result)
            if block.target_repo is not None:
                state.store.set_row_message(block.target_repo, result)
            elif block.target_child is not None:
                state.store.set_row_message(block.target_child, result)
        finally:
            state.review_drafts.set_suggesting(block.draft_id, False)

    repo_keys = (str(block.target_repo.path),) if block.target_repo is not None else ()
    child_keys = (str(block.target_child.nested_path),) if block.target_child is not None else ()
    _job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="review-suggest",
            label=f"{block.label}: suggest",
            local_mutation=False,
            repo_keys=repo_keys,
            child_keys=child_keys,
        ),
        worker,
    )
    if thread is None:
        state.review_drafts.set_suggesting(block.draft_id, False)


def _repo_has_lfs_tracked_files(path: Path) -> bool:
    rc, out, _ = git(path, ["lfs", "ls-files"], timeout=10)
    return rc == 0 and bool(out.strip())


def _start_lfs_upload_task(tasks: Tasks, push_task: Task, path: Path) -> Optional[Task]:
    if not _repo_has_lfs_tracked_files(path):
        return None
    return tasks.add("  ↳ uploading lfs objects", parent=push_task)


def _finish_lfs_upload_task(
        tasks: Tasks,
        task: Optional[Task],
        status: str,
        message: str = "") -> None:
    if task is None:
        return
    tasks.update(task, status, message)


def commit_worker(
    state: State,
    repo: Repo,
    msg: str,
    lfs_cands: List[LFSCandidate],
    staged_paths: Optional["dict[str, bool]"] = None,
    amend: bool = False,
    track_workflow: Optional["dict[str, bool]"] = None,
    then_run_after_push: str = "",
    then_run_params_after_push: Optional["dict[str, str]"] = None,
    then_run_after_workflow: Optional["dict[str, str]"] = None,
    then_run_params_after_workflow: Optional["dict[str, dict[str, str]]"] = None,
    push: Optional[bool] = None,
    task_bridge: Optional[JobTaskBridge] = None,
    cancel_event: "Optional[threading.Event]" = None,
    cancel_job_id: Optional[int] = None,
    claim_mutation: bool = True,
) -> None:
    """Run the full stage / commit / push / sync pipeline for one repo,
    publishing each step into the sidebar. After a successful push, kicks
    off GitHub Actions tracking for any workflows the user opted in to on
    the review screen.

    `staged_paths` (when provided) overrides the auto_stage default —
    only paths checked True end up in the index, anything checked
    False is unstaged before commit. None means "fall back to legacy
    auto_stage semantics" for callers that haven't been ported yet.

    `amend=True` swaps the commit step to `git commit --amend -m msg`,
    folding any staged changes (and the new message) into the latest
    local commit. The review-pane toolbar gates this on `ahead > 0`
    so a published commit never gets rewritten — but the worker
    trusts the caller and doesn't re-validate.

    `track_workflow` / `then_run_after_push` / `then_run_params_after_push`
    are snapshots captured at queue time in `kick_off_workers`. Per the
    spec "once review is accepted, then-runs are forgotten", the Repo's
    own fields are cleared there; this worker reads from the snapshot
    so a re-opened review screen sees an empty state immediately,
    regardless of how far along this worker is in the pipeline. None
    defaults preserve compatibility for any out-of-tree caller.

    `push` is the per-commit decision from the review screen's push
    toggle. None falls back to the workspace's `auto_push` setting, so
    callers that don't pass it behave exactly as before."""
    name = state.task_repo_label(repo)
    # Early-visibility task so the user sees the panel react the
    # moment review is accepted. Without this, the first per-step
    # task (stage / commit / push / pull-on-move) can be delayed
    # several seconds by the pre-stage pull (network +
    # `--recurse-submodules=on-demand`) and the gesture looks dead.
    # Also replaces the legacy `<name>: failed` fallback row — same
    # signal carried by this task's status on exception.
    tasks = task_bridge or JobTaskBridge(state.tasks)
    pipeline_task = tasks.add(f"{name}: working")
    # Cancel signal — the task-detail modal requests cancellation on
    # the owning job, and commit-batch passes that job's event here.
    # `git_cancellable` polls it during long network calls.
    active_cancel_event = cancel_event or threading.Event()
    # WorkerClaim carries the state-owned mutation lease read by refresh paths.
    # The store refresh mutex is acquired by `kick_off_workers`; this claim
    # only unifies the lease and cancel signal lifetime for the worker.
    with WorkerClaim(
        state,
        repo=repo,
        task=pipeline_task,
        cancel_job_id=cancel_job_id,
        mark_repo=False,
        claim_mutation=claim_mutation,
    ):
        try:
            _commit_worker_inner(
                state,
                repo,
                msg,
                lfs_cands,
                staged_paths,
                amend,
                track_workflow,
                then_run_after_push,
                then_run_params_after_push,
                then_run_after_workflow,
                then_run_params_after_workflow,
                push=push,
                cancel_event=active_cancel_event,
                cancel_job_id=cancel_job_id,
                task_bridge=tasks,
            )
            if active_cancel_event.is_set():
                tasks.update(pipeline_task, "warn", "cancelled")
            else:
                tasks.update(pipeline_task, "ok", "")
        except Exception as e:
            tasks.update(pipeline_task, "fail", first_line(str(e)))


def _commit_worker_inner(
    state: State,
    repo: Repo,
    msg: str,
    lfs_cands: List[LFSCandidate],
    staged_paths: Optional["dict[str, bool]"] = None,
    amend: bool = False,
    track_workflow: Optional["dict[str, bool]"] = None,
    then_run_after_push: str = "",
    then_run_params_after_push: Optional["dict[str, str]"] = None,
    then_run_after_workflow: Optional["dict[str, str]"] = None,
    then_run_params_after_workflow: Optional["dict[str, dict[str, str]]"] = None,
    push: Optional[bool] = None,
    cancel_event: "Optional[threading.Event]" = None,
    cancel_job_id: Optional[int] = None,
    task_bridge: Optional[JobTaskBridge] = None,
) -> None:
    auto_stage = state.auto_stage
    # `push` is the review screen's per-commit toggle; None means "use
    # the workspace default" for callers that don't set it.
    auto_push = state.auto_push if push is None else push
    tasks = task_bridge or JobTaskBridge(state.tasks)
    name = state.task_repo_label(repo)

    # Phase 1 — LFS tracking for any toggled candidates.
    for cand in lfs_cands:
        if not cand.track:
            continue
        t = tasks.add(f"{name}: lfs track {Path(cand.path).name}")
        ok, msg_ret = apply_lfs_tracking(cand)
        tasks.update(t, "ok" if ok else "fail", msg_ret)

    # Phase 2 — stage / commit / push / sync.
    if repo.merging:
        t = tasks.add(f"{name}: skipped")
        tasks.update(t, "warn", "merge in progress")
        return

    # Detached-HEAD pre-flight. Committing on a detached HEAD silently
    # creates an orphan commit; pushing it then tries `git push
    # --set-upstream origin "(detached)"` (the sentinel string from
    # refresh_repo) and fails with `error: src refspec (detached) does
    # not match`. Before the cardinal-rule rewrite, this code just
    # refused; now it tries to recover safely first by asking the user
    # via the DetachedRecoveryPrompt modal. On confirm + a successful
    # FF, HEAD lands on the resolved default branch and the pipeline
    # continues with that branch as the new commit/push target.
    rc, branch_out, _ = git(repo.path, ["branch", "--show-current"])
    if rc != 0 or not branch_out.strip():
        recovered, rmsg = _attempt_detached_recovery(state, repo.path, name)
        if not recovered:
            t = tasks.add(f"{name}: cannot commit")
            tasks.update(t, "fail", "detached HEAD — " + (rmsg or "user cancelled"))
            return
        t = tasks.add(f"{name}: recovered detached HEAD")
        tasks.update(t, "ok", "branch fast-forwarded to HEAD")

    # Integrate upstream before staging — once we have a local commit a
    # strict FF may refuse; try merge pull when needed (never rebase).
    # Only surface a task when HEAD actually moved or the pull itself
    # fails.
    if repo.upstream:
        ok_pull = _pull_prefer_ff_then_merge(
            repo.path, tasks, name, allow_merge_fallback=True, cancel_event=cancel_event
        )
        if not ok_pull:
            return

    # Staging: per-file plan when the review screen handed us a
    # staged_paths dict (the new path); fall back to legacy auto_stage
    # semantics when the dict is None (older callers / tests).
    if staged_paths is not None:
        if any(staged_paths.values()):
            t = tasks.add(f"{name}: stage")
            ok, stage_err = _apply_staging_plan(repo.path, staged_paths)
            if not ok:
                tasks.update(t, "fail", stage_err)
                return
            tasks.update(t, "ok")
    elif auto_stage:
        t = tasks.add(f"{name}: stage all")
        ok, stage_err = safe_stage_all(repo.path)
        if not ok:
            tasks.update(t, "fail", stage_err)
            return
        tasks.update(t, "ok")

    rc, _, _ = git(repo.path, ["diff", "--cached", "--quiet"])
    nothing_staged = rc == 0
    # `--amend -m msg` is happy to update just the message even with
    # nothing staged, so we let amend through; fresh commits still
    # bail when there's nothing to land.
    if nothing_staged and not amend:
        t = tasks.add(f"{name}: skipped")
        tasks.update(t, "warn", "nothing staged")
        return

    if amend:
        t = tasks.add(f"{name}: commit --amend")
        rc, _, err = git(repo.path, ["commit", "--amend", "-m", msg])
    else:
        t = tasks.add(f"{name}: commit")
        rc, _, err = git(repo.path, ["commit", "-m", msg])
    if rc != 0:
        tasks.update(t, "fail", first_line(err))
        return
    tasks.update(t, "ok")

    if not auto_push:
        return

    push_task = tasks.add(f"{name}: push")
    with WorkerClaim(
            state,
            repo=repo,
            task=push_task,
            cancel_job_id=cancel_job_id,
            mark_repo=True,
            claim_mutation=False):
        lfs_task = _start_lfs_upload_task(tasks, push_task, repo.path)
        rc_u, _, _ = git(
            repo.path,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        )
        try:
            if rc_u == 0:
                rc, _, err = git_cancellable(
                    repo.path,
                    ["push"],
                    cancel_event=cancel_event,
                    timeout=USER_PUSH_TIMEOUT_SECONDS,
                )
            else:
                rc_b, b_out, _ = git(repo.path, ["branch", "--show-current"])
                cur_branch = b_out.strip() if rc_b == 0 else ""
                if cur_branch:
                    if not is_safe_ref_arg(cur_branch):
                        _finish_lfs_upload_task(
                            tasks, lfs_task, "fail", "unsafe current branch name")
                        tasks.update(push_task, "fail", "unsafe current branch name")
                        return
                    rc, _, err = git_cancellable(
                        repo.path,
                        ["push", "--set-upstream", "origin", cur_branch],
                        cancel_event=cancel_event,
                        timeout=USER_PUSH_TIMEOUT_SECONDS,
                    )
                else:
                    rc, err = 1, "no current branch"
        except Exception as e:  # noqa: BLE001
            message = first_line(str(e))
            _finish_lfs_upload_task(tasks, lfs_task, "fail", message)
            tasks.update(push_task, "fail", message)
            return
    if rc != 0:
        # rc 130 = cancelled; tag the row distinctly so the user sees
        # the cancel landed rather than a generic push failure.
        if rc == 130:
            _finish_lfs_upload_task(tasks, lfs_task, "warn", "cancelled")
            tasks.update(push_task, "warn", "cancelled")
        else:
            message = first_line(err)
            _finish_lfs_upload_task(tasks, lfs_task, "fail", message)
            tasks.update(push_task, "fail", message)
        return
    _finish_lfs_upload_task(tasks, lfs_task, "ok")
    tasks.update(push_task, "ok")

    # Capture the freshly-pushed commit so the actions tracker can match
    # the run by sha. Pulled after push so we get the actual head, not a
    # cached snapshot from before the commit.
    rc_h, head_out, _ = git(repo.path, ["rev-parse", "HEAD"])
    pushed_sha = head_out.strip() if rc_h == 0 else ""
    # Snapshot values captured at queue time drive these. Legacy direct callers
    # that do not pass a snapshot consume the store-owned row intent once.
    fallback_intent = (
        WorkflowIntentSnapshot()
        if track_workflow is not None
        else state.store.take_repo_workflow_intent(repo)
    )
    track_wf_map = (
        track_workflow
        if track_workflow is not None
        else fallback_intent.track_workflow
    )
    tracked = [name for name, on in track_wf_map.items() if on]
    if tracked and pushed_sha:
        kick_off_post_push_run_tracking(
            state,
            repo,
            repo.branch,
            pushed_sha,
            tracked,
            then_run_after_workflow=then_run_after_workflow,
            then_run_params_after_workflow=then_run_params_after_workflow,
        )
    # "Then run after push" — fired once the push itself completes,
    # regardless of any tracked workflow runs. Two shapes:
    #   * a workflow name → dispatch the manual workflow
    #   * the "__add_tag__" sentinel → create a lightweight tag at
    #     the just-pushed sha. Per-action parameter buffers live in
    #     `then_run_params_after_push` (today only "tag", but the
    #     same dict will hold workflow_dispatch inputs in the
    #     future). Like `track_workflow` above, the snapshot
    #     supersedes store fallback intent when provided.
    if then_run_after_push or then_run_params_after_push is not None:
        after_push_target = then_run_after_push
        after_push_params = dict(then_run_params_after_push or {})
    else:
        after_push_target = fallback_intent.then_run_after_push
        after_push_params = dict(fallback_intent.then_run_params_after_push)
    if after_push_target == "__add_tag__":
        tag_name = after_push_params.get("tag", "").strip()
        tag_label = state.task_repo_label(repo)
        t_tag = tasks.add(f"{tag_label}: tag {tag_name or '(empty)'}")
        _create_and_push_tag(tasks, t_tag, repo.path, tag_name, pushed_sha)
    elif after_push_target:
        # `after_push_params` carries the buffered values for
        # `workflow_dispatch.inputs` — non-empty entries get
        # forwarded as `-F name=value` by dispatch_workflow.
        kick_off_manual_dispatch(
            state, repo, after_push_target, repo.branch, inputs=after_push_params
        )

    for sib_repo, sib_path in repo.siblings:
        t = tasks.add(f"  ↳ sync {state.task_repo_label(sib_repo)}", parent=push_task)
        child_claim = WorkerClaim(
            state, child=_find_child_at(sib_repo, sib_path), acquire_child=True, child_timeout=5.0
        )
        try:
            with child_claim:
                ok, sync_msg = _sync_sibling_safe(sib_path, repo.branch, parent_path=sib_repo.path)
        except RuntimeError:
            tasks.update(t, "warn", "skipped: child refresh lock held by another op")
            continue
        tasks.update(t, "ok" if ok else "fail", sync_msg)

    # Same parent-propagation as the canonical-sync path: once the
    # sibling submodule checkouts inside each parent advance to the
    # new HEAD, the parent's gitlink to this repo is stale. If the
    # only dirty change on the parent is that gitlink, auto-stage +
    # commit + push (cascading upward through any grandparents that
    # also gain only-submodule dirt). Gated on the same toggle the
    # canonical-sync path uses.
    if state.auto_push_submodule_parent and repo.siblings:
        try:
            _cascade_propagate_to_parents(state, [repo])
        except Exception as e:  # noqa: BLE001
            # Propagation failures shouldn't poison the push pipeline
            # — the push itself already landed. Surface as a warn
            # task and move on.
            t = tasks.add("  ↳ propagate to parents", parent=push_task)
            tasks.update(t, "fail", first_line(str(e)))


def commit_worker_for_child(
    state: State,
    parent: Repo,
    ref: ChildRef,
    msg: str,
    staged_paths: Optional["dict[str, bool]"] = None,
    amend: bool = False,
    track_workflow: Optional["dict[str, bool]"] = None,
    then_run_after_push: str = "",
    then_run_params_after_push: Optional["dict[str, str]"] = None,
    then_run_after_workflow: Optional["dict[str, str]"] = None,
    then_run_params_after_workflow: Optional["dict[str, dict[str, str]]"] = None,
    push: Optional[bool] = None,
    task_bridge: Optional[JobTaskBridge] = None,
    cancel_event: "Optional[threading.Event]" = None,
    cancel_job_id: Optional[int] = None,
    claim_mutation: bool = True,
) -> None:
    """Run the stage / commit / push pipeline against `ref.nested_path` —
    the working tree of a nested submodule checkout inside `parent`.

    After a successful push, sync every other place this submodule is
    checked out (the canonical top-level repo + every other parent's nested
    copy) so they all advance to the new commit. Workflow tracking is
    keyed off the canonical repo's track_workflow map, same as a top-level
    push.

    `staged_paths` mirrors `commit_worker` — review-screen per-file
    checkbox state, or None to fall back to legacy auto_stage. `amend`
    swaps the commit step to `git commit --amend -m msg` (same gating
    rule as the top-level worker — caller is responsible for ensuring
    the latest commit hasn't been pushed).

    `track_workflow` / `then_run_after_push` / `then_run_params_after_push`
    are snapshots captured + cleared from `ref.repo` at queue time in
    `kick_off_workers`. See `commit_worker` for the rationale (review-
    screen state appears empty as soon as the user accepts)."""
    name = f"{state.task_repo_label(ref.repo)} (in {state.task_repo_label(parent)})"
    # Early-visibility task — see `commit_worker` for the rationale.
    tasks = task_bridge or JobTaskBridge(state.tasks)
    pipeline_task = tasks.add(f"{name}: working")
    active_cancel_event = cancel_event or threading.Event()
    # WorkerClaim registers explicit mutation ownership for the parent and
    # specific child ref without taking over their existing locks.
    with WorkerClaim(
        state,
        repo=parent,
        child=ref,
        task=pipeline_task,
        cancel_job_id=cancel_job_id,
        mark_repo=False,
        mark_child=False,
        claim_mutation=claim_mutation,
    ):
        try:
            _commit_worker_for_child_inner(
                state,
                parent,
                ref,
                msg,
                staged_paths,
                amend,
                track_workflow,
                then_run_after_push,
                then_run_params_after_push,
                then_run_after_workflow,
                then_run_params_after_workflow,
                push=push,
                cancel_event=active_cancel_event,
                cancel_job_id=cancel_job_id,
                task_bridge=tasks,
            )
            if active_cancel_event.is_set():
                tasks.update(pipeline_task, "warn", "cancelled")
            else:
                tasks.update(pipeline_task, "ok", "")
        except Exception as e:
            tasks.update(pipeline_task, "fail", first_line(str(e)))


def _commit_worker_for_child_inner(
    state: State,
    parent: Repo,
    ref: ChildRef,
    msg: str,
    staged_paths: Optional["dict[str, bool]"] = None,
    amend: bool = False,
    track_workflow: Optional["dict[str, bool]"] = None,
    then_run_after_push: str = "",
    then_run_params_after_push: Optional["dict[str, str]"] = None,
    then_run_after_workflow: Optional["dict[str, str]"] = None,
    then_run_params_after_workflow: Optional["dict[str, dict[str, str]]"] = None,
    push: Optional[bool] = None,
    cancel_event: "Optional[threading.Event]" = None,
    cancel_job_id: Optional[int] = None,
    task_bridge: Optional[JobTaskBridge] = None,
) -> None:
    auto_stage = state.auto_stage
    # `push` is the review screen's per-commit toggle; None means "use
    # the workspace default" for callers that don't set it.
    auto_push = state.auto_push if push is None else push
    tasks = task_bridge or JobTaskBridge(state.tasks)
    name = f"{state.task_repo_label(ref.repo)} (in {state.task_repo_label(parent)})"

    rc, out, _ = git(ref.nested_path, ["branch", "--show-current"])
    nested_branch = out.strip() if rc == 0 else ""
    if not nested_branch:
        # Try cardinal-rule-safe recovery (modal asks the user). On
        # success the nested checkout lands on the resolved default
        # branch and the rest of the pipeline runs against that branch
        # as the commit/push target.
        recovered, rmsg = _attempt_detached_recovery(state, ref.nested_path, name)
        if not recovered:
            t = tasks.add(f"{name}: cannot commit")
            tasks.update(t, "fail", "detached HEAD — " + (rmsg or "user cancelled"))
            return
        t = tasks.add(f"{name}: recovered detached HEAD")
        tasks.update(t, "ok", "branch fast-forwarded to HEAD")
        # Re-read the now-current branch so the rest of the pipeline
        # uses the recovered branch as its push refspec.
        rc, out, _ = git(ref.nested_path, ["branch", "--show-current"])
        nested_branch = out.strip() if rc == 0 else ""
        if not nested_branch:
            t = tasks.add(f"{name}: cannot commit")
            tasks.update(t, "fail", "recovery succeeded but branch lookup failed")
            return

    if staged_paths is not None:
        if any(staged_paths.values()):
            t = tasks.add(f"{name}: stage")
            ok, stage_err = _apply_staging_plan(ref.nested_path, staged_paths)
            if not ok:
                tasks.update(t, "fail", stage_err)
                return
            tasks.update(t, "ok")
    elif auto_stage:
        t = tasks.add(f"{name}: stage all")
        ok, stage_err = safe_stage_all(ref.nested_path)
        if not ok:
            tasks.update(t, "fail", stage_err)
            return
        tasks.update(t, "ok")

    rc, _, _ = git(ref.nested_path, ["diff", "--cached", "--quiet"])
    nothing_staged = rc == 0
    if nothing_staged and not amend:
        t = tasks.add(f"{name}: skipped")
        tasks.update(t, "warn", "nothing staged")
        return

    if amend:
        t = tasks.add(f"{name}: commit --amend")
        rc, _, err = git(ref.nested_path, ["commit", "--amend", "-m", msg])
    else:
        t = tasks.add(f"{name}: commit")
        rc, _, err = git(ref.nested_path, ["commit", "-m", msg])
    if rc != 0:
        tasks.update(t, "fail", first_line(err))
        return
    tasks.update(t, "ok")

    if not auto_push:
        return

    rc, up_out, _ = git(
        ref.nested_path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    )
    has_upstream = rc == 0 and up_out.strip()

    push_task = tasks.add(f"{name}: push")
    with WorkerClaim(
            state,
            child=ref,
            task=push_task,
            cancel_job_id=cancel_job_id,
            mark_child=True,
            claim_mutation=False):
        lfs_task = _start_lfs_upload_task(tasks, push_task, ref.nested_path)
        try:
            if has_upstream:
                rc, _, err = git_cancellable(
                    ref.nested_path,
                    ["push"],
                    cancel_event=cancel_event,
                    timeout=USER_PUSH_TIMEOUT_SECONDS,
                )
            else:
                if not is_safe_ref_arg(nested_branch):
                    _finish_lfs_upload_task(
                        tasks, lfs_task, "fail", "unsafe current branch name")
                    tasks.update(push_task, "fail", "unsafe current branch name")
                    return
                rc, _, err = git_cancellable(
                    ref.nested_path,
                    ["push", "--set-upstream", "origin", nested_branch],
                    cancel_event=cancel_event,
                    timeout=USER_PUSH_TIMEOUT_SECONDS,
                )
        except Exception as e:  # noqa: BLE001
            message = first_line(str(e))
            _finish_lfs_upload_task(tasks, lfs_task, "fail", message)
            tasks.update(push_task, "fail", message)
            return
    if rc != 0:
        if rc == 130:
            _finish_lfs_upload_task(tasks, lfs_task, "warn", "cancelled")
            tasks.update(push_task, "warn", "cancelled")
        else:
            message = first_line(err)
            _finish_lfs_upload_task(tasks, lfs_task, "fail", message)
            tasks.update(push_task, "fail", message)
        return
    _finish_lfs_upload_task(tasks, lfs_task, "ok")
    tasks.update(push_task, "ok")

    rc_h, head_out, _ = git(ref.nested_path, ["rev-parse", "HEAD"])
    pushed_sha = head_out.strip() if rc_h == 0 else ""
    # Snapshot values captured at queue time drive these. Legacy direct callers
    # that do not pass a snapshot consume the canonical store intent once.
    fallback_intent = (
        WorkflowIntentSnapshot()
        if track_workflow is not None
        else state.store.take_repo_workflow_intent(ref.repo)
    )
    track_wf_map = (
        track_workflow
        if track_workflow is not None
        else fallback_intent.track_workflow
    )
    tracked = [n for n, on in track_wf_map.items() if on]
    if tracked and pushed_sha:
        kick_off_post_push_run_tracking(
            state,
            ref.repo,
            nested_branch,
            pushed_sha,
            tracked,
            then_run_after_workflow=then_run_after_workflow,
            then_run_params_after_workflow=then_run_params_after_workflow,
        )
    # "Then run after push" for the canonical — same semantics as
    # the top-level commit_worker version, including the
    # "__add_tag__" sentinel for creating a lightweight tag at the
    # pushed sha. Snapshot supersedes store fallback intent when provided.
    if then_run_after_push or then_run_params_after_push is not None:
        after_push_target = then_run_after_push
        after_push_params = dict(then_run_params_after_push or {})
    else:
        after_push_target = fallback_intent.then_run_after_push
        after_push_params = dict(fallback_intent.then_run_params_after_push)
    if after_push_target == "__add_tag__":
        tag_name = after_push_params.get("tag", "").strip()
        tag_label = f"{state.task_repo_label(ref.repo)} (in {state.task_repo_label(parent)})"
        t_tag = tasks.add(f"{tag_label}: tag {tag_name or '(empty)'}")
        _create_and_push_tag(tasks, t_tag, ref.nested_path, tag_name, pushed_sha)
    elif after_push_target:
        # `after_push_params` was popped above and holds buffered
        # `workflow_dispatch.inputs` values; forward as -F flags so
        # the dispatched run honours the user's review-screen edits.
        kick_off_manual_dispatch(
            state, ref.repo, after_push_target, nested_branch, inputs=after_push_params
        )

    # Build the post-push sync targets. For tracked canonicals we sync
    # the top-level checkout first; for synthetic canonicals there's no
    # workspace top-level, so we only fan out to the sibling parents.
    # Each target also carries an optional `(parent, sub_path)` pair
    # so the per-row spinner toggle has something to flag — the
    # top-level canonical's spinner is published through store-backed
    # row busy state; the in-parent submodule rows live on a ChildRef.
    targets: List[Tuple[str, Path, Optional[Tuple[Repo, Path]]]] = []
    ref_label = state.task_repo_label(ref.repo)
    if not ref.repo.synthetic:
        targets.append((f"top-level {ref_label}", ref.repo.path, None))
    for other_parent, other_path in ref.repo.siblings:
        if other_path == ref.nested_path:
            continue
        targets.append(
            (
                f"{ref_label} in {state.task_repo_label(other_parent)}",
                other_path,
                (other_parent, other_path),
            )
        )

    for label, target_path, child_pair in targets:
        t = tasks.add(f"  ↳ sync {label}", parent=push_task)
        if child_pair is None:
            claim = WorkerClaim(state, repo=ref.repo, acquire_repo=True, repo_timeout=5.0)
        else:
            claim = WorkerClaim(
                state,
                child=_find_child_at(child_pair[0], child_pair[1]),
                acquire_child=True,
                child_timeout=5.0,
            )
        try:
            with claim:
                parent_path = child_pair[0].path if child_pair is not None else None
                ok, sync_msg = _sync_sibling_safe(
                    target_path, nested_branch, parent_path=parent_path
                )
        except RuntimeError:
            tasks.update(t, "warn", "skipped: refresh lock held by another op")
            continue
        tasks.update(t, "ok" if ok else "fail", sync_msg)

    # Same parent-propagation as the canonical-sync path: each
    # parent containing this canonical (whether top-level or just
    # another nested copy) now has a stale gitlink to the new
    # commit. If the parent's only dirt is that gitlink, auto-bump
    # + push. `ref.repo.siblings` lists the parents; the propagation
    # helper skips the parent that owns this nested checkout when
    # its own gitlink isn't stale yet (it'll have been advanced as
    # part of the in-place commit above).
    if state.auto_push_submodule_parent and ref.repo.siblings:
        try:
            _cascade_propagate_to_parents(state, [ref.repo])
        except Exception as e:  # noqa: BLE001
            t = tasks.add("  ↳ propagate to parents", parent=push_task)
            tasks.update(t, "fail", first_line(str(e)))


def kick_off_workers(state: State, blocks: List[ReviewBlock]) -> None:
    """Launch one worker thread per repo / nested-child with a queued
    message and a supervisor thread that silently re-fetches repo state
    once everything finishes.

    `blocks` carries the review-screen-built per-target info (LFS
    candidates, per-file checkbox state). Each block's `target_repo`
    or `target_child` identifies which on-state row it belongs to;
    we look those up at dispatch time and feed them through to the
    matching worker.

    Repos and child refs that are already locked (refreshing=True from a
    concurrent kick_off_action) are skipped — their message is cleared so
    it does not re-appear, but we do not attempt to commit on top of an
    in-flight action. All repos/refs we do commit are locked synchronously
    before any thread is spawned, and unlocked by the supervisor after
    their individual refresh completes."""
    # Index blocks by their target so we can pull per-block info
    # (LFS cands, staged_paths) at dispatch time without re-walking.
    repo_blocks = {
        id(b.target_repo): b for b in blocks if b.target_repo is not None and b.target_child is None
    }
    child_blocks = {id(b.target_child): b for b in blocks if b.target_child is not None}

    # Per-repo plan now also carries a snapshot of the after-push
    # then-run state (workflow-tracking opt-ins, the `__add_tag__` /
    # workflow-dispatch target, the per-action params buffer). We
    # snapshot + clear from Repo HERE at queue time — not later
    # inside `commit_worker` — so the review screen's
    # `_then_run_current` reads (which look at Repo directly) see an
    # empty state as soon as the user has accepted the review. Without
    # this, re-opening the review while commit_worker was still
    # mid-pipeline (or via any of its early-return paths) would show
    # the previous gesture's tag/workflow still set.
    repo_plans: List[
        Tuple[
            Repo,
            str,
            List[LFSCandidate],
            "dict[str, bool]",
            bool,
            "dict[str, bool]",
            str,
            "dict[str, str]",
            "dict[str, str]",
            "dict[str, dict[str, str]]",
            bool,
        ]
    ] = []
    worker_claims: List[WorkerClaim] = []
    repo_rows = active_workspace_repo_rows(state)
    child_rows = active_workspace_child_rows(state)
    for repo in repo_rows:
        block = repo_blocks.get(id(repo))
        draft = state.review_drafts.get_or_create(block.draft_id) if block else None
        msg = (
            draft.message.strip()
            if draft is not None
            else state.store.row_message(repo).strip()
        )
        if not msg:
            continue
        state.store.set_row_message(repo, "")
        claim = WorkerClaim(state, repo=repo, acquire_repo=True)
        try:
            claim.__enter__()
        except RuntimeError:
            continue
        repo_cands = list(block.lfs_candidates) if block else []
        staged = (state.review_drafts.snapshot_staged(block.draft_id)
                  if block else {})
        amend = bool(draft.amend) if draft is not None else False
        # Per-commit push decision from the review screen's toggle;
        # fall back to the workspace default when there's no block.
        push = bool(draft.push) if draft is not None else state.auto_push
        if draft is not None:
            intent = _workflow_intent_from_draft(draft)
            state.review_drafts.clear_workflow_intent(block.draft_id)
        else:
            intent = state.store.take_repo_workflow_intent(repo)
        repo_plans.append(
            (
                repo,
                msg,
                repo_cands,
                staged,
                amend,
                dict(intent.track_workflow),
                intent.then_run_after_push,
                dict(intent.then_run_params_after_push),
                dict(intent.then_run_after_workflow),
                {
                    key: dict(value)
                    for key, value in intent.then_run_params_after_workflow.items()
                },
                push,
            )
        )
        worker_claims.append(claim)

    child_plans: List[
        Tuple[
            Repo,
            ChildRef,
            str,
            "dict[str, bool]",
            bool,
            "dict[str, bool]",
            str,
            "dict[str, str]",
            "dict[str, str]",
            "dict[str, dict[str, str]]",
            bool,
        ]
    ] = []
    for parent, ref in child_rows:
        status = state.store.child_status(ref)
        if status is None or status.kind != "submodule":
            continue
        block = child_blocks.get(id(ref))
        draft = state.review_drafts.get_or_create(
            block.draft_id) if block else None
        msg = (
            draft.message.strip()
            if draft is not None
            else state.store.row_message(ref).strip()
        )
        if not msg:
            continue
        state.store.set_row_message(ref, "")
        claim = WorkerClaim(state, child=ref, acquire_child=True)
        try:
            claim.__enter__()
        except RuntimeError:
            continue
        staged = (state.review_drafts.snapshot_staged(block.draft_id)
                  if block else {})
        amend = bool(draft.amend) if draft is not None else False
        push = bool(draft.push) if draft is not None else state.auto_push
        if draft is not None:
            intent = _workflow_intent_from_draft(draft)
            state.review_drafts.clear_workflow_intent(block.draft_id)
        else:
            intent = state.store.take_repo_workflow_intent(ref.repo)
        child_plans.append(
            (
                parent,
                ref,
                msg,
                staged,
                amend,
                dict(intent.track_workflow),
                intent.then_run_after_push,
                dict(intent.then_run_params_after_push),
                dict(intent.then_run_after_workflow),
                {
                    key: dict(value)
                    for key, value in intent.then_run_params_after_workflow.items()
                },
                push,
            )
        )
        worker_claims.append(claim)

    if not repo_plans and not child_plans:
        return

    # WorkerClaim captures the locked Repo / ChildRef objects directly
    # so the supervisor releases the exact instances we acquired,
    # regardless of what `state.repos` looks like by then.
    # Snapshot the workspace's repo + subtree list at queue time so
    # the supervisor's post-commit refresh + link_siblings operate on
    # the workspace where the commit happened, not whichever workspace
    # `state.repos` happens to point at when the join completes. Without
    # this, a user who switches workspaces mid-pipeline returns to a
    # cached snapshot frozen at switch-out time: dirty flags and HEAD
    # never advance, the commit message field appears repopulated, and
    # hitting Enter again would re-commit (and re-push) the same work.
    # Identity is preserved — these are the same Repo objects that the
    # cache lists hold, so mutations land on the right rows even when
    # the user is parked on a different workspace.
    snapshot_repos: List[Repo] = list(repo_rows)
    snapshot_subtrees = list(state.subtrees)

    job = state.job_registry.start(
        JobSpec(
            kind="commit-batch",
            label="commit workers",
            local_mutation=True,
            repo_keys=tuple(str(repo.path) for repo, *_ in repo_plans),
            child_keys=tuple(str(ref.nested_path) for _, ref, *_ in child_plans),
        )
    )
    task_bridge = JobTaskBridge(state.tasks, state.job_registry, job)

    def commit_thread_factory(target, args, daemon):
        thread = create_worker_thread(target, args, daemon)
        return thread

    worker_group = ThreadGroup(commit_thread_factory)

    def supervisor(_job: Job, *, max_refresh_workers: Optional[int] = None) -> None:
        try:
            worker_group.join_all()
            # Reconcile `snapshot_repos` (captured at queue time), not
            # `state.repos`, so a mid-pipeline workspace switch can't leave
            # the originally-committed repos showing stale pre-commit state
            # when the user navigates back. Reconciler failures are data and
            # must not prevent claim release in the finally below.
            reconcile_repos_bounded(
                snapshot_repos,
                snapshot_subtrees,
                refresh_fn=lambda repo: _refresh_repo_snapshot_into_state(
                    state, repo),
                link_fn=lambda link_repos, link_subtrees: _state_link_siblings(
                    state, link_repos, link_subtrees),
                max_workers=max_refresh_workers,
            )
            task_bridge.finish_failed_or_warned_job(state.job_registry, job)
        except Exception as e:  # noqa: BLE001
            message = first_line(str(e))
            t = task_bridge.add("commit workers")
            task_bridge.update(t, "fail", message)
            state.job_registry.finish(job, JobStatus.FAIL, message)
        finally:
            # Releases run in a finally so any earlier exception STILL
            # frees the locks. Without this, an exception in
            # refresh_repo / link_siblings strands every locked repo
            # and child ref. Iterates the captured refs directly so a
            # `state.repos` swap (workspace switch, fresh discovery)
            # between acquire and finally can't strand the lock.
            for claim in worker_claims:
                claim.__exit__(None, None, None)

    try:
        for (
            repo,
            msg,
            repo_cands,
            staged,
            amend,
            track_wf,
            then_push,
            then_push_params,
            then_workflow,
            then_workflow_params,
            push,
        ) in repo_plans:
            worker_group.start(
                commit_worker,
                (
                    state,
                    repo,
                    msg,
                    repo_cands,
                    staged,
                    amend,
                    track_wf,
                    then_push,
                    then_push_params,
                    then_workflow,
                    then_workflow_params,
                    push,
                    task_bridge,
                    job.cancel_event,
                    job.job_id,
                    False,
                ),
            )

        for (
            parent,
            ref,
            msg,
            staged,
            amend,
            track_wf,
            then_push,
            then_push_params,
            then_workflow,
            then_workflow_params,
            push,
        ) in child_plans:
            worker_group.start(
                commit_worker_for_child,
                (
                    state,
                    parent,
                    ref,
                    msg,
                    staged,
                    amend,
                    track_wf,
                    then_push,
                    then_push_params,
                    then_workflow,
                    then_workflow_params,
                    push,
                    task_bridge,
                    job.cancel_event,
                    job.job_id,
                    False,
                ),
            )
    except Exception as e:  # noqa: BLE001
        for claim in worker_claims[worker_group.started_count:]:
            claim.__exit__(None, None, None)
        t = task_bridge.add("commit workers")
        task_bridge.update(t, "fail", first_line(str(e)))
        state.job_registry.finish(job, JobStatus.FAIL, first_line(str(e)))
        if worker_group.started_count:
            supervisor(job, max_refresh_workers=1)
        return

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    supervisor_thread = start_job_thread(
        state.job_registry, job, supervisor, thread_factory=thread_factory
    )
    if supervisor_thread is None:
        message = first_line(job.message)
        t = task_bridge.add("commit supervisor")
        task_bridge.update(t, "fail", message)
        supervisor(job)


# ---------- Sibling sync (Ctrl+S) — alignment ------------------------------
#
# Smart-sync's job is to take every checkout of the same canonical
# submodule (top-level + every parent's nested clone) and bring their
# HEADs onto the same commit. Non-destructive throughout — any path
# that can't proceed safely is warn-skipped with a clear reason. The
# `align_heads` toggle in the top bar controls whether detached-HEAD
# checkouts are also pulled into line: off means "skip them with a
# warning", on means "pop a modal that asks the user which branch to
# push the winner's changes to, then sync everyone to that branch".
# In either mode the only git verbs used are fetch / merge (--ff-only,
# optionally a non-FF merge when aligning losers unless prevented by
# config) / checkout — git itself refuses on conflict, so we never
# overwrite uncommitted work or unique commits.


def _checkout_label(state: State, canonical: Repo, parent: Optional[Repo]) -> str:
    """Sidebar-friendly checkout label. Both repo names go through
    `task_repo_label` so a long display name doesn't crowd out the
    surrounding "align Foo: stage at … in …" task description."""
    canonical_label = state.task_repo_label(canonical)
    if parent is None:
        return f"top-level {canonical_label}"
    return f"{canonical_label} in {state.task_repo_label(parent)}"


def _probe_checkout_full(
    state: State, path: Path, parent: Optional[Repo], canonical: Repo
) -> SmartSyncCheckout:
    """Snapshot every field the alignment planner needs from one
    checkout. Read-only — no git mutations."""
    rc, out, _ = git(path, ["branch", "--show-current"])
    branch_raw = out.strip() if rc == 0 else ""
    branch = branch_raw or "(detached)"

    rc, out, _ = git(path, ["rev-parse", "HEAD"])
    head = out.strip() if rc == 0 else ""

    rc, out, _ = git(path, ["status", "--porcelain=v1"])
    dirty = rc == 0 and bool(out.strip())

    upstream: Optional[str] = None
    rc, out, _ = git(path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if rc == 0 and out.strip():
        upstream = out.strip()

    ahead = 0
    behind = 0
    if upstream:
        rc, out, _ = git(path, ["rev-list", "--count", "--left-right", f"{upstream}...HEAD"])
        if rc == 0:
            parts = out.split()
            if len(parts) == 2:
                try:
                    behind = int(parts[0])
                    ahead = int(parts[1])
                except ValueError:
                    pass

    sig = working_tree_signature(path) if dirty else ()
    sig_mt = signature_mtime(path, sig) if sig else 0.0

    return SmartSyncCheckout(
        canonical=canonical,
        parent=parent,
        path=path,
        branch=branch,
        label=_checkout_label(state, canonical, parent),
        head=head,
        dirty=dirty,
        ahead=ahead,
        behind=behind,
        upstream=upstream,
        signature=sig,
        sig_mtime=sig_mt,
    )


def _commit_dirty_winner(state: State, winner: SmartSyncCheckout, name: str) -> bool:
    """Stage all WT changes + commit with a suggested message. Pre-flight
    for push when the winner is dirty and `auto_stage` is on. The commit
    is local-only and reversible via `git reset --soft HEAD~1`, so this
    step is non-destructive even if the subsequent push fails."""
    t = state.tasks.add(f"  ↳ align {name}: stage at {winner.label}")
    ok, stage_err = safe_stage_all(winner.path)
    if not ok:
        state.tasks.update(t, "fail", stage_err)
        return False
    state.tasks.update(t, "ok")

    msg = (
        suggest_commit_message_at(
            winner.path,
            max_added=state.suggest_added,
            max_updated=state.suggest_updated,
            max_deleted=state.suggest_deleted,
            auto_stage=True,
        )
        or "consolidate working-tree changes"
    )

    t = state.tasks.add(f"  ↳ align {name}: commit at {winner.label}")
    rc, _, err = git(winner.path, ["commit", "-m", msg])
    if rc != 0:
        state.tasks.update(t, "fail", first_line(err))
        return False
    state.tasks.update(t, "ok", msg)
    return True


def _push_winner(state: State, winner: SmartSyncCheckout, branch: str, name: str) -> bool:
    """Push the winner's branch (with `--set-upstream` fallback for
    branches that don't yet have one). Plain `git push`, never forced."""
    t = state.tasks.add(f"  ↳ align {name}: push {winner.label}")
    if not is_safe_ref_arg(branch):
        state.tasks.update(t, "fail", "unsafe branch name")
        return False

    cancel_event = threading.Event()
    held_repo = winner.canonical if winner.parent is None else winner.parent
    held_child = _find_child_at(winner.parent, winner.path) if winner.parent is not None else None
    with WorkerClaim(
        state,
        repo=held_repo,
        child=held_child,
        task=t,
        mark_repo=False,
        mark_child=False,
    ):
        try:
            rc, _, err = git_cancellable(
                winner.path,
                ["push"],
                cancel_event=cancel_event,
                timeout=USER_PUSH_TIMEOUT_SECONDS,
            )
            if rc != 0:
                if rc == 130:
                    state.tasks.update(t, "warn", "cancelled")
                    return False
                if rc == 124:
                    state.tasks.update(t, "fail", first_line(err))
                    return False
                rc, _, err = git_cancellable(
                    winner.path,
                    ["push", "--set-upstream", "origin", branch],
                    cancel_event=cancel_event,
                    timeout=USER_PUSH_TIMEOUT_SECONDS,
                )
        except Exception as e:  # noqa: BLE001
            state.tasks.update(t, "fail", first_line(str(e)))
            return False
    if rc != 0:
        if rc == 130:
            state.tasks.update(t, "warn", "cancelled")
        else:
            state.tasks.update(t, "fail", first_line(err))
        return False
    state.tasks.update(t, "ok")
    return True


def _head_is_ancestor_of(path: Path, ref: str) -> bool:
    """True if HEAD is fully contained in `ref`'s history — i.e. `git
    checkout <ref>` would advance HEAD without orphaning any commits.

    This is the safety check smart-sync uses before any plain checkout
    from a detached HEAD. Git's own behaviour when leaving a detached
    HEAD with unique commits is to print a stderr warning + return 0,
    which means rc-only callers (us, until now) blow past it and the
    file content unique to those orphaned commits disappears from the
    working tree. Returns False on any error (missing ref, malformed
    HEAD, …) so callers default to "refuse to proceed"."""
    rc, _, _ = git(
        path,
        [
            "merge-base",
            "--is-ancestor",
            "HEAD",
            ref,
        ],
    )
    return rc == 0


def _ref_is_ancestor_of_head(path: Path, ref: str) -> bool:
    """Mirror of `_head_is_ancestor_of`, but the other direction —
    True when `ref` is fully contained in HEAD's history. This is the
    auto-recovery condition: if `ref` is an ancestor of HEAD, then
    `git checkout -B <ref> HEAD` (which moves the branch ref forward
    to HEAD's commit) is a fast-forward of `<ref>` — every commit
    that was reachable from `<ref>` before is still reachable from it
    now, and HEAD's unique commits are also captured by the branch."""
    rc, _, _ = git(
        path,
        [
            "merge-base",
            "--is-ancestor",
            ref,
            "HEAD",
        ],
    )
    return rc == 0


def _count_commits_between(path: Path, base_ref: str, head_ref: str = "HEAD") -> int:
    """`git rev-list --count base_ref..head_ref` — number of commits
    reachable from `head_ref` but not from `base_ref`. Used by the
    recovery prompt to tell the user how many commits would be saved
    by the FF. Returns 0 on any error so the prompt still renders
    (just without the count)."""
    rc, out, _ = git(
        path,
        [
            "rev-list",
            "--count",
            f"{base_ref}..{head_ref}",
        ],
    )
    if rc != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


def _ff_branch_to_head(path: Path, branch: str) -> Tuple[bool, str]:
    """Fast-forward `<branch>` to HEAD's current commit and switch onto
    it. Cardinal-rule safe IFF the caller has already verified that
    `_ref_is_ancestor_of_head(path, branch)` — without that check this
    would silently abandon any commits that were unique to <branch>'s
    prior tip.

    Implemented as `git checkout -B <branch> HEAD`: an existing branch
    gets its ref reset to HEAD (no commits orphaned because `<branch>`'s
    old position is in HEAD's history); the working tree shouldn't
    change because HEAD's tree IS the new branch tip's tree.

    Returns (ok, message). Empty message on success."""
    if not is_safe_ref_arg(branch):
        return False, "unsafe branch name"
    rc, _, err = git(path, ["checkout", "-B", branch, "HEAD"])
    if rc != 0:
        return False, first_line(err) or "checkout -B failed"
    return True, ""


def _default_recovery_target(path: Path) -> str:
    """Pick a sensible default branch to recover a detached HEAD onto.
    Tries `origin/HEAD` (which records the remote's default branch),
    then falls back to `master` and `main`. Returns "" when none of
    those refs exist — callers refuse the recovery in that case."""
    rc, out, _ = git(
        path,
        [
            "symbolic-ref",
            "--short",
            "refs/remotes/origin/HEAD",
        ],
    )
    if rc == 0 and out.strip().startswith("origin/"):
        candidate = out.strip()[len("origin/") :]
        rc2, _, _ = git(path, ["rev-parse", "--verify", candidate])
        if rc2 == 0:
            return candidate
    for candidate in ("master", "main"):
        rc, _, _ = git(path, ["rev-parse", "--verify", candidate])
        if rc == 0:
            return candidate
    return ""


def _build_recovery_prompt(
    path: Path, target_label: str, target_branch: str = ""
) -> Optional[DetachedRecoveryPrompt]:
    """Compute the metadata for a `DetachedRecoveryPrompt` against
    `path` — `(target_branch, can_ff, n_extra, head_sha)` — without
    blocking. Returns `None` when the path isn't actually detached or
    no recovery target exists; otherwise a fully-populated prompt the
    caller can install on `state` and resolve via either the worker-
    thread `result_event` pattern or a synchronous main-thread inner
    event loop."""
    rc, branch_out, _ = git(path, ["branch", "--show-current"])
    if rc == 0 and branch_out.strip():
        return None

    if not target_branch:
        target_branch = _default_recovery_target(path)
    if not target_branch:
        return None

    head_to_branch = _head_is_ancestor_of(path, target_branch)
    branch_to_head = _ref_is_ancestor_of_head(path, target_branch)
    can_ff = head_to_branch or branch_to_head

    n_extra = 0 if head_to_branch else _count_commits_between(path, target_branch, "HEAD")

    rc, head_out, _ = git(path, ["rev-parse", "HEAD"])
    head_sha = head_out.strip() if rc == 0 else ""

    return DetachedRecoveryPrompt(
        target_label=target_label,
        head_sha=head_sha,
        target_branch=target_branch,
        n_extra=n_extra,
        can_ff=can_ff,
    )


def execute_detached_recovery(path: Path, target_branch: str) -> Tuple[bool, str]:
    """Run the cardinal-rule-safe recovery checkout AFTER the user has
    confirmed via the modal. Picks strategy A (`git checkout <branch>`
    when HEAD is an ancestor of branch) vs strategy B (`git checkout
    -B <branch> HEAD` when branch is an ancestor of HEAD), defending
    against the divergent case so the caller can't accidentally turn
    a "user said yes" signal into orphaned commits."""
    if not is_safe_ref_arg(target_branch):
        return False, "unsafe branch name"
    if _head_is_ancestor_of(path, target_branch):
        rc, _, err = git(path, ["checkout", target_branch])
    elif _ref_is_ancestor_of_head(path, target_branch):
        rc, _, err = git(path, ["checkout", "-B", target_branch, "HEAD"])
    else:
        return False, "auto-recovery not safe (divergent histories)"
    if rc != 0:
        return False, first_line(err) or "recovery checkout failed"
    return True, ""


def review_detached_targets(state: State) -> List[Tuple[Path, str]]:
    """Commit-message targets currently known to be on detached HEAD."""
    targets: List[Tuple[Path, str]] = []
    for repo in active_workspace_repo_rows(state):
        status = state.store.repo_status(repo)
        if (
            status is not None
            and status.message.strip()
            and status.branch == "(detached)"
        ):
            targets.append((repo.path, repo.display_name))
    for parent, child in active_workspace_child_rows(state):
        status = state.store.child_status(child)
        if (
            status is None
            or status.kind != "submodule"
            or not status.message.strip()
        ):
            continue
        if status.branch != "(detached)":
            continue
        label = f"↳ {child.repo.display_name} in {parent.display_name}"
        targets.append((child.nested_path, label))
    return targets


def _refresh_detached_review_target(
        state: State,
        path: Path,
        label: str,
) -> JobTaskOutcome:
    for repo in active_workspace_repo_rows(state):
        if repo.path == path:
            try:
                _refresh_repo_snapshot_into_state(state, repo)
            except Exception as e:  # noqa: BLE001
                return JobTaskOutcome(JobStatus.WARN, f"{label}: {first_line(str(e))}")
            return JobTaskOutcome()
    for _, ref in active_workspace_child_rows(state):
        status = state.store.child_status(ref)
        if status is None or status.kind != "submodule" or ref.nested_path != path:
            continue
        try:
            _refresh_repo_snapshot_into_state(state, ref.repo)
        except Exception as e:  # noqa: BLE001
            return JobTaskOutcome(JobStatus.WARN, f"{label}: {first_line(str(e))}")
        return JobTaskOutcome()
    return JobTaskOutcome(JobStatus.WARN, f"{label}: target no longer visible")


def kick_off_detached_review_preflight(state: State) -> Optional[Job]:
    targets = review_detached_targets(state)
    if not targets:
        return None
    header = state.tasks.add("review preflight: detached recovery")

    def worker(job: Job) -> None:
        outcome = JobTaskOutcome(JobStatus.OK, "ready")
        for path, label in targets:
            if job.cancel_event.is_set():
                state.tasks.update(header, "warn", "cancelled")
                state.job_registry.finish(job, JobStatus.CANCELLED, "cancelled")
                return
            t = state.tasks.add(f"  ↳ recover {label}", parent=header)
            recovered, msg = _attempt_detached_recovery(state, path, label)
            if not recovered:
                message = msg or "recovery cancelled"
                state.tasks.update(t, "fail", message)
                state.tasks.update(header, "fail", message)
                state.job_registry.finish(job, JobStatus.FAIL, message)
                return
            state.tasks.update(t, "ok", msg if msg != "not detached" else "")
            refresh_outcome = _refresh_detached_review_target(state, path, label)
            outcome = _merge_job_task_outcome(outcome, refresh_outcome)
            if refresh_outcome.status == JobStatus.WARN:
                warn = state.tasks.add(f"  ↳ refresh {label}", parent=header)
                state.tasks.update(warn, "warn", refresh_outcome.message)
        if outcome.status == JobStatus.WARN:
            state.tasks.update(header, "warn", outcome.message)
            state.job_registry.finish(job, JobStatus.WARN, outcome.message)
            return
        state.tasks.update(header, "ok", outcome.message)
        state.job_registry.finish(job, JobStatus.OK, outcome.message)

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="review-preflight",
            label=header.label,
            local_mutation=True,
            repo_keys=tuple(str(path) for path, _label in targets),
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        bridge = JobTaskBridge(state.tasks, state.job_registry, job)
        bridge.attach(header)
        bridge.update(header, "fail", first_line(job.message))
    return job


def _attempt_detached_recovery(
    state: State, path: Path, target_label: str, target_branch: str = ""
) -> Tuple[bool, str]:
    """When `path` has a detached HEAD, ask the user (via the
    `DetachedRecoveryPrompt` modal) for permission to safely park HEAD
    on a branch — then do it.

    Two safe shapes (cardinal-rule preserving):

      A) HEAD is an ancestor of `target_branch`. `git checkout
         <branch>` advances HEAD without orphaning anything; HEAD's
         old position is already on the branch.

      B) `target_branch` is an ancestor of HEAD. `git checkout -B
         <branch> HEAD` fast-forwards the branch ref to HEAD; the
         branch's old commits are still in HEAD's history, so nothing
         is lost.

    Anything else (real divergence, or no recovery target found) is
    refused — the modal opens with `can_ff=False` so the user sees
    why and what to do manually.

    `target_branch=""` lets the helper resolve `origin/HEAD` and fall
    back to master/main. Smart-sync passes the user-picked branch
    explicitly so the prompt reflects their choice.

    Returns (recovered, msg):
      (True, "")            HEAD is now on a branch; caller continues.
      (False, reason)       user cancelled or no safe path; refuse."""
    # Pre-flight check — if we're not actually detached, recovery is
    # a no-op and the caller's flow can continue.
    rc, branch_out, _ = git(path, ["branch", "--show-current"])
    if rc == 0 and branch_out.strip():
        return True, "not detached"

    with _detached_recovery_prompt_lock:
        prompt = _build_recovery_prompt(path, target_label, target_branch)
        if prompt is None:
            return False, "no recovery target branch available"
        state.detached_recovery_prompt = prompt
        if not prompt.result_event.wait(PROMPT_WAIT_SECONDS):
            if state.detached_recovery_prompt is prompt:
                state.detached_recovery_prompt = None
            return False, "recovery prompt timed out"

        if prompt.chosen_action != "ff":
            return False, "user cancelled recovery"
        if not prompt.can_ff:
            # Defensive — the modal's Enter handler shouldn't return "ff"
            # when can_ff is False, but if it ever does we refuse rather
            # than execute an unsafe operation.
            return False, "auto-recovery not safe (divergent histories)"
        return execute_detached_recovery(path, prompt.target_branch)


def _switch_to_branch(state: State, c: SmartSyncCheckout, branch: str, name: str) -> bool:
    """Move a checkout onto a named branch. Git refuses if the WT has
    changes that would conflict with the new branch tip — non-destructive."""
    t = state.tasks.add(f"  ↳ align {name}: switch {c.label} → {branch}")
    if not is_safe_ref_arg(branch):
        state.tasks.update(t, "fail", "unsafe branch name")
        return False
    rc, _, err = git(c.path, ["checkout", branch])
    if rc != 0:
        state.tasks.update(t, "fail", first_line(err))
        return False
    state.tasks.update(t, "ok")
    return True


# Status XY codes the redundant-dirty check is willing to reason about.
# Each one is "the working-tree byte content is what we want to compare
# against the target's blob" — every member is safe to handle by
# hashing the WT path directly:
#   ' M' — WT modified (not staged)
#   'M ' — staged modified (WT == index, both differ from HEAD)
#   'MM' — staged AND further-modified in WT
#   'A ' — newly added in index (WT == index, file is new)
#   'AM' — added in index, then modified in WT
#   '??' — untracked file
# Renames, copies, and deletes are deliberately excluded — those cases
# need different reasoning (deletion implies the file SHOULDN'T exist
# in target; rename implies a path mapping). The fallback warn-skips
# them rather than risk a stash-and-drop that loses the user's intent.
_REDUNDANT_DIRTY_STATUS_CODES = frozenset(
    {
        " M",
        "??",
        "M ",
        "MM",
        "A ",
        "AM",
    }
)


def _verify_dirty_matches_target(c: SmartSyncCheckout, target_ref: str) -> Optional[bool]:
    """Hash every dirty path's working-tree content and compare to the
    same path's blob in `target_ref`. Returns True when every dirty
    path is bit-identical to the target's version (the change is
    "redundant" — what the user typed is already what's about to be
    installed by FF / checkout, so it's safe to stash → operate →
    drop), False when any path diverges (a real conflict), and None
    on infrastructure error. Conservative on weird status shapes —
    deletes, renames, and quoted paths short-circuit to False so the
    caller warn-skips rather than risks losing state. The caller
    never sees the False vs None split — it just means "don't take
    the redundant-dirty fast path"."""
    rc, status_out, _ = git(c.path, ["status", "--porcelain=v1"])
    if rc != 0:
        return None
    if not status_out.strip():
        return None  # not dirty; caller's failure must be something else

    dirty: List[Tuple[str, str]] = []
    for line in status_out.splitlines():
        if len(line) < 3:
            continue
        xy = line[:2]
        rest = line[3:]
        if xy not in _REDUNDANT_DIRTY_STATUS_CODES:
            return False
        if " -> " in rest:
            return False
        if rest.startswith('"'):
            return False
        dirty.append((xy, rest))

    if not dirty:
        return None

    for _, path_str in dirty:
        rc, lt_out, _ = git(c.path, ["ls-tree", target_ref, "--", path_str])
        if rc != 0 or not lt_out.strip():
            # Path absent in target — for a fresh "added" status this
            # means the winner didn't push the file, so dropping the
            # stash would lose the user's new file. For a modified
            # status it means the path was deleted upstream, also a
            # genuine divergence. Either way, not safe.
            return False
        head, _, _ = lt_out.partition("\t")
        parts = head.split()
        if len(parts) < 3 or parts[1] != "blob":
            return False
        target_hash = parts[2]
        rc, ho_out, _ = git(c.path, ["hash-object", "--", path_str])
        if rc != 0:
            return False
        if ho_out.strip() != target_hash:
            return False
    return True


def _post_merge_clean(path: Path) -> bool:
    """True if the working tree is fully clean — i.e. `git status
    --porcelain=v1` produces no output. Used as the safety gate before
    any `git stash drop` in smart-sync: if anything is unexpectedly
    dirty after a merge / checkout that was supposed to consolidate
    everything, we leave the stash on the stash list so the user's
    content is recoverable instead of silently discarded."""
    rc, status_out, _ = git(path, ["status", "--porcelain=v1"])
    return rc == 0 and not status_out.strip()


def _try_ff_through_redundant_dirty(
    state: State, c: SmartSyncCheckout, winner_branch: str, name: str
) -> Optional[bool]:
    """Best-effort fast-forward for the case where the loser is dirty
    with content that's bit-identical to what's about to land via FF
    (a common pattern when multiple sub-module checkouts received the
    same edit before smart-sync ran). Stash → merge, with safety nets
    on every step:

      - The pre-condition `_verify_dirty_matches_target` proves every
        dirty path is bit-identical to target_ref.
      - `merge --ff-only` itself refuses to orphan commits (FF-only
        won't run if HEAD has commits not on target_ref).
      - On post-merge cleanliness failure, the stash is left on the
        list so the user can recover via `git stash list`.

    Cardinal rule: idlegit NEVER calls `git stash drop` on the user's
    behalf. Even on a successful merge where the stash content is now
    redundant with HEAD (we verified bit-equality before stashing),
    the stash entry is left on the list — pruning is the user's call,
    via `git stash list` / `git stash drop`. The cost is one stash
    entry per redundant-dirty alignment; the benefit is that no idlegit
    code path can ever delete content a user expected to keep.

    Returns True/False/None where False means "let the caller warn-
    skip" and None means infrastructure error (same calling contract)."""
    if not is_safe_ref_arg(winner_branch):
        return False
    target_ref = f"origin/{winner_branch}"
    matches = _verify_dirty_matches_target(c, target_ref)
    if matches is not True:
        return matches

    stash_msg = "auto: redundant dirty changes"
    rc, _, _ = git(
        c.path,
        [
            "stash",
            "push",
            "--include-untracked",
            "-m",
            stash_msg,
        ],
    )
    if rc != 0:
        return False

    rc, _, _ = git(c.path, ["merge", "--ff-only", target_ref])
    if rc != 0:
        git(c.path, ["stash", "pop"])
        return False

    if not _post_merge_clean(c.path):
        # Something didn't reconcile the way verification predicted.
        # Leave the stash on the list — the user's content is fully
        # preserved there, recoverable via `git stash list`. Surface
        # this in the task panel so the user sees that there's
        # recoverable state and where to find it.
        kept = state.tasks.add(f"  ↳ align {name}: stash kept on {c.label}")
        state.tasks.update(kept, "warn", "post-merge WT not clean — recover via `git stash list`")
        return False

    # Successful merge. Stash is intentionally NOT dropped (cardinal
    # rule). Surface the kept stash so the user knows where the
    # redundant-dirty content is parked and can prune at leisure.
    kept = state.tasks.add(f"  ↳ align {name}: stash kept on {c.label}")
    state.tasks.update(kept, "ok", "redundant dirty preserved — prune via `git stash drop`")
    return True


def _try_detached_checkout_through_redundant_dirty(
    state: State, c: SmartSyncCheckout, winner_branch: str, name: str
) -> Optional[bool]:
    """Sibling of `_try_ff_through_redundant_dirty` for the detached-
    loser case. After a winner pushes, detached losers need a `git
    checkout origin/<branch>` to land them on the new commit; if their
    WT carries the same edit that the winner just published, that
    checkout would otherwise refuse with "would be overwritten." This
    helper verifies bit-equality against `origin/<branch>`, then does
    stash → checkout with the same safety nets as the FF path:

      - Pre-condition: `_verify_dirty_matches_target` confirms every
        dirty path is bit-identical to target_ref.
      - Ancestor check: HEAD must be in target_ref's history so the
        checkout doesn't orphan unique commits.
      - Post-condition: `_post_merge_clean` confirms WT is fully clean,
        otherwise the stash stays on the list.

    Cardinal rule: as in the FF sibling, idlegit NEVER calls `git stash
    drop` — the stash is preserved on every code path so no content can
    be lost to a wrong post-condition prediction."""
    if not is_safe_ref_arg(winner_branch):
        return False
    target_ref = f"origin/{winner_branch}"
    matches = _verify_dirty_matches_target(c, target_ref)
    if matches is not True:
        return matches

    if not _head_is_ancestor_of(c.path, target_ref):
        # HEAD has commits not on target_ref — switching would orphan
        # them; refuse rather than risk losing files unique to those
        # commits.
        return False

    stash_msg = "auto: redundant dirty changes"
    rc, _, _ = git(
        c.path,
        [
            "stash",
            "push",
            "--include-untracked",
            "-m",
            stash_msg,
        ],
    )
    if rc != 0:
        return False

    rc, _, _ = git(c.path, ["checkout", target_ref])
    if rc != 0:
        git(c.path, ["stash", "pop"])
        return False

    if not _post_merge_clean(c.path):
        kept = state.tasks.add(f"  ↳ align {name}: stash kept on {c.label}")
        state.tasks.update(
            kept, "warn", "post-checkout WT not clean — recover via `git stash list`"
        )
        return False

    # Successful checkout. Stash is intentionally NOT dropped.
    kept = state.tasks.add(f"  ↳ align {name}: stash kept on {c.label}")
    state.tasks.update(kept, "ok", "redundant dirty preserved — prune via `git stash drop`")
    return True


def _stash_switch_pop_winner(
    state: State, winner: SmartSyncCheckout, branch: str, name: str
) -> bool:
    """Switch a detached winner onto `branch` BEFORE committing its
    dirty content — committing first would create an orphan commit
    that the subsequent checkout would silently leave behind, and the
    push step would then push an empty change to the chosen branch.

    Plain `git checkout <branch>` works when the WT is clean OR when
    the chosen branch's tree happens to match the detached HEAD's
    tree for every dirty path. When git refuses ("would be overwritten
    by checkout"), we fall back to stash → checkout → pop so the
    user's edits get carried onto the new branch and end up included
    in the subsequent commit. Stash content is preserved on every
    failure path — pop conflicts in particular leave the stash on the
    stash list, so the user can recover with `git stash pop` manually."""
    t = state.tasks.add(f"  ↳ align {name}: switch {winner.label} → {branch}")
    if not is_safe_ref_arg(branch):
        state.tasks.update(t, "fail", "unsafe branch name")
        return False

    # Refuse to switch if HEAD has commits that aren't on `branch` —
    # git would silently orphan them (rc=0 with a stderr warning)
    # and any files unique to those commits would vanish from the
    # working tree because the new branch's tree replaces them. The
    # guard is the reason your file got deleted last time, so we
    # treat it as an absolute red line.
    if not _head_is_ancestor_of(winner.path, branch):
        # Auto-recovery: when `branch` is an ancestor of HEAD, the
        # winner's detached commits are a strict superset of the
        # chosen branch — a `git checkout -B <branch> HEAD` is a
        # fast-forward of the branch ref, fully cardinal-rule safe.
        # Pop a modal asking the user's permission first; on confirm
        # we do the FF and continue with the rest of the switch flow.
        if _ref_is_ancestor_of_head(winner.path, branch):
            recovered, msg = _attempt_detached_recovery(
                state, winner.path, winner.label, target_branch=branch
            )
            if recovered:
                state.tasks.update(t, "ok", f"fast-forwarded {branch} to HEAD")
                return True
            state.tasks.update(t, "warn", f"{winner.label}: {msg}")
            return False
        state.tasks.update(
            t,
            "warn",
            f"{winner.label}: detached HEAD has commits not on {branch} "
            "— would orphan them; manual: `git checkout -b <name>` to keep them",
        )
        return False

    rc, _, err = git(winner.path, ["checkout", branch])
    if rc == 0:
        state.tasks.update(t, "ok")
        return True
    initial_err = first_line(err)

    # Dirty WT blocked the plain checkout. Stash it so checkout has a
    # clean tree to land on, then pop the diffs back on top of the
    # branch. `git stash pop` preserves the stash on conflict, so the
    # user never loses their changes.
    rc, _, _ = git(
        winner.path,
        [
            "stash",
            "push",
            "--include-untracked",
            "-m",
            "auto: align detached HEAD",
        ],
    )
    if rc != 0:
        state.tasks.update(t, "fail", initial_err)
        return False

    rc, _, err = git(winner.path, ["checkout", branch])
    if rc != 0:
        git(winner.path, ["stash", "pop"])  # restore on bail
        state.tasks.update(t, "fail", f"checkout {branch}: {first_line(err)}")
        return False

    rc, _, err = git(winner.path, ["stash", "pop"])
    if rc != 0:
        # Pop conflicted. Git's behaviour: the stash entry is preserved
        # on the stash list AND the conflicted three-way merge is left
        # in the working tree (with conflict markers in the affected
        # files). Cardinal rule: idlegit MUST NOT run `git reset --hard`
        # to "tidy up" the WT here — even though the conflicted state
        # is in some sense "garbage" that we know is reproducible from
        # the stash, throwing it away on the user's behalf is exactly
        # the class of destructive op that has cost real files in the
        # past. The user resolves the conflicts manually, and if they
        # want a clean WT instead, they run `git reset --hard` themself
        # knowing the stash is recoverable.
        state.tasks.update(
            t,
            "fail",
            f"stash pop on {branch} conflicted — resolve conflict markers "
            "in WT, or `git reset --hard HEAD` then `git stash pop`",
        )
        return False

    state.tasks.update(t, "ok")
    return True


def _align_loser_ff(state: State, c: SmartSyncCheckout, winner_branch: str, name: str) -> bool:
    """Bring a same-branch loser up to the winner's published branch tip
    via `fetch`, then `merge --ff-only origin/<branch>`. When FF refuses,
    we try the redundant-dirty stash helper; when that does not apply or
    fails, and `state.prevent_smart_sync_silent_merge` is False (default),
    we run `merge --no-edit` so divergent histories get a merge commit.

    With `prevent_smart_sync_silent_merge` True, behaviour matches the
    historical FF-only alignment (warn-skip when FF is impossible).

    Refuses (warn-skip) on merge conflicts; the loser's state is never
    hard-reset or rebased."""
    t = state.tasks.add(f"  ↳ align {name}: ff {c.label}")
    if not is_safe_ref_arg(winner_branch):
        state.tasks.update(t, "fail", "unsafe branch name")
        return False
    rc, _, err = git(c.path, ["fetch", "origin", winner_branch])
    if rc != 0:
        state.tasks.update(t, "fail", first_line(err))
        return False
    rc, _, err = git(c.path, ["merge", "--ff-only", f"origin/{winner_branch}"])
    if rc == 0:
        state.tasks.update(t, "ok")
        return True

    # FF refused — usually because the WT has changes that "would be
    # overwritten by merge". When those changes are bit-identical to
    # what's about to land (a common pattern when the same edit was
    # made in multiple submodule checkouts), stash them, retry, and
    # drop the stash. Anything else: restore via the helper's stash
    # pop and warn-skip with the original merge error preserved.
    redundant = _try_ff_through_redundant_dirty(state, c, winner_branch, name)
    if redundant is True:
        state.tasks.update(t, "ok", "merged identical dirty changes")
        return True
    allow_merge = not state.prevent_smart_sync_silent_merge
    if allow_merge:
        rc_m, _, err_m = git(c.path, ["merge", "--no-edit", f"origin/{winner_branch}"])
        if rc_m == 0:
            state.tasks.update(t, "ok", "merged origin")
            return True
        err = err_m
    state.tasks.update(t, "warn", first_line(err))
    return False


def _align_detached_loser(
    state: State, c: SmartSyncCheckout, winner_branch: str, name: str
) -> bool:
    """Bring a detached-HEAD loser onto the winner's published commit
    via `fetch + checkout origin/<branch>`. Plain checkout fails when
    a dirty WT path differs from origin's — for the very common case
    where the dirty content is bit-identical to what origin now holds
    (multiple checkouts received the same edit before smart-sync ran),
    fall back to the same stash-and-retry pattern the FF path uses.

    Refuses to touch the checkout if HEAD has commits that aren't on
    `origin/<winner_branch>` — switching would orphan them and any
    files unique to those commits would vanish."""
    t = state.tasks.add(f"  ↳ align {name}: switch+sync {c.label}")
    if not is_safe_ref_arg(winner_branch):
        state.tasks.update(t, "fail", "unsafe branch name")
        return False
    rc, _, err = git(c.path, ["fetch", "origin", winner_branch])
    if rc != 0:
        state.tasks.update(t, "fail", first_line(err))
        return False
    target_ref = f"origin/{winner_branch}"
    if not _head_is_ancestor_of(c.path, target_ref):
        state.tasks.update(
            t,
            "warn",
            f"detached HEAD has commits not on {target_ref} "
            "— would orphan them; manual: `git checkout -b <name>`",
        )
        return False
    rc, _, err = git(c.path, ["checkout", target_ref])
    if rc == 0:
        state.tasks.update(t, "ok")
        return True

    # Plain checkout refused — verify the dirty content is bit-
    # identical to origin/<branch>'s. If so, stash + retry + drop;
    # if any path differs, leave the loser alone and warn-skip with
    # the original git error so the user can resolve manually.
    redundant = _try_detached_checkout_through_redundant_dirty(state, c, winner_branch, name)
    if redundant is True:
        state.tasks.update(t, "ok", "merged identical dirty changes")
        return True
    state.tasks.update(t, "warn", first_line(err))
    return False


def _resolve_origin_head_branch(path: Path) -> str:
    """Return the local short branch name pointed at by `origin/HEAD`,
    or "" when origin's HEAD pointer isn't set / can't be resolved.
    Used by the detached-winner flow when `prompt_for_branch` is OFF —
    we auto-resolve to whatever GitHub / the remote's clone considers
    its default branch (typically `main` or `master`) instead of asking
    the user."""
    rc, out, _ = git(
        path,
        [
            "symbolic-ref",
            "--short",
            "refs/remotes/origin/HEAD",
        ],
    )
    if rc != 0:
        return ""
    ref = out.strip()
    if ref.startswith("origin/"):
        return ref[len("origin/") :]
    return ""


def _open_align_heads_prompt_and_wait(state: State, winner: SmartSyncCheckout) -> str:
    """Pop the AlignHeadsPrompt modal and block until the user resolves
    it. Returns the chosen branch (or empty string on cancel). Called
    from the smart-sync worker thread; the modal handler in the main
    loop signals `result_event`. The modal receives FULL display names
    (no `task_repo_label` pre-truncation) and lays them out itself."""
    with _align_heads_prompt_lock:
        branches, _ = list_branches(winner.path)
        parent_name = winner.parent.display_name if winner.parent else ""
        prompt = AlignHeadsPrompt(
            canonical_name=winner.canonical.display_name,
            winner_parent_name=parent_name,
            winner_sha=winner.head,
            branches=branches,
            selected=0,
        )
        state.align_heads_prompt = prompt
        if not prompt.result_event.wait(PROMPT_WAIT_SECONDS):
            if state.align_heads_prompt is prompt:
                state.align_heads_prompt = None
            return ""
        return prompt.chosen_branch or ""


def _probe_canonical_checkouts(state: State, canonical: Repo) -> List[SmartSyncCheckout]:
    checkouts: List[SmartSyncCheckout] = [
        _probe_checkout_full(state, canonical.path, None, canonical)
    ]
    for parent, path in canonical.siblings:
        checkouts.append(_probe_checkout_full(state, path, parent, canonical))
    return checkouts


def _canonical_already_aligned(state: State, canonical: Repo) -> bool:
    checkouts = _probe_canonical_checkouts(state, canonical)
    if not checkouts or any(not c.head for c in checkouts):
        return False
    heads = {c.head for c in checkouts if c.head}
    plan = plan_canonical_alignment(
        (_checkout_snapshot(c, heads=heads) for c in checkouts),
        settings=_smart_sync_settings(state),
    )
    return plan.status == CanonicalPlanStatus.NOOP


def _smart_sync_settings(state: State) -> SmartSyncSettings:
    return SmartSyncSettings(
        auto_stage=state.auto_stage,
        auto_ff=state.auto_ff,
        align_heads=state.align_heads,
        prompt_for_branch=state.prompt_for_branch,
    )


def _checkout_id(checkout: SmartSyncCheckout) -> str:
    return str(checkout.path)


def _checkout_commit_time(checkout: SmartSyncCheckout, heads: "set[str]") -> int:
    if len(heads) <= 1 or checkout.dirty or checkout.ahead > 0:
        return 0
    rc, out, _ = git(checkout.path, ["log", "-1", "--format=%ct", "HEAD"])
    try:
        return int(out.strip()) if rc == 0 else 0
    except ValueError:
        return 0


def _checkout_snapshot(
        checkout: SmartSyncCheckout,
        *,
        heads: "set[str]",
) -> CheckoutSnapshot:
    return CheckoutSnapshot(
        checkout_id=_checkout_id(checkout),
        label=checkout.label,
        path=checkout.path,
        branch=checkout.branch,
        head=checkout.head,
        dirty=checkout.dirty,
        ahead=checkout.ahead,
        behind=checkout.behind,
        parent_id=str(checkout.parent.path) if checkout.parent is not None else None,
        commit_time=_checkout_commit_time(checkout, heads),
        sig_mtime=checkout.sig_mtime,
    )


def _refresh_checkout_head(checkout: SmartSyncCheckout) -> None:
    """Re-probe a checkout HEAD after branch switch, commit, or push."""
    rc, head_out, _ = git(checkout.path, ["rev-parse", "HEAD"])
    if rc == 0 and head_out.strip():
        checkout.head = head_out.strip()


def _align_canonical(
        state: State,
        canonical: Repo,
        *,
        task_bridge: Optional[JobTaskBridge] = None,
        cancel_event: "Optional[threading.Event]" = None,
) -> Tuple[int, int]:
    """Plan and execute alignment for one canonical's checkouts."""
    ok_total = 0
    fail_total = 0
    max_passes = 8
    for _pass in range(max_passes):
        if cancel_event is not None and cancel_event.is_set():
            return ok_total, fail_total
        ok, fail = _align_canonical_once(
            state,
            canonical,
            task_bridge=task_bridge,
        )
        ok_total += ok
        fail_total += fail
        if fail or ok == 0:
            return ok_total, fail_total
    tasks = task_bridge or JobTaskBridge(state.tasks)
    t = tasks.add(
        f"  ↳ align {state.task_repo_label(canonical)}")
    tasks.update(t, "warn", "alignment pass limit reached")
    return ok_total, fail_total + 1


def _align_canonical_once(
        state: State,
        canonical: Repo,
        *,
        task_bridge: Optional[JobTaskBridge] = None,
) -> Tuple[int, int]:
    """Run one probe/plan/execute pass for a canonical checkout group."""
    checkouts = _probe_canonical_checkouts(state, canonical)
    heads = {c.head for c in checkouts if c.head}
    checkout_by_id = {_checkout_id(c): c for c in checkouts}
    plan = plan_canonical_alignment(
        (_checkout_snapshot(c, heads=heads) for c in checkouts),
        settings=_smart_sync_settings(state),
    )
    plan = _same_branch_ff_chain_plan(checkouts, checkout_by_id, plan) or plan
    deps = CanonicalExecutionDeps(
        open_branch_prompt=_open_align_heads_prompt_and_wait,
        resolve_origin_head=_resolve_origin_head_branch,
        switch_winner_to_branch=_stash_switch_pop_winner,
        commit_dirty_winner=_commit_dirty_winner,
        push_winner=_push_winner,
        refresh_winner_head=_refresh_checkout_head,
        align_loser_ff=_align_loser_ff,
        align_detached_loser=_align_detached_loser,
    )
    return execute_canonical_plan(
        state,
        canonical,
        checkouts,
        checkout_by_id,
        plan,
        deps,
        task_bridge=task_bridge,
    )


def _same_branch_ff_chain_plan(
        checkouts: List[SmartSyncCheckout],
        checkout_by_id: Dict[str, SmartSyncCheckout],
        plan: CanonicalPlan,
) -> Optional[CanonicalPlan]:
    """Upgrade a conservative multi-ahead warning into a safe FF chain.

    The pure planner has no ancestry facts, so it warns when more than one
    checkout is ahead. Here we can ask git: if all ahead checkouts are on the
    same branch and one ahead HEAD is a descendant of all the others, pushing
    that descendant then FF-aligning the rest is safe and non-destructive.
    """
    if plan.status != CanonicalPlanStatus.WARN:
        return None
    aheads = [checkout for checkout in checkouts if checkout.ahead > 0]
    if len(aheads) <= 1:
        return None
    branches = {checkout.branch for checkout in aheads}
    if len(branches) != 1 or "(detached)" in branches:
        return None
    if any(checkout.dirty for checkout in aheads):
        return None

    winner: Optional[SmartSyncCheckout] = None
    for candidate in aheads:
        if all(
                other is candidate
                or _commit_is_ancestor(candidate.path, other.head, candidate.head)
                for other in aheads
        ):
            if winner is not None:
                return None
            winner = candidate
    if winner is None:
        return None

    steps: List[SyncStep] = [SyncStep(SyncStepKind.PUSH_WINNER, _checkout_id(winner))]
    winner_branch = winner.branch
    for checkout in checkouts:
        if checkout is winner:
            continue
        if checkout.head == winner.head and not checkout.dirty:
            continue
        if checkout.branch == winner_branch:
            steps.append(SyncStep(SyncStepKind.ALIGN_FF, _checkout_id(checkout)))
        elif checkout.branch == "(detached)":
            steps.append(SyncStep(SyncStepKind.ALIGN_DETACHED, _checkout_id(checkout)))
        else:
            return None
    # Defensive: only keep the fallback if all planned ids still map to live
    # checkout objects. This should always hold, but avoids producing a plan
    # with stale ids if a future caller changes id construction.
    if any(step.target_id not in checkout_by_id for step in steps):
        return None
    return CanonicalPlan(
        status=CanonicalPlanStatus.READY,
        winner_id=_checkout_id(winner),
        steps=tuple(steps),
    )


def _commit_is_ancestor(path: Path, maybe_ancestor: str, maybe_descendant: str) -> bool:
    if not maybe_ancestor or not maybe_descendant:
        return False
    rc, _, _ = git(
        path,
        [
            "merge-base",
            "--is-ancestor",
            maybe_ancestor,
            maybe_descendant,
        ],
    )
    return rc == 0


def _propagate_submodule_bump(
        state: State,
        parent: Repo,
        parent_label: str,
        *,
        task_bridge: Optional[JobTaskBridge] = None,
        cancel_event: "Optional[threading.Event]" = None,
) -> str:
    return propagate_submodule_bump(
        state,
        parent,
        parent_label,
        push_timeout_seconds=PROPAGATE_PUSH_TIMEOUT_SECONDS,
        task_bridge=task_bridge,
        cancel_event=cancel_event,
        git_fn=git,
        git_cancellable_fn=git_cancellable,
        refresh_fn=lambda repo: _refresh_repo_snapshot_into_state(state, repo),
        submodule_paths_fn=submodule_pointer_change_paths,
    )


def _ff_submodule_checkout_to(path: Path, branch: str, target_sha: str) -> bool:
    return ff_submodule_checkout_to(path, branch, target_sha, git_fn=git)


def _cascade_propagate_to_parents(
        state: State,
        canonicals_synced: List[Repo],
        *,
        task_bridge: Optional[JobTaskBridge] = None,
        cancel_event: "Optional[threading.Event]" = None,
) -> None:
    cascade_propagate_to_parents(
        state,
        canonicals_synced,
        find_child_at=_find_child_at,
        propagate_parent=_propagate_submodule_bump,
        ff_submodule=_ff_submodule_checkout_to,
        task_bridge=task_bridge,
        cancel_event=cancel_event,
        git_fn=git,
    )


def kick_off_sync_siblings(state: State) -> None:
    """Entry point for Ctrl+S — align every canonical's submodule
    checkouts (and pull subtrees). Non-destructive throughout.

    Canonicals are processed serially so the AlignHeadsPrompt modal
    can block one canonical's worker without blocking others, and so
    the user sees a coherent stream of tasks for one repo at a time.
    Subtrees fire in series after the canonicals."""
    header = state.tasks.add("smart-sync")
    state.tasks.update(header, "running", "preparing")
    plan = build_smart_sync_work_plan(state)
    job = state.job_registry.start(JobSpec(
        kind="smart-sync",
        label=header.label,
        local_mutation=bool(plan.repo_keys or plan.child_keys),
        repo_keys=plan.repo_keys,
        child_keys=plan.child_keys,
    ))
    task_bridge = JobTaskBridge(state.tasks, state.job_registry, job)
    task_bridge.attach(header)

    def worker(job: Job) -> None:
        try:
            _run_smart_sync_from_worker(state, header, job, plan, task_bridge)
        except Exception as e:  # noqa: BLE001
            task_bridge.update(header, "fail", first_line(str(e)))
            raise

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    thread = start_job_thread(
        state.job_registry,
        job,
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        state.tasks.update(header, "fail", first_line(job.message))


def _run_smart_sync_from_worker(
        state: State,
        header: Task,
        job: Job,
        plan: SmartSyncWorkPlan,
        task_bridge: JobTaskBridge,
) -> None:
    """Prepare and execute smart-sync after the visible job row exists."""
    if not plan.canonicals and not plan.subtree_items:
        task_bridge.update(header, "warn", "no submodules or subtrees to sync")
        state.job_registry.finish(
            job, JobStatus.WARN, "no submodules or subtrees to sync")
        return

    task_bridge.set_label(header, f"smart-sync ({plan.work_count})")
    if (
            plan.canonicals
            and not plan.subtree_items
            and not state.auto_push_submodule_parent
    ):
        try:
            all_aligned = all(
                _canonical_already_aligned(state, canonical)
                for canonical in plan.canonicals
            )
        except Exception:  # noqa: BLE001
            all_aligned = False
        if all_aligned:
            task_bridge.update(header, "ok", "all aligned")
            return

    # The lifecycle helper owns sentinel task rows and row-refresh state while
    # the job registry owns target-aware mutation gating.
    lifecycle = SmartSyncLifecycle(
        state, header, job, plan.canonicals, plan.subtree_items)
    try:
        lifecycle.acquire()
    except Exception as e:  # noqa: BLE001
        msg = first_line(str(e))
        lifecycle.fail_acquire(job, msg)
        return

    config = SmartSyncRunConfig(
        state=state,
        snapshot_repos=plan.snapshot_repos,
        snapshot_subtrees=plan.snapshot_subtrees,
        canonicals=plan.canonicals,
        subtree_items=plan.subtree_items,
        lifecycle=lifecycle,
        align_canonical=_align_canonical,
        propagate_parents=_cascade_propagate_to_parents,
        refresh_repo=lambda repo: _refresh_repo_snapshot_into_state(state, repo),
        sync_subtree=sync_subtree,
        link_siblings=lambda link_repos, link_subtrees: _state_link_siblings(
            state, link_repos, link_subtrees),
        first_line=first_line,
        task_bridge=task_bridge,
    )

    run_smart_sync_job(job, config)


# ---------- Inline refresh ------------------------------------------------


_INLINE_REFRESH_STALE_SECONDS = 30.0
_inline_refresh_queue = InlineRefreshQueue(
    stale_seconds=_INLINE_REFRESH_STALE_SECONDS)
_inline_refresh_lock = _inline_refresh_queue.lock
_inline_refresh_in_flight = False
_inline_refresh_pending = False
_inline_refresh_targets_in_flight = _inline_refresh_queue.in_flight
_inline_refresh_targets_pending = _inline_refresh_queue.pending
_inline_refresh_targets_started_at = _inline_refresh_queue.started_at


def _workspace_has_local_mutation(state: State, repos: List[Repo]) -> bool:
    return local_mutation_active_for(
        state, repos=repos, include_repo_children=True)


def _sync_inline_refresh_flags_locked() -> None:
    """Mirror target-set state onto legacy booleans used by tests."""
    global _inline_refresh_in_flight, _inline_refresh_pending
    _inline_refresh_in_flight = _inline_refresh_queue.has_in_flight()
    _inline_refresh_pending = _inline_refresh_queue.has_pending()


def kick_off_inline_refresh(state: State, manual: bool = False) -> None:
    """Re-discover repos in the workspace, removing gone entries and adding
    new ones inline, and refresh every kept/new repo in parallel — each
    one toggling its `refreshing` flag so the row spinner animates next to
    its name. The main view stays visible the whole time; no overlay.

    Gated per workspace index so duplicate refreshes for one workspace
    coalesce, while switching to another workspace can refresh the newly
    active workspace immediately instead of waiting behind stale work."""
    # Prefer the active workspace's folder list when available — it
    # supports multi-folder workspaces (which the legacy
    # `state.repos[0].path.parent` anchor couldn't, silently dropping
    # repos discovered from any folder other than the first one).
    # Pin the workspace this refresh belongs to. The worker runs async;
    # if the user switches workspaces before it finishes, we must not
    # assign the discovered list to whichever workspace is *currently*
    # active — that was the "always one workspace to the left" bug.
    refresh_scope = WorkspaceRefreshScope.capture(state)
    target_idx = refresh_scope.target_idx
    subtrees = list(refresh_scope.subtrees)
    folders = list(refresh_scope.folders)
    with _inline_refresh_lock:
        started = _inline_refresh_queue.try_start(
            target_idx,
            manual=manual,
            now=time.monotonic(),
            workspace_busy=(
                read_only_row_busy_active(state)
                or _workspace_has_local_mutation(state, state.repos)
            ),
        )
        _sync_inline_refresh_flags_locked()
        if not started:
            if manual:
                t = state.tasks.add("refresh workspace")
                state.tasks.update(t, "ok", "refresh queued")
            return

    if not folders:
        if state.repos:
            anchor = state.repos[0]
            folders = [anchor.path if anchor.rel == "." else anchor.path.parent]
        else:
            # No repos and no workspace folders — release the gate and bail.
            with _inline_refresh_lock:
                _inline_refresh_queue.release(target_idx)
                _sync_inline_refresh_flags_locked()
            if manual:
                t = state.tasks.add("refresh workspace")
                state.tasks.update(t, "warn", "no workspace folders")
            return

    # Claim the per-repo refresh slot SYNCHRONOUSLY before we spawn the
    # worker. Store-owned busy state is checked before the primitive lock
    # so rows already owned by another refresh workflow are skipped
    # without interpreting raw model flags. The claim then acquires the
    # legacy lock behind the RefreshClaim boundary so no other source can
    # start a concurrent refresh on the same repo.
    #
    # Local mutation ownership is checked before the lock so workers whose
    # lock is not held (or whose claim is represented by the job registry)
    # still suppress refresh on their targets. Lock check + job check are
    # belt-and-braces: either reason to bail is enough.
    repos_snapshot = list(state.repos)
    acquired: List[RefreshClaim] = []
    acquired_by_path: Dict[Path, RefreshClaim] = {}
    skipped_active: List[Repo] = []
    manual_task = state.tasks.add("refresh workspace") if manual else None
    for r in repos_snapshot:
        if read_only_row_busy_active(state, [r]):
            skipped_active.append(r)
            continue
        if _workspace_has_local_mutation(state, [r]):
            skipped_active.append(r)
            continue
        claim = RefreshClaim(state, repo=r)
        if claim.acquire():
                acquired.append(claim)
                acquired_by_path[r.path] = claim
    refresh_stale = [False]
    refresh_outcome = [JobTaskOutcome()]
    manual_task_terminal = [False]

    def worker(job: Job) -> None:
        try:
            fresh: List[Repo] = []
            refresh_warns = 0
            seen_paths: set = set()
            for folder in folders:
                try:
                    discovered = discover_repos(folder)
                except Exception as e:
                    t = state.tasks.add(f"refresh {folder}")
                    state.tasks.update(t, "warn", first_line(str(e)))
                    refresh_warns += 1
                    discovered = []
                for r in discovered:
                    if r.path in seen_paths:
                        continue
                    seen_paths.add(r.path)
                    fresh.append(r)
            fresh_by_path = {r.path: r for r in fresh}
            kept_by_path = {r.path: r for r in repos_snapshot if r.path in fresh_by_path}
            next_repos: List[Repo] = []
            for r in fresh:
                next_repos.append(kept_by_path.get(r.path, r))
            next_repos.sort(key=lambda r: (r.rel != ".", r.rel.lower() if r.rel != "." else ""))

            # Newly-discovered repos that weren't in `state.repos` at
            # sync-acquire time need their own claim. Membership check
            # uses `path` identity rather than `r in acquired` so we
            # don't depend on Repo equality (the dataclass `__eq__`
            # compares many fields and could surprise us).
            acquired_paths = set(acquired_by_path)
            skipped_active_paths = {r.path for r in skipped_active}
            for r in next_repos:
                if r.path in acquired_paths:
                    continue
                if r.path in skipped_active_paths:
                    continue
                if read_only_row_busy_active(state, [r]):
                    skipped_active.append(r)
                    skipped_active_paths.add(r.path)
                    continue
                if _workspace_has_local_mutation(state, [r]):
                    skipped_active.append(r)
                    skipped_active_paths.add(r.path)
                    continue
                claim = RefreshClaim(state, repo=r)
                if claim.acquire():
                    acquired.append(claim)
                    acquired_by_path[r.path] = claim
                    acquired_paths.add(r.path)

            # Refresh only repos we own. Vanished repos (acquired but
            # not in next_repos) are released in the `finally` below
            # without a refresh — they're about to fall off the list
            # anyway. Locked repos (in next_repos but not acquired)
            # are skipped — their current owner is mid-refresh and
            # will leave them in a consistent state.
            repos_to_refresh = [r for r in next_repos if r.path in acquired_paths]

            # Surface a warn for every repo we couldn't lock so a stuck
            # row no longer looks like a silent no-op on Ctrl+R. A
            # legitimately-held lock (fs_watcher fire mid-flight, ~100ms
            # window) emits the same warn; the user can press Ctrl+R
            # again and watch it clear. A wedged row keeps emitting the
            # warn each Ctrl+R, making the stuck state visible instead
            # of leaving the user staring at a forever-spinning row.
            # Force-clearing the lock isn't safe here — the holder may
            # be mid-write to the Repo's lists — so we report state and
            # let the user judge.
            for r in next_repos:
                if r.path in acquired_paths:
                    continue
                t = state.tasks.add(f"{state.task_repo_label(r)}: refresh skipped")
                # Distinguish "active job" (a worker is mutating this
                # repo's working tree right now) from "locked" (refresh
                # lock held — most often by a sibling refresh or
                # fs_watcher fire) so the user sees WHY their Ctrl+R
                # was a no-op on this row.
                if r.path in skipped_active_paths:
                    state.tasks.update(t, "warn", "task in progress")
                else:
                    state.tasks.update(t, "warn", "locked by another worker")
                refresh_warns += 1

            # `fetch_on_manual_refresh` (default off) makes Ctrl+R do
            # a `git fetch --all` per repo BEFORE the local state
            # re-read so the displayed ahead/behind reflects actual
            # upstream rather than the last fetch. Fetch failures don't
            # fail the local refresh, but they are surfaced so stale
            # ahead/behind state isn't mistaken for authoritative data.
            do_fetch = state.fetch_on_manual_refresh
            incremental_link_lock = threading.Lock()
            incremental_link_warned = [False]

            def maybe_publish_incremental_links(r: Repo) -> None:
                if not r.nested_subs and not subtrees:
                    return
                if not refresh_scope.is_active_current(state):
                    return
                with incremental_link_lock:
                    try:
                        _state_link_siblings(state, next_repos, subtrees)
                    except Exception as e:  # noqa: BLE001
                        if incremental_link_warned[0]:
                            return
                        incremental_link_warned[0] = True
                        t = state.tasks.add("refresh links failed")
                        state.tasks.update(t, "warn", first_line(str(e)))
                        nonlocal_refresh_warns[0] += 1

            def refresh_one(r: Repo) -> None:
                if do_fetch:
                    try:
                        rc, _, err = git(r.path, ["fetch", "--all"])
                    except Exception as e:  # noqa: BLE001
                        rc, err = 1, str(e)
                    if rc != 0:
                        t = state.tasks.add(f"{state.task_repo_label(r)}: fetch failed")
                        msg = first_line(err) or "local state only"
                        state.tasks.update(t, "warn", f"{msg}; local state only")
                        nonlocal_refresh_warns[0] += 1
                _refresh_repo_snapshot_into_state(state, r)
                maybe_publish_incremental_links(r)

            def release_after_refresh(r: Repo) -> None:
                claim = acquired_by_path.get(r.path)
                if claim is not None:
                    claim.release()

            nonlocal_refresh_warns = [refresh_warns]
            reconcile_result = reconcile_repos_bounded(
                repos_to_refresh,
                subtrees,
                link_repos=next_repos,
                refresh_fn=refresh_one,
                link_fn=lambda link_repos, link_subtrees: _state_link_siblings(
                    state, link_repos, link_subtrees),
                max_workers=MAX_PARALLEL_GIT_JOBS,
                on_done=release_after_refresh,
                should_link=lambda: refresh_scope.is_active_current(state),
            )
            for failure in reconcile_result.refresh.failures:
                failure.repo.error = failure.message or "refresh failed"
                t = state.tasks.add(
                    f"{state.task_repo_label(failure.repo)}: refresh failed")
                state.tasks.update(t, "warn", failure.repo.error)
                nonlocal_refresh_warns[0] += 1
            refresh_warns = nonlocal_refresh_warns[0]
            if reconcile_result.link_error:
                t = state.tasks.add("refresh links failed")
                state.tasks.update(t, "warn", reconcile_result.link_error)
                refresh_warns += 1

            workspace_current = refresh_scope.update_cache_if_current(state, next_repos)
            if not workspace_current:
                refresh_stale[0] = True

            # Only repaint the live UI when the user is still on the
            # workspace we refreshed — otherwise we'd flash the wrong
            # repo list (and poison the new workspace's cache).
            def reconcile_live_watchers(_state: State) -> None:
                # Reconcile fs-watchers against the new repo set: attach for
                # newly-appeared repos, drop watchers for repos that vanished.
                # Idempotent + safe to call even when the feature flag is off
                # (it stops any existing watchers). Lazy import keeps watchdog
                # off the workers module's import path for tests that stub
                # workers without touching the watcher manager.
                from .fs_watcher import reconcile_repo_watchers

                reconcile_repo_watchers(state)
            refresh_scope.publish_live_if_active(
                state,
                next_repos,
                on_published=reconcile_live_watchers,
            )
            if manual_task is not None:
                refreshed = len(repos_to_refresh)
                skipped = max(0, len(next_repos) - refreshed)
                if refresh_warns:
                    status = "warn"
                else:
                    status = "ok"
                parts = [f"{refreshed} refreshed"]
                if skipped:
                    parts.append(f"{skipped} skipped")
                message = ", ".join(parts)
                state.tasks.update(manual_task, status, message)
                manual_task_terminal[0] = True
                refresh_outcome[0] = _workflow_poll_outcome(status, message)
            elif refresh_warns:
                refresh_outcome[0] = JobTaskOutcome(
                    JobStatus.WARN,
                    "refresh completed with warnings",
                )
        finally:
            if manual_task is not None and not manual_task_terminal[0]:
                state.tasks.update(manual_task, "fail", "refresh aborted")
                manual_task_terminal[0] = True
                refresh_outcome[0] = JobTaskOutcome(
                    JobStatus.FAIL,
                    "refresh aborted",
                )
            # Release any claims not already released by refresh_one:
            # vanished repos, repos acquired after discovery but skipped
            # before refresh, or paths interrupted by an exception. Over-
            # release is guarded by RefreshClaim.release().
            for claim in acquired:
                claim.release()
            run_pending = False
            with _inline_refresh_lock:
                run_pending = _inline_refresh_queue.complete(
                    target_idx,
                    active_current=state.active_workspace_index == target_idx,
                    stale_result=refresh_stale[0],
                )
                _sync_inline_refresh_flags_locked()
            outcome = refresh_outcome[0]
            if outcome.status in (JobStatus.FAIL, JobStatus.WARN):
                state.job_registry.finish(job, outcome.status, outcome.message)
            if run_pending:
                kick_off_inline_refresh(state)

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="manual-refresh" if manual else "refresh",
            label="refresh workspace",
            local_mutation=False,
            repo_keys=tuple(
                str(claim.repo.path)
                for claim in acquired
                if claim.repo is not None
            ),
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        if manual_task is not None and not manual_task_terminal[0]:
            state.tasks.update(manual_task, "fail", first_line(job.message))
            manual_task_terminal[0] = True
        for claim in acquired:
            claim.release()
        with _inline_refresh_lock:
            _inline_refresh_queue.release(target_idx)
            _sync_inline_refresh_flags_locked()


def kick_off_pull_all(state: State) -> None:
    """Ctrl+P entry point — run `git pull --ff-only` against every
    repo in the active workspace that has an upstream. Repos without
    an upstream are silently skipped (no noise for local-only repos).
    Per-repo task rows surface the outcome; after every pull lands,
    state is re-read so the displayed ahead/behind reflects the new
    HEAD.

    Refusing on non-FF is deliberate (matches the user's mental model
    of "pull all" as a no-merge-commit-here gesture) — divergent
    branches surface a fail task and the user can either resolve them
    individually via the action menu's Pull (which DOES allow a merge
    fallback) or via shell.

    Per-repo refresh slots are claimed non-blocking — a repo whose
    lock is held by another source (commit pipeline, fs_watcher,
    action menu) is skipped. The pull runs in parallel up to
    MAX_PARALLEL_GIT_JOBS, mirroring the inline-refresh pool."""
    if not state.repos:
        return

    # Parent task surfaces the gesture itself so the user sees that
    # Ctrl+P registered even on a workspace where every repo is
    # already up to date (in which case the per-repo helpers add no
    # task rows by design — see `_pull_prefer_ff_then_merge`). The
    # parent gets a summary `n/total` count on completion so a quick
    # glance tells the user what landed.
    parent_task = state.tasks.add("pull all")
    snapshot_repos = list(state.repos)
    snapshot_subtrees = list(state.subtrees)

    acquired: List[RefreshClaim] = []
    acquired_repos: List[Repo] = []
    n_skipped_active = 0
    for r in snapshot_repos:
        # Local mutation ownership skips repos that have a live commit / push /
        # smart-sync worker so pull-all doesn't compete with mid-flight work.
        # Treated the same as the lock-held case for the summary count below.
        if _workspace_has_local_mutation(state, [r]):
            n_skipped_active += 1
            continue
        claim = RefreshClaim(state, repo=r)
        if claim.acquire():
            acquired.append(claim)
            acquired_repos.append(r)

    n_total = len(snapshot_repos)
    n_skipped_locked = n_total - len(acquired_repos) - n_skipped_active
    # Track outcomes from worker threads — use a lock since
    # ThreadPoolExecutor runs `pull_one` concurrently. ints, but
    # we wrap reads/writes in a tiny critical section so the final
    # summary count is stable.
    counters_lock = threading.Lock()
    n_pulled = [0]
    n_up_to_date = [0]
    n_skipped_no_upstream = [0]
    n_failed = [0]

    def worker(job: Job) -> None:
        try:

            def pull_one(r: Repo) -> None:
                name = state.task_repo_label(r)
                # Skip silently when there's no upstream — pulling
                # against nothing isn't a meaningful op and the
                # task row would just be noise on a workspace with
                # any local-only repos. Still tracked in the
                # summary so the user sees "5 had no upstream".
                rc, out, _ = git(
                    r.path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
                )
                if rc != 0 or not out.strip():
                    with counters_lock:
                        n_skipped_no_upstream[0] += 1
                    return
                # Snapshot HEAD around the pull so the summary can
                # distinguish "actually pulled new commits" from
                # "already up-to-date" — `_pull_prefer_ff_then_merge`
                # returns True for both and the count would otherwise
                # collapse them into one bucket (an earlier version
                # of the summary always reported 0 up-to-date because
                # ok_count = total - pulled - failed - skipped was
                # provably zero).
                _, before, _ = git(r.path, ["rev-parse", "HEAD"])
                ok = _pull_prefer_ff_then_merge(
                    r.path, state.tasks, name, allow_merge_fallback=False, parent_task=parent_task
                )
                _, after, _ = git(r.path, ["rev-parse", "HEAD"])
                with counters_lock:
                    if not ok:
                        n_failed[0] += 1
                    elif before.strip() == after.strip():
                        n_up_to_date[0] += 1
                    else:
                        n_pulled[0] += 1

            if acquired_repos:
                max_workers = min(len(acquired_repos), MAX_PARALLEL_GIT_JOBS)
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    list(ex.map(pull_one, acquired_repos))

            # Re-read state for every repo we touched so ahead/behind,
            # HEAD sha, and dirty flags reflect the post-pull world, then
            # relink the whole workspace snapshot so child rows reconcile
            # against the refreshed parents/canonicals.
            reconcile_result = reconcile_repos_bounded(
                acquired_repos,
                snapshot_subtrees,
                link_repos=snapshot_repos,
                refresh_fn=lambda repo: _refresh_repo_snapshot_into_state(
                    state, repo),
                link_fn=lambda link_repos, link_subtrees: _state_link_siblings(
                    state, link_repos, link_subtrees),
            )
            for failure in reconcile_result.refresh.failures:
                t = state.tasks.add(
                    f"{state.task_repo_label(failure.repo)}: refresh failed",
                    parent=parent_task,
                )
                state.tasks.update(t, "fail", failure.message or "refresh failed")
                with counters_lock:
                    n_failed[0] += 1
            if reconcile_result.link_error:
                t = state.tasks.add("pull all: refresh links failed", parent=parent_task)
                state.tasks.update(t, "fail", reconcile_result.link_error)
                with counters_lock:
                    n_failed[0] += 1
        finally:
            for claim in acquired:
                claim.release()
            # Summarise + close the parent task. Status reflects the
            # worst outcome: fail if anything failed, warn if some
            # repos were locked or had no upstream, ok if everything
            # cleanly pulled or no-op'd.
            parts: List[str] = []
            if n_pulled[0]:
                parts.append(f"{n_pulled[0]} pulled")
            if n_up_to_date[0]:
                parts.append(f"{n_up_to_date[0]} up-to-date")
            if n_skipped_no_upstream[0]:
                parts.append(f"{n_skipped_no_upstream[0]} no upstream")
            if n_skipped_locked:
                parts.append(f"{n_skipped_locked} locked")
            if n_skipped_active:
                parts.append(f"{n_skipped_active} busy")
            if n_failed[0]:
                parts.append(f"{n_failed[0]} failed")
            summary = ", ".join(parts) if parts else "no repos"
            if n_failed[0]:
                status = "fail"
            elif n_skipped_locked or n_skipped_active or n_skipped_no_upstream[0]:
                status = "warn"
            else:
                status = "ok"
            state.tasks.update(parent_task, status, summary)
            if status == "fail":
                state.job_registry.finish(job, JobStatus.FAIL, summary)
            elif status == "warn":
                state.job_registry.finish(job, JobStatus.WARN, summary)

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="pull-all",
            label="pull all",
            local_mutation=bool(acquired_repos),
            repo_keys=tuple(str(r.path) for r in acquired_repos),
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        for claim in acquired:
            claim.release()
        state.tasks.update(parent_task, "fail", first_line(job.message))


def switch_workspace(state: State, new_index: int) -> None:
    """Switch the active workspace. The cheap path — and the one taken
    every time after the first — is to swap `state.repos` to the new
    workspace's `cached_repos`, populated at startup. The swap is
    instant (no re-discovery), and a background `kick_off_inline_refresh`
    fires at the tail to correct any per-repo state (branch, head,
    dirty flags) that drifted while the user was on a different
    workspace — fs_watcher tears down watchers for repos not in the
    current `state.repos`, so changes made to this workspace's repos
    while away are never observed and the cached state would otherwise
    show as stale until the next manual Ctrl+R.

    The cache-miss path covers exactly two cases:
      1. A workspace freshly created at runtime via the creator wizard
         that hasn't been refreshed yet.
      2. A test or out-of-tree caller that built `state.workspaces`
         without populating cached_repos.
    Both fall back to discover-then-async-refresh, with `kick_off_
    inline_refresh`'s gate keeping concurrent refreshes well-behaved.

    No-op when the new index doesn't actually change the active
    workspace."""
    if not state.workspaces:
        return
    new_index %= len(state.workspaces)
    if new_index == state.active_workspace_index:
        return
    # Persist any in-place repo mutations on the way out — kick_off_
    # inline_refresh and the commit pipeline both mutate state.repos
    # in place (status fields, message strings, etc.), and we want
    # the next switch back to surface those changes rather than the
    # stale snapshot taken at startup.
    cur_idx = state.active_workspace_index
    if 0 <= cur_idx < len(state.workspaces):
        state.workspaces[cur_idx].cached_repos = state.repos

    state.active_workspace_index = new_index
    ws = state.workspaces[new_index]

    if ws.cached_repos:
        # Cache hit — instant visual swap to the cached state, then
        # kick a background refresh below. The cached list is the same
        # Python object subsequent kick_off_inline_refresh runs will
        # mutate in place, so a later Ctrl+R (and the background
        # refresh we kick at the tail) updates both `state.repos` and
        # the workspace's cache simultaneously without copying.
        #
        # The refresh matters because fs_watcher only observes events
        # for paths in the *current* `state.repos` — watchers for the
        # previous workspace's repos are torn down on switch, so any
        # edit landing on this workspace's repos while the user was
        # away never produced a refresh. Without the kick the row
        # would keep showing the stale-from-last-visit dirty/branch
        # state until Ctrl+R.
        state.replace_repos(ws.cached_repos, workspace=ws)
    else:
        # Cache miss (newly-added workspace or test-built workspace)
        # must not run discovery/relink in the key handler. Switch the
        # visible workspace immediately, then let a read-only job fill
        # the cache and publish if the user is still looking at it.
        state.replace_repos(ws.cached_repos, workspace=ws)

    state.workspace_name = ws.name

    # Re-apply settings from base config + this workspace's overrides.
    # Imported here to avoid a hard config dependency in workers' module
    # namespace (workers is a leaf used by tests that don't load config).
    from .config import apply_workspace_overrides

    if state.base_config is not None:
        apply_workspace_overrides(state, state.base_config, ws)
    else:
        state.subtrees = list(ws.subtrees)

    # Park focus back on the workspace selector and reset scroll so the
    # new list starts at the top. Tasks panel state is intentionally
    # untouched — running tasks belong to the previous workspace's
    # commits/syncs but the user can let them finish or kill them via
    # the task-detail modal.
    state.selected = -1
    state.body_scroll = 0
    state.field_cursor = 0
    state.task_selected = 0
    state.task_scroll = 0
    state.focused_panel = "repos"

    needs_load = not ws.cached_repos
    _kick_off_workspace_switch(state, load_workspace=needs_load)
    if ws.cached_repos:
        kick_off_inline_refresh(state)


def _discover_workspace_repos_for_switch(folders: List[Path]) -> Tuple[List[Repo], List[str]]:
    """Discover repos for a workspace switch and collect folder warnings."""
    fresh: List[Repo] = []
    warnings: List[str] = []
    seen_paths: set = set()
    for folder in folders:
        try:
            discovered = discover_repos(folder)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{folder}: {first_line(str(exc))}")
            discovered = []
        for repo in discovered:
            if repo.path in seen_paths:
                continue
            seen_paths.add(repo.path)
            fresh.append(repo)
    fresh.sort(key=lambda repo: repo.display_name.lower())
    return fresh, warnings


def _kick_off_workspace_switch(
        state: State,
        *,
        load_workspace: bool,
) -> None:
    """Persist the active workspace and optionally load a cache miss.

    Workspace switching is one user action, so it owns one visible task row even
    when it has two read-only phases: saving the active index and discovering a
    cache-miss workspace.
    """
    label = f"switch workspace: {state.workspace_name}"
    workspaces_snapshot = _snapshot_workspace_settings(state.workspaces)
    active_index_snapshot = state.active_workspace_index
    scope = WorkspaceRefreshScope.capture(state) if load_workspace else None

    job = state.job_registry.start(JobSpec(
        kind="workspace-switch",
        label=label,
        local_mutation=False,
        stale_after_seconds=10.0,
    ))
    bridge = JobTaskBridge(state.tasks, state.job_registry, job)
    task = bridge.add(label)

    def worker(job: Job) -> None:
        from .config import save_workspaces

        bridge.update(task, "running", "saving active workspace")
        try:
            save_workspaces(workspaces_snapshot, active_index_snapshot)
        except OSError as exc:
            bridge.update(task, "fail", f"could not write: {exc}")
            return

        if scope is None:
            bridge.update(task, "ok", "active workspace saved")
            return

        bridge.update(task, "running", "loading workspace")
        repos, warnings = _discover_workspace_repos_for_switch(
            list(scope.folders))
        link_error = ""
        if scope.is_current(state):
            # Freshly discovered repos have no child/sibling snapshot yet.
            reconcile_result = reconcile_repos_bounded(
                [],
                list(scope.subtrees),
                link_repos=repos,
                refresh_fn=lambda repo: _refresh_repo_snapshot_into_state(
                    state, repo),
                link_fn=lambda link_repos, link_subtrees: _state_link_siblings(
                    state, link_repos, link_subtrees),
            )
            link_error = reconcile_result.link_error
        scope.update_cache_if_current(state, repos)
        published = scope.publish_live_if_active(state, repos)
        if published:
            kick_off_inline_refresh(state)
        messages = list(warnings)
        if link_error:
            messages.append(link_error)
        if messages:
            message = "; ".join(messages)
            bridge.update(task, "warn", message)
            return
        if not scope.is_current(state):
            bridge.update(task, "warn", "workspace changed before load completed")
            return
        bridge.update(task, "ok", f"{len(repos)} repos")

    def run(job_arg: Job) -> None:
        worker(job_arg)
        bridge.finish_failed_or_warned_job(state.job_registry, job_arg)

    thread = start_job_thread(state.job_registry, job, run)
    if thread is None:
        bridge.update(task, "fail", first_line(job.message))


# ---------- Update check (GitHub Releases API) ---------------------------


def kick_off_check_for_updates(state: State, menu) -> None:
    """Query the GitHub Releases API for the latest tag in a daemon
    thread, then write the result back onto `menu` (an WorkspaceMenu) so
    the modal can re-render. Synchronously flips
    `menu.update_check = "checking"` before returning so the next
    redraw shows the spinner.

    Result lifecycle on `menu`:
      - `update_check`        — "checking" → "done" / "failed"
      - `latest_version`      — set on "done" (e.g. "v0.8.9")
      - `update_check_error`  — set on "failed" (short reason
        suitable for a single hint-line)

    No exceptions ever escape — network / HTTP / decode errors all
    land in `update_check_error`. Stdlib-only so the menu doesn't
    drag in a `requests` dependency. Curses-screen protection
    against noisy interpreter-side warnings (pyenv's broken-OpenSSL
    `blake2b/blake2s not found` errors from `hashlib`'s logging,
    for example) lives at the curses-entrypoint level
    (`idlegit.run`), which redirects fd 2 for the whole session —
    redirecting `sys.stderr` here wouldn't catch them because
    `logging.StreamHandler` captured the original stderr fd at
    startup."""
    import json
    import urllib.error
    import urllib.request
    from .config import GITHUB_REPO, VERSION

    menu.update_check = "checking"
    menu.update_check_error = ""
    menu.latest_version = ""

    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    user_agent = f"idlegit/{VERSION} (+https://github.com/{GITHUB_REPO})"

    def worker(job: Job) -> None:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or not data.get("tag_name"):
                menu.update_check_error = "release response missing tag_name"
                menu.update_check = "failed"
                state.job_registry.finish(
                    job, JobStatus.WARN, menu.update_check_error)
                return
            menu.latest_version = str(data["tag_name"])
            menu.update_check = "done"
        except urllib.error.HTTPError as e:
            # GitHub returns 404 from /releases/latest when the repo
            # exists but has zero published releases — not the same
            # as "the repo isn't there." Surface that as a softer
            # informational state so the menu reads "No releases
            # published yet" instead of a scary HTTP 404.
            if e.code == 404:
                menu.latest_version = ""
                menu.update_check_error = ""
                menu.update_check = "no_releases"
            else:
                menu.update_check_error = f"HTTP {e.code} {e.reason}"
                menu.update_check = "failed"
                state.job_registry.finish(
                    job, JobStatus.WARN, menu.update_check_error)
        except urllib.error.URLError as e:
            menu.update_check_error = f"network: {e.reason}"
            menu.update_check = "failed"
            state.job_registry.finish(
                job, JobStatus.WARN, menu.update_check_error)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            menu.update_check_error = f"parse error: {e}"
            menu.update_check = "failed"
            state.job_registry.finish(
                job, JobStatus.WARN, menu.update_check_error)
        except OSError as e:
            menu.update_check_error = f"i/o: {e}"
            menu.update_check = "failed"
            state.job_registry.finish(
                job, JobStatus.WARN, menu.update_check_error)

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="update-check",
            label="check for updates",
            local_mutation=False,
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        menu.update_check_error = first_line(job.message)
        menu.update_check = "failed"


# ---------- App menu background jobs ------------------------------------


def _kick_off_read_only_task(
        state: State,
        *,
        kind: str,
        label: str,
        worker: Callable[[Job, Task, JobTaskBridge], None],
        on_start_failure: Optional[Callable[[str], None]] = None,
) -> None:
    job = state.job_registry.start(JobSpec(
        kind=kind,
        label=label,
        local_mutation=False,
    ))
    bridge = JobTaskBridge(state.tasks, state.job_registry, job)
    task = bridge.add(label)

    def run(job_arg: Job) -> None:
        worker(job_arg, task, bridge)
        bridge.finish_failed_or_warned_job(state.job_registry, job_arg)

    thread = start_job_thread(state.job_registry, job, run)
    if thread is None:
        if on_start_failure is not None:
            on_start_failure(job.message)
        bridge.update(task, "fail", job.message)


def _kick_off_app_menu_task(
        state: State,
        *,
        kind: str,
        label: str,
        worker: Callable[[Job, Task, JobTaskBridge], None],
        on_start_failure: Optional[Callable[[str], None]] = None,
) -> None:
    _kick_off_read_only_task(
        state,
        kind=kind,
        label=label,
        worker=worker,
        on_start_failure=on_start_failure,
    )


def kick_off_app_menu_status_refresh(state: State, menu: AppMenu) -> None:
    if menu.ssh_status_checking or menu.task_log_checking:
        return
    menu.ssh_status_checking = True
    menu.task_log_checking = True

    def worker(
            job: Job,
            task: Task,
            bridge: JobTaskBridge,
    ) -> None:
        from .ssh import agent_status_label, keys_loaded_label, ssh_tools_status
        from .task_log import (
            format_size, task_log_line_count, task_log_size_bytes,
        )

        try:
            tools = ssh_tools_status()
            menu.ssh_tools_missing = tools.missing_tools
            menu.ssh_status = agent_status_label()
            menu.ssh_keys = keys_loaded_label()
            size_bytes = task_log_size_bytes(state.task_log_path)
            if size_bytes <= 0:
                menu.task_log_size = "0 B (empty)"
            else:
                lines = task_log_line_count(state.task_log_path)
                menu.task_log_size = f"{format_size(size_bytes)} ({lines:,} lines)"
            bridge.update(task, "ok", "loaded")
        except Exception as exc:  # noqa: BLE001
            menu.ssh_tools_missing = []
            menu.ssh_status = first_line(str(exc)) or "check failed"
            menu.ssh_keys = "unknown"
            menu.task_log_size = "check failed"
            bridge.update(task, "warn", menu.ssh_status)
            state.job_registry.finish(job, JobStatus.WARN, menu.ssh_status)
        finally:
            menu.ssh_status_checking = False
            menu.task_log_checking = False

    _kick_off_app_menu_task(
        state,
        kind="app-menu-status",
        label="load app menu status",
        worker=worker,
    )


def kick_off_task_log_toggle(state: State, enabled: bool) -> None:
    def load_defaults(
            _job: Job,
            task: Task,
            bridge: JobTaskBridge,
    ) -> None:
        from .config import set_conf_value
        from .task_log import unwire_task_log, wire_task_log

        if enabled:
            wire_task_log(state)
        else:
            unwire_task_log(state)
        if set_conf_value("task_log_enabled",
                          "true" if enabled else "false"):
            bridge.update(
                task, "ok",
                "logging on" if enabled else "logging off")
        else:
            bridge.update(
                task, "warn",
                "applied but conf write failed — won't persist across restart")
        if state.app_menu is not None:
            state.app_menu.task_log_size = "checking"

    _kick_off_app_menu_task(
        state,
        kind="task-log-toggle",
        label="enable task logging" if enabled else "disable task logging",
        worker=load_defaults,
    )


def kick_off_ssh_agent_toggle(state: State, enabled: bool) -> None:
    def load_defaults(
            _job: Job,
            task: Task,
            bridge: JobTaskBridge,
    ) -> None:
        from .config import set_conf_value

        if set_conf_value("auto_start_ssh_agent",
                          "true" if enabled else "false"):
            bridge.update(
                task, "ok",
                "will start agent on launch" if enabled
                else "agent autostart off")
        else:
            bridge.update(
                task, "warn",
                "applied but conf write failed — won't persist across restart")

    _kick_off_app_menu_task(
        state,
        kind="ssh-agent-toggle",
        label=(
            "enable ssh-agent autostart" if enabled
            else "disable ssh-agent autostart"
        ),
        worker=load_defaults,
    )


def kick_off_ssh_add_keys(state: State) -> None:
    def worker(
            _job: Job,
            task: Task,
            bridge: JobTaskBridge,
    ) -> None:
        from .ssh import add_default_keys_to_agent, ensure_ssh_agent, ssh_tools_status

        tools = ssh_tools_status()
        if not tools.has_ssh_add:
            bridge.update(task, "fail", "ssh-add not on PATH — install OpenSSH")
            return
        if state.auto_start_ssh_agent:
            ensure_ssh_agent(True)
        added, errors = add_default_keys_to_agent()
        if added and not errors:
            bridge.update(task, "ok", f"{added} key(s) loaded")
        elif added:
            bridge.update(task, "warn", f"{added} loaded; {'; '.join(errors)}")
        elif errors:
            bridge.update(task, "fail", "; ".join(errors))
        else:
            bridge.update(task, "warn", "no default keys found in ~/.ssh")
        if state.app_menu is not None:
            state.app_menu.ssh_status = "checking"
            state.app_menu.task_log_size = "checking"

    _kick_off_app_menu_task(
        state,
        kind="ssh-add-keys",
        label="ssh-add default keys",
        worker=worker,
    )


def kick_off_ssh_keygen_prepare(state: State, modal: SshKeygenModal) -> None:
    modal.preparing = True
    modal.error = ""

    def load_defaults(
            _job: Job,
            task: Task,
            bridge: JobTaskBridge,
    ) -> None:
        from .ssh import default_ed25519_path, git_user_email, ssh_tools_status

        tools = ssh_tools_status()
        if not tools.has_ssh_keygen:
            modal.email = ""
            modal.key_path_text = ""
            modal.key_path_placeholder = "ssh-keygen not found"
            modal.error = "ssh-keygen not on PATH - install OpenSSH"
            bridge.update(task, "fail", modal.error)
            return
        email = git_user_email()
        default_path = default_ed25519_path()
        modal.email = email
        modal.key_path_text = str(default_path)
        modal.key_path_placeholder = str(default_path)
        bridge.update(task, "ok", "ready")

    def worker(
            job: Job,
            task: Task,
            bridge: JobTaskBridge,
    ) -> None:
        try:
            load_defaults(job, task, bridge)
        finally:
            modal.preparing = False

    def on_start_failure(message: str) -> None:
        modal.preparing = False
        modal.error = message

    _kick_off_app_menu_task(
        state,
        kind="ssh-keygen-prepare",
        label="prepare SSH key modal",
        worker=worker,
        on_start_failure=on_start_failure,
    )


def kick_off_auto_refresh_toggle(state: State, enabled: bool) -> None:
    def worker(
            _job: Job,
            task: Task,
            bridge: JobTaskBridge,
    ) -> None:
        from .config import set_conf_value
        from .fs_watcher import reconcile_repo_watchers

        try:
            reconcile_repo_watchers(state)
        except Exception as exc:  # noqa: BLE001
            bridge.update(task, "fail", first_line(str(exc)))
            raise
        if set_conf_value("auto_refresh_on_fs_change",
                          "true" if enabled else "false"):
            bridge.update(
                task, "ok",
                "watching files" if enabled else "Ctrl+R only")
        else:
            bridge.update(
                task, "warn",
                "applied but conf write failed — won't persist across restart")

    _kick_off_app_menu_task(
        state,
        kind="auto-refresh-toggle",
        label="enable auto-refresh" if enabled else "disable auto-refresh",
        worker=worker,
    )


def kick_off_auto_refresh_debounce_save(state: State, value: int) -> None:
    def worker(
            _job: Job,
            task: Task,
            bridge: JobTaskBridge,
    ) -> None:
        from .config import set_conf_value

        if set_conf_value("auto_refresh_debounce_ms", str(value)):
            bridge.update(task, "ok", "saved")
        else:
            bridge.update(
                task, "warn",
                "applied but conf write failed — won't persist across restart")

    _kick_off_app_menu_task(
        state,
        kind="auto-refresh-debounce",
        label=f"debounce -> {value} ms",
        worker=worker,
    )


def kick_off_periodic_refresh_save(state: State, value: float, label: str) -> None:
    def worker(
            _job: Job,
            task: Task,
            bridge: JobTaskBridge,
    ) -> None:
        from .config import set_conf_value

        if set_conf_value("periodic_refresh_seconds", f"{value:g}"):
            bridge.update(task, "ok", "saved")
        else:
            bridge.update(
                task, "warn",
                "applied but conf write failed — won't persist across restart")

    _kick_off_app_menu_task(
        state,
        kind="periodic-refresh-setting",
        label=f"periodic refresh -> {label}",
        worker=worker,
    )


def kick_off_auto_remove_completed_save(
        state: State,
        value: float,
        label: str,
) -> None:
    def worker(
            _job: Job,
            task: Task,
            bridge: JobTaskBridge,
    ) -> None:
        from .config import set_conf_value

        if set_conf_value(
                "auto_remove_completed_tasks_after_interval",
                f"{value:g}"):
            bridge.update(task, "ok", "saved")
        else:
            bridge.update(
                task, "warn",
                "applied but conf write failed — won't persist across restart")

    _kick_off_app_menu_task(
        state,
        kind="task-auto-remove-setting",
        label=f"task auto-remove -> {label}",
        worker=worker,
    )


def kick_off_open_task_log(state: State) -> None:
    path = state.task_log_path

    def worker(
            job: Job,
            task: Task,
            bridge: JobTaskBridge,
    ) -> None:
        from .task_log import open_task_log

        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            except OSError as exc:
                message = f"could not create log: {exc}"
                bridge.update(task, "fail", message)
                state.job_registry.finish(job, JobStatus.FAIL, message)
                return
        if open_task_log(path):
            bridge.update(task, "ok", "opened")
        else:
            bridge.update(task, "warn", "no opener available")
            state.job_registry.finish(job, JobStatus.WARN, "no opener available")

    _kick_off_app_menu_task(
        state,
        kind="open-task-log",
        label=f"open {path.name}",
        worker=worker,
    )


def kick_off_clear_task_log(state: State) -> None:
    path = state.task_log_path

    def worker(
            _job: Job,
            task: Task,
            bridge: JobTaskBridge,
    ) -> None:
        from .task_log import clear_task_log

        if clear_task_log(path):
            bridge.update(task, "ok", "log cleared")
            if state.app_menu is not None:
                state.app_menu.task_log_size = "checking"
        else:
            bridge.update(task, "fail", "could not write log file")

    _kick_off_app_menu_task(
        state,
        kind="clear-task-log",
        label=f"clear {path.name}",
        worker=worker,
    )


# ---------- Safe-merge: stash → merge → resolve → commit → push → sync -----
#
# The interactive resolution lives in ui/safe_merge.py; these workers do the
# git work at each phase boundary and publish every step to the task panel.
# All Cardinal-Rule safe: see core/git_ops.py's safe-merge section. The only
# data-discarding step (dropping the backup stash) is gated behind an
# off-by-default user checkbox on the confirm screen.


def _safe_merge_build_decisions(screen: SafeMergeScreen) -> None:
    """(Re)build the flat ↑/↓ decision list from the parsed files: one
    entry per text hunk and per whole-file binary pick. Manual files
    contribute none — they can't be resolved inside the dialog."""
    decisions: List[Tuple[int, int]] = []
    for fi, cf in enumerate(screen.files):
        if cf.kind == "text":
            for hi in range(len(cf.hunks)):
                decisions.append((fi, hi))
        elif cf.kind == "binary":
            decisions.append((fi, -1))
    screen.decisions = decisions
    if screen.focus >= len(decisions):
        screen.focus = max(0, len(decisions) - 1)


def _safe_merge_release_locks(screen: SafeMergeScreen) -> None:
    """Release the refresh slots claimed for the flow. Idempotent so the
    confirm worker and the abort path can both call it safely."""
    if screen.repo_refresh_claim is not None:
        screen.repo_refresh_claim.release()
        screen.repo_refresh_claim = None
        screen.repo_locked = False
    if screen.child_refresh_claim is not None:
        screen.child_refresh_claim.release()
        screen.child_refresh_claim = None
        screen.child_locked = False


def _safe_merge_refresh_targets(state: State, screen: SafeMergeScreen) -> ReconcileResult:
    """Re-query the affected repo (and rebuild sibling links) so the row
    icons reflect the post-merge state. Per-repo guarded so a single
    failing refresh can't strand the teardown."""
    refresh_targets: List[Repo] = []
    repo = screen.target_repo
    if repo is not None:
        refresh_targets.append(repo)
    if screen.target_parent is not None and screen.target_parent is not repo:
        refresh_targets.append(screen.target_parent)
    if screen.snapshot_repos:
        repos = screen.snapshot_repos
        subtrees = screen.snapshot_subtrees
    else:
        repos = state.repos
        subtrees = state.subtrees
    return reconcile_repos_bounded(
        refresh_targets,
        subtrees,
        link_repos=repos,
        refresh_fn=lambda repo: _refresh_repo_snapshot_into_state(state, repo),
        link_fn=lambda link_repos, link_subtrees: _state_link_siblings(
            state, link_repos, link_subtrees),
    )


def _call_safe_merge_with_bridge(
        fn: Callable[..., object],
        state: State,
        screen: SafeMergeScreen,
        task_bridge: JobTaskBridge,
) -> object:
    try:
        return fn(state, screen, task_bridge=task_bridge)
    except TypeError as exc:
        if "task_bridge" not in str(exc):
            raise
        return fn(state, screen)


def safe_merge_abort(state: State, screen: SafeMergeScreen) -> None:
    """Tear down after the user dismisses the dialog. Cardinal-Rule safe:
    we NEVER run `git merge --abort` (it resets the working tree). An
    in-progress merge is simply left in place — its conflicts stay in the
    tree and the backup stash is preserved, so the user can finish by hand
    or re-open safe-merge (which adopts the existing conflicts)."""
    screen.cancel_event.set()
    header = screen.header_task
    if header is not None and not screen.header_terminal:
        if screen.phase == "confirm":
            # The merge commit exists; only push/sync was skipped.
            state.tasks.update(header, "warn", "merge committed — push skipped")
        elif merge_head_sha(screen.target_path):
            state.tasks.update(
                header, "warn", "merge left in progress — re-open safe-merge to finish"
            )
        else:
            state.tasks.update(header, "warn", "cancelled")
        screen.header_terminal = True
    _safe_merge_refresh_targets(state, screen)
    _safe_merge_release_locks(screen)


def kick_off_safe_merge(
    state: State,
    *,
    target_label: str,
    target_path: Path,
    target_repo: Optional[Repo],
    target_parent: Optional[Repo],
    target_child: Optional[ChildRef] = None,
    merge_ref: str = "",
    branch_label: str = "",
) -> bool:
    """Open the safe-merge dialog for `target_path`. `merge_ref` is the ref
    to merge in; pass "" to ADOPT an already in-progress merge (resolve its
    existing conflicts). Claims the target's refresh slot for the whole
    flow, builds the screen, and spawns the begin worker. Returns True when
    the dialog opened (False if the target was busy)."""
    if target_child is None:
        target_child = _find_child_at(target_parent, target_path)
    repo_claim: Optional[RefreshClaim] = None
    child_claim: Optional[RefreshClaim] = None
    if target_repo is not None:
        repo_claim = RefreshClaim(state, repo=target_repo)
        if not repo_claim.acquire():
            t = state.tasks.add(f"safe-merge {target_label}: skipped")
            state.tasks.update(t, "warn", "refresh in progress — try again")
            return False
    if target_child is not None:
        child_claim = RefreshClaim(state, child=target_child)
        if not child_claim.acquire():
            if repo_claim is not None:
                repo_claim.release()
            t = state.tasks.add(f"safe-merge {target_label}: skipped")
            state.tasks.update(t, "warn", "refresh in progress — try again")
            return False

    header = state.tasks.add(
        f"safe-merge {target_label}"
        + (f": merge {merge_ref}" if merge_ref else ": resolve conflicts")
    )
    screen = SafeMergeScreen(
        target_label=target_label,
        target_path=target_path,
        target_repo=target_repo,
        target_parent=target_parent,
        target_child=target_child,
        merge_ref=merge_ref,
        is_tracked_submodule=(
            target_child is not None or bool(target_repo is not None and target_repo.siblings)
        ),
        confirm_remove_stash=state.auto_remove_backup_stash_after_merge,
        header_task=header,
        repo_locked=repo_claim is not None,
        child_locked=child_claim is not None,
        repo_refresh_claim=repo_claim,
        child_refresh_claim=child_claim,
        snapshot_repos=list(state.repos),
        snapshot_subtrees=list(state.subtrees),
        phase="preparing",
    )
    state.safe_merge = screen

    repo_keys = (str(target_repo.path),) if target_repo is not None else ()
    child_keys = (str(target_child.nested_path),) if target_child is not None else ()
    job = state.job_registry.start(JobSpec(
        kind="safe-merge-begin",
        label=header.label,
        local_mutation=True,
        repo_keys=repo_keys,
        child_keys=child_keys,
    ))
    task_bridge = JobTaskBridge(state.tasks, state.job_registry, job)
    task_bridge.attach(header)

    def worker(job: Job) -> None:
        try:
            outcome = _call_safe_merge_with_bridge(
                _safe_merge_begin_worker,
                state,
                screen,
                task_bridge,
            )
        except Exception as e:  # noqa: BLE001
            msg = first_line(str(e))
            task_bridge.update(header, "fail", msg)
            screen.header_terminal = True
            state.job_registry.finish(job, JobStatus.FAIL, msg)
            return
        if outcome.status is not None:
            screen.header_terminal = True
            state.job_registry.finish(job, outcome.status, outcome.message)

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    thread = start_job_thread(
        state.job_registry,
        job,
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        task_bridge.update(header, "fail", first_line(job.message))
        screen.header_terminal = True
        state.safe_merge = None
        _safe_merge_release_locks(screen)
        return False
    return True


def _safe_merge_begin_worker(
        state: State,
        screen: SafeMergeScreen,
        task_bridge: Optional[JobTaskBridge] = None,
) -> JobTaskOutcome:
    """Phase 1: (optionally) stash a backup, start the merge, parse the
    conflicts. For a clean (conflict-free) merge, commit straight away and
    jump to the confirm screen."""
    tasks = task_bridge or JobTaskBridge(state.tasks)
    header = screen.header_task
    path = screen.target_path
    try:
        adopting = not screen.merge_ref
        if not adopting:
            # Backup stash of the pre-merge working tree. "if possible" —
            # a clean tree yields nothing to stash and that's fine.
            stash_name = time.strftime("pre-merge-at-%Y-%m-%d-%H:%M")
            t = tasks.add("  ↳ backup stash", parent=header)
            status, detail = create_named_stash(path, stash_name)
            if status == "created":
                screen.backup_stash_name = stash_name
                tasks.update(t, "ok", stash_name)
            elif status == "empty":
                tasks.update(t, "ok", "clean tree — nothing to back up")
            else:
                tasks.update(t, "fail", detail)
                screen.error = f"backup stash failed: {detail}"
                screen.phase = "error"
                return JobTaskOutcome(JobStatus.FAIL, screen.error)

            t = tasks.add(f"  ↳ merge {screen.merge_ref}", parent=header)
            rc, _out, err = begin_safe_merge(path, screen.merge_ref)
            # rc != 0 is the EXPECTED conflict path; only a hard failure
            # with no merge actually started is a real error.
            if rc != 0 and merge_head_sha(path) is None:
                low = (err or "").lower()
                if "already up to date" in low:
                    tasks.update(t, "ok", "already up to date")
                    screen.error = "already up to date — nothing to merge"
                    outcome = JobTaskOutcome(JobStatus.WARN, screen.error)
                else:
                    tasks.update(t, "fail", first_line(err))
                    screen.error = f"merge could not start: {first_line(err)}"
                    outcome = JobTaskOutcome(JobStatus.FAIL, screen.error)
                screen.phase = "error"
                return outcome
            tasks.update(t, "ok", "conflicts to resolve" if rc != 0 else "clean merge")

        if screen.cancel_event.is_set():
            return JobTaskOutcome(JobStatus.CANCELLED, "cancelled")

        # Describe both sides richly for the version labels.
        screen.ours = describe_merge_side(path, "HEAD", "ours")
        screen.theirs = describe_merge_side(
            path, "MERGE_HEAD", "theirs", branch_label=screen.merge_ref
        )

        screen.files = parse_safe_merge_conflicts(path)
        _safe_merge_build_decisions(screen)

        if not screen.files:
            # Conflict-free merge that's staged and ready — or an adopted
            # repo that's actually clean. If a merge is in progress, commit
            # it; otherwise there's nothing to do.
            if merge_head_sha(path) is not None:
                return _safe_merge_do_commit(state, screen, task_bridge=tasks)
            else:
                screen.error = "no conflicts and no merge in progress"
                screen.phase = "error"
                return JobTaskOutcome(JobStatus.WARN, screen.error)

        screen.phase = "resolve"
        return JobTaskOutcome()
    except Exception as e:  # noqa: BLE001
        screen.error = f"safe-merge failed: {e}"
        screen.phase = "error"
        if header is not None:
            tasks.update(header, "fail", first_line(str(e)))
        return JobTaskOutcome(JobStatus.FAIL, first_line(str(e)))


def _safe_merge_do_commit(
        state: State,
        screen: SafeMergeScreen,
        task_bridge: Optional[JobTaskBridge] = None,
) -> JobTaskOutcome:
    """Write every chosen resolution, stage it, and create the merge
    commit. On success, advance to the confirm screen; on a remaining
    (manual) conflict, drop back to resolve with a clear note."""
    tasks = task_bridge or JobTaskBridge(state.tasks)
    header = screen.header_task
    path = screen.target_path

    written = 0
    for cf in screen.files:
        if cf.kind == "manual":
            continue
        t = tasks.add(f"  ↳ resolve {cf.path}", parent=header)
        ok, detail = write_conflict_resolution(path, cf)
        tasks.update(t, "ok" if ok else "fail", detail)
        if ok:
            written += 1
        else:
            screen.status_note = f"could not write {cf.path}: {detail}"
            screen.phase = "resolve"
            return JobTaskOutcome(JobStatus.FAIL, screen.status_note)

    remaining = remaining_conflict_paths(path)
    if remaining:
        manual = ", ".join(remaining[:3]) + (" …" if len(remaining) > 3 else "")
        screen.status_note = (
            f"{len(remaining)} file(s) need manual resolution outside idlegit: {manual}"
        )
        screen.phase = "resolve"
        return JobTaskOutcome(JobStatus.WARN, screen.status_note)

    t = tasks.add("  ↳ merge commit", parent=header)
    rc, _out, err = complete_safe_merge_commit(path)
    if rc != 0:
        tasks.update(t, "fail", first_line(err))
        screen.status_note = f"commit failed: {first_line(err)}"
        # With no decisions to return to (a conflict-free merge that the
        # commit step still rejected, e.g. a pre-commit hook) the resolve
        # view would be empty and confusing — show the dismissable error
        # screen instead.
        if not screen.decisions:
            screen.error = f"merge commit failed: {first_line(err)}"
            screen.phase = "error"
        else:
            screen.phase = "resolve"
        return JobTaskOutcome(JobStatus.FAIL, first_line(err))
    sha, subject = head_short_info(path)
    tasks.update(t, "ok", sha)
    screen.commit_sha = sha
    screen.commit_subject = subject
    screen.confirm_focus = 0
    screen.phase = "confirm"
    return JobTaskOutcome(JobStatus.OK, sha)


def kick_off_safe_merge_finalize(state: State, screen: SafeMergeScreen) -> None:
    """Called from the dialog when the user finishes picking sides. Spawns
    the commit worker (phase → committing → confirm)."""
    screen.phase = "committing"
    repo_keys = (str(screen.target_repo.path),) if screen.target_repo is not None else ()
    child_keys = (str(screen.target_child.nested_path),) if screen.target_child is not None else ()
    job = state.job_registry.start(JobSpec(
        kind="safe-merge-finalize",
        label="safe-merge finalize",
        local_mutation=True,
        repo_keys=repo_keys,
        child_keys=child_keys,
    ))
    task_bridge = JobTaskBridge(state.tasks, state.job_registry, job)
    if screen.header_task is not None:
        task_bridge.attach(screen.header_task)

    def worker(job: Job) -> None:
        try:
            outcome = _call_safe_merge_with_bridge(
                _safe_merge_do_commit,
                state,
                screen,
                task_bridge,
            )
        except Exception as e:  # noqa: BLE001
            msg = first_line(str(e))
            screen.status_note = f"commit failed: {msg}"
            screen.phase = "resolve"
            state.job_registry.finish(job, JobStatus.FAIL, msg)
            return
        if outcome.status is None:
            return
        state.job_registry.finish(job, outcome.status, outcome.message)

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    thread = start_job_thread(
        state.job_registry,
        job,
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        screen.status_note = first_line(job.message)
        screen.phase = "resolve"


def kick_off_safe_merge_confirm(state: State, screen: SafeMergeScreen) -> None:
    """Called from the confirm screen. Pushes the merge commit (if chosen),
    syncs sibling submodule checkouts + bumps parent pointers (when the
    target is a tracked submodule), and drops the backup stash (only when
    the user ticked the box). phase → confirming → done."""
    screen.phase = "confirming"
    repo_keys = (str(screen.target_repo.path),) if screen.target_repo is not None else ()
    child_keys = (str(screen.target_child.nested_path),) if screen.target_child is not None else ()
    job = state.job_registry.start(JobSpec(
        kind="safe-merge-confirm",
        label="safe-merge confirm",
        local_mutation=True,
        repo_keys=repo_keys,
        child_keys=child_keys,
    ))
    task_bridge = JobTaskBridge(state.tasks, state.job_registry, job)
    if screen.header_task is not None:
        task_bridge.attach(screen.header_task)

    def worker(job: Job) -> None:
        tasks = task_bridge
        header = screen.header_task
        path = screen.target_path
        outcome = JobTaskOutcome(JobStatus.OK, screen.commit_sha)
        try:
            pushed = False
            if screen.confirm_push:
                pushed = bool(_call_safe_merge_with_bridge(
                    _safe_merge_push,
                    state,
                    screen,
                    task_bridge,
                ))
            if pushed and screen.is_tracked_submodule:
                outcome = _merge_job_task_outcome(
                    outcome,
                    _call_safe_merge_with_bridge(
                        _safe_merge_sync_submodule,
                        state,
                        screen,
                        task_bridge,
                    ),
                )
            if screen.confirm_remove_stash and screen.backup_stash_name:
                t = tasks.add("  ↳ drop backup stash", parent=header)
                ok, detail = drop_named_stash(path, screen.backup_stash_name)
                tasks.update(t, "ok" if ok else "warn", detail)
                if not ok:
                    outcome = _merge_job_task_outcome(
                        outcome,
                        JobTaskOutcome(JobStatus.WARN, detail),
                    )
            if screen.confirm_push and not pushed:
                outcome = _merge_job_task_outcome(
                    outcome,
                    JobTaskOutcome(JobStatus.WARN, "push failed"),
                )
            if header is not None:
                if outcome.status == JobStatus.FAIL:
                    task_bridge.update(header, "fail", outcome.message)
                elif outcome.status == JobStatus.WARN:
                    task_bridge.update(header, "warn", outcome.message)
                else:
                    task_bridge.update(header, "ok", outcome.message)
                screen.header_terminal = True
        except Exception as e:  # noqa: BLE001
            outcome = JobTaskOutcome(JobStatus.FAIL, first_line(str(e)))
            if header is not None:
                task_bridge.update(header, "fail", outcome.message)
                screen.header_terminal = True
        finally:
            _safe_merge_refresh_targets(state, screen)
            _safe_merge_release_locks(screen)
            screen.phase = "done"
            if outcome.status is not None:
                state.job_registry.finish(job, outcome.status, outcome.message)

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    thread = start_job_thread(
        state.job_registry,
        job,
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        if screen.header_task is not None:
            task_bridge.update(screen.header_task, "fail", first_line(job.message))
            screen.header_terminal = True
        _safe_merge_release_locks(screen)
        screen.phase = "done"


def _safe_merge_push(
        state: State,
        screen: SafeMergeScreen,
        task_bridge: Optional[JobTaskBridge] = None,
) -> bool:
    """Push the merge commit. Plain `git push` (with `--set-upstream`
    fallback) — never forced. Returns True on success."""
    tasks = task_bridge or JobTaskBridge(state.tasks)
    header = screen.header_task
    path = screen.target_path
    t = tasks.add("  ↳ push", parent=header)
    rc_b, b_out, _ = git(path, ["branch", "--show-current"])
    cur_branch = b_out.strip() if rc_b == 0 else ""
    rc_u, u_out, _ = git(path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    has_upstream = rc_u == 0 and bool(u_out.strip())
    try:
        if has_upstream:
            rc, _, err = git_cancellable(
                path,
                ["push"],
                cancel_event=screen.cancel_event,
                timeout=USER_PUSH_TIMEOUT_SECONDS,
            )
        elif cur_branch and is_safe_ref_arg(cur_branch):
            rc, _, err = git_cancellable(
                path,
                ["push", "--set-upstream", "origin", cur_branch],
                cancel_event=screen.cancel_event,
                timeout=USER_PUSH_TIMEOUT_SECONDS,
            )
        else:
            tasks.update(t, "fail", "no current branch to push")
            return False
    except Exception as e:  # noqa: BLE001
        tasks.update(t, "fail", first_line(str(e)))
        return False
    if rc != 0:
        if rc == 130:
            tasks.update(t, "warn", "cancelled")
        else:
            tasks.update(t, "fail", first_line(err))
        return False
    tasks.update(t, "ok")
    return True


def _safe_merge_sync_submodule(
        state: State,
        screen: SafeMergeScreen,
        task_bridge: Optional[JobTaskBridge] = None,
) -> JobTaskOutcome:
    """After a submodule-checkout merge lands and is pushed, fan the new
    commit out to sibling checkouts and bump the parent gitlink(s) — the
    same `sync_sibling` + `_cascade_propagate_to_parents` plumbing the
    commit pipeline uses."""
    tasks = task_bridge or JobTaskBridge(state.tasks)
    header = screen.header_task
    child = screen.target_child
    if child is None:
        return JobTaskOutcome()
    outcome = JobTaskOutcome()
    canonical = child.repo
    branch = child.branch or canonical.branch
    if not branch or branch == "(detached)" or not is_safe_ref_arg(branch):
        t = tasks.add("  ↳ sync siblings (skipped)", parent=header)
        msg = "submodule on detached HEAD — sync siblings manually"
        tasks.update(t, "warn", msg)
        return JobTaskOutcome(JobStatus.WARN, msg)
    ref_label = state.task_repo_label(canonical)
    targets: List[Tuple[str, Path, Optional[Tuple[Repo, Path]]]] = []
    if not canonical.synthetic:
        targets.append((f"top-level {ref_label}", canonical.path, None))
    for other_parent, other_path in canonical.siblings:
        if other_path == child.nested_path:
            continue
        targets.append(
            (
                f"{ref_label} in {state.task_repo_label(other_parent)}",
                other_path,
                (other_parent, other_path),
            )
        )
    for label, target_path, child_pair in targets:
        t = tasks.add(f"  ↳ sync {label}", parent=header)
        if child_pair is None:
            claim = WorkerClaim(state, repo=canonical)
        else:
            claim = WorkerClaim(state, child=_find_child_at(child_pair[0], child_pair[1]))
        with claim:
            parent_path = child_pair[0].path if child_pair is not None else None
            ok, sync_msg = _sync_sibling_safe(target_path, branch, parent_path=parent_path)
        tasks.update(t, "ok" if ok else "fail", sync_msg)
        if not ok:
            outcome = _merge_job_task_outcome(
                outcome,
                JobTaskOutcome(JobStatus.FAIL, sync_msg),
            )

    if state.auto_push_submodule_parent and canonical.siblings:
        try:
            _cascade_propagate_to_parents(state, [canonical])
        except Exception as e:  # noqa: BLE001
            t = tasks.add("  ↳ propagate to parents", parent=header)
            msg = first_line(str(e))
            tasks.update(t, "fail", msg)
            outcome = _merge_job_task_outcome(
                outcome,
                JobTaskOutcome(JobStatus.FAIL, msg),
            )
    return outcome
