# UI Thread Blocking Audit

## 2026-06-21

Scope: input handlers, app-menu row building, and smart-sync startup paths that can run before the user sees task feedback.

## Findings

1. `kick_off_sync_siblings` created a smart-sync task only after synchronous preflight work.
   - The path enumerated repos/subtrees and ran `_canonical_already_aligned` before the user had reliable task-panel feedback.
   - Fix: create the `smart-sync` task and job immediately, then run preflight and execution inside the job.

2. Smart-sync had two ownership phases instead of one visible job lifecycle.
   - The previous launch path registered the job after preflight and then started a worker around an already-built config.
   - Fix: one outer job now owns preparation, lifecycle acquisition, execution, and final status. The parent task is renamed to `smart-sync (N)` once the worker knows the work count.

3. App-menu SSH status rows probed `ssh-add -l` synchronously while building menu rows.
   - `ssh_tools_status`, `agent_status_label`, and `keys_loaded_label` can run subprocesses with timeouts.
   - Fix: opening the menu shows cached/checking SSH labels immediately. The first app-menu tick schedules the read-only status job after the menu has opened.

4. SSH action preflight used full SSH status snapshots when only PATH checks were needed.
   - "Create keypair" only needs `ssh-keygen` existence; "Load default keys" only needs `ssh-add` existence before its worker starts.
   - Fix: replace those UI-thread full probes with `shutil.which`.

## Follow-up Risks

- Config writes in app-menu toggles still happen synchronously for several small settings. They are usually tiny local writes, but they remain candidates for the same "task first, persist in job" pattern if the config directory is slow.
- Task-log size/line-count rows still inspect the log file while building the menu. This is bounded for normal logs but should move to cached async metadata if large logs or network-backed config paths become common.

## Follow-up Fixes

5. Opening the app menu still scheduled the SSH status job from `open_app_menu`.
   - Even though the subprocess probe moved to a worker, menu-open itself was not pure.
   - Fix: `open_app_menu` now only builds cached rows; `tick_app_menu_update_check` starts the SSH status job on the next loop and owns row rebuilds after the worker updates cached labels.

6. Disabled filesystem watching could leave debounce threads asleep for up to 60 seconds.
   - `detach()` set `_stopped` and `_fire_at`, but the debounce loop was blocked in `time.sleep`.
   - Fix: debounce now waits on a condition and `detach()` wakes it immediately.
