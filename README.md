# Idlegit

[![tests](https://github.com/jtelwar/idlegit/actions/workflows/tests.yml/badge.svg)](https://github.com/jtelwar/idlegit/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python: 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

An interactive multi-repo git tool. One screen for the status of the
whole workspace, switch workspaces easily, suggest messages, stage,
commit, and push across every repo. Track and chain the resulting
GitHub Actions.

**No destructive git operations allowed.**

## Why

Because clicking through a painfully inefficient GUI git tool to
commit-and-push the same set of repos every day, then navigating to
GitHub to track their actions, got old.

And because the name `lazygit` was already taken, but it's just not
lazy enough for me...

## Design goals

   Safe · Frictionless · Opinionated · Configurable

## Features

- Two-keypress staging, committing, pushing
- GitHub action running, tracking, chaining
- Completely non-destructive\*
- Auto-writes commit messages in an instant
- Smart-syncs submodules across a workspace
- Background workers; every operation tracked in a side panel
- Small footprint, light on resources
- In-app help

## Install

### macOS / Linux

```sh
git clone https://github.com/jtelwar/idlegit.git
cd idlegit
./install
idlegit
```

### Windows

```powershell
git clone https://github.com/jtelwar/idlegit.git
cd idlegit
pipx install ".[windows]"
idlegit
```

On Windows, update with `git pull && pipx reinstall .`. The bash
`./install` and `idlegit-update` scripts are Unix-only. If a non-UTF-8
locale (e.g. `LANG=C`) makes the Braille spinner render as tofu, set
`IDLEGIT_ASCII_GLYPHS=1` for the ASCII fallback.

## Update

```sh
idlegit-update     # check + prompt, install if available
```

Or in-app: `Tab` on the title row → **Check for updates** →
**Update now**. Pass `--help` for all flags.

## Requirements

- Python 3.9+
- `git` on `$PATH`
- Optional: `gh` (GitHub Actions panel), `git-lfs` (LFS toggle on the
  review screen)
- Runtime Python deps:
  [`wcwidth`](https://pypi.org/project/wcwidth/) (terminal cell
  widths), [`watchdog`](https://pypi.org/project/watchdog/) (fs
  auto-refresh),
  [`pathspec`](https://pypi.org/project/pathspec/) (gitignore-style
  fs-watch ignore patterns). On Windows the `[windows]` extra also
  pulls `windows-curses`.

## Configuration + getting started

Once installed, press `Tab` on the title row → **Help** for keyboard
shortcuts, configuration, smart-sync details, and where the per-user
config files live. `idlegit.conf` ships with inline `;` comments
documenting every option.

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for dev
setup, rules, and the PR workflow. Security issues: please follow
[SECURITY.md](SECURITY.md) rather than filing a public issue.

## License

Idlegit is licensed under the [MIT License](LICENSE).

---

\* NB no legal warranty to this effect is implied or offered. Don't come for me if this thing calls `rm -rf .git`.  _(Pretty sure I checked for that...)_
