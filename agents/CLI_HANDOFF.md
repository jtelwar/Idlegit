# CLI Handoff - Foundation Rewrite

Date: 2026-06-23
Repo: `/Users/joel/Workspace/idlegit`
Current version: `0.31.226`

## Goal

Continue the Idlegit foundation rewrite. Do not mark the long-term goal complete
yet. The rewrite is still in progress.

Target architecture:
- Single source of truth for repo state in `core/state/`.
- Runtime-owned jobs, task projection, leases, claims, and threads in
  `core/runtime/`.
- Task rows are presentation only. Workers publish through `JobTaskBridge`.
- Git operations should become centralized, bounded, cancellable services.
- Smart-sync must be reliable, non-destructive, cancellable, and must never leave
  input, task rows, or repo state stuck.

## Critical Rules

- Read `AGENTS.md` first in the CLI session.
- Idlegit product code must never run destructive git operations such as
  `reset --hard`, `checkout --`, `clean -fd`, `push --force`, `rebase`, or
  branch/ref deletion.
- Do not revert unrelated dirty work. This worktree is intentionally very dirty
  from the ongoing rewrite.
- Do several implementation slices between broad test runs. Use focused tests
  after risky slices.
- Use `apply_patch` for manual edits.

## Current State

The active worktree has many modified and untracked files from the foundation
rewrite. This is expected.

Important recent version entries:
- `0.31.226`: safe-merge, remote-edit, and smart-sync rows are bound to runtime
  jobs; smart-sync cancellation was hardened.
- `0.31.225`: commit-batch worker task rows were bound to runtime jobs.
- `0.31.224`: workflow polling and then-run rows were linked to runtime jobs.
- `0.31.223`: task-detail actions route through runtime projection queries.

Recent completed slice:
- `core/workers.py`
  - Action worker rows now use a job-bound `JobTaskBridge`.
  - App-menu read-only helper creates the job before task rows and binds rows to
    that job.
  - Remote-edit rows now use a job-bound bridge.
  - Safe-merge begin/finalize/confirm create runtime jobs before publishing rows.
  - Safe-merge helpers accept an optional `task_bridge`.
  - Fixed stale undefined names in app-menu toggle functions.
- `core/smart_sync/lifecycle.py`
  - Added `SmartSyncLifecycle.cancel()` to release row state and finish the job
    as cancelled.
- `core/smart_sync/runner.py`
  - Added cancellation checkpoints around canonical, propagation, and subtree
    phases.
  - Skips final cleanup on cancellation so cleanup cannot keep the app appearing
    busy after a cancelled smart-sync.
  - Passes the owning task bridge into smart-sync work.
- `core/smart_sync/propagation.py`
  - Propagation helpers accept optional `task_bridge` and `cancel_event`.
  - Propagation task rows now publish through the owning runtime bridge when
    called from smart-sync.
- `core/smart_sync/executor.py`
  - Canonical warning rows now publish through an optional runtime bridge.
- `tests/test_smart_sync_runner.py`
  - Added cancellation regression coverage proving cancellation releases locks,
    skips cleanup, and leaves no running task rows.

## Last Validation

Passed:

```sh
python3 -m unittest \
  tests.test_safe_merge_begin_job \
  tests.test_safe_merge_confirm_job \
  tests.test_safe_merge \
  tests.test_app_menu_feature \
  tests.test_job_task_bridge \
  tests.test_smart_sync_runner \
  tests.test_smart_sync_propagation \
  tests.test_smart_sync_executor
```

Result: 59 tests OK.

Passed:

```sh
python3 -m ruff check \
  core/workers.py \
  core/smart_sync/executor.py \
  core/smart_sync/runner.py \
  core/smart_sync/propagation.py \
  core/smart_sync/lifecycle.py \
  tests/test_smart_sync_runner.py
```

Passed:

```sh
git diff --check
```

Passed with writable pycache:

```sh
PYTHONPYCACHEPREFIX=/private/tmp/idlegit-pycache python3 -m py_compile \
  core/workers.py \
  core/smart_sync/executor.py \
  core/smart_sync/runner.py \
  core/smart_sync/propagation.py \
  core/smart_sync/lifecycle.py \
  tests/test_smart_sync_runner.py
```

Tooling caveat:
- `python3 -m pytest tests/test_architecture_guards.py -q` cannot run in this
  environment because `pytest` is not installed.
- `python3 -m unittest tests.test_architecture_guards` reports zero tests
  because that file is function-style pytest tests.
- Plain `py_compile` may try to write bytecode under
  `~/Library/Caches/com.apple.python/...`; use `PYTHONPYCACHEPREFIX`.

## Next Slice

Do this next:

1. Sweep remaining direct worker task publishers and route production paths
   through runtime-owned bridges.

   Useful search:

   ```sh
   rg -n "state\\.tasks\\.(add|update|set_label|clear_message|remove)" core/workers.py core/smart_sync features ui idlegit.py
   rg -n "JobTaskBridge\\(state\\.tasks\\)" core/workers.py core/smart_sync features ui idlegit.py
   ```

2. Prioritize direct publishers that sit inside runtime jobs and can leave rows
   orphaned or non-terminal:
   - review detached preflight around `kick_off_detached_review_preflight`
   - pull-all / refresh workspace rows
   - tag/clone/remote-change helper rows if still raw
   - any smart-sync cleanup or propagation fallback rows not yet bridge-owned

3. After that, move UI-thread preflight/discovery work into runtime intents/jobs:
   menus, branch pickers, app menu toggles, workspace loading, and anything that
   touches disk/git before showing a task row.

4. Then start the git gateway split:
   - typed command specs
   - central timeout/cancellation/progress policy
   - destructive-command deny policy
   - push/LFS progress hooks for task subtasks

## Files To Inspect First

- `AGENTS/CURRENT_TASK.md`
- `AGENTS/FOUNDATION_REWRITE_PLAN.md`
- `AGENTS/UI_THREAD_BLOCKING_AUDIT.md`
- `AGENTS/WORKER_INVENTORY.md`
- `VERSION`
- `core/runtime/jobs.py`
- `core/runtime/claims.py`
- `core/runtime/tasks.py`
- `core/workers.py`
- `core/smart_sync/runner.py`
- `core/smart_sync/lifecycle.py`
- `core/smart_sync/propagation.py`
- `core/smart_sync/executor.py`
- `tests/test_job_task_bridge.py`
- `tests/test_smart_sync_runner.py`
- `tests/test_safe_merge_begin_job.py`
- `tests/test_safe_merge_confirm_job.py`

## Acceptance Focus

The user's hard acceptance criterion is interaction responsiveness:
- Smart-sync completion, timeout, cancellation, and cleanup must never leave
  input frozen.
- No job should leave `Tasks.has_running()` true through a stale running or
  pending presentation row.
- Background work must be visible immediately as a task row and owned by a
  runtime job.
- Repo rows must be locked while mutating operations are active, especially
  push/LFS upload.
- Refresh/watch behavior should only track the active workspace unless a
  runtime job owns the state being tracked.

## Recommended First Command Set

```sh
sed -n '1,220p' AGENTS.md
sed -n '1,120p' AGENTS/CURRENT_TASK.md
sed -n '1,80p' VERSION
git status --short
rg -n "state\\.tasks\\.(add|update|set_label|clear_message|remove)" core/workers.py core/smart_sync features ui idlegit.py
rg -n "JobTaskBridge\\(state\\.tasks\\)" core/workers.py core/smart_sync features ui idlegit.py
```

Then pick the smallest set of related publishers and convert those production
launchers to create the `Job` before publishing rows, using
`JobTaskBridge(state.tasks, state.job_registry, job)`.

