"""Action-menu action planning and worker dispatch."""
from __future__ import annotations

import curses
from dataclasses import dataclass
from typing import Optional

from core.git_ops import merge_head_sha
from core.state.app import State
from core.state.remotes import RemoteRow
from core.state.action_menu import ActionMenu, ActionMenuItem
from core.workers import (
    kick_off_action,
    kick_off_remote_changes,
    kick_off_safe_merge,
)

from .projection import (
    build_main_items,
    build_remotes_items,
    current_items,
    current_selected,
    enter_submenu_for,
    exit_to_parent,
    filtered_commits,
    filtered_tree,
    first_actionable_index,
    in_submenu,
    reset_to_main_menu,
    set_current_selected,
    step_selection,
)


@dataclass(frozen=True)
class ActionMenuEffect:
    """UI-side effect requested by feature-owned action planning."""

    kind: str = "none"
    mode: str = ""
    file_path: str = ""
    untracked: bool = False
    sha: str = ""
    subject: str = ""


def no_effect() -> ActionMenuEffect:
    return ActionMenuEffect()


def close_effect() -> ActionMenuEffect:
    return ActionMenuEffect("close")


def open_effect(kind: str, mode: str = "") -> ActionMenuEffect:
    return ActionMenuEffect(kind, mode)


def prefetch_commits_effect() -> ActionMenuEffect:
    return ActionMenuEffect("prefetch_commits")


def open_diff_effect(file_path: str, untracked: bool) -> ActionMenuEffect:
    return ActionMenuEffect(
        kind="diff_viewer",
        file_path=file_path,
        untracked=untracked,
    )


def open_commit_effect(sha: str, subject: str) -> ActionMenuEffect:
    return ActionMenuEffect(
        kind="commit_view",
        sha=sha,
        subject=subject,
    )


def begin_rename_remote(menu: ActionMenu, name: str, url: str) -> None:
    menu.edit_field = "rename_remote"
    menu.edit_typed = name
    menu.edit_cursor = len(name)
    menu.edit_pre_value = name
    menu.edit_target_id = f"remote:{name}"
    menu.edit_extra = {"url": url}


def begin_set_url_remote(menu: ActionMenu, name: str, url: str) -> None:
    menu.edit_field = "set_url_remote"
    menu.edit_typed = url
    menu.edit_cursor = len(url)
    menu.edit_pre_value = url
    menu.edit_target_id = f"remote:{name}"
    menu.edit_extra = {"name": name, "old_url": url}


def begin_new_remote_name(menu: ActionMenu) -> None:
    menu.edit_field = "add_remote_name"
    menu.edit_typed = ""
    menu.edit_cursor = 0
    menu.edit_pre_value = ""
    menu.edit_target_id = "new_remote"
    menu.edit_extra = {}


def begin_new_remote_url(menu: ActionMenu, name: str) -> None:
    menu.edit_field = "add_remote_url"
    menu.edit_typed = ""
    menu.edit_cursor = 0
    menu.edit_pre_value = ""
    menu.edit_target_id = "new_remote"
    menu.edit_extra = {"name": name}


def cancel_inline_edit(menu: ActionMenu) -> None:
    menu.edit_field = ""
    menu.edit_typed = ""
    menu.edit_cursor = 0
    menu.edit_pre_value = ""
    menu.edit_target_id = ""
    menu.edit_extra = {}


def request_confirm(
        menu: ActionMenu,
        message: str,
        action: str,
        args: dict[str, str],
) -> None:
    menu.confirm_message = message
    menu.confirm_action = action
    menu.confirm_args = dict(args)


def clear_confirm(menu: ActionMenu) -> None:
    menu.confirm_message = ""
    menu.confirm_action = ""
    menu.confirm_args = {}


def begin_remote_rename_for_item(menu: ActionMenu, item: ActionMenuItem) -> None:
    if not item.id.startswith("remote:"):
        return
    name = item.id.split(":", 1)[1]
    begin_rename_remote(menu, name, _remote_url(menu, name))


def request_remote_delete_for_item(
        menu: ActionMenu,
        item: ActionMenuItem,
) -> None:
    if not item.id.startswith("remote:"):
        return
    name = item.id.split(":", 1)[1]
    request_confirm(
        menu,
        f"Remove remote {name}? [y/N]",
        "remove_remote",
        {"name": name, "url": _remote_url(menu, name)},
    )


def handle_action_menu_confirm_key(
        state: State,
        menu: ActionMenu,
        key: int,
) -> ActionMenuEffect:
    if key in (ord("y"), ord("Y")):
        apply_remote_op(state, menu)
        clear_confirm(menu)
        return no_effect()
    if key in (ord("n"), ord("N"), 27):
        clear_confirm(menu)
    return no_effect()


def handle_action_menu_inline_edit_key(
        menu: ActionMenu,
        key: int,
) -> ActionMenuEffect:
    """Apply one keypress to the action-menu inline edit state."""
    field = menu.edit_field
    text = menu.edit_typed
    cur = max(0, min(menu.edit_cursor, len(text)))
    if key == 27:
        cancel_inline_edit(menu)
        return no_effect()
    if key in (10, 13, curses.KEY_ENTER):
        committed = text.strip()
        if not committed:
            return no_effect()
        if field == "rename_remote":
            old = menu.edit_pre_value
            url = menu.edit_extra.get("url", "")
            if committed == old:
                cancel_inline_edit(menu)
                return no_effect()
            if committed.startswith("-"):
                return no_effect()
            cancel_inline_edit(menu)
            request_confirm(
                menu,
                f"Rename remote {old} → {committed}? [y/N]",
                "rename_remote",
                {"old": old, "new": committed, "url": url},
            )
            return no_effect()
        if field == "set_url_remote":
            name = menu.edit_extra.get("name", "")
            old_url = menu.edit_extra.get("old_url", "")
            if committed == old_url:
                cancel_inline_edit(menu)
                return no_effect()
            if committed.startswith("-"):
                return no_effect()
            cancel_inline_edit(menu)
            request_confirm(
                menu,
                f"Set {name} URL → {committed}? [y/N]",
                "set_url_remote",
                {"name": name, "url": committed, "old_url": old_url},
            )
            return no_effect()
        if field == "add_remote_name":
            if committed.startswith("-"):
                return no_effect()
            if any(name == committed for name, _ in menu.remotes_list):
                return no_effect()
            begin_new_remote_url(menu, committed)
            return no_effect()
        if field == "add_remote_url":
            if committed.startswith("-"):
                return no_effect()
            name = menu.edit_extra.get("name", "")
            cancel_inline_edit(menu)
            request_confirm(
                menu,
                f"Add remote {name} → {committed}? [y/N]",
                "add_remote",
                {"name": name, "url": committed},
            )
            return no_effect()
        return no_effect()
    if key == curses.KEY_LEFT:
        menu.edit_cursor = max(0, cur - 1)
        return no_effect()
    if key == curses.KEY_RIGHT:
        menu.edit_cursor = min(len(text), cur + 1)
        return no_effect()
    if key in (curses.KEY_HOME, 1):
        menu.edit_cursor = 0
        return no_effect()
    if key in (curses.KEY_END, 5):
        menu.edit_cursor = len(text)
        return no_effect()
    if key in (curses.KEY_BACKSPACE, 127, 8):
        if cur > 0:
            menu.edit_typed = text[: cur - 1] + text[cur:]
            menu.edit_cursor = cur - 1
        return no_effect()
    if key == curses.KEY_DC:
        if cur < len(text):
            menu.edit_typed = text[:cur] + text[cur + 1:]
        return no_effect()
    if 32 <= key < 127:
        menu.edit_typed = text[:cur] + chr(key) + text[cur:]
        menu.edit_cursor = cur + 1
    return no_effect()


def handle_action_menu_key_intent(
        state: State,
        menu: ActionMenu,
        key: int,
) -> ActionMenuEffect:
    if menu.confirm_message:
        return handle_action_menu_confirm_key(state, menu, key)
    if menu.edit_field:
        return handle_action_menu_inline_edit_key(menu, key)
    if key == 27:
        return close_effect()
    if key == curses.KEY_HOME:
        reset_to_main_menu(menu)
        return no_effect()
    if menu.pane_focus:
        return handle_action_menu_pane_key(menu, key)
    return handle_action_menu_list_key(state, menu, key)


def handle_action_menu_list_key(
        state: State,
        menu: ActionMenu,
        key: int,
) -> ActionMenuEffect:
    if key == 9:
        return close_effect()

    items = current_items(menu)
    selected = current_selected(menu)
    active_submenu = in_submenu(menu)

    if key == curses.KEY_LEFT and active_submenu:
        exit_to_parent(menu)
        return no_effect()

    if (key == curses.KEY_RIGHT and items
            and 0 <= selected < len(items)
            and items[selected].has_submenu
            and items[selected].enabled):
        enter_submenu_for(menu, items[selected])
        return no_effect()

    if (key in (ord("r"), ord("R")) and items
            and 0 <= selected < len(items)
            and items[selected].id.startswith("remote:")):
        begin_remote_rename_for_item(menu, items[selected])
        return no_effect()
    if (key in (ord("d"), ord("D")) and items
            and 0 <= selected < len(items)
            and items[selected].id.startswith("remote:")):
        request_remote_delete_for_item(menu, items[selected])
        return no_effect()

    if key == curses.KEY_UP and items:
        set_current_selected(
            menu,
            step_selection(items, selected, -1),
        )
        return no_effect()
    if key == curses.KEY_DOWN and items:
        next_idx = step_selection(items, selected, +1, no_wrap=True)
        if next_idx == selected and not active_submenu:
            menu.pane_focus = True
            return no_effect()
        set_current_selected(menu, next_idx)
        return no_effect()
    if key in (10, 13, curses.KEY_ENTER) and items:
        item = items[selected]
        if not item.enabled or item.is_separator:
            return no_effect()
        if item.is_back:
            exit_to_parent(menu)
            return no_effect()
        if item.has_submenu:
            enter_submenu_for(menu, item)
            return no_effect()
        return dispatch_action_menu_item(state, menu, item)
    return no_effect()


def handle_action_menu_pane_key(
        menu: ActionMenu,
        key: int,
) -> ActionMenuEffect:
    if key == curses.KEY_LEFT:
        menu.pane_tab = "tree" if menu.pane_tab == "commits" else "commits"
        return maybe_prefetch_commits_effect(menu)
    if key == curses.KEY_RIGHT:
        menu.pane_tab = "commits" if menu.pane_tab == "tree" else "tree"
        return maybe_prefetch_commits_effect(menu)

    if menu.pane_tab == "tree":
        effect = handle_action_menu_tree_key(menu, key)
    else:
        effect = handle_action_menu_commits_key(menu, key)
    if effect.kind != "none":
        return effect
    return maybe_prefetch_commits_effect(menu)


def handle_action_menu_tree_key(
        menu: ActionMenu,
        key: int,
) -> ActionMenuEffect:
    files = filtered_tree(menu)
    max_idx = len(files)
    on_filter = menu.tree_selected == 0

    if key == curses.KEY_UP:
        if on_filter:
            menu.pane_focus = False
            return no_effect()
        menu.tree_selected -= 1
        return no_effect()
    if key == curses.KEY_DOWN:
        if menu.tree_selected < max_idx:
            menu.tree_selected += 1
        return no_effect()
    if key == 9 and not on_filter:
        idx = menu.tree_selected - 1
        if 0 <= idx < len(files):
            entry = files[idx]
            return open_diff_effect(entry.path, entry.untracked)
        return no_effect()
    if on_filter and is_typing_key(key):
        if key in (curses.KEY_BACKSPACE, 127, 8):
            menu.tree_filter = menu.tree_filter[:-1]
        else:
            menu.tree_filter += chr(key)
    return no_effect()


def handle_action_menu_commits_key(
        menu: ActionMenu,
        key: int,
) -> ActionMenuEffect:
    commits = filtered_commits(menu)
    max_idx = len(commits)
    on_filter = menu.commits_selected == 0

    if key == curses.KEY_UP:
        if on_filter:
            menu.pane_focus = False
            return no_effect()
        menu.commits_selected -= 1
        return no_effect()
    if key == curses.KEY_DOWN:
        if menu.commits_selected < max_idx:
            menu.commits_selected += 1
        return no_effect()
    if key == 9 and not on_filter:
        idx = menu.commits_selected - 1
        if 0 <= idx < len(commits):
            commit = commits[idx]
            return open_commit_effect(commit.sha, commit.subject)
        return no_effect()
    if on_filter and is_typing_key(key):
        if key in (curses.KEY_BACKSPACE, 127, 8):
            menu.commits_filter = menu.commits_filter[:-1]
        else:
            menu.commits_filter += chr(key)
    return no_effect()


def maybe_prefetch_commits_effect(menu: ActionMenu) -> ActionMenuEffect:
    if menu.pane_tab != "commits" or not menu.pane_focus:
        return no_effect()
    visible = filtered_commits(menu)
    list_idx = max(0, menu.commits_selected - 1)
    if list_idx >= len(visible) - 5:
        return prefetch_commits_effect()
    return no_effect()


def is_typing_key(key: int) -> bool:
    if key in (curses.KEY_BACKSPACE, 127, 8):
        return True
    return 32 <= key <= 126


def apply_remote_op(state: State, menu: ActionMenu) -> None:
    args = menu.confirm_args
    row: Optional[RemoteRow] = None
    if menu.confirm_action == "rename_remote":
        old = args.get("old", "")
        new = args.get("new", "")
        url = args.get("url", "")
        row = RemoteRow(
            original_name=old,
            original_url=url,
            name=new,
            url=url,
            is_new=False,
        )
    elif menu.confirm_action == "set_url_remote":
        name = args.get("name", "")
        new_url = args.get("url", "")
        old_url = args.get("old_url", "")
        row = RemoteRow(
            original_name=name,
            original_url=old_url,
            name=name,
            url=new_url,
            is_new=False,
        )
    elif menu.confirm_action == "remove_remote":
        old = args.get("name", "")
        url = args.get("url", "")
        row = RemoteRow(
            original_name=old,
            original_url=url,
            name=old,
            url=url,
            to_delete=True,
            is_new=False,
        )
    elif menu.confirm_action == "add_remote":
        new_name = args.get("name", "")
        new_url = args.get("url", "")
        row = RemoteRow(
            original_name="",
            original_url="",
            name=new_name,
            url=new_url,
            to_delete=False,
            is_new=True,
        )
    if row is None:
        return
    kick_off_remote_changes(
        state,
        [row],
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
    )
    update_cached_remotes(menu)


def update_cached_remotes(menu: ActionMenu) -> None:
    args = menu.confirm_args
    if menu.confirm_action == "rename_remote":
        old = args.get("old", "")
        new = args.get("new", "")
        menu.remotes_list = [
            (new if name == old else name, url)
            for name, url in menu.remotes_list
        ]
    elif menu.confirm_action == "set_url_remote":
        name = args.get("name", "")
        new_url = args.get("url", "")
        menu.remotes_list = [
            (remote_name, new_url if remote_name == name else url)
            for remote_name, url in menu.remotes_list
        ]
    elif menu.confirm_action == "remove_remote":
        gone = args.get("name", "")
        menu.remotes_list = [
            (name, url) for name, url in menu.remotes_list
            if name != gone
        ]
        menu.remote_count = len(menu.remotes_list)
    elif menu.confirm_action == "add_remote":
        new_name = args.get("name", "")
        new_url = args.get("url", "")
        menu.remotes_list = [*menu.remotes_list, (new_name, new_url)]
        menu.remote_count = len(menu.remotes_list)
    if menu.submenu_stack and menu.submenu_stack[-1].name == "remotes":
        top = menu.submenu_stack[-1]
        top.items = build_remotes_items(menu.remotes_list)
        if top.selected >= len(top.items):
            top.selected = first_actionable_index(top.items)
    menu.items = build_main_items(
        menu.cached_meta or {},
        stash_count=menu.stash_count,
        remote_count=menu.remote_count,
        inventory_loading=False,
    )


def dispatch_action_menu_item(
        state: State,
        menu: ActionMenu,
        item: ActionMenuItem,
) -> ActionMenuEffect:
    if item.id == "switch_branch":
        return open_effect("branch_picker")
    if item.id == "checkout_remote_branch":
        return open_effect("remote_branch_picker")
    if item.id == "merge_branch":
        return open_effect("branch_picker", "merge")
    if item.id == "safe_merge":
        if merge_head_sha(menu.target_path) is not None:
            kick_off_safe_merge(
                state,
                target_label=menu.target_label,
                target_path=menu.target_path,
                target_repo=menu.target_repo,
                target_parent=menu.target_parent,
                merge_ref="",
            )
            return close_effect()
        return open_effect("branch_picker", "safe_merge")
    if item.id == "set_upstream":
        return open_effect("branch_picker", "set_upstream")
    if item.id == "branch_from_head":
        return open_effect("branch_name_prompt")
    if item.id == "rename_branch":
        return open_effect("branch_name_prompt", "rename")
    if item.id == "soft_reset":
        return open_effect("reset_prompt")
    if item.id == "run_workflow":
        return open_effect("workflow_picker")
    if item.id == "new_remote":
        begin_new_remote_name(menu)
        return no_effect()
    if item.id.startswith("remote:"):
        name = item.id.split(":", 1)[1]
        begin_set_url_remote(menu, name, _remote_url(menu, name))
        return no_effect()
    if item.id == "stash_create":
        kick_off_action(
            state,
            "stash_create",
            target_label=menu.target_label,
            target_path=menu.target_path,
            target_repo=menu.target_repo,
            target_parent=menu.target_parent,
        )
        return close_effect()
    if item.id.startswith("stash_apply:"):
        ref = item.id.split(":", 1)[1]
        kick_off_action(
            state,
            "stash_apply",
            target_label=menu.target_label,
            target_path=menu.target_path,
            target_repo=menu.target_repo,
            target_parent=menu.target_parent,
            branch_arg=ref,
        )
        return close_effect()
    kick_off_action(
        state,
        item.id,
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
        target_parent=menu.target_parent,
    )
    return close_effect()


def _remote_url(menu: ActionMenu, name: str) -> str:
    for remote_name, url in menu.remotes_list:
        if remote_name == name:
            return url
    return ""
