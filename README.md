# Idlegit

An interactive multi-repo git tool. One screen for status of the whole workspace, switch workspaces easily.

Suggest messages, stage, commit, and push across every repo. Track and chain the resulting actions. 

No destructive git operations allowed.

## Why

Because clicking through a painfully inefficient GUI git tool to commit-and-push the same set of repos every day, then navigating to github to track their actions, got old. 

And because the name lazygit was already taken, but it's just not lazy enough for me...

## Install

### macOS / Linux

Clone the repo and run `./install` from the root:

```sh
git clone https://github.com/jtelwar/idlegit.git
cd idlegit
./install
```

The installer copies the app into a stable home directory and
symlinks the launcher onto your `$PATH` so you can invoke it as
`idlegit` from anywhere. It also drops `idlegit-update` next to it
for in-place upgrades. If your bindir isn't already on `$PATH` it
asks before editing your shell profile; pass `-y` to accept the
prompts non-interactively.

### Windows

Install with [pipx](https://pipx.pypa.io/) into Windows Terminal
(the legacy `conhost.exe` works but isn't recommended):

```powershell
git clone https://github.com/jtelwar/idlegit.git
cd idlegit
pipx install ".[windows]"
```

The `[windows]` extra pulls in `windows-curses`, which Python on
Windows needs to render the curses UI. The bash `./install` script
and the `idlegit-update` updater are Unix-only; on Windows, update
with `git pull && pipx reinstall .`.

If a non-UTF-8 locale (e.g. `LANG=C`) makes the Braille spinner
render as tofu, set `IDLEGIT_ASCII_GLYPHS=1` to force the ASCII
fallback.

Defaults can be overridden with environment variables:

| Variable             | What it controls                                  |
| -------------------- | ------------------------------------------------- |
| `IDLEGIT_HOME`       | App directory (where files are copied to)         |
| `IDLEGIT_BINDIR`     | Directory the `idlegit` symlink lands in          |
| `IDLEGIT_CONFIG_DIR` | User config directory (see **Run** below)         |

If you'd rather not install, you can run directly from the cloned
tree with `./idlegit` — the install step is only there to put it on
`$PATH` and wire up `idlegit-update`.

## Run

```sh
idlegit
```

Everything is on one keyboard-driven screen. On first launch a
setup dialog asks for one or more folder paths to scan; each path
becomes a named workspace you can switch between at runtime.

Settings live in a per-user directory:

- **macOS:** `~/Library/Application Support/idlegit/`
- **Windows:** `%APPDATA%\idlegit\`
- **Linux:** `$XDG_CONFIG_HOME/idlegit` or `~/.config/idlegit/`

Two files: `idlegit.conf` (defaults) and `idlegit.workspaces`
(folder lists + per-workspace overrides). Override the directory
with `IDLEGIT_CONFIG_DIR` if you need to. If you previously kept
those files next to the app, they're copied into the new location
on first run. Relative `folders` paths are resolved against the
directory containing `idlegit.workspaces` (not the install path);
prefer absolute paths if you move machines.

## Update

In-app: press `Tab` on the title row to open the menu, then
`Enter` on **Check for updates** — and **Update now** when one is
offered. The app exits cleanly and re-execs the updater.

From a shell:

```sh
idlegit-update          # check, then prompt before installing
idlegit-update -y       # check + install without prompting
idlegit-update --check  # just print whether an update is available
idlegit-update --force  # reinstall the latest tag even if already current
```

The updater downloads the latest release tarball, extracts it to a
temp dir, and re-runs `install` against that source — your config
in `IDLEGIT_CONFIG_DIR` is left alone (only new keys are merged in).

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

- Python 3.9+
- [`wcwidth`](https://pypi.org/project/wcwidth/) — the one runtime
  dependency; falls back to `len()`-based width if missing, with mild
  layout drift on CJK / wide-char repo names
- `git` on `$PATH`
- `git-lfs` on `$PATH` if you want to use the LFS toggle on the review screen
- `gh` on `$PATH` if you want github action running/tracking
- On Windows: `windows-curses` (the `[windows]` extra installs it)


## License

Idlegit is licensed under the 3-clause BSD License. Contributions are welcome.

\* NB no legal warranty to this effect is implied or offered. Don't come for me if this thing calls `rm -rf .git`. (Pretty sure I checked for that...).