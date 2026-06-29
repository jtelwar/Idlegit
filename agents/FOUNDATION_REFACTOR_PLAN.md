# Foundation Refactor Plan: Jobs, Workers, State Monitoring, and Smart Sync

Date: 2026-06-20

## Goal

Build a reliable foundation for idlegit features by separating four concepts
that are currently tangled together:

- jobs: things idlegit is doing
- leases: repo or child rows currently owned by a job
- state monitoring: local git snapshots, filesystem events, and periodic/manual
  refreshes
- UI events: task rows, progress display, repaint wakeups, prompts, and input
  modes

Smart sync should be rebuilt on top of this foundation rather than continuing
to patch the current worker path.

## Current Diagnosis

The codebase has several good local fixes, but they are sitting on shaky
boundaries:

- `Task` rows are both UI history and active-job control-plane state.
- `Tasks.has_running()` means too many things: visible animation, pending
  follow-ups, and "do not refresh because a local mutation is active".
- `Repo.refreshing`, `ChildRef.refreshing`, `refresh_lock`, and task metadata
  all partially represent ownership.
- Workers create ad hoc daemon threads and are individually responsible for
  task terminalization, lock cleanup, refresh cleanup, and exception safety.
- State monitoring can run too much git work, duplicate relinks, and lose
  queued filesystem refreshes at exactly the completion edge.
- Smart sync has become the place where all of these weaknesses meet.

The user-visible symptom is freezes after push/sync/timeout. The underlying
systemic bug is that completion, cleanup, refresh, and input responsiveness are
not independent.

## Design Principles

1. Jobs are authoritative; task rows are presentation.
2. Leases own refresh/display flags; raw `refreshing = True/False` writes should
   disappear from orchestration code.
3. State monitoring is read-only unless it explicitly holds the right lease.
4. Refresh/relink work is bounded and generation-scoped.
5. Worker completion is terminal even when cleanup fails.
6. UI input and navigation are never owned by background cleanup.
7. Git operations return structured outcomes, not strings that callers infer.
8. Parent propagation is a shared safe service, not a smart-sync side effect.
9. Feature work should use the job foundation by default; exceptions should be
   rare and visible in review.

## Target Subsystems

### 1. Job Registry

Add a small authoritative job registry, separate from task rows.

Responsibilities:

- allocate a unique job id
- store job kind, workspace id, repo targets, child targets, started time, and
  cancellation event
- distinguish local mutation jobs from read-only monitoring jobs
- expose active local mutations by repo/child
- emit lifecycle events: started, progress, completed, failed, timed out,
  cancelled
- guarantee terminal transition once per job

Initial job kinds:

- `refresh`
- `manual-refresh`
- `commit`
- `push`
- `pull-all`
- `smart-sync`
- `safe-merge`
- `remote-edit`
- `workflow-poll`
- `task-followup`

Task rows should subscribe to job events and render them. A pending workflow
follow-up can remain visible without blocking refresh drains unless it owns a
local mutation lease.

### 2. Lease Manager

Replace scattered ownership with one RAII-style lease object.

Responsibilities:

- claim repo and child targets
- mark visible refreshing/suggesting state as needed
- block conflicting local mutations
- allow read-only refresh when safe
- preserve busy child identity across relinks
- release idempotently from `finally`
- report stale leases for diagnostics

The current `WorkerClaim` is the seed. The target is to move it out of
`core/workers.py`, make it the single blessed path, and remove smart-sync's
lockless sentinel pattern.

### 3. Worker Runner

Add a shared runner for threaded work.

Responsibilities:

- start a worker job with a job record and lease set
- create/update task presentation through event sinks
- catch all exceptions and convert them to terminal job outcomes
- handle thread-start failure by releasing leases immediately
- carry cancellation events
- enforce per-step timeouts where possible
- request reconciliation after mutation

Existing `kick_off_*` entry points should eventually become thin adapters:
validate UI selection, build a job spec, then submit it to the runner.

### 4. State Monitor and Refresh Queue

Refresh should be a bounded state-monitoring service, not a loose set of direct
calls from UI and workers.

Responsibilities:

- coalesce refresh requests by workspace/repo
- dedupe manual, periodic, fs-watch, and post-job refreshes
- run local refreshes through a shared bounded pool
- keep remote workflow hydration separate from local status refresh
- track workspace/repo generations
- discard stale late results
- surface stale-state warnings when reconciliation times out

This service should own the eventual replacement for inline refresh, watcher
drain, post-smart-sync cleanup, and workspace-entry refresh.

### 5. Git Snapshot Layer

Reduce repeated git calls and make state reads typed.

Responsibilities:

- produce a local `RepoSnapshot` from a bounded set of commands
- prefer `git status --branch --porcelain=v2 -z` for branch/upstream/status
  facts
- parse `.gitmodules` once per repo snapshot
- expose explicit snapshot freshness and errors
- keep remote workflow/query state out of ordinary local refresh

This is where performance work belongs. It should be tested independently from
the TUI and worker runner.

### 6. UI Event Bridge

The curses loop should draw and dispatch input; workers should publish events.

Responsibilities:

- wake the UI loop on job/task/refresh events
- keep task rows as a rendered event history
- keep modal/input mode ownership scoped and restorable
- make Esc and navigation independent of background cleanup

The first version can be a simple thread-safe queue plus wake event. It does
not need to become a large framework.

### 7. Smart Sync on the Foundation

Smart sync should then be rebuilt according to
`AGENTS/SMART_SYNC_REDESIGN_ADR.md`:

- pure planner
- bounded git backend
- runner execution
- shared submodule propagation
- generation-scoped reconciliation

Smart sync should not be allowed to introduce its own job/lease/task lifecycle.

## Recommended Refactor Order

### Phase 0: Guardrails and Inventory

Objective: stop adding new direct worker patterns while the foundation changes.

Tasks:

- document the rule that new long-running work must go through the runner
- add tests or lint-style greps for direct smart-sync raw `refreshing` writes
  once the lease manager exists
- inventory every `kick_off_*` entry point and classify it as mutation,
  read-only, prompt/modal, or monitor

Deliverable:

- `AGENTS/WORKER_INVENTORY.md`

### Phase 1: Split Task Predicates

Objective: remove the biggest semantic footgun without changing worker
architecture yet.

Tasks:

- add `Tasks.has_visible_activity()`
- add `Tasks.has_pending_followups()`
- add `Tasks.has_local_mutation_jobs()` as a compatibility wrapper around
  current active-job metadata
- update animation to use visible activity
- update fs-watch drain and periodic refresh idleness to use local mutation
  activity, not all pending task rows
- keep existing task UI behavior

Acceptance:

- pending workflow follow-ups animate or stay visible
- pending non-mutating tasks do not suppress fs-watch drain
- current smart-sync sentinels still suppress refresh while they hold active
  job metadata

### Phase 2: Extract Lease Manager

Objective: make ownership a first-class API.

Tasks:

- move `WorkerClaim` into a dedicated module, likely `core/jobs.py` or
  `core/leases.py`
- give leases explicit owner ids and target lists
- preserve current tests from `tests/test_worker_claim.py`
- migrate commit, push, remote-edit, safe-merge, inline refresh, and
  smart-sync sentinels onto the same lease API where possible
- add stale lease diagnostics in task/debug output

Acceptance:

- thread-start failures release every acquired lease
- child claim failures release parent claims
- raw orchestration-level `refreshing` writes are reduced and listed

### Phase 3: Introduce Job Registry and Runner

Objective: stop each worker from hand-writing lifecycle cleanup.

Tasks:

- add `core/jobs.py` with `Job`, `JobSpec`, `JobStatus`, `JobKind`, and
  `JobRegistry`
- add a `submit_job` runner that wraps thread start, exceptions, terminal
  state, cancellation, task bridge, and lease release
- route one low-risk worker through it first, probably remote-edit or manual
  refresh summary work
- then migrate commit/push/action workers

Acceptance:

- every submitted job has exactly one terminal status
- failed thread start produces terminal task rows and releases leases
- job registry, not task metadata, can answer "is this repo locally mutating?"

### Phase 4: Build Refresh Queue and Generation Reconciler

Objective: make state monitoring bounded, deduped, and safe under late results.

Tasks:

- add a refresh request queue keyed by workspace/repo
- run requests on a bounded executor
- mark rows refreshing through leases or monitor claims
- add workspace and repo generation tokens
- discard late refresh/relink results when generation changed
- make fs-watch drain enqueue refreshes instead of performing synchronous work
- make post-job cleanup request reconciliation instead of doing direct relink

Acceptance:

- workspace switching cannot be blocked by stale refresh work
- Ctrl+R after a failed job always schedules or reports a refresh
- late cleanup from an old workspace cannot mutate current workspace state
- fs-watch pending events are retried or retained when a target is still busy

### Phase 5: Git Snapshot Performance Pass

Objective: reduce local monitoring cost after ownership is reliable.

Tasks:

- introduce typed `RepoSnapshot` and `ChildSnapshot`
- consolidate branch/upstream/status reads with porcelain v2 where practical
- parse `.gitmodules` once
- move expensive child-state population outside the global sibling lock
- remove duplicate synchronous relink on workspace cache-hit switches
- keep remote workflow hydration lazy/cached and separate from local status

Acceptance:

- refresh command count per repo drops measurably
- active workspace enters quickly
- submodule-heavy workspaces do not serialize all child git reads under one
  global lock

### Phase 6: Shared Submodule Propagation Service

Objective: put the dangerous parent-push logic behind one safe API.

Tasks:

- extract propagation out of `core/workers.py`
- give pure-gitlink validation a typed result
- make dirty parent, nested dirty child, detached parent, and unrelated staged
  changes terminal skips
- use the same service from smart-sync, commit push, child push, and safe-merge

Acceptance:

- parent push is attempted only after fresh pure-gitlink validation
- dirty parent skip never calls `git push`
- skip/fail outcomes never leave a running task or active lease

### Phase 7: Rebuild Smart Sync

Objective: replace the current worker with the planner/runner/reconciler design.

Tasks:

- implement pure smart-sync types and planner
- add bounded git backend result types
- execute plans through the job runner
- request refresh reconciliation through the monitor
- remove sentinel-task active-job control
- delete replaced smart-sync orchestration code

Acceptance:

- smart-sync timeout cannot freeze input
- propagation skip cannot freeze input
- every step has a terminal outcome
- Ctrl+R works immediately after success, failure, timeout, or cancellation
- no smart-sync path invokes destructive git commands

## Suggested First PR Slice

Start with Phase 1 plus inventory:

1. Add task predicate split.
2. Update fs-watch drain and periodic refresh idleness to use local mutation
   activity.
3. Add tests proving pending non-mutating tasks do not block refresh drains.
4. Add `AGENTS/WORKER_INVENTORY.md` listing every current `kick_off_*` path and
   its migration target.

This is small enough to verify, but it immediately reduces the chance that a
stale pending row makes the app appear frozen.

## Non-Goals

- Do not rewrite every worker in one change.
- Do not introduce destructive git operations.
- Do not replace curses or the whole UI stack.
- Do not optimize git command count before ownership and generation safety are
  in place.
- Do not preserve old and new worker systems indefinitely; each phase should
  delete or narrow old pathways as it lands.

## Completion Definition

This foundation refactor is complete when:

- all long-running operations go through the job runner
- task rows no longer determine local mutation ownership
- repo/child leases are the only code path for mutation ownership and visible
  busy flags
- refreshes are queued, bounded, deduped, and generation-scoped
- smart-sync uses the shared foundation rather than custom sentinels
- manual refresh, workspace navigation, and Esc remain responsive after any
  job success, failure, timeout, or cancellation

