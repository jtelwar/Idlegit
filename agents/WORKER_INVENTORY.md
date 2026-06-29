# Worker Inventory

Date: 2026-06-20

Updated: 2026-06-21

Purpose: classify the current async entry points so the foundation refactor can
migrate them onto jobs, leases, refresh monitoring, and UI events without
mixing unrelated responsibilities.

## Core Worker Entry Points

| Entry point | Class | Current ownership | Migration target |
| --- | --- | --- | --- |
| `kick_off_workflow_tracking` | Remote monitor | Adds task rows and starts a poll thread; no local repo mutation lease. | Job kind `workflow-poll`; task rows become presentation only. |
| `kick_off_post_push_run_tracking` | Remote monitor | Starts a watcher thread after push; may create workflow tracking tasks. | Job kind `workflow-poll-discovery`; no refresh suppression. |
| `kick_off_manual_dispatch` | Remote mutation/API | Dispatches GitHub workflow and polls for run creation; no local git mutation. | Job kind `workflow-dispatch`; cancellable remote/API job. |
| `kick_off_action` | Local git mutation | Uses `WorkerClaim`, task metadata, and direct post-action refresh/relink. | Job runner with repo/child leases and refresh reconciler request. |
| `kick_off_remote_changes` | Local git config mutation | Uses `WorkerClaim`; applies remote edits and refreshes. | Job runner with repo lease and refresh reconciler request. |
| `kick_off_load_commit_view` | Read-only modal load | Starts background loaders for commit view details. | UI/modal load job or lightweight UI event task; no repo mutation lease. |
| `kick_off_add_tag` | Local git mutation | Uses action-like claim semantics for tag write. | Job runner with repo/child lease where applicable. |
| `kick_off_clone` | Filesystem/git mutation | Clones into a destination and refreshes workspace afterwards. | Job runner with workspace-level lease or destination-path lease. |
| `kick_off_suggest_for` | Read-only/AI helper | Marks suggesting state and updates message field. | Read-only helper job with UI event updates; no refresh suppression. |
| `kick_off_bulk_suggest` | Read-only/AI helper | Fans out `kick_off_suggest_for`. | Batch helper job or simple fan-out through suggestion worker pool. |
| `kick_off_review_suggest` | Read-only/AI helper | Suggests a review block message. | Read-only helper job with cancellation. |
| `kick_off_workers` | Local git mutation batch | Commit/push worker supervisor with repo/child claims and task metadata. | Job runner batch with per-block leases, structured results, refresh reconciler. |
| `kick_off_sync_siblings` | Local git mutation batch | Custom smart-sync orchestrator, sentinel tasks, direct refresh/relink cleanup. | Smart-sync planner/runner/reconciler on top of job/lease foundation. |
| `kick_off_inline_refresh` | State monitor | Global per-workspace gate, direct refresh locks, direct relink. | Refresh queue with bounded executor, dedupe, generations, stale-result discard. |
| `kick_off_pull_all` | Local git mutation batch | Acquires repo locks and starts pull worker pool. | Job runner batch with repo leases and refresh reconciler. |
| `kick_off_check_for_updates` | Remote monitor | Menu-local update check thread. | UI/menu remote monitor job; no repo mutation lease. |
| `kick_off_safe_merge` | Modal local git mutation | Opens safe-merge mode, owns repo/child locks and task metadata. | Scoped modal input mode plus job runner phases and leases. |
| `kick_off_safe_merge_finalize` | Modal local git mutation | Starts commit phase thread from safe-merge modal. | Safe-merge job substep through runner. |
| `kick_off_safe_merge_confirm` | Modal local git mutation | Pushes, syncs siblings, propagates parents, drops stash, refreshes. | Safe-merge job substeps plus shared propagation service and reconciler. |

## UI/Modal Worker Entry Points

| Entry point | Class | Current ownership | Migration target |
| --- | --- | --- | --- |
| `ui.review.kick_off_review_files_load` | Read-only modal load | One thread per review block, writes block file lists. | UI load jobs with cancellation and event wakeups. |
| `ui.modals.action_menu._kick_off_state_load` | Read-only modal load | Loads workflow state for action menu. | UI load job; remote workflow hydration cache. |
| `ui.modals.action_menu._kick_off_tree_load` | Read-only modal load | Loads tree/file data for action menu. | UI load job with cancellation. |
| `ui.modals.action_menu._kick_off_initial_commits` | Read-only modal load | Loads first commit page. | UI load job with pagination events. |
| `ui.modals.action_menu._kick_off_commits_page` | Read-only modal load | Loads subsequent commit page. | UI load job with pagination events. |
| `ui.modals.remote_branch_picker._kick_off_refs_load` | Read-only modal load | Loads remote branch refs. | UI load job. |
| `ui.modals.workspace_creator._kick_off_check` | Read-only path discovery | Checks candidate workspace path. | UI load job; bounded discovery pool. |
| `ui.modals.workspace_menu._kick_off_path_check` | Read-only path discovery | Checks workspace folder path drafts. | UI load job; bounded discovery pool. |
| `ui.modals.ssh_keygen._kick_off_generate` | Filesystem mutation | Generates SSH key material from modal. | Job runner with destination-path lease and terminal task/event outcome. |

## Current Migration State

Most async entry points now submit a `JobSpec` through `submit_job` and have
focused tests for thread-start failure, exception terminalization, or task/job
status mirroring. The old table above is intentionally retained as the original
inventory; this section is the current audit map.

### Routed Through Job Registry

- Remote/API monitor jobs: `kick_off_workflow_tracking`,
  `kick_off_post_push_run_tracking`, `kick_off_manual_dispatch`,
  `kick_off_check_for_updates`.
- Read-only UI/modal loaders: startup workspace loading, review file loading,
  action-menu state/tree/commit loading, commit-view loading, diff-viewer tab
  loading, remote-branch picker loading, task-log viewer loading, task-detail
  browser/cancel helpers, workspace creator/menu path checks, task-log opener,
  suggestion workers.
- Local mutation jobs: remote edit, add tag, clone, generic action, commit
  batch supervision, pull-all, safe-merge begin/finalize/confirm, smart-sync,
  inline refresh supervision, SSH key generation, ssh-add default keys.
- File-watch drains: pending fs-watch drains submit read-only jobs and requeue
  paths on job thread-start failure; debounce thread-start failure queues the
  repo for later drain instead of bubbling out of the watchdog callback.

### Remaining Raw Thread Sites

- `core/jobs.py`: canonical default thread factory for `submit_job`.
- `thread_factory` closures in workers/modals: thin adapters used by
  `submit_job` so tests can inject start failures.
- `kick_off_workers`: per-repo/child commit subworkers now fan out through
  `core.thread_group.ThreadGroup` under a single `commit-batch` job supervisor.
  A thin thread factory remains so tests can inject start failures. Current
  tests cover first-worker and later-worker start failure, supervisor start
  failure, and idempotent lease cleanup.
- `kick_off_sync_siblings`: final cleanup now runs inside the smart-sync job
  thread through the shared bounded reconciler with one refresh worker, so
  there is no abandonable cleanup helper thread. Current tests cover
  navigation while cleanup is blocked, absence of a secondary cleanup thread,
  no-op cleanup skipping, and sentinel release.
- `core/fs_watcher.RepoWatcher`: owns a single debounce thread per watched
  repo. Thread-start failure is converted into a pending drain.
- Bounded pools remain in startup loading, inline refresh, fs-watch drain, and
  pull-all. They are scoped inside job workers; their completion is represented
  by the owning job.

### Remaining Foundation Gaps

- Refresh/relink is still partly embedded in worker `finally` blocks. Shared
  bounded helpers now cover startup loading, fs-watch drains, and
  refresh-plus-relink cleanup for inline refresh, commit-batch, smart-sync,
  pull-all, action, safe-merge, startup/fs-watch relinks, and fresh
  workspace-switch relinks. Inline refresh final and incremental relinks, plus
  fs-watch drain relinks, now discard stale relinks when the workspace is no
  longer active. Inline refresh in-flight/pending/stale-target coalescing now
  lives in `core.refresh_queue.InlineRefreshQueue`, and inline refresh
  workspace identity/result publication, fs-watch drain relink guards, and
  live single-target action refresh relinks now use
  `core.refresh_scope.WorkspaceRefreshScope`. Commit-batch, pull-all,
  safe-merge, smart-sync, and startup loading relinks are intentionally
  snapshot-based; remaining work is final audit/verification rather than known
  live-workspace stale publication.
- Smart-sync sentinel task rows and compatibility claims now live in
  `core.smart_sync.lifecycle`, and its job registry record is active before
  those claims are entered. Canonical alignment uses the pure planner for
  no-op/manual-warning/winner selection, parent gitlink propagation lives in
  the shared safe `core.smart_sync.propagation` service, threaded execution
  lives in `core.smart_sync.runner`, canonical plan execution lives in
  `core.smart_sync.executor`, and final cleanup uses the shared bounded
  refresh-plus-relink reconciler inside the smart-sync job.
- Task rows still carry some active-job metadata while the transition is in
  progress. Job registry target queries now flow through a shared
  job-registry-first helper; inline-refresh and pull-all startup gates rely on
  that job path for local mutation ownership, and task metadata is retained
  only as a compatibility fallback.
- UI wakeups now have a coalesced `core.ui_events.UiEvents` bridge wired from
  task/job mutations into the main loop's timeout selection. The bridge avoids
  thread-unsafe curses calls from workers; it does not interrupt an in-flight
  `getch`, so the remaining improvement would be an OS-level input wake
  mechanism if sub-timeout idle wakeups become necessary.

## Migration Rules

1. Local git or filesystem mutation jobs must acquire leases through the lease
   manager before work starts.
2. Remote/API monitor jobs must not block local refresh drains unless they also
   hold a local mutation lease.
3. Read-only modal loaders should support cancellation and UI wakeups, but they
   should not own repo mutation state.
4. Post-job refresh/relink should be requested through the refresh reconciler,
   not run directly inside arbitrary worker `finally` blocks.
5. Task rows should be emitted from job events; task metadata should stop being
   the authoritative active-job registry as phases land.

## First Migration Candidates

1. `kick_off_remote_changes`: narrow local mutation worker, already close to
   the desired claim pattern.
2. `kick_off_add_tag`: small mutation worker with action-like semantics.
3. `kick_off_inline_refresh`: central to responsiveness and already has many
   tests; migrate only after the refresh queue exists.
4. `kick_off_action` and `kick_off_workers`: high-value mutation paths after
   the runner is proven on smaller workers.
5. `kick_off_sync_siblings`: migrate after job, lease, refresh, and propagation
   services exist.
