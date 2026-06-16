"""Background worker functions: every git action that takes more than a
moment runs in a daemon thread out of here, publishing progress to the
sidebar via state.tasks. None of these touch curses."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .models import (
    AlignHeadsPrompt, ChildRef, DetachedRecoveryPrompt,
    LFSCandidate, Repo, ReviewBlock, SafeMergeScreen,
    SmartSyncCheckout, State, Task, Tasks,
)
from .git_ops import (
    apply_lfs_tracking, begin_safe_merge, complete_safe_merge_commit,
    create_named_stash, describe_merge_side, discover_repos,
    dispatch_workflow, drop_named_stash, first_line,
    get_run_view, gh_available, git, git_cancellable,
    has_only_submodule_pointer_changes, head_short_info,
    link_siblings, list_branches,
    list_recent_runs, merge_head_sha, merge_remote_workflow_states,
    parse_github_slug, parse_safe_merge_conflicts,
    refresh_repo, remaining_conflict_paths, safe_stage_all, signature_mtime,
    suggest_commit_message,
    suggest_commit_message_at, suggest_commit_message_for_paths,
    sync_sibling, sync_subtree, is_safe_ref_arg,
    working_tree_signature, write_conflict_resolution,
    MAX_PARALLEL_GIT_JOBS,
)

PROMPT_WAIT_SECONDS = 15 * 60
MIN_ACTION_REFRESH_SECONDS = 0.35
_detached_recovery_prompt_lock = threading.Lock()
_align_heads_prompt_lock = threading.Lock()


def _pull_prefer_ff_then_merge(
        path: Path, tasks: Tasks, name: str, *,
        allow_merge_fallback: bool,
        parent_task: "Optional[Task]" = None,
        cancel_event: "Optional[threading.Event]" = None) -> bool:
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
    rc, _, err = git_cancellable(path, pull_args, cancel_event=cancel_event)
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
        tasks.update(t, "fail",
                     first_line(err) or "cannot fast-forward")
        return False
    merge_args = ["pull", "--no-rebase", "--no-edit"]
    rc2, _, err2 = git_cancellable(
        path, merge_args, cancel_event=cancel_event)
    _, head_after2, _ = git(path, ["rev-parse", "HEAD"])
    if rc2 == 130:
        t = tasks.add(f"{name}: pull", parent=parent_task)
        tasks.update(t, "warn", "cancelled")
        return False
    if rc2 != 0:
        t = tasks.add(f"{name}: pull", parent=parent_task)
        tasks.update(t, "fail",
                     first_line(err2) or first_line(err) or "pull failed")
        return False
    t = tasks.add(f"{name}: pull", parent=parent_task)
    detail = ""
    if head_before.strip() != head_after2.strip():
        detail = "merged upstream"
    tasks.update(t, "ok", detail)
    return True


def refresh_repo_with_remote_state(repo: Repo) -> None:
    """`refresh_repo` plus a `gh workflow list` merge so the workflow's
    GitHub-side state (active / disabled_*) is on hand for the review
    screen and the picker. Best-effort — silent if gh is unavailable
    or the remote isn't a github.com URL."""
    refresh_repo(repo)
    if not gh_available() or not repo.workflows:
        return
    slug = parse_github_slug(repo.remote_url_raw)
    if slug:
        merge_remote_workflow_states(repo.workflows, slug)


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
    if conclusion in ("failure", "timed_out", "startup_failure",
                      "action_required"):
        return "fail", conclusion or "failed"
    if conclusion in ("cancelled", "skipped", "neutral", "stale"):
        return "warn", conclusion or "skipped"
    return "warn", conclusion or "unknown"


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
    completed = [s for s in steps
                 if (s.get("status") or "").lower() == "completed"]
    if completed:
        return completed[-1].get("name", "") or ""
    return steps[0].get("name", "") or ""


def _format_run_label(repo_label: str, workflow_name: str,
                      current_step: str = "") -> str:
    base = f"↗ {repo_label}: {workflow_name}"
    if current_step:
        return f"{base} — {current_step}"
    return base


def _format_job_label(job_name: str, current_step: str = "") -> str:
    if current_step:
        return f"  ↳ {job_name} — {current_step}"
    return f"  ↳ {job_name}"


def _create_and_push_tag(tasks, task_obj, repo_path: Path,
                         tag_name: str, sha: str) -> None:
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
        tasks.update(
            task_obj, "fail",
            f"push: {first_line(err_p) or 'failed'}")
        return
    tasks.update(task_obj, "ok")


def _poll_run(state: State, slug: str, run_id: int,
              repo: Repo, workflow_name: str,
              run_task: Task,
              pending_task: Optional[Task] = None,
              pushed_sha: str = "") -> None:
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

    # Stash the workflow-tracking metadata on the run task so the
    # task-detail modal can show run id / URL / workflow name + walk
    # to its job sub-tasks via Task.parent.
    state.tasks.set_meta(
        run_task, repo=repo, slug=slug, run_id=run_id,
        workflow_name=workflow_name)

    while True:
        view = get_run_view(slug, run_id)
        if view is None:
            consecutive_failures += 1
            if consecutive_failures >= failure_budget:
                state.tasks.update(
                    run_task, "warn",
                    "polling abandoned — gh unreachable")
                if pending_task is not None:
                    state.tasks.update(
                        pending_task, "warn",
                        "skipped — polling abandoned")
                return
            time.sleep(poll_interval)
            continue
        consecutive_failures = 0

        # Capture the run-level URL once we have it, so the detail
        # modal's "Open in browser" item works even if later polls
        # fail or return None.
        url = view.get("url") or view.get("html_url") or ""
        state.tasks.set_meta(run_task, latest_view=view, run_url=url)

        jobs = view.get("jobs") or []
        for job in jobs:
            jid = job.get("databaseId") or job.get("id")
            if not isinstance(jid, int):
                continue
            jname = job.get("name") or "job"
            step_label = _current_step_label(job)
            label = _format_job_label(jname, step_label)
            if jid not in job_tasks:
                job_tasks[jid] = state.tasks.add(label, parent=run_task)
                # Job rows share the parent run's slug + run_id so a
                # cancel from a job row still hits the right run.
                state.tasks.set_meta(
                    job_tasks[jid], repo=repo, slug=slug, run_id=run_id,
                    workflow_name=workflow_name, job_id=jid)
            else:
                state.tasks.set_label(job_tasks[jid], label)
            jstatus, jmsg = _gh_run_status_to_task(job)
            state.tasks.update(job_tasks[jid], jstatus, jmsg)

        active = next(
            (j for j in jobs
             if (j.get("status") or "").lower() == "in_progress"),
            None,
        )
        if active is None:
            active = next(
                (j for j in jobs
                 if (j.get("status") or "").lower() != "completed"),
                None,
            )
        focus_step = _current_step_label(active) if active else ""
        state.tasks.set_label(
            run_task, _format_run_label(repo_label, workflow_name, focus_step))

        rstatus, rmsg = _gh_run_status_to_task(view)
        if rstatus != "running":
            state.tasks.update(run_task, rstatus, rmsg)
            # Drop the heavy `latest_view` JSON now that the run is
            # done — terminal tasks may sit in the panel for a long
            # time (default `auto_remove_completed_after = -1`), and
            # the snapshot can be 10–100 KB per run. The run id / url
            # / workflow_name fields stay so the detail modal still
            # works for "Open in browser".
            state.tasks.set_meta(run_task, latest_view=None)
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
                next_target = repo.then_run_after_workflow.pop(
                    workflow_name, "")
                if next_target == "__add_tag__":
                    # Pop the slot's parameter bucket — currently
                    # only "tag" lives in it, but the dict shape
                    # leaves room for more inputs (e.g.
                    # workflow_dispatch fields wired through the
                    # same ParamSpec pattern in the future).
                    params = repo.then_run_params_after_workflow.pop(
                        workflow_name, {})
                    tag_name = params.get("tag", "").strip()
                    sha = pushed_sha
                    if not sha:
                        rc_h, head_out, _ = git(
                            repo.path, ["rev-parse", "HEAD"])
                        sha = head_out.strip() if rc_h == 0 else ""
                    tag_label = (
                        f"  ↪ tag {tag_name}" if tag_name
                        else "  ↪ tag (empty name)")
                    if pending_task is not None:
                        state.tasks.set_label(pending_task, tag_label)
                        state.tasks.update(pending_task, "running", "")
                        tag_task = pending_task
                    else:
                        tag_task = state.tasks.add(tag_label,
                                                   parent=run_task)
                    _create_and_push_tag(
                        state.tasks, tag_task, repo.path, tag_name, sha)
                elif next_target:
                    branch = repo.branch or "main"
                    # Pop the per-workflow input buffer the same
                    # way the add-tag branch above pops its tag
                    # buffer — keeps a follow-up run from
                    # re-dispatching with stale -F values.
                    chain_inputs = (
                        repo.then_run_params_after_workflow.pop(
                            workflow_name, {}))
                    kick_off_manual_dispatch(
                        state, repo, next_target, branch,
                        existing_task=pending_task,
                        inputs=chain_inputs)
            elif pending_task is not None:
                # Parent didn't succeed — the chain is dead. Mark the
                # placeholder so the user can see the chain was skipped
                # rather than leave a stuck "pending" row.
                state.tasks.update(
                    pending_task, "warn",
                    "skipped — parent didn't succeed")
            return
        state.tasks.update(run_task, "running")
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


def kick_off_workflow_tracking(state: State, slug: str, run: dict,
                               repo: Repo,
                               pushed_sha: str = "") -> Optional[Task]:
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
    t = state.tasks.add(_format_run_label(repo_label, workflow_name))

    pending_then_run = repo.then_run_after_workflow.get(workflow_name, "")
    pending_task: Optional[Task] = None
    if pending_then_run:
        # Pretty up the placeholder when the chain is "add tag" —
        # the user-facing label says "tag <name>" so the row reads
        # like a real action rather than the sentinel.
        if pending_then_run == "__add_tag__":
            tag_name = (repo.then_run_params_after_workflow
                        .get(workflow_name, {})
                        .get("tag", ""))
            placeholder_label = (
                f"  ↪ then run: tag {tag_name}" if tag_name
                else "  ↪ then run: tag (name unset)")
        else:
            placeholder_label = f"  ↪ then run: {pending_then_run}"
        pending_task = state.tasks.add(placeholder_label, parent=t)
        state.tasks.update(pending_task, "pending",
                           f"waiting on {workflow_name}")
        state.tasks.set_meta(
            pending_task, repo=repo,
            pending_after_workflow=workflow_name,
            pending_target=pending_then_run)

    threading.Thread(
        target=_poll_run,
        args=(state, slug, run_id, repo, workflow_name, t, pending_task,
              pushed_sha),
        daemon=True,
    ).start()
    return t


def kick_off_post_push_run_tracking(state: State, repo: Repo, branch: str,
                                    sha: str,
                                    tracked_names: Iterable[str]) -> None:
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
    timeout = 120.0

    def watcher() -> None:
        deadline = time.monotonic() + timeout
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
                kick_off_workflow_tracking(
                    state, slug, run, repo, pushed_sha=sha)
            if remaining:
                time.sleep(poll_interval)
        for wf in remaining:
            t = state.tasks.add(f"↗ {repo_label}: {wf}")
            state.tasks.update(t, "warn", "no run triggered within 2 min")

    threading.Thread(target=watcher, daemon=True).start()


def kick_off_manual_dispatch(state: State, target_repo: Repo,
                             workflow_name: str, ref: str,
                             *, existing_task: Optional[Task] = None,
                             inputs: "Optional[dict[str, str]]" = None
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
            state.tasks.update(
                existing_task, "fail",
                "gh CLI / github remote unavailable")
        else:
            t = state.tasks.add(f"↗ {repo_label}: {workflow_name}")
            state.tasks.update(
                t, "fail", "gh CLI / github remote unavailable")
        return

    if existing_task is not None:
        dispatch_task = existing_task
        # Keep the indented "↪ …" prefix so the row stays visually
        # nested under the parent that triggered it.
        state.tasks.set_label(
            dispatch_task, f"  ↪ dispatch {workflow_name}")
        state.tasks.update(dispatch_task, "running", "")
    else:
        dispatch_task = state.tasks.add(
            f"↗ {repo_label}: dispatch {workflow_name}")

    def worker() -> None:
        ok, msg = dispatch_workflow(slug, workflow_name, ref,
                                    inputs=inputs)
        if not ok:
            state.tasks.update(dispatch_task, "fail", msg)
            return
        state.tasks.update(dispatch_task, "ok", msg)

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
                if (isinstance(rid, int) and rid not in before
                        and wf == workflow_name):
                    kick_off_workflow_tracking(state, slug, r, target_repo)
                    return
            time.sleep(poll_interval)
        t = state.tasks.add(f"↗ {repo_label}: {workflow_name}")
        state.tasks.update(t, "warn", "dispatched but no run appeared in 30s")

    threading.Thread(target=worker, daemon=True).start()


# ---------- Single-repo refresh after an action ---------------------------


def _refresh_target_state(state: State,
                          target_repo: Optional[Repo],
                          target_parent: Optional[Repo]) -> None:
    """Re-fetch state for just one row's repo. For top-level rows we
    refresh the Repo itself; for submodule child rows we refresh the
    parent (its dirty state changes when the nested checkout moves) and
    then re-link siblings so the child's HEAD/in_sync/dirty fields catch
    up."""
    if target_repo is not None:
        refresh_repo(target_repo)
    if target_parent is not None:
        refresh_repo(target_parent)
    link_siblings(state.repos, state.subtrees)


# ---------- Single git-action launcher ------------------------------------


def _find_child_at(parent: Optional[Repo],
                   path: Path) -> Optional[ChildRef]:
    """Locate the ChildRef inside `parent` whose nested checkout lives
    at `path`. Used by `kick_off_action` to flip the row's refreshing
    spinner while an action runs against a nested submodule child."""
    if parent is None:
        return None
    for child in parent.children:
        if child.nested_path == path:
            return child
    return None


def kick_off_action(state: State, action_id: str, *,
                    target_label: str, target_path: Path,
                    target_repo: Optional[Repo],
                    target_parent: Optional[Repo],
                    branch_arg: str = "",
                    reset_count: int = 0) -> None:
    """Spawn a daemon worker that runs one git action against `target_path`,
    publishes its progress to the sidebar, and quietly re-queries that one
    repo's state when it finishes. Returns immediately so the UI is free.

    The targeted row's `refreshing` flag is held high from the moment
    the action is submitted until the post-action refresh completes —
    its state dot renders as the global spinner glyph during that
    window, so it's obvious the row's state is in transition rather
    than the user wondering whether their keystroke registered."""
    known_actions = {
        "fetch", "pull", "push", "soft_reset", "switch_branch",
        "checkout_remote_branch",
        "branch_from_head", "create_branch", "ff_merge",
        "rename_branch", "set_upstream",
        "stash_create", "stash_apply",
    }
    should_refresh = action_id in known_actions
    target_child = _find_child_at(target_parent, target_path)
    # Claim the refresh slot SYNCHRONOUSLY before returning so the
    # very next redraw shows the spinner — the daemon worker may not
    # run for a tick, and even a 100ms gap reads as "did anything
    # happen?". `try_acquire_refresh` doubles as a mutex against any
    # other refresh source (fs_watcher's debounce timer, a concurrent
    # Ctrl+R) starting on the same target. The UI gate in main_loop
    # already prevents opening the action menu over a refreshing row,
    # but there's a tiny window where an fs event could win the race
    # between menu-open and action-dispatch — we surface a warn task
    # rather than silently racing the existing refresh.
    repo_acquired = False
    child_acquired = False
    if target_repo is not None:
        repo_acquired = target_repo.try_acquire_refresh()
        if not repo_acquired:
            t = state.tasks.add(f"{target_label}: skipped")
            state.tasks.update(
                t, "warn", "refresh in progress — try again")
            return
    if target_child is not None:
        child_acquired = target_child.try_acquire_refresh()
        if not child_acquired:
            # Release the parent's claim before bailing so we don't
            # strand its lock.
            if repo_acquired and target_repo is not None:
                target_repo.release_refresh()
            t = state.tasks.add(f"{target_label}: skipped")
            state.tasks.update(
                t, "warn", "refresh in progress — try again")
            return

    def worker() -> None:
      started_at = time.monotonic()
      try:
        if action_id == "fetch":
            t = state.tasks.add(f"{target_label}: fetch")
            rc, _, err = git(target_path, ["fetch", "--all"])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        elif action_id == "pull":
            ok = _pull_prefer_ff_then_merge(
                target_path, state.tasks, target_label,
                allow_merge_fallback=True)
            if not ok:
                return
        elif action_id == "push":
            t = state.tasks.add(f"{target_label}: push")
            rc_b, b_out, _ = git(target_path, ["branch", "--show-current"])
            cur_branch = b_out.strip() if rc_b == 0 else ""
            if cur_branch and not is_safe_ref_arg(cur_branch):
                state.tasks.update(t, "fail", "unsafe current branch name")
                return
            rc_u, u_out, _ = git(target_path, [
                "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
            has_upstream = rc_u == 0 and bool(u_out.strip())
            if has_upstream:
                ok_pull = _pull_prefer_ff_then_merge(
                    target_path, state.tasks, target_label,
                    allow_merge_fallback=True)
                if not ok_pull:
                    state.tasks.update(t, "fail", "skipped: cannot pull")
                    return
                rc, _, err = git(target_path, ["push"])
                state.tasks.update(t, "ok" if rc == 0 else "fail",
                                   "" if rc == 0 else first_line(err))
            elif cur_branch:
                rc, _, err = git(target_path, [
                    "push", "--set-upstream", "origin", cur_branch])
                state.tasks.update(t, "ok" if rc == 0 else "fail",
                                   "" if rc == 0 else first_line(err))
            else:
                state.tasks.update(t, "fail", "no current branch")
        elif action_id == "soft_reset":
            if reset_count <= 0:
                t = state.tasks.add(
                    f"{target_label}: soft reset all unpushed (to @{{u}})")
                rc, _, err = git(target_path, ["reset", "--soft", "@{u}"])
            else:
                t = state.tasks.add(
                    f"{target_label}: soft reset HEAD~{reset_count}")
                rc, _, err = git(target_path, [
                    "reset", "--soft", f"HEAD~{reset_count}"])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        elif action_id == "switch_branch":
            t = state.tasks.add(f"{target_label}: checkout {branch_arg}")
            if not is_safe_ref_arg(branch_arg):
                state.tasks.update(t, "fail", "unsafe branch name")
                return
            # Refuse the switch if HEAD has commits not on the chosen
            # branch — git would otherwise silently orphan them and
            # files unique to those commits would vanish from WT.
            # The user picked the branch via the menu, but they may
            # not realise their HEAD is detached with unpushed work.
            if not _head_is_ancestor_of(target_path, branch_arg):
                state.tasks.update(
                    t, "warn",
                    f"HEAD has commits not on {branch_arg} — would orphan "
                    "them; manual: `git checkout -b <name>` to keep them")
            else:
                rc, _, err = git(target_path, ["checkout", branch_arg])
                state.tasks.update(
                    t, "ok" if rc == 0 else "fail",
                    "" if rc == 0 else first_line(err))
        elif action_id == "checkout_remote_branch":
            t = state.tasks.add(
                f"{target_label}: checkout remote {branch_arg}")
            if not is_safe_ref_arg(branch_arg) or "/" not in branch_arg:
                state.tasks.update(t, "fail", "unsafe remote ref")
                return
            short = branch_arg.split("/", 1)[1]
            if not short or not is_safe_ref_arg(short):
                state.tasks.update(t, "fail", "unsafe branch name")
                return
            rc, out, _ = git(target_path, ["branch", "--list", short])
            local_exists = rc == 0 and bool(out.strip())
            checkout_ref = short if local_exists else branch_arg
            if not _head_is_ancestor_of(target_path, checkout_ref):
                state.tasks.update(
                    t, "warn",
                    f"HEAD has commits not on {checkout_ref} — would orphan "
                    "them; manual: `git checkout -b <name>` to keep them")
            elif local_exists:
                rc, _, err = git(target_path, ["checkout", short])
                state.tasks.update(
                    t, "ok" if rc == 0 else "fail",
                    "" if rc == 0 else first_line(err))
            else:
                rc, _, err = git(target_path, [
                    "checkout", "-b", short, branch_arg])
                state.tasks.update(
                    t, "ok" if rc == 0 else "fail",
                    "" if rc == 0 else first_line(err))
        elif action_id == "branch_from_head":
            # Save a detached HEAD's commits onto a fresh branch.
            # `git checkout -b <name>` only creates a ref + flips HEAD
            # to it — non-destructive (cardinal-rule safe). The new
            # branch points at the SAME commit HEAD was on, so every
            # unique commit is now reachable from a named branch and
            # `merge-base --is-ancestor` checks elsewhere will treat
            # the work as no longer at risk of being orphaned.
            t = state.tasks.add(
                f"{target_label}: branch HEAD as {branch_arg}")
            if not is_safe_ref_arg(branch_arg):
                state.tasks.update(t, "fail", "unsafe branch name")
                return
            rc, _, err = git(target_path, ["checkout", "-b", branch_arg])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        elif action_id == "create_branch":
            # Create a new branch off the current HEAD and switch to
            # it. Same `git checkout -b <name>` plumbing as
            # branch_from_head — distinct action_id so the task label
            # reads naturally when the user wasn't actually detached.
            t = state.tasks.add(
                f"{target_label}: create branch {branch_arg}")
            if not is_safe_ref_arg(branch_arg):
                state.tasks.update(t, "fail", "unsafe branch name")
                return
            rc, _, err = git(target_path, ["checkout", "-b", branch_arg])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        elif action_id == "ff_merge":
            # Fast-forward-only merge: refuses on its own if a real
            # merge commit would be needed, so divergent histories
            # never silently get a merge commit the user didn't ask
            # for. The lack of `--no-ff` etc. keeps this strict.
            t = state.tasks.add(
                f"{target_label}: merge --ff-only {branch_arg}")
            if not is_safe_ref_arg(branch_arg):
                state.tasks.update(t, "fail", "unsafe branch name")
                return
            rc, _, err = git(target_path, [
                "merge", "--ff-only", branch_arg])
            if rc == 0:
                state.tasks.update(t, "ok")
            else:
                state.tasks.update(
                    t, "fail",
                    first_line(err) or "not a fast-forward")
        elif action_id == "rename_branch":
            # `git branch -m <newname>` renames the *current* branch in
            # place — only touches refs, no commits orphaned. Refuses
            # on detached HEAD via git's own error. Cardinal-rule safe.
            t = state.tasks.add(
                f"{target_label}: rename branch → {branch_arg}")
            if not is_safe_ref_arg(branch_arg):
                state.tasks.update(t, "fail", "unsafe branch name")
                return
            rc, _, err = git(target_path, ["branch", "-m", branch_arg])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        elif action_id == "set_upstream":
            # `git branch --set-upstream-to=<ref>` only edits config,
            # never touches refs or commits. `branch_arg` is the fully
            # qualified remote-tracking ref (e.g. origin/main).
            t = state.tasks.add(
                f"{target_label}: upstream → {branch_arg}")
            if not is_safe_ref_arg(branch_arg):
                state.tasks.update(t, "fail", "unsafe ref name")
                return
            rc, _, err = git(target_path, [
                "branch", f"--set-upstream-to={branch_arg}"])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        elif action_id == "stash_create":
            # `git stash push` saves working-tree changes to a new
            # stash entry. Cardinal-rule safe: the entry preserves
            # both index and worktree state, and our pipeline never
            # calls `stash drop` / `pop` so nothing is destroyed.
            t = state.tasks.add(f"{target_label}: stash push")
            rc, _, err = git(target_path, ["stash", "push"])
            if rc != 0:
                state.tasks.update(t, "fail", first_line(err))
            else:
                state.tasks.update(t, "ok", "")
        elif action_id == "stash_apply":
            # `git stash apply <ref>` reapplies the stash without
            # dropping it — the entry stays around, so an apply that
            # silently drops content can be re-attempted. Pop (apply
            # + drop) is intentionally NOT supported here; that's a
            # cardinal-rule violation.
            t = state.tasks.add(
                f"{target_label}: stash apply {branch_arg}")
            # branch_arg is `stash@{N}` — protect against shell-style
            # tricks even though git's argv parsing makes them moot.
            if not branch_arg or branch_arg.startswith("-"):
                state.tasks.update(t, "fail", "unsafe stash ref")
                return
            rc, _, err = git(target_path,
                             ["stash", "apply", "--", branch_arg])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        else:
            return  # unknown action — nothing to do
      except Exception as e:
        t = state.tasks.add(f"{target_label}: failed")
        state.tasks.update(t, "fail", first_line(str(e)))
      finally:
        if should_refresh:
            try:
                _refresh_target_state(state, target_repo, target_parent)
            except Exception as e:
                t = state.tasks.add(f"{target_label}: refresh")
                state.tasks.update(t, "fail", first_line(str(e)))
            remaining = MIN_ACTION_REFRESH_SECONDS - (
                time.monotonic() - started_at)
            if remaining > 0:
                time.sleep(remaining)
        # Always release the refresh slot — even on early-return /
        # exception paths — so a row never gets stuck spinning AND
        # so the underlying lock is freed for fs_watcher / Ctrl+R to
        # acquire next. `release_refresh` is idempotent on the lock
        # (swallows RuntimeError if not held), so the guard against
        # double-release is built in.
        if repo_acquired and target_repo is not None:
            target_repo.release_refresh()
        if child_acquired and target_child is not None:
            target_child.release_refresh()

    threading.Thread(target=worker, daemon=True).start()


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
    return (by_kind["remove"] + by_kind["rename"]
            + by_kind["set_url"] + by_kind["add"])


def kick_off_remote_changes(state: State, modal_rows,
                            target_label: str, target_path: Path,
                            target_repo: Optional[Repo]) -> int:
    """Apply pending remote changes from the modal as a single batched
    sidebar task. Returns the number of operations dispatched (0 means
    "nothing to do" — caller can skip the confirmation prompt). Each
    op runs sequentially in the same daemon thread so a rename
    completes before its follow-up set-url fires."""
    ops = _compute_remote_ops(modal_rows)
    if not ops:
        return 0

    plural = "" if len(ops) == 1 else "s"
    t = state.tasks.add(
        f"{target_label}: applying {len(ops)} remote change{plural}")

    def worker() -> None:
        try:
            for op in ops:
                if op[0] == "remove":
                    _, name = op
                    if not is_safe_ref_arg(name):
                        state.tasks.update(t, "fail",
                                           f"unsafe remote name: {name}")
                        return
                    rc, _, err = git(target_path,
                                     ["remote", "remove", name])
                elif op[0] == "rename":
                    _, old, new = op
                    if not is_safe_ref_arg(old) or not is_safe_ref_arg(new):
                        state.tasks.update(t, "fail",
                                           f"unsafe remote name: {old}/{new}")
                        return
                    rc, _, err = git(target_path,
                                     ["remote", "rename", old, new])
                elif op[0] == "set_url":
                    _, name, url = op
                    if not is_safe_ref_arg(name) or not url \
                            or url.startswith("-"):
                        state.tasks.update(t, "fail",
                                           f"unsafe url for {name}")
                        return
                    rc, _, err = git(target_path,
                                     ["remote", "set-url", name, url])
                elif op[0] == "add":
                    _, name, url = op
                    if not is_safe_ref_arg(name) or not url \
                            or url.startswith("-"):
                        state.tasks.update(t, "fail",
                                           f"unsafe url for {name}")
                        return
                    rc, _, err = git(target_path,
                                     ["remote", "add", name, url])
                else:
                    continue
                if rc != 0:
                    state.tasks.update(t, "fail", first_line(err))
                    return
            state.tasks.update(t, "ok", "")
        finally:
            # Re-query the repo so its remote_url cache reflects the
            # new origin URL (if origin was touched). Best-effort.
            if target_repo is not None:
                refresh_repo_with_remote_state(target_repo)

    threading.Thread(target=worker, daemon=True).start()
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
        get_commit_details, list_tags_at, query_commit_files,
        query_commit_reflog,
    )

    def worker() -> None:
        try:
            if modal.cancel_event.is_set():
                return
            modal.tags = list_tags_at(modal.target_path, modal.sha)
            modal.tags_loading = False
            if modal.cancel_event.is_set():
                return
            author, date, subject, body = get_commit_details(
                modal.target_path, modal.sha)
            modal.author = author
            modal.date = date
            # Subject was already populated from the CommitEntry the
            # caller had on hand; only overwrite if the on-disk show
            # disagrees (rare — mostly when the commit moved).
            if subject and not modal.subject:
                modal.subject = subject
            modal.body = body
            modal.details_loading = False
            if modal.cancel_event.is_set():
                return
            modal.files = query_commit_files(modal.target_path, modal.sha)
            modal.files_loading = False
            if modal.cancel_event.is_set():
                return
            modal.reflog_entries = query_commit_reflog(
                modal.target_path, modal.sha)
        finally:
            modal.tags_loading = False
            modal.details_loading = False
            modal.files_loading = False
            modal.reflog_loading = False

    threading.Thread(target=worker, daemon=True).start()


def kick_off_add_tag(state: State, target_label: str,
                     target_path: Path,
                     target_repo: Optional[Repo],
                     target_parent: Optional[Repo],
                     name: str, sha: str) -> None:
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
    # Same try_acquire / warn-and-bail pattern as kick_off_action — a
    # tag write is a fast ref-only op, but it still mutates `.git/`
    # and would race a concurrent refresh on the row's flags.
    repo_acquired = False
    child_acquired = False
    if target_repo is not None:
        repo_acquired = target_repo.try_acquire_refresh()
        if not repo_acquired:
            t = state.tasks.add(f"{target_label}: skipped")
            state.tasks.update(
                t, "warn", "refresh in progress — try again")
            return
    if target_child is not None:
        child_acquired = target_child.try_acquire_refresh()
        if not child_acquired:
            if repo_acquired and target_repo is not None:
                target_repo.release_refresh()
            t = state.tasks.add(f"{target_label}: skipped")
            state.tasks.update(
                t, "warn", "refresh in progress — try again")
            return

    def worker() -> None:
        try:
            t = state.tasks.add(f"{target_label}: tag {name}")
            if not is_safe_ref_arg(name):
                state.tasks.update(t, "fail", "unsafe tag name")
                return
            if not sha or sha.startswith("-"):
                state.tasks.update(t, "fail", "unsafe sha")
                return

            # 1) Create the tag locally.
            rc, _, err = git(target_path, ["tag", name, sha])
            if rc != 0:
                state.tasks.update(
                    t, "fail", first_line(err) or "git tag failed")
                return

            # 2) Check whether the commit is reachable from any
            # `refs/remotes/origin/*` ref. `for-each-ref --contains`
            # walks the named refs and returns those whose tip is a
            # descendant (or equal) of `sha`. Empty output means
            # "no origin ref reaches this commit yet" — push the
            # branch first, otherwise we'd be carrying the commit
            # to origin via the tag.
            rc, out, _ = git(target_path, [
                "for-each-ref", "--contains", sha,
                "--format=%(refname)",
                "refs/remotes/origin/"])
            on_origin = rc == 0 and bool(out.strip())
            if not on_origin:
                state.tasks.update(
                    t, "warn",
                    "tagged locally — commit not on origin yet; "
                    "push the branch first, then re-add the tag")
                return

            # 3) Push the tag — safe ref-only operation since the
            # commit objects it points at are already on origin.
            rc, _, err = git(target_path, ["push", "origin", name])
            if rc != 0:
                state.tasks.update(
                    t, "fail", f"push: {first_line(err) or 'failed'}")
                return
            state.tasks.update(t, "ok")
        finally:
            if repo_acquired and target_repo is not None:
                target_repo.release_refresh()
            if child_acquired and target_child is not None:
                target_child.release_refresh()

    threading.Thread(target=worker, daemon=True).start()


def kick_off_clone(state: State, url: str, dest: Path, branch: str,
                   recurse_submodules: bool,
                   on_done=None) -> None:
    """Run `git clone` in a daemon thread, publishing progress to the
    sidebar. `on_done` is called with `(ok, message)` once the clone
    settles, on the worker thread — caller wires it up to refresh the
    workspace's repo list and close the modal."""
    from .git_ops import clone_repo
    label = dest.name or "clone"
    t = state.tasks.add(f"{label}: clone")

    def worker() -> None:
        ok, msg = clone_repo(url, dest, branch=branch,
                             recurse_submodules=recurse_submodules)
        state.tasks.update(t, "ok" if ok else "fail", msg)
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

    threading.Thread(target=worker, daemon=True).start()


# ---------- Async commit-message suggestion -------------------------------


def _suggest_into_repo(state: State, repo: Repo) -> None:
    repo.suggesting = True
    try:
        result = suggest_commit_message(
            repo,
            max_added=state.suggest_added,
            max_updated=state.suggest_updated,
            max_deleted=state.suggest_deleted,
            auto_stage=state.auto_stage,
        )
        if result and not repo.refreshing:
            repo.message = result
    finally:
        repo.suggesting = False


def _suggest_into_child(state: State, child: ChildRef) -> None:
    child.suggesting = True
    try:
        result = suggest_commit_message_at(
            child.nested_path,
            max_added=state.suggest_added,
            max_updated=state.suggest_updated,
            max_deleted=state.suggest_deleted,
            auto_stage=state.auto_stage,
        )
        if result and not child.refreshing:
            child.message = result
    finally:
        child.suggesting = False


def kick_off_suggest_for(state: State, target) -> None:
    """Run a single suggestion in a background thread; UI shows a spinner
    in the field meanwhile via target.suggesting."""
    if getattr(target, "refreshing", False):
        return
    if isinstance(target, Repo):
        threading.Thread(
            target=_suggest_into_repo, args=(state, target), daemon=True).start()
    else:  # ChildRef
        threading.Thread(
            target=_suggest_into_child, args=(state, target), daemon=True).start()


def kick_off_bulk_suggest(state: State) -> None:
    """For every dirty row with an empty message, kick off a background
    suggestion. Each row animates independently."""
    for repo in state.repos:
        if (repo.is_dirty and not repo.message.strip() and not repo.suggesting
                and not repo.refreshing):
            kick_off_suggest_for(state, repo)
    for parent in state.repos:
        for child in parent.children:
            if (child.kind == "submodule" and child.dirty
                    and not child.message.strip() and not child.suggesting
                    and not child.refreshing):
                kick_off_suggest_for(state, child)


# ---------- Commit pipelines ----------------------------------------------


def _apply_staging_plan(target_path: Path,
                        staged_paths: "dict[str, bool]"
                        ) -> Tuple[bool, str]:
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
    rc, status_out, _ = git(target_path,
                            ["status", "--porcelain=v1", "-z"])
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

    to_stage = sorted(p for p, on in staged_paths.items()
                      if on and p in current_status
                      and _needs_add(current_status[p]))
    to_unstage = sorted(p for p, on in staged_paths.items()
                        if not on and p in current_status)
    if to_unstage:
        rc, _, err = git(target_path,
                         ["restore", "--staged", "--"] + to_unstage)
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
    in the right pane. Result is written to BOTH the block's `message`
    (so the review screen shows it immediately) and the underlying
    repo / child's message (so backing out of the review preserves it,
    matching the main-screen suggest semantics)."""
    if block.suggesting or block.merging:
        return
    block.suggesting = True

    def worker() -> None:
        try:
            paths = [p for p, on in block.staged_paths.items() if on]
            if not paths:
                return
            result = suggest_commit_message_for_paths(
                block.target_path, paths,
                max_added=state.suggest_added,
                max_updated=state.suggest_updated,
                max_deleted=state.suggest_deleted,
            )
            if not result:
                return
            block.message = result
            if block.target_repo is not None:
                block.target_repo.message = result
            elif block.target_child is not None:
                block.target_child.message = result
        finally:
            block.suggesting = False

    threading.Thread(target=worker, daemon=True).start()


def commit_worker(state: State, repo: Repo, msg: str,
                  lfs_cands: List[LFSCandidate],
                  staged_paths: Optional["dict[str, bool]"] = None,
                  amend: bool = False,
                  track_workflow: Optional["dict[str, bool]"] = None,
                  then_run_after_push: str = "",
                  then_run_params_after_push: Optional["dict[str, str]"] = None,
                  push: Optional[bool] = None,
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
    pipeline_task = state.tasks.add(f"{name}: working")
    # Cancel signal — the task-detail modal sets this when the user
    # picks "Cancel commit/push" on this row. `git_cancellable` polls
    # it while the long-running network calls (pre-stage pull, push)
    # block on subprocess.wait, so the cancel takes effect within
    # ~250ms of the user's keystroke instead of after the push times
    # out or completes.
    cancel_event = threading.Event()
    # `holds_repo` is the explicit "this task is mutating repo's working
    # tree" tag — read by `Tasks.repo_has_active_job` so refresh paths
    # skip the row while the pipeline is alive. The refresh_lock acquired
    # by `kick_off_workers` covers the same window, but the lock check
    # alone misses the brief release-then-reacquire gaps the supervisor
    # opens around its post-commit `refresh_repo` calls.
    state.tasks.set_meta(
        pipeline_task, cancel_event=cancel_event, holds_repo=repo)
    try:
        _commit_worker_inner(state, repo, msg, lfs_cands, staged_paths,
                             amend, track_workflow, then_run_after_push,
                             then_run_params_after_push, push=push,
                             cancel_event=cancel_event)
        if cancel_event.is_set():
            state.tasks.update(pipeline_task, "warn", "cancelled")
        else:
            state.tasks.update(pipeline_task, "ok", "")
    except Exception as e:
        state.tasks.update(pipeline_task, "fail", first_line(str(e)))


def _commit_worker_inner(state: State, repo: Repo, msg: str,
                         lfs_cands: List[LFSCandidate],
                         staged_paths: Optional["dict[str, bool]"] = None,
                         amend: bool = False,
                         track_workflow: Optional["dict[str, bool]"] = None,
                         then_run_after_push: str = "",
                         then_run_params_after_push: Optional["dict[str, str]"] = None,
                         push: Optional[bool] = None,
                         cancel_event: "Optional[threading.Event]" = None,
                         ) -> None:
    auto_stage = state.auto_stage
    # `push` is the review screen's per-commit toggle; None means "use
    # the workspace default" for callers that don't set it.
    auto_push = state.auto_push if push is None else push
    tasks = state.tasks
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
            tasks.update(t, "fail",
                         "detached HEAD — " + (rmsg or "user cancelled"))
            return
        t = tasks.add(f"{name}: recovered detached HEAD")
        tasks.update(t, "ok", "branch fast-forwarded to HEAD")

    # Integrate upstream before staging — once we have a local commit a
    # strict FF may refuse; try merge pull when needed (never rebase).
    # Only surface a task when HEAD actually moved or the pull itself
    # fails.
    if repo.upstream:
        ok_pull = _pull_prefer_ff_then_merge(
            repo.path, tasks, name, allow_merge_fallback=True,
            cancel_event=cancel_event)
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
    nothing_staged = (rc == 0)
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
    rc_u, _, _ = git(repo.path, [
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if rc_u == 0:
        rc, _, err = git_cancellable(
            repo.path, ["push"], cancel_event=cancel_event)
    else:
        rc_b, b_out, _ = git(repo.path, ["branch", "--show-current"])
        cur_branch = b_out.strip() if rc_b == 0 else ""
        if cur_branch:
            if not is_safe_ref_arg(cur_branch):
                tasks.update(push_task, "fail", "unsafe current branch name")
                return
            rc, _, err = git_cancellable(
                repo.path,
                ["push", "--set-upstream", "origin", cur_branch],
                cancel_event=cancel_event)
        else:
            rc, err = 1, "no current branch"
    if rc != 0:
        # rc 130 = cancelled; tag the row distinctly so the user sees
        # the cancel landed rather than a generic push failure.
        if rc == 130:
            tasks.update(push_task, "warn", "cancelled")
        else:
            tasks.update(push_task, "fail", first_line(err))
        return
    tasks.update(push_task, "ok")

    # Capture the freshly-pushed commit so the actions tracker can match
    # the run by sha. Pulled after push so we get the actual head, not a
    # cached snapshot from before the commit.
    rc_h, head_out, _ = git(repo.path, ["rev-parse", "HEAD"])
    pushed_sha = head_out.strip() if rc_h == 0 else ""
    # Snapshot values (captured + cleared from Repo at queue time in
    # kick_off_workers) drive these — reading from `repo.…` here
    # would race the review screen re-open since its UI reads from
    # the same Repo fields. None means the caller didn't bother
    # snapshotting; fall back to Repo so out-of-tree callers don't
    # silently lose state.
    track_wf_map = (track_workflow if track_workflow is not None
                    else repo.track_workflow)
    tracked = [name for name, on in track_wf_map.items() if on]
    if tracked and pushed_sha:
        kick_off_post_push_run_tracking(
            state, repo, repo.branch, pushed_sha, tracked)
    if track_workflow is None:
        # Legacy non-snapshot path — clear on Repo so we don't loop
        # the same tracked workflow next push.
        repo.track_workflow.clear()

    # "Then run after push" — fired once the push itself completes,
    # regardless of any tracked workflow runs. Two shapes:
    #   * a workflow name → dispatch the manual workflow
    #   * the "__add_tag__" sentinel → create a lightweight tag at
    #     the just-pushed sha. Per-action parameter buffers live in
    #     `then_run_params_after_push` (today only "tag", but the
    #     same dict will hold workflow_dispatch inputs in the
    #     future). Like `track_workflow` above, the snapshot
    #     supersedes Repo when provided.
    if then_run_after_push or then_run_params_after_push is not None:
        after_push_target = then_run_after_push
        after_push_params = dict(then_run_params_after_push or {})
    else:
        after_push_target = repo.then_run_after_push
        after_push_params = dict(repo.then_run_params_after_push)
        repo.then_run_after_push = ""
        repo.then_run_params_after_push.clear()
    if after_push_target == "__add_tag__":
        tag_name = after_push_params.get("tag", "").strip()
        tag_label = state.task_repo_label(repo)
        t_tag = tasks.add(f"{tag_label}: tag {tag_name or '(empty)'}")
        _create_and_push_tag(
            tasks, t_tag, repo.path, tag_name, pushed_sha)
    elif after_push_target:
        # `after_push_params` carries the buffered values for
        # `workflow_dispatch.inputs` — non-empty entries get
        # forwarded as `-F name=value` by dispatch_workflow.
        kick_off_manual_dispatch(
            state, repo, after_push_target, repo.branch,
            inputs=after_push_params)

    for sib_repo, sib_path in repo.siblings:
        t = tasks.add(
            f"  ↳ sync {state.task_repo_label(sib_repo)}", parent=push_task)
        # Flip the matching ChildRef's `refreshing` flag for the
        # duration of the sync so the submodule row inside `sib_repo`
        # animates while its on-disk checkout is being advanced. The
        # try/finally guarantees the flag is cleared even on a
        # mid-sync exception, otherwise the row would spin forever.
        _set_child_ref_refreshing(sib_repo, sib_path, True)
        try:
            ok, sync_msg = sync_sibling(sib_path, repo.branch)
        finally:
            _set_child_ref_refreshing(sib_repo, sib_path, False)
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


def commit_worker_for_child(state: State, parent: Repo, ref: ChildRef,
                            msg: str,
                            staged_paths: Optional["dict[str, bool]"] = None,
                            amend: bool = False,
                            track_workflow: Optional["dict[str, bool]"] = None,
                            then_run_after_push: str = "",
                            then_run_params_after_push: Optional["dict[str, str]"] = None,
                            push: Optional[bool] = None,
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
    name = (f"{state.task_repo_label(ref.repo)} "
            f"(in {state.task_repo_label(parent)})")
    # Early-visibility task — see `commit_worker` for the rationale.
    pipeline_task = state.tasks.add(f"{name}: working")
    cancel_event = threading.Event()
    # `holds_repo` + `holds_child` flag the pipeline against the parent
    # AND the specific child ref — refresh paths consult both via
    # `repo_has_active_job` / `child_has_active_job` so neither the
    # parent's state dot nor the child's nested-row spinner reverts to
    # a stale value mid-push.
    state.tasks.set_meta(
        pipeline_task, cancel_event=cancel_event,
        holds_repo=parent, holds_child=ref)
    try:
        _commit_worker_for_child_inner(
            state, parent, ref, msg, staged_paths, amend,
            track_workflow, then_run_after_push, then_run_params_after_push,
            push=push, cancel_event=cancel_event)
        if cancel_event.is_set():
            state.tasks.update(pipeline_task, "warn", "cancelled")
        else:
            state.tasks.update(pipeline_task, "ok", "")
    except Exception as e:
        state.tasks.update(pipeline_task, "fail", first_line(str(e)))


def _commit_worker_for_child_inner(state: State, parent: Repo,
                                   ref: ChildRef, msg: str,
                                   staged_paths: Optional["dict[str, bool]"]
                                   = None,
                                   amend: bool = False,
                                   track_workflow: Optional["dict[str, bool]"]
                                   = None,
                                   then_run_after_push: str = "",
                                   then_run_params_after_push: Optional["dict[str, str]"]
                                   = None,
                                   push: Optional[bool] = None,
                                   cancel_event: "Optional[threading.Event]"
                                   = None,
                                   ) -> None:
    auto_stage = state.auto_stage
    # `push` is the review screen's per-commit toggle; None means "use
    # the workspace default" for callers that don't set it.
    auto_push = state.auto_push if push is None else push
    tasks = state.tasks
    name = (f"{state.task_repo_label(ref.repo)} "
            f"(in {state.task_repo_label(parent)})")

    rc, out, _ = git(ref.nested_path, ["branch", "--show-current"])
    nested_branch = out.strip() if rc == 0 else ""
    if not nested_branch:
        # Try cardinal-rule-safe recovery (modal asks the user). On
        # success the nested checkout lands on the resolved default
        # branch and the rest of the pipeline runs against that branch
        # as the commit/push target.
        recovered, rmsg = _attempt_detached_recovery(
            state, ref.nested_path, name)
        if not recovered:
            t = tasks.add(f"{name}: cannot commit")
            tasks.update(t, "fail",
                         "detached HEAD — " + (rmsg or "user cancelled"))
            return
        t = tasks.add(f"{name}: recovered detached HEAD")
        tasks.update(t, "ok", "branch fast-forwarded to HEAD")
        # Re-read the now-current branch so the rest of the pipeline
        # uses the recovered branch as its push refspec.
        rc, out, _ = git(ref.nested_path, ["branch", "--show-current"])
        nested_branch = out.strip() if rc == 0 else ""
        if not nested_branch:
            t = tasks.add(f"{name}: cannot commit")
            tasks.update(t, "fail",
                         "recovery succeeded but branch lookup failed")
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
    nothing_staged = (rc == 0)
    if nothing_staged and not amend:
        t = tasks.add(f"{name}: skipped")
        tasks.update(t, "warn", "nothing staged")
        return

    if amend:
        t = tasks.add(f"{name}: commit --amend")
        rc, _, err = git(ref.nested_path,
                         ["commit", "--amend", "-m", msg])
    else:
        t = tasks.add(f"{name}: commit")
        rc, _, err = git(ref.nested_path, ["commit", "-m", msg])
    if rc != 0:
        tasks.update(t, "fail", first_line(err))
        return
    tasks.update(t, "ok")

    if not auto_push:
        return

    rc, up_out, _ = git(ref.nested_path, [
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    has_upstream = rc == 0 and up_out.strip()

    push_task = tasks.add(f"{name}: push")
    if has_upstream:
        rc, _, err = git_cancellable(
            ref.nested_path, ["push"], cancel_event=cancel_event)
    else:
        if not is_safe_ref_arg(nested_branch):
            tasks.update(push_task, "fail", "unsafe current branch name")
            return
        rc, _, err = git_cancellable(
            ref.nested_path,
            ["push", "--set-upstream", "origin", nested_branch],
            cancel_event=cancel_event)
    if rc != 0:
        if rc == 130:
            tasks.update(push_task, "warn", "cancelled")
        else:
            tasks.update(push_task, "fail", first_line(err))
        return
    tasks.update(push_task, "ok")

    rc_h, head_out, _ = git(ref.nested_path, ["rev-parse", "HEAD"])
    pushed_sha = head_out.strip() if rc_h == 0 else ""
    # Snapshot values supersede `ref.repo.…` reads when provided. See
    # `_commit_worker_inner` for the rationale (review-screen state
    # appears empty as soon as queue time, regardless of where this
    # worker is in the pipeline).
    track_wf_map = (track_workflow if track_workflow is not None
                    else ref.repo.track_workflow)
    tracked = [n for n, on in track_wf_map.items() if on]
    if tracked and pushed_sha:
        kick_off_post_push_run_tracking(
            state, ref.repo, nested_branch, pushed_sha, tracked)
    if track_workflow is None:
        ref.repo.track_workflow.clear()

    # "Then run after push" for the canonical — same semantics as
    # the top-level commit_worker version, including the
    # "__add_tag__" sentinel for creating a lightweight tag at the
    # pushed sha. Snapshot supersedes ref.repo when provided.
    if then_run_after_push or then_run_params_after_push is not None:
        after_push_target = then_run_after_push
        after_push_params = dict(then_run_params_after_push or {})
    else:
        after_push_target = ref.repo.then_run_after_push
        after_push_params = dict(ref.repo.then_run_params_after_push)
        ref.repo.then_run_after_push = ""
        ref.repo.then_run_params_after_push.clear()
    if after_push_target == "__add_tag__":
        tag_name = after_push_params.get("tag", "").strip()
        tag_label = (f"{state.task_repo_label(ref.repo)} "
                     f"(in {state.task_repo_label(parent)})")
        t_tag = tasks.add(f"{tag_label}: tag {tag_name or '(empty)'}")
        _create_and_push_tag(
            tasks, t_tag, ref.nested_path, tag_name, pushed_sha)
    elif after_push_target:
        # `after_push_params` was popped above and holds buffered
        # `workflow_dispatch.inputs` values; forward as -F flags so
        # the dispatched run honours the user's review-screen edits.
        kick_off_manual_dispatch(
            state, ref.repo, after_push_target, nested_branch,
            inputs=after_push_params)

    # Build the post-push sync targets. For tracked canonicals we sync
    # the top-level checkout first; for synthetic canonicals there's no
    # workspace top-level, so we only fan out to the sibling parents.
    # Each target also carries an optional `(parent, sub_path)` pair
    # so the per-row spinner toggle has something to flag — the
    # top-level canonical's spinner lives on `ref.repo.refreshing`
    # itself; the in-parent submodule rows live on a ChildRef.
    targets: List[Tuple[str, Path, Optional[Tuple[Repo, Path]]]] = []
    ref_label = state.task_repo_label(ref.repo)
    if not ref.repo.synthetic:
        targets.append((f"top-level {ref_label}", ref.repo.path, None))
    for other_parent, other_path in ref.repo.siblings:
        if other_path == ref.nested_path:
            continue
        targets.append(
            (f"{ref_label} in {state.task_repo_label(other_parent)}",
             other_path, (other_parent, other_path)))

    for label, target_path, child_pair in targets:
        t = tasks.add(f"  ↳ sync {label}", parent=push_task)
        # Flip the matching spinner on for the duration of the sync.
        # For the top-level canonical that's `ref.repo.refreshing`;
        # for an in-parent nested copy it's the ChildRef on the
        # other parent. The try/finally guarantees the flag clears
        # even on a mid-sync exception so the row can't spin forever.
        if child_pair is None:
            ref.repo.refreshing = True
        else:
            _set_child_ref_refreshing(
                child_pair[0], child_pair[1], True)
        try:
            ok, sync_msg = sync_sibling(target_path, nested_branch)
        finally:
            if child_pair is None:
                ref.repo.refreshing = False
            else:
                _set_child_ref_refreshing(
                    child_pair[0], child_pair[1], False)
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
    repo_blocks = {id(b.target_repo): b for b in blocks
                   if b.target_repo is not None and b.target_child is None}
    child_blocks = {id(b.target_child): b for b in blocks
                    if b.target_child is not None}

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
    repo_plans: List[Tuple[Repo, str, List[LFSCandidate],
                           "dict[str, bool]", bool,
                           "dict[str, bool]", str,
                           "dict[str, str]", bool]] = []
    for repo in state.repos:
        msg = repo.message.strip()
        if not msg:
            continue
        repo.message = ""
        # Claim the refresh slot via the mutex (replaces the previous
        # `if repo.refreshing: continue` flag check + later `= True`
        # assignment). `try_acquire_refresh` is atomic: either we own
        # the slot for the entire commit + post-commit refresh, or
        # another source already has it and we skip this row.
        if not repo.try_acquire_refresh():
            continue
        block = repo_blocks.get(id(repo))
        repo_cands = list(block.lfs_candidates) if block else []
        staged = dict(block.staged_paths) if block else {}
        amend = bool(block.amend) if block else False
        # Per-commit push decision from the review screen's toggle;
        # fall back to the workspace default when there's no block.
        push = bool(block.push) if block else state.auto_push
        # Snapshot + clear the after-push then-run state. After-
        # workflow chains (`then_run_after_workflow`) are read much
        # later by `_poll_run` and so are NOT cleared here — they
        # stay on Repo and remain susceptible to leak across review
        # screens. The Ctrl+K "clear chains" gesture covers that
        # explicitly if the user wants to reset.
        track_wf = dict(repo.track_workflow)
        then_push = repo.then_run_after_push
        then_push_params = dict(repo.then_run_params_after_push)
        repo.track_workflow.clear()
        repo.then_run_after_push = ""
        repo.then_run_params_after_push.clear()
        repo_plans.append((repo, msg, repo_cands, staged, amend,
                           track_wf, then_push, then_push_params, push))

    child_plans: List[Tuple[Repo, ChildRef, str,
                            "dict[str, bool]", bool,
                            "dict[str, bool]", str,
                            "dict[str, str]", bool]] = []
    for parent in state.repos:
        for ref in parent.children:
            if ref.kind != "submodule":
                continue
            msg = ref.message.strip()
            if not msg:
                continue
            ref.message = ""
            if not ref.try_acquire_refresh():
                continue
            block = child_blocks.get(id(ref))
            staged = dict(block.staged_paths) if block else {}
            amend = bool(block.amend) if block else False
            push = bool(block.push) if block else state.auto_push
            # Submodule child's then-run state lives on `ref.repo`
            # (the canonical) — same snapshot + clear pattern as the
            # top-level branch above.
            track_wf = dict(ref.repo.track_workflow)
            then_push = ref.repo.then_run_after_push
            then_push_params = dict(ref.repo.then_run_params_after_push)
            ref.repo.track_workflow.clear()
            ref.repo.then_run_after_push = ""
            ref.repo.then_run_params_after_push.clear()
            child_plans.append((parent, ref, msg, staged, amend,
                                track_wf, then_push, then_push_params, push))

    if not repo_plans and not child_plans:
        return

    # Capture the locked Repo / ChildRef objects directly so the
    # supervisor's finally releases the exact instances we acquired,
    # regardless of what `state.repos` looks like by then. A previous
    # version did `for r in state.repos: if id(r) in locked_ids: …` —
    # which silently dropped releases when `state.repos` got swapped
    # out between acquire and finally (workspace switch with cache
    # miss → `state.repos = fresh` rebuilds the list with new Repo
    # instances). Result: locks stranded for the rest of the process
    # lifetime, manifesting as rows stuck in `refreshing=True` with
    # an empty sidebar (the pipeline tasks have long since marked
    # terminal) and Ctrl+R unable to recover. Holding references to
    # the actual locked objects closes that hole — release operates
    # on the identity we acquired, not whatever happens to share the
    # path now.
    locked_repo_refs: List[Repo] = [plan[0] for plan in repo_plans]
    locked_child_refs: List[ChildRef] = [plan[1] for plan in child_plans]
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
    snapshot_repos: List[Repo] = list(state.repos)
    snapshot_subtrees = list(state.subtrees)

    workers: List[threading.Thread] = []
    for (repo, msg, repo_cands, staged, amend,
            track_wf, then_push, then_push_params, push) in repo_plans:
        w = threading.Thread(
            target=commit_worker,
            args=(state, repo, msg, repo_cands, staged, amend,
                  track_wf, then_push, then_push_params, push),
            daemon=True,
        )
        w.start()
        workers.append(w)

    for (parent, ref, msg, staged, amend,
            track_wf, then_push, then_push_params, push) in child_plans:
        w = threading.Thread(
            target=commit_worker_for_child,
            args=(state, parent, ref, msg, staged, amend,
                  track_wf, then_push, then_push_params, push),
            daemon=True,
        )
        w.start()
        workers.append(w)

    def supervisor() -> None:
        try:
            for w in workers:
                w.join()
            # Per-repo try/except so a failing refresh_repo (corrupt
            # index, missing .git, network hiccup mid-workflow-poll)
            # can't escape the loop and skip the release_refresh calls
            # that follow — that's how repos got "stuck refreshing"
            # forever, blocking the action menu with no way to recover
            # short of restarting idlegit.
            #
            # Iterates `snapshot_repos` (captured at queue time), not
            # `state.repos`, so a mid-pipeline workspace switch can't
            # leave the originally-committed repos un-refreshed and
            # showing pre-commit state when the user navigates back.
            for r in snapshot_repos:
                try:
                    refresh_repo(r)
                except Exception:  # noqa: BLE001
                    pass
            try:
                link_siblings(snapshot_repos, snapshot_subtrees)
            except Exception:  # noqa: BLE001
                pass
        finally:
            # Releases run in a finally so any earlier exception STILL
            # frees the locks. Without this, an exception in
            # refresh_repo / link_siblings strands every locked repo
            # and child ref. Iterates the captured refs directly so a
            # `state.repos` swap (workspace switch, fresh discovery)
            # between acquire and finally can't strand the lock.
            for r in locked_repo_refs:
                r.release_refresh()
            for ref in locked_child_refs:
                ref.release_refresh()

    threading.Thread(target=supervisor, daemon=True).start()


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


def _checkout_label(state: State, canonical: Repo,
                    parent: Optional[Repo]) -> str:
    """Sidebar-friendly checkout label. Both repo names go through
    `task_repo_label` so a long display name doesn't crowd out the
    surrounding "align Foo: stage at … in …" task description."""
    canonical_label = state.task_repo_label(canonical)
    if parent is None:
        return f"top-level {canonical_label}"
    return f"{canonical_label} in {state.task_repo_label(parent)}"


def _probe_checkout_full(state: State, path: Path, parent: Optional[Repo],
                         canonical: Repo) -> SmartSyncCheckout:
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
    rc, out, _ = git(path, [
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if rc == 0 and out.strip():
        upstream = out.strip()

    ahead = 0
    behind = 0
    if upstream:
        rc, out, _ = git(path, [
            "rev-list", "--count", "--left-right", f"{upstream}...HEAD"])
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
        canonical=canonical, parent=parent, path=path,
        branch=branch, label=_checkout_label(state, canonical, parent),
        head=head, dirty=dirty, ahead=ahead, behind=behind,
        upstream=upstream, signature=sig, sig_mtime=sig_mt,
    )


def _commit_dirty_winner(state: State, winner: SmartSyncCheckout,
                         name: str) -> bool:
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

    msg = suggest_commit_message_at(
        winner.path,
        max_added=state.suggest_added,
        max_updated=state.suggest_updated,
        max_deleted=state.suggest_deleted,
        auto_stage=True,
    ) or "consolidate working-tree changes"

    t = state.tasks.add(f"  ↳ align {name}: commit at {winner.label}")
    rc, _, err = git(winner.path, ["commit", "-m", msg])
    if rc != 0:
        state.tasks.update(t, "fail", first_line(err))
        return False
    state.tasks.update(t, "ok", msg)
    return True


def _push_winner(state: State, winner: SmartSyncCheckout,
                 branch: str, name: str) -> bool:
    """Push the winner's branch (with `--set-upstream` fallback for
    branches that don't yet have one). Plain `git push`, never forced."""
    t = state.tasks.add(f"  ↳ align {name}: push {winner.label}")
    if not is_safe_ref_arg(branch):
        state.tasks.update(t, "fail", "unsafe branch name")
        return False
    rc, _, err = git(winner.path, ["push"])
    if rc != 0:
        rc, _, err = git(
            winner.path, ["push", "--set-upstream", "origin", branch])
    if rc != 0:
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
    rc, _, _ = git(path, [
        "merge-base", "--is-ancestor", "HEAD", ref,
    ])
    return rc == 0


def _ref_is_ancestor_of_head(path: Path, ref: str) -> bool:
    """Mirror of `_head_is_ancestor_of`, but the other direction —
    True when `ref` is fully contained in HEAD's history. This is the
    auto-recovery condition: if `ref` is an ancestor of HEAD, then
    `git checkout -B <ref> HEAD` (which moves the branch ref forward
    to HEAD's commit) is a fast-forward of `<ref>` — every commit
    that was reachable from `<ref>` before is still reachable from it
    now, and HEAD's unique commits are also captured by the branch."""
    rc, _, _ = git(path, [
        "merge-base", "--is-ancestor", ref, "HEAD",
    ])
    return rc == 0


def _count_commits_between(path: Path, base_ref: str,
                           head_ref: str = "HEAD") -> int:
    """`git rev-list --count base_ref..head_ref` — number of commits
    reachable from `head_ref` but not from `base_ref`. Used by the
    recovery prompt to tell the user how many commits would be saved
    by the FF. Returns 0 on any error so the prompt still renders
    (just without the count)."""
    rc, out, _ = git(path, [
        "rev-list", "--count", f"{base_ref}..{head_ref}",
    ])
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
    rc, out, _ = git(path, [
        "symbolic-ref", "--short", "refs/remotes/origin/HEAD",
    ])
    if rc == 0 and out.strip().startswith("origin/"):
        candidate = out.strip()[len("origin/"):]
        rc2, _, _ = git(path, ["rev-parse", "--verify", candidate])
        if rc2 == 0:
            return candidate
    for candidate in ("master", "main"):
        rc, _, _ = git(path, ["rev-parse", "--verify", candidate])
        if rc == 0:
            return candidate
    return ""


def _build_recovery_prompt(path: Path, target_label: str,
                           target_branch: str = ""
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

    n_extra = (0 if head_to_branch
               else _count_commits_between(path, target_branch, "HEAD"))

    rc, head_out, _ = git(path, ["rev-parse", "HEAD"])
    head_sha = head_out.strip() if rc == 0 else ""

    return DetachedRecoveryPrompt(
        target_label=target_label,
        head_sha=head_sha,
        target_branch=target_branch,
        n_extra=n_extra,
        can_ff=can_ff,
    )


def execute_detached_recovery(path: Path,
                              target_branch: str) -> Tuple[bool, str]:
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


def _attempt_detached_recovery(state: State, path: Path,
                               target_label: str,
                               target_branch: str = ""
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


def _switch_to_branch(state: State, c: SmartSyncCheckout,
                      branch: str, name: str) -> bool:
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
_REDUNDANT_DIRTY_STATUS_CODES = frozenset({
    " M", "??", "M ", "MM", "A ", "AM",
})


def _verify_dirty_matches_target(c: SmartSyncCheckout,
                                 target_ref: str) -> Optional[bool]:
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


def _try_ff_through_redundant_dirty(state: State, c: SmartSyncCheckout,
                                    winner_branch: str,
                                    name: str) -> Optional[bool]:
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
    rc, _, _ = git(c.path, [
        "stash", "push", "--include-untracked", "-m", stash_msg,
    ])
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
        state.tasks.update(
            kept, "warn",
            "post-merge WT not clean — recover via `git stash list`")
        return False

    # Successful merge. Stash is intentionally NOT dropped (cardinal
    # rule). Surface the kept stash so the user knows where the
    # redundant-dirty content is parked and can prune at leisure.
    kept = state.tasks.add(f"  ↳ align {name}: stash kept on {c.label}")
    state.tasks.update(
        kept, "ok",
        "redundant dirty preserved — prune via `git stash drop`")
    return True


def _try_detached_checkout_through_redundant_dirty(
        state: State, c: SmartSyncCheckout,
        winner_branch: str, name: str) -> Optional[bool]:
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
    rc, _, _ = git(c.path, [
        "stash", "push", "--include-untracked", "-m", stash_msg,
    ])
    if rc != 0:
        return False

    rc, _, _ = git(c.path, ["checkout", target_ref])
    if rc != 0:
        git(c.path, ["stash", "pop"])
        return False

    if not _post_merge_clean(c.path):
        kept = state.tasks.add(f"  ↳ align {name}: stash kept on {c.label}")
        state.tasks.update(
            kept, "warn",
            "post-checkout WT not clean — recover via `git stash list`")
        return False

    # Successful checkout. Stash is intentionally NOT dropped.
    kept = state.tasks.add(f"  ↳ align {name}: stash kept on {c.label}")
    state.tasks.update(
        kept, "ok",
        "redundant dirty preserved — prune via `git stash drop`")
    return True


def _stash_switch_pop_winner(state: State, winner: SmartSyncCheckout,
                             branch: str, name: str) -> bool:
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
    t = state.tasks.add(
        f"  ↳ align {name}: switch {winner.label} → {branch}")
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
                state, winner.path, winner.label, target_branch=branch)
            if recovered:
                state.tasks.update(
                    t, "ok",
                    f"fast-forwarded {branch} to HEAD")
                return True
            state.tasks.update(
                t, "warn",
                f"{winner.label}: {msg}")
            return False
        state.tasks.update(
            t, "warn",
            f"{winner.label}: detached HEAD has commits not on {branch} "
            "— would orphan them; manual: `git checkout -b <name>` to keep them")
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
    rc, _, _ = git(winner.path, [
        "stash", "push", "--include-untracked",
        "-m", "auto: align detached HEAD",
    ])
    if rc != 0:
        state.tasks.update(t, "fail", initial_err)
        return False

    rc, _, err = git(winner.path, ["checkout", branch])
    if rc != 0:
        git(winner.path, ["stash", "pop"])  # restore on bail
        state.tasks.update(t, "fail",
                           f"checkout {branch}: {first_line(err)}")
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
            t, "fail",
            f"stash pop on {branch} conflicted — resolve conflict markers "
            "in WT, or `git reset --hard HEAD` then `git stash pop`")
        return False

    state.tasks.update(t, "ok")
    return True


def _align_loser_ff(state: State, c: SmartSyncCheckout,
                    winner_branch: str, name: str) -> bool:
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
    rc, _, err = git(
        c.path, ["merge", "--ff-only", f"origin/{winner_branch}"])
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
        rc_m, _, err_m = git(
            c.path, ["merge", "--no-edit", f"origin/{winner_branch}"])
        if rc_m == 0:
            state.tasks.update(t, "ok", "merged origin")
            return True
        err = err_m
    state.tasks.update(t, "warn", first_line(err))
    return False


def _align_detached_loser(state: State, c: SmartSyncCheckout,
                          winner_branch: str, name: str) -> bool:
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
            t, "warn",
            f"detached HEAD has commits not on {target_ref} "
            "— would orphan them; manual: `git checkout -b <name>`")
        return False
    rc, _, err = git(c.path, ["checkout", target_ref])
    if rc == 0:
        state.tasks.update(t, "ok")
        return True

    # Plain checkout refused — verify the dirty content is bit-
    # identical to origin/<branch>'s. If so, stash + retry + drop;
    # if any path differs, leave the loser alone and warn-skip with
    # the original git error so the user can resolve manually.
    redundant = _try_detached_checkout_through_redundant_dirty(
        state, c, winner_branch, name)
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
    rc, out, _ = git(path, [
        "symbolic-ref", "--short", "refs/remotes/origin/HEAD",
    ])
    if rc != 0:
        return ""
    ref = out.strip()
    if ref.startswith("origin/"):
        return ref[len("origin/"):]
    return ""


def _open_align_heads_prompt_and_wait(state: State,
                                      winner: SmartSyncCheckout) -> str:
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


def _align_canonical(state: State, canonical: Repo) -> Tuple[int, int]:
    """Plan + execute alignment for one canonical's checkouts. Returns
    (ok, fail) counts for the smart-sync header roll-up.

    Winner-selection rules:
      1. Multiple checkouts ahead of upstream → genuine cross-divergence;
         each has unique commits, can't auto-resolve. Warn-skip.
      2. Exactly one ahead → it's the winner.
      3. None ahead but some dirty → most-recent-mtime dirty wins (it'll
         be auto-staged + committed before push if `auto_stage` is on).
      4. None ahead, none dirty, but HEADs differ → most-recent commit
         time wins; the others FF up to it.

    All loser-alignment ops use `merge --ff-only` first for same-branch
    checkouts (then `merge --no-edit` when allowed by workspace config),
    or `checkout origin/<branch>` (detached) - git itself refuses on
    conflict, so we never overwrite uncommitted work."""
    name = state.task_repo_label(canonical)

    checkouts: List[SmartSyncCheckout] = [
        _probe_checkout_full(state, canonical.path, None, canonical)
    ]
    for parent, path in canonical.siblings:
        checkouts.append(
            _probe_checkout_full(state, path, parent, canonical))

    heads = {c.head for c in checkouts if c.head}
    any_dirty = any(c.dirty for c in checkouts)
    any_ahead = any(c.ahead > 0 for c in checkouts)
    if len(heads) <= 1 and not any_dirty and not any_ahead:
        # Already aligned — no task noise.
        return 0, 0

    aheads = [c for c in checkouts if c.ahead > 0]
    if len(aheads) > 1:
        t = state.tasks.add(f"  ↳ align {name}")
        labels = ", ".join(c.label for c in aheads)
        state.tasks.update(
            t, "warn",
            f"{len(aheads)} checkouts ahead — manual resolve: {labels}")
        return 0, 1

    winner: Optional[SmartSyncCheckout] = None
    if aheads:
        winner = aheads[0]
    elif any_dirty:
        winner = max(
            (c for c in checkouts if c.dirty), key=lambda c: c.sig_mtime)
    elif len(heads) > 1:
        def commit_time(c: SmartSyncCheckout) -> int:
            rc, out, _ = git(c.path, ["log", "-1", "--format=%ct", "HEAD"])
            try:
                return int(out.strip()) if rc == 0 else 0
            except ValueError:
                return 0
        winner = max(checkouts, key=commit_time)

    if winner is None:
        return 0, 0

    # Detached winner: pick a branch via the modal and switch onto it
    # BEFORE committing. Committing on a detached HEAD would create an
    # orphan commit that the post-checkout `git checkout <branch>`
    # silently leaves behind — push would then succeed but carry no
    # change, and losers would fail to align because origin still
    # holds the pre-edit content. Switching first means the
    # subsequent commit lands on the chosen branch and propagates
    # properly through push + loser-FF.
    winner_branch = winner.branch
    if winner_branch == "(detached)":
        if not state.align_heads:
            t = state.tasks.add(f"  ↳ align {name}")
            state.tasks.update(
                t, "warn",
                f"{winner.label} detached — turn on align-heads to pick a branch")
            return 0, 1
        if state.prompt_for_branch:
            chosen = _open_align_heads_prompt_and_wait(state, winner)
            if not chosen:
                t = state.tasks.add(f"  ↳ align {name}")
                state.tasks.update(
                    t, "warn", "user cancelled detached-branch pick")
                return 0, 1
        else:
            # `prompt_for_branch` off: auto-resolve to origin/HEAD
            # (whatever the remote considers its default branch).
            chosen = _resolve_origin_head_branch(winner.path)
            if not chosen:
                t = state.tasks.add(f"  ↳ align {name}")
                state.tasks.update(
                    t, "warn",
                    f"{winner.label}: origin/HEAD not set — turn on "
                    "prompt-for-branch to pick manually")
                return 0, 1
        if not _stash_switch_pop_winner(state, winner, chosen, name):
            return 0, 1
        winner.branch = chosen
        winner_branch = chosen

    # Stage + commit dirty winner (auto-stage off → warn-skip). With
    # the detached → branch switch done above, we're guaranteed to be
    # on a real branch by the time the commit lands.
    if winner.dirty:
        if not state.auto_stage:
            t = state.tasks.add(f"  ↳ align {name}")
            state.tasks.update(
                t, "warn",
                f"{winner.label} dirty — turn on auto-stage to consolidate")
            return 0, 1
        if not _commit_dirty_winner(state, winner, name):
            return 0, 1
        # We just produced a new local commit — make sure the push step
        # below fires. (refresh would re-derive ahead, but we're not
        # re-probing in the middle of the worker.)
        winner.ahead = max(winner.ahead, 1)

    # Push winner if it has unpushed commits (real or just-committed).
    if winner.ahead > 0:
        if not _push_winner(state, winner, winner_branch, name):
            return 0, 1

    # Re-probe winner.head — the branch switch / commit / push above
    # may have advanced HEAD to a brand-new sha that the original
    # `_probe_checkout_full` couldn't have known about. Without this,
    # the loser-skip shortcut below would compare each loser's head
    # against the PRE-OP winner sha and falsely skip any loser that
    # was already at that earlier point, leaving it 1+ behind origin.
    rc, head_out, _ = git(winner.path, ["rev-parse", "HEAD"])
    if rc == 0 and head_out.strip():
        winner.head = head_out.strip()

    # Align losers. With `auto_ff` off the user opted out of automatic
    # alignment entirely — winner still commits + pushes, but each
    # loser warn-skips so the user can resolve them manually (or in a
    # follow-up Ctrl+S after re-enabling).
    ok = 1 if winner.ahead > 0 else 0
    fail = 0
    for c in checkouts:
        if c is winner:
            continue
        if c.head == winner.head and not c.dirty:
            # Already in sync.
            continue
        if not state.auto_ff:
            t = state.tasks.add(f"  ↳ align {name}: {c.label}")
            state.tasks.update(
                t, "warn", "auto-ff off — manual align")
            fail += 1
            continue
        if c.branch == winner_branch:
            if _align_loser_ff(state, c, winner_branch, name):
                ok += 1
            else:
                fail += 1
        elif c.branch == "(detached)":
            if state.align_heads:
                if _align_detached_loser(state, c, winner_branch, name):
                    ok += 1
                else:
                    fail += 1
            else:
                t = state.tasks.add(f"  ↳ align {name}: {c.label}")
                state.tasks.update(t, "warn", "detached — align-heads off")
                fail += 1
        else:
            t = state.tasks.add(f"  ↳ align {name}: {c.label}")
            state.tasks.update(
                t, "warn",
                f"on '{c.branch}' (winner '{winner_branch}') — manual")
            fail += 1

    return ok, fail


def _propagate_submodule_bump(state: State, parent: Repo,
                              parent_label: str) -> str:
    """Stage + commit + push `parent` if its working tree is only dirty
    because of submodule pointer updates left over by smart-sync. No-op
    (returns "") when the parent has unrelated dirt, is on a detached
    HEAD, or any of the three steps fails. Returns the parent's new HEAD
    sha on success, so the cascade caller can FF parent's own siblings
    onto it.

    Uses the same Cardinal-Rule-safe primitives as `_commit_dirty_winner`
    and `_push_winner`: `safe_stage_all` (refuses pointer deletions),
    plain `git commit -m`, plain `git push` with `--set-upstream` fallback
    on the first miss. No force-pushes, no rebases, no hard resets.

    Sets `parent.refreshing = True` for the duration so the parent's
    row reads as "working" on the main screen — its state dot becomes
    the spinner, its commit field is hidden, and any concurrent action
    on the row is locked out. Wrapped in try/finally so the flag clears
    on every exit path (success, no-op, or failure mid-pipeline).
    """
    # Acquire the parent's refresh slot with a BLOCKING acquire —
    # the cascade is a user-initiated commit-pipeline step that
    # MUST run, so the silent-skip semantics of
    # `try_acquire_refresh` would manifest as a regression where
    # "I pushed the canonical but the parent's gitlink never got
    # bumped". The non-blocking variant raced fs_watcher's post-
    # task drain (which could fire on the parent in the brief gap
    # between push completion and the first sibling-sync task) and
    # silently dropped the propagation. fs_watcher's refresh is
    # bounded at ~100ms so 5s is generous headroom; on a stuck
    # contender we still bail rather than block the pipeline
    # forever.
    if not parent.acquire_refresh(timeout=5.0):
        t = state.tasks.add(f"  ↳ propagate {parent_label}")
        state.tasks.update(
            t, "warn", "skipped: parent refresh lock held by another op")
        return ""
    try:
        return _propagate_submodule_bump_inner(state, parent, parent_label)
    finally:
        parent.release_refresh()


def _propagate_submodule_bump_inner(state: State, parent: Repo,
                                    parent_label: str) -> str:
    """Inner body of `_propagate_submodule_bump` — extracted so the
    `parent.refreshing` flag toggle stays a single point of control."""
    refresh_repo(parent)
    if parent.error:
        return ""
    if not has_only_submodule_pointer_changes(parent.path):
        return ""

    rc, out, _ = git(parent.path, ["branch", "--show-current"])
    if rc != 0 or not out.strip():
        t = state.tasks.add(f"  ↳ propagate {parent_label}")
        state.tasks.update(
            t, "warn", "detached HEAD — no branch to commit on")
        return ""
    branch = out.strip()
    if not is_safe_ref_arg(branch):
        t = state.tasks.add(f"  ↳ propagate {parent_label}")
        state.tasks.update(t, "fail", "unsafe branch name")
        return ""

    # Name the submodule paths we're about to bump so the commit
    # message and the task row are both self-explanatory. Pulled from
    # the same porcelain that has_only_submodule_pointer_changes
    # already validated, so there's no need to re-check the codes.
    rc, status_out, _ = git(parent.path, ["status", "--porcelain=v1"])
    sub_paths: List[str] = []
    if rc == 0:
        for line in status_out.splitlines():
            if len(line) >= 3:
                sub_paths.append(line[3:].rstrip("/").strip())
    if len(sub_paths) == 1:
        msg = f"bump submodule {sub_paths[0]}"
    elif sub_paths:
        joined = ", ".join(sub_paths)
        msg = f"bump submodules {joined}"
    else:
        msg = "bump submodule pointer(s)"

    t = state.tasks.add(f"  ↳ propagate {parent_label}: stage")
    ok, stage_err = safe_stage_all(parent.path)
    if not ok:
        state.tasks.update(t, "fail", stage_err)
        return ""
    state.tasks.update(t, "ok")

    t = state.tasks.add(f"  ↳ propagate {parent_label}: commit")
    rc, _, err = git(parent.path, ["commit", "-m", msg])
    if rc != 0:
        state.tasks.update(t, "fail", first_line(err))
        return ""
    state.tasks.update(t, "ok", msg)

    t = state.tasks.add(f"  ↳ propagate {parent_label}: push")
    rc, _, err = git(parent.path, ["push"])
    if rc != 0:
        rc, _, err = git(
            parent.path, ["push", "--set-upstream", "origin", branch])
    if rc != 0:
        state.tasks.update(t, "fail", first_line(err))
        return ""
    state.tasks.update(t, "ok")

    rc, out, _ = git(parent.path, ["rev-parse", "HEAD"])
    if rc != 0 or not out.strip():
        return ""
    return out.strip()


def _ff_submodule_checkout_to(path: Path, branch: str,
                              target_sha: str) -> bool:
    """Fetch + `merge --ff-only target_sha` inside a submodule checkout,
    but only when the checkout is on `branch`, clean, and the FF would
    not orphan any local commits. Used by the propagation cascade to
    advance the submodule-checkout of a just-pushed parent up to the
    parent's new HEAD before checking the grandparent's gitlink. Refuses
    on dirty / off-branch / divergent state — propagation simply stops
    that cascade path."""
    if not is_safe_ref_arg(branch):
        return False
    rc, _, _ = git(path, ["fetch", "origin", branch])
    if rc != 0:
        return False
    rc, out, _ = git(path, ["branch", "--show-current"])
    if rc != 0 or out.strip() != branch:
        return False
    rc, out, _ = git(path, ["status", "--porcelain=v1"])
    if rc != 0 or out.strip():
        return False
    # Already at the target — nothing to do.
    rc, out, _ = git(path, ["rev-parse", "HEAD"])
    if rc == 0 and out.strip() == target_sha:
        return True
    # Strict FF: target_sha must be reachable from HEAD's history (i.e.
    # HEAD is an ancestor of target_sha). Refuses to orphan local-only
    # commits the way a hard reset would.
    rc, _, _ = git(
        path, ["merge-base", "--is-ancestor", "HEAD", target_sha])
    if rc != 0:
        return False
    rc, _, _ = git(path, ["merge", "--ff-only", target_sha])
    return rc == 0


def _cascade_propagate_to_parents(state: State,
                                  canonicals_synced: List[Repo]) -> None:
    """Walk up through every parent that holds a stale submodule gitlink
    after the canonical sync. For each parent whose only dirt is the
    submodule pointer(s), commit + push it; then FF the parent's own
    sibling submodule checkouts onto the new HEAD and recurse into any
    grandparent that thereby gains only-submodule-pointer dirt.

    Visited-by-id() bookkeeping prevents revisiting the same parent
    when several synced canonicals share it. Failures on one cascade
    path (refusal to FF a sibling, dirty parent, etc.) don't poison
    other branches — they just stop that branch's walk."""
    visited: "set[int]" = set()
    pending: List[Repo] = []
    for canonical in canonicals_synced:
        for parent, _sub_path in canonical.siblings:
            if id(parent) not in visited and parent not in pending:
                pending.append(parent)

    while pending:
        parent = pending.pop(0)
        if id(parent) in visited:
            continue
        visited.add(id(parent))

        parent_label = state.task_repo_label(parent)
        new_head = _propagate_submodule_bump(state, parent, parent_label)
        if not new_head:
            continue

        # Parent is now ahead of every other on-disk checkout of itself
        # (the submodule-checkouts that live inside grandparents). FF
        # those checkouts so each grandparent's gitlink check sees only
        # submodule-pointer dirt.
        rc, branch_out, _ = git(parent.path, ["branch", "--show-current"])
        if rc != 0 or not branch_out.strip():
            continue
        branch = branch_out.strip()
        for grandparent, sub_path in parent.siblings:
            grandparent_label = state.task_repo_label(grandparent)
            t = state.tasks.add(
                f"  ↳ propagate {parent_label}: align in "
                f"{grandparent_label}")
            # Flag the matching ChildRef on the grandparent for the
            # duration of the FF so its row animates while the
            # underlying checkout moves forward. try/finally ensures
            # the flag clears even if the FF helper raises.
            _set_child_ref_refreshing(grandparent, sub_path, True)
            try:
                ff_ok = _ff_submodule_checkout_to(sub_path, branch, new_head)
            finally:
                _set_child_ref_refreshing(grandparent, sub_path, False)
            if ff_ok:
                state.tasks.update(t, "ok")
                if id(grandparent) not in visited:
                    pending.append(grandparent)
            else:
                state.tasks.update(
                    t, "warn", "skipped — non-FF or dirty checkout")


def _smart_sync_set_canonical_tree_refreshing(canonical: Repo, value: bool
                                             ) -> None:
    """Mark a canonical repo and every nested submodule ChildRef that
    points at it as refreshing (or clear). Smart-sync aligns multiple
    on-disk checkouts of the same URL; without flipping ChildRef flags,
    only the top-level row shows the working spinner."""
    canonical.refreshing = value
    for parent, _nested_path in canonical.siblings:
        for ref in parent.children:
            if ref.kind == "submodule" and ref.repo is canonical:
                ref.refreshing = value


def _set_child_ref_refreshing(parent: Repo, sub_path: Path,
                              value: bool) -> None:
    """Toggle the `refreshing` flag on the ChildRef of `parent` whose
    nested-path matches `sub_path`. No-op when the ref isn't found —
    handles a stale `link_siblings` snapshot gracefully. Used by the
    commit pipeline's post-push fan-out and the parent-propagation
    cascade so the per-submodule row animates while its underlying
    checkout is being advanced."""
    for ref in parent.children:
        if ref.kind == "submodule" and ref.nested_path == sub_path:
            ref.refreshing = value
            return


def kick_off_sync_siblings(state: State) -> None:
    """Entry point for Ctrl+S — align every canonical's submodule
    checkouts (and pull subtrees). Non-destructive throughout.

    Canonicals are processed serially so the AlignHeadsPrompt modal
    can block one canonical's worker without blocking others, and so
    the user sees a coherent stream of tasks for one repo at a time.
    Subtrees fire in series after the canonicals."""
    canonicals_with_siblings = [r for r in state.repos if r.siblings]
    subtree_items: List[Tuple[Repo, ChildRef]] = []
    for parent in state.repos:
        for ref in parent.children:
            if ref.kind == "subtree":
                subtree_items.append((parent, ref))

    if not canonicals_with_siblings and not subtree_items:
        t = state.tasks.add("smart-sync")
        state.tasks.update(
            t, "warn", "no submodules or subtrees to sync")
        return

    work_count = len(canonicals_with_siblings) + len(subtree_items)
    header = state.tasks.add(f"smart-sync ({work_count})")

    # Lock synchronously so the very next redraw shows spinners on every
    # checkout involved (canonical row + nested submodule ChildRefs).
    # Each canonical also gets a tagged sentinel task carrying
    # `holds_repo` — `kick_off_inline_refresh` consults
    # `repo_has_active_job` to skip refresh on those rows. Without the
    # tag, smart-sync's direct `refreshing=True` setter doesn't take
    # the per-repo refresh_lock, so a concurrent Ctrl+R would succeed
    # at `try_acquire_refresh` and stomp on smart-sync's mid-flight
    # state. The sentinel is the source of truth for the active-job
    # check; the lockless `refreshing=True` is still set so the row
    # spinner animates immediately, before this thread starts running.
    sentinel_by_canonical: "dict[int, Task]" = {}
    for canonical in canonicals_with_siblings:
        _smart_sync_set_canonical_tree_refreshing(canonical, True)
        sent = state.tasks.add(
            f"  ↳ smart-sync {state.task_repo_label(canonical)}",
            parent=header)
        state.tasks.set_meta(sent, holds_repo=canonical)
        sentinel_by_canonical[id(canonical)] = sent
    sentinel_by_subtree: "dict[int, Task]" = {}
    for parent, ref in subtree_items:
        ref.refreshing = True
        sent = state.tasks.add(
            f"  ⊕ smart-sync {state.task_repo_label(ref.repo)}",
            parent=header)
        # Subtree alignment writes through the parent's working tree
        # (subtree pulls land as a parent commit), so tag the parent
        # as the held repo — refresh of the parent would race the
        # subtree-pull's commit step otherwise.
        state.tasks.set_meta(sent, holds_repo=parent)
        sentinel_by_subtree[id(ref)] = sent

    def worker() -> None:
        ok_total = 0
        fail_total = 0
        try:
            for canonical in canonicals_with_siblings:
                try:
                    ok, fail = _align_canonical(state, canonical)
                except Exception as e:
                    t = state.tasks.add(
                        f"  ↳ align {state.task_repo_label(canonical)}")
                    state.tasks.update(t, "fail", first_line(str(e)))
                    ok, fail = 0, 1
                finally:
                    refresh_repo(canonical)
                    # Release the active-job tag as soon as this
                    # canonical's local work is done so refresh paths
                    # can resume on it. The lockless `refreshing=True`
                    # flag stays high until the outer `finally` below
                    # so the row keeps spinning until the final batch
                    # refresh + link rebuild lands.
                    sent = sentinel_by_canonical.get(id(canonical))
                    if sent is not None:
                        state.tasks.update(
                            sent, "ok" if fail == 0 else "warn",
                            "" if fail == 0 else f"{fail} failed")
                ok_total += ok
                fail_total += fail

            # Auto-push each parent whose only dirt is the now-stale
            # submodule gitlink (cascading up through grandparents that
            # also become only-submodule-dirty). Off → leaves the parent
            # commit as a manual decision, which is what existing setups
            # got before this knob existed.
            if (state.auto_push_submodule_parent
                    and canonicals_with_siblings):
                try:
                    _cascade_propagate_to_parents(
                        state, canonicals_with_siblings)
                except Exception as e:
                    t = state.tasks.add("  ↳ propagate to parents")
                    state.tasks.update(t, "fail", first_line(str(e)))
                    fail_total += 1

            for parent, ref in subtree_items:
                t = state.tasks.add(
                    f"  ⊕ {state.task_repo_label(ref.repo)} "
                    f"in {state.task_repo_label(parent)}")
                ok_this = False
                try:
                    try:
                        prefix = str(ref.nested_path.relative_to(parent.path))
                    except ValueError:
                        prefix = ""
                    ok, msg = sync_subtree(
                        parent.path, prefix,
                        ref.repo.remote_url_raw or "", ref.repo.branch)
                    state.tasks.update(t, "ok" if ok else "fail", msg)
                    ok_this = ok
                    if ok:
                        ok_total += 1
                    else:
                        fail_total += 1
                finally:
                    refresh_repo(parent)
                    sent = sentinel_by_subtree.get(id(ref))
                    if sent is not None:
                        state.tasks.update(
                            sent, "ok" if ok_this else "warn",
                            "" if ok_this else "subtree sync failed")
        finally:
            # Final full refresh + sibling-link rebuild BEFORE we
            # clear `refreshing` flags or mark the header task
            # terminal. Order matters: clearing flags AND marking the
            # header terminal both drop `anim_running` in the main
            # loop, which then switches its getch timeout from 100ms
            # to 1s — and the per-repo `refresh_repo` calls below run
            # serially on this thread, so with N repos the loop spends
            # ~N×100ms (or longer) on the 1s tick before redrawing
            # the post-sync state. The user perceives this as the row
            # icons staying stale for "a couple of seconds" after the
            # sync tasks complete. Running the refresh while the
            # spinner state is still live keeps the loop on the 100ms
            # tick so the next redraw after release-flags lands within
            # a frame.
            #
            # Parallelised across MAX_PARALLEL_GIT_JOBS (matches
            # `kick_off_inline_refresh`) — each `refresh_repo` is a
            # handful of independent git subprocesses, network-free,
            # so contention is just the subprocess fork cost.
            # Wrap the refresh in a per-repo guard so a single failing
            # `refresh_repo` (corrupt index, missing .git/, …) can't
            # raise out of the finally and skip the rest of the
            # cleanup. A raised exception here would otherwise leave
            # spinner flags stuck True forever AND the header task in
            # "running" status — both blocking, no way for the user
            # to recover without restarting the app.
            def _refresh_safe(r: Repo) -> None:
                try:
                    refresh_repo(r)
                except Exception:  # noqa: BLE001
                    pass
            if state.repos:
                max_workers = min(
                    len(state.repos), MAX_PARALLEL_GIT_JOBS)
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    list(ex.map(_refresh_safe, state.repos))
            try:
                link_siblings(state.repos, state.subtrees)
            except Exception:  # noqa: BLE001
                # link_siblings is mostly a re-link of in-memory
                # ChildRef objects, but it does call git for HEAD/
                # branch/etc. on each nested checkout. Treat the same
                # as a per-repo refresh failure — swallow so cleanup
                # still runs.
                pass

            for canonical in canonicals_with_siblings:
                _smart_sync_set_canonical_tree_refreshing(canonical, False)
            for _parent, ref in subtree_items:
                ref.refreshing = False

            # Defensive sweep: an earlier exception (failed
            # _align_canonical, cascade error) could leave a sentinel in
            # `running` status, which would keep `repo_has_active_job`
            # returning True forever and block all subsequent refreshes
            # of that canonical. Mark any still-running sentinel as
            # warn so the active-job check clears.
            for sent in list(sentinel_by_canonical.values()):
                if sent.status not in ("ok", "fail", "warn"):
                    state.tasks.update(sent, "warn", "smart-sync aborted")
            for sent in list(sentinel_by_subtree.values()):
                if sent.status not in ("ok", "fail", "warn"):
                    state.tasks.update(sent, "warn", "smart-sync aborted")

            total = ok_total + fail_total
            if total == 0:
                state.tasks.update(header, "ok", "all aligned")
            elif fail_total == 0:
                state.tasks.update(header, "ok", f"{ok_total} synced")
            elif ok_total == 0:
                state.tasks.update(header, "fail", f"{fail_total} failed")
            else:
                state.tasks.update(
                    header, "warn", f"{ok_total} ok / {fail_total} failed")

    threading.Thread(target=worker, daemon=True).start()


# ---------- Inline refresh ------------------------------------------------


_inline_refresh_lock = threading.Lock()
_inline_refresh_in_flight = False


def kick_off_inline_refresh(state: State) -> None:
    """Re-discover repos in the workspace, removing gone entries and adding
    new ones inline, and refresh every kept/new repo in parallel — each
    one toggling its `refreshing` flag so the row spinner animates next to
    its name. The main view stays visible the whole time; no overlay.

    Gated by `_inline_refresh_in_flight` so a second Ctrl+R while a refresh
    is still running becomes a no-op rather than spawning a second worker
    that races the first inside `link_siblings` (both call `r.children = []`
    idempotently but then `append` non-idempotently — the result is
    duplicated submodule rows under each parent)."""
    global _inline_refresh_in_flight
    with _inline_refresh_lock:
        if _inline_refresh_in_flight:
            return
        _inline_refresh_in_flight = True

    # Prefer the active workspace's folder list when available — it
    # supports multi-folder workspaces (which the legacy
    # `state.repos[0].path.parent` anchor couldn't, silently dropping
    # repos discovered from any folder other than the first one).
    # Pin the workspace this refresh belongs to. The worker runs async;
    # if the user switches workspaces before it finishes, we must not
    # assign the discovered list to whichever workspace is *currently*
    # active — that was the "always one workspace to the left" bug.
    target_idx = state.active_workspace_index
    target_ws = state.active_workspace
    subtrees = (list(target_ws.subtrees) if target_ws is not None
                else list(state.subtrees))
    folders = list(state.active_folders)
    if not folders:
        if state.repos:
            anchor = state.repos[0]
            folders = [anchor.path if anchor.rel == "." else anchor.path.parent]
        else:
            # No repos and no workspace folders — release the gate and bail.
            with _inline_refresh_lock:
                _inline_refresh_in_flight = False
            return

    # Claim the per-repo refresh slot SYNCHRONOUSLY before we spawn the
    # worker. `try_acquire_refresh` flips `refreshing=True` so the main
    # loop's `anim_running` check fires immediately (the next iteration
    # drops the getch timeout from 1s to 100ms and row spinners light
    # up), AND it acquires `repo.refresh_lock` so no other source can
    # start a concurrent refresh on the same repo. Repos already locked
    # by another source (fs_watcher, action menu) are skipped — the
    # current owner will finish refreshing them. Without this gate, a
    # Ctrl+R during an in-flight auto-refresh would race the auto-
    # refresh on the same Repo's `staged`/`unstaged`/etc. lists and
    # leave the row briefly flickering between half-populated states.
    #
    # `repo_has_active_job` is the explicit "running job" check — it
    # catches workers whose lock isn't held (smart-sync's lockless
    # `refreshing=True` setter on a canonical mid-align) so we never
    # refresh on top of in-flight work. Lock check + job check are
    # belt-and-braces: either reason to bail is enough.
    repos_snapshot = list(state.repos)
    acquired: List[Repo] = []
    skipped_active: List[Repo] = []
    for r in repos_snapshot:
        if state.tasks.repo_has_active_job(r):
            skipped_active.append(r)
            continue
        if r.try_acquire_refresh():
            acquired.append(r)

    def worker() -> None:
        global _inline_refresh_in_flight
        try:
            fresh: List[Repo] = []
            seen_paths: set = set()
            for folder in folders:
                try:
                    discovered = discover_repos(folder)
                except Exception as e:
                    t = state.tasks.add(f"refresh {folder}")
                    state.tasks.update(t, "warn", first_line(str(e)))
                    discovered = []
                for r in discovered:
                    if r.path in seen_paths:
                        continue
                    seen_paths.add(r.path)
                    fresh.append(r)
            fresh_by_path = {r.path: r for r in fresh}
            kept_by_path = {r.path: r for r in repos_snapshot
                            if r.path in fresh_by_path}
            next_repos: List[Repo] = []
            for r in fresh:
                next_repos.append(kept_by_path.get(r.path, r))
            next_repos.sort(
                key=lambda r: (r.rel != ".", r.rel.lower() if r.rel != "." else ""))

            # Newly-discovered repos that weren't in `state.repos` at
            # sync-acquire time need their own claim. Membership check
            # uses `path` identity rather than `r in acquired` so we
            # don't depend on Repo equality (the dataclass `__eq__`
            # compares many fields and could surprise us).
            acquired_paths = {a.path for a in acquired}
            skipped_active_paths = {r.path for r in skipped_active}
            for r in next_repos:
                if r.path in acquired_paths:
                    continue
                if r.path in skipped_active_paths:
                    continue
                if state.tasks.repo_has_active_job(r):
                    skipped_active.append(r)
                    skipped_active_paths.add(r.path)
                    continue
                if r.try_acquire_refresh():
                    acquired.append(r)
                    acquired_paths.add(r.path)

            # Refresh only repos we own. Vanished repos (acquired but
            # not in next_repos) are released in the `finally` below
            # without a refresh — they're about to fall off the list
            # anyway. Locked repos (in next_repos but not acquired)
            # are skipped — their current owner is mid-refresh and
            # will leave them in a consistent state.
            repos_to_refresh = [r for r in next_repos
                                if r.path in acquired_paths]

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
                t = state.tasks.add(
                    f"{state.task_repo_label(r)}: refresh skipped")
                # Distinguish "active job" (a worker is mutating this
                # repo's working tree right now) from "locked" (refresh
                # lock held — most often by a sibling refresh or
                # fs_watcher fire) so the user sees WHY their Ctrl+R
                # was a no-op on this row.
                if r.path in skipped_active_paths:
                    state.tasks.update(t, "warn", "task in progress")
                else:
                    state.tasks.update(t, "warn", "locked by another worker")

            # `fetch_on_manual_refresh` (default off) makes Ctrl+R do
            # a `git fetch --all` per repo BEFORE the local state
            # re-read so the displayed ahead/behind reflects actual
            # upstream rather than the last fetch. Fetch failures are
            # silently swallowed — they don't fail the refresh; the
            # local state re-read still runs and the ahead/behind
            # numbers just stay stale.
            do_fetch = state.fetch_on_manual_refresh

            def refresh_one(r: Repo) -> None:
                if do_fetch:
                    try:
                        git(r.path, ["fetch", "--all"])
                    except Exception:  # noqa: BLE001
                        pass
                refresh_repo_with_remote_state(r)

            if repos_to_refresh:
                max_workers = min(
                    len(repos_to_refresh), MAX_PARALLEL_GIT_JOBS)
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    list(ex.map(refresh_one, repos_to_refresh))
            link_siblings(next_repos, subtrees)

            if 0 <= target_idx < len(state.workspaces):
                state.workspaces[target_idx].cached_repos = next_repos

            # Only repaint the live UI when the user is still on the
            # workspace we refreshed — otherwise we'd flash the wrong
            # repo list (and poison the new workspace's cache).
            if state.active_workspace_index == target_idx:
                # Capture focus before swapping the list so navigation
                # the user made while refresh was in flight is preserved.
                # Index-only clamping used to snap to row 0 whenever the
                # body shrank (e.g. submodule rows missing mid-refresh).
                focus_key = state.body_focus_key()
                state.repos = next_repos
                if focus_key is not None:
                    state.restore_body_focus(focus_key)
                elif state.selected >= 0:
                    rows = state.selectable_rows()
                    if rows:
                        state.selected = max(
                            0, min(state.selected, len(rows) - 1))

                # Reconcile fs-watchers against the new repo set: attach for
                # newly-appeared repos, drop watchers for repos that vanished.
                # Idempotent + safe to call even when the feature flag is off
                # (it stops any existing watchers). Lazy import keeps watchdog
                # off the workers module's import path for tests that stub
                # workers without touching the watcher manager.
                from .fs_watcher import reconcile_repo_watchers
                reconcile_repo_watchers(state)
        finally:
            # Always release every claim, including for repos that
            # vanished (not in next_repos) or that we skipped because
            # another source held the lock — release on the latter is
            # a no-op since we never acquired. Done inside the
            # try/finally so a discover-time exception can't leave a
            # repo permanently locked.
            for r in acquired:
                r.release_refresh()
            with _inline_refresh_lock:
                _inline_refresh_in_flight = False

    threading.Thread(target=worker, daemon=True).start()


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

    acquired: List[Repo] = []
    n_skipped_active = 0
    for r in state.repos:
        # `repo_has_active_job` skips repos that have a live commit /
        # push / smart-sync worker tagged against them so pull-all
        # doesn't compete with mid-flight work. Treated the same as
        # the lock-held case for the summary count below.
        if state.tasks.repo_has_active_job(r):
            n_skipped_active += 1
            continue
        if r.try_acquire_refresh():
            acquired.append(r)

    n_total = len(state.repos)
    n_skipped_locked = n_total - len(acquired) - n_skipped_active
    # Track outcomes from worker threads — use a lock since
    # ThreadPoolExecutor runs `pull_one` concurrently. ints, but
    # we wrap reads/writes in a tiny critical section so the final
    # summary count is stable.
    counters_lock = threading.Lock()
    n_pulled = [0]
    n_up_to_date = [0]
    n_skipped_no_upstream = [0]
    n_failed = [0]

    def worker() -> None:
        try:
            def pull_one(r: Repo) -> None:
                name = state.task_repo_label(r)
                # Skip silently when there's no upstream — pulling
                # against nothing isn't a meaningful op and the
                # task row would just be noise on a workspace with
                # any local-only repos. Still tracked in the
                # summary so the user sees "5 had no upstream".
                rc, out, _ = git(r.path, [
                    "rev-parse", "--abbrev-ref",
                    "--symbolic-full-name", "@{u}"])
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
                    r.path, state.tasks, name,
                    allow_merge_fallback=False,
                    parent_task=parent_task)
                _, after, _ = git(r.path, ["rev-parse", "HEAD"])
                with counters_lock:
                    if not ok:
                        n_failed[0] += 1
                    elif before.strip() == after.strip():
                        n_up_to_date[0] += 1
                    else:
                        n_pulled[0] += 1

            if acquired:
                max_workers = min(len(acquired), MAX_PARALLEL_GIT_JOBS)
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    list(ex.map(pull_one, acquired))

            # Re-read state for every repo we touched so ahead/behind,
            # HEAD sha, and dirty flags reflect the post-pull world.
            # Skip the gh workflow query — pull doesn't change which
            # workflows exist, and the inline-refresh path will pick
            # any drift up on the next Ctrl+R.
            for r in acquired:
                refresh_repo(r)
            link_siblings(state.repos, state.subtrees)
        finally:
            for r in acquired:
                r.release_refresh()
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
                parts.append(
                    f"{n_skipped_no_upstream[0]} no upstream")
            if n_skipped_locked:
                parts.append(f"{n_skipped_locked} locked")
            if n_skipped_active:
                parts.append(f"{n_skipped_active} busy")
            if n_failed[0]:
                parts.append(f"{n_failed[0]} failed")
            summary = ", ".join(parts) if parts else "no repos"
            if n_failed[0]:
                status = "fail"
            elif (n_skipped_locked or n_skipped_active
                    or n_skipped_no_upstream[0]):
                status = "warn"
            else:
                status = "ok"
            state.tasks.update(parent_task, status, summary)

    threading.Thread(target=worker, daemon=True).start()


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
        state.repos = ws.cached_repos
        kick_refresh = True
    else:
        # Cache miss (newly-added workspace) — do the expensive bits.
        # Sync discovery is fast; remote-state refresh stays async so
        # the UI doesn't block.
        fresh: List[Repo] = []
        seen_paths: set = set()
        for folder in ws.folders:
            try:
                discovered = discover_repos(folder)
            except Exception as e:
                t = state.tasks.add(f"switch {folder}")
                state.tasks.update(t, "warn", first_line(str(e)))
                discovered = []
            for r in discovered:
                if r.path in seen_paths:
                    continue
                seen_paths.add(r.path)
                fresh.append(r)
        fresh.sort(key=lambda r: r.display_name.lower())
        state.repos = fresh
        ws.cached_repos = fresh
        kick_refresh = True

    state.workspace_name = ws.name

    # Re-apply settings from base config + this workspace's overrides.
    # Imported here to avoid a hard config dependency in workers' module
    # namespace (workers is a leaf used by tests that don't load config).
    from .config import apply_workspace_overrides
    if state.base_config is not None:
        apply_workspace_overrides(state, state.base_config, ws)
    else:
        state.subtrees = list(ws.subtrees)

    # Always re-link — link_siblings is idempotent and inexpensive on
    # already-refreshed Repos, and the cache-hit path skipped any
    # discovery that would have touched these references.
    link_siblings(state.repos, state.subtrees)

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

    # Persist the new active workspace name so the next session lands
    # the user back here automatically. Save failures are non-fatal —
    # the in-memory switch already happened, the file just won't
    # remember it; on next launch we'll default to the first workspace.
    from .config import save_workspaces
    try:
        save_workspaces(state.workspaces, state.active_workspace_index)
    except OSError:
        pass

    # Reconcile fs-watchers for the new repo set. When kick_refresh is
    # True the subsequent kick_off_inline_refresh will reconcile again
    # at its tail (and pick up any repos that got refreshed in-place);
    # we still reconcile here so the cache-hit path attaches watchers
    # immediately rather than waiting for the next Ctrl+R.
    from .fs_watcher import reconcile_repo_watchers
    reconcile_repo_watchers(state)

    if kick_refresh:
        kick_off_inline_refresh(state)


# ---------- Update check (GitHub Releases API) ---------------------------


def kick_off_check_for_updates(menu) -> None:
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

    def worker() -> None:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": user_agent,
                "Accept": "application/vnd.github+json",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or not data.get("tag_name"):
                menu.update_check_error = (
                    "release response missing tag_name")
                menu.update_check = "failed"
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
        except urllib.error.URLError as e:
            menu.update_check_error = f"network: {e.reason}"
            menu.update_check = "failed"
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            menu.update_check_error = f"parse error: {e}"
            menu.update_check = "failed"
        except OSError as e:
            menu.update_check_error = f"i/o: {e}"
            menu.update_check = "failed"

    threading.Thread(target=worker, daemon=True).start()


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
    if screen.repo_locked and screen.target_repo is not None:
        screen.target_repo.release_refresh()
        screen.repo_locked = False
    if screen.child_locked and screen.target_child is not None:
        screen.target_child.release_refresh()
        screen.child_locked = False


def _safe_merge_refresh_targets(state: State,
                                screen: SafeMergeScreen) -> None:
    """Re-query the affected repo (and rebuild sibling links) so the row
    icons reflect the post-merge state. Per-repo guarded so a single
    failing refresh can't strand the teardown."""
    repo = screen.target_repo
    if repo is not None:
        try:
            refresh_repo(repo)
        except Exception:  # noqa: BLE001
            pass
    if screen.target_parent is not None and screen.target_parent is not repo:
        try:
            refresh_repo(screen.target_parent)
        except Exception:  # noqa: BLE001
            pass
    try:
        link_siblings(state.repos, state.subtrees)
    except Exception:  # noqa: BLE001
        pass


def safe_merge_abort(state: State, screen: SafeMergeScreen) -> None:
    """Tear down after the user dismisses the dialog. Cardinal-Rule safe:
    we NEVER run `git merge --abort` (it resets the working tree). An
    in-progress merge is simply left in place — its conflicts stay in the
    tree and the backup stash is preserved, so the user can finish by hand
    or re-open safe-merge (which adopts the existing conflicts)."""
    screen.cancel_event.set()
    header = screen.header_task
    if header is not None and header.status not in ("ok", "fail", "warn"):
        if screen.phase == "confirm":
            # The merge commit exists; only push/sync was skipped.
            state.tasks.update(
                header, "warn", "merge committed — push skipped")
        elif merge_head_sha(screen.target_path):
            state.tasks.update(
                header, "warn",
                "merge left in progress — re-open safe-merge to finish")
        else:
            state.tasks.update(header, "warn", "cancelled")
    _safe_merge_refresh_targets(state, screen)
    _safe_merge_release_locks(screen)


def kick_off_safe_merge(state: State, *, target_label: str,
                        target_path: Path,
                        target_repo: Optional[Repo],
                        target_parent: Optional[Repo],
                        target_child: Optional[ChildRef] = None,
                        merge_ref: str = "",
                        branch_label: str = "") -> bool:
    """Open the safe-merge dialog for `target_path`. `merge_ref` is the ref
    to merge in; pass "" to ADOPT an already in-progress merge (resolve its
    existing conflicts). Claims the target's refresh slot for the whole
    flow, builds the screen, and spawns the begin worker. Returns True when
    the dialog opened (False if the target was busy)."""
    if target_child is None:
        target_child = _find_child_at(target_parent, target_path)
    repo_locked = False
    child_locked = False
    if target_repo is not None:
        repo_locked = target_repo.try_acquire_refresh()
        if not repo_locked:
            t = state.tasks.add(f"safe-merge {target_label}: skipped")
            state.tasks.update(t, "warn", "refresh in progress — try again")
            return False
    if target_child is not None:
        child_locked = target_child.try_acquire_refresh()
        if not child_locked:
            if repo_locked and target_repo is not None:
                target_repo.release_refresh()
            t = state.tasks.add(f"safe-merge {target_label}: skipped")
            state.tasks.update(t, "warn", "refresh in progress — try again")
            return False

    header = state.tasks.add(
        f"safe-merge {target_label}"
        + (f": merge {merge_ref}" if merge_ref else ": resolve conflicts"))
    screen = SafeMergeScreen(
        target_label=target_label,
        target_path=target_path,
        target_repo=target_repo,
        target_parent=target_parent,
        target_child=target_child,
        merge_ref=merge_ref,
        is_tracked_submodule=(
            target_child is not None
            or bool(target_repo is not None and target_repo.siblings)),
        confirm_remove_stash=state.auto_remove_backup_stash_after_merge,
        header_task=header,
        repo_locked=repo_locked,
        child_locked=child_locked,
        phase="preparing",
    )
    state.tasks.set_meta(
        header, holds_repo=target_repo, holds_child=target_child)
    state.safe_merge = screen
    threading.Thread(
        target=_safe_merge_begin_worker, args=(state, screen),
        daemon=True).start()
    return True


def _safe_merge_begin_worker(state: State, screen: SafeMergeScreen) -> None:
    """Phase 1: (optionally) stash a backup, start the merge, parse the
    conflicts. For a clean (conflict-free) merge, commit straight away and
    jump to the confirm screen."""
    tasks = state.tasks
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
                return

            t = tasks.add(f"  ↳ merge {screen.merge_ref}", parent=header)
            rc, _out, err = begin_safe_merge(path, screen.merge_ref)
            # rc != 0 is the EXPECTED conflict path; only a hard failure
            # with no merge actually started is a real error.
            if rc != 0 and merge_head_sha(path) is None:
                low = (err or "").lower()
                if "already up to date" in low:
                    tasks.update(t, "ok", "already up to date")
                    screen.error = "already up to date — nothing to merge"
                else:
                    tasks.update(t, "fail", first_line(err))
                    screen.error = f"merge could not start: {first_line(err)}"
                screen.phase = "error"
                return
            tasks.update(
                t, "ok",
                "conflicts to resolve" if rc != 0 else "clean merge")

        if screen.cancel_event.is_set():
            return

        # Describe both sides richly for the version labels.
        screen.ours = describe_merge_side(path, "HEAD", "ours")
        screen.theirs = describe_merge_side(
            path, "MERGE_HEAD", "theirs", branch_label=screen.merge_ref)

        screen.files = parse_safe_merge_conflicts(path)
        _safe_merge_build_decisions(screen)

        if not screen.files:
            # Conflict-free merge that's staged and ready — or an adopted
            # repo that's actually clean. If a merge is in progress, commit
            # it; otherwise there's nothing to do.
            if merge_head_sha(path) is not None:
                _safe_merge_do_commit(state, screen)
            else:
                screen.error = "no conflicts and no merge in progress"
                screen.phase = "error"
            return

        screen.phase = "resolve"
    except Exception as e:  # noqa: BLE001
        screen.error = f"safe-merge failed: {e}"
        screen.phase = "error"
        if header is not None:
            tasks.update(header, "fail", first_line(str(e)))


def _safe_merge_do_commit(state: State, screen: SafeMergeScreen) -> None:
    """Write every chosen resolution, stage it, and create the merge
    commit. On success, advance to the confirm screen; on a remaining
    (manual) conflict, drop back to resolve with a clear note."""
    tasks = state.tasks
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
            return

    remaining = remaining_conflict_paths(path)
    if remaining:
        manual = ", ".join(remaining[:3]) + (
            " …" if len(remaining) > 3 else "")
        screen.status_note = (
            f"{len(remaining)} file(s) need manual resolution outside "
            f"idlegit: {manual}")
        screen.phase = "resolve"
        return

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
        return
    sha, subject = head_short_info(path)
    tasks.update(t, "ok", sha)
    screen.commit_sha = sha
    screen.commit_subject = subject
    screen.confirm_focus = 0
    screen.phase = "confirm"


def kick_off_safe_merge_finalize(state: State,
                                 screen: SafeMergeScreen) -> None:
    """Called from the dialog when the user finishes picking sides. Spawns
    the commit worker (phase → committing → confirm)."""
    screen.phase = "committing"

    def worker() -> None:
        try:
            _safe_merge_do_commit(state, screen)
        except Exception as e:  # noqa: BLE001
            screen.status_note = f"commit failed: {e}"
            screen.phase = "resolve"

    threading.Thread(target=worker, daemon=True).start()


def kick_off_safe_merge_confirm(state: State,
                                screen: SafeMergeScreen) -> None:
    """Called from the confirm screen. Pushes the merge commit (if chosen),
    syncs sibling submodule checkouts + bumps parent pointers (when the
    target is a tracked submodule), and drops the backup stash (only when
    the user ticked the box). phase → confirming → done."""
    screen.phase = "confirming"

    def worker() -> None:
        tasks = state.tasks
        header = screen.header_task
        path = screen.target_path
        try:
            pushed = False
            if screen.confirm_push:
                pushed = _safe_merge_push(state, screen)
            if pushed and screen.is_tracked_submodule:
                _safe_merge_sync_submodule(state, screen)
            if screen.confirm_remove_stash and screen.backup_stash_name:
                t = tasks.add("  ↳ drop backup stash", parent=header)
                ok, detail = drop_named_stash(path, screen.backup_stash_name)
                tasks.update(t, "ok" if ok else "warn", detail)
            if header is not None:
                msg = screen.commit_sha
                if screen.confirm_push and not pushed:
                    state.tasks.update(header, "warn", "push failed")
                else:
                    state.tasks.update(header, "ok", msg)
        except Exception as e:  # noqa: BLE001
            if header is not None:
                state.tasks.update(header, "fail", first_line(str(e)))
        finally:
            _safe_merge_refresh_targets(state, screen)
            _safe_merge_release_locks(screen)
            screen.phase = "done"

    threading.Thread(target=worker, daemon=True).start()


def _safe_merge_push(state: State, screen: SafeMergeScreen) -> bool:
    """Push the merge commit. Plain `git push` (with `--set-upstream`
    fallback) — never forced. Returns True on success."""
    tasks = state.tasks
    header = screen.header_task
    path = screen.target_path
    t = tasks.add("  ↳ push", parent=header)
    rc_b, b_out, _ = git(path, ["branch", "--show-current"])
    cur_branch = b_out.strip() if rc_b == 0 else ""
    rc_u, u_out, _ = git(path, [
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    has_upstream = rc_u == 0 and bool(u_out.strip())
    if has_upstream:
        rc, _, err = git(path, ["push"])
    elif cur_branch and is_safe_ref_arg(cur_branch):
        rc, _, err = git(path, ["push", "--set-upstream", "origin", cur_branch])
    else:
        tasks.update(t, "fail", "no current branch to push")
        return False
    if rc != 0:
        tasks.update(t, "fail", first_line(err))
        return False
    tasks.update(t, "ok")
    return True


def _safe_merge_sync_submodule(state: State,
                               screen: SafeMergeScreen) -> None:
    """After a submodule-checkout merge lands and is pushed, fan the new
    commit out to sibling checkouts and bump the parent gitlink(s) — the
    same `sync_sibling` + `_cascade_propagate_to_parents` plumbing the
    commit pipeline uses."""
    tasks = state.tasks
    header = screen.header_task
    child = screen.target_child
    if child is None:
        return
    canonical = child.repo
    branch = child.branch or canonical.branch
    if not branch or branch == "(detached)" or not is_safe_ref_arg(branch):
        t = tasks.add("  ↳ sync siblings (skipped)", parent=header)
        tasks.update(
            t, "warn",
            "submodule on detached HEAD — sync siblings manually")
        return
    ref_label = state.task_repo_label(canonical)
    targets: List[Tuple[str, Path, Optional[Tuple[Repo, Path]]]] = []
    if not canonical.synthetic:
        targets.append((f"top-level {ref_label}", canonical.path, None))
    for other_parent, other_path in canonical.siblings:
        if other_path == child.nested_path:
            continue
        targets.append(
            (f"{ref_label} in {state.task_repo_label(other_parent)}",
             other_path, (other_parent, other_path)))
    for label, target_path, child_pair in targets:
        t = tasks.add(f"  ↳ sync {label}", parent=header)
        if child_pair is None:
            canonical.refreshing = True
        else:
            _set_child_ref_refreshing(child_pair[0], child_pair[1], True)
        try:
            ok, sync_msg = sync_sibling(target_path, branch)
        finally:
            if child_pair is None:
                canonical.refreshing = False
            else:
                _set_child_ref_refreshing(child_pair[0], child_pair[1], False)
        tasks.update(t, "ok" if ok else "fail", sync_msg)

    if state.auto_push_submodule_parent and canonical.siblings:
        try:
            _cascade_propagate_to_parents(state, [canonical])
        except Exception as e:  # noqa: BLE001
            t = tasks.add("  ↳ propagate to parents", parent=header)
            tasks.update(t, "fail", first_line(str(e)))
