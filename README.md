# idlegit

A small interactive multi-repo git tool. One screen for status of the whole workspace. Stage, commit, and
push across every repo and track the resulting actions.

## Why

Because clicking through a painfully inefficient GUI git tool to commit-and-push the same set of repos every day, then navigating to github to track their actions, got old. 

And because the name lazygit was already taken, but it's just not lazy enough for me...

## Run

Drop the `idlegit/` folder anywhere and run it; everything is on one
keyboard-driven screen.

```sh
./idlegit/idlegit
```

On first launch it pops a setup dialog asking you to type one or more
folder paths to scan; each one becomes a named workspace you can switch
between at runtime (Up to the title row, ←/→ to cycle, Space to edit
that workspace's overrides). Workspaces are persisted to
`idlegit.workspaces` next to the script.

## Design Goals

   Safe - Frictionless - Opinionated - Configurable

## Features

- Two-keypress staging, committing, pushing
- Github action running, tracking, chaining
- Completely non-destructive*
- Auto-writes commit messages in an instant
- Smart-syncs submodules across a workspace
- Uses background workers
- Tracks every operation
- Small footprint, light on resources

## Configuration

`idlegit.conf` next to the script. Inline comments (`;`) document every option.
Defaults are sensible — you can run with no config at all.

## Requirements

- Python 3.9+ (stdlib only: `configparser`, `curses`, `concurrent.futures`)
- `git` on `$PATH`
- `git-lfs` on `$PATH` if you want to use the LFS toggle on the review screen
- `gh` on `$PATH` if you want github action running/tracking


\* NB no legal warranty to this effect is implied or offered. Don't come for me if this thing calls `rm -rf .git`. (I'm fairly sure it won't...).