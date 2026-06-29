"""Discovery + loading for the bundled help pages.

Help pages live under `<install_dir>/help/` as `.md` files. The
directory sits alongside `core/`, `ui/`, and the other top-level
modules — same location as `idlegit.default.conf` — so the installer
copies it in one piece and `update.py` refreshes it on every release.

The loader returns a list of `HelpPage` records sorted by filename, so
a numeric prefix (`01-overview.md`, `02-getting-started.md`, …) gives
a deterministic page order without any per-file metadata. The first
`# ...` heading in each page is parsed out as the human-readable title;
files with no heading fall back to the de-prefixed, de-extensioned
filename (`02-getting-started.md` → `getting started`).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

from .config import TOOL_DIR
from .state.views import HelpPage

# Where help pages live in both the dev-tree and post-install layouts.
# Same anchor as `idlegit.default.conf` (`TOOL_DIR`), so a missing
# directory means the install lost the help dir — surfacing as
# "no help pages" rather than crashing.
_HELP_DIR_NAME = "help"

# First H1 capture — `# Title` at the start of a line, allowing
# leading whitespace tolerance. Pulled into a precompiled regex
# since the loader can be called once per session but the cost is
# negligible and keeps the regex literal local to its only use.
_H1_RE = re.compile(r"^\s{0,3}#\s+(.+?)\s*$", re.MULTILINE)

# Strip a leading numeric sort prefix like "01-" or "10_" from the
# fallback title. Keeps the visible title clean when no `# Title`
# heading is present in the body. Captures the rest.
_NUMERIC_PREFIX_RE = re.compile(r"^\d+[-_\s]+(.*)$")


def help_dir() -> Path:
    """Return the directory help pages are loaded from. Anchored to
    `TOOL_DIR` (`core/config.py`'s install root) so both the dev tree
    and the post-install flat layout resolve to the same shipped
    `help/` directory."""
    return TOOL_DIR / _HELP_DIR_NAME


def _title_for(path: Path, body: str) -> str:
    """Best-effort title resolution: first `# heading`, then the
    de-prefixed filename. Always returns a non-empty string — the
    sidebar relies on every page having a label."""
    m = _H1_RE.search(body)
    if m:
        return m.group(1).strip()
    stem = path.stem  # `02-getting-started`
    pretty = _NUMERIC_PREFIX_RE.sub(r"\1", stem)
    return pretty.replace("-", " ").replace("_", " ").strip() or stem


def load_help_pages() -> List[HelpPage]:
    """List + read every `.md` file in the bundled `help/` directory.
    Returns pages sorted by filename (so a numeric prefix gives a
    deterministic order); on a missing directory or read error the
    returned list is empty rather than raised — the UI handles that
    case gracefully by showing a single "no help available" page."""
    d = help_dir()
    if not d.is_dir():
        return []
    pages: List[HelpPage] = []
    for path in sorted(d.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            # Best-effort: skip a single unreadable file rather than
            # killing the help screen for everyone. The page just
            # doesn't appear in the sidebar.
            continue
        pages.append(HelpPage(
            title=_title_for(path, body),
            filename=path.name,
            body=body,
        ))
    return pages
