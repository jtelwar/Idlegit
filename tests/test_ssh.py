"""Unit tests for core.ssh — no curses, no real ssh-agent."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.ssh import (  # noqa: E402
    key_path_conflict_message,
    key_path_taken,
    parse_ssh_agent_exports,
    default_ed25519_path,
    public_key_path,
    ssh_tools_status,
)


class TestParseSshAgentExports(unittest.TestCase):
    def test_bash_export_lines(self) -> None:
        text = (
            "SSH_AUTH_SOCK=/tmp/ssh-abc/agent.123;\n"
            "SSH_AGENT_PID=999;\n"
        )
        env = parse_ssh_agent_exports(text)
        self.assertEqual(env["SSH_AUTH_SOCK"], "/tmp/ssh-abc/agent.123")
        self.assertEqual(env["SSH_AGENT_PID"], "999")

    def test_ssh_agent_s_style(self) -> None:
        text = (
            'export SSH_AUTH_SOCK="/tmp/agent.sock";\n'
            'export SSH_AGENT_PID="42";\n'
        )
        env = parse_ssh_agent_exports(text)
        self.assertEqual(env["SSH_AUTH_SOCK"], "/tmp/agent.sock")
        self.assertEqual(env["SSH_AGENT_PID"], "42")


class TestPublicKeyPath(unittest.TestCase):
    def test_appends_pub(self) -> None:
        self.assertEqual(
            public_key_path(Path("/home/u/.ssh/id_ed25519")),
            Path("/home/u/.ssh/id_ed25519.pub"))

    def test_dotted_basename(self) -> None:
        self.assertEqual(
            public_key_path(Path("my.key")),
            Path("my.key.pub"))


class TestSshToolsStatus(unittest.TestCase):
    def test_reports_path_presence(self) -> None:
        tools = ssh_tools_status()
        # CI/dev machines usually have OpenSSH; we only assert types.
        self.assertIsInstance(tools.has_ssh_agent, bool)
        self.assertIsInstance(tools.has_ssh_add, bool)
        self.assertIsInstance(tools.has_ssh_keygen, bool)
        self.assertIsInstance(tools.missing_tools, list)


class TestKeyPathConflict(unittest.TestCase):
    def test_empty_path_is_free(self) -> None:
        self.assertIsNone(key_path_conflict_message(""))
        self.assertIsNone(key_path_conflict_message("   "))

    def test_detects_private_key(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            priv = Path(tmp) / "id_test"
            priv.write_text("secret\n")
            self.assertTrue(key_path_taken(priv))
            self.assertIn("private key", key_path_conflict_message(str(priv)))

    def test_detects_public_key_only(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pub = root / "id_test.pub"
            pub.write_text("ssh-ed25519 AAA\n")
            self.assertTrue(key_path_taken(root / "id_test"))
            self.assertIn("public key",
                          key_path_conflict_message(str(root / "id_test")))


class TestDefaultEd25519Path(unittest.TestCase):
    def test_returns_path_under_ssh_dir(self) -> None:
        path = default_ed25519_path()
        self.assertEqual(path.parent.name, ".ssh")
        self.assertTrue(
            path.name in ("id_ed25519", "id_ed25519_github"))


if __name__ == "__main__":
    unittest.main()
