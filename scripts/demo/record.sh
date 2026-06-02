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
#   tmux -L idlegit-demo attach

set -euo pipefail

SESSION=idlegit-demo
TMUX_SOCKET=idlegit-demo            # isolated -L socket so we don't touch
                                    # the user's normal tmux server
CAST="${CAST:-demo.cast}"
COLS="${COLS:-160}"
ROWS="${ROWS:-44}"
WORKSPACE_STARTED=0
TMUX_CONF=""

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKSPACE_SH="$REPO_ROOT/scripts/demo/demo-workspace.sh"
CAST_PATH="$(cd "$(dirname "$CAST")" 2>/dev/null && pwd)/$(basename "$CAST")"

# Always operate on our isolated socket so a) the user's other tmux
# sessions aren't disturbed, b) `has-session` and `kill-session` can't
# collide with whatever they have running.
T() { tmux -L "$TMUX_SOCKET" "$@"; }

# ---- output helpers --------------------------------------------------------
say() { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# ---- tmux send-keys wrapper ------------------------------------------------
# `send K1 K2 ...` — each arg is a tmux key spec (Down, Enter, C-s, M-s,
# BTab, etc.) or a literal string. Multiple args go to one tmux call so
# the keys queue without any inter-key delay (the only delay is what we
# explicitly Sleep for).
send() { T send-keys -t "$SESSION" "$@"; }

# ---- cleanup ---------------------------------------------------------------
cleanup() {
  if T has-session -t "$SESSION" 2>/dev/null; then
    T send-keys -t "$SESSION" C-c 2>/dev/null || true
    sleep 1
    T kill-session -t "$SESSION" 2>/dev/null || true
  fi
  # The -L socket has its own server; nuke it too so we leave nothing
  # behind for the next run.
  T kill-server 2>/dev/null || true
  [ -n "$TMUX_CONF" ] && rm -f "$TMUX_CONF"
  if [ "$WORKSPACE_STARTED" = "1" ]; then
    bash "$WORKSPACE_SH" down >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# ---- preflight -------------------------------------------------------------
command -v tmux      >/dev/null || die "tmux not on PATH    (brew install tmux)"
command -v asciinema >/dev/null || die "asciinema not on PATH (brew install asciinema)"

if T has-session -t "$SESSION" 2>/dev/null; then
  die "tmux session '$SESSION' already exists on the -L $TMUX_SOCKET socket — kill it: tmux -L $TMUX_SOCKET kill-server"
fi

# ---- tmux config: make the pane look like iTerm ----------------------------
# Default tmux advertises `screen-256color`, which strips truecolor and
# loses some capabilities idlegit relies on. Force `xterm-256color` and
# pass RGB/truecolor through so the recorded escape stream matches what
# iTerm would emit. Also hide tmux's status bar — it's noise in the cast.
TMUX_CONF="$(mktemp)"
cat >"$TMUX_CONF" <<'EOF'
set -g default-terminal "xterm-256color"
set -ga terminal-overrides ",*256col*:Tc"
set -g status off
EOF

# ---- build the sandboxed workspace -----------------------------------------
say "Setting up sandboxed demo workspaces…"
bash "$WORKSPACE_SH" down >/dev/null 2>&1 || true
bash "$WORKSPACE_SH" up >/dev/null
WORKSPACE_STARTED=1
ok "Workspaces ready under /tmp/idlegit-demo"

# ---- launch tmux + asciinema + idlegit -------------------------------------
say "Launching idlegit inside asciinema (cast → $CAST_PATH)"
say "Live-watch: tmux -L $TMUX_SOCKET attach -t $SESSION"

# Sandbox env vars set inside the pane so they only apply to the demo.
# COLORTERM=truecolor is what most modern TUIs check before emitting 24-bit
# color; setting it explicitly avoids depending on tmux propagating it.
read -r -d '' DEMO_CMD <<EOF || true
export TERM=xterm-256color
export COLORTERM=truecolor
export HOME=/tmp/idlegit-demo/home
export GIT_CONFIG_GLOBAL=\$HOME/.gitconfig
export IDLEGIT_CONFIG_DIR=\$HOME/.config/idlegit
cd $REPO_ROOT
exec asciinema rec --overwrite --quiet -c 'python idlegit.py' '$CAST_PATH'
EOF

T -f "$TMUX_CONF" new-session -d -s "$SESSION" -x "$COLS" -y "$ROWS" \
  "bash -lc \"$DEMO_CMD\""

# Give asciinema + idlegit time to start drawing.
sleep 6

# ─── workspace 1, act 1: commit the dirty submodule ─────────────────────────
say "Workspace 1 — suggest + commit + push the dirty submodule"
send Down;          sleep 1.5                 # parent → libs/toolkit child
send Left;          sleep 2.5                 # suggest
send Enter;         sleep 1.5                 # review modal
send Enter;         sleep 6                   # commit + propagate

# ─── switch to workspace 2 ──────────────────────────────────────────────────
say "Switching to workspace 2 (sync-demo)"
send Up Up Up Up Up Up Up Up Up Up;   sleep 0.5
send Right;         sleep 3

# ─── smart-sync ─────────────────────────────────────────────────────────────
say "Smart-sync across three different submodule pins"
send C-s;           sleep 10

# ─── back to workspace 1 ────────────────────────────────────────────────────
# After Left cycles workspaces, cursor is still on the workspace row.
# `Down` lands us back on a repo so the bulk-suggest action has a target
# message holder. M-s is our Alt+S alternate for the bulk-suggest key —
# Shift+Left over tmux's TERM is unreliable (curses doesn't always decode
# the escape sequence as KEY_SLEFT), and our read_key wrapper would then
# treat the bare ESC as a quit signal.
say "Back to workspace 1 — bulk suggest + commit remaining repos"
send Up Up Up Up Up Up Up Up Up Up;   sleep 0.5
send Left;          sleep 2.5
send Down;          sleep 0.5                 # workspace row → first repo
send M-s;           sleep 4                   # bulk-suggest (Alt+S)
send Enter;         sleep 1.5
send Enter;         sleep 6

# ─── switch to workspace 3 via switcher modal ───────────────────────────────
say "Opening workspace switcher → idlegit"
send Up Up Up Up Up Up Up Up Up Up;   sleep 0.5
send Enter;         sleep 1.5                 # opens picker modal
send Down Down;     sleep 0.5                 # commit-demo → sync-demo → idlegit
send Enter;         sleep 3

# ─── trigger Tests workflow ─────────────────────────────────────────────────
say "Triggering Tests workflow on the idlegit repo"
send Tab;           sleep 1.5                 # action menu
send Down Down Down; sleep 0.5                # navigate to "run a workflow…"
send Enter;         sleep 2                   # workflow picker opens
send Enter;         sleep 2.5                 # dispatch first workflow

# ─── focus task panel and dwell ─────────────────────────────────────────────
say "Watching the run in the task panel"
send BTab;          sleep 12

# ─── quit idlegit (asciinema closes with it) ────────────────────────────────
send C-c
sleep 2

ok "Cast saved → $CAST_PATH"
printf '\nPreview:        asciinema play %s\n' "$CAST_PATH"
printf 'Convert to GIF: agg %s demo.gif    # brew install agg\n' "$CAST_PATH"
