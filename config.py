"""Defaults, the Config dataclass, and the conf-file loader."""
from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from models import SubtreeSpec

TOOL_DIR = Path(__file__).resolve().parent
CONFIG_FILE = TOOL_DIR / "idlegit.conf"

DEFAULT_REPOSITORY_FOLDERS = ".."
DEFAULT_SUGGEST = 3
DEFAULT_LFS_WARN_MB = 100  # GitHub rejects non-LFS pushes for blobs over 100 MB.
DEFAULT_BRANCH_DISPLAY_MAX = 12
DEFAULT_NAME_DISPLAY_MAX = 40
# Repo names embedded in sidebar task labels are truncated separately —
# the panel is narrow and a long display name like
# "Upskill.Health.Domain.Models" can crowd out the actual task name.
# 16 chars + middle ellipsis ("Upskill…l.Models") tends to read fine.
DEFAULT_TASK_NAME_DISPLAY_MAX = 16
DEFAULT_TRUNCATION_MODE = "middle"
TRUNCATION_MODES = ("start", "middle", "end")
DEFAULT_MAX_VISIBLE_REPO_ROWS = 0  # 0 = use all available height
DEFAULT_TRACK_ACTIONS = True
DEFAULT_ACTIONS_POLL_SECONDS = 5.0
DEFAULT_AUTO_REMOVE_COMPLETED_AFTER = -1.0  # <0 = never auto-remove


@dataclass
class Config:
    # Every git repo discovered under any of these folders is shown.
    # Multiple paths are supported; results are merged + deduped by
    # absolute path, so overlapping folders won't list a repo twice.
    repository_folders: List[Path]
    suggest_added: int = DEFAULT_SUGGEST
    suggest_updated: int = DEFAULT_SUGGEST
    suggest_deleted: int = DEFAULT_SUGGEST
    lfs_warn_bytes: int = DEFAULT_LFS_WARN_MB * 1024 * 1024
    default_auto_stage: bool = True
    default_auto_push: bool = True
    branch_display_max: int = DEFAULT_BRANCH_DISPLAY_MAX
    name_display_max: int = DEFAULT_NAME_DISPLAY_MAX
    task_name_display_max: int = DEFAULT_TASK_NAME_DISPLAY_MAX
    name_truncation: str = DEFAULT_TRUNCATION_MODE
    branch_truncation: str = DEFAULT_TRUNCATION_MODE
    task_name_truncation: str = DEFAULT_TRUNCATION_MODE
    max_visible_repo_rows: int = DEFAULT_MAX_VISIBLE_REPO_ROWS
    subtrees: List[SubtreeSpec] = field(default_factory=list)
    track_actions_default: bool = DEFAULT_TRACK_ACTIONS
    actions_poll_seconds: float = DEFAULT_ACTIONS_POLL_SECONDS
    auto_remove_completed_after: float = DEFAULT_AUTO_REMOVE_COMPLETED_AFTER


def load_config() -> Config:
    """Read idlegit.conf and return a Config. Missing keys fall back to
    defaults; a malformed file falls back wholesale."""
    folders_str = DEFAULT_REPOSITORY_FOLDERS
    suggest_added = DEFAULT_SUGGEST
    suggest_updated = DEFAULT_SUGGEST
    suggest_deleted = DEFAULT_SUGGEST
    lfs_warn_mb = DEFAULT_LFS_WARN_MB
    default_auto_stage = True
    default_auto_push = True
    branch_display_max = DEFAULT_BRANCH_DISPLAY_MAX
    name_display_max = DEFAULT_NAME_DISPLAY_MAX
    task_name_display_max = DEFAULT_TASK_NAME_DISPLAY_MAX
    name_truncation = DEFAULT_TRUNCATION_MODE
    branch_truncation = DEFAULT_TRUNCATION_MODE
    task_name_truncation = DEFAULT_TRUNCATION_MODE
    max_visible_repo_rows = DEFAULT_MAX_VISIBLE_REPO_ROWS
    track_actions_default = DEFAULT_TRACK_ACTIONS
    actions_poll_seconds = DEFAULT_ACTIONS_POLL_SECONDS
    auto_remove_completed_after = DEFAULT_AUTO_REMOVE_COMPLETED_AFTER
    subtrees: List[SubtreeSpec] = []

    if CONFIG_FILE.exists():
        try:
            cp = configparser.ConfigParser(inline_comment_prefixes=(";",))
            cp.read(CONFIG_FILE)
            folders_str = cp.get(
                "idlegit", "repository_folders",
                fallback=DEFAULT_REPOSITORY_FOLDERS)
            suggest_added = cp.getint("idlegit", "suggest_added", fallback=DEFAULT_SUGGEST)
            suggest_updated = cp.getint("idlegit", "suggest_updated", fallback=DEFAULT_SUGGEST)
            suggest_deleted = cp.getint("idlegit", "suggest_deleted", fallback=DEFAULT_SUGGEST)
            lfs_warn_mb = cp.getint("idlegit", "lfs_warn_mb", fallback=DEFAULT_LFS_WARN_MB)
            default_auto_stage = cp.getboolean(
                "idlegit", "default_auto_stage", fallback=True)
            default_auto_push = cp.getboolean(
                "idlegit", "default_auto_push", fallback=True)
            branch_display_max = cp.getint(
                "idlegit", "branch_display_max", fallback=DEFAULT_BRANCH_DISPLAY_MAX)
            name_display_max = cp.getint(
                "idlegit", "name_display_max", fallback=DEFAULT_NAME_DISPLAY_MAX)
            task_name_display_max = cp.getint(
                "idlegit", "task_name_display_max",
                fallback=DEFAULT_TASK_NAME_DISPLAY_MAX)
            name_truncation = cp.get(
                "idlegit", "name_truncation",
                fallback=DEFAULT_TRUNCATION_MODE).strip().lower()
            branch_truncation = cp.get(
                "idlegit", "branch_truncation",
                fallback=DEFAULT_TRUNCATION_MODE).strip().lower()
            task_name_truncation = cp.get(
                "idlegit", "task_name_truncation",
                fallback=DEFAULT_TRUNCATION_MODE).strip().lower()
            max_visible_repo_rows = cp.getint(
                "idlegit", "max_visible_repo_rows",
                fallback=DEFAULT_MAX_VISIBLE_REPO_ROWS)
            track_actions_default = cp.getboolean(
                "idlegit", "track_actions_default",
                fallback=DEFAULT_TRACK_ACTIONS)
            actions_poll_seconds = cp.getfloat(
                "idlegit", "actions_poll_seconds",
                fallback=DEFAULT_ACTIONS_POLL_SECONDS)
            auto_remove_completed_after = cp.getfloat(
                "idlegit", "auto_remove_completed_tasks_after_interval",
                fallback=DEFAULT_AUTO_REMOVE_COMPLETED_AFTER)
            for section in cp.sections():
                if not section.startswith("subtree."):
                    continue
                name = section[len("subtree."):]
                parent_rel = cp.get(section, "parent", fallback="").strip()
                source_rel = cp.get(section, "source", fallback="").strip()
                prefix = cp.get(section, "prefix", fallback="").strip().strip("/")
                if parent_rel and source_rel and prefix:
                    subtrees.append(SubtreeSpec(
                        name=name, parent=parent_rel,
                        source=source_rel, prefix=prefix,
                    ))
        except (configparser.Error, OSError, ValueError):
            pass

    if name_truncation not in TRUNCATION_MODES:
        name_truncation = DEFAULT_TRUNCATION_MODE
    if branch_truncation not in TRUNCATION_MODES:
        branch_truncation = DEFAULT_TRUNCATION_MODE
    if task_name_truncation not in TRUNCATION_MODES:
        task_name_truncation = DEFAULT_TRUNCATION_MODE

    # One path per line (configparser continuation-lines). Splitting
    # on a delimiter would mangle paths that legitimately contain it
    # — comma, space, semicolon are all valid POSIX filename chars.
    # Each entry is `~`-expanded, resolved relative to the conf file
    # when not absolute, then deduped while preserving the user's
    # listed order.
    folders: List[Path] = []
    seen: set = set()
    for raw in folders_str.splitlines():
        token = raw.strip()
        if not token:
            continue
        p = Path(token).expanduser()
        if not p.is_absolute():
            p = TOOL_DIR / p
        resolved = p.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        folders.append(resolved)
    if not folders:
        # Fallback: never return an empty list — the loader's contract
        # is "always at least one folder, even if config is broken".
        folders = [(TOOL_DIR / DEFAULT_REPOSITORY_FOLDERS).resolve()]

    return Config(
        repository_folders=folders,
        suggest_added=max(0, suggest_added),
        suggest_updated=max(0, suggest_updated),
        suggest_deleted=max(0, suggest_deleted),
        lfs_warn_bytes=max(0, lfs_warn_mb) * 1024 * 1024,
        default_auto_stage=default_auto_stage,
        default_auto_push=default_auto_push,
        branch_display_max=branch_display_max,
        name_display_max=name_display_max,
        task_name_display_max=task_name_display_max,
        name_truncation=name_truncation,
        branch_truncation=branch_truncation,
        task_name_truncation=task_name_truncation,
        max_visible_repo_rows=max(0, max_visible_repo_rows),
        subtrees=subtrees,
        track_actions_default=track_actions_default,
        actions_poll_seconds=max(0.5, actions_poll_seconds),
        auto_remove_completed_after=auto_remove_completed_after,
    )
