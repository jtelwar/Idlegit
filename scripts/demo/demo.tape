# Idlegit demo — VHS tape file.
#
# Prerequisites:
#   brew install vhs                    # https://github.com/charmbracelet/vhs
#   gh auth login                       # for the workspace 3 action trigger
#
# Run from repo root:
#   vhs scripts/demo/demo.tape
#
# Produces ./demo.gif and ./demo.mp4 — embed the gif in the README, use
# the mp4 for social (HN / X / Bluesky / etc.). VHS doesn't output
# asciinema `.cast`; if you want one, re-record manually with
# `asciinema rec` using the same demo-workspace.sh setup.
#
# Timing-sensitive sections are marked with `# TUNE` — re-time these
# after the first render. The Up-count to reach the workspace row is
# the most likely thing to need adjustment if you add or remove repos.

Output demo.gif
Output demo.mp4

Set Width 1400
Set Height 900
Set FontSize 14
Set Theme "Catppuccin Mocha"
Set TypingSpeed 30ms
Set Padding 20

# ─── setup (hidden) ────────────────────────────────────────────────────────
Hide
Type "scripts/demo/demo-workspace.sh down >/dev/null 2>&1; scripts/demo/demo-workspace.sh up >/dev/null 2>&1"
Enter
Sleep 25s
Type "export HOME=/tmp/idlegit-demo/home"
Enter
Type 'export GIT_CONFIG_GLOBAL="$HOME/.gitconfig"'
Enter
Type 'export IDLEGIT_CONFIG_DIR="$HOME/.config/idlegit"'
Enter
Type "clear"
Enter
Show

# ─── launch idlegit ────────────────────────────────────────────────────────
Type "python idlegit.py"
Enter
Sleep 4s

# Lands in workspace `commit-demo` (active by default per demo-workspace.sh).
# Rows: workspace switcher / nimbus-cms (dirty + dirty child) /
#       libs/toolkit (dirty child) / nimbus-portal / nimbus-shop.

# ─── workspace 1, act 1: commit the dirty submodule ────────────────────────
# Cursor starts on nimbus-cms (first repo). Step into the dirty child.
Down
Sleep 1500ms

# Suggest a commit message (pure-logic — no LLM). The hint bar shows
# this as `←  suggest` when the message field is empty.
Left
Sleep 2500ms

# Review modal: Enter to open, Enter again to fire commit+push.
Enter
Sleep 1500ms
Enter
Sleep 6s        # TUNE: propagation — toolkit push, parent gitlink auto-push

# ─── switch to workspace 2 (sync-demo) ─────────────────────────────────────
# Spam Up to reach the workspace switcher row; over-presses no-op.
Up 10
Sleep 500ms
Right           # cycle: commit-demo → sync-demo
Sleep 3s

# ─── smart-sync across three different submodule pins ──────────────────────
Ctrl+S
Sleep 10s       # TUNE: watch convergence — adjust to taste

# ─── back to workspace 1, finish the rest ──────────────────────────────────
Up 10
Sleep 500ms
Left            # cycle: sync-demo → commit-demo
Sleep 2500ms

# Suggest commit messages for ALL remaining dirty repos.
# VHS can't emit Shift+Arrow, so idlegit accepts Alt+S as an alternate
# binding (Alt+M is the same for the large commit-message editor).
# Quotes are required: VHS's lexer treats bare `s` and `m` as the time
# units SECONDS / MINUTES, so they have to be passed as STRING tokens.
Alt+"s"
Sleep 4s        # TUNE: lets all suggestions populate

Enter
Sleep 1500ms
Enter
Sleep 6s        # TUNE: everything pushes; rows turn green

# ─── switch to workspace 3 via switcher modal ──────────────────────────────
Up 10
Sleep 500ms
Enter           # opens the full workspace picker
Sleep 1500ms

# Picker order matches idlegit.workspaces write order: commit-demo,
# sync-demo, idlegit. Active workspace may or may not appear — TUNE
# the Down count after the first render.
Down 2          # TUNE
Sleep 500ms
Enter
Sleep 3s

# ─── trigger the Tests action on idlegit ───────────────────────────────────
# Cursor on the only repo in workspace 3. Tab → action menu.
Tab
Sleep 1500ms

# Navigate to "run a workflow…". Position depends on menu length —
# TUNE this Down count once you see the menu.
Down 3          # TUNE
Sleep 500ms
Enter
Sleep 2s

# Workflow picker. Assumes Tests is first (or near it). TUNE.
Enter
Sleep 2500ms

# ─── focus task panel, watch the run ───────────────────────────────────────
Shift+Tab
Sleep 12s       # TUNE: how long you want to dwell on the running action

# ─── exit ──────────────────────────────────────────────────────────────────
Ctrl+C
Sleep 1500ms

# ─── teardown (hidden) ─────────────────────────────────────────────────────
Hide
Type "scripts/demo/demo-workspace.sh down >/dev/null 2>&1"
Enter
Sleep 2s
