"""Smart-sync canonical executor tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.state.app import State  # noqa: E402
from core.state.smart_sync import SmartSyncCheckout  # noqa: E402
from core.state.repos import Repo  # noqa: E402
from core.smart_sync.executor import (  # noqa: E402
    CanonicalExecutionDeps,
    execute_canonical_plan,
)
from core.smart_sync.types import (  # noqa: E402
    CanonicalPlan,
    CanonicalPlanStatus,
    SyncStep,
    SyncStepKind,
)


def checkout(
        repo: Repo,
        label: str,
        *,
        branch: str = "main",
        head: str = "aaa",
        dirty: bool = False,
        ahead: int = 0,
) -> SmartSyncCheckout:
    return SmartSyncCheckout(
        canonical=repo,
        parent=None,
        path=repo.path / label,
        branch=branch,
        label=label,
        head=head,
        dirty=dirty,
        ahead=ahead,
    )


class TestSmartSyncExecutor(unittest.TestCase):
    def test_push_winner_then_aligns_same_branch_loser(self) -> None:
        repo = Repo(rel="repo", path=Path("/workspace/repo"))
        state = State(repos=[repo], workspace_name="ws")
        winner = checkout(repo, "winner", head="bbb", ahead=1)
        loser = checkout(repo, "loser", head="aaa")
        calls: list[str] = []
        deps = self._deps(
            calls,
            push=lambda _state, _winner, _branch, _name: calls.append("push") or True,
            refresh=lambda target: setattr(target, "head", "bbb"),
            align_ff=lambda _state, _loser, _branch, _name: calls.append("ff") or True,
        )

        ok, fail = execute_canonical_plan(
            state,
            repo,
            [winner, loser],
            {str(winner.path): winner, str(loser.path): loser},
            CanonicalPlan(
                status=CanonicalPlanStatus.READY,
                winner_id=str(winner.path),
                steps=(
                    SyncStep(SyncStepKind.PUSH_WINNER, str(winner.path)),
                    SyncStep(SyncStepKind.ALIGN_FF, str(loser.path)),
                ),
            ),
            deps,
        )

        self.assertEqual((ok, fail), (2, 0))
        self.assertEqual(calls, ["push", "ff"])

    def test_detached_winner_resolves_branch_before_commit_and_push(self) -> None:
        repo = Repo(rel="repo", path=Path("/workspace/repo"))
        state = State(repos=[repo], workspace_name="ws")
        state.prompt_for_branch = False
        winner = checkout(repo, "winner", branch="(detached)", dirty=True)
        loser = checkout(repo, "loser", head="old")
        calls: list[str] = []
        deps = self._deps(
            calls,
            resolve=lambda _path: calls.append("resolve") or "main",
            switch=lambda _state, _winner, branch, _name: calls.append(f"switch:{branch}") or True,
            commit=lambda _state, _winner, _name: calls.append("commit") or True,
            push=lambda _state, _winner, branch, _name: calls.append(f"push:{branch}") or True,
            refresh=lambda target: setattr(target, "head", "new"),
            align_ff=lambda _state, _loser, branch, _name: calls.append(f"ff:{branch}") or True,
        )

        ok, fail = execute_canonical_plan(
            state,
            repo,
            [winner, loser],
            {str(winner.path): winner, str(loser.path): loser},
            CanonicalPlan(
                status=CanonicalPlanStatus.READY,
                winner_id=str(winner.path),
                steps=(
                    SyncStep(SyncStepKind.RESOLVE_ORIGIN_HEAD, str(winner.path)),
                    SyncStep(SyncStepKind.COMMIT_DIRTY, str(winner.path)),
                    SyncStep(SyncStepKind.PUSH_WINNER, str(winner.path)),
                    SyncStep(SyncStepKind.ALIGN_FF, str(loser.path)),
                ),
            ),
            deps,
        )

        self.assertEqual((ok, fail), (2, 0))
        self.assertEqual(winner.branch, "main")
        self.assertEqual(calls, ["resolve", "switch:main", "commit", "push:main", "ff:main"])

    def _deps(
            self,
            calls: list[str],
            *,
            resolve=None,
            switch=None,
            commit=None,
            push=None,
            refresh=None,
            align_ff=None,
            align_detached=None,
    ) -> CanonicalExecutionDeps:
        return CanonicalExecutionDeps(
            open_branch_prompt=lambda _state, _winner: "",
            resolve_origin_head=resolve or (lambda _path: ""),
            switch_winner_to_branch=switch or (
                lambda _state, _winner, _branch, _name: False),
            commit_dirty_winner=commit or (
                lambda _state, _winner, _name: calls.append("commit") or True),
            push_winner=push or (
                lambda _state, _winner, _branch, _name: calls.append("push") or True),
            refresh_winner_head=refresh or (lambda _winner: None),
            align_loser_ff=align_ff or (
                lambda _state, _loser, _branch, _name: calls.append("ff") or True),
            align_detached_loser=align_detached or (
                lambda _state, _loser, _branch, _name: calls.append("detached") or True),
        )


if __name__ == "__main__":
    unittest.main()
