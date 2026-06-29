"""Pure smart-sync planning.

This module intentionally knows nothing about curses, State, Task, threads,
leases, or git subprocess execution. It mirrors the current winner-selection
rules from ``core.workers._align_canonical`` so the threaded runner can migrate
toward a typed plan without changing behavior in one risky jump.
"""

from __future__ import annotations

from typing import Iterable, List

from .types import (
    CanonicalPlan,
    CanonicalPlanStatus,
    CheckoutSnapshot,
    SmartSyncSettings,
    SyncStep,
    SyncStepKind,
)


def plan_canonical_alignment(
    checkouts: Iterable[CheckoutSnapshot],
    *,
    settings: SmartSyncSettings,
) -> CanonicalPlan:
    """Plan alignment for one canonical group from read-only checkout facts."""
    items = list(checkouts)
    if not items:
        return CanonicalPlan(status=CanonicalPlanStatus.NOOP)

    heads = {c.head for c in items if c.head}
    any_dirty = any(c.dirty for c in items)
    any_ahead = any(c.ahead > 0 for c in items)
    if len(heads) <= 1 and not any_dirty and not any_ahead:
        return CanonicalPlan(status=CanonicalPlanStatus.NOOP)

    aheads = [c for c in items if c.ahead > 0]
    if len(aheads) > 1:
        labels = ", ".join(c.label for c in aheads)
        return CanonicalPlan(
            status=CanonicalPlanStatus.WARN,
            warning=(f"{len(aheads)} checkouts ahead - manual resolve: {labels}"),
            steps=tuple(
                SyncStep(SyncStepKind.WARN_MANUAL, c.checkout_id, "checkout ahead of upstream")
                for c in aheads
            ),
        )

    winner = _choose_winner(items, heads, aheads, any_dirty)
    if winner is None:
        return CanonicalPlan(status=CanonicalPlanStatus.NOOP)

    steps: List[SyncStep] = []
    winner_branch = winner.branch
    if winner_branch == "(detached)":
        if not settings.align_heads:
            return CanonicalPlan(
                status=CanonicalPlanStatus.WARN,
                winner_id=winner.checkout_id,
                warning=(f"{winner.label} detached - turn on align-heads to pick a branch"),
                steps=(
                    SyncStep(
                        SyncStepKind.WARN_MANUAL,
                        winner.checkout_id,
                        "detached winner needs a branch",
                    ),
                ),
            )
        if settings.prompt_for_branch:
            steps.append(SyncStep(SyncStepKind.PROMPT_BRANCH, winner.checkout_id))
        else:
            steps.append(SyncStep(SyncStepKind.RESOLVE_ORIGIN_HEAD, winner.checkout_id))

    winner_will_commit = False
    if winner.dirty:
        if not settings.auto_stage:
            return CanonicalPlan(
                status=CanonicalPlanStatus.WARN,
                winner_id=winner.checkout_id,
                warning=(f"{winner.label} dirty - turn on auto-stage to consolidate"),
                steps=(
                    SyncStep(
                        SyncStepKind.WARN_MANUAL,
                        winner.checkout_id,
                        "dirty winner requires auto-stage",
                    ),
                ),
            )
        winner_will_commit = True
        steps.append(SyncStep(SyncStepKind.COMMIT_DIRTY, winner.checkout_id))

    if winner.ahead > 0 or winner_will_commit:
        steps.append(SyncStep(SyncStepKind.PUSH_WINNER, winner.checkout_id))

    for checkout in items:
        if checkout.checkout_id == winner.checkout_id:
            continue
        if not winner_will_commit and checkout.head == winner.head and not checkout.dirty:
            continue
        if not settings.auto_ff:
            steps.append(
                SyncStep(
                    SyncStepKind.WARN_MANUAL, checkout.checkout_id, "auto-ff off - manual align"
                )
            )
            continue
        if checkout.branch == winner_branch:
            steps.append(SyncStep(SyncStepKind.ALIGN_FF, checkout.checkout_id))
            continue
        if checkout.branch == "(detached)":
            if settings.align_heads:
                steps.append(SyncStep(SyncStepKind.ALIGN_DETACHED, checkout.checkout_id))
            else:
                steps.append(
                    SyncStep(
                        SyncStepKind.WARN_MANUAL, checkout.checkout_id, "detached - align-heads off"
                    )
                )
            continue
        steps.append(
            SyncStep(
                SyncStepKind.WARN_MANUAL,
                checkout.checkout_id,
                f"on '{checkout.branch}' (winner '{winner_branch}') - manual",
            )
        )

    return CanonicalPlan(
        status=CanonicalPlanStatus.READY,
        winner_id=winner.checkout_id,
        steps=tuple(steps),
    )


def _choose_winner(
    checkouts: List[CheckoutSnapshot],
    heads: set[str],
    aheads: List[CheckoutSnapshot],
    any_dirty: bool,
) -> CheckoutSnapshot | None:
    if aheads:
        return aheads[0]
    if any_dirty:
        return max((c for c in checkouts if c.dirty), key=lambda c: c.sig_mtime)
    if len(heads) > 1:
        return max(checkouts, key=lambda c: c.commit_time)
    return None
