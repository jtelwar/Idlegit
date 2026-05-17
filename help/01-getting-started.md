# Getting started

Welcome to **Idlegit** — a TUI for managing git repo workspaces.

## What it does

Idlegit scans one or more folders for git repos and shows them on a
single screen. You can commit, push, sync submodules, and track
GitHub Actions across the whole workspace without leaving the
terminal.

## Workspaces

A *workspace* is a named list of folders to scan. The first launch
opens the workspace creator — point it at one or more folders and
the app discovers every immediate-child git repo inside each.

Switch between workspaces two ways:

- *←* / *→* on the workspace switcher row (just below the title) —
  cycles through the configured workspaces.
- *Tab* on the title row — opens the app menu; pick a workspace
  from the WORKSPACES section.

## The first commit

1. Edit any file inside a tracked repo.
2. The row picks up a yellow "dirty" dot. Press *←* to suggest a
   commit message, or just type one in.
3. Press *Enter* to open the review screen.
4. Confirm — Idlegit stages, commits, and (if auto-push is on)
   pushes for you.

## Picking up changes

Idlegit watches the filesystem by default (via `watchdog`) and
re-queries any repo whose working tree or `.git/` changes on disk.
Editing a file in your editor, a `git checkout` in another
terminal, or a hook that writes to the tree all surface as a row
update within ~400ms.

If the watcher is off (per-workspace setting), or the change
happened on a network mount where fs events are unreliable, three
manual gestures cover the gaps:

- *Ctrl+R* — re-query every repo locally (no network).
- *Ctrl+P* — `pull --ff-only` every repo with an upstream so
  ahead/behind reflects actual remote state.
- *Ctrl+S* — smart-sync: align every submodule checkout across the
  workspace.

All three work from anywhere on the main screen (title, workspace
switcher, repo list, task sidebar).

## Where to go next

- *Keyboard shortcuts* — every binding, grouped by panel.
- *Configuration* — what lives in `idlegit.conf` and how to tune it.
- *Smart-sync* — how submodule sibling alignment works.
- *Auto-refresh* — filesystem-watched row updates + ignore patterns.
- *Review screen* — what happens between Enter and the commit
  landing, including then-run chains.
