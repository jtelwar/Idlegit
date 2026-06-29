# Foundation Regression Audit

Date: 2026-06-21

## Trigger

The app froze after toggling filesystem watching from the app menu, and recent
smart-sync runs have repeatedly left the UI unresponsive after timeout/failure
states. The user also reported higher CPU usage after the foundation refactor.

## Findings

### 1. App-menu filesystem watcher toggle blocked the UI thread

`ui.modals.app_menu._fire_toggle_auto_refresh` changed
`state.auto_refresh_on_fs_change` and then called
`core.fs_watcher.reconcile_repo_watchers(state)` directly from the app-menu key
handler.

That reconcile path can start an observer, recursively schedule every repo in
the active workspace, or stop and join the existing observer. On large
workspaces this is too much work for the curses input thread and can make the
app appear frozen.

Fix: the toggle now updates visible state immediately, then runs watcher
reconciliation and config persistence in a read-only `auto-refresh-toggle` job.

### 2. macOS file watching used a recursive polling observer

`core.fs_watcher` selected `watchdog.observers.polling.PollingObserver` on
macOS. Recursive polling over many repos and nested working trees can explain
the higher CPU usage when file watching is enabled.

Fix: use watchdog's platform observer by default and keep polling only as an
import fallback.

## Remaining Risk

The smart-sync completion freeze reports may still have additional causes, but
this audit found and fixed one concrete UI-thread blocking path and one concrete
CPU-heavy watcher backend introduced by recent lifecycle changes.

## Verification

- `python3 -m unittest tests.test_workspaces.TestAppMenu.test_filesystem_watcher_toggle_schedules_nonblocking_job tests.test_fs_watcher.ReconcileTests.test_reconcile_attaches_for_each_repo tests.test_fs_watcher.ReconcileTests.test_disable_after_enable_tears_observer_down -q`
- `python3 -m ruff check ui/modals/app_menu.py core/fs_watcher.py tests/test_workspaces.py`
- `python3 -m compileall -q ui/modals/app_menu.py core/fs_watcher.py`
- `git diff --check`
