# Auto-refresh

Idlegit watches each repo's working tree for changes and refreshes its
row when something on disk moves — branch switches in another terminal,
file edits in your editor, post-commit hook output. You don't have to
hit *Ctrl+R* after every external action; the row catches up on its
own.

## Toggling

App menu → **AUTO REFRESH** section (open with *Tab* on the title row).
The two rows there are:

- **Enable / Disable filesystem auto-refresh** — flips the global
  switch. When off, *Ctrl+R* is the only way to update a row.
- **Debounce: N ms** — cycles through `200 / 400 / 800 / 1500` ms.
  Bursts of writes within the debounce window collapse into one
  refresh; longer values are kinder to noisy projects, shorter ones
  feel snappier on quiet repos.

Both changes save to `idlegit.conf` immediately
(`auto_refresh_on_fs_change`, `auto_refresh_debounce_ms`).

## What's filtered automatically

The watcher never reacts to:

- Writes anywhere under `.git/` — git itself touches `.git/index` on
  every `git status`, and reacting to that would loop the auto-refresh
  into a flicker.
- Events that fire while a row's action is already in flight (a commit
  / sync / etc. is already going to refresh that row when it lands).
- Events that fire while you're in the review/confirm screen — your
  per-file checkboxes don't shift under your cursor mid-decision.
  Suppressed events fire once on review exit.
- Events that fire while *any* sidebar task is running (e.g. a smart-
  sync writing to multiple working trees). Once the last task settles,
  one refresh fires per affected repo.

## Per-workspace ignore list

Open the workspace menu (*Tab* on the workspace row) → **FILE WATCH
IGNORE** section. *Enter* on a row to edit, *Enter* on *+ Add
pattern…* to add, *Backspace* on a row to remove.

Patterns use **gitignore syntax** (compiled via `pathspec`):

- `*.log` — any `.log` file at any depth.
- `dist/**` — everything under a `dist/` directory.
- `/build` — `build` only at the repo root (leading `/` anchors).
- `node_modules/` — directory-only match (trailing `/`).
- `!keep.log` — un-ignore a previously matched path.
- `# comment lines start with hash` (comments are ignored).

Patterns are matched against each event's path relative to the repo
root. Saved per-workspace to `idlegit.workspaces`. Changes take effect
on the next fs event — no restart needed.

## When to disable

- Network mounts (NFS, SMB) where filesystem events are unreliable or
  not delivered at all — auto-refresh will silently not work; flip it
  off and use *Ctrl+R*.
- Very large monorepos where the watcher's per-event overhead is
  noticeable — disable globally or use the ignore list to exclude
  busy directories (build outputs, language-server caches).
