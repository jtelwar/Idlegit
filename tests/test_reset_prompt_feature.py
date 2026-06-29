from __future__ import annotations

import curses
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from _helpers import make_state as _state  # noqa: E402
from core.state.action_menu import ActionMenu  # noqa: E402
from core.state.prompts import ResetPrompt  # noqa: E402
from features.reset_prompt.actions import handle_reset_prompt_key  # noqa: E402
from features.reset_prompt.projection import (  # noqa: E402
    reset_count_from_typed,
    reset_prompt_hint_specs,
    reset_prompt_title,
)
from features.reset_prompt.session import (  # noqa: E402
    close_reset_prompt,
    open_reset_prompt,
)


class TestResetPromptFeature(unittest.TestCase):
    def _state(self):
        repo = _make_repo("repo")
        state = _state(repo)
        state.action_menu = ActionMenu(
            target_label="repo",
            target_path=repo.path,
            target_repo=repo,
            ahead=3,
        )
        return state

    def _prompt(self, typed: str = "") -> ResetPrompt:
        return ResetPrompt(
            target_label="repo",
            target_path=Path("/tmp/repo"),
            typed=typed,
            ahead=3,
        )

    def test_open_session_copies_action_menu_target(self) -> None:
        state = self._state()

        open_reset_prompt(state)

        self.assertIsNotNone(state.reset_prompt)
        self.assertEqual(state.reset_prompt.target_label, "repo")
        self.assertEqual(state.reset_prompt.ahead, 3)

    def test_close_session_clears_prompt_only(self) -> None:
        state = self._state()
        state.reset_prompt = self._prompt()

        close_reset_prompt(state)

        self.assertIsNone(state.reset_prompt)
        self.assertIsNotNone(state.action_menu)

    def test_projection_describes_count_and_wipe(self) -> None:
        self.assertEqual(reset_prompt_title(self._prompt()), "Soft reset")
        self.assertEqual(reset_count_from_typed("bad"), 0)
        count_actions = [
            action for _keys, action in reset_prompt_hint_specs(self._prompt("2"))
        ]
        wipe_actions = [
            action for _keys, action in reset_prompt_hint_specs(self._prompt("0"))
        ]
        self.assertIn("reset 2 commits", count_actions)
        self.assertIn("wipe ALL unpushed", wipe_actions)

    def test_key_handler_edits_digits_only(self) -> None:
        state = self._state()
        state.reset_prompt = self._prompt()

        handle_reset_prompt_key(state, ord("a"))
        handle_reset_prompt_key(state, ord("1"))
        handle_reset_prompt_key(state, ord("2"))
        handle_reset_prompt_key(state, curses.KEY_BACKSPACE)

        self.assertEqual(state.reset_prompt.typed, "1")

    def test_enter_dispatches_soft_reset_and_closes_modals(self) -> None:
        state = self._state()
        state.reset_prompt = self._prompt("2")

        with mock.patch("features.reset_prompt.actions.kick_off_action") as action:
            handle_reset_prompt_key(state, 10)

        action.assert_called_once()
        self.assertEqual(action.call_args.args[1], "soft_reset")
        self.assertEqual(action.call_args.kwargs["reset_count"], 2)
        self.assertIsNone(state.reset_prompt)
        self.assertIsNone(state.action_menu)

    def test_empty_enter_does_not_dispatch(self) -> None:
        state = self._state()
        state.reset_prompt = self._prompt()

        with mock.patch("features.reset_prompt.actions.kick_off_action") as action:
            handle_reset_prompt_key(state, 10)

        action.assert_not_called()
        self.assertIsNotNone(state.reset_prompt)
        self.assertIsNotNone(state.action_menu)


if __name__ == "__main__":
    unittest.main()
