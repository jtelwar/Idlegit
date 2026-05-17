# Configuration

Two files live in the user data dir:

- `idlegit.conf` — global defaults.
- `idlegit.workspaces` — workspace folder lists and per-workspace
  overrides of any conf key.

## Where they live

- **macOS:** `~/Library/Application Support/idlegit/`
- **Windows:** `%APPDATA%\idlegit\`
- **Linux:** `$XDG_CONFIG_HOME/idlegit` or `~/.config/idlegit/`

Override with the `IDLEGIT_CONFIG_DIR` env var.

## Editing

`idlegit.conf` ships with inline `;` comments documenting every
option — open it in your editor of choice. Restart Idlegit to pick
up changes.

The workspace menu (*Tab* on the workspace row) lets you flip most
defaults *per workspace* without leaving the app. Those overrides
land in `idlegit.workspaces`.

## Auto-refresh

`auto_refresh_on_fs_change` (default `true`) and
`auto_refresh_debounce_ms` (default `400`) control the filesystem
watcher. Per-workspace ignore patterns live under each
`[workspace.<name>]` section as a multi-line `fs_watch_ignore =` block
(gitignore syntax). All three are also editable in the app menu's
**AUTO REFRESH** section and the workspace menu's **FILE WATCH
IGNORE** section. See the *Auto-refresh* help page for details.

`fetch_on_manual_refresh` (default `false`) makes *Ctrl+R* run
`git fetch --all` per repo before re-reading state, so the
ahead/behind columns reflect actual upstream rather than the last
fetch. Off by default — *Ctrl+R* has historically been instant +
offline; turning this on adds ~200ms/repo against a fast remote.
Working trees are never modified by *Ctrl+R*.

## Submodule handling

`auto_recurse_submodules` (default `true`) makes idlegit's pull /
fetch operations pass `--recurse-submodules=on-demand` so a
parent's working tree advances AND its submodule checkouts get
synced to the new gitlinks in one shot. Internal only — never
writes to your `git config submodule.recurse`. Affects:

- The action menu's **Pull** / **Fetch**.
- The commit pipeline's pre-stage pull.
- *Ctrl+P* "pull all".
- The pre-pull step in *Ctrl+S* smart-sync.

Editable in the workspace settings modal's **SMART-SYNC** section
(toggle row "Auto-recurse submodules"). Outside-of-idlegit
`git pull` is unchanged.

## Task logging

When `task_log_enabled` is on, every task that lands in a terminal
status (ok/fail/warn) appends a line to `task_log_path`. The app
menu's **TASK LOGGING** section shows the live state and exposes
toggle / open / clear actions.

Caps and rotation:

- `task_log_max_lines = 0` — unlimited (no rotation).
- `task_log_max_lines = N > 0` — trim oldest lines first when N is
  exceeded.

## Reset

Delete `idlegit.conf` and Idlegit re-seeds it from the bundled
template on next launch. `idlegit.workspaces` is *not* re-seeded —
you'll be sent back to the workspace creator wizard.
