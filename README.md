# Idlegit

An interactive multi-repo git tool. One screen for status of the whole workspace, switch workspaces easily.

Suggest messages, stage, commit, and push across every repo. Track and chain the resulting actions. 

No destructive git operations allowed.

## Why

Because clicking through a painfully inefficient GUI git tool to commit-and-push the same set of repos every day, then navigating to github to track their actions, got old. 

And because the name lazygit was already taken, but it's just not lazy enough for me...

## Run

Clone or copy the repository and run the launcher from the root; everything is on one
keyboard-driven screen.

```sh
./idlegit
```

On first launch it pops a setup dialog asking you to type one or more
folder paths to scan; each one becomes a named workspace you can switch
between at runtime (Up to the title row, ←/→ to cycle, Space to edit
that workspace's overrides). Settings live in a per-user directory:
**macOS** `~/Library/Application Support/idlegit/`, **Windows**
`%APPDATA%\idlegit\`, **Linux** `$XDG_CONFIG_HOME/idlegit` or
`~/.config/idlegit/` (`idlegit.conf` and `idlegit.workspaces`). Override
with `IDLEGIT_CONFIG_DIR` if needed. If you previously kept those files next
to the app, they are copied into the new location on first run.
Relative `folders` paths are resolved against the directory containing
`idlegit.workspaces` (not the install path); prefer absolute paths if you
move machines.

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

`idlegit.conf` in the per-user directory (see **Run** above). Inline comments (`;`) document every option.
Defaults are sensible — you can run with no config at all.

## Requirements

- Python 3.9+ (stdlib only: `configparser`, `curses`, `concurrent.futures`)
- `git` on `$PATH`
- `git-lfs` on `$PATH` if you want to use the LFS toggle on the review screen
- `gh` on `$PATH` if you want github action running/tracking


## License

Idlegit is licensed under the 3-clause BSD License. Contributions are welcome.

\* NB no legal warranty to this effect is implied or offered. Don't come for me if this thing calls `rm -rf .git`. (Pretty sure I checked for that...).