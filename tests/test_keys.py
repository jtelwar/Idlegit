"""Tests for the keyboard handlers in ui.py — state-machine only, no
curses screen, no git. Each handler is a pure function on State; we
poke it with key codes and assert on the resulting state."""
from __future__ import annotations

import curses
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import (  # noqa: E402
    ActionMenu, ActionMenuItem, BranchPicker, CommitEntry, FileEntry, Repo,
    ResetPrompt, State,
)


# ui imports curses at module load. On non-tty hosts that fails; skip the
# whole module under that condition rather than erroring out import.
try:
    from ui import (  # noqa: E402
        handle_action_menu_key, handle_branch_picker_key, handle_main_key,
        handle_reset_prompt_key, handle_task_action_menu_key,
        handle_task_panel_key, open_task_action_menu,
    )
    UI_AVAILABLE = True
except Exception:  # pragma: no cover
    UI_AVAILABLE = False


def _make_repo(rel: str = "r", **kwargs) -> Repo:
    return Repo(rel=rel, path=Path(f"/tmp/{rel}"), **kwargs)


def _state(*repos: Repo, selected: int = 0, **kwargs) -> State:
    # Default selected=0: first repo row. The old toggle row (auto-
    # stage / auto-push / align-heads) moved into the workspace menu;
    # body rows now start at index 0.
    return State(repos=list(repos), workspace_name="ws",
                 selected=selected, **kwargs)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestNavigation(unittest.TestCase):
    def test_down_advances_selection(self) -> None:
        s = _state(_make_repo("a"), _make_repo("b"), selected=0)
        handle_main_key(s, curses.KEY_DOWN)
        self.assertEqual(s.selected, 1)
        # Wraps via mod when stepping past the last body row.
        handle_main_key(s, curses.KEY_DOWN)
        self.assertEqual(s.selected, 0)

    def test_up_from_top_body_row_lands_on_workspace_row(self) -> None:
        # Up from the first body row lands on the workspace title-row
        # selector (selected = -1), not the bottom of the body.
        # Pressing Up again from -1 wraps to the last body row.
        s = _state(_make_repo("a"), _make_repo("b"), selected=0)
        handle_main_key(s, curses.KEY_UP)
        self.assertEqual(s.selected, -1)
        handle_main_key(s, curses.KEY_UP)
        self.assertEqual(s.selected, 1)  # last body row (2 repos → idx 1)

    def test_navigation_resets_field_cursor_to_message_end(self) -> None:
        a = _make_repo("a")
        a.message = "hello"
        s = _state(a, selected=-1)  # start on workspace row
        s.field_cursor = 999
        handle_main_key(s, curses.KEY_DOWN)  # → first body row (a)
        self.assertEqual(s.selected, 0)
        self.assertEqual(s.field_cursor, len("hello"))


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestEnterReview(unittest.TestCase):
    def test_enter_with_no_messages_returns_none(self) -> None:
        s = _state(_make_repo("a"))
        self.assertIsNone(handle_main_key(s, 10))

    def test_enter_with_message_returns_confirm(self) -> None:
        a = _make_repo("a")
        a.message = "fix bug"
        s = _state(a)
        self.assertEqual(handle_main_key(s, 10), "confirm")


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestEscBehavior(unittest.TestCase):
    def test_esc_no_messages_returns_quit(self) -> None:
        s = _state(_make_repo("a"))
        self.assertEqual(handle_main_key(s, 27), "quit")

    def test_esc_with_other_messages_returns_confirm_quit(self) -> None:
        a = _make_repo("a")
        b = _make_repo("b")
        b.message = "wip"
        s = _state(a, b, selected=0)  # focused on a (no message)
        self.assertEqual(handle_main_key(s, 27), "confirm-quit")

    def test_esc_clears_focused_message_first(self) -> None:
        a = _make_repo("a")
        a.message = "wip"
        s = _state(a, selected=0)
        result = handle_main_key(s, 27)
        self.assertIsNone(result)
        self.assertEqual(a.message, "")


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestTypingAndCursor(unittest.TestCase):
    def test_typing_inserts_at_cursor(self) -> None:
        a = _make_repo("a")
        a.staged = [("M", "x")]  # mark dirty so the field is "live"
        s = _state(a, selected=0)
        handle_main_key(s, ord("h"))
        handle_main_key(s, ord("i"))
        self.assertEqual(a.message, "hi")
        self.assertEqual(s.field_cursor, 2)

    def test_typing_in_middle(self) -> None:
        a = _make_repo("a")
        a.staged = [("M", "x")]
        a.message = "hllo"
        s = _state(a, selected=0)
        s.field_cursor = 1  # between "h" and "l"
        handle_main_key(s, ord("e"))
        self.assertEqual(a.message, "hello")
        self.assertEqual(s.field_cursor, 2)

    def test_backspace_deletes_before_cursor(self) -> None:
        a = _make_repo("a")
        a.staged = [("M", "x")]
        a.message = "ab"
        s = _state(a, selected=0)
        s.field_cursor = 2
        handle_main_key(s, curses.KEY_BACKSPACE)
        self.assertEqual(a.message, "a")
        self.assertEqual(s.field_cursor, 1)

    def test_forward_delete_drops_char_under_cursor(self) -> None:
        a = _make_repo("a")
        a.staged = [("M", "x")]
        a.message = "abc"
        s = _state(a, selected=0)
        s.field_cursor = 1
        handle_main_key(s, curses.KEY_DC)
        self.assertEqual(a.message, "ac")
        self.assertEqual(s.field_cursor, 1)

    def test_left_with_message_moves_cursor(self) -> None:
        a = _make_repo("a")
        a.staged = [("M", "x")]
        a.message = "hello"
        s = _state(a, selected=0)
        s.field_cursor = 3
        handle_main_key(s, curses.KEY_LEFT)
        self.assertEqual(s.field_cursor, 2)

    def test_right_clamps_at_end(self) -> None:
        a = _make_repo("a")
        a.staged = [("M", "x")]
        a.message = "hi"
        s = _state(a, selected=0)
        s.field_cursor = 2
        handle_main_key(s, curses.KEY_RIGHT)
        self.assertEqual(s.field_cursor, 2)

    def test_home_jumps_to_zero(self) -> None:
        a = _make_repo("a")
        a.staged = [("M", "x")]
        a.message = "hello"
        s = _state(a, selected=0)
        s.field_cursor = 4
        handle_main_key(s, curses.KEY_HOME)
        self.assertEqual(s.field_cursor, 0)

    def test_end_jumps_to_len(self) -> None:
        a = _make_repo("a")
        a.staged = [("M", "x")]
        a.message = "hello"
        s = _state(a, selected=0)
        s.field_cursor = 0
        handle_main_key(s, curses.KEY_END)
        self.assertEqual(s.field_cursor, 5)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestRefreshAndSyncShortcuts(unittest.TestCase):
    def test_ctrl_r_returns_refresh(self) -> None:
        s = _state(_make_repo("a"))
        self.assertEqual(handle_main_key(s, 18), "refresh")

    def test_f5_returns_refresh(self) -> None:
        s = _state(_make_repo("a"))
        self.assertEqual(handle_main_key(s, curses.KEY_F5), "refresh")

    def test_ctrl_s_returns_sync(self) -> None:
        s = _state(_make_repo("a"))
        self.assertEqual(handle_main_key(s, 19), "sync")


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestShiftTabTogglesPanel(unittest.TestCase):
    def test_shift_tab_moves_focus_to_tasks(self) -> None:
        s = _state(_make_repo("a"))
        self.assertEqual(s.focused_panel, "repos")
        handle_main_key(s, curses.KEY_BTAB)
        self.assertEqual(s.focused_panel, "tasks")

    def test_shift_tab_again_returns_to_repos(self) -> None:
        s = _state(_make_repo("a"))
        handle_main_key(s, curses.KEY_BTAB)
        handle_main_key(s, curses.KEY_BTAB)
        self.assertEqual(s.focused_panel, "repos")

    def test_esc_in_task_focus_returns_to_repos_not_quit(self) -> None:
        s = _state(_make_repo("a"))
        handle_main_key(s, curses.KEY_BTAB)
        # In task focus, Esc should toggle back rather than triggering
        # the quit/confirm-quit signal that handle_main_key returns when
        # repos has focus.
        result = handle_main_key(s, 27)
        self.assertIsNone(result)
        self.assertEqual(s.focused_panel, "repos")


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestTaskPanelKeyHandler(unittest.TestCase):
    def _state_with_tasks(self, *labels: str) -> State:
        s = _state(_make_repo("a"))
        s.focused_panel = "tasks"
        for lbl in labels:
            s.tasks.add(lbl)
        return s

    def test_down_advances_selection(self) -> None:
        s = self._state_with_tasks("a", "b", "c")
        handle_task_panel_key(s, curses.KEY_DOWN)
        self.assertEqual(s.task_selected, 1)
        handle_task_panel_key(s, curses.KEY_DOWN)
        self.assertEqual(s.task_selected, 2)
        # Doesn't go past the end.
        handle_task_panel_key(s, curses.KEY_DOWN)
        self.assertEqual(s.task_selected, 2)

    def test_up_retreats_selection(self) -> None:
        s = self._state_with_tasks("a", "b", "c")
        s.task_selected = 2
        handle_task_panel_key(s, curses.KEY_UP)
        self.assertEqual(s.task_selected, 1)
        handle_task_panel_key(s, curses.KEY_UP)
        self.assertEqual(s.task_selected, 0)
        handle_task_panel_key(s, curses.KEY_UP)
        self.assertEqual(s.task_selected, 0)

    def test_enter_removes_completed_task(self) -> None:
        s = self._state_with_tasks("a", "b", "c")
        items = s.tasks.snapshot()
        s.tasks.update(items[1], "ok")  # mark "b" finished
        s.task_selected = 1
        handle_task_panel_key(s, 10)  # Enter
        labels = [t.label for t in s.tasks.snapshot()]
        self.assertEqual(labels, ["a", "c"])

    def test_enter_keeps_running_task(self) -> None:
        s = self._state_with_tasks("a", "b", "c")
        s.task_selected = 1
        handle_task_panel_key(s, 10)  # Enter on a running task
        labels = [t.label for t in s.tasks.snapshot()]
        self.assertEqual(labels, ["a", "b", "c"])  # unchanged

    def test_enter_clamps_selection_after_removal(self) -> None:
        s = self._state_with_tasks("a", "b", "c")
        items = s.tasks.snapshot()
        for t in items:
            s.tasks.update(t, "ok")
        s.task_selected = 2
        handle_task_panel_key(s, 10)  # remove "c"
        # Selection should drop to the new last index.
        self.assertEqual(s.task_selected, 1)

    def test_esc_returns_focus_to_repos(self) -> None:
        s = self._state_with_tasks("a")
        handle_task_panel_key(s, 27)
        self.assertEqual(s.focused_panel, "repos")


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestActionMenuHandler(unittest.TestCase):
    def _menu(self, items=None, selected=0) -> ActionMenu:
        items = items or [
            ActionMenuItem(id="fetch", label="fetch", enabled=True),
            ActionMenuItem(id="pull", label="pull", enabled=False, reason="no upstream"),
            ActionMenuItem(id="switch_branch", label="switch branch…", enabled=True),
            ActionMenuItem(id="soft_reset", label="soft reset…", enabled=True),
            ActionMenuItem(id="push", label="push", enabled=True),
        ]
        return ActionMenu(
            target_label="repo", target_path=Path("/tmp/repo"),
            items=items, selected=selected,
        )

    def test_esc_closes(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu()
        handle_action_menu_key(s, 27)
        self.assertIsNone(s.action_menu)

    def test_down_off_last_item_enters_pane(self) -> None:
        # Down past the last action item drops focus into the bottom
        # pane (working tree / recent commits) instead of wrapping.
        s = _state(_make_repo("a"))
        s.action_menu = self._menu(selected=4)  # last item
        handle_action_menu_key(s, curses.KEY_DOWN)
        self.assertTrue(s.action_menu.pane_focus)
        self.assertEqual(s.action_menu.selected, 4)

    def test_enter_disabled_item_is_noop(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu(selected=1)  # pull (disabled)
        handle_action_menu_key(s, 10)
        # Menu still open, selection unchanged.
        self.assertIsNotNone(s.action_menu)
        self.assertEqual(s.action_menu.selected, 1)

    def test_enter_switch_branch_opens_picker(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu(selected=2)  # switch_branch
        handle_action_menu_key(s, 10)
        # Submodal opens; parent stays open.
        self.assertIsNotNone(s.branch_picker)
        self.assertIsNotNone(s.action_menu)

    def test_enter_soft_reset_opens_prompt(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu(selected=3)  # soft_reset
        handle_action_menu_key(s, 10)
        self.assertIsNotNone(s.reset_prompt)
        self.assertIsNotNone(s.action_menu)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestActionMenuPane(unittest.TestCase):
    """Bottom-pane behaviour: focus traversal between action items and
    pane, tab swapping, filter typing, and Home returning to the menu.
    Constructs an ActionMenu directly with pre-baked tree/commits lists
    so we don't need to touch the filesystem."""

    def _menu(self, *, files=None, commits=None, selected=4) -> ActionMenu:
        items = [
            ActionMenuItem(id="fetch", label="fetch"),
            ActionMenuItem(id="pull", label="pull", enabled=False),
            ActionMenuItem(id="switch_branch", label="switch branch…"),
            ActionMenuItem(id="soft_reset", label="soft reset…"),
            ActionMenuItem(id="push", label="push"),
        ]
        files = files or [
            FileEntry(path="src/foo.py", x="M", y=" ", inserted=12, deleted=3),
            FileEntry(path="src/bar.py", x=" ", y="M", inserted=1, deleted=1),
            FileEntry(path="new.txt", untracked=True),
        ]
        commits = commits or [
            CommitEntry(sha="abc1234", subject="fix: bug", relative="2h ago"),
            CommitEntry(sha="def5678", subject="feat: thing", relative="3d ago"),
            CommitEntry(sha="ace9abc", subject="docs", relative="1w ago"),
        ]
        return ActionMenu(
            target_label="repo", target_path=Path("/tmp/repo"),
            items=items, selected=selected,
            tree_files=files, commits_full=commits, commits_exhausted=True,
        )

    # ---- Focus traversal ----

    def test_up_off_filter_returns_focus_to_actions(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu()
        s.action_menu.pane_focus = True
        s.action_menu.tree_selected = 0  # on filter row
        handle_action_menu_key(s, curses.KEY_UP)
        self.assertFalse(s.action_menu.pane_focus)

    def test_down_in_pane_advances_through_filter_then_rows(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu()
        s.action_menu.pane_focus = True
        # Initially on filter row (0); Down should move to first list row.
        handle_action_menu_key(s, curses.KEY_DOWN)
        self.assertEqual(s.action_menu.tree_selected, 1)
        handle_action_menu_key(s, curses.KEY_DOWN)
        self.assertEqual(s.action_menu.tree_selected, 2)

    def test_down_clamps_at_list_end(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu()
        s.action_menu.pane_focus = True
        s.action_menu.tree_selected = 3  # last file row (3 files)
        handle_action_menu_key(s, curses.KEY_DOWN)
        self.assertEqual(s.action_menu.tree_selected, 3)

    def test_up_through_list_back_to_filter(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu()
        s.action_menu.pane_focus = True
        s.action_menu.tree_selected = 1
        handle_action_menu_key(s, curses.KEY_UP)
        self.assertEqual(s.action_menu.tree_selected, 0)
        # Still in pane; one more Up returns to action items.
        self.assertTrue(s.action_menu.pane_focus)
        handle_action_menu_key(s, curses.KEY_UP)
        self.assertFalse(s.action_menu.pane_focus)

    # ---- Tab swap ----

    def test_left_right_swaps_tabs_only_in_pane(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu()
        # Without pane focus, L/R is a no-op (today's behavior preserved).
        handle_action_menu_key(s, curses.KEY_RIGHT)
        self.assertEqual(s.action_menu.pane_tab, "tree")
        # With pane focus, R goes to commits, L back to tree.
        s.action_menu.pane_focus = True
        handle_action_menu_key(s, curses.KEY_RIGHT)
        self.assertEqual(s.action_menu.pane_tab, "commits")
        handle_action_menu_key(s, curses.KEY_LEFT)
        self.assertEqual(s.action_menu.pane_tab, "tree")

    def test_tab_swap_preserves_per_tab_filter(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu()
        s.action_menu.pane_focus = True
        s.action_menu.tree_filter = "foo"
        handle_action_menu_key(s, curses.KEY_RIGHT)  # → commits
        s.action_menu.commits_filter = "bug"
        handle_action_menu_key(s, curses.KEY_LEFT)   # → tree
        self.assertEqual(s.action_menu.tree_filter, "foo")
        handle_action_menu_key(s, curses.KEY_RIGHT)
        self.assertEqual(s.action_menu.commits_filter, "bug")

    # ---- Filter typing ----

    def test_typing_appends_to_active_tab_filter(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu()
        s.action_menu.pane_focus = True
        # On filter row by default. Type "fo".
        handle_action_menu_key(s, ord("f"))
        handle_action_menu_key(s, ord("o"))
        self.assertEqual(s.action_menu.tree_filter, "fo")
        self.assertEqual(s.action_menu.commits_filter, "")

    def test_backspace_drops_last_filter_char(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu()
        s.action_menu.pane_focus = True
        s.action_menu.tree_filter = "foo"
        handle_action_menu_key(s, curses.KEY_BACKSPACE)
        self.assertEqual(s.action_menu.tree_filter, "fo")

    def test_typing_off_filter_row_does_nothing(self) -> None:
        # Once you arrow-down off the filter, printable keys should not
        # mutate the filter — they're navigation, not text input.
        s = _state(_make_repo("a"))
        s.action_menu = self._menu()
        s.action_menu.pane_focus = True
        s.action_menu.tree_selected = 1  # past the filter row
        handle_action_menu_key(s, ord("x"))
        self.assertEqual(s.action_menu.tree_filter, "")

    # ---- Home key ----

    def test_home_returns_to_first_enabled_action_item(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu()
        s.action_menu.pane_focus = True
        s.action_menu.tree_selected = 2
        handle_action_menu_key(s, curses.KEY_HOME)
        self.assertFalse(s.action_menu.pane_focus)
        # First enabled item is index 0 (fetch).
        self.assertEqual(s.action_menu.selected, 0)

    # ---- Esc still closes from pane focus ----

    def test_esc_closes_modal_from_pane_focus(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = self._menu()
        s.action_menu.pane_focus = True
        handle_action_menu_key(s, 27)
        self.assertIsNone(s.action_menu)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestResetPromptHandler(unittest.TestCase):
    def _prompt(self, ahead=3) -> ResetPrompt:
        return ResetPrompt(
            target_label="repo", target_path=Path("/tmp/repo"), ahead=ahead,
        )

    def test_digits_append_to_typed(self) -> None:
        s = _state(_make_repo("a"))
        s.reset_prompt = self._prompt()
        handle_reset_prompt_key(s, ord("1"))
        handle_reset_prompt_key(s, ord("2"))
        self.assertEqual(s.reset_prompt.typed, "12")

    def test_non_digit_ignored(self) -> None:
        s = _state(_make_repo("a"))
        s.reset_prompt = self._prompt()
        handle_reset_prompt_key(s, ord("a"))
        self.assertEqual(s.reset_prompt.typed, "")

    def test_backspace_drops_last(self) -> None:
        s = _state(_make_repo("a"))
        s.reset_prompt = self._prompt()
        s.reset_prompt.typed = "12"
        handle_reset_prompt_key(s, curses.KEY_BACKSPACE)
        self.assertEqual(s.reset_prompt.typed, "1")

    def test_esc_closes_prompt_only(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = ActionMenu(target_label="x", target_path=Path("/tmp/x"))
        s.reset_prompt = self._prompt()
        handle_reset_prompt_key(s, 27)
        self.assertIsNone(s.reset_prompt)
        # Parent action menu stays open.
        self.assertIsNotNone(s.action_menu)

    def test_enter_dispatches_and_closes_both(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = ActionMenu(target_label="x", target_path=Path("/tmp/x"))
        s.reset_prompt = self._prompt()
        s.reset_prompt.typed = "2"
        handle_reset_prompt_key(s, 10)
        # Both modals close. (The actual git work runs in a daemon thread
        # against /tmp/x — it will fail silently, that's fine.)
        self.assertIsNone(s.reset_prompt)
        self.assertIsNone(s.action_menu)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestBranchPickerHandler(unittest.TestCase):
    def _picker(self, branches=None, selected=0) -> BranchPicker:
        branches = branches or ["main", "feature/x", "bugfix/y"]
        return BranchPicker(
            target_label="repo", target_path=Path("/tmp/repo"),
            branches=branches, current="main", selected=selected,
        )

    def test_down_clamps_to_last(self) -> None:
        s = _state(_make_repo("a"))
        s.branch_picker = self._picker(selected=3)  # already last
        handle_branch_picker_key(s, curses.KEY_DOWN)
        self.assertEqual(s.branch_picker.selected, 2)

    def test_up_clamps_to_zero(self) -> None:
        s = _state(_make_repo("a"))
        s.branch_picker = self._picker(selected=0)
        handle_branch_picker_key(s, curses.KEY_UP)
        self.assertEqual(s.branch_picker.selected, 0)

    def test_esc_closes_picker_only(self) -> None:
        s = _state(_make_repo("a"))
        s.action_menu = ActionMenu(target_label="x", target_path=Path("/tmp/x"))
        s.branch_picker = self._picker()
        handle_branch_picker_key(s, 27)
        self.assertIsNone(s.branch_picker)
        self.assertIsNotNone(s.action_menu)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestTaskActionMenu(unittest.TestCase):
    """`open_task_action_menu` builds different action lists per task
    archetype: plain bookkeeping rows, terminal tasks, running workflow
    runs (with/without a chained then-run), and pending then-run
    placeholders. Each archetype is exercised here so a regression in
    the item-derivation logic gets caught without firing up curses."""

    def _state(self) -> State:
        return _state(_make_repo("a"))

    def _ids(self, s: State) -> list:
        assert s.task_action_menu is not None
        return [it.id for it in s.task_action_menu.items]

    def test_plain_task_offers_only_close(self) -> None:
        # No metadata, status=running → no cancel/open/remove/then-run.
        s = self._state()
        t = s.tasks.add("housekeeping")
        open_task_action_menu(s, t)
        self.assertEqual(self._ids(s), ["close"])

    def test_terminal_task_offers_remove_and_close(self) -> None:
        s = self._state()
        t = s.tasks.add("done thing")
        s.tasks.update(t, "ok")
        open_task_action_menu(s, t)
        self.assertEqual(self._ids(s), ["remove", "close"])

    def test_running_run_offers_cancel_and_close(self) -> None:
        s = self._state()
        repo = _make_repo("a")
        t = s.tasks.add("↗ a: Build")
        s.tasks.set_meta(t, repo=repo, slug="o/a", run_id=42,
                         workflow_name="Build")
        open_task_action_menu(s, t)
        self.assertEqual(self._ids(s), ["cancel_run", "close"])

    def test_running_run_with_url_offers_open_in_browser(self) -> None:
        s = self._state()
        repo = _make_repo("a")
        t = s.tasks.add("↗ a: Build")
        s.tasks.set_meta(t, repo=repo, slug="o/a", run_id=42,
                         workflow_name="Build",
                         run_url="https://example/runs/42")
        open_task_action_menu(s, t)
        self.assertEqual(
            self._ids(s), ["cancel_run", "open_in_browser", "close"])

    def test_running_run_with_pending_then_run_child(self) -> None:
        # Parent run task has a pending placeholder child — the modal
        # offers Change/Cancel for the chained workflow.
        s = self._state()
        repo = _make_repo("a")
        parent = s.tasks.add("↗ a: Build")
        s.tasks.set_meta(parent, repo=repo, slug="o/a", run_id=42,
                         workflow_name="Build")
        child = s.tasks.add("  ↪ then run: Deploy", parent=parent)
        s.tasks.update(child, "pending")
        s.tasks.set_meta(child, repo=repo,
                         pending_after_workflow="Build",
                         pending_target="Deploy")
        open_task_action_menu(s, parent)
        self.assertEqual(
            self._ids(s),
            ["cancel_run", "change_then_run", "clear_then_run", "close"],
        )

    def test_pending_then_run_placeholder_offers_change_and_clear(self) -> None:
        s = self._state()
        repo = _make_repo("a")
        parent = s.tasks.add("↗ a: Build")
        s.tasks.set_meta(parent, repo=repo, slug="o/a", run_id=42,
                         workflow_name="Build")
        child = s.tasks.add("  ↪ then run: Deploy", parent=parent)
        s.tasks.update(child, "pending")
        s.tasks.set_meta(child, repo=repo,
                         pending_after_workflow="Build",
                         pending_target="Deploy")
        open_task_action_menu(s, child)
        # Placeholder itself: no cancel_run (no run_id of its own),
        # but Change/Cancel-then-run + Close.
        self.assertEqual(
            self._ids(s),
            ["change_then_run", "clear_then_run", "close"],
        )

    def test_terminal_run_disables_cancel(self) -> None:
        # Run task that already finished: cancel_run is offered as a
        # disabled item so the user sees why it's unavailable.
        s = self._state()
        repo = _make_repo("a")
        t = s.tasks.add("↗ a: Build")
        s.tasks.set_meta(t, repo=repo, slug="o/a", run_id=42,
                         workflow_name="Build")
        s.tasks.update(t, "ok")
        open_task_action_menu(s, t)
        ids = self._ids(s)
        self.assertIn("cancel_run", ids)
        self.assertIn("remove", ids)
        cancel = next(it for it in s.task_action_menu.items
                      if it.id == "cancel_run")
        self.assertFalse(cancel.enabled)

    def test_open_initial_selection_skips_disabled(self) -> None:
        # When the leading items are disabled, the cursor should land
        # on the first enabled one rather than parking on a no-op row.
        s = self._state()
        repo = _make_repo("a")
        t = s.tasks.add("↗ a: Build")
        s.tasks.set_meta(t, repo=repo, slug="o/a", run_id=42,
                         workflow_name="Build")
        s.tasks.update(t, "ok")  # makes cancel_run disabled
        open_task_action_menu(s, t)
        first_enabled = next(
            i for i, it in enumerate(s.task_action_menu.items) if it.enabled)
        self.assertEqual(s.task_action_menu.selected, first_enabled)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestTaskActionMenuKeyHandler(unittest.TestCase):
    """Esc/Up/Down/Enter on the task-detail modal — the surfaces that
    don't need a real `gh` invocation. Cancel/open-in-browser spawn
    daemon threads we don't want to exercise here; close/remove/sub-
    picker are pure state transitions."""

    def _open_menu(self, status: str = "running"):
        s = _state(_make_repo("a"))
        t = s.tasks.add("housekeeping")
        if status != "running":
            s.tasks.update(t, status)
        open_task_action_menu(s, t)
        return s, t

    def test_esc_closes_menu(self) -> None:
        s, _ = self._open_menu()
        handle_task_action_menu_key(s, 27)
        self.assertIsNone(s.task_action_menu)

    def test_close_action_dismisses(self) -> None:
        s, _ = self._open_menu()
        # Plain task: only "close" exists.
        self.assertEqual(s.task_action_menu.items[0].id, "close")
        handle_task_action_menu_key(s, 10)  # Enter
        self.assertIsNone(s.task_action_menu)

    def test_remove_action_drops_task_and_closes(self) -> None:
        s, t = self._open_menu(status="ok")
        # Items: ["remove", "close"]; cursor lands on first enabled = remove.
        ids = [it.id for it in s.task_action_menu.items]
        self.assertEqual(ids[s.task_action_menu.selected], "remove")
        handle_task_action_menu_key(s, 10)
        self.assertIsNone(s.task_action_menu)
        self.assertNotIn(t, s.tasks.snapshot())

    def test_arrow_keys_cycle_selection(self) -> None:
        s, _ = self._open_menu(status="ok")  # 2 items: remove, close
        s.task_action_menu.selected = 0
        handle_task_action_menu_key(s, curses.KEY_DOWN)
        self.assertEqual(s.task_action_menu.selected, 1)
        handle_task_action_menu_key(s, curses.KEY_DOWN)
        self.assertEqual(s.task_action_menu.selected, 0)  # wrap


if __name__ == "__main__":
    unittest.main()
