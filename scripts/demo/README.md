# Demo workspace + recording

Builds three sandboxed idlegit workspaces and drives a full demo recording
with [VHS](https://github.com/charmbracelet/vhs). Nothing in here writes
to your real `~/.gitconfig`, `~/.ssh`, or `~/.config/idlegit` — all state
lives under `/tmp/idlegit-demo` and is wiped on teardown.

## Files

| File | Purpose |
|---|---|
| `demo-workspace.sh` | Builds bare repos, starts `git daemon`, registers three idlegit workspaces. Subcommands: `up`, `down`, `status`. |
| `demo.tape` | VHS tape that runs `demo-workspace.sh up`, drives the TUI through the full demo arc, then tears down. |

## Prerequisites

```sh
brew install vhs       # only needed for scripted recording
gh auth login          # only needed for the workspace 3 action trigger
```

## Run it

### Manual recording (paste and demo by hand)

```sh
scripts/demo/demo-workspace.sh up
export HOME=/tmp/idlegit-demo/home
export GIT_CONFIG_GLOBAL="$HOME/.gitconfig"
export IDLEGIT_CONFIG_DIR="$HOME/.config/idlegit"
python idlegit.py
# ... record with asciinema / OBS / etc ...
scripts/demo/demo-workspace.sh down
```

### Scripted recording (VHS)

```sh
vhs scripts/demo/demo.tape
```

Self-contained: the tape calls `demo-workspace.sh up` and `down` itself, so each
invocation is hermetic.

## Output

Both produced in the repo root:

| File | Use |
|---|---|
| `demo.gif` | README hero / inline preview. |
| `demo.mp4` | Social posts (smaller than GIF, plays everywhere). |

Render times are roughly the demo duration (~90s) plus a few seconds
for encoding.

### What about an asciinema `.cast`?

VHS only renders to `gif`/`mp4`/`webm`/PNG-frames — there's no
asciinema output. If you need a `.cast` (for the
[asciinema-player](https://github.com/asciinema/asciinema-player)
README embed), record it manually using the same sandbox:

```sh
scripts/demo/demo-workspace.sh up
export HOME=/tmp/idlegit-demo/home
export GIT_CONFIG_GLOBAL="$HOME/.gitconfig"
export IDLEGIT_CONFIG_DIR="$HOME/.config/idlegit"
asciinema rec demo.cast -c "python idlegit.py"
# ...run through the demo arc by hand, then quit idlegit...
scripts/demo/demo-workspace.sh down
```

## Workspaces

| Workspace | Active default | What's there |
|---|---|---|
| `commit-demo` | ✓ | Toolkit submodule shared by 3 apps. One app's submodule checkout is dirty; all three parents have unrelated dirty edits too. |
| `sync-demo` | | Three superprojects, each pinning the same `libs/core` at a different commit. The smart-sync money shot. |
| `idlegit` | | Real clone of `jtelwar/Idlegit` for the GitHub Action demo. |

## Demo arc (what `demo.tape` does)

1. **Workspace 1, act 1** — focus the dirty submodule checkout, `←` suggest commit message, `Enter Enter` to review + commit + push. Watch the parent's gitlink auto-push.
2. **Switch to workspace 2** — `↑` to the workspace row, `→` to `sync-demo`.
3. **Smart-sync** — `Ctrl+S`. Three different submodule pins converge to one winner; parent gitlinks cascade.
4. **Back to workspace 1** — `Shift+←` suggest-all on remaining dirty, `Enter Enter` to commit all. Everything goes green.
5. **Workspace switcher** — `Enter` on the workspace row opens the picker, navigate to `idlegit`.
6. **GitHub Action** — `Tab` opens the action menu, pick "run a workflow", pick `Tests`, dispatch.
7. **Task panel** — `Shift+Tab` to focus the task panel, watch the action progress.
8. **Quit** — `Ctrl+C`.

## Tuning the tape

Lines tagged `# TUNE` in `demo.tape` are the ones most likely to need
adjustment after watching the first render:

- **`Up 10`** — over-counts intentionally; harmless if cursor is already
  at the top. Change if you add/remove repos and want pixel-precise
  scrolling.
- **`Down N`** in the action menu — depends on menu order; check once
  with the live menu open.
- **`Sleep`s** after long-running ops (smart-sync, action dispatch) —
  adjust to taste depending on how much dwell time you want.

## Troubleshooting

- **`port 9418 already in use`** — another `git daemon` is running. Set
  `PORT=<other>` before invoking.
- **Workspace 3 has no idlegit clone** — your `~/.config/gh` either
  doesn't exist or isn't authed for `github.com`. Run `gh auth login`.
- **Demo daemon won't die** — `scripts/demo/demo-workspace.sh down` always tries to
  clean up. If something is wedged, the pidfile is at
  `/tmp/idlegit-demo/daemon.pid`.

## What the sandbox protects

- `HOME=/tmp/idlegit-demo/home` — every tool that resolves config via
  `$HOME` (git, gh, idlegit) reads from the sandbox copy.
- `GIT_CONFIG_GLOBAL` — bypasses your real `~/.gitconfig` even if
  something un-sets `HOME`.
- `GIT_CONFIG_SYSTEM=/dev/null` — ignores `/etc/gitconfig` too.
- `IDLEGIT_CONFIG_DIR` — points idlegit at the sandbox workspaces file.
- `gh` config is **copied** (not symlinked) so the demo can't write back
  into your real `~/.config/gh`.
