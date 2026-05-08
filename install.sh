#!/usr/bin/env bash
# Interactive installer for idlegit: checks dependencies, installs the app
# bundle under IDLEGIT_HOME, symlinks the launcher into IDLEGIT_BINDIR, and
# merges new idlegit.conf keys into the user's config (see install_merge_config.py).
#
# Non-interactive optional deps: pass -y or set IDLEGIT_INSTALL_ASSUME_YES=1
# (required checks still abort on failure).
#
# Environment (optional):
#   IDLEGIT_HOME                      App directory (default: $XDG_DATA_HOME/idlegit or ~/.local/share/idlegit)
#   IDLEGIT_BINDIR                    Symlink directory (default: ~/.local/bin)
#   IDLEGIT_CONFIG_DIR                Same as runtime: overrides user config directory for merge
#   IDLEGIT_INSTALL_ASSUME_YES=1      Do not prompt on optional dependency warnings
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IDLEGIT_HOME="${IDLEGIT_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/idlegit}"
IDLEGIT_BINDIR="${IDLEGIT_BINDIR:-$HOME/.local/bin}"
ASSUME_YES="${IDLEGIT_INSTALL_ASSUME_YES:-}"

for _arg in "$@"; do
  case "$_arg" in
    -y | --yes) ASSUME_YES=1 ;;
  esac
done

LOG_PREFIX="[install]"
SUMMARY=()

log() {
  printf '%s %s\n' "$LOG_PREFIX" "$*"
}

die() {
  printf '%s ERROR: %s\n' "$LOG_PREFIX" "$*" >&2
  exit 1
}

summarize() {
  SUMMARY+=("$1")
}

prompt_continue() {
  local _msg="$1"
  if [[ -n "$ASSUME_YES" ]]; then
    log "${_msg} (assuming yes, non-interactive)"
    return 0
  fi
  local _ans
  read -r -p "${LOG_PREFIX} ${_msg} [Y/n] " _ans || true
  case "${_ans:-y}" in
    [Nn] | [Nn][Oo]) return 1 ;;
    *) return 0 ;;
  esac
}

# --- Dependency: repository layout -------------------------------------------------
log "Checking repository layout…"
require_file() {
  local path="$1"
  if [[ ! -f "$ROOT/$path" ]]; then
    die "missing \"$path\" — run install.sh from the idlegit repo root (got ROOT=$ROOT)"
  fi
}
require_file idlegit
require_file idlegit.py
require_file install_merge_config.py
require_file idlegit.conf
require_file config.py
require_file models.py
require_file git_ops.py
require_file workers.py
if [[ ! -d "$ROOT/ui" ]]; then
  die "missing ui/ — run install.sh from the idlegit repo root"
fi
log "  repository: ok"
summarize "Verified repo layout under $ROOT"

# --- Dependency: Python + curses (required) ----------------------------------------
log "Checking Python (3.9+ with curses)…"
PY=""
if command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PY="$(command -v python)"
else
  log "  no python3/python on PATH."
  echo "       • macOS:  brew install python3   or  https://www.python.org/downloads/"
  echo "       • Debian/Ubuntu:  sudo apt install python3"
  echo "       • Fedora:  sudo dnf install python3"
  die "Python is required."
fi
if ! "$PY" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)" 2>/dev/null; then
  die "need Python 3.9 or newer (found: $("$PY" -V 2>&1))"
fi
if ! "$PY" -c "import curses" 2>/dev/null; then
  log "  the interpreter cannot import curses (required for the terminal UI)."
  echo "       • Use the official Python installer or your OS package \"python3-curses\" / full python3."
  die "curses module is required."
fi
log "  Python: ok ($("$PY" -V 2>&1), $PY)"
summarize "Python OK: $("$PY" -V 2>&1)"

# --- Optional: git -------------------------------------------------------------------
log "Checking git (needed to use idlegit)…"
if command -v git >/dev/null 2>&1; then
  log "  git: ok ($(command -v git))"
  summarize "git present"
else
  log "  git not found."
  echo "       • macOS:  xcode-select --install   or   brew install git"
  echo "       • Debian/Ubuntu:  sudo apt install git"
  if ! prompt_continue "Install idlegit anyway (without git on PATH)?"; then
    die "Aborted."
  fi
  summarize "Optional: git missing — install before running idlegit"
fi

# --- Optional: GitHub CLI ------------------------------------------------------------
log "Checking gh (optional — GitHub Actions panel)…"
if command -v gh >/dev/null 2>&1; then
  log "  gh: ok ($(command -v gh))"
  summarize "gh present"
else
  log "  gh not on PATH — workflow tracking in the review screen will stay inactive."
  echo "       • https://cli.github.com/  or  brew install gh"
  if ! prompt_continue "Continue without gh?"; then
    die "Aborted."
  fi
  summarize "Optional: gh not installed"
fi

# --- Optional: git-lfs ---------------------------------------------------------------
log "Checking git-lfs (optional — LFS review hints)…"
if command -v git-lfs >/dev/null 2>&1; then
  log "  git-lfs: ok ($(command -v git-lfs))"
  summarize "git-lfs present"
else
  log "  git-lfs not on PATH — LFS-specific hints may be limited."
  echo "       • https://git-lfs.com/  or  brew install git-lfs"
  if ! prompt_continue "Continue without git-lfs?"; then
    die "Aborted."
  fi
  summarize "Optional: git-lfs not installed"
fi

# --- Install application files ------------------------------------------------------
log "Installing application to $IDLEGIT_HOME …"
mkdir -p "$IDLEGIT_HOME" "$IDLEGIT_BINDIR"

install -m 0755 "$ROOT/idlegit" "$IDLEGIT_HOME/idlegit"
install -m 0644 "$ROOT/idlegit.conf" "$IDLEGIT_HOME/idlegit.conf.sample"
install -m 0644 \
  "$ROOT/idlegit.py" \
  "$ROOT/config.py" \
  "$ROOT/models.py" \
  "$ROOT/git_ops.py" \
  "$ROOT/workers.py" \
  "$ROOT/install_merge_config.py" \
  "$IDLEGIT_HOME/"

rm -rf "$IDLEGIT_HOME/ui"
cp -R "$ROOT/ui" "$IDLEGIT_HOME/ui"
log "  copied launcher, Python modules, ui/, install_merge_config.py"
summarize "Installed / refreshed app files in $IDLEGIT_HOME"

log "Removing __pycache__ under install tree…"
while IFS= read -r -d '' _dir; do
  rm -rf "$_dir"
done < <(find "$IDLEGIT_HOME" -type d -name __pycache__ -print0)
summarize "Stripped __pycache__ under install prefix"

log "Linking $IDLEGIT_BINDIR/idlegit → $IDLEGIT_HOME/idlegit …"
ln -sfn "$IDLEGIT_HOME/idlegit" "$IDLEGIT_BINDIR/idlegit"
summarize "Symlink: $IDLEGIT_BINDIR/idlegit"

# --- Merge user config --------------------------------------------------------------
log "Merging idlegit.conf (template → user config dir)…"
export PYTHONPATH="$ROOT"
_merge_line="$("$PY" "$ROOT/install_merge_config.py" "$ROOT/idlegit.conf" 2>&1)" || {
  log "config merge reported an error:"
  echo "$_merge_line"
  die "config merge failed"
}
log "  $_merge_line"
summarize "$_merge_line"

# --- PATH hint ----------------------------------------------------------------------
case ":${PATH:-}:" in
  *":$IDLEGIT_BINDIR:"*) summarize "PATH already includes $IDLEGIT_BINDIR" ;;
  *)
    log "Note: $IDLEGIT_BINDIR is not on your PATH."
    summarize "Add $IDLEGIT_BINDIR to PATH to run idlegit from any directory"
    ;;
esac

# --- Summary ------------------------------------------------------------------------
echo ""
log "Done. Summary:"
for _line in "${SUMMARY[@]}"; do
  printf '  • %s\n' "$_line"
done
echo ""
printf '%s Launcher: %s\n' "$LOG_PREFIX" "$IDLEGIT_BINDIR/idlegit"
printf '%s User config: set IDLEGIT_CONFIG_DIR or use the default per docs in config.user_state_dir\n' "$LOG_PREFIX"
printf '%s To merge new config keys later: %s %s/install_merge_config.py\n' "$LOG_PREFIX" "$PY" "$IDLEGIT_HOME"
