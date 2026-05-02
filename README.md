# idlegit

A small interactive multi-repo git tool. One screen for status, commit, and
push across every repo in a workspace.

## Why

Because clicking through a GUI git tool to commit-and-push the same set
of repos every day got old. 

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

- Frictionless 
- Completely non-destructive
- Opinionated but configurable

## Features

- Two-keypress staging, committing, pushing, github action tracking and chaining
- Smart-sync submodules across monitored repos
- Does everything heavy with background workers
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