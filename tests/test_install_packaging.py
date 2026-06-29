"""Installer and packaging manifest regression tests."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.install import install_files  # noqa: E402


class TestInstallPackaging(unittest.TestCase):
    def test_install_files_copies_features_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "idlegit"
            summary: list[str] = []
            with contextlib.redirect_stdout(io.StringIO()):
                install_files(home, summary)

            self.assertTrue((home / "features" / "__init__.py").is_file())
            self.assertTrue((home / "features" / "action_menu" / "actions.py").is_file())

    def test_pyproject_package_discovery_includes_features(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()

        self.assertIn('"features*"', text)


if __name__ == "__main__":
    unittest.main()
