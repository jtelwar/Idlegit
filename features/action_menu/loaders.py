"""Worker-owned action-menu data loaders."""
from __future__ import annotations

from typing import Optional

from core.git_ops import (
    list_remotes,
    list_stashes,
    load_commits,
    query_target_state,
    query_working_tree,
)
from core.runtime.jobs import JobSpec, submit_job
from core.runtime.threads import create_job_thread
from core.state.app import State
from core.state.action_menu import ActionMenu
from core.state.repos import Repo

from .projection import (
    build_actions_items,
    build_branch_items,
    build_main_items,
    build_remotes_items,
    build_stashes_items,
    ensure_action_menu_load_ids,
    has_any_workflow,
    run_workflow_reason,
    state_label_for,
)


COMMITS_PAGE = 30


def kick_off_action_menu_loaders(
        state: State,
        menu: ActionMenu,
        workflows_repo: Optional[Repo],
) -> None:
    """Start every initial action-menu loader after the modal projection exists."""
    kick_off_action_menu_state_load(state, menu, workflows_repo)
    kick_off_action_menu_inventory_load(state, menu)
    kick_off_action_menu_tree_load(state, menu)
    kick_off_action_menu_initial_commits(state, menu)


def kick_off_action_menu_state_load(
        state: State,
        menu: ActionMenu,
        workflows_repo: Optional[Repo],
) -> None:
    path = menu.target_path
    ensure_action_menu_load_ids(menu)
    state.view_loads.create(menu.state_load_id)

    def worker(_job) -> None:
        try:
            if _load_cancelled(state, menu.state_load_id):
                return
            target_state = query_target_state(path)
            if _load_cancelled(state, menu.state_load_id):
                return
            meta = {
                "branch": target_state.branch,
                "upstream": target_state.upstream,
                "ahead": target_state.ahead,
                "behind": target_state.behind,
                "merging": target_state.merging,
                "dirty": target_state.dirty,
                "has_origin": target_state.has_origin,
                "has_any_workflow": has_any_workflow(workflows_repo),
                "run_workflow_reason": run_workflow_reason(workflows_repo),
            }
            menu.branch = target_state.branch
            menu.upstream = target_state.upstream
            menu.ahead = target_state.ahead
            menu.behind = target_state.behind
            menu.state_label, menu.state_pair = state_label_for(meta)
            menu.cached_meta = meta
            menu.items = build_main_items(
                meta,
                stash_count=menu.stash_count,
                remote_count=menu.remote_count,
                inventory_loading=_inventory_loading(state, menu),
            )
            if menu.selected < len(menu.items) and not menu.items[
                    menu.selected].enabled:
                menu.selected = _first_actionable_index(menu.items)
            if menu.submenu_stack:
                top = menu.submenu_stack[-1]
                if top.name == "branch":
                    top.items = build_branch_items(meta)
                elif top.name == "actions":
                    top.items = build_actions_items(meta)
                if top.selected < len(top.items):
                    selected = top.items[top.selected]
                    if not selected.enabled or selected.is_separator:
                        top.selected = _first_actionable_index(top.items)
        finally:
            _finish_load(state, menu.state_load_id)

    _submit_action_menu_job(
        state,
        menu,
        "action-menu-state-load",
        "load action state",
        worker,
        menu.state_load_id,
    )


def kick_off_action_menu_inventory_load(
        state: State,
        menu: ActionMenu,
) -> None:
    path = menu.target_path
    ensure_action_menu_load_ids(menu)
    state.view_loads.create(menu.inventory_load_id)

    def worker(_job) -> None:
        try:
            if _load_cancelled(state, menu.inventory_load_id):
                return
            stashes = list_stashes(path)
            remotes = list_remotes(path)
            if _load_cancelled(state, menu.inventory_load_id):
                return
            menu.stashes = stashes
            menu.stash_count = len(stashes)
            menu.remotes_list = remotes
            menu.remote_count = len(remotes)
            menu.items = build_main_items(
                menu.cached_meta,
                stash_count=menu.stash_count,
                remote_count=menu.remote_count,
                inventory_loading=False,
            )
            if menu.submenu_stack:
                top = menu.submenu_stack[-1]
                if top.name == "stashes":
                    top.items = build_stashes_items(menu.stashes)
                elif top.name == "remotes":
                    top.items = build_remotes_items(menu.remotes_list)
        finally:
            _finish_load(state, menu.inventory_load_id)

    started = _submit_action_menu_job(
        state,
        menu,
        "action-menu-inventory-load",
        "load inventory",
        worker,
        menu.inventory_load_id,
    )
    if not started:
        menu.items = build_main_items(
            menu.cached_meta,
            stash_count=menu.stash_count,
            remote_count=menu.remote_count,
            inventory_loading=False,
        )


def kick_off_action_menu_tree_load(state: State, menu: ActionMenu) -> None:
    path = menu.target_path
    ensure_action_menu_load_ids(menu)
    state.view_loads.create(menu.tree_load_id)

    def worker(_job) -> None:
        try:
            if _load_cancelled(state, menu.tree_load_id):
                return
            files = query_working_tree(path)
            if _load_cancelled(state, menu.tree_load_id):
                return
            menu.tree_files = files
        finally:
            _finish_load(state, menu.tree_load_id)

    _submit_action_menu_job(
        state,
        menu,
        "action-menu-tree-load",
        "load working tree",
        worker,
        menu.tree_load_id,
    )


def kick_off_action_menu_initial_commits(
        state: State,
        menu: ActionMenu,
) -> None:
    path = menu.target_path
    ensure_action_menu_load_ids(menu)
    state.view_loads.create(menu.commits_load_id)

    def worker(_job) -> None:
        try:
            if _load_cancelled(state, menu.commits_load_id):
                return
            page, exhausted = load_commits(path, 0, COMMITS_PAGE)
            if _load_cancelled(state, menu.commits_load_id):
                return
            menu.commits_full = page
            menu.commits_exhausted = exhausted
        finally:
            _finish_load(state, menu.commits_load_id)

    _submit_action_menu_job(
        state,
        menu,
        "action-menu-commits-load",
        "load commits",
        worker,
        menu.commits_load_id,
    )


def kick_off_action_menu_commits_page(
        state: State,
        menu: ActionMenu,
) -> None:
    ensure_action_menu_load_ids(menu)
    if _commits_loading(state, menu) or menu.commits_exhausted:
        return
    if _load_cancelled(state, menu.commits_load_id):
        return
    state.view_loads.create(menu.commits_load_id)
    skip = len(menu.commits_full)
    path = menu.target_path

    def worker(_job) -> None:
        try:
            if _load_cancelled(state, menu.commits_load_id):
                return
            page, exhausted = load_commits(path, skip, COMMITS_PAGE)
            if _load_cancelled(state, menu.commits_load_id):
                return
            menu.commits_full.extend(page)
            menu.commits_exhausted = exhausted
        finally:
            _finish_load(state, menu.commits_load_id)

    _submit_action_menu_job(
        state,
        menu,
        "action-menu-commits-page",
        "load more commits",
        worker,
        menu.commits_load_id,
    )


def _load_cancelled(state: State, load_id: str) -> bool:
    return bool(load_id and state.view_loads.is_cancelled(load_id))


def _finish_load(state: State, load_id: str) -> None:
    if load_id:
        state.view_loads.finish(load_id, [])


def _inventory_loading(state: State, menu: ActionMenu) -> bool:
    if not menu.inventory_load_id:
        return False
    record = state.view_loads.get(menu.inventory_load_id)
    return bool(record is not None and record.loading)


def _commits_loading(state: State, menu: ActionMenu) -> bool:
    if not menu.commits_load_id:
        return False
    record = state.view_loads.get(menu.commits_load_id)
    return bool(record is not None and record.loading)


def _first_actionable_index(items) -> int:
    for i, item in enumerate(items):
        if item.enabled and not item.is_back and not item.is_separator:
            return i
    for i, item in enumerate(items):
        if item.enabled and not item.is_separator:
            return i
    return 0


def _submit_action_menu_job(
        state: State,
        menu: ActionMenu,
        kind: str,
        label: str,
        worker,
        load_id: str,
) -> bool:
    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind=kind,
            label=f"{menu.target_label}: {label}",
            local_mutation=False,
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        state.view_loads.fail(load_id, job.message)
        return False
    return True
