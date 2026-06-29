from __future__ import annotations

import shlex
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import make_repo  # noqa: E402
from core.git_ops import git_cancellable  # noqa: E402


class TestGitCancellable(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.repo = make_repo(self.tmp, "repo")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _python_alias(self, script: str) -> list[str]:
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
        return ["-c", f"alias.idlegit-test=!{command}", "idlegit-test"]

    def test_verbose_command_does_not_block_on_full_pipe(self) -> None:
        script = (
            "import sys; "
            "sys.stdout.write('x' * 262144); "
            "sys.stdout.flush()"
        )
        rc, out, err = git_cancellable(
            self.repo,
            self._python_alias(script),
            cancel_event=threading.Event(),
            timeout=5.0,
            poll_interval=0.05,
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(out), 262144)

    def test_cancel_terminates_process(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()
        rc, out, err = git_cancellable(
            self.repo,
            self._python_alias("import time; time.sleep(10)"),
            cancel_event=cancel_event,
            timeout=5.0,
            poll_interval=0.05,
        )
        self.assertEqual(rc, 130)
        self.assertEqual(out, "")
        self.assertTrue(err)

    def test_timeout_kills_process(self) -> None:
        rc, out, err = git_cancellable(
            self.repo,
            self._python_alias("import time; time.sleep(10)"),
            cancel_event=threading.Event(),
            timeout=0.1,
            poll_interval=0.02,
        )
        self.assertEqual(rc, 124)
        self.assertEqual(out, "")
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
