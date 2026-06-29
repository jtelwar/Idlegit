from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from _helpers import make_state as _state  # noqa: E402
from core.state.action_menu import ActionMenu  # noqa: E402
from core.state.prompts import BranchNamePrompt  # noqa: E402
from features.branch_name_prompt.actions import (  # noqa: E402
    handle_branch_name_prompt_key,
)
from features.branch_name_prompt.session import (  # noqa: E402
    kick_off_branch_name_prompt_prepare,
    open_branch_name_prompt,
)


class TestBranchNamePromptFeature(unittest.TestCase):
    def _state_with_menu(self):
        state = _state(_make_repo("repo"))
        state.action_menu = ActionMenu(
            target_label="repo",
            target_path=Path("/tmp/repo"),
            target_repo=state.repos[0],
            branch="main",
        )
        return state

    def test_open_installs_prompt_without_synchronous_git(self) -> None:
        state = self._state_with_menu()
        fake_job = SimpleNamespace(terminal=False)
        fake_thread = object()

        with (
            mock.patch(
                "features.branch_name_prompt.session.submit_job",
                return_value=(fake_job, fake_thread),
            ) as submit,
            mock.patch("features.branch_name_prompt.session.git") as git,
        ):
            open_branch_name_prompt(state)

        self.assertIsNotNone(state.branch_name_prompt)
        self.assertEqual(state.branch_name_prompt.default_name, "wip-head")
        submit.assert_called_once()
        git.assert_not_called()

    def test_prepare_job_populates_head_default(self) -> None:
        state = self._state_with_menu()
        prompt = BranchNamePrompt(
            target_label="repo",
            target_path=Path("/tmp/repo"),
            default_name="wip-head",
        )
        state.branch_name_prompt = prompt
        captured = {}

        def fake_submit(_registry, _spec, target):
            captured["target"] = target
            return SimpleNamespace(terminal=False), object()

        with (
            mock.patch(
                "features.branch_name_prompt.session.submit_job",
                side_effect=fake_submit,
            ),
            mock.patch(
                "features.branch_name_prompt.session.git",
                side_effect=[
                    (0, "abcdef123456\n", ""),
                    (0, "main\n", ""),
                ],
            ),
        ):
            kick_off_branch_name_prompt_prepare(state, prompt)
            captured["target"](SimpleNamespace())

        self.assertEqual(prompt.head_sha, "abcdef123456")
        self.assertEqual(prompt.current_branch, "main")
        self.assertEqual(prompt.default_name, "wip-abcdef12")

    def test_rename_prepare_preserves_user_typing(self) -> None:
        state = self._state_with_menu()
        prompt = BranchNamePrompt(
            target_label="repo",
            target_path=Path("/tmp/repo"),
            typed="main",
            default_name="main",
            current_branch="main",
            mode="rename",
        )
        state.branch_name_prompt = prompt
        captured = {}

        def fake_submit(_registry, _spec, target):
            captured["target"] = target
            return SimpleNamespace(terminal=False), object()

        with (
            mock.patch(
                "features.branch_name_prompt.session.submit_job",
                side_effect=fake_submit,
            ),
            mock.patch(
                "features.branch_name_prompt.session.git",
                side_effect=[
                    (0, "abcdef123456\n", ""),
                    (0, "main\n", ""),
                ],
            ),
        ):
            kick_off_branch_name_prompt_prepare(
                state,
                prompt,
                initial_typed="main",
            )
            prompt.typed = "feature/new"
            captured["target"](SimpleNamespace())

        self.assertEqual(prompt.typed, "feature/new")
        self.assertEqual(prompt.current_branch, "main")

    def test_submit_dispatches_worker_and_closes_prompts(self) -> None:
        state = self._state_with_menu()
        state.branch_name_prompt = BranchNamePrompt(
            target_label="repo",
            target_path=Path("/tmp/repo"),
            target_repo=state.repos[0],
            typed="feature/new",
            default_name="wip-head",
        )

        with mock.patch(
            "features.branch_name_prompt.actions.kick_off_action",
        ) as action:
            handle_branch_name_prompt_key(state, 10)

        action.assert_called_once_with(
            state,
            "branch_from_head",
            target_label="repo",
            target_path=Path("/tmp/repo"),
            target_repo=state.repos[0],
            target_parent=None,
            branch_arg="feature/new",
        )
        self.assertIsNone(state.branch_name_prompt)
        self.assertIsNone(state.action_menu)

    def test_rename_to_same_branch_closes_without_worker(self) -> None:
        state = self._state_with_menu()
        state.branch_name_prompt = BranchNamePrompt(
            target_label="repo",
            target_path=Path("/tmp/repo"),
            typed="main",
            default_name="main",
            mode="rename",
            current_branch="main",
        )

        with mock.patch(
            "features.branch_name_prompt.actions.kick_off_action",
        ) as action:
            handle_branch_name_prompt_key(state, 10)

        action.assert_not_called()
        self.assertIsNone(state.branch_name_prompt)
        self.assertIsNone(state.action_menu)


if __name__ == "__main__":
    unittest.main()
