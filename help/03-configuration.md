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

Idlegit never passes `--recurse-submodules` to its `pull` / `fetch`
calls. The flag re-checks out each submodule to the parent's
recorded gitlink, which silently orphans any local-only submodule
commits ahead of that gitlink. Use *Ctrl+S* (smart-sync) to align
submodule checkouts safely — its `sync_sibling` step refuses to
orphan commits and surfaces a task row when manual intervention is
needed.

`git clone` started from the **Clone** modal still recurses (initial
clone — no HEAD to rewind).

## SSH agent

`auto_start_ssh_agent` (default `true`) starts `ssh-agent` at launch when
no usable `SSH_AUTH_SOCK` is set. The app menu **SSH** section shows agent
status, toggles autostart, can generate an ed25519 keypair for GitHub, and
can run `ssh-add` on the usual `~/.ssh/id_ed25519` / `id_rsa` paths.

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
