"""Tests for the hints framework and per-screen registries.

These tests deliberately make exact-list assertions: the whole point
of the registry pass was that footer hints stop drifting away from
what the keys actually do, so we want a regression alarm whenever
a hint's wording or order silently changes."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import (  # noqa: E402
    Repo, State, Workspace, WorkspaceCreator, WorkspaceDraft, WorkspaceMenu,
    WorkspacesPicker,
)

# UI imports curses at module load — skip on headless.
try:
    import curses  # noqa: F401
    from ui.hints import Hint, fit_hints, render_hint
    from ui import (  # noqa: F401
        _body_row_hints, _confirm_hints, _esc_hint,
        _main_hints_global, _main_hints_primary,
        _task_panel_hints, _toggle_row_hints, _workspace_row_hints,
    )
    from ui.modals.workspace_creator import _hints as creator_hints
    from ui.modals.workspaces_picker import _hints as picker_hints
    from ui.modals.workspace_menu import (
        _hints_edit_mode, _hints_nav_mode,
    )
    from ui.modals.branch_picker import _hints as branch_picker_hints
    from ui.modals.reset_prompt import _hints as reset_prompt_hints
    from ui.modals.workflow_picker import _hints as workflow_picker_hints
    from ui.modals.align_heads_prompt import _hints as align_heads_hints
    UI_AVAILABLE = True
except Exception:  # pragma: no cover
    UI_AVAILABLE = False


def _pairs(hints):
    """Convert a hint list to (keys, action) tuples for compact
    assertions — the dataclass equality is fine but tuples diff
    nicer in failure output."""
    return [(h.keys, h.action) for h in hints]


def _make_repo(rel: str = "r") -> Repo:
    return Repo(rel=rel, path=Path(f"/tmp/{rel}"))


def _state(*repos: Repo, **kwargs) -> State:
    return State(repos=list(repos), workspace_name="ws", **kwargs)


# ---------- Framework primitives -----------------------------------------


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestRenderHint(unittest.TestCase):
    def test_render_glyph_then_action(self) -> None:
        self.assertEqual(render_hint(Hint("Enter", "commit")), "Enter commit")

    def test_render_arrow_pair(self) -> None:
        self.assertEqual(render_hint(Hint("↑/↓", "select")), "↑/↓ select")


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestFitHints(unittest.TestCase):
    def test_empty_returns_empty_string(self) -> None:
        self.assertEqual(fit_hints([], 80), "")

    def test_zero_max_returns_empty_string(self) -> None:
        self.assertEqual(fit_hints([Hint("Esc", "back")], 0), "")

    def test_full_fit_returns_joined_text(self) -> None:
        hints = [Hint("Enter", "commit"), Hint("Esc", "back")]
        self.assertEqual(fit_hints(hints, 80), "Enter commit · Esc back")

    def test_truncates_with_ellipsis_when_too_narrow(self) -> None:
        hints = [Hint("Enter", "commit"), Hint("Esc", "back"),
                 Hint("Tab", "actions")]
        # "Enter commit" = 12, separator = 3, so 12+3+1=16 fits "Enter commit · …"
        result = fit_hints(hints, 16)
        self.assertTrue(result.endswith("…"))
        self.assertIn("Enter commit", result)

    def test_single_hint_too_long_gets_ellipsis(self) -> None:
        # "very long action" doesn't fit in 5 cells.
        result = fit_hints([Hint("X", "very long action")], 5)
        self.assertEqual(len(result), 5)
        self.assertTrue(result.endswith("…"))


# ---------- Main-screen registries ---------------------------------------


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestEscHint(unittest.TestCase):
    def test_esc_quits_when_no_messages(self) -> None:
        s = _state(_make_repo("a"))
        self.assertEqual(_esc_hint(s).action, "quit")

    def test_esc_clears_message_when_focused_field_is_dirty(self) -> None:
        a = _make_repo("a")
        a.message = "wip"
        s = _state(a, selected=3)  # focus row 3 = first repo
        self.assertEqual(_esc_hint(s).action, "clear message")

    def test_esc_warns_about_discard_with_unfocused_messages(self) -> None:
        a = _make_repo("a")
        b = _make_repo("b")
        b.message = "wip"
        s = _state(a, b, selected=3)  # focused on a (no message)
        self.assertEqual(_esc_hint(s).action, "discard messages + quit")

    def test_esc_returns_back_to_repos_in_task_focus(self) -> None:
        s = _state(_make_repo("a"))
        s.focused_panel = "tasks"
        self.assertEqual(_esc_hint(s).action, "back to repos")


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestWorkspaceRowHints(unittest.TestCase):
    def _state_with(self, n_workspaces: int) -> State:
        wss = [Workspace(name=f"w{i}", folders=[Path(f"/p{i}")])
               for i in range(n_workspaces)]
        s = _state(_make_repo("r"), workspaces=wss)
        s.selected = -1
        return s

    def test_single_workspace_omits_cycle_hint(self) -> None:
        hints = _workspace_row_hints(self._state_with(1))
        keys = [h.keys for h in hints]
        self.assertNotIn("←/→", keys)

    def test_multiple_workspaces_include_cycle_hint(self) -> None:
        hints = _workspace_row_hints(self._state_with(3))
        keys = [h.keys for h in hints]
        self.assertIn("←/→", keys)

    def test_tab_and_settings_hints_always_present(self) -> None:
        hints = _workspace_row_hints(self._state_with(1))
        keys = [h.keys for h in hints]
        self.assertIn("Tab", keys)
        self.assertTrue(any("Space" in k for k in keys))


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestToggleRowHints(unittest.TestCase):
    def test_auto_stage_off_describes_turn_on(self) -> None:
        s = _state(_make_repo("r"), selected=0, auto_stage=False)
        action = _toggle_row_hints(s)[1].action
        self.assertEqual(action, "turn auto-stage on")

    def test_auto_push_on_describes_turn_off(self) -> None:
        s = _state(_make_repo("r"), selected=1, auto_push=True)
        action = _toggle_row_hints(s)[1].action
        self.assertEqual(action, "turn auto-push off")

    def test_align_heads_at_index_2(self) -> None:
        s = _state(_make_repo("r"), selected=2, align_heads=True)
        action = _toggle_row_hints(s)[1].action
        self.assertEqual(action, "turn align-heads off")


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestBodyRowHints(unittest.TestCase):
    def test_dirty_repo_shows_tab_and_suggest(self) -> None:
        a = _make_repo("a")
        a.staged = [("M", "x")]  # marks dirty
        s = _state(a, selected=3)
        keys = [h.keys for h in _body_row_hints(s)]
        self.assertIn("Tab", keys)
        self.assertIn("←", keys)  # left arrow → suggest

    def test_dirty_repo_with_message_omits_suggest_hints(self) -> None:
        a = _make_repo("a")
        a.staged = [("M", "x")]
        a.message = "fix"
        s = _state(a, selected=3)
        keys = [h.keys for h in _body_row_hints(s)]
        # Suggest hints disappear once a message is typed.
        self.assertNotIn("←", keys)
        # Enter is offered as commit because state.has_messages.
        actions = [h.action for h in _body_row_hints(s)]
        self.assertIn("review + commit", actions)

    def test_clean_repo_omits_enter_when_no_messages(self) -> None:
        a = _make_repo("a")
        s = _state(a, selected=3)
        actions = [h.action for h in _body_row_hints(s)]
        self.assertNotIn("review + commit", actions)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestTaskPanelHints(unittest.TestCase):
    def test_empty_panel_emits_no_hints(self) -> None:
        s = _state(_make_repo("a"))
        s.focused_panel = "tasks"
        self.assertEqual(_task_panel_hints(s), [])

    def test_running_task_shows_no_remove_hint(self) -> None:
        s = _state(_make_repo("a"))
        s.focused_panel = "tasks"
        s.tasks.add("doing thing")
        actions = [h.action for h in _task_panel_hints(s)]
        self.assertNotIn("remove task", actions)

    def test_completed_task_shows_remove_hint(self) -> None:
        s = _state(_make_repo("a"))
        s.focused_panel = "tasks"
        t = s.tasks.add("done thing")
        s.tasks.update(t, "ok")
        actions = [h.action for h in _task_panel_hints(s)]
        self.assertIn("remove task", actions)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestMainHintsRouting(unittest.TestCase):
    """The primary-line dispatcher should pick the right registry for
    the focused zone — exercise each branch end-to-end."""

    def test_workspace_row_routes_to_workspace_hints(self) -> None:
        ws = Workspace(name="w", folders=[Path("/p")])
        s = _state(_make_repo("r"), workspaces=[ws])
        s.selected = -1
        actions = [h.action for h in _main_hints_primary(s)]
        # workspaces row always offers Tab + settings.
        self.assertIn("workspaces…", actions)

    def test_toggle_row_routes_to_toggle_hints(self) -> None:
        s = _state(_make_repo("r"), selected=0, auto_stage=True)
        actions = [h.action for h in _main_hints_primary(s)]
        self.assertEqual(actions[1], "turn auto-stage off")

    def test_body_row_routes_to_body_hints(self) -> None:
        a = _make_repo("a")
        a.staged = [("M", "x")]
        s = _state(a, selected=3)
        keys = [h.keys for h in _main_hints_primary(s)]
        self.assertIn("Tab", keys)

    def test_task_focus_routes_to_task_panel_hints(self) -> None:
        s = _state(_make_repo("r"))
        s.focused_panel = "tasks"
        s.tasks.add("x")
        keys = [h.keys for h in _main_hints_primary(s)]
        self.assertIn("Tab", keys)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestMainHintsGlobal(unittest.TestCase):
    def test_repos_focus_advertises_tasks_panel(self) -> None:
        s = _state(_make_repo("r"))
        actions = [h.action for h in _main_hints_global(s)]
        self.assertIn("tasks panel", actions)

    def test_tasks_focus_advertises_back_to_repos(self) -> None:
        s = _state(_make_repo("r"))
        s.focused_panel = "tasks"
        actions = [h.action for h in _main_hints_global(s)]
        self.assertIn("back to repos", actions)


# ---------- Confirm-screen hints -----------------------------------------


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestConfirmHints(unittest.TestCase):
    def test_no_focusables_offers_scroll_and_execute(self) -> None:
        actions = [h.action for h in _confirm_hints([], [], [], cursor=-1)]
        self.assertIn("scroll", actions)
        self.assertIn("execute commits", actions)
        self.assertIn("back", actions)


# ---------- Modal registries ---------------------------------------------


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestBranchPickerHints(unittest.TestCase):
    def _picker(self, branches=("main", "dev"), current="main", selected=0):
        from models import BranchPicker
        return BranchPicker(
            target_label="x", target_path=Path("/tmp"),
            branches=list(branches), current=current, selected=selected)

    def test_empty_branches_only_back(self) -> None:
        p = self._picker(branches=())
        self.assertEqual(_pairs(branch_picker_hints(p)),
                         [("Esc", "back")])

    def test_focused_on_current_branch_shows_stay(self) -> None:
        p = self._picker(branches=("main",), current="main", selected=0)
        actions = [h.action for h in branch_picker_hints(p)]
        self.assertIn("stay (already checked out)", actions)

    def test_focused_on_other_branch_names_checkout_target(self) -> None:
        p = self._picker(branches=("main", "dev"), current="main", selected=1)
        actions = [h.action for h in branch_picker_hints(p)]
        self.assertIn("checkout dev", actions)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestResetPromptHints(unittest.TestCase):
    def _prompt(self, typed=""):
        from models import ResetPrompt
        return ResetPrompt(target_label="x", target_path=Path("/tmp"),
                           typed=typed)

    def test_empty_input_offers_wipe_all(self) -> None:
        actions = [h.action for h in reset_prompt_hints(self._prompt())]
        self.assertIn("wipe ALL unpushed", actions)

    def test_typed_count_describes_exact_count(self) -> None:
        actions = [h.action for h in reset_prompt_hints(self._prompt("3"))]
        self.assertIn("reset 3 commits", actions)

    def test_typed_zero_still_offers_wipe_all(self) -> None:
        actions = [h.action for h in reset_prompt_hints(self._prompt("0"))]
        self.assertIn("wipe ALL unpushed", actions)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestWorkflowPickerHints(unittest.TestCase):
    def test_runnable_row_describes_branch_target(self) -> None:
        from models import WorkflowInfo, WorkflowPicker
        wf = WorkflowInfo(
            name="ci", path=".github/workflows/ci.yml",
            state="active", dispatchable=True)
        p = WorkflowPicker(target_label="x", target_path=Path("/tmp"),
                           workflows=[wf], branch="main", selected=0)
        actions = [h.action for h in workflow_picker_hints(p)]
        self.assertIn("run on main", actions)

    def test_disabled_workflow_marks_unavailable(self) -> None:
        from models import WorkflowInfo, WorkflowPicker
        wf = WorkflowInfo(
            name="ci", path=".github/workflows/ci.yml",
            state="disabled_manually", dispatchable=True)
        p = WorkflowPicker(target_label="x", target_path=Path("/tmp"),
                           workflows=[wf], branch="main", selected=0)
        actions = [h.action for h in workflow_picker_hints(p)]
        self.assertTrue(any(a.startswith("unavailable") for a in actions))


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestAlignHeadsHints(unittest.TestCase):
    def test_branch_chosen_names_target(self) -> None:
        from models import AlignHeadsPrompt
        p = AlignHeadsPrompt(canonical_label="x", winner_label="y",
                             winner_sha="deadbeef",
                             branches=["main", "dev"], selected=1)
        actions = [h.action for h in align_heads_hints(p)]
        self.assertIn("push winner to dev", actions)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestWorkspacesPickerHints(unittest.TestCase):
    def test_create_row_says_create(self) -> None:
        ws = Workspace(name="A", folders=[Path("/a")])
        s = _state(_make_repo("r"), workspaces=[ws])
        s.workspaces_picker = WorkspacesPicker(selected=1)  # past last
        actions = [h.action for h in picker_hints(s)]
        self.assertIn("create new workspace…", actions)

    def test_active_row_says_stay(self) -> None:
        ws = Workspace(name="A", folders=[Path("/a")])
        s = _state(_make_repo("r"), workspaces=[ws],
                   active_workspace_index=0)
        s.workspaces_picker = WorkspacesPicker(selected=0)
        actions = [h.action for h in picker_hints(s)]
        self.assertIn("stay (already active)", actions)

    def test_other_workspace_row_names_target(self) -> None:
        a = Workspace(name="A", folders=[Path("/a")])
        b = Workspace(name="B", folders=[Path("/b")])
        s = _state(_make_repo("r"), workspaces=[a, b],
                   active_workspace_index=0)
        s.workspaces_picker = WorkspacesPicker(selected=1)
        actions = [h.action for h in picker_hints(s)]
        self.assertIn("switch to B", actions)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestWorkspaceCreatorHints(unittest.TestCase):
    def test_done_row_with_one_path_says_create_1(self) -> None:
        c = WorkspaceCreator(
            drafts=[WorkspaceDraft(path_text="/tmp")], selected=1)
        actions = [h.action for h in creator_hints(c)]
        self.assertIn("create 1 workspace", actions)

    def test_done_row_with_multiple_says_create_n(self) -> None:
        c = WorkspaceCreator(
            drafts=[WorkspaceDraft(path_text="/a"),
                    WorkspaceDraft(path_text="/b"),
                    WorkspaceDraft(path_text="/c")], selected=3)
        actions = [h.action for h in creator_hints(c)]
        self.assertIn("create 3 workspaces", actions)

    def test_done_row_with_no_paths_warns(self) -> None:
        c = WorkspaceCreator(drafts=[WorkspaceDraft()], selected=1)
        actions = [h.action for h in creator_hints(c)]
        self.assertIn("type a path above first", actions)


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestWorkspaceMenuHints(unittest.TestCase):
    def _state_with_menu(self, folders) -> State:
        from config import Config
        ws = Workspace(name="W", folders=list(folders))
        s = _state(_make_repo("r"), workspaces=[ws],
                   active_workspace_index=0, base_config=Config())
        return s

    def test_edit_mode_advertises_save_and_cancel(self) -> None:
        m = WorkspaceMenu(editing=True)
        actions = [h.action for h in _hints_edit_mode(m)]
        self.assertIn("save path", actions)
        self.assertIn("cancel edit", actions)

    def test_folder_row_with_multiple_offers_remove(self) -> None:
        from ui.modals.workspace_menu import _build_rows
        s = self._state_with_menu([Path("/a"), Path("/b")])
        rows = _build_rows(s.active_workspace)
        m = WorkspaceMenu(rows=rows)
        # Find the first folder row index
        for i, r in enumerate(rows):
            if r.kind == "folder":
                m.selected = i
                break
        actions = [h.action for h in _hints_nav_mode(s, m)]
        self.assertIn("remove folder", actions)

    def test_folder_row_with_single_refuses_remove(self) -> None:
        from ui.modals.workspace_menu import _build_rows
        s = self._state_with_menu([Path("/only")])
        rows = _build_rows(s.active_workspace)
        m = WorkspaceMenu(rows=rows)
        for i, r in enumerate(rows):
            if r.kind == "folder":
                m.selected = i
                break
        actions = [h.action for h in _hints_nav_mode(s, m)]
        self.assertIn("(can't remove last folder)", actions)


if __name__ == "__main__":
    unittest.main()
