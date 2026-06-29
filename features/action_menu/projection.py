"""Action-menu projection builders and cached metadata helpers."""
from __future__ import annotations

from typing import List, Optional, Tuple

from core.git_ops import gh_available, parse_github_slug
from core.state.action_menu import (
    ActionMenu,
    ActionMenuItem,
    ActionSubmenuFrame,
    CommitEntry,
    FileEntry,
)
from core.state.repos import ChildRef, Repo

# The existing ActionMenu dataclass stores a curses color-pair id for the state
# badge. Keep the feature boundary independent of `ui.*` imports to avoid
# feature<->UI cycles while the wider state/view model is still being split.
_PAIR_DLG_CYAN = 35
_PAIR_DLG_OK = 36
_PAIR_DLG_ERR = 37
_PAIR_DLG_WARN = 38
_PAIR_DLG_MAGENTA = 39


def ensure_action_menu_load_ids(menu: ActionMenu) -> None:
    base = f"action-menu:{id(menu)}:{id(menu.target_path)}"
    if not menu.state_load_id:
        menu.state_load_id = f"{base}:state"
    if not menu.inventory_load_id:
        menu.inventory_load_id = f"{base}:inventory"
    if not menu.tree_load_id:
        menu.tree_load_id = f"{base}:tree"
    if not menu.commits_load_id:
        menu.commits_load_id = f"{base}:commits"


def action_menu_load_ids(menu: ActionMenu) -> List[str]:
    ensure_action_menu_load_ids(menu)
    return [
        menu.state_load_id,
        menu.inventory_load_id,
        menu.tree_load_id,
        menu.commits_load_id,
    ]


def count_label(label: str, count: int, loading: bool = False) -> str:
    if loading:
        return f"{label} (...)"
    return f"{label} ({count})"


def build_main_items(
        branch_meta,
        stash_count: int = 0,
        remote_count: int = 0,
        inventory_loading: bool = False,
) -> List[ActionMenuItem]:
    has_origin = branch_meta["has_origin"]
    upstream = branch_meta["upstream"]
    merging = branch_meta["merging"]
    items: List[ActionMenuItem] = []
    if merging:
        items.append(ActionMenuItem(
            id="safe_merge", label="resolve merge conflicts…",
            enabled=True))
    items += [
        ActionMenuItem(
            id="fetch", label="fetch (all branches)",
            enabled=has_origin,
            reason="" if has_origin else "no origin"),
        ActionMenuItem(
            id="pull", label="pull",
            enabled=has_origin and upstream is not None and not merging,
            reason=("merging" if merging
                    else ("no upstream" if upstream is None
                          else ("" if has_origin else "no origin")))),
        ActionMenuItem(
            id="push", label="push",
            enabled=has_origin,
            reason="" if has_origin else "no origin"),
        ActionMenuItem(
            id="branch_submenu", label="branch",
            enabled=not merging,
            reason="" if not merging else "merging",
            has_submenu=True),
        ActionMenuItem(
            id="actions_submenu", label="actions",
            enabled=True, has_submenu=True),
        ActionMenuItem(
            id="stashes_submenu",
            label=count_label("stashes", stash_count, inventory_loading),
            enabled=True, has_submenu=True),
        ActionMenuItem(
            id="remotes_submenu",
            label=count_label("remotes", remote_count, inventory_loading),
            enabled=True, has_submenu=True),
    ]
    return items


def back_item() -> ActionMenuItem:
    return ActionMenuItem(id="back", label="back",
                          enabled=True, is_back=True)


def build_stash_apply_items(ref: str) -> List[ActionMenuItem]:
    """Per-stash submenu items.

    Only `apply` is exposed here. Dropping or popping a stash would remove
    saved work, so those actions stay out of Idlegit's safe action surface.
    """
    return [
        back_item(),
        ActionMenuItem(
            id=f"stash_apply:{ref}",
            label=f"apply {ref}",
            enabled=True,
        ),
    ]


def in_submenu(menu: ActionMenu) -> bool:
    return bool(menu.submenu_stack)


def current_items(menu: ActionMenu) -> List[ActionMenuItem]:
    if menu.submenu_stack:
        return menu.submenu_stack[-1].items
    return menu.items


def current_selected(menu: ActionMenu) -> int:
    if menu.submenu_stack:
        return menu.submenu_stack[-1].selected
    return menu.selected


def set_current_selected(menu: ActionMenu, value: int) -> None:
    if menu.submenu_stack:
        menu.submenu_stack[-1].selected = value
        return
    menu.selected = value


def breadcrumb_segments(menu: ActionMenu) -> List[str]:
    segs = ["repo"]
    for frame in menu.submenu_stack:
        segs.append(frame.label)
    return segs


def first_actionable_index(items: List[ActionMenuItem]) -> int:
    for i, item in enumerate(items):
        if item.enabled and not item.is_back and not item.is_separator:
            return i
    for i, item in enumerate(items):
        if item.enabled and not item.is_separator:
            return i
    return 0


def step_selection(
        items: List[ActionMenuItem],
        current: int,
        direction: int,
        *,
        no_wrap: bool = False,
) -> int:
    n = len(items)
    if n == 0:
        return current
    idx = current
    for _ in range(n):
        idx = idx + direction
        if no_wrap and (idx < 0 or idx >= n):
            return current
        idx %= n
        item = items[idx]
        if item.is_separator:
            continue
        return idx
    return current


def filtered_tree(menu: ActionMenu) -> List[FileEntry]:
    if not menu.tree_filter:
        return menu.tree_files
    needle = menu.tree_filter.lower()
    return [entry for entry in menu.tree_files
            if needle in entry.path.lower()]


def filtered_commits(menu: ActionMenu) -> List[CommitEntry]:
    if not menu.commits_filter:
        return menu.commits_full
    needle = menu.commits_filter.lower()
    return [
        commit for commit in menu.commits_full
        if needle in commit.subject.lower() or needle in commit.sha.lower()
    ]


def push_submenu(
        menu: ActionMenu,
        name: str,
        label: str,
        items: List[ActionMenuItem],
) -> None:
    menu.submenu_stack.append(ActionSubmenuFrame(
        name=name,
        label=label,
        items=items,
        selected=first_actionable_index(items),
    ))


def pop_submenu(menu: ActionMenu) -> None:
    if menu.submenu_stack:
        menu.submenu_stack.pop()


def reset_to_main_menu(menu: ActionMenu) -> None:
    menu.pane_focus = False
    menu.submenu_stack.clear()
    menu.selected = first_actionable_index(menu.items)


def exit_to_parent(menu: ActionMenu) -> None:
    if not menu.submenu_stack:
        return
    leaving = menu.submenu_stack[-1]
    pop_submenu(menu)
    parent_items = current_items(menu)
    target_id = ""
    if leaving.name in ("branch", "actions", "stashes", "remotes"):
        target_id = f"{leaving.name}_submenu"
    elif leaving.name.startswith("stash:"):
        target_id = f"stash:{leaving.name.split(':', 1)[1]}"
    if not target_id:
        return
    for i, item in enumerate(parent_items):
        if item.id == target_id:
            set_current_selected(menu, i)
            return


def enter_submenu_for(menu: ActionMenu, item: ActionMenuItem) -> None:
    meta = menu.cached_meta or {}
    if item.id == "branch_submenu":
        push_submenu(menu, "branch", "branch", build_branch_items(meta))
        return
    if item.id == "actions_submenu":
        push_submenu(menu, "actions", "actions", build_actions_items(meta))
        return
    if item.id == "stashes_submenu":
        push_submenu(menu, "stashes", "stashes",
                     build_stashes_items(menu.stashes))
        return
    if item.id == "remotes_submenu":
        push_submenu(menu, "remotes", "remotes",
                     build_remotes_items(menu.remotes_list))
        return
    if item.id.startswith("stash:"):
        ref = item.id.split(":", 1)[1]
        push_submenu(menu, f"stash:{ref}", ref,
                     build_stash_apply_items(ref))


def build_branch_items(branch_meta) -> List[ActionMenuItem]:
    has_origin = branch_meta["has_origin"]
    merging = branch_meta["merging"]
    branch = branch_meta.get("branch") or ""
    detached = (not branch) or branch == "(detached)"
    return [
        back_item(),
        ActionMenuItem(
            id="switch_branch", label="switch branch…",
            enabled=not merging,
            reason="" if not merging else "merging"),
        ActionMenuItem(
            id="checkout_remote_branch",
            label="checkout remote branch…",
            enabled=not merging,
            reason="" if not merging else "merging"),
        ActionMenuItem(
            id="branch_from_head", label="save HEAD to new branch…",
            enabled=detached and not merging,
            reason=("merging" if merging
                    else ("" if detached else "already on a branch"))),
        ActionMenuItem(
            id="merge_branch", label="merge in branch…",
            enabled=(not detached) and (not merging),
            reason=("merging" if merging
                    else ("detached HEAD" if detached else ""))),
        ActionMenuItem(
            id="safe_merge",
            label=("resolve merge conflicts…" if merging
                   else "safe-merge in branch…"),
            enabled=merging or (not detached),
            reason=("" if (merging or not detached) else "detached HEAD")),
        ActionMenuItem(
            id="rename_branch", label="rename branch…",
            enabled=(not detached) and (not merging),
            reason=("merging" if merging
                    else ("detached HEAD" if detached else ""))),
        ActionMenuItem(
            id="set_upstream", label="set upstream…",
            enabled=(not detached) and (not merging) and has_origin,
            reason=("merging" if merging
                    else ("detached HEAD" if detached
                          else ("" if has_origin else "no origin")))),
    ]


def build_actions_items(branch_meta) -> List[ActionMenuItem]:
    ahead = branch_meta.get("ahead", 0) if branch_meta else 0
    return [
        back_item(),
        ActionMenuItem(
            id="soft_reset",
            label=f"soft reset ({ahead} unpushed)…",
            enabled=ahead > 0,
            reason="" if ahead > 0 else "no unpushed commits"),
        ActionMenuItem(
            id="run_workflow", label="run a workflow…",
            enabled=branch_meta["has_any_workflow"]
            if branch_meta else False,
            reason=(branch_meta["run_workflow_reason"]
                    if branch_meta else "")),
    ]


def build_stashes_items(
        stashes: List[Tuple[str, str]],
) -> List[ActionMenuItem]:
    items: List[ActionMenuItem] = [
        back_item(),
        ActionMenuItem(id="stash_create", label="new stash"),
        ActionMenuItem(id="sep_after_new", label="",
                       enabled=False, is_separator=True),
    ]
    for ref, msg in stashes:
        label = f"{ref}  {msg}" if msg else ref
        items.append(ActionMenuItem(
            id=f"stash:{ref}", label=label, has_submenu=True))
    if not stashes:
        items.append(ActionMenuItem(
            id="stash_empty", label="(no stashes yet)",
            enabled=False))
    return items


def build_remotes_items(
        remotes: List[Tuple[str, str]],
) -> List[ActionMenuItem]:
    items: List[ActionMenuItem] = [
        back_item(),
        ActionMenuItem(id="new_remote", label="new remote"),
        ActionMenuItem(id="sep_after_new", label="",
                       enabled=False, is_separator=True),
    ]
    for name, url in remotes:
        label = f"{name}: {url}" if url else name
        items.append(ActionMenuItem(
            id=f"remote:{name}", label=label))
    if not remotes:
        items.append(ActionMenuItem(
            id="remote_empty", label="(no remotes configured)",
            enabled=False))
    return items


def state_label_for(branch_meta):
    if branch_meta["merging"]:
        return "merging", _PAIR_DLG_ERR
    if branch_meta["ahead"] > 0 and branch_meta["behind"] > 0:
        return "diverged", _PAIR_DLG_ERR
    if branch_meta["dirty"]:
        return "dirty", _PAIR_DLG_WARN
    if branch_meta["behind"] > 0:
        return "behind", _PAIR_DLG_MAGENTA
    if branch_meta["ahead"] > 0:
        return "ahead", _PAIR_DLG_CYAN
    if branch_meta["upstream"] is None:
        return "no upstream", 0
    return "clean", _PAIR_DLG_OK


def initial_meta_from_cache(
        target_repo: Optional[Repo],
        target_child: Optional[ChildRef],
        workflows_repo: Optional[Repo],
) -> dict:
    if target_repo is not None:
        branch = target_repo.branch
        upstream = target_repo.upstream
        ahead = target_repo.ahead
        behind = target_repo.behind
        merging = target_repo.merging
        dirty = target_repo.is_dirty
        has_origin = bool(target_repo.remote_url)
    elif target_child is not None:
        branch = target_child.branch
        upstream = target_child.upstream
        ahead = target_child.ahead
        behind = target_child.behind
        merging = target_child.merging
        dirty = target_child.dirty
        has_origin = True
    else:
        branch = ""
        upstream = None
        ahead = 0
        behind = 0
        merging = False
        dirty = False
        has_origin = False
    return {
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "merging": merging,
        "dirty": dirty,
        "has_origin": has_origin,
        "has_any_workflow": has_any_workflow(workflows_repo),
        "run_workflow_reason": run_workflow_reason(workflows_repo),
    }


def has_any_workflow(workflows_repo: Optional[Repo]) -> bool:
    if workflows_repo is None or not gh_available():
        return False
    slug = parse_github_slug(workflows_repo.remote_url_raw)
    return bool(slug) and bool(workflows_repo.workflows)


def run_workflow_reason(workflows_repo: Optional[Repo]) -> str:
    if has_any_workflow(workflows_repo):
        return ""
    if workflows_repo is None or not gh_available():
        return "gh CLI / repo unavailable"
    if not parse_github_slug(workflows_repo.remote_url_raw):
        return "no github remote"
    return "no workflows in this repo"
