"""State-level stale owner diagnostics tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from core.runtime.jobs import JobSpec, JobStatus  # noqa: E402
from core.state.app import State  # noqa: E402
from core.state.repos import ChildRef  # noqa: E402
from core.state.diagnostics import collect_stale_owner_diagnostics  # noqa: E402


class TestStateDiagnostics(unittest.TestCase):
    def test_collects_stale_jobs_and_leases_without_mutating_state(self) -> None:
        repo = _make_repo("repo")
        child_repo = _make_repo("sdk")
        child = ChildRef(
            repo=child_repo,
            nested_path=repo.path / "vendor" / "sdk",
        )
        repo.children = [child]
        state = State(repos=[repo, child_repo], workspace_name="A")
        job = state.job_registry.start(JobSpec(
            kind="push",
            label="push repo",
            local_mutation=True,
            repo_keys=(str(repo.path),),
            stale_after_seconds=5.0,
        ))
        job.started_at = 10.0
        with mock.patch("core.runtime.leases._monotonic", return_value=10.0):
            state.leases.acquire(
                child=child,
                owner_id="lease-1",
                owner_label="child sync",
                stale_after_seconds=5.0,
            )

        diagnostics = collect_stale_owner_diagnostics(state, now=20.0)

        self.assertEqual(
            [diagnostic.owner_kind for diagnostic in diagnostics],
            ["job", "lease"],
        )
        self.assertEqual(diagnostics[0].owner_id, "job-1")
        self.assertEqual(diagnostics[0].owner_label, "push repo")
        self.assertEqual(diagnostics[0].target_label, "repo")
        self.assertEqual(diagnostics[1].owner_id, "lease-1")
        self.assertEqual(diagnostics[1].owner_label, "child sync")
        self.assertEqual(diagnostics[1].target_label, "sdk")
        self.assertEqual(job.status, JobStatus.RUNNING)
        self.assertTrue(state.job_registry.has_active_local_mutation())
        self.assertTrue(state.leases.has_lease_for(children=[child]))

    def test_terminal_jobs_are_not_reported(self) -> None:
        state = State(repos=[], workspace_name="A")
        job = state.job_registry.start(JobSpec(
            kind="refresh",
            label="refresh",
            stale_after_seconds=1.0,
        ))
        job.started_at = 10.0
        state.job_registry.finish(job, JobStatus.OK)

        self.assertEqual(
            collect_stale_owner_diagnostics(state, now=20.0),
            [],
        )


if __name__ == "__main__":
    unittest.main()
