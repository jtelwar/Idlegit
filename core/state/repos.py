"""State-owned repo, child, and workflow metadata records."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


@dataclass
class WorkflowInput:
    """One `workflow_dispatch.inputs` entry parsed from a workflow YAML file."""

    name: str
    description: str = ""
    default: str = ""


@dataclass
class WorkflowInfo:
    """A GitHub Actions workflow advertised by `gh workflow list`."""

    name: str
    path: str
    state: str = ""
    dispatchable: bool = False
    triggers_push: bool = False
    push_branches: List[str] = field(default_factory=list)
    push_branches_ignore: List[str] = field(default_factory=list)
    push_tags: List[str] = field(default_factory=list)
    push_tags_ignore: List[str] = field(default_factory=list)
    inputs: List[WorkflowInput] = field(default_factory=list)


@dataclass
class Repo:
    """One top-level repo row in a workspace projection."""

    rel: str
    path: Path
    branch: str = ""
    head: str = ""
    upstream: Optional[str] = None
    remote_url: Optional[str] = None
    remote_url_raw: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    staged: List[Tuple[str, str]] = field(default_factory=list)
    unstaged: List[Tuple[str, str]] = field(default_factory=list)
    untracked: List[str] = field(default_factory=list)
    nested_subs: List[Tuple[str, Path]] = field(default_factory=list)
    siblings: List[Tuple["Repo", Path]] = field(default_factory=list)
    children: List["ChildRef"] = field(default_factory=list)
    message: str = ""
    error: str = ""
    merging: bool = False
    conflict_paths: List[str] = field(default_factory=list)
    synthetic: bool = False
    workflows: List[WorkflowInfo] = field(default_factory=list)
    workflow_states_hydrated: bool = False

    @property
    def display_name(self) -> str:
        """Name as shown in workspace rows."""
        if self.rel == ".":
            return f"{self.path.name} (root)"
        return self.rel

    @property
    def is_dirty(self) -> bool:
        """Whether this repo has staged, unstaged, or untracked changes."""
        return bool(self.staged or self.unstaged or self.untracked)


@dataclass
class ChildRef:
    """A nested-content reference inside another tracked repo."""

    repo: Repo
    nested_path: Path
    head: str = ""
    branch: str = ""
    in_sync: bool = True
    kind: str = "submodule"
    message: str = ""
    dirty: bool = False
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    merging: bool = False
    error: str = ""
