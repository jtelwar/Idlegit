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
    AlignHeadsPrompt, ChildRef, DetachedRecoveryPrompt, LFSCandidate,
    Repo, ReviewBlock, SmartSyncCheckout, State,
    Task,
)
from git_ops import (
    apply_lfs_tracking, discover_repos, dispatch_workflow, first_line,
    get_run_view, gh_available, git, link_siblings, list_branches,
    list_recent_runs, merge_remote_workflow_states, parse_github_slug,
    refresh_repo, safe_stage_all, signature_mtime, suggest_commit_message,
    suggest_commit_message_at, suggest_commit_message_for_paths,
    sync_sibling, sync_subtree, is_safe_ref_arg,
    working_tree_signature,
    MAX_PARALLEL_GIT_JOBS,
)

PROMPT_WAIT_SECONDS = 15 * 60
MIN_ACTION_REFRESH_SECONDS = 0.35
_detached_recovery_prompt_lock = threading.Lock()
_align_heads_prompt_lock = threading.Lock()


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
                    if not tag_name or tag_name.startswith("-"):
                        state.tasks.update(
                            tag_task, "fail",
                            "tag name empty or unsafe")
                    elif not sha:
                        state.tasks.update(
                            tag_task, "fail", "no sha to tag")
                    else:
                        rc_t, _, err_t = git(
                            repo.path, ["tag", tag_name, sha])
                        state.tasks.update(
                            tag_task,
                            "ok" if rc_t == 0 else "fail",
                            "" if rc_t == 0 else first_line(err_t))
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
        "branch_from_head", "create_branch", "ff_merge",
        "rename_branch", "set_upstream",
        "stash_create", "stash_apply",
    }
    should_refresh = action_id in known_actions
    target_child = _find_child_at(target_parent, target_path)
    # Flip refreshing SYNCHRONOUSLY before returning so the very next
    # redraw shows the spinner — the daemon worker may not run for a
    # tick, and even a 100ms gap reads as "did anything happen?".
    if target_repo is not None:
        target_repo.refreshing = True
    if target_child is not None:
        target_child.refreshing = True

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
            t = state.tasks.add(f"{target_label}: pull --ff-only")
            rc, _, err = git(target_path, ["pull", "--ff-only"])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
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
                _, head_before, _ = git(target_path, ["rev-parse", "HEAD"])
                rc_pull, _, pull_err = git(target_path, ["pull", "--ff-only"])
                _, head_after, _ = git(target_path, ["rev-parse", "HEAD"])
                if rc_pull != 0:
                    t_pull = state.tasks.add(f"{target_label}: pull --ff-only")
                    state.tasks.update(t_pull, "fail",
                                       first_line(pull_err) or "cannot fast-forward")
                    state.tasks.update(t, "fail", "skipped: cannot fast-forward")
                else:
                    if head_before.strip() != head_after.strip():
                        t_pull = state.tasks.add(f"{target_label}: pull --ff-only")
                        state.tasks.update(t_pull, "ok")
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
        # Always release the refreshing flags — even on early-return
        # / exception paths — so a row never gets stuck spinning.
        if target_repo is not None:
            target_repo.refreshing = False
        if target_child is not None:
            target_child.refreshing = False

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
    from git_ops import (
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
    """Create a lightweight tag pointing at `sha`. Refuses unsafe
    name / sha (defence-in-depth). Creating a tag is cardinal-rule
    safe: it only writes a new ref. Pushing the tag is a separate
    operation we don't run automatically."""
    target_child = _find_child_at(target_parent, target_path)
    if target_repo is not None:
        target_repo.refreshing = True
    if target_child is not None:
        target_child.refreshing = True

    def worker() -> None:
        try:
            t = state.tasks.add(f"{target_label}: tag {name}")
            if not is_safe_ref_arg(name):
                state.tasks.update(t, "fail", "unsafe tag name")
                return
            if not sha or sha.startswith("-"):
                state.tasks.update(t, "fail", "unsafe sha")
                return
            rc, _, err = git(target_path, ["tag", name, sha])
            state.tasks.update(
                t, "ok" if rc == 0 else "fail",
                "" if rc == 0 else first_line(err))
        finally:
            if target_repo is not None:
                target_repo.refreshing = False
            if target_child is not None:
                target_child.refreshing = False

    threading.Thread(target=worker, daemon=True).start()


def kick_off_clone(state: State, url: str, dest: Path, branch: str,
                   recurse_submodules: bool,
                   on_done=None) -> None:
    """Run `git clone` in a daemon thread, publishing progress to the
    sidebar. `on_done` is called with `(ok, message)` once the clone
    settles, on the worker thread — caller wires it up to refresh the
    workspace's repo list and close the modal."""
    from git_ops import clone_repo
    label = dest.name or "clone"
    t = state.tasks.add(f"{label}: clone")

    def worker() -> None:
        ok, msg = clone_repo(url, dest, branch=branch,
                             recurse_submodules=recurse_submodules)
        state.tasks.update(t, "ok" if ok else "fail", msg)
        if on_done is not None:
            try:
                on_done(ok, msg)
            except Exception:
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
    from git_ops import _iter_porcelain_z_entries  # local: avoid circ
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
                  amend: bool = False) -> None:
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
    trusts the caller and doesn't re-validate."""
    try:
        _commit_worker_inner(state, repo, msg, lfs_cands, staged_paths,
                             amend)
    except Exception as e:
        name = state.task_repo_label(repo)
        t = state.tasks.add(f"{name}: failed")
        state.tasks.update(t, "fail", first_line(str(e)))


def _commit_worker_inner(state: State, repo: Repo, msg: str,
                         lfs_cands: List[LFSCandidate],
                         staged_paths: Optional["dict[str, bool]"] = None,
                         amend: bool = False
                         ) -> None:
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

    # Fast-forward before staging — once we have a local commit we'll be
    # diverged and --ff-only will refuse. Pull also fetches, so we catch
    # commits that arrived after the last refresh. Only surface a task
    # when HEAD actually moved or the pull itself fails.
    if repo.upstream:
        _, head_before, _ = git(repo.path, ["rev-parse", "HEAD"])
        rc_pull, _, pull_err = git(repo.path, ["pull", "--ff-only"])
        _, head_after, _ = git(repo.path, ["rev-parse", "HEAD"])
        if rc_pull != 0:
            t_pull = tasks.add(f"{name}: pull --ff-only")
            tasks.update(t_pull, "fail",
                         first_line(pull_err) or "cannot fast-forward")
            return
        if head_before.strip() != head_after.strip():
            t_pull = tasks.add(f"{name}: pull --ff-only")
            tasks.update(t_pull, "ok")

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
        rc, _, err = git(repo.path, ["push"])
    else:
        rc_b, b_out, _ = git(repo.path, ["branch", "--show-current"])
        cur_branch = b_out.strip() if rc_b == 0 else ""
        if cur_branch:
            if not is_safe_ref_arg(cur_branch):
                tasks.update(push_task, "fail", "unsafe current branch name")
                return
            rc, _, err = git(repo.path, [
                "push", "--set-upstream", "origin", cur_branch])
        else:
            rc, err = 1, "no current branch"
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

    # "Then run after push" — fired once the push itself completes,
    # regardless of any tracked workflow runs. Two shapes:
    #   * a workflow name → dispatch the manual workflow
    #   * the "__add_tag__" sentinel → create a lightweight tag at
    #     the just-pushed sha. Per-action parameter buffers live in
    #     `then_run_params_after_push` (today only "tag", but the
    #     same dict will hold workflow_dispatch inputs in the
    #     future). We pop the slot's params unconditionally so a
    #     follow-up push doesn't double-fire stale values.
    after_push_target = repo.then_run_after_push
    after_push_params = dict(repo.then_run_params_after_push)
    repo.then_run_after_push = ""
    repo.then_run_params_after_push.clear()
    if after_push_target == "__add_tag__":
        tag_name = after_push_params.get("tag", "").strip()
        tag_label = state.task_repo_label(repo)
        t_tag = tasks.add(f"{tag_label}: tag {tag_name or '(empty)'}")
        if not tag_name or tag_name.startswith("-"):
            tasks.update(t_tag, "fail", "tag name empty or unsafe")
        elif not pushed_sha:
            tasks.update(t_tag, "fail", "no pushed sha")
        else:
            rc_tag, _, err_tag = git(repo.path, [
                "tag", tag_name, pushed_sha,
            ])
            tasks.update(
                t_tag, "ok" if rc_tag == 0 else "fail",
                "" if rc_tag == 0 else first_line(err_tag))
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
        ok, sync_msg = sync_sibling(sib_path, repo.branch)
        tasks.update(t, "ok" if ok else "fail", sync_msg)


def commit_worker_for_child(state: State, parent: Repo, ref: ChildRef,
                            msg: str,
                            staged_paths: Optional["dict[str, bool]"] = None,
                            amend: bool = False
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
    the latest commit hasn't been pushed)."""
    try:
        _commit_worker_for_child_inner(state, parent, ref, msg,
                                       staged_paths, amend)
    except Exception as e:
        name = (f"{state.task_repo_label(ref.repo)} "
                f"(in {state.task_repo_label(parent)})")
        t = state.tasks.add(f"{name}: failed")
        state.tasks.update(t, "fail", first_line(str(e)))


def _commit_worker_for_child_inner(state: State, parent: Repo,
                                   ref: ChildRef, msg: str,
                                   staged_paths: Optional["dict[str, bool]"]
                                   = None,
                                   amend: bool = False) -> None:
    auto_stage = state.auto_stage
    auto_push = state.auto_push
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
        rc, _, err = git(ref.nested_path, ["push"])
    else:
        if not is_safe_ref_arg(nested_branch):
            tasks.update(push_task, "fail", "unsafe current branch name")
            return
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

    # "Then run after push" for the canonical — same semantics as
    # the top-level commit_worker version, including the
    # "__add_tag__" sentinel for creating a lightweight tag at the
    # pushed sha. Reads the action's per-parameter buffer dict,
    # popping it so a follow-up push doesn't replay stale params.
    after_push_target = ref.repo.then_run_after_push
    after_push_params = dict(ref.repo.then_run_params_after_push)
    ref.repo.then_run_after_push = ""
    ref.repo.then_run_params_after_push.clear()
    if after_push_target == "__add_tag__":
        tag_name = after_push_params.get("tag", "").strip()
        tag_label = (f"{state.task_repo_label(ref.repo)} "
                     f"(in {state.task_repo_label(parent)})")
        t_tag = tasks.add(f"{tag_label}: tag {tag_name or '(empty)'}")
        if not tag_name or tag_name.startswith("-"):
            tasks.update(t_tag, "fail", "tag name empty or unsafe")
        elif not pushed_sha:
            tasks.update(t_tag, "fail", "no pushed sha")
        else:
            rc_tag, _, err_tag = git(ref.nested_path, [
                "tag", tag_name, pushed_sha,
            ])
            tasks.update(
                t_tag, "ok" if rc_tag == 0 else "fail",
                "" if rc_tag == 0 else first_line(err_tag))
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

    repo_plans: List[Tuple[Repo, str, List[LFSCandidate],
                           "dict[str, bool]", bool]] = []
    for repo in state.repos:
        msg = repo.message.strip()
        if not msg:
            continue
        repo.message = ""
        if repo.refreshing:  # another action owns this repo — skip
            continue
        block = repo_blocks.get(id(repo))
        repo_cands = list(block.lfs_candidates) if block else []
        staged = dict(block.staged_paths) if block else {}
        amend = bool(block.amend) if block else False
        repo_plans.append((repo, msg, repo_cands, staged, amend))
        repo.refreshing = True  # lock synchronously before spawning

    child_plans: List[Tuple[Repo, ChildRef, str,
                            "dict[str, bool]", bool]] = []
    for parent in state.repos:
        for ref in parent.children:
            if ref.kind != "submodule":
                continue
            msg = ref.message.strip()
            if not msg:
                continue
            ref.message = ""
            if ref.refreshing:  # another action owns this child — skip
                continue
            block = child_blocks.get(id(ref))
            staged = dict(block.staged_paths) if block else {}
            amend = bool(block.amend) if block else False
            child_plans.append((parent, ref, msg, staged, amend))
            ref.refreshing = True  # lock synchronously before spawning

    if not repo_plans and not child_plans:
        return

    locked_repos = {id(repo) for repo, _, _, _, _ in repo_plans}
    locked_refs = {id(ref) for _, ref, _, _, _ in child_plans}

    workers: List[threading.Thread] = []
    for repo, msg, repo_cands, staged, amend in repo_plans:
        w = threading.Thread(
            target=commit_worker,
            args=(state, repo, msg, repo_cands, staged, amend),
            daemon=True,
        )
        w.start()
        workers.append(w)

    for parent, ref, msg, staged, amend in child_plans:
        w = threading.Thread(
            target=commit_worker_for_child,
            args=(state, parent, ref, msg, staged, amend),
            daemon=True,
        )
        w.start()
        workers.append(w)

    def supervisor() -> None:
        for w in workers:
            w.join()
        for r in state.repos:
            refresh_repo(r)
            if id(r) in locked_repos:
                r.refreshing = False
        link_siblings(state.repos, state.subtrees)
        for parent in state.repos:
            for ref in parent.children:
                if id(ref) in locked_refs:
                    ref.refreshing = False

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

    stash_msg = "idlegit smart-sync: redundant dirty changes"
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

    stash_msg = "idlegit smart-sync: redundant dirty changes"
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
        "-m", "idlegit smart-sync: align detached winner",
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
    """Bring a same-branch loser up to the winner's commit via
    `fetch + merge --ff-only`. Refuses (warn-skip) when the WT has
    changes that would conflict with the new commit, OR when the
    loser has local commits not in the winner. Both cases preserve
    the loser's state — the user resolves manually."""
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

    # Lock synchronously so the very next redraw shows spinners.
    for canonical in canonicals_with_siblings:
        canonical.refreshing = True
    for _parent, ref in subtree_items:
        ref.refreshing = True

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
            finally:
                refresh_repo(canonical)
                canonical.refreshing = False
            ok_total += ok
            fail_total += fail

        for parent, ref in subtree_items:
            t = state.tasks.add(
                f"  ⊕ {state.task_repo_label(ref.repo)} "
                f"in {state.task_repo_label(parent)}")
            try:
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
            finally:
                refresh_repo(parent)
                ref.refreshing = False

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

        # Final full refresh + sibling-link rebuild to catch any repos
        # not covered by the per-item refreshes above.
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

    # Prefer the active workspace's folder list when available — it
    # supports multi-folder workspaces (which the legacy
    # `state.repos[0].path.parent` anchor couldn't, silently dropping
    # repos discovered from any folder other than the first one).
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
            kept_by_path = {r.path: r for r in state.repos
                            if r.path in fresh_by_path}
            next_repos: List[Repo] = []
            for r in fresh:
                next_repos.append(kept_by_path.get(r.path, r))
            next_repos.sort(
                key=lambda r: (r.rel != ".", r.rel.lower() if r.rel != "." else ""))

            for r in next_repos:
                r.refreshing = True

            def refresh_one(r: Repo) -> None:
                try:
                    refresh_repo_with_remote_state(r)
                finally:
                    r.refreshing = False

            if next_repos:
                max_workers = min(len(next_repos), MAX_PARALLEL_GIT_JOBS)
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    list(ex.map(refresh_one, next_repos))
            link_siblings(next_repos, state.subtrees)

            state.repos = next_repos
            ws = state.active_workspace
            if ws is not None:
                ws.cached_repos = next_repos

            # `selected = -1` is the title-row workspace selector — keep
            # it as-is rather than clamping back into the body. Other
            # values clamp into [0, total_rows-1] so a removed repo
            # doesn't leave the cursor pointing past the new end.
            if state.selected != -1:
                state.selected = max(
                    0, min(state.selected, max(0, state.total_rows - 1)))
            state.body_scroll = max(
                0, min(state.body_scroll, max(0, state.total_rows - 1)))
        finally:
            with _inline_refresh_lock:
                _inline_refresh_in_flight = False

    threading.Thread(target=worker, daemon=True).start()


def switch_workspace(state: State, new_index: int) -> None:
    """Switch the active workspace. The cheap path — and the one taken
    every time after the first — is to swap `state.repos` to the new
    workspace's `cached_repos`, populated at startup. No re-discovery,
    no async refresh: each ←/→ keystroke is instant and the previously-
    fetched per-repo state (branch, head, dirty flags) is preserved.

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
        # Cache hit — instant swap. The cached list is the same Python
        # object subsequent kick_off_inline_refresh runs will mutate
        # in place, so a later Ctrl+R updates both `state.repos` and
        # the workspace's cache simultaneously without copying.
        state.repos = ws.cached_repos
        kick_refresh = False
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
    from config import apply_workspace_overrides
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
    from config import save_workspaces
    try:
        save_workspaces(state.workspaces, state.active_workspace_index)
    except OSError:
        pass

    if kick_refresh:
        kick_off_inline_refresh(state)
