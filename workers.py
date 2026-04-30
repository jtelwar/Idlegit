"""Background worker functions: every git action that takes more than a
moment runs in a daemon thread out of here, publishing progress to the
sidebar via state.tasks. None of these touch curses."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from models import (
    AlignHeadsPrompt, ChildRef, LFSCandidate, Repo, SmartSyncCheckout, State,
    Task,
)
from git_ops import (
    apply_lfs_tracking, discover_repos, dispatch_workflow, first_line,
    get_run_view, gh_available, git, link_siblings, list_branches,
    list_recent_runs, merge_remote_workflow_states, parse_github_slug,
    refresh_repo, signature_mtime, suggest_commit_message,
    suggest_commit_message_at, sync_sibling, sync_subtree,
    working_tree_signature,
)


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


def _poll_run(state: State, slug: str, run_id: int,
              repo: Repo, workflow_name: str,
              run_task: Task) -> None:
    """Poll one run's detailed view until it terminates, mirroring its
    progress to the sidebar. Each job materialises as its own indented
    sub-task, and the parent task's label refreshes with the current step
    of whichever job is most active. When the run finishes successfully,
    fire the repo's "then run after <workflow>" chain — if any.

    If a then-run is wired up, we add a `pending`-status placeholder task
    indented under the parent before polling so the user sees the chain
    queued from the start. On success the placeholder transforms (via
    `existing_task=` on `kick_off_manual_dispatch`) into the dispatch
    step rather than leaving a duplicate row behind; on fail/warn it
    short-circuits to a "skipped" warn."""
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

    # Snapshot the chained then-run target up front (don't pop yet —
    # we still need it visible in the dict so the row can render the
    # "waiting on …" message until the parent finishes).
    pending_then_run = repo.then_run_after_workflow.get(workflow_name, "")
    pending_task: Optional[Task] = None
    if pending_then_run:
        pending_task = state.tasks.add(
            f"  ↪ then run: {pending_then_run}", parent=run_task)
        state.tasks.update(pending_task, "pending",
                           f"waiting on {workflow_name}")
        state.tasks.set_meta(
            pending_task, repo=repo,
            pending_after_workflow=workflow_name,
            pending_target=pending_then_run)

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
            if rstatus == "ok":
                next_workflow = repo.then_run_after_workflow.pop(
                    workflow_name, "")
                if next_workflow:
                    branch = repo.branch or "main"
                    kick_off_manual_dispatch(
                        state, repo, next_workflow, branch,
                        existing_task=pending_task)
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
                               repo: Repo) -> Optional[Task]:
    """Add a sidebar task for a known GitHub Actions run and spawn a daemon
    that updates it (and its job sub-tasks) until completion. Returns the
    parent task on success, or None when the run dict is unusable."""
    workflow_name = run.get("workflowName") or run.get("name") or "workflow"
    repo_label = state.task_repo_label(repo)
    run_id = run.get("databaseId")
    if not isinstance(run_id, int):
        t = state.tasks.add(_format_run_label(repo_label, workflow_name))
        state.tasks.update(t, "fail", "no run id")
        return None
    t = state.tasks.add(_format_run_label(repo_label, workflow_name))
    threading.Thread(
        target=_poll_run,
        args=(state, slug, run_id, repo, workflow_name, t),
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
                kick_off_workflow_tracking(state, slug, run, repo)
            if remaining:
                time.sleep(poll_interval)
        for wf in remaining:
            t = state.tasks.add(f"↗ {repo_label}: {wf}")
            state.tasks.update(t, "warn", "no run triggered within 2 min")

    threading.Thread(target=watcher, daemon=True).start()


def kick_off_manual_dispatch(state: State, target_repo: Repo,
                             workflow_name: str, ref: str,
                             *, existing_task: Optional[Task] = None) -> None:
    """Fire `gh workflow run` for `workflow_name` against `ref`, then poll
    for the resulting run id and hand off to kick_off_workflow_tracking.
    workflow_dispatch runs don't carry a commit filter, so we identify the
    new run by tracking which run ids existed before dispatch and looking
    for a fresh one with a matching workflowName.

    `existing_task` lets a chained then-run reuse the placeholder row
    that `_poll_run` added in `pending` state, so the dispatch step
    transforms in place rather than spawning a fresh row alongside it."""
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
        ok, msg = dispatch_workflow(slug, workflow_name, ref)
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


def kick_off_action(state: State, action_id: str, *,
                    target_label: str, target_path: Path,
                    target_repo: Optional[Repo],
                    target_parent: Optional[Repo],
                    branch_arg: str = "",
                    reset_count: int = 0) -> None:
    """Spawn a daemon worker that runs one git action against `target_path`,
    publishes its progress to the sidebar, and quietly re-queries that one
    repo's state when it finishes. Returns immediately so the UI is free."""

    def worker() -> None:
        if action_id == "fetch":
            t = state.tasks.add(f"{target_label}: fetch")
            rc, _, err = git(target_path, ["fetch", "--all"])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        elif action_id == "pull":
            t = state.tasks.add(f"{target_label}: pull")
            rc, _, err = git(target_path, ["pull"])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        elif action_id == "push":
            t = state.tasks.add(f"{target_label}: push")
            rc_b, b_out, _ = git(target_path, ["branch", "--show-current"])
            cur_branch = b_out.strip() if rc_b == 0 else ""
            rc_u, u_out, _ = git(target_path, [
                "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
            has_upstream = rc_u == 0 and bool(u_out.strip())
            if has_upstream:
                rc, _, err = git(target_path, ["push"])
            elif cur_branch:
                rc, _, err = git(target_path, [
                    "push", "--set-upstream", "origin", cur_branch])
            else:
                rc, err = 1, "no current branch"
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
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
            rc, _, err = git(target_path, ["checkout", branch_arg])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        else:
            return  # unknown action — nothing to do

        _refresh_target_state(state, target_repo, target_parent)

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
        if result:
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
        if result:
            child.message = result
    finally:
        child.suggesting = False


def kick_off_suggest_for(state: State, target) -> None:
    """Run a single suggestion in a background thread; UI shows a spinner
    in the field meanwhile via target.suggesting."""
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
        if repo.is_dirty and not repo.message.strip() and not repo.suggesting:
            kick_off_suggest_for(state, repo)
    for parent in state.repos:
        for child in parent.children:
            if (child.kind == "submodule" and child.dirty
                    and not child.message.strip() and not child.suggesting):
                kick_off_suggest_for(state, child)


# ---------- Commit pipelines ----------------------------------------------


def commit_worker(state: State, repo: Repo, msg: str,
                  lfs_cands: List[LFSCandidate]) -> None:
    """Run the full stage / commit / push / sync pipeline for one repo,
    publishing each step into the sidebar. After a successful push, kicks
    off GitHub Actions tracking for any workflows the user opted in to on
    the review screen."""
    auto_stage = state.auto_stage
    auto_push = state.auto_push
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

    if auto_stage:
        t = tasks.add(f"{name}: git add -A")
        rc, _, err = git(repo.path, ["add", "-A"])
        if rc != 0:
            tasks.update(t, "fail", first_line(err))
            return
        tasks.update(t, "ok")

    rc, _, _ = git(repo.path, ["diff", "--cached", "--quiet"])
    if rc == 0:
        t = tasks.add(f"{name}: skipped")
        tasks.update(t, "warn", "nothing staged")
        return

    t = tasks.add(f"{name}: commit")
    rc, _, err = git(repo.path, ["commit", "-m", msg])
    if rc != 0:
        tasks.update(t, "fail", first_line(err))
        return
    tasks.update(t, "ok")

    if not auto_push:
        return

    push_task = tasks.add(f"{name}: push")
    if repo.upstream:
        rc, _, err = git(repo.path, ["push"])
    else:
        rc, _, err = git(repo.path, [
            "push", "--set-upstream", "origin", repo.branch])
    if rc != 0:
        tasks.update(push_task, "fail", first_line(err))
        return
    tasks.update(push_task, "ok")

    # Capture the freshly-pushed commit so the actions tracker can match
    # the run by sha. Pulled after push so we get the actual head, not a
    # cached snapshot from before the commit.
    rc_h, head_out, _ = git(repo.path, ["rev-parse", "HEAD"])
    pushed_sha = head_out.strip() if rc_h == 0 else ""
    tracked = [name for name, on in repo.track_workflow.items() if on]
    if tracked and pushed_sha:
        kick_off_post_push_run_tracking(
            state, repo, repo.branch, pushed_sha, tracked)
    repo.track_workflow.clear()

    # "Then run after push" — dispatch a manual workflow once the push
    # itself completes, regardless of any tracked workflow runs. Lives
    # alongside per-workflow then-runs (which fire when the tracked run
    # finishes successfully — handled inside _poll_run).
    after_push_target = repo.then_run_after_push
    repo.then_run_after_push = ""
    if after_push_target:
        kick_off_manual_dispatch(
            state, repo, after_push_target, repo.branch)

    for sib_repo, sib_path in repo.siblings:
        t = tasks.add(
            f"  ↳ sync {state.task_repo_label(sib_repo)}", parent=push_task)
        ok, sync_msg = sync_sibling(sib_path, repo.branch)
        tasks.update(t, "ok" if ok else "fail", sync_msg)


def commit_worker_for_child(state: State, parent: Repo, ref: ChildRef,
                            msg: str) -> None:
    """Run the stage / commit / push pipeline against `ref.nested_path` —
    the working tree of a nested submodule checkout inside `parent`.

    After a successful push, sync every other place this submodule is
    checked out (the canonical top-level repo + every other parent's nested
    copy) so they all advance to the new commit. Workflow tracking is
    keyed off the canonical repo's track_workflow map, same as a top-level
    push."""
    auto_stage = state.auto_stage
    auto_push = state.auto_push
    tasks = state.tasks
    name = (f"{state.task_repo_label(ref.repo)} "
            f"(in {state.task_repo_label(parent)})")

    rc, out, _ = git(ref.nested_path, ["branch", "--show-current"])
    nested_branch = out.strip() if rc == 0 else ""
    if not nested_branch:
        t = tasks.add(f"{name}: cannot commit")
        tasks.update(t, "fail",
                     "detached HEAD — checkout a branch in the nested "
                     "submodule first")
        return

    if auto_stage:
        t = tasks.add(f"{name}: git add -A")
        rc, _, err = git(ref.nested_path, ["add", "-A"])
        if rc != 0:
            tasks.update(t, "fail", first_line(err))
            return
        tasks.update(t, "ok")

    rc, _, _ = git(ref.nested_path, ["diff", "--cached", "--quiet"])
    if rc == 0:
        t = tasks.add(f"{name}: skipped")
        tasks.update(t, "warn", "nothing staged")
        return

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
        rc, _, err = git(ref.nested_path, ["push"])
    else:
        rc, _, err = git(ref.nested_path, [
            "push", "--set-upstream", "origin", nested_branch])
    if rc != 0:
        tasks.update(push_task, "fail", first_line(err))
        return
    tasks.update(push_task, "ok")

    rc_h, head_out, _ = git(ref.nested_path, ["rev-parse", "HEAD"])
    pushed_sha = head_out.strip() if rc_h == 0 else ""
    tracked = [n for n, on in ref.repo.track_workflow.items() if on]
    if tracked and pushed_sha:
        kick_off_post_push_run_tracking(
            state, ref.repo, nested_branch, pushed_sha, tracked)
    ref.repo.track_workflow.clear()

    # "Then run after push" for the canonical — same semantics as the
    # top-level commit_worker version.
    after_push_target = ref.repo.then_run_after_push
    ref.repo.then_run_after_push = ""
    if after_push_target:
        kick_off_manual_dispatch(
            state, ref.repo, after_push_target, nested_branch)

    # Build the post-push sync targets. For tracked canonicals we sync
    # the top-level checkout first; for synthetic canonicals there's no
    # workspace top-level, so we only fan out to the sibling parents.
    targets: List[Tuple[str, Path]] = []
    ref_label = state.task_repo_label(ref.repo)
    if not ref.repo.synthetic:
        targets.append((f"top-level {ref_label}", ref.repo.path))
    for other_parent, other_path in ref.repo.siblings:
        if other_path == ref.nested_path:
            continue
        targets.append(
            (f"{ref_label} in {state.task_repo_label(other_parent)}",
             other_path))

    for label, target_path in targets:
        t = tasks.add(f"  ↳ sync {label}", parent=push_task)
        ok, sync_msg = sync_sibling(target_path, nested_branch)
        tasks.update(t, "ok" if ok else "fail", sync_msg)


def kick_off_workers(state: State, candidates: List[LFSCandidate]) -> None:
    """Launch one worker thread per repo / nested-child with a queued
    message and a supervisor thread that silently re-fetches repo state
    once everything finishes."""
    repo_plans: List[Tuple[Repo, str, List[LFSCandidate]]] = []
    for repo in state.repos:
        msg = repo.message.strip()
        if not msg:
            continue
        repo_cands = [c for c in candidates if c.repo is repo]
        repo_plans.append((repo, msg, repo_cands))
        repo.message = ""

    child_plans: List[Tuple[Repo, ChildRef, str]] = []
    for parent in state.repos:
        for ref in parent.children:
            if ref.kind != "submodule":
                continue
            msg = ref.message.strip()
            if not msg:
                continue
            child_plans.append((parent, ref, msg))
            ref.message = ""

    if not repo_plans and not child_plans:
        return

    workers: List[threading.Thread] = []
    for repo, msg, repo_cands in repo_plans:
        w = threading.Thread(
            target=commit_worker,
            args=(state, repo, msg, repo_cands),
            daemon=True,
        )
        w.start()
        workers.append(w)

    for parent, ref, msg in child_plans:
        w = threading.Thread(
            target=commit_worker_for_child,
            args=(state, parent, ref, msg),
            daemon=True,
        )
        w.start()
        workers.append(w)

    def supervisor() -> None:
        for w in workers:
            w.join()
        for r in state.repos:
            refresh_repo(r)
        link_siblings(state.repos, state.subtrees)

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
# In either mode the only git verbs used are fetch / merge --ff-only /
# checkout — git itself refuses on conflict, so we never overwrite
# uncommitted work or unique commits.


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
    rc, _, err = git(winner.path, ["add", "-A"])
    if rc != 0:
        state.tasks.update(t, "fail", first_line(err))
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
    rc, _, err = git(winner.path, ["push"])
    if rc != 0:
        rc, _, err = git(
            winner.path, ["push", "--set-upstream", "origin", branch])
    if rc != 0:
        state.tasks.update(t, "fail", first_line(err))
        return False
    state.tasks.update(t, "ok")
    return True


def _switch_to_branch(state: State, c: SmartSyncCheckout,
                      branch: str, name: str) -> bool:
    """Move a checkout onto a named branch. Git refuses if the WT has
    changes that would conflict with the new branch tip — non-destructive."""
    t = state.tasks.add(f"  ↳ align {name}: switch {c.label} → {branch}")
    rc, _, err = git(c.path, ["checkout", branch])
    if rc != 0:
        state.tasks.update(t, "fail", first_line(err))
        return False
    state.tasks.update(t, "ok")
    return True


def _align_loser_ff(state: State, c: SmartSyncCheckout,
                    winner_branch: str, name: str) -> bool:
    """Bring a same-branch loser up to the winner's commit via
    `fetch + merge --ff-only`. Refuses (warn-skip) when the WT has
    changes that would conflict with the new commit, OR when the
    loser has local commits not in the winner. Both cases preserve
    the loser's state — the user resolves manually."""
    t = state.tasks.add(f"  ↳ align {name}: ff {c.label}")
    rc, _, err = git(c.path, ["fetch", "origin", winner_branch])
    if rc != 0:
        state.tasks.update(t, "fail", first_line(err))
        return False
    rc, _, err = git(
        c.path, ["merge", "--ff-only", f"origin/{winner_branch}"])
    if rc != 0:
        state.tasks.update(t, "warn", first_line(err))
        return False
    state.tasks.update(t, "ok")
    return True


def _align_detached_loser(state: State, c: SmartSyncCheckout,
                          winner_branch: str, name: str) -> bool:
    """Bring a detached-HEAD loser onto the winner's published commit
    via `fetch + checkout origin/<branch>`. Git refuses if a WT change
    would clobber a tracked file — surfaces as a warn task."""
    t = state.tasks.add(f"  ↳ align {name}: switch+sync {c.label}")
    rc, _, err = git(c.path, ["fetch", "origin", winner_branch])
    if rc != 0:
        state.tasks.update(t, "fail", first_line(err))
        return False
    rc, _, err = git(c.path, ["checkout", f"origin/{winner_branch}"])
    if rc != 0:
        state.tasks.update(t, "warn", first_line(err))
        return False
    state.tasks.update(t, "ok")
    return True


def _open_align_heads_prompt_and_wait(state: State, canonical_name: str,
                                      winner: SmartSyncCheckout) -> str:
    """Pop the AlignHeadsPrompt modal and block until the user resolves
    it. Returns the chosen branch (or empty string on cancel). Called
    from the smart-sync worker thread; the modal handler in the main
    loop signals `result_event`."""
    branches, _ = list_branches(winner.path)
    prompt = AlignHeadsPrompt(
        canonical_label=canonical_name,
        winner_label=winner.label,
        winner_sha=winner.head,
        branches=branches,
        selected=0,
    )
    state.align_heads_prompt = prompt
    prompt.result_event.wait()
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

    All loser-alignment ops use `merge --ff-only` (same-branch) or
    `checkout origin/<branch>` (detached) — git itself refuses on
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

    # Stage + commit dirty winner (auto-stage off → warn-skip).
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

    # Detached winner: pick a branch via the modal (align_heads on) or
    # warn-skip the canonical entirely (align_heads off).
    winner_branch = winner.branch
    if winner_branch == "(detached)":
        if not state.align_heads:
            t = state.tasks.add(f"  ↳ align {name}")
            state.tasks.update(
                t, "warn",
                f"{winner.label} detached — turn on align-heads to pick a branch")
            return 0, 1
        chosen = _open_align_heads_prompt_and_wait(state, name, winner)
        if not chosen:
            t = state.tasks.add(f"  ↳ align {name}")
            state.tasks.update(t, "warn", "user cancelled detached-branch pick")
            return 0, 1
        if not _switch_to_branch(state, winner, chosen, name):
            return 0, 1
        winner_branch = chosen

    # Push winner if it has unpushed commits (real or just-committed).
    if winner.ahead > 0:
        if not _push_winner(state, winner, winner_branch, name):
            return 0, 1

    # Align losers.
    ok = 1 if winner.ahead > 0 else 0
    fail = 0
    for c in checkouts:
        if c is winner:
            continue
        if c.head == winner.head and not c.dirty:
            # Already in sync.
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

    def worker() -> None:
        ok_total = 0
        fail_total = 0

        for canonical in canonicals_with_siblings:
            try:
                ok, fail = _align_canonical(state, canonical)
            except Exception as e:
                t = state.tasks.add(
                    f"  ↳ align {state.task_repo_label(canonical)}")
                state.tasks.update(t, "fail", first_line(str(e)))
                ok, fail = 0, 1
            ok_total += ok
            fail_total += fail

        for parent, ref in subtree_items:
            t = state.tasks.add(
                f"  ⊕ {state.task_repo_label(ref.repo)} "
                f"in {state.task_repo_label(parent)}")
            try:
                prefix = str(ref.nested_path.relative_to(parent.path))
            except ValueError:
                prefix = ""
            ok, msg = sync_subtree(
                parent.path, prefix,
                ref.repo.remote_url_raw or "", ref.repo.branch)
            state.tasks.update(t, "ok" if ok else "fail", msg)
            if ok:
                ok_total += 1
            else:
                fail_total += 1

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

        # Refresh the model so main-screen dots reflect the new on-disk
        # state without needing a follow-up Ctrl+R.
        for r in state.repos:
            refresh_repo(r)
        link_siblings(state.repos, state.subtrees)

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

    if state.repos:
        if state.repos[0].rel == ".":
            workspace = state.repos[0].path
        else:
            workspace = state.repos[0].path.parent
    else:
        # No repos to anchor — release the gate and bail.
        with _inline_refresh_lock:
            _inline_refresh_in_flight = False
        return

    # Flip every row's refreshing flag SYNCHRONOUSLY before we spawn the
    # worker. The main loop's `anim_running` check fires as soon as any
    # repo has `refreshing=True`; flipping it now means the very next
    # loop iteration drops the getch timeout from 1s back to 100ms, so
    # the row spinners light up immediately and the user gets visible
    # feedback that their Ctrl+R registered. Without this, the loop
    # may block for up to a second on its idle-timeout getch before
    # noticing activity, which feels like a missed keystroke and tempts
    # a second press (which is what triggered the duplication race).
    for r in state.repos:
        r.refreshing = True

    def worker() -> None:
        global _inline_refresh_in_flight
        try:
            try:
                fresh = discover_repos(workspace)
            except Exception:
                fresh = []
            fresh_by_rel = {r.rel: r for r in fresh}
            kept_rels = {r.rel for r in state.repos if r.rel in fresh_by_rel}

            state.repos[:] = [r for r in state.repos if r.rel in kept_rels]

            existing_rels = {r.rel for r in state.repos}
            for r in fresh:
                if r.rel not in existing_rels:
                    state.repos.append(r)
            state.repos.sort(
                key=lambda r: (r.rel != ".", r.rel.lower() if r.rel != "." else ""))

            for r in state.repos:
                r.refreshing = True

            def refresh_one(r: Repo) -> None:
                try:
                    refresh_repo_with_remote_state(r)
                finally:
                    r.refreshing = False

            if state.repos:
                with ThreadPoolExecutor(max_workers=len(state.repos)) as ex:
                    list(ex.map(refresh_one, state.repos))
            link_siblings(state.repos, state.subtrees)

            state.selected = max(
                0, min(state.selected, max(0, state.total_rows - 1)))
            state.body_scroll = max(
                0, min(state.body_scroll, max(0, state.total_rows - 1)))
        finally:
            with _inline_refresh_lock:
                _inline_refresh_in_flight = False

    threading.Thread(target=worker, daemon=True).start()
