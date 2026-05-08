#!/usr/bin/env bash
# Thin launcher — finds python3, hands off to install.py for the
# real install pipeline. The Python script reads VERSION from
# config.py, renders a colored header + status rows, and runs the
# same checks-then-install flow (repo layout, git/gh/git-lfs
# detection, file copy, symlink, config merge, optional PATH
# update) that this script used to drive directly in bash.
#
# Forwards every argument to install.py:
#   -y / --yes                       Don't prompt on optional dep warnings
#   IDLEGIT_HOME                     App directory
#   IDLEGIT_BINDIR                   Symlink directory
#   IDLEGIT_CONFIG_DIR               Same as runtime: user config override
#   IDLEGIT_INSTALL_ASSUME_YES=1     Same as --yes
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$ROOT/scripts/install.py"

if [[ ! -f "$INSTALLER" ]]; then
    echo "install.sh: missing scripts/install.py (got ROOT=$ROOT)" >&2
    exit 1
fi

if command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    PY="$(command -v python)"
else
    echo "install.sh: python3 not found on PATH" >&2
    echo "       • macOS:  brew install python3   or   https://www.python.org/" >&2
    echo "       • Debian/Ubuntu:  sudo apt install python3" >&2
    exit 1
fi

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)' 2>/dev/null; then
    echo "install.sh: need Python 3.9 or newer (found: $("$PY" -V 2>&1))" >&2
    exit 1
fi

exec "$PY" "$INSTALLER" "$@"
