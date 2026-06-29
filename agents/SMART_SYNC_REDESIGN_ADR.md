# ADR-001: Smart Sync Planner, Runner, and Reconciler Redesign

## Status

Proposed

## Date

2026-06-20

## Deciders

Joel, Codex

## Context

Smart sync has accumulated too many responsibilities in one worker path. The
current `kick_off_sync_siblings` flow plans sync work, mutates repositories,
owns task rows, acts as refresh gate, opens prompts, runs submodule parent
propagation, and performs final UI reconciliation. Recent fixes have improved
specific failure edges, but the shape remains fragile: a timeout, leaked
pending task, late cleanup thread, or parent-propagation refusal can still
affect input responsiveness and refresh state.

The failure reported here is a parent push timing out and the app becoming
unresponsive afterwards. The root architectural problem is broader than the
push itself:

- task rows are used as control-plane active-job state
- `Tasks.has_running()` mixes visible activity, pending follow-ups, and local
  mutation gates
- refresh ownership is split across booleans, locks, and task metadata
- final cleanup can continue mutating live state after the UI has released
  ownership
- parent propagation is a private smart-sync helper even though commit,
  child-push, and safe-merge flows also depend on it
- smart-sync planning is not testable without git, threads, task rows, and UI
  prompts

The new design must preserve idlegit's safety rules. It must not use destructive
git operations, force pushes, rebases, hard resets, or working-tree deletion.

## Decision

Replace smart sync with a planner/runner/reconciler architecture.

Smart sync will become a read-only planning phase followed by a bounded
execution phase. UI rendering, task rows, refresh reconciliation, active job
ownership, and submodule parent propagation will be explicit services rather
than hidden side effects of one worker function.

The core invariant is:

> No smart-sync success, failure, timeout, cancellation, prompt, cleanup, or
> parent-propagation result may keep the TUI input loop, workspace navigation,
> or manual Ctrl+R refresh hostage.

## Target Architecture

### 1. Pure Smart Sync Domain

Add `core/smart_sync/types.py`.

Initial domain objects:

- `CheckoutSnapshot`: read-only facts about one checkout
- `CanonicalGroup`: one canonical repo plus sibling checkouts
- `SyncPlan`: immutable list of sync steps and dependencies
- `SyncStep`: one bounded action with target, preconditions, timeout, and
  failure policy
- `StepOutcome`: typed result for success, skip, warning, failure, timeout, or
  cancellation
- `PropagationCandidate`: parent repo plus expected gitlink changes
- `PropagationResult`: typed parent propagation result

No object in this layer should know about curses, `State`, `Task`, refresh
locks, or threads.

### 2. Snapshot and Planner

Add `core/smart_sync/discovery.py` and `core/smart_sync/planner.py`.

Discovery will snapshot the active workspace topology into canonical groups.
Planning will decide:

- which checkout is canonical
- whether a winner has dirty work to commit first
- which sibling checkouts can fast-forward
- which checkouts require a prompt before non-fast-forward alignment
- which subtrees need a pull
- which parent repos are propagation candidates

Planning is read-only. It must not stage, commit, fetch, merge, push, update
tasks, or open prompts.

Planner tests should be pure unit tests: given topology plus checkout facts,
produce an expected `SyncPlan`.

### 3. Bounded Git Backend

Add `core/smart_sync/git_backend.py`.

This layer wraps git operations used by the runner. It returns structured
outcomes instead of strings that callers have to interpret.

Required operation families:

- probe checkout facts
- fetch a remote with timeout and cancellation
- fast-forward a checkout safely
- commit a dirty canonical checkout
- push a branch with timeout and cancellation
- verify pure parent gitlink changes immediately before propagation
- stage and commit only validated gitlink paths

The backend must continue to avoid destructive operations. It should centralize
timeouts, process cleanup, output draining, and cancellation.

### 4. Smart Sync Runner

Add `core/smart_sync/runner.py`.

The runner executes a `SyncPlan`. It is the only smart-sync module that may know
about `State.tasks`, prompts, and worker claims. It owns:

- a unique run id
- cancellation state
- task row creation and terminalization
- active local mutation leases
- prompt adapter calls
- progress events
- execution summary

Every started step must reach a terminal outcome. A failed cleanup may create a
terminal warning task, but it must not keep the smart-sync task running.

### 5. Job and Lease Ownership

Introduce a single active-job or lease abstraction that subsumes the current
mix of `refreshing` flags, refresh locks, active task metadata, and manual
sentinel rows.

The lease should:

- identify owner run id and step id
- own repo and child display flags
- expose whether it is a local mutation
- release in one idempotent `finally` path
- support stale/deadline diagnostics

Task rows can render job state, but task rows should not be the source of truth
for whether refreshes and filesystem drains are allowed.

### 6. Shared Submodule Propagation Service

Add `core/submodule_propagation.py`.

Move parent propagation out of `core/workers.py`. Ctrl+S, normal commit push,
child commit push, and safe-merge should call the same service.

Propagation policy:

- only propagate when the parent has exactly the expected gitlink changes
- reject nested dirty submodule checkouts
- reject unrelated staged or unstaged parent changes
- reject detached or unsafe parent branch state
- stage only validated gitlink paths
- push only after a fresh preflight immediately before commit/push
- never push a parent when the parent is dirty for any reason other than the
  validated submodule pointer bump

An unsafe parent is a successful skip, not a hung operation. The user should see
a terminal warning row explaining why the parent was left alone.

### 7. Refresh Reconciler

Move smart-sync final refresh and relink into a generation-scoped reconciler.

The runner may request reconciliation, but reconciliation must be best-effort
and bounded. Late reconciliation results may update live UI state only when the
workspace generation and repo generation still match the run that requested
them. Otherwise the result is discarded and the user gets a terminal warning
such as "state may be stale; press Ctrl+R".

### 8. UI Wake and Task Predicates

Split task predicates:

- visible activity: rows that should animate or update relative times
- local mutation activity: jobs that must suppress refresh/fs-watch drains
- pending follow-ups: queued task rows waiting on a parent task

The main loop should eventually wake on worker/task events instead of relying
only on dynamic polling timeouts. This is not required for the first migration
slice, but it is part of the target design because it makes "frozen" versus
"idle tick" behavior observable.

## Options Considered

### Option A: Keep Patching the Current Worker

This is the smallest short-term path. It can add more timeouts, more sentinel
cleanup, and more exception guards.

Rejected as the long-term answer. The current worker already contains multiple
scars from completion-edge bugs. More patches reduce individual failures but
keep the planner, executor, refresh reconciler, and task gate tangled together.

### Option B: Planner, Runner, Reconciler, and Shared Propagation

This is the recommended path. It extracts pure planning first, then routes
execution through bounded services while preserving current user behavior during
migration.

Accepted.

### Option C: External Sync Subprocess or Durable Job Queue

This would isolate crashes and make cancellation cleaner, but it is too large
for the immediate need. Idlegit can get the main safety and responsiveness
benefits inside the current process once ownership and refresh reconciliation
are explicit.

Deferred.

## Trade-off Analysis

The recommended design adds several modules and typed result objects, but it
buys simpler tests and clearer failure boundaries. The migration can be
incremental because the old `kick_off_sync_siblings` entry point can initially
delegate to the new runner while existing tests continue to cover end-to-end
behavior.

The main risk is a half-migration where old task metadata and new job leases
both act as control-plane state. To avoid that, each phase needs a narrow
ownership rule and tests that assert the old source of truth no longer controls
the migrated behavior.

## Migration Plan

### Phase 1: Pure Types and Planner Surface

- Add `core/smart_sync/types.py`.
- Add pure planner tests for canonical groups, dirty canonical handling,
  sibling alignment, prompt-required cases, subtree steps, and parent
  propagation candidates.
- Keep existing runtime behavior unchanged.

### Phase 2: Structured Git Backend

- Add bounded git backend wrappers for smart-sync operations.
- Convert safety predicates to typed results, especially parent gitlink
  validation.
- Keep destructive operations forbidden.
- Add integration tests for timeout, cancellation, dirty-parent skip, nested
  dirty skip, and structured result mapping.

### Phase 3: Shared Propagation Service

- Move parent propagation logic out of `core/workers.py`.
- Make Ctrl+S, commit push, child push, and safe-merge use the same service.
- Preserve the rule that dirty parents are skipped, not pushed.
- Add tests proving dirty parents do not call `git push`.

### Phase 4: Smart Sync Runner Behind Existing Entry Point

- Add `core/smart_sync/runner.py`.
- Keep `kick_off_sync_siblings(state)` as the public entry point, but make it
  build a snapshot, create a plan, and delegate execution to the runner.
- Every runner step gets a terminal task outcome.
- Worker exceptions, git timeouts, and cancellations release leases.

### Phase 5: Job Registry and Lease Consolidation

- Move active local-mutation gating out of task metadata.
- Split `Tasks.has_running()` callers onto more precise predicates.
- Remove raw smart-sync `refreshing = True/False` writes from orchestration
  code after equivalent lease coverage exists.

### Phase 6: Generation-Scoped Refresh Reconciler

- Replace smart-sync final cleanup thread with a bounded reconciliation request.
- Drop late reconciliation writes when workspace/repo generations are stale.
- Preserve visible stale-state warnings and manual Ctrl+R recovery.

### Phase 7: UI Wakeups

- Add a worker event wake path for task transitions, lease releases, cleanup
  warnings, and refresh requests.
- Keep curses drawing and input dispatch on the main thread.

## Acceptance Tests

The redesign is not complete until these behaviors are covered:

- navigation and Esc remain responsive while smart-sync is running
- navigation and Esc remain responsive after smart-sync success
- navigation and Esc remain responsive after smart-sync timeout
- navigation and Esc remain responsive after parent propagation skip
- every started smart-sync task reaches `ok`, `warn`, `fail`, `timeout`, or
  `cancelled`
- dirty parent propagation skip never invokes `git push`
- nested dirty submodule skip never invokes parent `git push`
- cleanup timeout releases all leases and leaves a terminal warning task
- late cleanup cannot mutate a newer workspace generation
- Ctrl+R works after smart-sync failure or timeout
- pending non-mutating task rows do not suppress refresh drains
- no smart-sync path invokes destructive git commands

## Immediate Next Step

Start with Phase 1 and Phase 2. This creates a testable core without changing
the public Ctrl+S behavior yet. Once the planner and backend surfaces are in
place, migrate parent propagation before replacing the runner.

