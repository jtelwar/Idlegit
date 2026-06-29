"""Smart-sync canonical plan execution.

This module sits between the pure planner and the git-specific worker helpers.
It owns the stateful walk of a canonical alignment plan while keeping actual git
operations injected by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from core.state.app import State
from ..runtime.jobs import JobTaskBridge
from ..state.smart_sync import SmartSyncCheckout
from ..state.repos import Repo
from .types import CanonicalPlan, CanonicalPlanStatus


OpenBranchPrompt = Callable[[State, SmartSyncCheckout], str]
ResolveOriginHead = Callable[[Path], str]
SwitchWinnerToBranch = Callable[[State, SmartSyncCheckout, str, str], bool]
CommitDirtyWinner = Callable[[State, SmartSyncCheckout, str], bool]
PushWinner = Callable[[State, SmartSyncCheckout, str, str], bool]
RefreshWinnerHead = Callable[[SmartSyncCheckout], None]
AlignLoser = Callable[[State, SmartSyncCheckout, str, str], bool]


@dataclass(frozen=True)
class CanonicalExecutionDeps:
    """Injected side-effect helpers required to execute a canonical plan."""

    open_branch_prompt: OpenBranchPrompt
    resolve_origin_head: ResolveOriginHead
    switch_winner_to_branch: SwitchWinnerToBranch
    commit_dirty_winner: CommitDirtyWinner
    push_winner: PushWinner
    refresh_winner_head: RefreshWinnerHead
    align_loser_ff: AlignLoser
    align_detached_loser: AlignLoser


def execute_canonical_plan(
        state: State,
        canonical: Repo,
        checkouts: List[SmartSyncCheckout],
        checkout_by_id: Dict[str, SmartSyncCheckout],
        plan: CanonicalPlan,
        deps: CanonicalExecutionDeps,
        task_bridge: Optional[JobTaskBridge] = None,
) -> Tuple[int, int]:
    """Execute one canonical smart-sync plan and return ``(ok, fail)`` counts."""
    tasks = task_bridge or JobTaskBridge(state.tasks)
    name = state.task_repo_label(canonical)
    if plan.status == CanonicalPlanStatus.NOOP:
        return 0, 0
    if plan.status == CanonicalPlanStatus.WARN:
        _warn(state, tasks, name, plan.warning)
        return 0, 1
    if plan.winner_id is None:
        return 0, 0

    winner = checkout_by_id[plan.winner_id]
    winner_branch = winner.branch
    if winner_branch == "(detached)":
        resolved_branch = _resolve_detached_winner_branch(
            state, tasks, winner, name, deps)
        if not resolved_branch:
            return 0, 1
        winner.branch = resolved_branch
        winner_branch = resolved_branch

    if winner.dirty:
        if not _plan_has_step(plan, "commit-dirty"):
            _warn(
                state,
                tasks,
                name,
                f"{winner.label} dirty - turn on auto-stage to consolidate",
            )
            return 0, 1
        if not deps.commit_dirty_winner(state, winner, name):
            return 0, 1
        winner.ahead = max(winner.ahead, 1)

    if winner.ahead > 0:
        if not deps.push_winner(state, winner, winner_branch, name):
            return 0, 1

    deps.refresh_winner_head(winner)
    return _align_losers(
        state, tasks, name, checkouts, winner, winner_branch, deps)


def _resolve_detached_winner_branch(
        state: State,
        tasks: JobTaskBridge,
        winner: SmartSyncCheckout,
        name: str,
        deps: CanonicalExecutionDeps,
) -> str:
    if not state.align_heads:
        _warn(
            state,
            tasks,
            name,
            f"{winner.label} detached - turn on align-heads to pick a branch",
        )
        return ""
    if state.prompt_for_branch:
        chosen = deps.open_branch_prompt(state, winner)
        if not chosen:
            _warn(state, tasks, name, "user cancelled detached-branch pick")
            return ""
    else:
        chosen = deps.resolve_origin_head(winner.path)
        if not chosen:
            _warn(
                state,
                tasks,
                name,
                f"{winner.label}: origin/HEAD not set - turn on "
                "prompt-for-branch to pick manually",
            )
            return ""
    if not deps.switch_winner_to_branch(state, winner, chosen, name):
        return ""
    return chosen


def _align_losers(
        state: State,
        tasks: JobTaskBridge,
        name: str,
        checkouts: List[SmartSyncCheckout],
        winner: SmartSyncCheckout,
        winner_branch: str,
        deps: CanonicalExecutionDeps,
) -> Tuple[int, int]:
    ok = 1 if winner.ahead > 0 else 0
    fail = 0
    for checkout in checkouts:
        if checkout is winner:
            continue
        if checkout.head == winner.head and not checkout.dirty:
            continue
        if not state.auto_ff:
            _warn(state, tasks, name, "auto-ff off - manual align", checkout)
            fail += 1
            continue
        if checkout.branch == winner_branch:
            if deps.align_loser_ff(state, checkout, winner_branch, name):
                ok += 1
            else:
                fail += 1
            continue
        if checkout.branch == "(detached)":
            if state.align_heads:
                if deps.align_detached_loser(state, checkout, winner_branch, name):
                    ok += 1
                else:
                    fail += 1
            else:
                _warn(state, tasks, name, "detached - align-heads off", checkout)
                fail += 1
            continue
        _warn(
            state,
            tasks,
            name,
            f"on '{checkout.branch}' (winner '{winner_branch}') - manual",
            checkout,
        )
        fail += 1
    return ok, fail


def _warn(
        state: State,
        tasks: JobTaskBridge,
        name: str,
        message: str,
        checkout: SmartSyncCheckout | None = None,
) -> None:
    label = f"  ↳ align {name}"
    if checkout is not None:
        label = f"{label}: {checkout.label}"
    t = tasks.add(label)
    tasks.update(t, "warn", message)


def _plan_has_step(plan: CanonicalPlan, kind_value: str) -> bool:
    return any(step.kind.value == kind_value for step in plan.steps)
