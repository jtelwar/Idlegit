# Foundation Rewrite Plan

Date: 2026-06-23

## Position

The prior foundation refactor is not complete. It introduced useful pieces,
but it did not make them the owners of the app. The current system still has
old trunk modules deciding state, jobs, git operations, input flow, and task
lifecycles while newer modules mirror or partially observe those decisions.

This plan replaces those owner boundaries. It is not an allowlist migration,
not a bypass plan, and not a field-by-field cleanup track. Each phase rewrites
one whole ownership boundary, moves production app functions onto it, deletes
the old owner path for that boundary, and adds guards that state the final rule.

The target architecture is:

- `app/`: input routing, screen coordination, ticking, modal stack policy,
  workspace switching, and render scheduling.
- `core/state/`: the single source of truth for workspaces, repo rows, child
  rows, status, topology, messages, workflow intent, selection, edit buffers,
  modal session state, and task/job projections.
- `core/runtime/`: job registry, worker runner, cancellation, subtask events,
  leases, timeout terminalization, task-row projection, and UI wakeups.
- `core/git/`: the only product module family allowed to run git, gh, ssh, or
  subprocess-backed filesystem/git work.
- `features/`: vertical workflows that translate user intents into runtime job
  specs and state mutations.
- `ui/`: pure rendering plus key/mouse-to-intent translation. No git, no
  subprocesses, no filesystem persistence, no thread creation, and no task-row
  readback for control flow.

## Non-Negotiable Rules

1. **No allowlists.** Guards describe the final ownership rule. They may not
   encode permanent exceptions for old files or old call paths.

2. **No bypasses.** A temporary adapter may only exist inside the active phase,
   only to keep an unmoved caller compiling, and only with a named deletion gate
   in that same phase.

3. **State has one owner.** Repo/child status, topology, messages, workflow
   intent, job lifecycle, task projection, leases, and workspace membership are
   owned by the state/runtime stores. Row objects and UI sessions do not carry
   independent lifecycle truth.

4. **Workers own time.** Anything that can block, scan, wait, spawn a process,
   touch the network, write config, or mutate git runs through runtime. The job
   row is visible before work starts.

5. **Task rows are presentation.** The app never asks a rendered task row
   whether a job is active, whether a repo is locked, or whether input should be
   enabled.

6. **Git has one gateway.** UI and features never invoke raw `git_ops`,
   `subprocess`, `threading.Thread`, or ad hoc command helpers. Features call
   typed runtime/git services.

7. **Leases are exclusive and RAII.** A mutation lease claim either succeeds
   exclusively or the job does not start. Read-only refresh busy state is a
   projection of claims, not a competing lock system.

8. **Old code is deleted.** A phase is not complete while the old owner path
   still does real work.

## Current Confirmed Problems

- `core/workers.py` is still a 6k+ LOC switchboard for unrelated workflows:
  commits, pushes, refresh, smart-sync, safe merge, workspace switching, app
  menu persistence, modal loaders, task rows, and direct thread factories.
- `core/git_ops.py` still mixes command execution, semantic git safety,
  refresh/status parsing, topology relinking, GitHub Actions, LFS, safe merge,
  stash helpers, and row-object mutation.
- `core.state.app.State` still owns `repos` beside `StateStore`; workspaces
  also keep `cached_repos`. Active workspace membership therefore exists in
  several places.
- `Repo` and `ChildRef` remain mutable status/topology projections. They still
  duplicate store-owned status facts, message state, child topology, siblings,
  and workflow metadata.
- Refresh and relink code returns typed snapshots but still mutates row
  projections afterward. The store is not yet the only consumer of git status.
- Feature modules still import `core.workers` and `core.git_ops` directly.
  Several UI/action paths therefore dispatch work by calling old worker
  functions instead of submitting typed runtime intents.
- Job lifecycle is split between `JobRegistry`, `Tasks`, `ThreadGroup`, and
  direct `threading.Thread` factories. Task rows are described as presentation,
  but task status is still used in places as lifecycle truth.
- Mutation leases are diagnostics/gating data, not exclusive ownership.
  `LeaseManager.acquire()` records overlap instead of refusing it.
- Smart-sync has typed modules, but the planner is not authoritative. The
  executor re-derives decisions from mutable checkout/state fields, propagation
  warnings are task-row-only truth, and cancellation is not threaded end to end.
- Startup discovery still scans before the loading UI can become responsive.
- Known synchronous UI-thread git/preflight paths remain:
  safe-merge abort cleanup, branch picker fast-forward checks, action-menu
  safe-merge entry checks, remotes modal queries, and startup discovery.
- Product code still has at least one destructive helper path:
  `git stash drop` after safe merge. That violates the cardinal rule as written
  and must be removed, not wrapped.

## Rewrite Phases

### Phase 0: Freeze the Old Shape

Goal: stop treating the existing hybrid as a destination.

Work:

- Keep `AGENTS/CURRENT_TASK.md` pointed at this rewrite until the foundation is
  actually complete.
- Add/adjust architecture guards so new production code cannot import old
  workers/git/threading/subprocess paths from UI or features.
- Make guards express final boundaries, not allowlists for old modules.
- Stop Phase 1B field cleanup unless a field blocks a trunk cutover.

Deletion gate:

- No new `kick_off_*` entry points may be added to `core.workers.py`.
- No new feature or UI module may import `core.workers` or `core.git_ops`.

Focused validation:

- Architecture guard tests.
- `git diff --check`.

### Phase 1: Make StateStore the Repo/Workspace SSOT

Goal: `StateStore` owns workspace membership, repo records, child records,
topology, status, messages, workflow intent, busy/suggesting state, and row
selection identifiers.

Work:

- Replace `State.repos` and workspace `cached_repos` with store workspace
  records and selectors.
- Convert `Repo` and `ChildRef` into identity-only handles or delete them in
  favor of store records.
- Move branch/head/upstream/ahead/behind/dirty/error/merging/message/workflows
  out of row objects.
- Move `Repo.children` and `Repo.siblings` into store topology.
- Make refresh/link producers return store-ready snapshots only.
- Delete projection mutation helpers after the store accepts snapshots.
- Move action-menu/review modal cached status metadata back to store selectors.

Deletion gate:

- Delete `apply_repo_refresh_snapshot`, `apply_child_refresh_snapshot`, and
  projection-mutating relink application.
- Delete row-owned lifecycle/status/topology fields once selectors cover all
  consumers.
- Delete `State.replace_repos()` as a dual-write compatibility path.

Focused validation:

- Store/topology/status/message tests.
- Main-screen/action-menu/review projection tests.
- Refresh and workspace-switch tests.

### Phase 2: Replace Jobs, Tasks, Leases, and Threads with Runtime

Goal: one runtime owns job lifecycle, task projections, cancellation, timeout
terminalization, worker threads, leases, subtasks, and UI wakeups.

Work:

- Create `core/runtime/` and move job registry, worker runner, task projection,
  lease manager, thread start, cancellation, and wakeup dispatch into it.
- Make task rows a projection of runtime job/subtask events.
- Make mutation lease acquisition exclusive. Overlapping mutation claims fail
  before the worker starts.
- Convert read-only busy indicators into runtime/store projections.
- Replace `ThreadGroup`, direct thread factories, and direct `submit_job` calls
  outside runtime.
- Ensure every user action creates the visible job row before preflight work.
- Terminalize jobs even if cleanup, refresh, relink, or task publishing fails.

Deletion gate:

- Delete `core/jobs.py`, `core/thread_group.py`, and `core/leases.py` as
  parallel runtime modules after their contents move.
- Delete `Tasks.has_running()` and any task-row lifecycle predicate used for
  control flow.
- Delete direct `threading.Thread` construction outside `core/runtime`.

Focused validation:

- Worker start failure releases leases and terminalizes the job.
- Timeout/cancel terminalizes and releases leases.
- Sidebar projection tests prove task rows are rendered from runtime events.

### Phase 3: Split Git into Typed Safe Services

Goal: all subprocess and git/gh/ssh execution goes through `core/git/`, with
typed command results, central safety policy, cancellation, timeout, and
progress events.

Work:

- Create `core/git/commands.py` for subprocess execution and cancellation.
- Create typed services for status snapshots, topology discovery, safe local
  mutations, remote actions, workflows, LFS progress, SSH/config helpers, and
  safe-merge helpers.
- Centralize cardinal-rule enforcement. Remove destructive product commands,
  including stash-drop pruning.
- Replace feature/workflow calls to raw `git`, `git_cancellable`,
  `git_bounded_output`, `merge_head_sha`, `is_fast_forward_merge`,
  `list_remotes`, and similar helpers with typed service calls.
- Emit progress events such as LFS upload progress through runtime subtasks.

Deletion gate:

- Delete or reduce `core/git_ops.py` to a non-production compatibility shell,
  then remove the shell.
- No production code outside `core/git/` imports `subprocess`, raw git helpers,
  or `gh` helpers.

Focused validation:

- Destructive-command guard tests.
- Git service unit tests with fake command runner.
- LFS/progress event projection tests.

### Phase 4: Replace the App Shell

Goal: curses input/rendering no longer coordinates workflows directly.

Work:

- Create `app/` modules for startup, main loop, input router, modal stack,
  workspace switching, scheduler ticks, watcher/periodic refresh scheduling,
  and quit confirmation.
- Make UI key handlers return intents, not call workers.
- Move startup discovery into a visible startup job so the loader draws before
  scanning.
- Make workspace entry publish a refresh intent through runtime.
- Keep Esc/quit handling independent of background cleanup.

Deletion gate:

- `idlegit.py` becomes a small composition/entrypoint file.
- `ui/main_loop.py` stops importing workers or git modules.
- No UI module creates jobs, threads, or subprocess-backed work directly.

Focused validation:

- Key routing tests for Esc, Ctrl-R, workspace cycling, app menu, and task
  focus.
- Startup loading test proves first paint can occur before discovery finishes.
- UI-thread blocking architecture guards.

### Phase 5: Rewrite Core Features Vertically

Goal: each app function is rebuilt on the new state/runtime/git/app foundation.

Order:

1. Refresh and workspace entry.
2. Smart-sync.
3. Commit/push/LFS/workflow tracking.
4. Pull all.
5. Safe merge.
6. Action menu and branch/remote/workflow pickers.
7. App menu/config/SSH/task log.
8. Clone/workspace management/update checks.
9. Diff/commit/task detail viewers.

Smart-sync requirements:

- Planner output is the execution contract.
- Executor never re-derives plan decisions from mutable row state.
- Parent propagation returns structured `ok/warn/fail` results.
- Parent pushes are skipped once per dirty parent decision and never retried
  for another child in the same run.
- A parent push is allowed only when the parent has exactly the intended
  submodule pointer change and no other dirty changes.
- Cancellation/timeout is threaded through every push, sync, prompt wait, and
  cleanup step.
- Cleanup/reconcile is a runtime job with bounded, terminal behavior.

Deletion gate:

- No feature module imports `core.workers`, `core.git_ops`, `threading`, or
  `subprocess`.
- Delete `core/workers.py` after the last feature moves.

Focused validation:

- Per-feature unit tests using fake runtime/git services.
- One timeout/cancellation test for every mutation feature.
- Integration-style tests for refresh, smart-sync no-op, dirty parent skip,
  LFS push progress, and Esc after job completion.

### Phase 6: Final Audit and Versioned Completion

Goal: prove the old architecture is gone.

Work:

- Run architecture guards for imports, direct threads, direct subprocesses,
  task-row control flow, row-object status fields, and destructive git commands.
- Run focused behavioral suites for state, runtime, git services, app shell,
  refresh, smart-sync, commit/push, safe merge, and menus.
- Run the broader required project gate when the final slice is ready:
  `make fmt && make test && make lint`.
- Update `VERSION` and clear `AGENTS/CURRENT_TASK.md` only when the complete
  foundation rewrite is actually done.
- Write a final audit in `AGENTS/FOUNDATION_REWRITE_FINAL_AUDIT.md`.

Completion criteria:

- `StateStore` and runtime are the only control-plane owners.
- UI and features are free of worker/git/thread/subprocess imports.
- `core/workers.py` and old `core/git_ops.py` no longer perform production
  orchestration.
- Smart-sync is a typed, cancellable runtime pipeline with structured results.
- Esc, Ctrl-R, workspace switching, app menu actions, and smart-sync completion
  cannot block on cleanup or git work in the UI thread.

## Immediate Next Slice

Start with Phase 2 before more projection-field polishing. The observed user
regression is app freezing/input loss, and that is a runtime/app-shell failure.

Slice 2A:

- Create `core/runtime/` with exclusive leases, job/subtask events, task-row
  projection, and a single worker runner.
- Move one small read-only action and one small mutation action onto runtime
  end to end.
- Add guards blocking new direct `threading.Thread` usage outside runtime.
- Do not remove all old workers yet, but do not add new old-worker routes.

Slice 2B:

- Move smart-sync job lifecycle onto runtime before rewriting smart-sync
  internals.
- Task row appears immediately.
- Runtime owns cancellation/timeout/final terminalization.
- Existing smart-sync implementation becomes a job body, then Phase 5 rewrites
  planner/executor/propagation properly.

Slice 4A should follow early:

- Move startup discovery and direct UI key preflights off the UI thread.
- Replace direct safe-merge abort, branch fast-forward probe, and action-menu
  merge-head probe with runtime intents.

