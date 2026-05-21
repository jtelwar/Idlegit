"""Mouse wheel → arrow-key translation."""
from __future__ import annotations

import curses
import unittest
from unittest.mock import patch


class TestNormalizeInput(unittest.TestCase):
    def test_passes_through_non_mouse_keys(self) -> None:
        from ui.mouse import normalize_input
        self.assertEqual(normalize_input(27), 27)
        self.assertEqual(normalize_input(curses.KEY_UP), curses.KEY_UP)

    def test_wheel_up_maps_to_key_up(self) -> None:
        from ui.mouse import normalize_input
        with patch("curses.getmouse",
                   return_value=(0, 0, 0, 0, curses.BUTTON4_PRESSED)):
            self.assertEqual(normalize_input(curses.KEY_MOUSE),
                             curses.KEY_UP)

    def test_wheel_down_maps_to_key_down(self) -> None:
        from ui.mouse import normalize_input
        from ui.mouse import _WHEEL_DOWN_MASK
        with patch("curses.getmouse",
                   return_value=(0, 0, 0, 0, _WHEEL_DOWN_MASK)):
            self.assertEqual(normalize_input(curses.KEY_MOUSE),
                             curses.KEY_DOWN)

    def test_unrecognized_mouse_event_is_ignored(self) -> None:
        from ui.mouse import normalize_input
        with patch("curses.getmouse", return_value=(0, 0, 0, 0, 0)):
            self.assertEqual(normalize_input(curses.KEY_MOUSE), -1)

    def test_getmouse_failure_is_ignored(self) -> None:
        from ui.mouse import normalize_input
        with patch("curses.getmouse", side_effect=curses.error):
            self.assertEqual(normalize_input(curses.KEY_MOUSE), -1)
