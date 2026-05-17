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

Idlegit does **not** poll the filesystem. The repo-state column you
see on screen is a snapshot taken when the workspace was loaded (or
last refreshed) — files edited outside the app, a `git pull` run
from another terminal, or a teammate's push that landed since you
last looked, won't show until you ask for a refresh.

Press *Ctrl+R* on the main screen to re-query every repo. The same
binding works while the task sidebar has focus.

## Where to go next

- *Keyboard shortcuts* — every binding, grouped by panel.
- *Configuration* — what lives in `idlegit.conf` and how to tune it.
- *Smart-sync* — how submodule sibling alignment works.
