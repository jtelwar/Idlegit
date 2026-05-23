"""Tests for ui.mouse: wheel→arrow normalization and Alt+letter
detection by ``read_key``."""
from __future__ import annotations

import curses
import unittest
from unittest.mock import MagicMock, patch


class TestNormalizeMouse(unittest.TestCase):
    def test_passes_through_non_mouse_keys(self) -> None:
        from ui.mouse import _normalize_mouse
        self.assertEqual(_normalize_mouse(27), 27)
        self.assertEqual(_normalize_mouse(curses.KEY_UP), curses.KEY_UP)

    def test_wheel_up_maps_to_key_up(self) -> None:
        from ui.mouse import _normalize_mouse
        with patch("curses.getmouse",
                   return_value=(0, 0, 0, 0, curses.BUTTON4_PRESSED)):
            self.assertEqual(_normalize_mouse(curses.KEY_MOUSE),
                             curses.KEY_UP)

    def test_wheel_down_maps_to_key_down(self) -> None:
        from ui.mouse import _normalize_mouse, _WHEEL_DOWN_MASK
        with patch("curses.getmouse",
                   return_value=(0, 0, 0, 0, _WHEEL_DOWN_MASK)):
            self.assertEqual(_normalize_mouse(curses.KEY_MOUSE),
                             curses.KEY_DOWN)

    def test_unrecognized_mouse_event_is_ignored(self) -> None:
        from ui.mouse import _normalize_mouse
        with patch("curses.getmouse", return_value=(0, 0, 0, 0, 0)):
            self.assertEqual(_normalize_mouse(curses.KEY_MOUSE), -1)

    def test_getmouse_failure_is_ignored(self) -> None:
        from ui.mouse import _normalize_mouse
        with patch("curses.getmouse", side_effect=curses.error):
            self.assertEqual(_normalize_mouse(curses.KEY_MOUSE), -1)


class TestReadKey(unittest.TestCase):
    def _stdscr(self, *keys: int) -> MagicMock:
        """Mock stdscr whose getch() yields ``keys`` in order."""
        stdscr = MagicMock()
        stdscr.getch.side_effect = list(keys)
        return stdscr

    def test_plain_key_passes_through(self) -> None:
        from ui.mouse import read_key
        stdscr = self._stdscr(curses.KEY_LEFT)
        self.assertEqual(read_key(stdscr), curses.KEY_LEFT)

    def test_bare_escape_returns_27(self) -> None:
        from ui.mouse import read_key
        # ESC followed by -1 (no follower within the nonblocking peek)
        stdscr = self._stdscr(27, -1)
        self.assertEqual(read_key(stdscr), 27)

    def test_alt_s_detected(self) -> None:
        from ui.mouse import read_key, ALT_S
        stdscr = self._stdscr(27, ord('s'))
        self.assertEqual(read_key(stdscr), ALT_S)

    def test_alt_m_detected(self) -> None:
        from ui.mouse import read_key, ALT_M
        stdscr = self._stdscr(27, ord('m'))
        self.assertEqual(read_key(stdscr), ALT_M)

    def test_unknown_alt_letter_falls_back_to_escape(self) -> None:
        from ui.mouse import read_key
        # ESC+x — x is not a registered Alt binding; treat as bare ESC.
        stdscr = self._stdscr(27, ord('x'))
        self.assertEqual(read_key(stdscr), 27)

    def test_mouse_wheel_still_normalized(self) -> None:
        from ui.mouse import read_key
        stdscr = self._stdscr(curses.KEY_MOUSE)
        with patch("curses.getmouse",
                   return_value=(0, 0, 0, 0, curses.BUTTON4_PRESSED)):
            self.assertEqual(read_key(stdscr), curses.KEY_UP)


if __name__ == "__main__":
    unittest.main()
