"""Stable runtime identifiers for workspaces, repos, and child rows."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, order=True)
class WorkspaceId:
    """Stable identity for a configured workspace."""

    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class RepoId:
    """Stable identity for a repo checkout within a workspace."""

    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class ChildId:
    """Stable identity for a nested repo row within a parent checkout."""

    value: str

    def __str__(self) -> str:
        return self.value


def workspace_id(name: str) -> WorkspaceId:
    """Build a workspace id from its configured name."""
    return WorkspaceId(_clean_component(name or "(unnamed)"))


def repo_id(workspace: WorkspaceId, path: Path) -> RepoId:
    """Build a repo id from workspace identity and checkout path."""
    return RepoId(f"{workspace.value}:{_path_key(path)}")


def child_id(parent: RepoId, nested_path: Path, kind: str) -> ChildId:
    """Build a child id from parent repo identity, nested path, and kind."""
    return ChildId(f"{parent.value}:{_clean_component(kind)}:{_path_key(nested_path)}")


def _path_key(path: Path) -> str:
    try:
        return str(path.expanduser().resolve(strict=False))
    except OSError:
        return str(path.expanduser().absolute())


def _clean_component(value: str) -> str:
    return value.strip() or "(empty)"
