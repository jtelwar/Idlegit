"""Action-menu session lifecycle."""
from __future__ import annotations

from typing import Optional

from core.state.app import State
from core.state.action_menu import ActionMenu
from core.state.repos import ChildRef, Repo

from .loaders import kick_off_action_menu_loaders
from .projection import (
    action_menu_load_ids,
    build_main_items,
    ensure_action_menu_load_ids,
    first_actionable_index,
    initial_meta_from_cache,
    state_label_for,
)


def open_action_menu(state: State) -> None:
    if state.on_workspace_row:
        return

    cur_repo = state.current_repo
    cur_child = state.current_child

    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    if cur_repo is not None:
        label = cur_repo.display_name
        target_path = cur_repo.path
        target_repo = cur_repo
    elif cur_child is not None and cur_child[1].kind == "submodule":
        parent, child = cur_child
        label = f"↳ {child.repo.display_name} in {parent.display_name}"
        target_path = child.nested_path
        target_parent = parent
        target_child = child
    else:
        return

    workflows_repo = target_repo or (target_child.repo if target_child else None)
    meta = initial_meta_from_cache(target_repo, target_child, workflows_repo)
    items = build_main_items(meta, inventory_loading=True)
    state_label, state_pair = state_label_for(meta)

    menu = ActionMenu(
        target_label=label,
        target_path=target_path,
        target_repo=target_repo,
        target_parent=target_parent,
        target_child=target_child,
        branch=meta["branch"],
        upstream=meta["upstream"],
        ahead=meta["ahead"],
        behind=meta["behind"],
        state_label=state_label,
        state_pair=state_pair,
        items=items,
        selected=first_actionable_index(items),
        cached_meta=meta,
        stash_count=0,
        stashes=[],
        remotes_list=[],
        remote_count=0,
    )
    state.action_menu = menu
    ensure_action_menu_load_ids(menu)

    kick_off_action_menu_loaders(state, menu, workflows_repo)


def close_action_menu(state: State) -> None:
    menu = state.action_menu
    if menu is None:
        return
    state.view_loads.remove_many(action_menu_load_ids(menu))
    state.action_menu = None
