"""Detection helper for the "launched from inside a git repo" flow.

When `idlegit` runs from inside a git repo that's not already covered
by any configured workspace, the startup code mints an *ephemeral*
workspace pointing at that repo so the user lands in something useful
without having to first configure a workspace by hand. The ephemeral
workspace is not persisted to `idlegit.workspaces` — it disappears the
moment the session ends, unless the user explicitly saves it via the
workspace menu's "Save as workspace…" action.

This module's sole job is the discovery half — walking up from `cwd`
to find a `.git` marker, plus the duplicate-check against an already-
configured workspace. The workspace-construction and UI rendering live
in `idlegit.py` and `ui/` respectively.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .models import Workspace


def find_git_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from `start` (default: cwd) looking for a `.git` entry
    (either a directory in a normal checkout, or a file in a submodule
    / worktree). Returns the first directory that contains a `.git`,
    or None if we hit the filesystem root without finding one.

    Resolved before walking so `cd repo/src/foo && idlegit` correctly
    finds the repo regardless of symlinks along the way. Errors during
    resolution (a permission issue on the symlink chain, say) return
    None — caller treats absence as "no ephemeral workspace today"."""
    try:
        path = (start if start is not None else Path.cwd()).resolve()
    except OSError:
        return None
    # `Path.parents` doesn't include the path itself, so check `path`
    # first then iterate ancestors.
    for candidate in (path, *path.parents):
        try:
            if (candidate / ".git").exists():
                return candidate
        except OSError:
            # A permission error on an ancestor shouldn't break the
            # walk for deeper repos — skip and continue.
            continue
    return None


def repo_covered_by_workspace(repo_path: Path,
                              workspaces: List[Workspace]) -> Optional[Workspace]:
    """Return the configured workspace whose `folders` list already
    covers `repo_path`, or None when no match.

    "Covers" means either: a folder IS the repo root (the repo is a
    top-level entry in that workspace), or a folder is the repo's
    parent (the repo is one of that folder's immediate-child repos —
    same rule `discover_repos` uses). This deliberately avoids deeper
    ancestor matches: `~/work` containing a workspace covering
    `~/work/myproject` is still "covered" via parent-match, but
    `~/` listed as a workspace folder would NOT make every nested
    repo "covered" — that'd be too aggressive.
    """
    try:
        repo_resolved = repo_path.resolve()
    except OSError:
        return None
    repo_parent = repo_resolved.parent
    for ws in workspaces:
        for folder in ws.folders:
            try:
                folder_resolved = folder.resolve()
            except OSError:
                continue
            if folder_resolved == repo_resolved:
                return ws
            if folder_resolved == repo_parent:
                return ws
    return None


def build_ephemeral_workspace(repo_path: Path) -> Workspace:
    """Construct the in-memory `Workspace` for a detected repo. The
    workspace's single folder IS the repo root, so `discover_repos`
    picks the repo up as `rel="."` (plus any nested child repos under
    it). The display name is the repo's directory basename — the
    bracket formatting (`[name]`) lives in the UI layer."""
    name = repo_path.name or "local repo"
    return Workspace(
        name=name,
        folders=[repo_path],
        ephemeral=True,
    )
