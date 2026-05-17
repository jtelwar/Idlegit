# Review screen

Pressing *Enter* on the main screen — when at least one row has a
commit message — opens the **review screen**. This is where you
confirm what's about to land before idlegit stages, commits, and
pushes.

## Two panes

- **Left pane** — one block per commit target (top-level repo or
  submodule child). Each block stacks: header, commit-message row,
  LFS warnings, push summary, workflow toggles, then-run selectors.
- **Right pane** — the file list + toolbar for whichever block has
  focus on the left. Files have per-file stage/unstage checkboxes.

*Shift+Tab* toggles which pane has focus. The hint footer changes
to reflect the focused pane.

## Staging

- Files default to "staged" when `auto_stage` is on (workspace
  default). The right-pane checkbox flips per-file.
- The toolbar above the file list has **stage all**, **unstage
  all**, and **amend** buttons. *↑* from the top file in the right
  pane lifts focus onto the toolbar.

## Workflow tracking

For each GitHub Actions workflow that would fire on push (parsed
from `.github/workflows/*.yml`), the block shows a tracking
checkbox. When ticked, idlegit polls the resulting workflow run
after push and surfaces it as a sub-task in the sidebar.

Default tick state is controlled by `track_actions_default` in the
workspace settings.

## Then-runs (chains)

Below the workflow toggles, each block carries one or more
**then-run selectors**: actions that fire once the push (or a
specific workflow run) completes.

- **After push** — fires once the push itself lands.
- **After `<workflow>`** — fires when the named tracked workflow
  run finishes. Requires the matching workflow's tracking
  checkbox to be ticked.

Selector options:

- **(none)** — no chained action.
- **`add tag`** — creates a lightweight tag at the just-pushed
  commit and pushes the tag. An inline parameter row appears below
  the selector for the tag name.
- **`<workflow_name>`** — dispatches a `workflow_dispatch` workflow.
  If the workflow declares `inputs`, parameter rows appear for each
  input. Empty values use the workflow's declared defaults.

*←* / *→* cycles a selector through its options.

## Lifecycle of then-run state

When you accept the review with *Enter*, the after-push state
(workflow tracking opt-ins, tag/dispatch target, parameter buffers)
is **snapshotted into the worker thread and cleared from the repo
immediately**. Re-opening the review for a new commit on the same
repo starts with an empty selector — the old gesture doesn't bleed
into the next one.

After-workflow chains are read by the workflow-tracking thread
*when the run completes* (possibly minutes later). They stay on the
repo until that read fires. *Ctrl+K* on the review screen is the
manual escape hatch — it wipes every repo's after-push *and*
after-workflow state in one shot. The hint only appears in the
footer when at least one repo has some chain set.

## Esc preserves, Enter commits

- *Esc* leaves all in-progress state alone — messages, staged-paths,
  then-run selectors, workflow toggles. You can navigate away,
  edit more code, and re-open the review to find everything as
  you left it.
- *Enter* (left pane) fires the commit pipeline. State is cleared
  per the rules above.

## Diff viewer

*Tab* on a focused file in the right pane opens a side-by-side diff
viewer. *Esc* or *Enter* close it back to the review.
