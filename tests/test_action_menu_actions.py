from __future__ import annotations

import sys
import unittest
import curses
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from _helpers import make_state as _state  # noqa: E402
from core.state.action_menu import (  # noqa: E402
    ActionMenu,
    ActionMenuItem,
    CommitEntry,
    FileEntry,
)
from features.action_menu.actions import (  # noqa: E402
    apply_remote_op,
    begin_new_remote_name,
    begin_rename_remote,
    dispatch_action_menu_item,
    handle_action_menu_confirm_key,
    handle_action_menu_key_intent,
    handle_action_menu_inline_edit_key,
)
from features.action_menu.projection import (  # noqa: E402
    current_items,
    current_selected,
    enter_submenu_for,
    exit_to_parent,
)


class TestActionMenuActions(unittest.TestCase):
    def _menu(self) -> ActionMenu:
        repo = _make_repo("repo")
        return ActionMenu(
            target_label="repo",
            target_path=repo.path,
            target_repo=repo,
            remotes_list=[("origin", "git@example.com:old/repo.git")],
            remote_count=1,
            cached_meta={
                "has_origin": True,
                "upstream": "origin/main",
                "merging": False,
                "ahead": 0,
                "behind": 0,
                "dirty": False,
                "has_any_workflow": False,
                "run_workflow_reason": "no workflows in this repo",
            },
        )

    def test_switch_branch_returns_ui_effect_without_worker_dispatch(self) -> None:
        state = _state(_make_repo("repo"))
        menu = self._menu()

        effect = dispatch_action_menu_item(
            state,
            menu,
            ActionMenuItem(id="switch_branch", label="switch branch"),
        )

        self.assertEqual(effect.kind, "branch_picker")
        self.assertEqual(effect.mode, "")

    def test_stash_create_dispatches_worker_and_requests_close(self) -> None:
        state = _state(_make_repo("repo"))
        menu = self._menu()

        with mock.patch("features.action_menu.actions.kick_off_action") as action:
            effect = dispatch_action_menu_item(
                state,
                menu,
                ActionMenuItem(id="stash_create", label="new stash"),
            )

        self.assertEqual(effect.kind, "close")
        action.assert_called_once_with(
            state,
            "stash_create",
            target_label=menu.target_label,
            target_path=menu.target_path,
            target_repo=menu.target_repo,
            target_parent=menu.target_parent,
        )

    def test_safe_merge_adopts_existing_merge_in_feature_boundary(self) -> None:
        state = _state(_make_repo("repo"))
        menu = self._menu()

        with (
            mock.patch("features.action_menu.actions.merge_head_sha",
                       return_value="abc123"),
            mock.patch("features.action_menu.actions.kick_off_safe_merge")
            as safe_merge,
        ):
            effect = dispatch_action_menu_item(
                state,
                menu,
                ActionMenuItem(id="safe_merge", label="safe merge"),
            )

        self.assertEqual(effect.kind, "close")
        safe_merge.assert_called_once_with(
            state,
            target_label=menu.target_label,
            target_path=menu.target_path,
            target_repo=menu.target_repo,
            target_parent=menu.target_parent,
            merge_ref="",
        )

    def test_safe_merge_without_existing_merge_returns_picker_effect(self) -> None:
        state = _state(_make_repo("repo"))
        menu = self._menu()

        with mock.patch("features.action_menu.actions.merge_head_sha",
                        return_value=None):
            effect = dispatch_action_menu_item(
                state,
                menu,
                ActionMenuItem(id="safe_merge", label="safe merge"),
            )

        self.assertEqual(effect.kind, "branch_picker")
        self.assertEqual(effect.mode, "safe_merge")

    def test_apply_remote_op_dispatches_remote_worker_and_updates_cache(self) -> None:
        state = _state(_make_repo("repo"))
        menu = self._menu()
        menu.confirm_action = "set_url_remote"
        menu.confirm_args = {
            "name": "origin",
            "old_url": "git@example.com:old/repo.git",
            "url": "git@example.com:new/repo.git",
        }

        with mock.patch("features.action_menu.actions.kick_off_remote_changes") as remote:
            apply_remote_op(state, menu)

        remote.assert_called_once()
        rows = remote.call_args.args[1]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "origin")
        self.assertEqual(rows[0].url, "git@example.com:new/repo.git")
        self.assertEqual(
            menu.remotes_list,
            [("origin", "git@example.com:new/repo.git")],
        )

    def test_confirm_key_applies_and_clears_remote_action(self) -> None:
        state = _state(_make_repo("repo"))
        menu = self._menu()
        menu.confirm_message = "Set origin URL? [y/N]"
        menu.confirm_action = "set_url_remote"
        menu.confirm_args = {
            "name": "origin",
            "old_url": "git@example.com:old/repo.git",
            "url": "git@example.com:new/repo.git",
        }

        with mock.patch("features.action_menu.actions.kick_off_remote_changes"):
            effect = handle_action_menu_confirm_key(state, menu, ord("y"))

        self.assertEqual(effect.kind, "none")
        self.assertEqual(menu.confirm_message, "")
        self.assertEqual(menu.confirm_action, "")
        self.assertEqual(menu.confirm_args, {})

    def test_enter_remotes_submenu_builds_frame_with_new_remote_selected(
            self,
    ) -> None:
        menu = self._menu()

        enter_submenu_for(
            menu,
            ActionMenuItem(
                id="remotes_submenu",
                label="remotes",
                has_submenu=True,
            ),
        )

        self.assertEqual(menu.submenu_stack[-1].name, "remotes")
        self.assertEqual(current_selected(menu), 1)
        self.assertEqual(current_items(menu)[1].id, "new_remote")

    def test_exiting_stash_child_returns_to_opening_stash_row(self) -> None:
        menu = self._menu()
        menu.stashes = [("stash@{0}", "WIP")]

        enter_submenu_for(
            menu,
            ActionMenuItem(
                id="stashes_submenu",
                label="stashes",
                has_submenu=True,
            ),
        )
        stash_item = next(
            item for item in current_items(menu)
            if item.id == "stash:stash@{0}"
        )
        enter_submenu_for(menu, stash_item)

        exit_to_parent(menu)

        self.assertEqual(menu.submenu_stack[-1].name, "stashes")
        self.assertEqual(
            current_items(menu)[current_selected(menu)].id,
            "stash:stash@{0}",
        )

    def test_inline_rename_remote_enter_requests_confirm(self) -> None:
        menu = self._menu()
        begin_rename_remote(
            menu,
            "origin",
            "git@example.com:old/repo.git",
        )
        menu.edit_typed = "upstream"
        menu.edit_cursor = len(menu.edit_typed)

        effect = handle_action_menu_inline_edit_key(menu, curses.KEY_ENTER)

        self.assertEqual(effect.kind, "none")
        self.assertEqual(menu.edit_field, "")
        self.assertEqual(menu.confirm_action, "rename_remote")
        self.assertEqual(menu.confirm_args["old"], "origin")
        self.assertEqual(menu.confirm_args["new"], "upstream")

    def test_inline_new_remote_refuses_duplicate_name(self) -> None:
        menu = self._menu()
        begin_new_remote_name(menu)
        menu.edit_typed = "origin"
        menu.edit_cursor = len(menu.edit_typed)

        handle_action_menu_inline_edit_key(menu, curses.KEY_ENTER)

        self.assertEqual(menu.edit_field, "add_remote_name")
        self.assertEqual(menu.confirm_action, "")

    def test_inline_new_remote_advances_to_url_then_confirm(self) -> None:
        menu = self._menu()
        begin_new_remote_name(menu)
        menu.edit_typed = "backup"
        menu.edit_cursor = len(menu.edit_typed)

        handle_action_menu_inline_edit_key(menu, curses.KEY_ENTER)
        self.assertEqual(menu.edit_field, "add_remote_url")
        self.assertEqual(menu.edit_extra, {"name": "backup"})

        menu.edit_typed = "git@example.com:backup/repo.git"
        menu.edit_cursor = len(menu.edit_typed)
        handle_action_menu_inline_edit_key(menu, curses.KEY_ENTER)

        self.assertEqual(menu.edit_field, "")
        self.assertEqual(menu.confirm_action, "add_remote")
        self.assertEqual(menu.confirm_args["name"], "backup")
        self.assertEqual(
            menu.confirm_args["url"],
            "git@example.com:backup/repo.git",
        )

    def test_key_intent_down_past_main_actions_moves_to_pane(self) -> None:
        state = _state(_make_repo("repo"))
        menu = self._menu()
        menu.items = [
            ActionMenuItem(id="fetch", label="fetch"),
            ActionMenuItem(id="push", label="push"),
        ]
        menu.selected = 1

        effect = handle_action_menu_key_intent(state, menu, curses.KEY_DOWN)

        self.assertEqual(effect.kind, "none")
        self.assertTrue(menu.pane_focus)

    def test_key_intent_right_enters_submenu(self) -> None:
        state = _state(_make_repo("repo"))
        menu = self._menu()
        menu.items = [
            ActionMenuItem(
                id="remotes_submenu",
                label="remotes",
                has_submenu=True,
            ),
        ]

        effect = handle_action_menu_key_intent(state, menu, curses.KEY_RIGHT)

        self.assertEqual(effect.kind, "none")
        self.assertEqual(menu.submenu_stack[-1].name, "remotes")

    def test_key_intent_tree_tab_returns_diff_effect(self) -> None:
        state = _state(_make_repo("repo"))
        menu = self._menu()
        menu.pane_focus = True
        menu.pane_tab = "tree"
        menu.tree_selected = 1
        menu.tree_files = [
            FileEntry(path="changed.txt", x="M", y=" "),
        ]

        effect = handle_action_menu_key_intent(state, menu, 9)

        self.assertEqual(effect.kind, "diff_viewer")
        self.assertEqual(effect.file_path, "changed.txt")
        self.assertFalse(effect.untracked)

    def test_key_intent_commit_tab_returns_commit_effect(self) -> None:
        state = _state(_make_repo("repo"))
        menu = self._menu()
        menu.pane_focus = True
        menu.pane_tab = "commits"
        menu.commits_selected = 1
        menu.commits_full = [
            CommitEntry(sha="abc123", subject="fix bug"),
        ]

        effect = handle_action_menu_key_intent(state, menu, 9)

        self.assertEqual(effect.kind, "commit_view")
        self.assertEqual(effect.sha, "abc123")
        self.assertEqual(effect.subject, "fix bug")

    def test_key_intent_commit_filter_typing_updates_feature_state(self) -> None:
        state = _state(_make_repo("repo"))
        menu = self._menu()
        menu.pane_focus = True
        menu.pane_tab = "commits"
        menu.commits_selected = 0

        handle_action_menu_key_intent(state, menu, ord("f"))
        handle_action_menu_key_intent(state, menu, ord("i"))
        handle_action_menu_key_intent(state, menu, curses.KEY_BACKSPACE)

        self.assertEqual(menu.commits_filter, "f")


if __name__ == "__main__":
    unittest.main()
