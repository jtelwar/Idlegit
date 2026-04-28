# idlegit

A small interactive multi-repo git tool. One screen for status, commit, and
push across every repo in a workspace.

## Why

Because clicking through a GUI git tool to commit-and-push the same set
of repos every day got old. Drop the `idlegit/` folder next to the repos you
want to manage and run it; everything is on one keyboard-driven screen.

And because the name lazygit was already taken, but it's just not lazy enough for me...

## Run

```sh
./idlegit/idlegit
```

By default it scans the parent folder of `idlegit/` for git repos: the
workspace itself if it is one, plus any immediate child folder containing
`.git`. Edit `idlegit.conf` if you want it to look elsewhere.

## Keys

| Key | Action |
|---|---|
| `↑` `↓` | Navigate rows |
| typing | Enter a per-row commit message |
| `←` / `→` / `Home` / `End` | Move the cursor inside the message field |
| `Left` on an empty row | Generate a single commit-message suggestion (in the background) |
| `Shift+Left` on an empty row | Generate suggestions for every dirty row with no message yet |
| `Tab` | Open the per-row action menu (fetch / pull / switch branch / soft reset / push) |
| `Shift+Tab` | Open the workspace-wide menu (Suggest all messages / Refresh all) |
| `Space` | Flip the focused toggle |
| `Enter` | Open the review screen, or kick off the work from review |
| `Ctrl+R` / `F5` | Prune completed tasks and re-fetch every repo (inline — no overlay) |
| `Ctrl+S` | Sync every tracked sibling submodule (fetch + checkout, no commits) |
| `Esc` | Close the topmost modal, clear the row's message, or quit (with confirmation if any messages are queued) |

## Highlights

- Per-repo status dot: clean / dirty / merging / ahead / behind / no-upstream / error.
- Merge / rebase / cherry-pick / revert state is detected up front; commits
  are skipped automatically with a clear note on the review screen.
- Tracked-repo nested submodules are listed indented under their parent with
  a sync-status dot (green if the parent's checkout matches the top-level
  HEAD, magenta if it doesn't). Subtrees can be declared in `idlegit.conf`
  and render the same way (with a `⊕` glyph instead of `↳`); `Ctrl+S` will
  `git subtree pull --squash` them in addition to fetching submodules.
- Submodule child rows are themselves editable — type a commit message on a
  `↳` row to commit + push from that nested checkout (handy when the change
  lives inside `Mobile/.../Domain.Models` rather than the top-level
  `Domain.Models/`). After a successful push, every other instance of that
  repo (top-level + sibling parents' nested copies) is automatically synced.
  Detached-HEAD nested checkouts are refused with a clear message — check
  out a branch in the submodule first.
- ≥100 MB files (configurable) are flagged on the review screen with a
  Space-to-toggle "add to `.gitattributes` via `git lfs track`" affordance,
  applied just before the commit.
- After a successful push, every other tracked repo that has the pushed repo
  as a nested submodule is fetched + checked out to the new commit, keeping
  all instances in step.
- Live task sidebar: every git operation runs in a background thread, so the
  UI stays responsive on slow stages or large pushes. Each step shows up on
  the right with an animated spinner while running, then ✓ / ✗ / ⚠ once it
  finishes. Completed entries stick around until you press `Ctrl+R`.

## Configuration

`idlegit.conf` next to the script. Inline comments (`;`) document every option.
Defaults are sensible — you can run with no config at all.

## Requirements

- Python 3.9+ (stdlib only: `configparser`, `curses`, `concurrent.futures`)
- `git` on `$PATH`
- `git-lfs` on `$PATH` if you want to use the LFS toggle on the review screen
