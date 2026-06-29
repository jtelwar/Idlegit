# Refresh, Status, Sync, and Locking Audit

Date: 2026-06-19

## Summary

The recent slowdown is most likely not from `git fetch` being always-on. `fetch_on_manual_refresh` is still default-off and only affects Ctrl+R when enabled. The heavier regressions are:

- Startup now discovers and refreshes every configured workspace before entering the main UI.
- Startup and manual refresh use `refresh_repo_with_remote_state`, which can run `gh workflow list` per GitHub repo with local workflow files.
- Workspace switching does an immediate submodule relink and then schedules a full async refresh that relinks again.
- `link_siblings` holds a global lock while doing multiple git calls per submodule checkout.

These combine into a "status feels like it is fetching the world" effect even when the actual `git fetch --all` path is off.

## High Priority

### 1. Startup Refresh Is Unbounded Across Every Workspace

Evidence:

- `idlegit.py:258-280` discovers every configured workspace and calls `refresh_all_workspaces` before the main UI starts.
- `ui/loading.py:49-78` flattens all repos across all workspaces and starts one daemon thread per repo, bypassing `MAX_PARALLEL_GIT_JOBS`.
- `ui/loading.py:65-67` calls `refresh_repo_with_remote_state` for each repo.

Impact:

Large workspace sets can launch a burst of `git` and `gh` subprocesses. The loading screen cannot complete until every workspace's repos finish, so inactive workspaces delay use of the active workspace.

Recommendation:

Refresh the active workspace first with a bounded executor. Enter the main UI as soon as the active workspace is ready, then hydrate inactive workspace caches lazily or through a shared bounded refresh pool.

### 2. Remote Workflow State Is Queried During Refresh

Evidence:

- `core/workers.py:105-115` wraps `refresh_repo` and then merges remote workflow state.
- `core/git_ops.py:1428-1458` runs `gh workflow list --repo <slug> --all --json ...`.
- `ui/loading.py:65-67` and `core/workers.py:3660-3667` use this remote-state wrapper for startup and inline refresh.

Impact:

Any GitHub repo with local workflow files can perform network/API work during a normal-looking refresh. Each `gh` call has a 60 second timeout, and this happens per repo. This is probably the strongest explanation for refresh/status now feeling like it is doing remote work.

Recommendation:

Split local status refresh from remote workflow hydration. Keep startup, fs-watch refresh, workspace switch refresh, and Ctrl+R local by default. Fetch remote workflow state lazily when opening review/workflow UI, or cache it with a TTL and a clear "workflow state stale/refreshing" UI state.

### 3. Refresh Uses Many Git Processes Per Repo

Evidence:

- `core/git_ops.py:270-387` runs separate commands for work-tree check, branch, HEAD, upstream, origin URL, ahead/behind, status, git-dir, and submodule config.
- `.gitmodules` parsing currently gets paths with one `git config --get-regexp`, then fetches each URL with another `git config` call per submodule.

Impact:

Even local-only refresh is expensive across many repos. It also repeats similar state reads in action-menu paths.

Recommendation:

Collapse the core status read into fewer commands:

- Use `git status --branch --porcelain=v2 -z` to get branch, upstream, ahead/behind, and file status in one call.
- Use one `.gitmodules` parse for path and url entries, then join by submodule name in memory.
- Keep `remote get-url origin` separate if needed, but avoid redundant branch/upstream calls after porcelain v2.

### 4. Submodule Relinking Holds a Global Lock During Git Work

Evidence:

- `core/git_ops.py:447-466` serializes all `link_siblings` calls through `_link_siblings_lock`.
- `core/git_ops.py:583-640` populates each child while the lock is held, running `rev-parse HEAD`, `branch --show-current`, `status`, upstream lookup, `rev-list`, and `rev-parse --git-dir` per child.

Impact:

Concurrent refresh supervisors queue behind `link_siblings`, and the lock protects both the final atomic swap and the expensive snapshot work. On submodule-heavy workspaces this can dominate refresh time.

Recommendation:

Compute child snapshots outside the global lock using a bounded pool, then acquire the lock only to reconcile busy old refs and perform the final atomic swap. If preserving busy-ref identity requires a pre-snapshot, take a short locked snapshot of existing children, release, compute, then short-lock the swap.

### 5. Workspace Switch Performs Duplicate Relinking

Evidence:

- `core/workers.py:3897-3913` uses cached repos on workspace switch.
- `core/workers.py:3948-3951` immediately calls `link_siblings`.
- `core/workers.py:3983-3984` then calls `kick_off_inline_refresh`.
- `core/workers.py:3673` relinks again at the end of inline refresh.

Impact:

A cache-hit switch into a submodule-heavy workspace does an expensive child population immediately, then repeats it after the background refresh. This undermines the intended "instant" cache-hit switch.

Recommendation:

On cache hit, trust cached children for the immediate paint and skip the synchronous relink unless the cache is empty or invalidated. Let the async refresh perform the full relink. If a quick safety pass is needed, make it metadata-only and do not populate child git state synchronously.

## Correctness and Locking

### 6. Submodule URL Drift Is Not Reconciled Before Sync

Evidence:

- `core/git_ops.py:363-387` reads submodule URLs from `.gitmodules` for matching/display.
- `core/git_ops.py:755-782` syncs a nested checkout by running `git fetch origin` and `git checkout origin/<branch>` inside the nested checkout, without ensuring its local `origin` matches `.gitmodules`.

Impact:

If `.gitmodules` changes, Idlegit can display/match the new URL while fetching from a stale nested `origin`. That can sync the wrong remote.

Recommendation:

Before syncing a nested checkout, compare the parent `.gitmodules` URL to the nested checkout's `origin`. If they differ, run `git submodule sync -- <path>` from the parent or `git remote set-url origin <url>` in the nested checkout, with explicit task output.

### 7. `git_cancellable` Can Stall on Verbose Commands

Evidence:

- `core/git_ops.py:140-185` starts `Popen` with stdout/stderr pipes, waits for process exit, then reads the pipes afterward.
- The cancellable path is used for long-running commands such as pull/fetch/push.

Impact:

A noisy command can fill the OS pipe buffer and block before `wait()` returns, making cancellation/timeout behavior unreliable.

Recommendation:

Drain pipes while the process runs. Options: use `communicate(timeout=...)` in a loop with cancellation checks, or use background readers/selector-style draining while polling the process.

### 8. Remote-Edit Operations Bypass Refresh Locks

Evidence:

- `core/workers.py:934-999` applies remote remove/rename/set-url/add in a worker and refreshes afterward.
- It does not acquire `target_repo.try_acquire_refresh()` the way action workers and safe-merge paths do.

Impact:

Remote edits can race Ctrl+R, fs-watch refresh, or another action reading/mutating the same repo state. This is lower probability than the refresh hot path, but the pattern is inconsistent with the rest of the locking model.

Recommendation:

Wrap `kick_off_remote_changes` in the same acquire/release pattern as `kick_off_action`, with a warn task when the target is busy.

### 9. Pending Fs-Watch Drain Runs Synchronously on the UI Thread

Evidence:

- `core/fs_watcher.py:422-432` drains pending refreshes by calling `w.fire_refresh()` sequentially on the caller's thread.
- The main loop calls this after tasks drain and after review/safe-merge exits.

Impact:

The comment assumes one or two repos, but smart-sync and bulk actions can queue many affected repos. Each drained repo runs a full local refresh synchronously, so the TUI can pause exactly when the user expects it to become responsive again.

Recommendation:

Queue pending refreshes onto the same bounded refresh pool used by inline refresh. Mark rows refreshing synchronously for feedback, then let the pool perform the actual git reads.

### 10. Manual Refresh Fetch Failures Are Silent

Evidence:

- `core/workers.py:3651-3667` optionally runs `git fetch --all` before refresh when `fetch_on_manual_refresh` is enabled.
- Fetch exceptions and failures are swallowed, and local refresh still presents ahead/behind from stale refs.

Impact:

When the opt-in fetch mode fails, the UI can look authoritative while showing old remote-tracking state.

Recommendation:

Add a warn task per failed fetch or attach a "fetch failed; local state only" note to the refresh result.

## Suggested Implementation Order

1. Make startup active-workspace-first and bound the startup worker pool.
2. Split local refresh from remote workflow-state hydration; make workflow state lazy or cached.
3. Skip duplicate synchronous relink on workspace cache-hit switches.
4. Reduce `refresh_repo` command count with porcelain v2 and one-pass `.gitmodules` parsing.
5. Move expensive child-state population outside the `link_siblings` global lock.
6. Fix `git_cancellable` pipe draining.
7. Add submodule remote reconciliation before sibling sync.
8. Apply refresh locks around remote-edit workers.
9. Move fs-watch pending drain to a bounded async refresh queue.
10. Surface fetch failures when `fetch_on_manual_refresh` is enabled.

## Notes

`fetch_on_manual_refresh` itself is not the default culprit. It is configured default-off in `core/config.py:238-246`, `core/models.py:1753-1759`, and `idlegit.default.conf:46-53`. If a user enabled it globally or per workspace, Ctrl+R will indeed fetch every repo, but the current code has other remote and duplicate-local work even with that setting off.
