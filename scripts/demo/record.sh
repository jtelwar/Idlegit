#!/usr/bin/env bash
# Record an idlegit demo to an asciinema .cast file.
#
# Drives a fresh demo workspace through the full demo arc using
# `tmux send-keys` while `asciinema rec` captures the pane. The keystroke
# sequence below is the canonical demo; comments split it into the
# narrative sections you'd describe to a viewer.
#
# Prerequisites:
#   brew install tmux asciinema
#   gh auth login                       # for the workspace 3 action trigger
#
# Run from anywhere:
#   scripts/demo/record.sh              # produces ./demo.cast
#   CAST=takes/take3.cast scripts/demo/record.sh   # alternate output
#
# Live-watch the recording in another terminal:
#   tmux attach -t idlegit-demo

set -euo pipefail

SESSION=idlegit-demo
CAST="${CAST:-demo.cast}"
COLS="${COLS:-200}"
ROWS="${ROWS:-50}"
WORKSPACE_STARTED=0

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKSPACE_SH="$REPO_ROOT/scripts/demo/demo-workspace.sh"
CAST_PATH="$(cd "$(dirname "$CAST")" 2>/dev/null && pwd)/$(basename "$CAST")"

# ---- output helpers --------------------------------------------------------
say() { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# ---- tmux send-keys wrapper ------------------------------------------------
# `send K1 K2 ...` — each arg is a tmux key spec (Down, Enter, C-s, S-Left,
# M-s, BTab, etc.) or a literal string. Multiple args go to one tmux call
# so the keys queue without any inter-key delay (the only delay is what
# we explicitly Sleep for).
send() { tmux send-keys -t "$SESSION" "$@"; }

# ---- cleanup ---------------------------------------------------------------
cleanup() {
  # If asciinema/idlegit are still running inside tmux, give them a clean
  # Ctrl+C so the cast file gets finalized before tmux kills the pane.
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux send-keys -t "$SESSION" C-c 2>/dev/null || true
    sleep 1
    tmux kill-session -t "$SESSION" 2>/dev/null || true
  fi
  if [ "$WORKSPACE_STARTED" = "1" ]; then
    bash "$WORKSPACE_SH" down >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# ---- preflight -------------------------------------------------------------
command -v tmux      >/dev/null || die "tmux not on PATH    (brew install tmux)"
command -v asciinema >/dev/null || die "asciinema not on PATH (brew install asciinema)"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  die "tmux session '$SESSION' already exists — kill it first: tmux kill-session -t $SESSION"
fi

# ---- build the sandboxed workspace -----------------------------------------
say "Setting up sandboxed demo workspaces…"
bash "$WORKSPACE_SH" down >/dev/null 2>&1 || true
bash "$WORKSPACE_SH" up >/dev/null
WORKSPACE_STARTED=1
ok "Workspaces ready under /tmp/idlegit-demo"

# ---- launch tmux + asciinema + idlegit -------------------------------------
say "Launching idlegit inside asciinema (cast → $CAST_PATH)"
say "Tip: open another terminal and run \`tmux attach -t $SESSION\` to watch live."

# The sandbox env vars (HOME, GIT_CONFIG_GLOBAL, IDLEGIT_CONFIG_DIR) need
# to be present for the python idlegit.py invocation. We set them in the
# tmux session's shell command so they only apply inside the demo pane.
read -r -d '' DEMO_CMD <<EOF || true
export HOME=/tmp/idlegit-demo/home
export GIT_CONFIG_GLOBAL=\$HOME/.gitconfig
export IDLEGIT_CONFIG_DIR=\$HOME/.config/idlegit
cd $REPO_ROOT
exec asciinema rec --overwrite --quiet -c 'python idlegit.py' '$CAST_PATH'
EOF

tmux new-session -d -s "$SESSION" -x "$COLS" -y "$ROWS" "bash -lc \"$DEMO_CMD\""

# Give asciinema + idlegit time to start drawing.
sleep 6

# ─── workspace 1, act 1: commit the dirty submodule ─────────────────────────
say "Workspace 1 — suggest + commit + push the dirty submodule"
send Down;          sleep 1.5
send Left;          sleep 2.5
send Enter;         sleep 1.5
send Enter;         sleep 6           # propagation: lib push + parent auto-push

# ─── switch to workspace 2 ──────────────────────────────────────────────────
say "Switching to workspace 2 (sync-demo)"
send Up Up Up Up Up Up Up Up Up Up;   sleep 0.5
send Right;         sleep 3

# ─── smart-sync ─────────────────────────────────────────────────────────────
say "Smart-sync across three different submodule pins"
send C-s;           sleep 10

# ─── back to workspace 1 ────────────────────────────────────────────────────
say "Back to workspace 1 — bulk suggest + commit remaining repos"
send Up Up Up Up Up Up Up Up Up Up;   sleep 0.5
send Left;          sleep 2.5
send S-Left;        sleep 4            # bulk-suggest. M-s would also work.
send Enter;         sleep 1.5
send Enter;         sleep 6

# ─── switch to workspace 3 via switcher modal ───────────────────────────────
say "Opening workspace switcher → idlegit"
send Up Up Up Up Up Up Up Up Up Up;   sleep 0.5
send Enter;         sleep 1.5         # opens picker modal
send Down Down;     sleep 0.5         # commit-demo → sync-demo → idlegit
send Enter;         sleep 3

# ─── trigger Tests workflow ─────────────────────────────────────────────────
say "Triggering Tests workflow on the idlegit repo"
send Tab;           sleep 1.5         # action menu
send Down Down Down; sleep 0.5        # navigate to "run a workflow…"
send Enter;         sleep 2           # workflow picker opens
send Enter;         sleep 2.5         # dispatch first workflow

# ─── focus task panel and dwell ─────────────────────────────────────────────
say "Watching the run in the task panel"
send BTab;          sleep 12

# ─── quit idlegit (asciinema closes with it) ────────────────────────────────
send C-c
sleep 2

ok "Cast saved → $CAST_PATH"
printf '\nPreview:        asciinema play %s\n' "$CAST_PATH"
printf 'Convert to GIF: agg %s demo.gif    # brew install agg\n' "$CAST_PATH"
