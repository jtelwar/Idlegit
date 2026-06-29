"""Workspace + multi-workspace tests — config loader/saver, State
helpers, key handlers for the title-row selector, and the workspace
creator/menu modals."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import config  # noqa: E402
from _helpers import (  # noqa: E402
    assert_repo_refresh_available,
    make_repo_model as _make_repo,
    make_state as _state,
)
from core.config import (  # noqa: E402
    Config,
    apply_workspace_overrides,
    base_value_for_override,
    coerce_override_value,
    get_load_warnings,
    load_config,
    load_workspaces,
    save_workspaces,
    set_conf_value,
    state_attr_value_from_override,
)
from core.git_ops import LinkSiblingsSnapshot, RepoRefreshSnapshot  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.workspaces import (  # noqa: E402
    Workspace, WorkspaceCreator, WorkspaceDraft, WorkspaceMenu,
)


def _empty_link_snapshot(repos):
    return LinkSiblingsSnapshot(
        repos=tuple(repos),
        children_by_parent={id(repo): () for repo in repos},
        siblings_by_repo={id(repo): () for repo in repos},
        synthetic_by_url={},
    )


# UI handlers depend on curses; skip the whole module on headless CI
# the same way test_keys.py does.
try:
    import curses  # noqa: F401
    from ui import handle_main_key  # noqa: F401
    from features.workspace_creator.actions import (  # noqa: F401
        commit_workspace_creator,
    )
    from features.workspace_creator.session import open_workspace_creator  # noqa: F401
    from ui.modals.workspace_creator import handle_workspace_creator_key  # noqa: F401
    from features.workspace_menu.session import open_workspace_menu  # noqa: F401
    from ui.modals.workspace_menu import handle_workspace_menu_key  # noqa: F401
    from ui.modals.app_menu import (  # noqa: F401
        handle_app_menu_key,
        open_app_menu,
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
        self.assertTrue(coerce_override_value("default_prevent_smart_sync_silent_merge", "true"))
        self.assertFalse(coerce_override_value("default_prevent_smart_sync_silent_merge", "false"))

    def test_int_round_trips(self) -> None:
        self.assertEqual(coerce_override_value("suggest_added", "5"), 5)
        self.assertEqual(coerce_override_value("suggest_added", "-1"), -1)
        self.assertIsNone(coerce_override_value("suggest_added", "five"))

    def test_trunc_mode_normalizes(self) -> None:
        self.assertEqual(coerce_override_value("name_truncation", "MIDDLE"), "middle")
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
        self.assertTrue(any("using defaults" in w for w in get_load_warnings()))

    def test_task_width_percentages_load_and_clamp(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.conf"
            tmp.write_text(
                "[idlegit]\ntasks_min_width_percent = 0.25\ntasks_max_width_percent = 2.0\n"
            )
            with mock.patch.object(config, "CONFIG_FILE", tmp):
                cfg = load_config()

        self.assertEqual(cfg.tasks_min_width_percent, 0.25)
        self.assertEqual(cfg.tasks_max_width_percent, 1.0)

    def test_suggest_limits_preserve_minus_one_and_clamp_lower_values(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.conf"
            tmp.write_text(
                "[idlegit]\n"
                "suggest_added = -1\n"
                "suggest_updated = 0\n"
                "suggest_deleted = -5\n"
            )
            with mock.patch.object(config, "CONFIG_FILE", tmp):
                cfg = load_config()

        self.assertEqual(cfg.suggest_added, -1)
        self.assertEqual(cfg.suggest_updated, 0)
        self.assertEqual(cfg.suggest_deleted, -1)


class TestApplyWorkspaceOverrides(unittest.TestCase):
    def test_resets_state_to_base_then_applies_overrides(self) -> None:
        cfg = Config(suggest_added=3, name_truncation="middle", default_auto_stage=True)
        ws = Workspace(
            name="W",
            folders=[Path("/tmp")],
            overrides={
                "default_auto_stage": False,
                "suggest_added": 7,
                "name_truncation": "end",
            },
        )
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
        ws = Workspace(name="W", folders=[Path("/tmp")], overrides={"lfs_warn_mb": 50})
        s = _state(_make_repo("a"))
        apply_workspace_overrides(s, cfg, ws)
        self.assertEqual(s.lfs_warn_bytes, 50 * 1024 * 1024)

    def test_prevent_smart_sync_merge_override(self) -> None:
        cfg = Config()
        ws = Workspace(
            name="W",
            folders=[Path("/tmp")],
            overrides={"default_prevent_smart_sync_silent_merge": True},
        )
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
        self._ensure_patch = mock.patch.object(config, "_ensure_config_ready", lambda: None)
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
        self._seed("[idlegit]\ntask_log_enabled = false   ; off by default\n")
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
            "name_truncation = middle\n"
        )
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
        self.assertEqual(state_attr_value_from_override("lfs_warn_mb", 50), 50 * 1024 * 1024)

    def test_state_attr_translation_passthrough(self) -> None:
        self.assertEqual(state_attr_value_from_override("suggest_added", 7), 7)
        self.assertEqual(state_attr_value_from_override("suggest_added", -1), -1)
        self.assertEqual(state_attr_value_from_override("suggest_added", -5), -1)

    def test_state_attr_translation_for_task_width_percent(self) -> None:
        self.assertEqual(state_attr_value_from_override("tasks_min_width_percent", 1.5), 1.0)

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
            with mock.patch.object(config, "WORKSPACES_FILE", Path(d) / "missing.workspaces"):
                ws, active_idx = load_workspaces()
        self.assertEqual(ws, [])
        self.assertEqual(active_idx, 0)

    def test_save_then_load_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.workspaces"
            with mock.patch.object(config, "WORKSPACES_FILE", tmp):
                src = [
                    Workspace(
                        name="Personal",
                        folders=[Path(d) / "p1"],
                        overrides={"default_auto_stage": False, "suggest_added": 5},
                    ),
                    Workspace(
                        name="Work",
                        folders=[Path(d) / "w1", Path(d) / "w2"],
                        overrides={"name_truncation": "end"},
                    ),
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
                f"[idlegit]\nactive_workspace = Gone\n\n[workspace.Stays]\nfolders = {d}\n"
            )
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
                    Workspace(name="Upskill.Health", folders=[Path(d) / "u"]),
                    Workspace(name="A.B.C", folders=[Path(d) / "a"]),
                ]
                for ws in src:
                    for f in ws.folders:
                        f.mkdir(parents=True, exist_ok=True)
                save_workspaces(src, active_index=0)
                loaded, _ = load_workspaces()
        self.assertEqual([w.name for w in loaded], ["Upskill.Health", "A.B.C"])

    def test_malformed_section_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "idlegit.workspaces"
            tmp.write_text(
                f"[workspace.NoFolders]\nname = oops\n\n[workspace.Good]\nfolders = {d}\n"
            )
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
        self.assertTrue(any("could not read" in w for w in get_load_warnings()))

    def test_bad_folder_line_is_skipped_without_losing_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            good = Path(d) / "good"
            good.mkdir()
            tmp = Path(d) / "idlegit.workspaces"
            bad = Path(d) / "bad"
            tmp.write_text(f"[workspace.W]\nfolders = {good}\n          {bad}\n")
            with mock.patch.object(Path, "resolve", autospec=True) as resolve:

                def fake_resolve(path):
                    if path == bad:
                        raise OSError("denied")
                    return path

                resolve.side_effect = fake_resolve
                with mock.patch.object(config, "WORKSPACES_FILE", tmp):
                    loaded, _ = load_workspaces()

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].folders, [good])
        self.assertTrue(any("workspace folder ignored" in w for w in get_load_warnings()))


# ---------- State helpers --------------------------------------------------


class TestStateWorkspaceProperties(unittest.TestCase):
    def test_active_workspace_is_none_when_empty(self) -> None:
        s = _state(_make_repo("a"))
        self.assertIsNone(s.active_workspace)
        self.assertEqual(s.active_folders, [])

    def test_active_workspace_returned_at_index(self) -> None:
        ws_a = Workspace(name="A", folders=[Path("/a")])
        ws_b = Workspace(name="B", folders=[Path("/b")])
        s = _state(_make_repo("r"), workspaces=[ws_a, ws_b], active_workspace_index=1)
        self.assertIs(s.active_workspace, ws_b)
        self.assertEqual([str(f) for f in s.active_folders], ["/b"])

    def test_active_workspace_clamps_out_of_range_index(self) -> None:
        ws_a = Workspace(name="A", folders=[Path("/a")])
        s = _state(_make_repo("r"), workspaces=[ws_a], active_workspace_index=5)
        # Out-of-range index clamps back into the list rather than
        # raising — defensive against stale / corrupt state.
        self.assertIs(s.active_workspace, ws_a)


class TestSwitchWorkspaceCache(unittest.TestCase):
    """switch_workspace prefers each workspace's `cached_repos` over a
    fresh discover so rapid ←/→ keystrokes don't churn discovery and
    don't leave the user staring at half-loaded repos while a refresh
    races on a stale folder list.

    EVERY test in this class MUST patch the workspace settings save launcher —
    switch_workspace persists the new active index through a worker-owned job,
    and an unmocked test would enqueue writes for the throw-away fixture data
    below. (We learned this the hard way.)"""

    def setUp(self) -> None:
        # Defensive belt-and-braces: also point WORKSPACES_FILE at a
        # tempdir so even if a future test forgets to patch
        # save_workspaces directly, persistence lands somewhere
        # disposable rather than the real config file.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._patches = [
            mock.patch.object(
                config, "WORKSPACES_FILE", Path(self._tmp.name) / "idlegit.workspaces"
            ),
            mock.patch("core.workers.kick_off_inline_refresh"),
            mock.patch("core.workers.kick_off_workspace_settings_save"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _state(self, *workspaces) -> State:
        s = State(
            repos=[], workspace_name="", workspaces=list(workspaces), active_workspace_index=0
        )
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
        with (
            mock.patch("core.workers.discover_repos") as disc,
            mock.patch("core.workers.link_siblings") as link,
            mock.patch("core.workers.kick_off_inline_refresh") as kick,
            mock.patch("core.fs_watcher.reconcile_repo_watchers") as reconcile,
        ):
            switch_workspace(s, 1)
            disc.assert_not_called()
            link.assert_not_called()
            kick.assert_called_once()
            reconcile.assert_not_called()
        self.assertIs(s.repos, b_repos)
        self.assertEqual(s.workspace_name, "B")

    def test_cache_miss_schedules_workspace_load_without_sync_discovery(self) -> None:
        from core.workers import switch_workspace

        a_repos = [_make_repo("a1")]
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=a_repos)
        b = Workspace(name="B", folders=[Path("/b")])
        s = self._state(a, b)
        submitted = {}

        def fake_start(registry, job, target, *, thread_factory=None):
            submitted["spec"] = job.spec
            submitted["job"] = job
            submitted["target"] = target
            return object()

        with (
            mock.patch("core.workers.start_job_thread", side_effect=fake_start),
            mock.patch("core.workers.discover_repos") as disc,
            mock.patch("core.workers.reconcile_repos_bounded") as reconcile,
            mock.patch("core.workers.kick_off_inline_refresh") as kick,
        ):
            switch_workspace(s, 1)

        disc.assert_not_called()
        reconcile.assert_not_called()
        kick.assert_not_called()
        self.assertEqual(submitted["spec"].kind, "workspace-switch")
        self.assertFalse(submitted["spec"].local_mutation)
        self.assertEqual(s.workspace_name, "B")
        self.assertIs(s.repos, b.cached_repos)
        self.assertEqual(s.repos, [])
        task = s.tasks.snapshot()[-1]
        self.assertEqual(task.label, "switch workspace: B")
        self.assertEqual(task.status, "running")

    def test_workspace_load_job_discovers_relinks_publishes_and_refreshes(self) -> None:
        from core.jobs import JobStatus
        from core.reconcile import ReconcileResult
        from core.workers import switch_workspace

        a_repos = [_make_repo("a1")]
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=a_repos)
        b = Workspace(name="B", folders=[Path("/b")])
        s = self._state(a, b)
        fresh = [_make_repo("b1"), _make_repo("b2")]
        submitted = {}

        def fake_start(registry, job, target, *, thread_factory=None):
            submitted["job"] = job
            submitted["target"] = target
            return object()

        with mock.patch("core.workers.start_job_thread", side_effect=fake_start):
            switch_workspace(s, 1)

        with (
            mock.patch("core.config.save_workspaces"),
            mock.patch("core.workers.discover_repos", return_value=fresh) as disc,
            mock.patch(
                "core.workers.reconcile_repos_bounded",
                return_value=ReconcileResult()) as reconcile,
            mock.patch("core.workers.kick_off_inline_refresh") as kick,
        ):
            submitted["target"](submitted["job"])
            if not submitted["job"].terminal:
                s.job_registry.finish(submitted["job"], JobStatus.OK)

        disc.assert_called_once_with(Path("/b"))
        reconcile.assert_called_once_with(
            [],
            [],
            link_repos=fresh,
            refresh_fn=mock.ANY,
            link_fn=mock.ANY,
        )
        kick.assert_called_once()
        self.assertIs(s.repos, b.cached_repos)
        self.assertEqual([r.rel for r in b.cached_repos], ["b1", "b2"])
        self.assertEqual(submitted["job"].status, JobStatus.OK)
        task = s.tasks.snapshot()[-1]
        self.assertEqual(task.label, "switch workspace: B")
        self.assertEqual(task.status, "ok")
        self.assertEqual(task.message, "2 repos")

    def test_cache_miss_link_failure_warns_without_aborting_switch(self) -> None:
        from core.jobs import JobStatus
        from core.reconcile import ReconcileResult
        from core.workers import switch_workspace

        a_repos = [_make_repo("a1")]
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=a_repos)
        b = Workspace(name="B", folders=[Path("/b")])
        s = self._state(a, b)
        result = ReconcileResult(link_error="link boom")
        submitted = {}

        def fake_start(registry, job, target, *, thread_factory=None):
            submitted["job"] = job
            submitted["target"] = target
            return object()

        with mock.patch("core.workers.start_job_thread", side_effect=fake_start):
            switch_workspace(s, 1)
        with (
            mock.patch("core.config.save_workspaces"),
            mock.patch("core.workers.discover_repos", return_value=[_make_repo("b1")]),
            mock.patch("core.workers.reconcile_repos_bounded", return_value=result),
            mock.patch("core.workers.kick_off_inline_refresh"),
        ):
            submitted["target"](submitted["job"])

        self.assertEqual(s.workspace_name, "B")
        warn = next(t for t in s.tasks.snapshot() if t.label == "switch workspace: B")
        self.assertEqual(warn.status, "warn")
        self.assertEqual(warn.message, "link boom")
        self.assertEqual(submitted["job"].status, JobStatus.WARN)

    def test_workspace_load_thread_start_failure_marks_task_failed(self) -> None:
        from core.jobs import JobStatus
        from core.workers import switch_workspace

        a_repos = [_make_repo("a1")]
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=a_repos)
        b = Workspace(name="B", folders=[Path("/b")])
        s = self._state(a, b)

        def fake_start(registry, job, target, *, thread_factory=None):
            registry.finish(job, JobStatus.FAIL, "thread start failed")
            return None

        with mock.patch("core.workers.start_job_thread", side_effect=fake_start):
            switch_workspace(s, 1)

        task = s.tasks.snapshot()[-1]
        self.assertEqual(task.label, "switch workspace: B")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")
        self.assertEqual(s.repos, [])

    def test_workspace_load_stale_completion_does_not_publish_after_switch(self) -> None:
        from core.jobs import JobStatus
        from core.reconcile import ReconcileResult
        from core.workers import switch_workspace

        a_repos = [_make_repo("a1")]
        c_repos = [_make_repo("c1")]
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=a_repos)
        b = Workspace(name="B", folders=[Path("/b")])
        c = Workspace(name="C", folders=[Path("/c")], cached_repos=c_repos)
        s = self._state(a, b, c)
        fresh = [_make_repo("b1")]
        submitted = {}

        def fake_start(registry, job, target, *, thread_factory=None):
            submitted.setdefault("jobs", []).append(job)
            submitted.setdefault("targets", []).append(target)
            return object()

        with (
            mock.patch("core.workers.start_job_thread", side_effect=fake_start),
            mock.patch("core.workers.kick_off_inline_refresh") as switch_kick,
        ):
            switch_workspace(s, 1)
            switch_workspace(s, 2)
        switch_kick.assert_called_once()

        with (
            mock.patch("core.config.save_workspaces"),
            mock.patch("core.workers.discover_repos", return_value=fresh),
            mock.patch(
                "core.workers.reconcile_repos_bounded",
                return_value=ReconcileResult()) as reconcile,
            mock.patch("core.workers.kick_off_inline_refresh") as kick,
        ):
            submitted["targets"][0](submitted["jobs"][0])
            if not submitted["jobs"][0].terminal:
                s.job_registry.finish(submitted["jobs"][0], JobStatus.OK)

        reconcile.assert_called_once()
        kick.assert_not_called()
        self.assertIs(s.repos, c.cached_repos)
        self.assertEqual([repo.rel for repo in b.cached_repos], ["b1"])
        self.assertEqual([repo.rel for repo in s.repos], ["c1"])
        task = next(t for t in s.tasks.snapshot() if t.label == "switch workspace: B")
        self.assertEqual(task.status, "ok")
        self.assertEqual(submitted["jobs"][0].status, JobStatus.OK)

    def test_store_row_messages_persist_across_switches(self) -> None:
        from core.workers import switch_workspace

        a_repos = [_make_repo("a1")]
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=a_repos)
        b = Workspace(name="B", folders=[Path("/b")], cached_repos=[_make_repo("b1")])
        s = self._state(a, b)
        s.replace_repos(a.cached_repos, workspace=a)
        s.store.set_row_message(a_repos[0], "pending edit")
        with mock.patch("core.workers.kick_off_inline_refresh"):
            switch_workspace(s, 1)  # A → B
            switch_workspace(s, 0)  # B → A
        # Coming back to A surfaces the store-owned unsaved message exactly
        # as it was — no flicker, no fresh empty draft.
        self.assertEqual(s.store.row_message(s.repos[0]), "pending edit")
        self.assertEqual(s.repos[0].message, "")

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


class TestInlineRefreshWorkspacePin(unittest.TestCase):
    """Inline refresh must update the workspace it started on, not whichever
    workspace is active when the worker finishes."""

    def setUp(self) -> None:
        import core.workers as workers_mod

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._save_patch = mock.patch("core.config.save_workspaces")
        self._save_patch.start()
        self.addCleanup(self._save_patch.stop)
        with workers_mod._inline_refresh_lock:
            workers_mod._inline_refresh_in_flight = False
            workers_mod._inline_refresh_pending = False
            workers_mod._inline_refresh_targets_in_flight.clear()
            workers_mod._inline_refresh_targets_pending.clear()
            workers_mod._inline_refresh_targets_started_at.clear()

    def test_stale_refresh_does_not_repaint_after_switch(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_inline_refresh, switch_workspace

        a_repos = [_make_repo("a1")]
        b_repos = [_make_repo("b1")]
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=a_repos)
        b = Workspace(name="B", folders=[Path("/b")], cached_repos=b_repos)
        s = State(
            repos=list(a_repos), workspace_name="A", workspaces=[a, b], active_workspace_index=0
        )
        resume_refresh = threading.Event()

        def discover_side_effect(folder: Path):
            resume_refresh.wait(timeout=2)
            return [_make_repo("a-stale")]

        with (
            mock.patch.object(workers_mod, "discover_repos", side_effect=discover_side_effect),
            mock.patch.object(workers_mod, "refresh_repo"),
            mock.patch.object(workers_mod, "link_siblings") as link,
            mock.patch.object(workers_mod, "kick_off_inline_refresh"),
            mock.patch("core.fs_watcher.reconcile_repo_watchers"),
        ):
            kick_off_inline_refresh(s)
            switch_workspace(s, 1)
            resume_refresh.set()
            deadline = time.monotonic() + 2.0
            while workers_mod._inline_refresh_in_flight and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(s.active_workspace_index, 1)
        self.assertIs(s.repos, b_repos)
        self.assertEqual(s.repos[0].rel, "b1")
        self.assertEqual([r.rel for r in a.cached_repos], ["a-stale"])
        link.assert_not_called()

    def test_rapid_workspace_cycle_refreshes_final_workspace(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_inline_refresh, switch_workspace

        a_repos = [_make_repo("a1")]
        b_repos = [_make_repo("b1")]
        c_repos = [_make_repo("c1")]
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=a_repos)
        b = Workspace(name="B", folders=[Path("/b")], cached_repos=b_repos)
        c = Workspace(name="C", folders=[Path("/c")], cached_repos=c_repos)
        s = State(
            repos=list(a_repos), workspace_name="A", workspaces=[a, b, c], active_workspace_index=0
        )
        release_a = threading.Event()
        seen_c = threading.Event()
        calls = []

        def discover_side_effect(folder: Path):
            calls.append(folder)
            if folder == Path("/a"):
                release_a.wait(timeout=2)
                return [_make_repo("a-refresh")]
            if folder == Path("/c"):
                seen_c.set()
                return [_make_repo("c-refresh")]
            return [_make_repo(f"{folder.name}-refresh")]

        with (
            mock.patch.object(workers_mod, "discover_repos", side_effect=discover_side_effect),
            mock.patch.object(workers_mod, "refresh_repo"),
            mock.patch.object(workers_mod, "link_siblings"),
            mock.patch("core.fs_watcher.reconcile_repo_watchers"),
        ):
            kick_off_inline_refresh(s)
            switch_workspace(s, 1)
            switch_workspace(s, 2)
            release_a.set()
            self.assertTrue(seen_c.wait(timeout=2.0))
            deadline = time.monotonic() + 2.0
            while workers_mod._inline_refresh_in_flight and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(s.active_workspace_index, 2)
        self.assertEqual(s.workspace_name, "C")
        self.assertEqual([r.rel for r in c.cached_repos], ["c-refresh"])
        self.assertIs(s.repos, c.cached_repos)
        self.assertIn(Path("/c"), calls)

    def test_workspace_switch_refreshes_active_before_stale_refresh_finishes(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_inline_refresh, switch_workspace

        a_repos = [_make_repo("a1")]
        b_repos = [_make_repo("b1")]
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=a_repos)
        b = Workspace(name="B", folders=[Path("/b")], cached_repos=b_repos)
        s = State(
            repos=list(a_repos), workspace_name="A", workspaces=[a, b], active_workspace_index=0
        )
        release_a = threading.Event()
        seen_b = threading.Event()

        def discover_side_effect(folder: Path):
            if folder == Path("/a"):
                release_a.wait(timeout=2)
                return [_make_repo("a-refresh")]
            if folder == Path("/b"):
                seen_b.set()
                return [_make_repo("b-refresh")]
            return []

        with (
            mock.patch.object(workers_mod, "discover_repos", side_effect=discover_side_effect),
            mock.patch.object(workers_mod, "refresh_repo"),
            mock.patch.object(workers_mod, "link_siblings"),
            mock.patch("core.fs_watcher.reconcile_repo_watchers"),
        ):
            kick_off_inline_refresh(s)
            switch_workspace(s, 1)
            self.assertTrue(seen_b.wait(timeout=2.0))
            self.assertEqual(s.active_workspace_index, 1)
            release_a.set()
            deadline = time.monotonic() + 2.0
            while workers_mod._inline_refresh_in_flight and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual([r.rel for r in b.cached_repos], ["b-refresh"])
        self.assertIs(s.repos, b.cached_repos)

    def test_manual_refresh_fetch_failure_adds_warn_task(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_inline_refresh

        repo = _make_repo("a")
        ws = Workspace(name="A", folders=[repo.path.parent], cached_repos=[repo])
        s = State(
            repos=[repo],
            workspace_name="A",
            workspaces=[ws],
            active_workspace_index=0,
            fetch_on_manual_refresh=True,
        )

        with (
            mock.patch.object(workers_mod, "discover_repos", return_value=[repo]),
            mock.patch.object(workers_mod, "git", return_value=(1, "", "network down\n")),
            mock.patch.object(workers_mod, "refresh_repo"),
            mock.patch.object(workers_mod, "link_siblings"),
            mock.patch("core.fs_watcher.reconcile_repo_watchers"),
        ):
            kick_off_inline_refresh(s)
            deadline = time.monotonic() + 2.0
            while workers_mod._inline_refresh_in_flight and time.monotonic() < deadline:
                time.sleep(0.01)

        warns = [t for t in s.tasks.items if t.status == "warn"]
        self.assertEqual(len(warns), 1)
        self.assertIn("fetch failed", warns[0].label)
        self.assertIn("network down", warns[0].message)
        self.assertIn("local state only", warns[0].message)

    def test_manual_refresh_adds_visible_summary_task(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_inline_refresh

        repo = _make_repo("a")
        ws = Workspace(name="A", folders=[repo.path.parent], cached_repos=[repo])
        s = State(
            repos=[repo],
            workspace_name="A",
            workspaces=[ws],
            active_workspace_index=0,
        )

        with (
            mock.patch.object(workers_mod, "discover_repos", return_value=[repo]),
            mock.patch.object(workers_mod, "refresh_repo"),
            mock.patch.object(workers_mod, "link_siblings"),
            mock.patch("core.fs_watcher.reconcile_repo_watchers"),
        ):
            kick_off_inline_refresh(s, manual=True)
            deadline = time.monotonic() + 2.0
            while workers_mod._inline_refresh_in_flight and time.monotonic() < deadline:
                time.sleep(0.01)

        summaries = [t for t in s.tasks.items if t.label == "refresh workspace"]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].status, "ok")
        self.assertEqual(summaries[0].message, "1 refreshed")

    def test_manual_refresh_queues_when_refresh_gate_busy(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_inline_refresh

        repo = _make_repo("a")
        ws = Workspace(name="A", folders=[repo.path.parent], cached_repos=[repo])
        s = State(
            repos=[repo],
            workspace_name="A",
            workspaces=[ws],
            active_workspace_index=0,
        )

        with workers_mod._inline_refresh_lock:
            workers_mod._inline_refresh_targets_in_flight.add(0)
            workers_mod._inline_refresh_targets_started_at[0] = time.monotonic()
            workers_mod._sync_inline_refresh_flags_locked()
        try:
            kick_off_inline_refresh(s, manual=True)
        finally:
            with workers_mod._inline_refresh_lock:
                workers_mod._inline_refresh_targets_in_flight.clear()
                workers_mod._inline_refresh_targets_pending.clear()
                workers_mod._inline_refresh_targets_started_at.clear()
                workers_mod._sync_inline_refresh_flags_locked()

        summaries = [t for t in s.tasks.items if t.label == "refresh workspace"]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].status, "ok")
        self.assertEqual(summaries[0].message, "refresh queued")

    def test_manual_refresh_clears_stale_refresh_gate(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_inline_refresh

        repo = _make_repo("a")
        ws = Workspace(name="A", folders=[repo.path.parent], cached_repos=[repo])
        s = State(
            repos=[repo],
            workspace_name="A",
            workspaces=[ws],
            active_workspace_index=0,
        )

        with workers_mod._inline_refresh_lock:
            workers_mod._inline_refresh_targets_in_flight.add(0)
            workers_mod._inline_refresh_targets_started_at[0] = (
                time.monotonic() - workers_mod._INLINE_REFRESH_STALE_SECONDS - 1.0
            )
            workers_mod._sync_inline_refresh_flags_locked()

        with (
            mock.patch.object(workers_mod, "discover_repos", return_value=[repo]),
            mock.patch.object(workers_mod, "refresh_repo"),
            mock.patch.object(workers_mod, "link_siblings"),
            mock.patch("core.fs_watcher.reconcile_repo_watchers"),
        ):
            kick_off_inline_refresh(s, manual=True)
            deadline = time.monotonic() + 2.0
            while workers_mod._inline_refresh_in_flight and time.monotonic() < deadline:
                time.sleep(0.01)

        summaries = [t for t in s.tasks.items if t.label == "refresh workspace"]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].status, "ok")
        self.assertEqual(summaries[0].message, "1 refreshed")

    def test_repo_spinner_clears_before_relink_finishes(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_inline_refresh

        repo = _make_repo("a")
        ws = Workspace(name="A", folders=[repo.path.parent], cached_repos=[repo])
        s = State(
            repos=[repo],
            workspace_name="A",
            workspaces=[ws],
            active_workspace_index=0,
        )
        refresh_done = threading.Event()
        relink_entered = threading.Event()
        release_relink = threading.Event()

        def read_repo_refresh_snapshot(target, **_kwargs) -> RepoRefreshSnapshot:
            self.assertIs(target, repo)
            self.assertTrue(s.store.repo_busy(repo))
            refresh_done.set()
            return RepoRefreshSnapshot()

        def read_link_siblings_snapshot(repos, _subtrees, **_kwargs):
            relink_entered.set()
            self.assertTrue(release_relink.wait(timeout=2.0))
            return _empty_link_snapshot(repos)

        with (
            mock.patch.object(workers_mod, "discover_repos", return_value=[repo]),
            mock.patch.object(
                workers_mod,
                "read_repo_refresh_snapshot",
                side_effect=read_repo_refresh_snapshot,
            ),
            mock.patch.object(
                workers_mod,
                "read_link_siblings_snapshot",
                side_effect=read_link_siblings_snapshot,
            ),
            mock.patch("core.fs_watcher.reconcile_repo_watchers"),
        ):
            kick_off_inline_refresh(s)
            self.assertTrue(refresh_done.wait(timeout=2.0))
            self.assertTrue(relink_entered.wait(timeout=2.0))
            self.assertFalse(s.store.repo_busy(repo))
            release_relink.set()
            deadline = time.monotonic() + 2.0
            while workers_mod._inline_refresh_in_flight and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(s.store.repo_busy(repo))

    def test_repo_refresh_exception_does_not_abort_workspace_apply(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_inline_refresh

        repo = _make_repo("a")
        ws = Workspace(name="A", folders=[repo.path.parent], cached_repos=[repo])
        s = State(
            repos=[repo],
            workspace_name="A",
            workspaces=[ws],
            active_workspace_index=0,
        )

        with (
            mock.patch.object(workers_mod, "discover_repos", return_value=[repo]),
            mock.patch.object(
                workers_mod,
                "read_repo_refresh_snapshot",
                side_effect=RuntimeError("status boom"),
            ),
            mock.patch.object(
                workers_mod,
                "read_link_siblings_snapshot",
                side_effect=lambda repos, _subtrees, **_kwargs:
                _empty_link_snapshot(repos),
            ),
            mock.patch("core.fs_watcher.reconcile_repo_watchers"),
        ):
            kick_off_inline_refresh(s)
            deadline = time.monotonic() + 2.0
            while workers_mod._inline_refresh_in_flight and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(s.store.repo_busy(repo))
        self.assertEqual(repo.error, "status boom")
        self.assertIs(s.repos, ws.cached_repos)
        self.assertEqual(ws.cached_repos, [repo])
        warns = [t for t in s.tasks.items if t.status == "warn"]
        self.assertEqual(len(warns), 1)
        self.assertIn("refresh failed", warns[0].label)
        self.assertEqual(warns[0].message, "status boom")

    def test_link_siblings_exception_does_not_abort_workspace_apply(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_inline_refresh

        repo = _make_repo("a")
        ws = Workspace(name="A", folders=[repo.path.parent], cached_repos=[repo])
        s = State(
            repos=[repo],
            workspace_name="A",
            workspaces=[ws],
            active_workspace_index=0,
        )

        with (
            mock.patch.object(workers_mod, "discover_repos", return_value=[repo]),
            mock.patch.object(
                workers_mod,
                "read_repo_refresh_snapshot",
                return_value=RepoRefreshSnapshot(),
            ),
            mock.patch.object(
                workers_mod,
                "read_link_siblings_snapshot",
                side_effect=RuntimeError("link boom"),
            ),
            mock.patch("core.fs_watcher.reconcile_repo_watchers"),
        ):
            kick_off_inline_refresh(s)
            deadline = time.monotonic() + 2.0
            while workers_mod._inline_refresh_in_flight and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(s.store.repo_busy(repo))
        self.assertIs(s.repos, ws.cached_repos)
        self.assertEqual(ws.cached_repos, [repo])
        warns = [t for t in s.tasks.items if t.status == "warn"]
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0].label, "refresh links failed")
        self.assertEqual(warns[0].message, "link boom")

    def test_thread_start_failure_releases_inline_refresh_claims(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_inline_refresh

        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("a")
        ws = Workspace(name="A", folders=[repo.path.parent], cached_repos=[repo])
        s = State(
            repos=[repo],
            workspace_name="A",
            workspaces=[ws],
            active_workspace_index=0,
        )

        with mock.patch.object(workers_mod.threading, "Thread", FailingThread):
            kick_off_inline_refresh(s, manual=True)

        self.assertFalse(workers_mod._inline_refresh_in_flight)
        self.assertFalse(s.store.repo_busy(repo))
        assert_repo_refresh_available(self, s, repo)
        summary = next(t for t in s.tasks.snapshot() if t.label == "refresh workspace")
        self.assertEqual(summary.status, "fail")
        self.assertEqual(summary.message, "thread start failed")

    def test_changed_workspace_folders_rerun_refreshes_current_state(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_inline_refresh

        old_repo = _make_repo("old/repo")
        new_repo = _make_repo("new/repo")
        old_folder = old_repo.path.parent
        new_folder = new_repo.path.parent
        ws = Workspace(name="A", folders=[old_folder], cached_repos=[old_repo])
        s = State(
            repos=[old_repo],
            workspace_name="A",
            workspaces=[ws],
            active_workspace_index=0,
        )
        first_refresh_entered = threading.Event()
        release_first_refresh = threading.Event()
        old_refresh_seen = [False]

        def discover_side_effect(folder: Path):
            if folder == old_folder:
                return [old_repo]
            if folder == new_folder:
                return [new_repo]
            return []

        def read_repo_refresh_snapshot(repo, **_kwargs) -> RepoRefreshSnapshot:
            if repo.path == old_repo.path and not old_refresh_seen[0]:
                old_refresh_seen[0] = True
                first_refresh_entered.set()
                self.assertTrue(release_first_refresh.wait(timeout=2.0))
            return RepoRefreshSnapshot()

        with (
            mock.patch.object(workers_mod, "discover_repos", side_effect=discover_side_effect),
            mock.patch.object(
                workers_mod,
                "read_repo_refresh_snapshot",
                side_effect=read_repo_refresh_snapshot,
            ),
            mock.patch.object(
                workers_mod,
                "read_link_siblings_snapshot",
                side_effect=lambda repos, _subtrees, **_kwargs:
                _empty_link_snapshot(repos),
            ),
            mock.patch("core.fs_watcher.reconcile_repo_watchers"),
        ):
            kick_off_inline_refresh(s)
            self.assertTrue(first_refresh_entered.wait(timeout=2.0))
            ws.folders = [new_folder]
            release_first_refresh.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not workers_mod._inline_refresh_in_flight and s.repos == [new_repo]:
                    break
                time.sleep(0.01)

        self.assertFalse(workers_mod._inline_refresh_in_flight)
        self.assertEqual(s.repos, [new_repo])
        self.assertEqual(ws.cached_repos, [new_repo])
        self.assertFalse(s.store.repo_busy(old_repo))
        self.assertFalse(s.store.repo_busy(new_repo))


class TestPullAllWorkspacePin(unittest.TestCase):
    def setUp(self) -> None:
        import core.workers as workers_mod

        self._save_patch = mock.patch("core.config.save_workspaces")
        self._save_patch.start()
        self.addCleanup(self._save_patch.stop)
        with workers_mod._inline_refresh_lock:
            workers_mod._inline_refresh_in_flight = False
            workers_mod._inline_refresh_pending = False
            workers_mod._inline_refresh_targets_in_flight.clear()
            workers_mod._inline_refresh_targets_pending.clear()
            workers_mod._inline_refresh_targets_started_at.clear()

    def test_pull_all_relinks_starting_workspace_after_switch(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_pull_all, switch_workspace

        a_repo = _make_repo("a")
        b_repo = _make_repo("b")
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=[a_repo])
        b = Workspace(name="B", folders=[Path("/b")], cached_repos=[b_repo])
        s = State(repos=[a_repo], workspace_name="A", workspaces=[a, b], active_workspace_index=0)
        pull_started = threading.Event()
        release_pull = threading.Event()

        def pull_side_effect(*_args, **_kwargs) -> bool:
            pull_started.set()
            self.assertTrue(release_pull.wait(timeout=2.0))
            return True

        git_results = [
            (0, "origin/main\n", ""),
            (0, "before\n", ""),
            (0, "after\n", ""),
        ]

        def git_side_effect(*_args, **_kwargs):
            return git_results.pop(0)

        with (
            mock.patch.object(workers_mod, "git", side_effect=git_side_effect),
            mock.patch.object(
                workers_mod, "_pull_prefer_ff_then_merge", side_effect=pull_side_effect
            ),
            mock.patch.object(workers_mod, "refresh_repo"),
            mock.patch.object(workers_mod, "read_link_siblings_snapshot",
                              side_effect=lambda repos, _subtrees, **_kwargs:
                              _empty_link_snapshot(repos)) as link,
            mock.patch.object(workers_mod, "kick_off_inline_refresh"),
            mock.patch("core.fs_watcher.reconcile_repo_watchers"),
        ):
            kick_off_pull_all(s)
            self.assertTrue(pull_started.wait(timeout=2.0))
            switch_workspace(s, 1)
            self.assertIs(s.repos, b.cached_repos)
            release_pull.set()
            deadline = time.monotonic() + 2.0
            while s.store.repo_busy(a_repo) and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(s.store.repo_busy(a_repo))
        link.assert_called_once_with(
            [a_repo], [],
            busy_child_predicate=mock.ANY,
            child_message_lookup=mock.ANY)
        predicate = link.call_args.kwargs["busy_child_predicate"]
        self.assertTrue(callable(predicate))
        self.assertIs(s.repos, b.cached_repos)

    def test_thread_start_failure_releases_pull_all_locks(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_pull_all

        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("a")
        repo.upstream = "origin/main"
        s = State(repos=[repo], workspace_name="A")

        with mock.patch.object(workers_mod.threading, "Thread", FailingThread):
            kick_off_pull_all(s)

        self.assertFalse(s.store.repo_busy(repo))
        assert_repo_refresh_available(self, s, repo)
        task = next(t for t in s.tasks.snapshot() if t.label == "pull all")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")


class TestSmartSyncWorkspacePin(unittest.TestCase):
    def setUp(self) -> None:
        self._save_patch = mock.patch("core.config.save_workspaces")
        self._save_patch.start()
        self.addCleanup(self._save_patch.stop)

    def test_smart_sync_job_is_active_before_sentinel_row_state(self) -> None:
        import core.smart_sync.lifecycle as lifecycle_mod
        import core.workers as workers_mod
        from core.state.repos import ChildRef
        from core.workers import kick_off_sync_siblings

        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        nested = parent.path / "vendor" / "canonical"
        child = ChildRef(repo=canonical, nested_path=nested, branch="main")
        parent.children = [child]
        canonical.siblings = [(parent, nested)]
        s = State(repos=[parent, canonical], workspace_name="A")
        saw_active_job = []
        original_enter = lifecycle_mod.WorkerClaim.__enter__

        def enter(claim):
            if claim.task is not None and claim.task.label.startswith("  ↳ smart-sync"):
                saw_active_job.append(s.job_registry.has_active_local_mutation())
            return original_enter(claim)

        with (
            mock.patch.object(lifecycle_mod.WorkerClaim, "__enter__", enter),
            mock.patch.object(workers_mod, "_align_canonical", return_value=(0, 1)),
            mock.patch.object(workers_mod, "refresh_repo"),
            mock.patch.object(workers_mod, "reconcile_repos_bounded"),
            mock.patch.object(workers_mod, "link_siblings"),
        ):
            kick_off_sync_siblings(s)
            deadline = time.monotonic() + 2.0
            while s.job_registry.has_active_local_mutation() and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(saw_active_job, [True])

    def test_smart_sync_job_starts_with_precise_targets(self) -> None:
        import core.workers as workers_mod
        from core.state.repos import ChildRef
        from core.workers import kick_off_sync_siblings

        class CapturedThread:
            def __init__(self, target, name):
                self.target = target
                self.name = name
                self.daemon = False

            def start(self):
                return None

        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        nested = parent.path / "vendor" / "canonical"
        child = ChildRef(repo=canonical, nested_path=nested, branch="main")
        parent.children = [child]
        canonical.siblings = [(parent, nested)]
        s = State(repos=[parent, canonical], workspace_name="A")

        with mock.patch.object(workers_mod.threading, "Thread", CapturedThread):
            kick_off_sync_siblings(s)

        jobs = s.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].spec.kind, "smart-sync")
        self.assertEqual(
            jobs[0].spec.repo_keys,
            (str(canonical.path), str(parent.path)),
        )
        self.assertEqual(jobs[0].spec.child_keys, (str(nested),))
        self.assertFalse(
            s.job_registry.has_active_local_mutation_for(
                repo_keys=("/tmp/unrelated",)))

    def test_smart_sync_task_is_visible_before_preflight_finishes(self) -> None:
        import core.workers as workers_mod
        from core.state.repos import ChildRef
        from core.workers import kick_off_sync_siblings

        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        nested = parent.path / "vendor" / "canonical"
        child = ChildRef(repo=canonical, nested_path=nested, branch="main")
        parent.children = [child]
        canonical.siblings = [(parent, nested)]
        s = State(repos=[parent, canonical], workspace_name="A")
        s.auto_push_submodule_parent = False
        preflight_entered = threading.Event()
        release_preflight = threading.Event()

        def aligned_side_effect(_state, _canonical):
            preflight_entered.set()
            self.assertTrue(release_preflight.wait(timeout=2.0))
            return True

        with mock.patch.object(
                workers_mod,
                "_canonical_already_aligned",
                side_effect=aligned_side_effect):
            kick_off_sync_siblings(s)
            tasks = s.tasks.snapshot()
            self.assertEqual(tasks[-1].label, "smart-sync (1)")
            self.assertEqual(tasks[-1].status, "running")
            self.assertEqual(tasks[-1].message, "preparing")
            self.assertTrue(preflight_entered.wait(timeout=2.0))
            self.assertTrue(s.job_registry.has_active_local_mutation())
            release_preflight.set()
            deadline = time.monotonic() + 2.0
            while s.job_registry.has_active_local_mutation() and time.monotonic() < deadline:
                time.sleep(0.01)

        header = next(t for t in s.tasks.snapshot() if t.label.startswith("smart-sync"))
        self.assertEqual(header.status, "ok")
        self.assertEqual(header.message, "all aligned")

    def test_smart_sync_relinks_starting_workspace_after_switch(self) -> None:
        import core.smart_sync.runner as runner_mod
        import core.workers as workers_mod
        from core.state.repos import ChildRef
        from core.workers import kick_off_sync_siblings, switch_workspace

        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        nested = parent.path / "vendor" / "canonical"
        child = ChildRef(repo=canonical, nested_path=nested, branch="main")
        parent.children = [child]
        canonical.siblings = [(parent, nested)]
        b_repo = _make_repo("b")
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=[parent, canonical])
        b = Workspace(name="B", folders=[Path("/b")], cached_repos=[b_repo])
        s = State(
            repos=[parent, canonical],
            workspace_name="A",
            workspaces=[a, b],
            active_workspace_index=0,
        )
        align_started = threading.Event()
        release_align = threading.Event()

        def align_side_effect(_state, _canonical):
            align_started.set()
            self.assertTrue(release_align.wait(timeout=2.0))
            return (1, 0)

        from core.reconcile import ReconcileResult

        with (
            mock.patch.object(workers_mod, "_align_canonical", side_effect=align_side_effect),
            mock.patch.object(
                runner_mod, "reconcile_repos_bounded",
                return_value=ReconcileResult()) as reconcile_batch,
            mock.patch.object(
                workers_mod,
                "read_link_siblings_snapshot",
                side_effect=lambda repos, _subtrees, **_kwargs:
                _empty_link_snapshot(repos),
            ),
            mock.patch.object(workers_mod, "kick_off_inline_refresh"),
            mock.patch("core.fs_watcher.reconcile_repo_watchers"),
        ):
            kick_off_sync_siblings(s)
            self.assertTrue(align_started.wait(timeout=2.0))
            self.assertTrue(s.job_registry.has_active_local_mutation())
            switch_workspace(s, 1)
            self.assertIs(s.repos, b.cached_repos)
            release_align.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                jobs = s.job_registry.snapshot()
                cleanup = next(
                    (job for job in jobs if job.spec.kind == "smart-sync-cleanup"),
                    None,
                )
                if cleanup is not None and cleanup.terminal:
                    break
                time.sleep(0.01)

        self.assertFalse(s.store.repo_busy(canonical))
        self.assertFalse(s.store.child_busy(child))
        deadline = time.monotonic() + 2.0
        while not any(
                job.spec.kind == "smart-sync-cleanup"
                for job in s.job_registry.snapshot()
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
        jobs = s.job_registry.snapshot()
        smart_sync = next(job for job in jobs if job.spec.kind == "smart-sync")
        cleanup = next(job for job in jobs if job.spec.kind == "smart-sync-cleanup")
        self.assertEqual(smart_sync.status, "ok")
        self.assertFalse(cleanup.spec.local_mutation)
        self.assertFalse(s.job_registry.has_active_local_mutation())
        reconcile_batch.assert_called_once()
        args, kwargs = reconcile_batch.call_args
        self.assertEqual(args, ([parent, canonical], []))
        self.assertTrue(callable(kwargs["refresh_fn"]))
        self.assertTrue(callable(kwargs["link_fn"]))
        self.assertEqual(kwargs["max_workers"], 1)
        self.assertTrue(callable(kwargs["should_stop"]))
        self.assertIs(s.repos, b.cached_repos)

    def test_smart_sync_thread_start_failure_clears_row_state(self) -> None:
        import core.workers as workers_mod
        from core.state.repos import ChildRef
        from core.workers import kick_off_sync_siblings

        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        nested = parent.path / "vendor" / "canonical"
        child = ChildRef(repo=canonical, nested_path=nested, branch="main")
        parent.children = [child]
        canonical.siblings = [(parent, nested)]
        s = State(repos=[parent, canonical], workspace_name="A")

        with mock.patch.object(workers_mod.threading, "Thread", FailingThread):
            kick_off_sync_siblings(s)

        self.assertFalse(s.store.repo_busy(canonical))
        self.assertFalse(s.store.child_busy(child))
        self.assertFalse(s.leases.has_lease_for(repos=[canonical]))
        self.assertTrue(
            any(t.label.startswith("smart-sync") and t.status == "fail" for t in s.tasks.snapshot())
        )
        jobs = s.job_registry.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].spec.kind, "smart-sync")
        self.assertEqual(jobs[0].status, "fail")
        self.assertEqual(jobs[0].message, "thread start failed")
        self.assertFalse(s.job_registry.has_active_local_mutation())

    def test_smart_sync_cleanup_allows_navigation_until_completion(self) -> None:
        import core.workers as workers_mod
        from core.state.repos import ChildRef
        from core.workers import kick_off_sync_siblings

        if not UI_AVAILABLE:
            self.skipTest("ui module unavailable")

        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        nested = parent.path / "vendor" / "canonical"
        child = ChildRef(repo=canonical, nested_path=nested, branch="main")
        parent.children = [child]
        canonical.siblings = [(parent, nested)]
        s = State(repos=[parent, canonical], workspace_name="A")
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()

        def read_link_siblings_snapshot(repos, _subtrees, **_kwargs):
            cleanup_entered.set()
            release_cleanup.wait(timeout=2.0)
            return _empty_link_snapshot(repos)

        with (
            mock.patch.object(workers_mod, "_align_canonical", return_value=(1, 0)),
            mock.patch.object(
                workers_mod,
                "read_repo_refresh_snapshot",
                return_value=RepoRefreshSnapshot(),
            ),
            mock.patch.object(
                workers_mod,
                "read_link_siblings_snapshot",
                side_effect=read_link_siblings_snapshot,
            ),
        ):
            kick_off_sync_siblings(s)
            self.assertTrue(cleanup_entered.wait(timeout=2.0))
            sentinel = next(t for t in s.tasks.snapshot() if t.label == "  ↳ smart-sync canonical")
            self.assertEqual(sentinel.status, "ok")
            self.assertFalse(s.leases.has_lease_for(repos=[canonical]))
            self.assertFalse(s.job_registry.has_active_local_mutation())
            self.assertFalse(s.store.repo_busy(canonical))
            cleanup_task = next(
                t for t in s.tasks.snapshot()
                if t.label == "  ↳ smart-sync refresh cleanup")
            self.assertEqual(cleanup_task.status, "running")
            jobs = s.job_registry.snapshot()
            self.assertEqual(len(jobs), 2)
            self.assertEqual(jobs[0].spec.kind, "smart-sync")
            self.assertEqual(jobs[0].status, "ok")
            self.assertEqual(jobs[1].spec.kind, "smart-sync-cleanup")
            self.assertFalse(jobs[1].spec.local_mutation)
            handle_main_key(s, curses.KEY_DOWN)
            self.assertEqual(s.selected, 1)
            release_cleanup.set()
            deadline = time.monotonic() + 2.0
            while (not jobs[1].terminal) and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(s.leases.has_lease_for(repos=[canonical]))
        self.assertFalse(s.job_registry.has_active_local_mutation())
        self.assertFalse(s.store.repo_busy(canonical))
        self.assertFalse(s.store.child_busy(child))
        self.assertEqual(sentinel.status, "ok")
        jobs = s.job_registry.snapshot()
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].spec.kind, "smart-sync")
        self.assertEqual(jobs[0].status, "ok")
        self.assertEqual(jobs[1].spec.kind, "smart-sync-cleanup")
        self.assertEqual(jobs[1].status, "ok")
        self.assertEqual(s.selected, 1)
        self.assertEqual(cleanup_task.status, "ok")

    def test_smart_sync_noop_does_not_run_final_cleanup(self) -> None:
        import core.workers as workers_mod
        from core.state.repos import ChildRef
        from core.workers import kick_off_sync_siblings

        parent = _make_repo("parent")
        canonical = _make_repo("canonical")
        nested = parent.path / "vendor" / "canonical"
        child = ChildRef(repo=canonical, nested_path=nested, branch="main")
        parent.children = [child]
        canonical.siblings = [(parent, nested)]
        s = State(repos=[parent, canonical], workspace_name="A")
        s.auto_push_submodule_parent = False

        with (
            mock.patch.object(workers_mod, "_canonical_already_aligned", return_value=True),
            mock.patch.object(workers_mod, "_align_canonical") as align,
            mock.patch.object(workers_mod, "refresh_repo"),
            mock.patch.object(workers_mod, "link_siblings") as link,
        ):
            kick_off_sync_siblings(s)
            deadline = time.monotonic() + 2.0
            while s.job_registry.has_active_local_mutation() and time.monotonic() < deadline:
                time.sleep(0.01)

        align.assert_not_called()
        self.assertEqual(len(s.job_registry.snapshot()), 1)
        self.assertEqual(s.job_registry.snapshot()[0].status, "ok")
        self.assertFalse(s.job_registry.has_active_local_mutation())
        self.assertFalse(s.store.repo_busy(canonical))
        self.assertFalse(s.store.child_busy(child))
        link.assert_not_called()
        self.assertFalse(
            any(t.label == "  ↳ smart-sync refresh cleanup" for t in s.tasks.snapshot())
        )
        header = next(t for t in s.tasks.snapshot() if t.label.startswith("smart-sync ("))
        self.assertEqual(header.status, "ok")
        self.assertEqual(header.message, "all aligned")


class TestCommitWorkerClaims(unittest.TestCase):
    def test_thread_start_failure_releases_repo_claim(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_workers

        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("a")
        s = State(repos=[repo], workspace_name="A")
        s.store.set_row_message(repo, "commit this")

        with mock.patch.object(workers_mod.threading, "Thread", FailingThread):
            kick_off_workers(s, [])

        self.assertFalse(s.store.repo_busy(repo))
        self.assertFalse(s.leases.has_lease_for(repos=[repo]))
        assert_repo_refresh_available(self, s, repo)
        task = next(t for t in s.tasks.snapshot() if t.label == "commit workers")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")


class TestActionWorkspacePin(unittest.TestCase):
    def setUp(self) -> None:
        self._save_patch = mock.patch("core.config.save_workspaces")
        self._save_patch.start()
        self.addCleanup(self._save_patch.stop)

    def test_action_refresh_relinks_starting_workspace_after_switch(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_action, switch_workspace

        a_repo = _make_repo("a")
        b_repo = _make_repo("b")
        a = Workspace(name="A", folders=[Path("/a")], cached_repos=[a_repo])
        b = Workspace(name="B", folders=[Path("/b")], cached_repos=[b_repo])
        s = State(repos=[a_repo], workspace_name="A", workspaces=[a, b], active_workspace_index=0)
        action_started = threading.Event()
        release_action = threading.Event()

        def git_side_effect(*_args, **_kwargs):
            action_started.set()
            self.assertTrue(release_action.wait(timeout=2.0))
            return (0, "", "")

        with (
            mock.patch.object(workers_mod, "git", side_effect=git_side_effect),
            mock.patch.object(workers_mod, "refresh_repo"),
            mock.patch.object(
                workers_mod,
                "read_link_siblings_snapshot",
                side_effect=lambda repos, _subtrees, **_kwargs:
                _empty_link_snapshot(repos),
            ) as link,
            mock.patch.object(workers_mod, "kick_off_inline_refresh"),
            mock.patch("core.fs_watcher.reconcile_repo_watchers"),
        ):
            kick_off_action(
                s,
                "fetch",
                target_label="a",
                target_path=a_repo.path,
                target_repo=a_repo,
                target_parent=None,
            )
            self.assertTrue(action_started.wait(timeout=2.0))
            switch_workspace(s, 1)
            self.assertIs(s.repos, b.cached_repos)
            release_action.set()
            deadline = time.monotonic() + 2.0
            while s.store.repo_busy(a_repo) and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(s.store.repo_busy(a_repo))
        link.assert_called_once_with(
            [a_repo], [],
            busy_child_predicate=mock.ANY,
            child_message_lookup=mock.ANY)
        predicate = link.call_args.kwargs["busy_child_predicate"]
        self.assertTrue(callable(predicate))
        self.assertIs(s.repos, b.cached_repos)

    def test_thread_start_failure_releases_action_claim(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_action

        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("a")
        s = State(repos=[repo], workspace_name="A")

        with mock.patch.object(workers_mod.threading, "Thread", FailingThread):
            kick_off_action(
                s,
                "fetch",
                target_label="a",
                target_path=repo.path,
                target_repo=repo,
                target_parent=None,
            )

        self.assertFalse(s.store.repo_busy(repo))
        assert_repo_refresh_available(self, s, repo)
        task = next(t for t in s.tasks.snapshot() if t.label == "a: failed")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")

    def test_thread_start_failure_releases_tag_claim(self) -> None:
        import core.workers as workers_mod
        from core.workers import kick_off_add_tag

        class FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        repo = _make_repo("a")
        s = State(repos=[repo], workspace_name="A")

        with mock.patch.object(workers_mod.threading, "Thread", FailingThread):
            kick_off_add_tag(
                s,
                target_label="a",
                target_path=repo.path,
                target_repo=repo,
                target_parent=None,
                name="v1",
                sha="abc123",
            )

        self.assertFalse(s.store.repo_busy(repo))
        assert_repo_refresh_available(self, s, repo)
        task = next(t for t in s.tasks.snapshot() if t.label == "a: tag v1")
        self.assertEqual(task.status, "fail")
        self.assertEqual(task.message, "thread start failed")


# ---------- Key handler — workspace row + cycling -------------------------


@unittest.skipUnless(UI_AVAILABLE, "ui module unavailable")
class TestWorkspaceRowKeys(unittest.TestCase):
    def _state_two_ws(self) -> State:
        ws_a = Workspace(name="A", folders=[Path("/a")])
        ws_b = Workspace(name="B", folders=[Path("/b")])
        s = _state(_make_repo("r"), workspaces=[ws_a, ws_b], active_workspace_index=0)
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

    def test_enter_opens_workspace_switcher(self) -> None:
        s = self._state_two_ws()
        handle_main_key(s, 10)
        self.assertIsNotNone(s.workspace_switcher)
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
        self.assertEqual(s.workspace_creator.selected, len(s.workspace_creator.drafts))
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
        s = _state(_make_repo("r"), workspaces=[ws], active_workspace_index=0, base_config=cfg)
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
        with mock.patch("features.workspace_menu.actions.kick_off_workspace_settings_save"):
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
        with mock.patch("features.workspace_menu.actions.kick_off_workspace_settings_save"):
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
        s = _state(_make_repo("r"), workspaces=[ws_a, ws_b, ws_c], active_workspace_index=1)
        return s

    def _focused(self, s: State):
        """The focused row of the global app menu — used in place of
        bare-index assertions so tests don't break each time the
        APPLICATION section adds or removes a row above the
        WORKSPACES list."""
        menu = s.app_menu
        assert menu is not None
        return menu.rows[menu.selected]

    def _focus_action(self, s: State, action_id: str) -> None:
        menu = s.app_menu
        assert menu is not None
        for i, row in enumerate(menu.rows):
            if row.kind == "app_action" and row.attr_name == action_id:
                menu.selected = i
                return
        self.fail(f"missing app action {action_id}")

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

    def test_app_menu_exposes_completed_task_removal(self) -> None:
        s = self._state()
        s.auto_remove_completed_after = 6
        open_app_menu(s)
        rows = s.app_menu.rows
        labels = [row.label for row in rows]
        self.assertIn("TASKS", labels)
        row = next(row for row in rows if row.attr_name == "cycle_auto_remove_completed")
        self.assertEqual(row.kind, "app_action")
        self.assertEqual(row.label, "Remove successful tasks: 6s")

    def test_app_menu_exposes_periodic_refresh(self) -> None:
        s = self._state()
        s.periodic_refresh_seconds = 60
        open_app_menu(s)
        row = next(row for row in s.app_menu.rows if row.attr_name == "adjust_periodic_refresh")
        self.assertEqual(row.kind, "app_action")
        self.assertEqual(row.label, "Periodic refresh: 60s")

    def test_app_menu_marks_periodic_refresh_off(self) -> None:
        s = self._state()
        s.periodic_refresh_seconds = 0
        open_app_menu(s)
        row = next(row for row in s.app_menu.rows if row.attr_name == "adjust_periodic_refresh")
        self.assertEqual(row.label, "Periodic refresh: 0s (OFF)")

    def test_periodic_refresh_enter_toggles_off_and_default(self) -> None:
        s = self._state()
        s.periodic_refresh_seconds = 60
        with mock.patch("features.app_menu.session.kick_off_app_menu_status_refresh"):
            open_app_menu(s)
        self._focus_action(s, "adjust_periodic_refresh")
        with mock.patch(
                "features.app_menu.actions.kick_off_periodic_refresh_save") as m:
            handle_app_menu_key(s, 10)
        self.assertEqual(s.periodic_refresh_seconds, 0)
        m.assert_called_once_with(s, 0.0, "0s (OFF)")

        with mock.patch(
                "features.app_menu.actions.kick_off_periodic_refresh_save") as m:
            handle_app_menu_key(s, 10)
        self.assertEqual(s.periodic_refresh_seconds, 60)
        m.assert_called_once_with(s, 60.0, "60s")

    def test_periodic_refresh_arrows_adjust_by_one_second(self) -> None:
        s = self._state()
        s.periodic_refresh_seconds = 1
        with mock.patch("features.app_menu.session.kick_off_app_menu_status_refresh"):
            open_app_menu(s)
        self._focus_action(s, "adjust_periodic_refresh")
        with mock.patch(
                "features.app_menu.actions.kick_off_periodic_refresh_save") as m:
            handle_app_menu_key(s, curses.KEY_RIGHT)
        self.assertEqual(s.periodic_refresh_seconds, 2)
        m.assert_called_once_with(s, 2, "2s")

        with mock.patch(
                "features.app_menu.actions.kick_off_periodic_refresh_save") as m:
            handle_app_menu_key(s, curses.KEY_LEFT)
        self.assertEqual(s.periodic_refresh_seconds, 1)
        m.assert_called_once_with(s, 1, "1s")

        with mock.patch(
                "features.app_menu.actions.kick_off_periodic_refresh_save") as m:
            handle_app_menu_key(s, curses.KEY_LEFT)
        self.assertEqual(s.periodic_refresh_seconds, 0)
        m.assert_called_once_with(s, 0.0, "0s (OFF)")

    def test_completed_task_removal_cycle_saves_config(self) -> None:
        s = self._state()
        s.auto_remove_completed_after = 6
        with mock.patch("features.app_menu.session.kick_off_app_menu_status_refresh"):
            open_app_menu(s)
        self._focus_action(s, "cycle_auto_remove_completed")
        with mock.patch(
                "features.app_menu.actions.kick_off_auto_remove_completed_save") as m:
            handle_app_menu_key(s, 10)
        self.assertEqual(s.auto_remove_completed_after, 10)
        m.assert_called_once_with(s, 10.0, "10s")

    def test_filesystem_watcher_toggle_schedules_nonblocking_job(self) -> None:
        s = self._state()
        s.auto_refresh_on_fs_change = False
        with mock.patch("features.app_menu.session.kick_off_app_menu_status_refresh"):
            open_app_menu(s)
        self._focus_action(s, "toggle_auto_refresh")

        with (
            mock.patch("features.app_menu.actions.kick_off_auto_refresh_toggle") as kickoff,
            mock.patch("core.fs_watcher.reconcile_repo_watchers") as reconcile,
        ):
            handle_app_menu_key(s, 10)

        self.assertTrue(s.auto_refresh_on_fs_change)
        reconcile.assert_not_called()
        kickoff.assert_called_once_with(s, True)

    def test_open_app_menu_does_not_probe_status_synchronously(self) -> None:
        s = self._state()

        with (
            mock.patch("features.app_menu.session.kick_off_app_menu_status_refresh") as kickoff,
            mock.patch("core.ssh.ssh_tools_status") as status,
            mock.patch("core.ssh.agent_status_label") as agent_label,
            mock.patch("core.ssh.keys_loaded_label") as keys_label,
            mock.patch("core.task_log.task_log_size_bytes") as size_bytes,
        ):
            open_app_menu(s)

        status.assert_not_called()
        agent_label.assert_not_called()
        keys_label.assert_not_called()
        size_bytes.assert_not_called()
        kickoff.assert_called_once_with(s, s.app_menu)
        self.assertIn(
            "Agent: checking",
            [row.label for row in s.app_menu.rows],
        )

    def test_app_menu_tick_schedules_status_when_snapshot_is_checking(self) -> None:
        import features.app_menu.session as app_menu_session

        s = self._state()
        with mock.patch("features.app_menu.session.kick_off_app_menu_status_refresh"):
            open_app_menu(s)
        s.app_menu.ssh_status_checking = False
        s.app_menu.task_log_checking = False

        def mark_checking(_state, menu):
            menu.ssh_status_checking = True

        with mock.patch.object(
                app_menu_session,
                "kick_off_app_menu_status_refresh",
                side_effect=mark_checking,
        ) as kickoff:
            self.assertTrue(app_menu_session.tick_app_menu_update_check(s))

        kickoff.assert_called_once_with(s, s.app_menu)

    def test_enter_on_workspace_calls_switch(self) -> None:
        s = self._state()
        open_app_menu(s)
        handle_app_menu_key(s, curses.KEY_DOWN)  # land on idx 2
        with mock.patch("features.app_menu.actions.switch_workspace") as m:
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
        s = _state(_make_repo("r"), workspaces=[ws], active_workspace_index=0, base_config=cfg)
        return s

    def _focus_row_by_kind(self, s: State, kind: str, index: int = 0) -> None:
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
            self.assertNotEqual(menu.rows[menu.selected].kind, "header")

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
        with mock.patch("features.workspace_menu.actions.kick_off_workspace_settings_save"):
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
        with mock.patch("features.workspace_menu.actions.kick_off_workspace_settings_save"):
            handle_workspace_menu_key(s, curses.KEY_BACKSPACE)
        self.assertEqual(len(s.active_workspace.folders), 1)
        self.assertEqual(str(s.active_workspace.folders[0]), "/var")

    def test_backspace_on_last_folder_does_not_remove(self) -> None:
        s = self._state([Path("/tmp")])
        open_workspace_menu(s)
        self._focus_row_by_kind(s, "folder", 0)
        with mock.patch("features.workspace_menu.actions.kick_off_workspace_settings_save"):
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
        with mock.patch("features.workspace_menu.actions.kick_off_workspace_settings_save"):
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
        ws = Workspace(name="W", folders=[Path("/tmp")], fs_watch_ignore=list(patterns))
        s = _state(_make_repo("r"), workspaces=[ws], active_workspace_index=0, base_config=cfg)
        # Mirror what apply_workspace_overrides would do at startup —
        # the modal trusts state.fs_watch_ignore is in sync with the
        # active workspace's patterns.
        s.fs_watch_ignore = list(patterns)
        return s

    def _focus_row_by_kind(self, s: State, kind: str, index: int = 0) -> None:
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
        with mock.patch("features.workspace_menu.actions.kick_off_workspace_settings_save"):
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
        with mock.patch("features.workspace_menu.actions.kick_off_workspace_settings_save"):
            handle_workspace_menu_key(s, 10)  # commit
        self.assertEqual(s.active_workspace.fs_watch_ignore, ["build/**"])
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
        with mock.patch("features.workspace_menu.actions.kick_off_workspace_settings_save"):
            handle_workspace_menu_key(s, curses.KEY_BACKSPACE)
        self.assertEqual(s.active_workspace.fs_watch_ignore, [])
        self.assertEqual(s.fs_watch_ignore, [])

    def test_backspace_at_middle_index_renumbers_remaining(self) -> None:
        s = self._state(["*.log", "build/**", "dist/**"])
        open_workspace_menu(s)
        # Focus the middle pattern row.
        self._focus_row_by_kind(s, "ignore_pattern", 1)
        with mock.patch("features.workspace_menu.actions.kick_off_workspace_settings_save"):
            handle_workspace_menu_key(s, curses.KEY_BACKSPACE)
        # Remaining list keeps order, "build/**" is gone.
        self.assertEqual(s.active_workspace.fs_watch_ignore, ["*.log", "dist/**"])


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
