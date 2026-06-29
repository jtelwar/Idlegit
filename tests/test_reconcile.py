from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo_model as _make_repo  # noqa: E402
from core.reconcile import reconcile_repos_bounded, refresh_repos_bounded  # noqa: E402


class TestRefreshReposBounded(unittest.TestCase):
    def test_refreshes_all_repos_and_reports_count(self) -> None:
        repo_a = _make_repo("a")
        repo_b = _make_repo("b")
        seen = []

        result = refresh_repos_bounded(
            [repo_a, repo_b],
            refresh_fn=lambda repo: seen.append(repo.rel),
        )

        self.assertEqual(set(seen), {"a", "b"})
        self.assertEqual(result.refreshed, 2)
        self.assertEqual(result.failures, [])

    def test_captures_per_repo_failures_and_still_marks_done(self) -> None:
        repo_a = _make_repo("a")
        repo_b = _make_repo("b")
        done = []

        def refresh(repo):
            if repo is repo_b:
                raise RuntimeError("index bad")

        result = refresh_repos_bounded(
            [repo_a, repo_b],
            refresh_fn=refresh,
            on_done=lambda repo: done.append(repo.rel),
        )

        self.assertEqual(set(done), {"a", "b"})
        self.assertEqual(result.refreshed, 1)
        self.assertEqual(result.failed, 1)
        self.assertIs(result.failures[0].repo, repo_b)
        self.assertEqual(result.failures[0].message, "index bad")

    def test_should_stop_skips_remaining_work(self) -> None:
        repo = _make_repo("a")
        seen = []

        result = refresh_repos_bounded(
            [repo],
            refresh_fn=lambda r: seen.append(r.rel),
            should_stop=lambda: True,
        )

        self.assertEqual(seen, [])
        self.assertEqual(result.refreshed, 0)
        self.assertEqual(result.failed, 0)

    def test_single_worker_refresh_runs_synchronously(self) -> None:
        repo_a = _make_repo("a")
        repo_b = _make_repo("b")
        seen = []

        result = refresh_repos_bounded(
            [repo_a, repo_b],
            refresh_fn=lambda repo: seen.append(repo.rel),
            max_workers=1,
        )

        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(result.refreshed, 2)


class TestReconcileReposBounded(unittest.TestCase):
    def test_refreshes_then_links_repos(self) -> None:
        repo_a = _make_repo("a")
        repo_b = _make_repo("b")
        events = []

        result = reconcile_repos_bounded(
            [repo_a, repo_b],
            refresh_fn=lambda repo: events.append(f"refresh:{repo.rel}"),
            link_fn=lambda repos, subtrees: events.append(
                f"link:{','.join(repo.rel for repo in repos)}:{subtrees}"
            ),
        )

        self.assertEqual(set(events[:2]), {"refresh:a", "refresh:b"})
        self.assertEqual(events[2], "link:a,b:None")
        self.assertEqual(result.refresh.refreshed, 2)
        self.assertEqual(result.failed, 0)

    def test_captures_link_failure(self) -> None:
        repo = _make_repo("a")

        def fail_link(_repos, _subtrees):
            raise RuntimeError("link exploded")

        result = reconcile_repos_bounded(
            [repo],
            refresh_fn=lambda _repo: None,
            link_fn=fail_link,
        )

        self.assertEqual(result.refresh.refreshed, 1)
        self.assertEqual(result.link_error, "link exploded")
        self.assertEqual(result.failed, 1)

    def test_should_stop_after_refresh_skips_link(self) -> None:
        repo = _make_repo("a")
        calls = []

        result = reconcile_repos_bounded(
            [repo],
            refresh_fn=lambda _repo: calls.append("refresh"),
            link_fn=lambda _repos, _subtrees: calls.append("link"),
            should_stop=lambda: bool(calls),
        )

        self.assertEqual(calls, ["refresh"])
        self.assertEqual(result.refresh.refreshed, 1)
        self.assertEqual(result.link_error, "")

    def test_should_link_false_skips_link_after_refresh(self) -> None:
        repo = _make_repo("a")
        calls = []

        result = reconcile_repos_bounded(
            [repo],
            refresh_fn=lambda _repo: calls.append("refresh"),
            link_fn=lambda _repos, _subtrees: calls.append("link"),
            should_link=lambda: False,
        )

        self.assertEqual(calls, ["refresh"])
        self.assertTrue(result.link_skipped)
        self.assertEqual(result.link_error, "")

    def test_can_relink_a_larger_workspace_snapshot(self) -> None:
        repo_a = _make_repo("a")
        repo_b = _make_repo("b")
        linked = []

        reconcile_repos_bounded(
            [repo_a],
            refresh_fn=lambda _repo: None,
            link_repos=[repo_a, repo_b],
            link_fn=lambda repos, _subtrees: linked.extend(repo.rel for repo in repos),
        )

        self.assertEqual(linked, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
