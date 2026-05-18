"""Workspace + multi-workspace tests — config loader/saver, State
helpers, key handlers for the title-row selector, and the workspace
creator/menu modals."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import config  # noqa: E402
from _helpers import (  # noqa: E402
    make_repo_model as _make_repo, make_state as _state,
)
from core.config import (  # noqa: E402
    Config, apply_workspace_overrides, base_value_for_override,
    coerce_override_value, get_load_warnings, load_config,
    load_workspaces, save_workspaces, set_conf_value,
    state_attr_value_from_override,
)
from core.models import (  # noqa: E402
    State, Workspace, WorkspaceCreator, WorkspaceDraft,
    WorkspaceMenu,
)

# UI handlers depend on curses; skip the whole module on headless CI
# the same way test_keys.py does.
try:
    import curses  # noqa: F401
    from ui import handle_main_key  # noqa: F401
    from ui.modals.workspace_creator import (  # noqa: F401
        commit_workspace_creator, handle_workspace_creator_key,
        open_workspace_creator,
    )
    from ui.modals.workspace_menu import (  # noqa: F401
        handle_workspace_menu_key, open_workspace_menu,
    )
    from ui.modals.app_menu import (  # noqa: F401
        handle_app_menu_key, open_app_menu,
    )
    UI_AVAILABLE = True
except Exception:  # pragma: no cover
    UI_AVAILABLE = False


# ---------- Config loader / overrides -------------------------------------


class TestOverrideCoercion(unittest.TestCase):
    def test_bool_truthy_values(self) -> None:
        for raw in ("true", "True", "yes", "on", "1"):
            self.assertTrue(coerce_override_value("default_auto_stage", raw))

    def test_bool_falsy_values(self) -> None:
        for raw in ("false", "False", "no", "off", "0"):
            self.assertFalse(coerce_override_value("default_auto_stage", raw))

    def test_bool_garbage_returns_none(self) -> None:
        self.assertIsNone(coerce_override_value("default_auto_stage", "maybe"))

    def test_prevent_merge_key_coerces(self) -> None:
        self.assertTrue(coerce_override_value(
            "default_prevent_smart_sync_silent_merge", "true"))
        self.assertFalse(coerce_override_value(
            "default_prevent_smart_sync_silent_merge", "false"))

    def test_int_round_trips(self) -> None:
        self.assertEqual(coerce_override_value("suggest_added", "5"), 5)
        self.assertIsNone(coerce_override_value("suggest_added", "five"))

    def test_trunc_mode_normalizes(self) -> None:
        self.assertEqual(
            coerce_override_value("name_truncation", "MIDDLE"), "middle")
        self.assertIsNone(coerce_override_value("name_truncation", "weird"))

    def test_unknown_key_dropped(self) -> None:
        self.assertIsNone(coerce_override_value("not_a_key", "value"))


class TestConfigWarnings(unittest.TestCase):
    def test_malformed_config_surfaces_warning_and_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.conf"
            tmp.write_text("[idlegit]\nsuggest_added = nope\n")
            with mock.patch.object(config, "CONFIG_FILE", tmp):
                cfg = load_config()

        self.assertEqual(cfg.suggest_added, config.DEFAULT_SUGGEST)
        self.assertTrue(any("using defaults" in w
                            for w in get_load_warnings()))

    def test_task_width_percentages_load_and_clamp(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.conf"
            tmp.write_text(
                "[idlegit]\n"
                "tasks_min_width_percent = 0.25\n"
                "tasks_max_width_percent = 2.0\n")
            with mock.patch.object(config, "CONFIG_FILE", tmp):
                cfg = load_config()

        self.assertEqual(cfg.tasks_min_width_percent, 0.25)
        self.assertEqual(cfg.tasks_max_width_percent, 1.0)


class TestApplyWorkspaceOverrides(unittest.TestCase):
    def test_resets_state_to_base_then_applies_overrides(self) -> None:
        cfg = Config(
            suggest_added=3, name_truncation="middle",
            default_auto_stage=True)
        ws = Workspace(
            name="W", folders=[Path("/tmp")],
            overrides={
                "default_auto_stage": False,
                "suggest_added": 7,
                "name_truncation": "end",
            })
        s = _state(_make_repo("a"))
        # Pretend a previous workspace had set non-default values.
        s.auto_stage = True
        s.suggest_added = 99
        s.name_truncation = "start"
        apply_workspace_overrides(s, cfg, ws)
        self.assertFalse(s.auto_stage)
        self.assertEqual(s.suggest_added, 7)
        self.assertEqual(s.name_truncation, "end")

    def test_unset_keys_revert_to_base(self) -> None:
        cfg = Config(suggest_added=3)
        ws = Workspace(name="W", folders=[Path("/tmp")])  # no overrides
        s = _state(_make_repo("a"))
        s.suggest_added = 99
        apply_workspace_overrides(s, cfg, ws)
        self.assertEqual(s.suggest_added, 3)

    def test_lfs_warn_mb_translates_to_bytes(self) -> None:
        cfg = Config()  # default 100 MB
        ws = Workspace(
            name="W", folders=[Path("/tmp")],
            overrides={"lfs_warn_mb": 50})
        s = _state(_make_repo("a"))
        apply_workspace_overrides(s, cfg, ws)
        self.assertEqual(s.lfs_warn_bytes, 50 * 1024 * 1024)

    def test_prevent_smart_sync_merge_override(self) -> None:
        cfg = Config()
        ws = Workspace(
            name="W", folders=[Path("/tmp")],
            overrides={"default_prevent_smart_sync_silent_merge": True})
        s = _state(_make_repo("a"))
        apply_workspace_overrides(s, cfg, ws)
        self.assertTrue(s.prevent_smart_sync_silent_merge)


class TestSetConfValue(unittest.TestCase):
    """`set_conf_value` is the writer behind app-menu toggles (today:
    task logging on/off). Comments + ordering MUST round-trip — the
    bundled `idlegit.default.conf` ships an inline-comment-per-key hint,
    and losing those on every UI toggle would shred the docs the user
    relies on. These tests pin down the preservation contract."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name) / "idlegit.conf"
        self._patch = mock.patch.object(config, "CONFIG_FILE", self.tmp_path)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        # `_ensure_config_ready` seeds CONFIG_FILE from the bundled
        # template; the patched path is empty so it triggers the seed
        # on first call. Patch `_ensure_config_ready` to a no-op
        # instead so the tests run hermetically without depending on
        # the template's exact contents — we write the seed we want.
        self._ensure_patch = mock.patch.object(
            config, "_ensure_config_ready", lambda: None)
        self._ensure_patch.start()
        self.addCleanup(self._ensure_patch.stop)

    def _seed(self, text: str) -> None:
        self.tmp_path.parent.mkdir(parents=True, exist_ok=True)
        self.tmp_path.write_text(text, encoding="utf-8")

    def test_updates_existing_key_in_place(self) -> None:
        self._seed("[idlegit]\ntask_log_enabled = false\n")
        self.assertTrue(set_conf_value("task_log_enabled", "true"))
        text = self.tmp_path.read_text(encoding="utf-8")
        self.assertIn("task_log_enabled = true", text)
        self.assertNotIn("task_log_enabled = false", text)

    def test_preserves_inline_comment(self) -> None:
        # The default template ships every key with a `; explanation`
        # comment. The writer must keep that comment intact so the
        # docs survive each UI toggle.
        self._seed(
            "[idlegit]\n"
            "task_log_enabled = false   ; off by default\n")
        self.assertTrue(set_conf_value("task_log_enabled", "true"))
        text = self.tmp_path.read_text(encoding="utf-8")
        self.assertIn("task_log_enabled = true", text)
        self.assertIn("; off by default", text)

    def test_preserves_unrelated_keys_and_comments(self) -> None:
        self._seed(
            "; top-of-file note\n"
            "[idlegit]\n"
            "suggest_added = 5   ; keep me\n"
            "task_log_enabled = false\n"
            "name_truncation = middle\n")
        self.assertTrue(set_conf_value("task_log_enabled", "true"))
        text = self.tmp_path.read_text(encoding="utf-8")
        self.assertIn("; top-of-file note", text)
        self.assertIn("suggest_added = 5", text)
        self.assertIn("; keep me", text)
        self.assertIn("name_truncation = middle", text)
        self.assertIn("task_log_enabled = true", text)

    def test_appends_missing_key_under_idlegit_section(self) -> None:
        self._seed("[idlegit]\nsuggest_added = 5\n")
        self.assertTrue(set_conf_value("task_log_enabled", "true"))
        text = self.tmp_path.read_text(encoding="utf-8")
        self.assertIn("task_log_enabled = true", text)
        # The new key sits inside the [idlegit] section (no other
        # sections exist yet, so it lands at the end of the file).
        self.assertIn("[idlegit]", text)

    def test_round_trip_via_load_config(self) -> None:
        # The whole point of the writer: a toggle persists across a
        # reload. Write `task_log_enabled = true`, reload the Config,
        # and verify the live value reflects the change.
        self._seed("[idlegit]\ntask_log_enabled = false\n")
        self.assertTrue(set_conf_value("task_log_enabled", "true"))
        cfg = load_config()
        self.assertTrue(cfg.task_log_enabled)

    def test_handles_missing_file_by_seeding(self) -> None:
        # No prior conf exists — writer must create one with the new
        # key under [idlegit] rather than failing.
        self.assertFalse(self.tmp_path.exists())
        # `_ensure_config_ready` is patched to no-op in setUp, so the
        # writer's own missing-file path is what's being exercised.
        # That path expects the file to exist (read_text fails OSError),
        # so simulate the post-`_ensure_config_ready` state with an
        # empty section header.
        self._seed("[idlegit]\n")
        self.assertTrue(set_conf_value("task_log_enabled", "true"))
        text = self.tmp_path.read_text(encoding="utf-8")
        self.assertIn("task_log_enabled = true", text)


class TestBaseValueLookup(unittest.TestCase):
    def test_state_attr_translation_for_lfs_warn_mb(self) -> None:
        # 50 MB persisted → 50 * 1024 * 1024 bytes runtime.
        self.assertEqual(
            state_attr_value_from_override("lfs_warn_mb", 50),
            50 * 1024 * 1024)

    def test_state_attr_translation_passthrough(self) -> None:
        self.assertEqual(
            state_attr_value_from_override("suggest_added", 7), 7)

    def test_state_attr_translation_for_task_width_percent(self) -> None:
        self.assertEqual(
            state_attr_value_from_override("tasks_min_width_percent", 1.5),
            1.0)

    def test_base_value_for_lfs_warn_mb_reads_bytes(self) -> None:
        cfg = Config(lfs_warn_bytes=100 * 1024 * 1024)
        self.assertEqual(base_value_for_override(cfg, "lfs_warn_mb"), 100)

    def test_base_value_for_unknown_key_is_none(self) -> None:
        cfg = Config()
        self.assertIsNone(base_value_for_override(cfg, "made_up_key"))


# ---------- Workspaces file round-trip ------------------------------------


class TestWorkspacesFileRoundTrip(unittest.TestCase):
    """Stub the WORKSPACES_FILE module global to point at a tmp file so
    save_workspaces / load_workspaces talk to a real disk roundtrip
    without polluting the user's idlegit/ checkout."""

    def test_missing_file_returns_empty_list(self) -> None:
        # Returning ([], 0) is the contract that signals "no workspaces
        # yet" — idlegit.run() interprets it as "launch the creator
        # wizard." The trailing 0 is the persisted active-workspace
        # index (irrelevant when the list is empty).
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(config, "WORKSPACES_FILE",
                                   Path(d) / "missing.workspaces"):
                ws, active_idx = load_workspaces()
        self.assertEqual(ws, [])
        self.assertEqual(active_idx, 0)

    def test_save_then_load_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.workspaces"
            with mock.patch.object(config, "WORKSPACES_FILE", tmp):
                src = [
                    Workspace(name="Personal",
                              folders=[Path(d) / "p1"],
                              overrides={"default_auto_stage": False,
                                         "suggest_added": 5}),
                    Workspace(name="Work",
                              folders=[Path(d) / "w1", Path(d) / "w2"],
                              overrides={"name_truncation": "end"}),
                ]
                for ws in src:
                    for f in ws.folders:
                        f.mkdir(parents=True, exist_ok=True)
                save_workspaces(src, active_index=0)
                loaded, _ = load_workspaces()
        names = [w.name for w in loaded]
        self.assertEqual(names, ["Personal", "Work"])
        personal = loaded[0]
        self.assertEqual(personal.overrides.get("default_auto_stage"), False)
        self.assertEqual(personal.overrides.get("suggest_added"), 5)
        work = loaded[1]
        self.assertEqual(len(work.folders), 2)
        self.assertEqual(work.overrides.get("name_truncation"), "end")

    def test_active_workspace_persists_across_save_load(self) -> None:
        # The non-zero active index round-trips by name through the
        # [idlegit] / active_workspace key.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.workspaces"
            with mock.patch.object(config, "WORKSPACES_FILE", tmp):
                src = [
                    Workspace(name="A", folders=[Path(d) / "a"]),
                    Workspace(name="B", folders=[Path(d) / "b"]),
                    Workspace(name="C", folders=[Path(d) / "c"]),
                ]
                for ws in src:
                    for f in ws.folders:
                        f.mkdir(parents=True, exist_ok=True)
                save_workspaces(src, active_index=2)
                loaded, active_idx = load_workspaces()
        self.assertEqual([w.name for w in loaded], ["A", "B", "C"])
        self.assertEqual(active_idx, 2)

    def test_active_workspace_falls_back_when_name_missing(self) -> None:
        # If the persisted active_workspace points at a workspace that
        # no longer exists in the file, the loader falls back to 0
        # rather than raising.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.workspaces"
            tmp.write_text(
                "[idlegit]\nactive_workspace = Gone\n\n"
                f"[workspace.Stays]\nfolders = {d}\n")
            with mock.patch.object(config, "WORKSPACES_FILE", tmp):
                loaded, active_idx = load_workspaces()
        self.assertEqual([w.name for w in loaded], ["Stays"])
        self.assertEqual(active_idx, 0)

    def test_dotted_workspace_name_survives_round_trip(self) -> None:
        # Regression: the loader used to skip any section whose
        # remainder contained a "." (the .subtree.<name> guard was
        # too eager), so workspaces named "Upskill.Health" or "A.B.C"
        # silently disappeared on reload.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.workspaces"
            with mock.patch.object(config, "WORKSPACES_FILE", tmp):
                src = [
                    Workspace(name="Upskill.Health",
                              folders=[Path(d) / "u"]),
                    Workspace(name="A.B.C",
                              folders=[Path(d) / "a"]),
                ]
                for ws in src:
                    for f in ws.folders:
                        f.mkdir(parents=True, exist_ok=True)
                save_workspaces(src, active_index=0)
                loaded, _ = load_workspaces()
        self.assertEqual([w.name for w in loaded],
                         ["Upskill.Health", "A.B.C"])

    def test_malformed_section_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.workspaces"
            tmp.write_text("[workspace.NoFolders]\nname = oops\n\n"
                           "[workspace.Good]\nfolders = "
                           f"{d}\n")
            with mock.patch.object(config, "WORKSPACES_FILE", tmp):
                loaded, _ = load_workspaces()
        self.assertEqual([w.name for w in loaded], ["Good"])

    def test_malformed_workspaces_file_surfaces_warning(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.workspaces"
            tmp.write_text("not a section\n")
            with mock.patch.object(config, "WORKSPACES_FILE", tmp):
                loaded, active_idx = load_workspaces()

        self.assertEqual(loaded, [])
        self.assertEqual(active_idx, 0)
        self.assertTrue(any("could not read" in w
                            for w in get_load_warnings()))

    def test_bad_folder_line_is_skipped_without_losing_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            good = Path(d) / "good"
            good.mkdir()
            tmp = Path(d) / "idlegit.workspaces"
            bad = Path(d) / "bad"
            tmp.write_text(
                "[workspace.W]\nfolders = "
                f"{good}\n"
                f"          {bad}\n")
            with mock.patch.object(Path, "resolve",
                                   autospec=True) as resolve:
                def fake_resolve(path):
                    if path == bad:
                        raise OSError("denied")
                    return path

                resolve.side_effect = fake_resolve
                with mock.patch.object(config, "WORKSPACES_FILE", tmp):
                    loaded, _ = load_workspaces()

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].folders, [good])
        self.assertTrue(any("workspace folder ignored" in w
                            for w in get_load_warnings()))


# ---------- State helpers --------------------------------------------------


class TestStateWorkspaceProperties(unittest.TestCase):
    def test_active_workspace_is_none_when_empty(self) -> None:
        s = _state(_make_repo("a"))
        self.assertIsNone(s.active_workspace)
        self.assertEqual(s.active_folders, [])

    def test_active_workspace_returned_at_index(self) -> None:
        ws_a = Workspace(name="A", folders=[Path("/a")])
        ws_b = Workspace(name="B", folders=[Path("/b")])
        s = _state(_make_repo("r"), workspaces=[ws_a, ws_b],
                   active_workspace_index=1)
        self.assertIs(s.active_workspace, ws_b)
        self.assertEqual([str(f) for f in s.active_folders], ["/b"])

    def test_active_workspace_clamps_out_of_range_index(self) -> None:
        ws_a = Workspace(name="A", folders=[Path("/a")])
        s = _state(_make_repo("r"), workspaces=[ws_a],
                   active_workspace_index=5)
        # Out-of-range index clamps back into the list rather than
        # raising — defensive against stale / corrupt state.
        self.assertIs(s.active_workspace, ws_a)


class TestSwitchWorkspaceCache(unittest.TestCase):
    """switch_workspace prefers each workspace's `cached_repos` over a
    fresh discover so rapid ←/→ keystrokes don't churn discovery and
    don't leave the user staring at half-loaded repos while a refresh
    races on a stale folder list.

    EVERY test in this class MUST patch save_workspaces — switch_workspace
    persists the new active index on every call, and an unmocked test
    would clobber the user's real idlegit.workspaces with the throw-away
    fixture data below. (We learned this the hard way.)"""

    def setUp(self) -> None:
        # Defensive belt-and-braces: also point WORKSPACES_FILE at a
        # tempdir so even if a future test forgets to patch
        # save_workspaces directly, persistence lands somewhere
        # disposable rather than the real config file.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._patches = [
            mock.patch.object(config, "WORKSPACES_FILE",
                              Path(self._tmp.name) / "idlegit.workspaces"),
            mock.patch("core.workers.kick_off_inline_refresh"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _state(self, *workspaces) -> State:
        s = State(repos=[], workspace_name="",
                  workspaces=list(workspaces),
                  active_workspace_index=0)
        if workspaces:
            s.repos = list(workspaces[0].cached_repos)
            s.workspace_name = workspaces[0].name
        return s

    def test_cache_hit_skips_discovery_but_kicks_background_refresh(self) -> None:
        # Cache hit gives an instant visual swap (no discover_repos),
        # then kicks an inline refresh so per-repo dirty/branch state
        # — possibly stale from edits made while the user was on a
        # different workspace — gets corrected within a frame or two.
        # fs_watcher tears down watchers for the away workspace, so
        # without this kick the cached state would remain stale until
        # the user hit Ctrl+R.
        from core.workers import switch_workspace
        a_repos = [_make_repo("a1"), _make_repo("a2")]
        b_repos = [_make_repo("b1")]
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=a_repos)
        b = Workspace(name="B", folders=[Path("/b")], cached_repos=b_repos)
        s = self._state(a, b)
        with mock.patch("core.workers.discover_repos") as disc, \
             mock.patch("core.workers.kick_off_inline_refresh") as kick:
            switch_workspace(s, 1)
            disc.assert_not_called()
            kick.assert_called_once()
        self.assertIs(s.repos, b_repos)
        self.assertEqual(s.workspace_name, "B")

    def test_cache_miss_falls_back_to_discover_plus_async_refresh(self) -> None:
        from core.workers import switch_workspace
        a_repos = [_make_repo("a1")]
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=a_repos)
        b = Workspace(name="B", folders=[Path("/b")])
        s = self._state(a, b)
        fresh = [_make_repo("b1"), _make_repo("b2")]
        with mock.patch("core.workers.discover_repos", return_value=fresh) as disc, \
             mock.patch("core.workers.kick_off_inline_refresh") as kick:
            switch_workspace(s, 1)
            disc.assert_called_once()
            kick.assert_called_once()
        self.assertIs(s.repos, b.cached_repos)
        self.assertEqual([r.rel for r in b.cached_repos], ["b1", "b2"])

    def test_in_place_repo_mutations_persist_across_switches(self) -> None:
        from core.workers import switch_workspace
        a_repos = [_make_repo("a1")]
        a_repos[0].message = "pending edit"
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=a_repos)
        b = Workspace(name="B", folders=[Path("/b")],
                      cached_repos=[_make_repo("b1")])
        s = self._state(a, b)
        with mock.patch("core.workers.kick_off_inline_refresh"):
            switch_workspace(s, 1)  # A → B
            switch_workspace(s, 0)  # B → A
        # Coming back to A surfaces the unsaved message exactly as it
        # was — no flicker, no fresh empty Repo.
        self.assertEqual(s.repos[0].message, "pending edit")

    def test_on_workspace_row_sentinel(self) -> None:
        s = _state(_make_repo("a"))
        s.selected = -1
        self.assertTrue(s.on_workspace_row)
        s.selected = 0
        self.assertFalse(s.on_workspace_row)

    def test_workspace_row_is_a_distinct_zone_from_body(self) -> None:
        # The toggle row that used to occupy indices 0..2 is gone; only
        # the workspace row (-1) and body rows (>=0) are reachable now.
        s = _state(_make_repo("a"))
        s.selected = -1
        self.assertTrue(s.on_workspace_row)
        s.selected = 0
        self.assertFalse(s.on_workspace_row)


# ---------- Key handler — workspace row + cycling -------------------------


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestWorkspaceRowKeys(unittest.TestCase):
    def _state_two_ws(self) -> State:
        ws_a = Workspace(name="A", folders=[Path("/a")])
        ws_b = Workspace(name="B", folders=[Path("/b")])
        s = _state(_make_repo("r"), workspaces=[ws_a, ws_b],
                   active_workspace_index=0)
        s.selected = -1
        return s

    def test_down_from_workspace_row_lands_on_first_body_row(self) -> None:
        s = self._state_two_ws()
        handle_main_key(s, curses.KEY_DOWN)
        self.assertEqual(s.selected, 0)

    def test_up_from_workspace_row_lands_on_title_row(self) -> None:
        s = self._state_two_ws()
        handle_main_key(s, curses.KEY_UP)
        # New 3-level nav: workspace row Up → title row at -2.
        self.assertEqual(s.selected, -2)

    def test_left_right_cycle_workspace_calls_switch(self) -> None:
        s = self._state_two_ws()
        with mock.patch("core.workers.switch_workspace") as m:
            handle_main_key(s, curses.KEY_RIGHT)
            self.assertEqual(m.call_count, 1)
            self.assertEqual(m.call_args.args[1], 1)
            handle_main_key(s, curses.KEY_LEFT)
            self.assertEqual(m.call_count, 2)
            # `cycle` was called from index 0 with -1 → wraps to len-1 = 1
            self.assertEqual(m.call_args.args[1], 1)

    def test_left_right_no_op_with_single_workspace(self) -> None:
        ws_a = Workspace(name="A", folders=[Path("/a")])
        s = _state(_make_repo("r"), workspaces=[ws_a])
        s.selected = -1
        with mock.patch("core.workers.switch_workspace") as m:
            handle_main_key(s, curses.KEY_RIGHT)
            handle_main_key(s, curses.KEY_LEFT)
            self.assertEqual(m.call_count, 0)

    def test_tab_opens_workspace_menu(self) -> None:
        s = self._state_two_ws()
        self.assertIsNone(s.workspace_menu)
        handle_main_key(s, 9)  # Tab
        self.assertIsNotNone(s.workspace_menu)

    def test_enter_no_op_on_workspace_row(self) -> None:
        # Tab now opens settings; Enter on the workspace row does
        # nothing (no per-row commit-pipeline target here).
        s = self._state_two_ws()
        handle_main_key(s, 10)
        self.assertIsNone(s.workspace_menu)


# ---------- Workspace creator modal ---------------------------------------


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestWorkspaceCreator(unittest.TestCase):
    def _state(self) -> State:
        s = _state(_make_repo("r"))
        open_workspace_creator(s)
        return s

    def test_typing_appends_to_focused_draft(self) -> None:
        s = self._state()
        for ch in "/tmp":
            handle_workspace_creator_key(s, ord(ch))
        self.assertEqual(s.workspace_creator.drafts[0].path_text, "/tmp")
        self.assertEqual(s.workspace_creator.field_cursor, 4)

    def test_enter_advances_to_next_row_and_seeds_blank(self) -> None:
        s = self._state()
        for ch in "/tmp":
            handle_workspace_creator_key(s, ord(ch))
        handle_workspace_creator_key(s, 10)  # Enter
        self.assertEqual(len(s.workspace_creator.drafts), 2)
        self.assertEqual(s.workspace_creator.selected, 1)

    def test_down_from_last_blank_jumps_to_done(self) -> None:
        s = self._state()
        # Single blank draft + Down should jump to Done row (index 1).
        handle_workspace_creator_key(s, curses.KEY_DOWN)
        self.assertEqual(s.workspace_creator.selected, 1)

    def test_enter_on_done_with_drafts_commits(self) -> None:
        s = self._state()
        for ch in "/tmp":
            handle_workspace_creator_key(s, ord(ch))
        # After typing the first row gains a trailing-blank sibling, so
        # we have 2 drafts ("/tmp", "") and Done is index 2. Down once
        # lands on the blank; Down again jumps to Done (the blank-row
        # shortcut catches it).
        handle_workspace_creator_key(s, curses.KEY_DOWN)
        handle_workspace_creator_key(s, curses.KEY_DOWN)
        self.assertEqual(s.workspace_creator.selected,
                         len(s.workspace_creator.drafts))
        handle_workspace_creator_key(s, 10)
        # `result` is set to the workspace list on commit.
        result = s.workspace_creator.result
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].folders[0].name, "tmp")

    def test_esc_cancels(self) -> None:
        s = self._state()
        for ch in "/tmp":
            handle_workspace_creator_key(s, ord(ch))
        handle_workspace_creator_key(s, 27)
        # Esc closes the modal (sets to None) and stamps an empty result.
        self.assertIsNone(s.workspace_creator)


# ---------- Workspace overrides menu --------------------------------------


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestWorkspaceMenuOverrides(unittest.TestCase):
    def _state(self) -> State:
        cfg = Config()
        ws = Workspace(name="W", folders=[Path("/tmp")])
        s = _state(_make_repo("r"), workspaces=[ws],
                   active_workspace_index=0, base_config=cfg)
        s.auto_stage = cfg.default_auto_stage
        return s

    def test_open_installs_modal(self) -> None:
        s = self._state()
        open_workspace_menu(s)
        self.assertIsInstance(s.workspace_menu, WorkspaceMenu)
        self.assertGreater(len(s.workspace_menu.rows), 0)

    def _focus_row_by_attr(self, s: State, attr_name: str) -> None:
        """Move the menu cursor onto the row whose `attr_name` matches —
        the rows now include a folders section + headers, so tests can't
        assume a fixed offset."""
        menu = s.workspace_menu
        for i, row in enumerate(menu.rows):
            if row.attr_name == attr_name and row.kind != "header":
                menu.selected = i
                return
        raise AssertionError(f"row with attr_name={attr_name!r} not found")

    def test_space_on_bool_row_toggles_value_and_persists_override(self) -> None:
        s = self._state()
        open_workspace_menu(s)
        self._focus_row_by_attr(s, "default_auto_stage")
        with mock.patch("ui.modals.workspace_menu.save_workspaces"):
            handle_workspace_menu_key(s, ord(" "))
        ws = s.active_workspace
        self.assertIn("default_auto_stage", ws.overrides)
        # State should have flipped too.
        self.assertEqual(s.auto_stage, ws.overrides["default_auto_stage"])

    def test_backspace_clears_override_and_restores_base(self) -> None:
        s = self._state()
        s.auto_stage = False
        s.active_workspace.overrides["default_auto_stage"] = False
        open_workspace_menu(s)
        self._focus_row_by_attr(s, "default_auto_stage")
        with mock.patch("ui.modals.workspace_menu.save_workspaces"):
            handle_workspace_menu_key(s, curses.KEY_BACKSPACE)
        self.assertNotIn("default_auto_stage", s.active_workspace.overrides)
        # Restored to base config default (True).
        self.assertTrue(s.auto_stage)

    def test_esc_closes_menu(self) -> None:
        s = self._state()
        open_workspace_menu(s)
        handle_workspace_menu_key(s, 27)
        self.assertIsNone(s.workspace_menu)


# ---------- Workspace row Tab opens picker --------------------------------


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestTitleRowTab(unittest.TestCase):
    def test_tab_on_title_row_opens_picker(self) -> None:
        # Picker now lives one nav level up — Tab on the Idlegit title
        # (selected = -2) opens the workspaces global switcher;
        # Tab on the workspace row (-1) opens the settings menu instead.
        ws_a = Workspace(name="A", folders=[Path("/a")])
        ws_b = Workspace(name="B", folders=[Path("/b")])
        s = _state(_make_repo("r"), workspaces=[ws_a, ws_b])
        s.selected = -2
        self.assertIsNone(s.app_menu)
        handle_main_key(s, 9)  # Tab
        self.assertIsNotNone(s.app_menu)

    def test_enter_on_title_row_opens_picker(self) -> None:
        # Enter matches the title row's underline affordance — same
        # action as Tab, kept in parity so muscle memory works either
        # way. Verified for all three Enter codepoints the rest of the
        # app recognises (10, 13, curses.KEY_ENTER).
        ws_a = Workspace(name="A", folders=[Path("/a")])
        ws_b = Workspace(name="B", folders=[Path("/b")])
        for key in (10, 13, curses.KEY_ENTER):
            with self.subTest(key=key):
                s = _state(_make_repo("r"), workspaces=[ws_a, ws_b])
                s.selected = -2
                self.assertIsNone(s.app_menu)
                handle_main_key(s, key)
                self.assertIsNotNone(s.app_menu)


# ---------- Workspaces picker --------------------------------------------


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestAppMenu(unittest.TestCase):
    def _state(self) -> State:
        ws_a = Workspace(name="A", folders=[Path("/a")])
        ws_b = Workspace(name="B", folders=[Path("/b")])
        ws_c = Workspace(name="C", folders=[Path("/c")])
        s = _state(_make_repo("r"), workspaces=[ws_a, ws_b, ws_c],
                   active_workspace_index=1)
        return s

    def _focused(self, s: State):
        """The focused row of the global app menu — used in place of
        bare-index assertions so tests don't break each time the
        APPLICATION section adds or removes a row above the
        WORKSPACES list."""
        menu = s.app_menu
        assert menu is not None
        return menu.rows[menu.selected]

    def test_open_lands_on_active_workspace(self) -> None:
        s = self._state()
        open_app_menu(s)
        row = self._focused(s)
        self.assertEqual(row.kind, "workspace")
        self.assertEqual(row.attr_name, "1")  # active_workspace_index

    def test_down_lands_on_next_workspace_then_create_row(self) -> None:
        s = self._state()
        open_app_menu(s)
        handle_app_menu_key(s, curses.KEY_DOWN)  # active (1) → ws 2
        row = self._focused(s)
        self.assertEqual(row.kind, "workspace")
        self.assertEqual(row.attr_name, "2")
        handle_app_menu_key(s, curses.KEY_DOWN)  # ws 2 → Create
        row = self._focused(s)
        self.assertEqual(row.kind, "create_workspace")

    def test_down_past_workspaces_descends_into_task_logging(self) -> None:
        # Below the WORKSPACES section sits the TASK LOGGING section
        # (toggle / open / clear actions); walking all the way Down
        # lands on the final clear_task_log action and stops there.
        s = self._state()
        open_app_menu(s)
        for _ in range(50):
            handle_app_menu_key(s, curses.KEY_DOWN)
        row = self._focused(s)
        self.assertEqual(row.kind, "app_action")
        self.assertEqual(row.attr_name, "clear_task_log")

    def test_enter_on_workspace_calls_switch(self) -> None:
        s = self._state()
        open_app_menu(s)
        handle_app_menu_key(s, curses.KEY_DOWN)  # land on idx 2
        with mock.patch("core.workers.switch_workspace") as m:
            handle_app_menu_key(s, 10)  # Enter
        # Picker closes, switch is invoked.
        self.assertIsNone(s.app_menu)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(m.call_args.args[1], 2)

    def test_enter_on_active_index_does_not_switch(self) -> None:
        s = self._state()
        open_app_menu(s)
        # Cursor starts on the active workspace (index 1).
        with mock.patch("core.workers.switch_workspace") as m:
            handle_app_menu_key(s, 10)
        self.assertIsNone(s.app_menu)
        self.assertEqual(m.call_count, 0)

    def test_enter_on_create_row_opens_creator(self) -> None:
        s = self._state()
        open_app_menu(s)
        # Walk down until the cursor lands on the Create row. The
        # exact step count depends on whatever rows the APPLICATION
        # section emits above the workspace list, so loop on the
        # focused row's kind rather than baking in a magic number.
        for _ in range(20):
            if self._focused(s).kind == "create_workspace":
                break
            handle_app_menu_key(s, curses.KEY_DOWN)
        self.assertEqual(self._focused(s).kind, "create_workspace")
        self.assertIsNone(s.workspace_creator)
        handle_app_menu_key(s, 10)
        self.assertIsNotNone(s.workspace_creator)
        # Picker stays open underneath; the main loop closes it once
        # the creator commits.
        self.assertIsNotNone(s.app_menu)

    def test_esc_closes_picker(self) -> None:
        s = self._state()
        open_app_menu(s)
        handle_app_menu_key(s, 27)
        self.assertIsNone(s.app_menu)

    def test_open_with_no_workspaces_falls_back_to_creator(self) -> None:
        s = _state(_make_repo("r"))  # no workspaces
        open_app_menu(s)
        # No picker, but the creator should be open instead.
        self.assertIsNone(s.app_menu)
        self.assertIsNotNone(s.workspace_creator)


# ---------- Workspace menu folder editing --------------------------------


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestWorkspaceMenuFolders(unittest.TestCase):
    def _state(self, folders) -> State:
        cfg = Config()
        ws = Workspace(name="W", folders=list(folders))
        s = _state(_make_repo("r"), workspaces=[ws],
                   active_workspace_index=0, base_config=cfg)
        return s

    def _focus_row_by_kind(self, s: State, kind: str,
                           index: int = 0) -> None:
        menu = s.workspace_menu
        seen = 0
        for i, row in enumerate(menu.rows):
            if row.kind == kind:
                if seen == index:
                    menu.selected = i
                    return
                seen += 1
        raise AssertionError(f"row of kind={kind!r} idx={index} not found")

    def test_open_includes_folder_rows_and_add_sentinel(self) -> None:
        s = self._state([Path("/tmp"), Path("/var")])
        open_workspace_menu(s)
        kinds = [r.kind for r in s.workspace_menu.rows]
        self.assertEqual(kinds.count("folder"), 2)
        self.assertEqual(kinds.count("add_folder"), 1)
        # Headers are present and non-focusable.
        self.assertIn("header", kinds)

    def test_navigation_skips_headers(self) -> None:
        s = self._state([Path("/tmp")])
        open_workspace_menu(s)
        menu = s.workspace_menu
        first = menu.selected
        self.assertNotEqual(menu.rows[first].kind, "header")
        # Stepping down should never land on a header row.
        for _ in range(20):
            handle_workspace_menu_key(s, curses.KEY_DOWN)
            self.assertNotEqual(
                menu.rows[menu.selected].kind, "header")

    def test_enter_on_folder_enters_edit_mode(self) -> None:
        s = self._state([Path("/tmp")])
        open_workspace_menu(s)
        self._focus_row_by_kind(s, "folder", 0)
        handle_workspace_menu_key(s, 10)
        self.assertTrue(s.workspace_menu.editing)
        self.assertEqual(s.workspace_menu.edit_buffer, "/tmp")

    def test_enter_in_edit_mode_commits_path_change(self) -> None:
        s = self._state([Path("/tmp")])
        open_workspace_menu(s)
        self._focus_row_by_kind(s, "folder", 0)
        handle_workspace_menu_key(s, 10)  # enter edit mode
        # Replace the buffer with /var.
        s.workspace_menu.edit_buffer = "/var"
        s.workspace_menu.edit_cursor = 4
        with mock.patch("ui.modals.workspace_menu.save_workspaces"):
            handle_workspace_menu_key(s, 10)  # commit
        self.assertFalse(s.workspace_menu.editing)
        self.assertEqual(str(s.active_workspace.folders[0]), "/var")

    def test_esc_in_edit_mode_cancels_without_persisting(self) -> None:
        s = self._state([Path("/tmp")])
        open_workspace_menu(s)
        self._focus_row_by_kind(s, "folder", 0)
        handle_workspace_menu_key(s, 10)  # enter edit mode
        s.workspace_menu.edit_buffer = "/different"
        handle_workspace_menu_key(s, 27)  # Esc
        self.assertFalse(s.workspace_menu.editing)
        self.assertEqual(str(s.active_workspace.folders[0]), "/tmp")
        # Esc here does NOT close the modal — only the inner edit mode.
        self.assertIsNotNone(s.workspace_menu)

    def test_backspace_on_folder_removes_when_more_than_one(self) -> None:
        s = self._state([Path("/tmp"), Path("/var")])
        open_workspace_menu(s)
        self._focus_row_by_kind(s, "folder", 0)
        with mock.patch("ui.modals.workspace_menu.save_workspaces"):
            handle_workspace_menu_key(s, curses.KEY_BACKSPACE)
        self.assertEqual(len(s.active_workspace.folders), 1)
        self.assertEqual(str(s.active_workspace.folders[0]), "/var")

    def test_backspace_on_last_folder_does_not_remove(self) -> None:
        s = self._state([Path("/tmp")])
        open_workspace_menu(s)
        self._focus_row_by_kind(s, "folder", 0)
        with mock.patch("ui.modals.workspace_menu.save_workspaces"):
            handle_workspace_menu_key(s, curses.KEY_BACKSPACE)
        # Refused — workspace must keep at least one folder.
        self.assertEqual(len(s.active_workspace.folders), 1)

    def test_add_folder_sentinel_enters_edit_mode_for_new_path(self) -> None:
        s = self._state([Path("/tmp")])
        open_workspace_menu(s)
        self._focus_row_by_kind(s, "add_folder", 0)
        handle_workspace_menu_key(s, 10)
        self.assertTrue(s.workspace_menu.editing)
        self.assertEqual(s.workspace_menu.edit_buffer, "")
        # Type a new path.
        for ch in "/var":
            handle_workspace_menu_key(s, ord(ch))
        with mock.patch("ui.modals.workspace_menu.save_workspaces"):
            handle_workspace_menu_key(s, 10)  # commit
        self.assertEqual(len(s.active_workspace.folders), 2)
        self.assertEqual(str(s.active_workspace.folders[1]), "/var")


@unittest.skipUnless(UI_AVAILABLE, "ui imports unavailable")
class TestWorkspaceMenuIgnorePatterns(unittest.TestCase):
    """FILE WATCH IGNORE section of the workspace settings modal.
    The flow mirrors the folder rows pattern (`ignore_pattern` rows
    + an `add_ignore_pattern` sentinel sharing the same `editing` /
    `edit_buffer` primitive), but with no live discover_repos
    validation and no "can't remove the last one" guard — the empty
    list is the default and reverts the watcher to its un-filtered
    behaviour."""

    def _state(self, patterns) -> State:
        cfg = Config()
        ws = Workspace(name="W", folders=[Path("/tmp")],
                       fs_watch_ignore=list(patterns))
        s = _state(_make_repo("r"), workspaces=[ws],
                   active_workspace_index=0, base_config=cfg)
        # Mirror what apply_workspace_overrides would do at startup —
        # the modal trusts state.fs_watch_ignore is in sync with the
        # active workspace's patterns.
        s.fs_watch_ignore = list(patterns)
        return s

    def _focus_row_by_kind(self, s: State, kind: str,
                           index: int = 0) -> None:
        menu = s.workspace_menu
        seen = 0
        for i, row in enumerate(menu.rows):
            if row.kind == kind:
                if seen == index:
                    menu.selected = i
                    return
                seen += 1
        raise AssertionError(f"row of kind={kind!r} idx={index} not found")

    def test_open_includes_ignore_rows_and_add_sentinel(self) -> None:
        s = self._state(["*.log", "build/**"])
        open_workspace_menu(s)
        kinds = [r.kind for r in s.workspace_menu.rows]
        self.assertEqual(kinds.count("ignore_pattern"), 2)
        self.assertEqual(kinds.count("add_ignore_pattern"), 1)

    def test_add_sentinel_commits_new_pattern(self) -> None:
        s = self._state([])
        open_workspace_menu(s)
        self._focus_row_by_kind(s, "add_ignore_pattern", 0)
        handle_workspace_menu_key(s, 10)  # Enter to start edit
        self.assertTrue(s.workspace_menu.editing)
        self.assertEqual(s.workspace_menu.edit_buffer, "")
        for ch in "*.log":
            handle_workspace_menu_key(s, ord(ch))
        with mock.patch("ui.modals.workspace_menu.save_workspaces"):
            handle_workspace_menu_key(s, 10)  # Enter to commit
        self.assertFalse(s.workspace_menu.editing)
        self.assertEqual(s.active_workspace.fs_watch_ignore, ["*.log"])
        # State mirror is also updated so the next fs event recompiles
        # against the new list.
        self.assertEqual(s.fs_watch_ignore, ["*.log"])

    def test_enter_on_pattern_loads_buffer_for_edit(self) -> None:
        s = self._state(["*.log"])
        open_workspace_menu(s)
        self._focus_row_by_kind(s, "ignore_pattern", 0)
        handle_workspace_menu_key(s, 10)
        self.assertTrue(s.workspace_menu.editing)
        self.assertEqual(s.workspace_menu.edit_buffer, "*.log")

    def test_edit_commits_replacement(self) -> None:
        s = self._state(["*.log"])
        open_workspace_menu(s)
        self._focus_row_by_kind(s, "ignore_pattern", 0)
        handle_workspace_menu_key(s, 10)  # enter edit
        s.workspace_menu.edit_buffer = "build/**"
        s.workspace_menu.edit_cursor = len("build/**")
        with mock.patch("ui.modals.workspace_menu.save_workspaces"):
            handle_workspace_menu_key(s, 10)  # commit
        self.assertEqual(
            s.active_workspace.fs_watch_ignore, ["build/**"])
        self.assertEqual(s.fs_watch_ignore, ["build/**"])

    def test_esc_in_edit_mode_cancels_without_persisting(self) -> None:
        s = self._state(["*.log"])
        open_workspace_menu(s)
        self._focus_row_by_kind(s, "ignore_pattern", 0)
        handle_workspace_menu_key(s, 10)
        s.workspace_menu.edit_buffer = "newpattern"
        handle_workspace_menu_key(s, 27)  # Esc
        self.assertFalse(s.workspace_menu.editing)
        self.assertEqual(s.active_workspace.fs_watch_ignore, ["*.log"])
        # Modal itself stays open — Esc only exits the inner edit.
        self.assertIsNotNone(s.workspace_menu)

    def test_backspace_removes_pattern_including_when_last(self) -> None:
        # Unlike folders, removing the last ignore pattern is fine —
        # an empty list means "watch everything", which is the
        # default behaviour anyway.
        s = self._state(["*.log"])
        open_workspace_menu(s)
        self._focus_row_by_kind(s, "ignore_pattern", 0)
        with mock.patch("ui.modals.workspace_menu.save_workspaces"):
            handle_workspace_menu_key(s, curses.KEY_BACKSPACE)
        self.assertEqual(s.active_workspace.fs_watch_ignore, [])
        self.assertEqual(s.fs_watch_ignore, [])

    def test_backspace_at_middle_index_renumbers_remaining(self) -> None:
        s = self._state(["*.log", "build/**", "dist/**"])
        open_workspace_menu(s)
        # Focus the middle pattern row.
        self._focus_row_by_kind(s, "ignore_pattern", 1)
        with mock.patch("ui.modals.workspace_menu.save_workspaces"):
            handle_workspace_menu_key(s, curses.KEY_BACKSPACE)
        # Remaining list keeps order, "build/**" is gone.
        self.assertEqual(
            s.active_workspace.fs_watch_ignore, ["*.log", "dist/**"])


# ---------- Sanity: WorkspaceCreator dataclass init -----------------------


class TestWorkspaceCreatorDataclass(unittest.TestCase):
    def test_default_creator_has_no_drafts_until_opened(self) -> None:
        c = WorkspaceCreator()
        self.assertEqual(c.drafts, [])
        self.assertEqual(c.selected, 0)
        self.assertIsNone(c.result)

    def test_workspace_draft_defaults(self) -> None:
        d = WorkspaceDraft()
        self.assertEqual(d.path_text, "")
        self.assertEqual(d.repo_count, -1)


if __name__ == "__main__":
    unittest.main()
