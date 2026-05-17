# Keyboard shortcuts

## Global

- *Ctrl+R* — refresh every repo's state. Idlegit also auto-refreshes
  on filesystem events by default; see the *Auto-refresh* help page
  for the toggle, debounce, and per-workspace ignore list.
- *Ctrl+S* — smart-sync (align submodule siblings across the
  workspace).
- *Ctrl+P* — `pull --ff-only` every repo in the workspace that has
  an upstream. Repos without an upstream are silently skipped;
  non-FF pulls fail with a task entry (use the action menu's Pull
  for a merge fallback). Honours the workspace's
  `auto_recurse_submodules` setting.
- *Tab* on the title row — open the app menu (workspaces, updates,
  auto-refresh, task logging, help).
- *Shift+Tab* — toggle focus between the repos panel and the task
  sidebar.

## Main panel (repos)

- *↑* / *↓* — move between repo rows. *↓* from the last row wraps to
  the first; *↑* from the first row steps up onto the workspace
  switcher and then the title row above it.
- *Tab* on a row — open the per-row action menu (push, branch,
  reset, remotes, …).
- *Enter* — open the review screen when at least one row has a
  pending commit message.
- *←* on a dirty row with an empty message — generate a commit-
  message suggestion. When the field already has content, *←*
  moves the cursor within the field.
- *Shift+←* on a dirty row with an empty message — generate
  suggestions for every dirty row at once.
- *Shift+→* on a dirty row — open the larger commit-message editor.
- *Esc* — when the focused row has a draft message, clear it.
  Otherwise quit (with a confirm prompt when any other row still
  carries a pending message).

## Task sidebar

- *Up* / *Down* — select a task row.
- *Tab* — open the task-detail modal.
- *Enter* — remove a finished task.
- *Esc* — return focus to the repos panel.

## Review screen

- *↑* / *↓* — navigate files / commit-message field.
- *Space* — toggle stage on the focused file (right pane); toggle
  workflow-tracking / LFS-tracking checkboxes (left pane).
- *←* / *→* — cycle a then-run target (workflow name / "add tag" /
  none) when the focused row is a then-run selector.
- *Tab* on a file — open the diff viewer.
- *Shift+Tab* — toggle focus between left (commit blocks) and
  right (files / toolbar) pane.
- *Enter* — confirm the commits (stages, commits, pushes, fires
  then-runs). On a toolbar button: invokes that button.
- *Ctrl+K* — clear every repo's then-run chains (tag / workflow
  dispatch / tracking opt-ins) without committing. Only advertised
  in the hint footer when something is actually set. See the
  *Review screen* help page for the full chain semantics.
- *Esc* — back to the main screen, discarding nothing (messages,
  staged-paths, and unfired then-runs all persist for next time).

## Modal convention

- *Tab* closes modals that were *opened* with Tab (app menu,
  workspace menu, task detail, action menu, commit view, diff
  viewer). Other modals close on *Esc* only.
- The commit-message editor opens with *Shift+→* and closes on
  *Esc* or *Enter*.
