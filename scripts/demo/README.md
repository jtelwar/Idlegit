# Demo workspace + recording

Builds three sandboxed idlegit workspaces and drives a full demo
recording with `tmux send-keys` + `asciinema rec`. Nothing writes to
your real `~/.gitconfig`, `~/.ssh`, or `~/.config/idlegit` — all state
lives under `/tmp/idlegit-demo` and is wiped on teardown.

## Files

| File | Purpose |
|---|---|
| `demo-workspace.sh` | Builds bare repos, starts `git daemon`, registers three idlegit workspaces. Subcommands: `up`, `down`, `status`. |
| `record.sh` | Drives a fresh workspace through the full demo arc and saves an asciinema `.cast`. |

## Prerequisites

```sh
brew install tmux asciinema      # for scripted recording
brew install agg                 # optional: convert .cast → .gif
gh auth login                    # for the workspace 3 action trigger
```

## Run it

### Scripted recording (recommended)

```sh
scripts/demo/record.sh                       # writes ./demo.cast
CAST=takes/take2.cast scripts/demo/record.sh # alternate output path
```

While it's running, watch live from another terminal:

```sh
tmux attach -t idlegit-demo
```

### Manual recording (when you want a different keystroke sequence)

```sh
scripts/demo/demo-workspace.sh up
export HOME=/tmp/idlegit-demo/home
export GIT_CONFIG_GLOBAL="$HOME/.gitconfig"
export IDLEGIT_CONFIG_DIR="$HOME/.config/idlegit"
asciinema rec demo.cast -c "python idlegit.py"
# ...drive the demo by hand, then quit idlegit (Ctrl+C)...
scripts/demo/demo-workspace.sh down
```

## Output

`record.sh` writes `demo.cast` (asciinema v2 format, ~tens of KB).

| You want… | Do this |
|---|---|
| Preview locally | `asciinema play demo.cast` |
| Embed in a README | Upload to asciinema.org (`asciinema upload demo.cast`) or use [asciinema-player](https://github.com/asciinema/asciinema-player) |
| GIF for socials / HN thumbnail | `agg demo.cast demo.gif` |
| MP4 | `agg demo.cast demo.gif && ffmpeg -i demo.gif demo.mp4` |

## Workspaces

| Workspace | Active default | What's there |
|---|---|---|
| `commit-demo` | ✓ | Toolkit submodule shared by 3 apps. One app's submodule checkout is dirty; all three parents have unrelated dirty edits too. |
| `sync-demo` | | Three superprojects, each pinning the same `libs/core` at a different commit. The smart-sync money shot. |
| `idlegit` | | Real clone of `jtelwar/Idlegit` for the GitHub Action demo. |

## Demo arc (what `record.sh` does)

1. **Workspace 1, act 1** — focus the dirty submodule checkout, `←` suggest, `Enter Enter` commit + push. The parent's gitlink auto-pushes.
2. **Switch to workspace 2** — Up to the workspace row, Right cycles to `sync-demo`.
3. **Smart-sync** — `Ctrl+S`. Three different submodule pins converge to one winner; parent gitlinks cascade.
4. **Back to workspace 1** — `Shift+←` bulk suggest, `Enter Enter` commit all. Everything goes green.
5. **Workspace switcher** — `Enter` on the workspace row opens the picker; navigate to `idlegit`.
6. **GitHub Action** — `Tab` opens the action menu, pick "run a workflow", pick `Tests`, dispatch.
7. **Task panel** — `Shift+Tab` to focus the task panel, watch the action progress.
8. **Quit** — `Ctrl+C`.

## Tuning timings

Sleep durations in `record.sh` are best-guess; you'll iterate after
watching the first take. The bits most likely to need adjustment:

- **Long sleeps after triggers** — propagation after the first commit
  (6s), smart-sync convergence (10s), and the final task-panel dwell
  (12s). All taste-dependent.
- **Up-counts to reach the workspace row** — `Up Up Up Up Up Up Up Up
  Up Up` over-counts intentionally; harmless if the cursor is already
  at the top. If you add or remove repos and the cursor *doesn't*
  reach the row, increase further.
- **Action menu navigation** — `Down 3` is a guess. After the first
  take, count how many Downs actually land on "run a workflow…" and
  edit the script.

## Troubleshooting

- **`port 9418 already in use`** — another `git daemon` is running.
  `PORT=<other> scripts/demo/demo-workspace.sh up`.
- **Workspace 3 has no idlegit clone** — your `~/.config/gh` is either
  missing or not authed. Run `gh auth login`.
- **`tmux session 'idlegit-demo' already exists`** — previous run
  didn't clean up. Kill it: `tmux kill-session -t idlegit-demo`.

## What the sandbox protects

- `HOME=/tmp/idlegit-demo/home` — every tool that resolves config via
  `$HOME` (git, gh, idlegit) reads the sandbox copy.
- `GIT_CONFIG_GLOBAL` — bypasses your real `~/.gitconfig` even if
  something un-sets `HOME`.
- `GIT_CONFIG_SYSTEM=/dev/null` — ignores `/etc/gitconfig` too.
- `IDLEGIT_CONFIG_DIR` — points idlegit at the sandbox workspaces file.
- `gh` config is **copied** (not symlinked) so the demo can't write
  back into your real `~/.config/gh`.
