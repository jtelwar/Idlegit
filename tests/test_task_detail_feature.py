from __future__ import annotations

import curses
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo, make_state as _state  # noqa: E402
from features.task_detail.actions import handle_task_action_menu_key  # noqa: E402
from features.task_detail.projection import is_safe_browser_url  # noqa: E402
from features.task_detail.session import open_task_action_menu  # noqa: E402


class TestTaskDetailFeature(unittest.TestCase):
    def test_projection_allows_only_http_browser_urls(self) -> None:
        self.assertTrue(is_safe_browser_url("https://example.test/run"))
        self.assertTrue(is_safe_browser_url("http://example.test/run"))
        self.assertFalse(is_safe_browser_url("file:///tmp/run"))
        self.assertFalse(is_safe_browser_url("javascript:alert(1)"))

    def test_view_log_action_returns_ui_effect(self) -> None:
        state = _state(_make_repo("a"))
        task = state.tasks.add("↗ a: Build")
        state.workflow_runs.create_for_task(
            task,
            repo=_make_repo("a"),
            slug="o/a",
            run_id=42,
            workflow_name="Build",
        )
        open_task_action_menu(state, task)
        for i, item in enumerate(state.task_action_menu.items):
            if item.id == "view_log":
                state.task_action_menu.selected = i
                break

        effect = handle_task_action_menu_key(state, curses.KEY_ENTER)

        self.assertEqual(effect.kind, "open_task_log")
        self.assertIs(effect.task, task)
        self.assertIsNone(state.task_log_viewer)

    def test_close_action_dismisses_menu_in_feature(self) -> None:
        state = _state(_make_repo("a"))
        task = state.tasks.add("housekeeping")
        open_task_action_menu(state, task)

        handle_task_action_menu_key(state, curses.KEY_ENTER)

        self.assertIsNone(state.task_action_menu)

    def test_set_then_run_updates_followup_record_not_repo_intent(self) -> None:
        repo = _make_repo("a")
        state = _state(repo)
        task = state.tasks.add("  ↪ then run: deploy")
        state.workflow_followups.create_for_task(
            task,
            repo=repo,
            parent_workflow="CI",
            target="Deploy",
        )
        open_task_action_menu(state, task)
        menu = state.task_action_menu
        assert menu is not None
        menu.sub_picker_open = True
        menu.sub_picker_options = ["Release"]
        menu.sub_picker_selected = 0

        handle_task_action_menu_key(state, curses.KEY_ENTER)

        followup = state.workflow_followups.record_for_task(task)
        self.assertIsNotNone(followup)
        assert followup is not None
        self.assertEqual(followup.target, "Release")
        self.assertTrue(state.store.repo_workflow_intent(repo).empty)
        self.assertFalse(hasattr(repo, "then_run_after_workflow"))
        self.assertFalse(hasattr(repo, "then_run_after_push"))

    def test_pending_then_run_projection_uses_followup_registry_not_task_parent(self) -> None:
        repo = _make_repo("a")
        state = _state(repo)
        run_task = state.tasks.add("↗ a: Build")
        state.workflow_runs.create_for_task(
            run_task,
            repo=repo,
            slug="o/a",
            run_id=42,
            workflow_name="Build",
        )
        pending_task = state.tasks.add("  ↪ then run: Deploy")
        state.tasks.update(pending_task, "pending")
        state.workflow_followups.create_for_task(
            pending_task,
            repo=repo,
            parent_workflow="Build",
            target="Deploy",
        )

        open_task_action_menu(state, run_task)

        assert state.task_action_menu is not None
        ids = [item.id for item in state.task_action_menu.items]
        self.assertIn("change_then_run", ids)
        self.assertIs(state.task_action_menu.pending_child, pending_task)
