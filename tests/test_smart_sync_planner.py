"""Pure smart-sync planner tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import workers  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.smart_sync import SmartSyncCheckout  # noqa: E402
from core.state.repos import Repo  # noqa: E402
from core.smart_sync.planner import plan_canonical_alignment  # noqa: E402
from core.smart_sync.types import (  # noqa: E402
    CanonicalPlan,
    CanonicalPlanStatus,
    CheckoutSnapshot,
    SmartSyncSettings,
    SyncStep,
    SyncStepKind,
)


def checkout(
    checkout_id: str,
    *,
    branch: str = "main",
    head: str = "aaa",
    dirty: bool = False,
    ahead: int = 0,
    commit_time: int = 0,
    sig_mtime: float = 0.0,
) -> CheckoutSnapshot:
    return CheckoutSnapshot(
        checkout_id=checkout_id,
        label=checkout_id,
        path=Path("/") / checkout_id,
        branch=branch,
        head=head,
        dirty=dirty,
        ahead=ahead,
        commit_time=commit_time,
        sig_mtime=sig_mtime,
    )


def settings(
    *,
    auto_stage: bool = True,
    auto_ff: bool = True,
    align_heads: bool = True,
    prompt_for_branch: bool = False,
) -> SmartSyncSettings:
    return SmartSyncSettings(
        auto_stage=auto_stage,
        auto_ff=auto_ff,
        align_heads=align_heads,
        prompt_for_branch=prompt_for_branch,
    )


class TestSmartSyncPlanner(unittest.TestCase):
    def test_aligned_clean_group_is_noop(self) -> None:
        plan = plan_canonical_alignment(
            [checkout("canonical"), checkout("nested")],
            settings=settings(),
        )

        self.assertEqual(plan.status, CanonicalPlanStatus.NOOP)
        self.assertEqual(plan.steps, ())

    def test_multiple_ahead_checkouts_warns_for_manual_resolution(self) -> None:
        plan = plan_canonical_alignment(
            [
                checkout("canonical", ahead=1),
                checkout("nested", ahead=2),
            ],
            settings=settings(),
        )

        self.assertEqual(plan.status, CanonicalPlanStatus.WARN)
        self.assertIn("2 checkouts ahead", plan.warning)
        self.assertEqual(
            [step.kind for step in plan.steps],
            [SyncStepKind.WARN_MANUAL, SyncStepKind.WARN_MANUAL],
        )

    def test_single_ahead_winner_pushes_and_ff_aligns_loser(self) -> None:
        plan = plan_canonical_alignment(
            [
                checkout("canonical", head="bbb", ahead=1),
                checkout("nested", head="aaa"),
            ],
            settings=settings(),
        )

        self.assertEqual(plan.status, CanonicalPlanStatus.READY)
        self.assertEqual(plan.winner_id, "canonical")
        self.assertEqual(
            [(step.kind, step.target_id) for step in plan.steps],
            [
                (SyncStepKind.PUSH_WINNER, "canonical"),
                (SyncStepKind.ALIGN_FF, "nested"),
            ],
        )

    def test_dirty_newest_checkout_commits_then_pushes(self) -> None:
        plan = plan_canonical_alignment(
            [
                checkout("canonical", dirty=True, sig_mtime=1.0),
                checkout("nested", dirty=True, sig_mtime=2.0),
            ],
            settings=settings(),
        )

        self.assertEqual(plan.winner_id, "nested")
        self.assertEqual(
            [step.kind for step in plan.steps[:2]],
            [SyncStepKind.COMMIT_DIRTY, SyncStepKind.PUSH_WINNER],
        )

    def test_differing_clean_heads_choose_newest_commit_time(self) -> None:
        plan = plan_canonical_alignment(
            [
                checkout("canonical", head="aaa", commit_time=100),
                checkout("nested", head="bbb", commit_time=200),
            ],
            settings=settings(),
        )

        self.assertEqual(plan.status, CanonicalPlanStatus.READY)
        self.assertEqual(plan.winner_id, "nested")
        self.assertEqual(
            [(step.kind, step.target_id) for step in plan.steps],
            [(SyncStepKind.ALIGN_FF, "canonical")],
        )

    def test_dirty_winner_warns_when_auto_stage_is_off(self) -> None:
        plan = plan_canonical_alignment(
            [checkout("canonical", dirty=True)],
            settings=settings(auto_stage=False),
        )

        self.assertEqual(plan.status, CanonicalPlanStatus.WARN)
        self.assertEqual(plan.winner_id, "canonical")
        self.assertIn("auto-stage", plan.warning)

    def test_detached_winner_prompts_or_resolves_origin_head(self) -> None:
        with_prompt = plan_canonical_alignment(
            [
                checkout("canonical", branch="(detached)", head="bbb", ahead=1),
                checkout("nested", head="aaa"),
            ],
            settings=settings(prompt_for_branch=True),
        )
        without_prompt = plan_canonical_alignment(
            [
                checkout("canonical", branch="(detached)", head="bbb", ahead=1),
                checkout("nested", head="aaa"),
            ],
            settings=settings(prompt_for_branch=False),
        )

        self.assertEqual(with_prompt.steps[0].kind, SyncStepKind.PROMPT_BRANCH)
        self.assertEqual(without_prompt.steps[0].kind, SyncStepKind.RESOLVE_ORIGIN_HEAD)

    def test_branch_mismatch_and_auto_ff_off_are_manual_steps(self) -> None:
        branch_mismatch = plan_canonical_alignment(
            [
                checkout("canonical", head="bbb", ahead=1),
                checkout("nested", branch="feature", head="aaa"),
            ],
            settings=settings(),
        )
        auto_ff_off = plan_canonical_alignment(
            [
                checkout("canonical", head="bbb", ahead=1),
                checkout("nested", head="aaa"),
            ],
            settings=settings(auto_ff=False),
        )

        self.assertEqual(branch_mismatch.steps[-1].kind, SyncStepKind.WARN_MANUAL)
        self.assertIn("feature", branch_mismatch.steps[-1].message)
        self.assertEqual(auto_ff_off.steps[-1].kind, SyncStepKind.WARN_MANUAL)
        self.assertIn("auto-ff off", auto_ff_off.steps[-1].message)


class TestSmartSyncPlannerProductionPath(unittest.TestCase):
    def test_canonical_already_aligned_uses_planner_noop_status(self) -> None:
        repo = Repo(rel="repo", path=Path("/workspace/repo"))
        state = State(repos=[repo], workspace_name="ws")

        clean_checkout = SmartSyncCheckout(
            canonical=repo,
            parent=None,
            path=repo.path,
            branch="main",
            label="repo",
            head="aaa",
        )
        dirty_checkout = SmartSyncCheckout(
            canonical=repo,
            parent=None,
            path=repo.path,
            branch="main",
            label="repo",
            head="aaa",
            dirty=True,
        )

        with mock.patch.object(
            workers,
            "_probe_canonical_checkouts",
            return_value=[clean_checkout],
        ):
            self.assertTrue(workers._canonical_already_aligned(state, repo))

        with mock.patch.object(
            workers,
            "_probe_canonical_checkouts",
            return_value=[dirty_checkout],
        ):
            self.assertFalse(workers._canonical_already_aligned(state, repo))

    def test_align_canonical_uses_planner_warning_without_git_execution(self) -> None:
        repo = Repo(rel="repo", path=Path("/workspace/repo"))
        checkout = SmartSyncCheckout(
            canonical=repo,
            parent=None,
            path=repo.path,
            branch="main",
            label="repo",
        )
        state = State(repos=[repo], workspace_name="ws")

        with (
            mock.patch.object(
                workers,
                "_probe_canonical_checkouts",
                return_value=[checkout],
            ),
            mock.patch.object(
                workers,
                "plan_canonical_alignment",
                return_value=CanonicalPlan(
                    status=CanonicalPlanStatus.WARN,
                    warning="planner warning",
                ),
            ) as planner,
        ):
            pushed, warned = workers._align_canonical(state, repo)

        self.assertEqual((pushed, warned), (0, 1))
        planner.assert_called_once()
        self.assertEqual(len(state.tasks.items), 1)
        self.assertEqual(state.tasks.items[0].status, "warn")
        self.assertEqual(state.tasks.items[0].message, "planner warning")

    def test_same_branch_multi_ahead_chain_upgrades_manual_warning(self) -> None:
        repo = Repo(rel="repo", path=Path("/workspace/repo"))
        older = SmartSyncCheckout(
            canonical=repo,
            parent=None,
            path=repo.path / "older",
            branch="main",
            label="older",
            head="old",
            ahead=1,
        )
        newer = SmartSyncCheckout(
            canonical=repo,
            parent=None,
            path=repo.path / "newer",
            branch="main",
            label="newer",
            head="new",
            ahead=2,
        )
        plan = CanonicalPlan(
            status=CanonicalPlanStatus.WARN,
            warning="2 checkouts ahead",
            steps=(
                SyncStep(SyncStepKind.WARN_MANUAL, str(older.path)),
                SyncStep(SyncStepKind.WARN_MANUAL, str(newer.path)),
            ),
        )

        with mock.patch.object(
                workers,
                "_commit_is_ancestor",
                side_effect=lambda _path, ancestor, descendant:
                    ancestor == "old" and descendant == "new",
        ):
            upgraded = workers._same_branch_ff_chain_plan(
                [older, newer],
                {str(older.path): older, str(newer.path): newer},
                plan,
            )

        self.assertIsNotNone(upgraded)
        self.assertEqual(upgraded.status, CanonicalPlanStatus.READY)
        self.assertEqual(upgraded.winner_id, str(newer.path))
        self.assertEqual(
            [(step.kind, step.target_id) for step in upgraded.steps],
            [
                (SyncStepKind.PUSH_WINNER, str(newer.path)),
                (SyncStepKind.ALIGN_FF, str(older.path)),
            ],
        )

    def test_align_canonical_reprobes_after_successful_work(self) -> None:
        repo = Repo(rel="repo", path=Path("/workspace/repo"))
        state = State(repos=[repo], workspace_name="ws")
        checkout_obj = SmartSyncCheckout(
            canonical=repo,
            parent=None,
            path=repo.path,
            branch="main",
            label="repo",
            head="old",
            ahead=1,
        )

        with (
            mock.patch.object(
                workers,
                "_probe_canonical_checkouts",
                return_value=[checkout_obj],
            ) as probe,
            mock.patch.object(
                workers,
                "plan_canonical_alignment",
                side_effect=[
                    CanonicalPlan(
                        status=CanonicalPlanStatus.READY,
                        winner_id=str(repo.path),
                        steps=(SyncStep(SyncStepKind.PUSH_WINNER, str(repo.path)),),
                    ),
                    CanonicalPlan(status=CanonicalPlanStatus.NOOP),
                ],
            ),
            mock.patch.object(
                workers,
                "execute_canonical_plan",
                side_effect=[(1, 0), (0, 0)],
            ) as execute,
        ):
            result = workers._align_canonical(state, repo)

        self.assertEqual(result, (1, 0))
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(execute.call_count, 2)


if __name__ == "__main__":
    unittest.main()
