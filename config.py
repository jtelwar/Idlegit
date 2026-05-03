"""Defaults, the Config dataclass, and the conf-file loaders.

Configuration lives in two files:
  - idlegit.conf — global defaults (display, suggestion, LFS, action
    polling…). Restart idlegit to pick up edits.
  - idlegit.workspaces — one or more named workspaces, each with its own
    folder list, optional setting overrides, and optional subtree
    declarations. Workspaces are switchable at runtime via the title-row
    selector; the workspace-overrides modal persists changes back here.
"""
from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import List

from models import SubtreeSpec, Workspace

TOOL_DIR = Path(__file__).resolve().parent
CONFIG_FILE = TOOL_DIR / "idlegit.conf"
WORKSPACES_FILE = TOOL_DIR / "idlegit.workspaces"

DEFAULT_SUGGEST = 3
DEFAULT_LFS_WARN_MB = 100  # GitHub rejects non-LFS pushes for blobs over 100 MB.
DEFAULT_BRANCH_DISPLAY_MAX = 12
DEFAULT_NAME_DISPLAY_MAX = 40
# Submodule + subtree child rows render under their parent with a "↳"
# / "⊕" glyph. They share the same name column as the parent rows by
# default (so the visual layout doesn't wobble), but can be capped
# tighter via `child_name_display_max` when the user wants a more
# compact submodule list. -1 = "use the parent's name_display_max".
DEFAULT_CHILD_NAME_DISPLAY_MAX = -1
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
# Cap on commit-message length displayed on the review screen. The
# message wraps across as many rows as needed to fit; only end-
# truncation kicks in once a single message exceeds the cap. Long
# messages get fully visible by default (480 chars ≈ 6-8 wrapped rows
# at typical pane widths) — set to 0 to disable the cap entirely.
DEFAULT_MAX_COMMIT_MESSAGE_LENGTH_IN_REVIEW = 480
# Smart-sync defaults — match the historical always-on behaviour so
# existing setups are unaffected by the new toggles.
DEFAULT_ALIGN_HEADS = True
DEFAULT_AUTO_FF = True
DEFAULT_PROMPT_FOR_BRANCH = True


@dataclass
class Config:
    """Global defaults, read once at startup from idlegit.conf. Workspaces
    layer per-workspace overrides on top of these values; folders and
    subtrees are workspace-scoped and live in idlegit.workspaces."""
    suggest_added: int = DEFAULT_SUGGEST
    suggest_updated: int = DEFAULT_SUGGEST
    suggest_deleted: int = DEFAULT_SUGGEST
    lfs_warn_bytes: int = DEFAULT_LFS_WARN_MB * 1024 * 1024
    default_auto_stage: bool = True
    default_auto_push: bool = True
    branch_display_max: int = DEFAULT_BRANCH_DISPLAY_MAX
    name_display_max: int = DEFAULT_NAME_DISPLAY_MAX
    child_name_display_max: int = DEFAULT_CHILD_NAME_DISPLAY_MAX
    task_name_display_max: int = DEFAULT_TASK_NAME_DISPLAY_MAX
    name_truncation: str = DEFAULT_TRUNCATION_MODE
    branch_truncation: str = DEFAULT_TRUNCATION_MODE
    task_name_truncation: str = DEFAULT_TRUNCATION_MODE
    max_visible_repo_rows: int = DEFAULT_MAX_VISIBLE_REPO_ROWS
    track_actions_default: bool = DEFAULT_TRACK_ACTIONS
    actions_poll_seconds: float = DEFAULT_ACTIONS_POLL_SECONDS
    auto_remove_completed_after: float = DEFAULT_AUTO_REMOVE_COMPLETED_AFTER
    max_commit_message_length_in_review: int = (
        DEFAULT_MAX_COMMIT_MESSAGE_LENGTH_IN_REVIEW)
    # Smart-sync starting values. Each one maps onto a State attribute
    # of the same root name and lives in the workspace menu's
    # SMART-SYNC section.
    default_align_heads: bool = DEFAULT_ALIGN_HEADS
    default_auto_ff: bool = DEFAULT_AUTO_FF
    default_prompt_for_branch: bool = DEFAULT_PROMPT_FOR_BRANCH


def load_config() -> Config:
    """Read idlegit.conf and return a Config. Missing keys fall back to
    defaults; a malformed file falls back wholesale."""
    suggest_added = DEFAULT_SUGGEST
    suggest_updated = DEFAULT_SUGGEST
    suggest_deleted = DEFAULT_SUGGEST
    lfs_warn_mb = DEFAULT_LFS_WARN_MB
    default_auto_stage = True
    default_auto_push = True
    branch_display_max = DEFAULT_BRANCH_DISPLAY_MAX
    name_display_max = DEFAULT_NAME_DISPLAY_MAX
    child_name_display_max = DEFAULT_CHILD_NAME_DISPLAY_MAX
    task_name_display_max = DEFAULT_TASK_NAME_DISPLAY_MAX
    name_truncation = DEFAULT_TRUNCATION_MODE
    branch_truncation = DEFAULT_TRUNCATION_MODE
    task_name_truncation = DEFAULT_TRUNCATION_MODE
    max_visible_repo_rows = DEFAULT_MAX_VISIBLE_REPO_ROWS
    track_actions_default = DEFAULT_TRACK_ACTIONS
    actions_poll_seconds = DEFAULT_ACTIONS_POLL_SECONDS
    auto_remove_completed_after = DEFAULT_AUTO_REMOVE_COMPLETED_AFTER
    max_commit_message_length_in_review = (
        DEFAULT_MAX_COMMIT_MESSAGE_LENGTH_IN_REVIEW)
    default_align_heads = DEFAULT_ALIGN_HEADS
    default_auto_ff = DEFAULT_AUTO_FF
    default_prompt_for_branch = DEFAULT_PROMPT_FOR_BRANCH

    if CONFIG_FILE.exists():
        try:
            cp = configparser.ConfigParser(inline_comment_prefixes=(";",))
            cp.read(CONFIG_FILE)
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
            child_name_display_max = cp.getint(
                "idlegit", "child_name_display_max",
                fallback=DEFAULT_CHILD_NAME_DISPLAY_MAX)
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
            max_commit_message_length_in_review = cp.getint(
                "idlegit", "max_commit_message_length_in_review",
                fallback=DEFAULT_MAX_COMMIT_MESSAGE_LENGTH_IN_REVIEW)
            default_align_heads = cp.getboolean(
                "idlegit", "default_align_heads",
                fallback=DEFAULT_ALIGN_HEADS)
            default_auto_ff = cp.getboolean(
                "idlegit", "default_auto_ff", fallback=DEFAULT_AUTO_FF)
            default_prompt_for_branch = cp.getboolean(
                "idlegit", "default_prompt_for_branch",
                fallback=DEFAULT_PROMPT_FOR_BRANCH)
        except (configparser.Error, OSError, ValueError):
            pass

    if name_truncation not in TRUNCATION_MODES:
        name_truncation = DEFAULT_TRUNCATION_MODE
    if branch_truncation not in TRUNCATION_MODES:
        branch_truncation = DEFAULT_TRUNCATION_MODE
    if task_name_truncation not in TRUNCATION_MODES:
        task_name_truncation = DEFAULT_TRUNCATION_MODE

    return Config(
        suggest_added=max(0, suggest_added),
        suggest_updated=max(0, suggest_updated),
        suggest_deleted=max(0, suggest_deleted),
        lfs_warn_bytes=max(0, lfs_warn_mb) * 1024 * 1024,
        default_auto_stage=default_auto_stage,
        default_auto_push=default_auto_push,
        branch_display_max=branch_display_max,
        name_display_max=name_display_max,
        child_name_display_max=child_name_display_max,
        task_name_display_max=task_name_display_max,
        name_truncation=name_truncation,
        branch_truncation=branch_truncation,
        task_name_truncation=task_name_truncation,
        max_visible_repo_rows=max(0, max_visible_repo_rows),
        track_actions_default=track_actions_default,
        actions_poll_seconds=max(0.5, actions_poll_seconds),
        auto_remove_completed_after=auto_remove_completed_after,
        max_commit_message_length_in_review=max(
            0, max_commit_message_length_in_review),
        default_align_heads=default_align_heads,
        default_auto_ff=default_auto_ff,
        default_prompt_for_branch=default_prompt_for_branch,
    )


# ---------- Workspace overrides schema ------------------------------------


# Map of override key (the ini key in idlegit.workspaces) → coercion type.
# Only these keys are recognised by load_workspaces / save_workspaces;
# anything else is silently dropped on read so a stale schema in the file
# never crashes the loader. Keys mirror Config field names so applying an
# override is a straightforward `setattr(state, key, value)` from
# WORKSPACE_OVERRIDE_TARGETS below.
WORKSPACE_OVERRIDE_TYPES: "dict[str, str]" = {
    "default_auto_stage": "bool",
    "default_auto_push": "bool",
    "track_actions_default": "bool",
    "default_align_heads": "bool",
    "default_auto_ff": "bool",
    "default_prompt_for_branch": "bool",
    "suggest_added": "int",
    "suggest_updated": "int",
    "suggest_deleted": "int",
    "name_truncation": "trunc_mode",
    "branch_truncation": "trunc_mode",
    "task_name_truncation": "trunc_mode",
    "name_display_max": "int",
    "child_name_display_max": "int",
    "branch_display_max": "int",
    "task_name_display_max": "int",
    "max_visible_repo_rows": "int",
    "lfs_warn_mb": "int",
    "actions_poll_seconds": "float",
    "auto_remove_completed_tasks_after_interval": "float",
}


# Map of override key → State attribute the override drives at runtime.
# Most are 1:1; a few (default_auto_stage → state.auto_stage) translate
# the conf field-name into the State attribute it initialises. Keys not
# present here are configuration-only and don't have a live State mirror.
WORKSPACE_OVERRIDE_TARGETS: "dict[str, str]" = {
    "default_auto_stage": "auto_stage",
    "default_auto_push": "auto_push",
    "track_actions_default": "track_actions_default",
    "default_align_heads": "align_heads",
    "default_auto_ff": "auto_ff",
    "default_prompt_for_branch": "prompt_for_branch",
    "suggest_added": "suggest_added",
    "suggest_updated": "suggest_updated",
    "suggest_deleted": "suggest_deleted",
    "name_truncation": "name_truncation",
    "branch_truncation": "branch_truncation",
    "task_name_truncation": "task_name_truncation",
    "name_display_max": "name_display_max",
    "child_name_display_max": "child_name_display_max",
    "branch_display_max": "branch_display_max",
    "task_name_display_max": "task_name_display_max",
    "max_visible_repo_rows": "max_visible_repo_rows",
    "lfs_warn_mb": "lfs_warn_bytes",
    "actions_poll_seconds": "actions_poll_seconds",
    "auto_remove_completed_tasks_after_interval": "auto_remove_completed_after",
}


def coerce_override_value(key: str, raw: str):
    """Parse `raw` into the right Python type for override `key`. Returns
    None when the value is malformed or `key` isn't recognised — the
    caller drops malformed entries silently. Callers persisting back via
    save_workspaces should pass already-coerced values; this helper is
    the read-side counterpart."""
    kind = WORKSPACE_OVERRIDE_TYPES.get(key)
    if kind is None:
        return None
    s = raw.strip()
    if kind == "bool":
        low = s.lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
        return None
    if kind == "int":
        try:
            return int(s)
        except ValueError:
            return None
    if kind == "float":
        try:
            return float(s)
        except ValueError:
            return None
    if kind == "trunc_mode":
        low = s.lower()
        return low if low in TRUNCATION_MODES else None
    return None


def base_value_for_override(cfg: Config, key: str):
    """Return the inherited (un-overridden) value for override `key`, read
    from the loaded base Config. Used by the modal's "clear override"
    action to revert a row to its inherited state, and for rendering the
    "(default)" hint next to inherited rows."""
    if key == "lfs_warn_mb":
        return cfg.lfs_warn_bytes // (1024 * 1024)
    return getattr(cfg, key, None)


def state_attr_value_from_override(key: str, value):
    """Translate a coerced override value into the form the State
    attribute expects. Right now this only matters for `lfs_warn_mb`,
    which lives in the conf as megabytes but on State as bytes."""
    if key == "lfs_warn_mb":
        try:
            return max(0, int(value)) * 1024 * 1024
        except (TypeError, ValueError):
            return 0
    return value


# ---------- Workspaces loader / saver -------------------------------------


def _parse_folders_block(text: str, anchor: Path) -> List[Path]:
    """Parse a multi-line `folders` value: one path per line,
    ~/-expanded, resolved against `anchor` when relative, deduped while
    preserving the user's listed order."""
    folders: List[Path] = []
    seen: set = set()
    for raw in text.splitlines():
        token = raw.strip()
        if not token:
            continue
        p = Path(token).expanduser()
        if not p.is_absolute():
            p = anchor / p
        resolved = p.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        folders.append(resolved)
    return folders


def load_workspaces() -> "tuple[List[Workspace], int]":
    """Read idlegit.workspaces and return (workspaces, active_index).
    `workspaces` is the list of [workspace.<name>] sections in file
    order; `active_index` is the workspace recorded as last-active in
    the optional top-level [idlegit] section (defaulting to 0 when
    absent or naming a workspace that no longer exists). Returns
    ([], 0) when the file is missing or malformed — the caller
    (idlegit.run) treats that as the signal to launch the creator
    wizard before the main UI takes over."""
    if not WORKSPACES_FILE.exists():
        return [], 0

    try:
        cp = configparser.ConfigParser(inline_comment_prefixes=(";",))
        cp.read(WORKSPACES_FILE)
    except (configparser.Error, OSError):
        return [], 0

    # Optional top-level [idlegit] block carries cross-workspace
    # preferences. Right now it just records which workspace was last
    # active so we can land the user back where they left off after
    # restart.
    remembered_active = ""
    if cp.has_section("idlegit"):
        remembered_active = cp.get(
            "idlegit", "active_workspace", fallback="").strip()

    # Group sections by workspace name so a workspace's folder/override
    # block and any nested subtree blocks aggregate together.
    workspaces: List[Workspace] = []
    for section in cp.sections():
        if not section.startswith("workspace."):
            continue
        # Skip nested .subtree.* sections; they're collected in a second
        # pass once their parent workspace is known by name. Match on
        # the literal ".subtree." infix instead of any dot — a previous
        # `if "." in rest` lost workspaces with dotted names like
        # "Upskill.Health" because the dot wasn't reserved syntax.
        rest = section[len("workspace."):]
        if ".subtree." in rest:
            continue
        name = rest
        folders_raw = cp.get(section, "folders", fallback="").strip()
        if not folders_raw:
            # Workspace without a folders entry is meaningless — skip
            # rather than producing a silently-empty workspace.
            continue
        folders = _parse_folders_block(folders_raw, TOOL_DIR)
        if not folders:
            continue
        overrides: dict = {}
        for key in cp[section]:
            if key == "folders":
                continue
            value = coerce_override_value(key, cp.get(section, key))
            if value is not None:
                overrides[key] = value
        workspaces.append(Workspace(
            name=name, folders=folders, overrides=overrides))

    # Second pass: workspace-scoped subtrees. `[workspace.X.subtree.Y]`
    # attaches to workspace X; if X doesn't exist we silently drop the
    # subtree (matches the loader's overall tolerance for malformed
    # entries).
    by_name = {ws.name: ws for ws in workspaces}
    for section in cp.sections():
        if not section.startswith("workspace."):
            continue
        rest = section[len("workspace."):]
        if ".subtree." not in rest:
            continue
        ws_name, _, sub_name = rest.partition(".subtree.")
        ws = by_name.get(ws_name)
        if ws is None:
            continue
        parent_rel = cp.get(section, "parent", fallback="").strip()
        source_rel = cp.get(section, "source", fallback="").strip()
        prefix = cp.get(section, "prefix", fallback="").strip().strip("/")
        if parent_rel and source_rel and prefix:
            ws.subtrees.append(SubtreeSpec(
                name=sub_name, parent=parent_rel,
                source=source_rel, prefix=prefix))

    # Resolve the remembered active-workspace name to an index, or
    # default to 0 when the file's reference is stale (workspace was
    # renamed or removed between sessions).
    active_index = 0
    if remembered_active:
        for i, ws in enumerate(workspaces):
            if ws.name == remembered_active:
                active_index = i
                break
    return workspaces, active_index


def _format_folders_block(folders: List[Path]) -> str:
    """Render a folders list back into the multi-line continuation form
    used by configparser. The first line sits inline after `folders =`;
    every subsequent line is indented so the parser treats it as a
    continuation of the same value."""
    if not folders:
        return ""
    lines = [str(folders[0])]
    for f in folders[1:]:
        lines.append("                        " + str(f))
    return "\n".join(lines)


def save_workspaces(workspaces: List[Workspace],
                    active_index: int = 0) -> None:
    """Persist the workspaces list to idlegit.workspaces. Overrides are
    written as plain key=value entries inside each [workspace.<name>]
    section, in the schema order defined by WORKSPACE_OVERRIDE_TYPES so
    two consecutive saves with the same data produce byte-identical
    files (helpful for diffs and tests). Workspace-scoped subtrees are
    emitted as nested [workspace.<name>.subtree.<sub>] sections.

    `active_index` is recorded under a top-level [idlegit] section as
    `active_workspace = <name>` so the next session can land the user
    back in the same workspace they last used. Out-of-range indices
    (e.g. when no workspaces are configured yet) silently skip the
    [idlegit] block; load_workspaces handles a missing block as
    "default to index 0"."""
    cp = configparser.ConfigParser()
    if 0 <= active_index < len(workspaces):
        cp["idlegit"] = {
            "active_workspace": workspaces[active_index].name,
        }
    for ws in workspaces:
        section = f"workspace.{ws.name}"
        cp[section] = {}
        cp[section]["folders"] = _format_folders_block(ws.folders)
        for key in WORKSPACE_OVERRIDE_TYPES:
            if key not in ws.overrides:
                continue
            value = ws.overrides[key]
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            else:
                rendered = str(value)
            cp[section][key] = rendered
        for sub in ws.subtrees:
            sub_section = f"workspace.{ws.name}.subtree.{sub.name}"
            cp[sub_section] = {
                "parent": sub.parent,
                "source": sub.source,
                "prefix": sub.prefix,
            }

    # Write atomically: stage a sibling file and rename. Stops a partial
    # write from leaving the user with a half-truncated file if we crash
    # or get interrupted between the open and the close.
    tmp = WORKSPACES_FILE.with_suffix(WORKSPACES_FILE.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write("# idlegit workspaces. Each [workspace.<name>] section "
                 "defines a workspace.\n")
        fh.write("# `folders` is required (one path per line, ~/-expansion "
                 "supported).\n")
        fh.write("# Other keys override the matching idlegit.conf default. "
                 "Subtree relationships go in\n")
        fh.write("# nested [workspace.<name>.subtree.<sub>] sections.\n\n")
        cp.write(fh)
    tmp.replace(WORKSPACES_FILE)


def apply_workspace_overrides(state, cfg: Config, ws: Workspace) -> None:
    """Reset the live State settings to base-config defaults, then apply
    the workspace's overrides on top. Called both at startup (after
    instantiating State) and on workspace switch (to swap settings).

    Booleans-flavoured-as-defaults (`default_auto_stage` →
    `state.auto_stage`) are applied directly; the user's runtime toggles
    on the main screen are reset to the new workspace's defaults rather
    than carried over, which is the same behaviour they'd see if they
    relaunched the app pointed at the new workspace."""
    # Start from base defaults.
    state.suggest_added = cfg.suggest_added
    state.suggest_updated = cfg.suggest_updated
    state.suggest_deleted = cfg.suggest_deleted
    state.lfs_warn_bytes = cfg.lfs_warn_bytes
    state.branch_display_max = cfg.branch_display_max
    state.name_display_max = cfg.name_display_max
    state.child_name_display_max = cfg.child_name_display_max
    state.task_name_display_max = cfg.task_name_display_max
    state.name_truncation = cfg.name_truncation
    state.branch_truncation = cfg.branch_truncation
    state.task_name_truncation = cfg.task_name_truncation
    state.max_visible_repo_rows = cfg.max_visible_repo_rows
    state.track_actions_default = cfg.track_actions_default
    state.actions_poll_seconds = cfg.actions_poll_seconds
    state.auto_remove_completed_after = cfg.auto_remove_completed_after
    state.max_commit_message_length_in_review = (
        cfg.max_commit_message_length_in_review)
    state.auto_stage = cfg.default_auto_stage
    state.auto_push = cfg.default_auto_push
    state.align_heads = cfg.default_align_heads
    state.auto_ff = cfg.default_auto_ff
    state.prompt_for_branch = cfg.default_prompt_for_branch
    state.subtrees = list(ws.subtrees)
    # Now overlay the workspace's overrides.
    for key, value in ws.overrides.items():
        target = WORKSPACE_OVERRIDE_TARGETS.get(key)
        if target is None:
            continue
        setattr(state, target, state_attr_value_from_override(key, value))
