"""SSH agent detection/startup and keypair helpers for GitHub auth.

Idlegit never runs destructive git operations; `ssh-keygen` only creates
new key material when the target path does not already exist."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

_GITHUB_KEYS_URL = "https://github.com/settings/keys"
_GITHUB_NEW_KEY_URL = "https://github.com/settings/ssh/new"


def github_keys_help_url() -> str:
    return _GITHUB_KEYS_URL


def github_new_key_url() -> str:
    return _GITHUB_NEW_KEY_URL


def default_ssh_dir() -> Path:
    return Path.home() / ".ssh"


@dataclass(frozen=True)
class SshToolsStatus:
    """Which OpenSSH CLI tools are on PATH and whether the agent answers."""
    has_ssh_agent: bool
    has_ssh_add: bool
    has_ssh_keygen: bool
    agent_running: bool
    keys_loaded: Optional[int]  # None = could not determine

    @property
    def missing_tools(self) -> List[str]:
        missing: List[str] = []
        if not self.has_ssh_agent:
            missing.append("ssh-agent")
        if not self.has_ssh_add:
            missing.append("ssh-add")
        if not self.has_ssh_keygen:
            missing.append("ssh-keygen")
        return missing


def ssh_tools_status() -> SshToolsStatus:
    """Snapshot for the app menu and pre-flight checks before SSH actions."""
    running = agent_running()
    keys = count_loaded_keys() if running else None
    return SshToolsStatus(
        has_ssh_agent=shutil.which("ssh-agent") is not None,
        has_ssh_add=shutil.which("ssh-add") is not None,
        has_ssh_keygen=shutil.which("ssh-keygen") is not None,
        agent_running=running,
        keys_loaded=keys,
    )


def public_key_path(key_path: Path) -> Path:
    """Path ssh-keygen writes for ``-f key_path`` (always ``<path>.pub``)."""
    return Path(f"{key_path.expanduser()}.pub")


def key_path_taken(key_path: Path) -> bool:
    """True when either the private key or its ``.pub`` file exists."""
    key_path = key_path.expanduser()
    return key_path.exists() or public_key_path(key_path).exists()


def key_path_conflict_message(path_text: str) -> Optional[str]:
    """User-facing refusal when `path_text` would overwrite existing keys.

    Returns None when the path is empty, unparsable, or free to use."""
    text = (path_text or "").strip()
    if not text:
        return None
    try:
        key_path = Path(text).expanduser()
    except (TypeError, ValueError):
        return None
    if key_path.exists() and public_key_path(key_path).exists():
        return f"key already exists: {key_path}"
    if key_path.exists():
        return f"private key already exists: {key_path}"
    if public_key_path(key_path).exists():
        return f"public key already exists: {public_key_path(key_path)}"
    return None


def default_ed25519_path() -> Path:
    """Prefer a GitHub-specific name when the generic key already exists."""
    ssh_dir = default_ssh_dir()
    generic = ssh_dir / "id_ed25519"
    if generic.exists() or generic.with_suffix(".pub").exists():
        return ssh_dir / "id_ed25519_github"
    return generic


def parse_ssh_agent_exports(text: str) -> Dict[str, str]:
    """Parse `ssh-agent -s` / `-c` shell output into env var names → values."""
    env: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip().rstrip(";").strip()
        if not line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        raw = raw.strip()
        if (raw.startswith('"') and raw.endswith('"')
                or raw.startswith("'") and raw.endswith("'")):
            raw = raw[1:-1]
        if key:
            env[key] = raw
    return env


def agent_socket_path() -> Optional[Path]:
    sock = os.environ.get("SSH_AUTH_SOCK", "").strip()
    if not sock:
        return None
    path = Path(sock)
    try:
        if path.exists():
            return path
    except OSError:
        return None
    return None


def agent_pid() -> Optional[int]:
    raw = os.environ.get("SSH_AGENT_PID", "").strip()
    if raw.isdigit():
        return int(raw)
    return None


def _agent_responds() -> bool:
    """True when ``ssh-add -l`` can talk to the socket (exit 0 or 1)."""
    if shutil.which("ssh-add") is None:
        # No probe available — fall back to socket existence only.
        return agent_socket_path() is not None
    try:
        proc = subprocess.run(
            ["ssh-add", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode in (0, 1)


def agent_running() -> bool:
    if agent_socket_path() is None:
        return False
    return _agent_responds()


def count_loaded_keys() -> Optional[int]:
    """Return key count, 0 when the agent is up but empty, None if unknown."""
    if shutil.which("ssh-add") is None:
        return None
    if agent_socket_path() is None:
        return None
    try:
        proc = subprocess.run(
            ["ssh-add", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    combined = (proc.stdout + proc.stderr).lower()
    if proc.returncode == 0:
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        return len(lines)
    if proc.returncode == 1 and "no identities" in combined:
        return 0
    return None


def _apply_agent_env(env: Dict[str, str]) -> None:
    if "SSH_AUTH_SOCK" in env:
        os.environ["SSH_AUTH_SOCK"] = env["SSH_AUTH_SOCK"]
    if "SSH_AGENT_PID" in env:
        os.environ["SSH_AGENT_PID"] = env["SSH_AGENT_PID"]


def start_ssh_agent_unix() -> Tuple[bool, Optional[str]]:
    agent_bin = shutil.which("ssh-agent")
    if not agent_bin:
        return False, "ssh-agent not found on PATH"
    try:
        proc = subprocess.run(
            [agent_bin, "-s"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "ssh-agent failed").strip()
        return False, err or "ssh-agent failed"
    env = parse_ssh_agent_exports(proc.stdout)
    if "SSH_AUTH_SOCK" not in env:
        return False, "could not parse ssh-agent output"
    _apply_agent_env(env)
    return True, None


def start_ssh_agent_windows() -> Tuple[bool, Optional[str]]:
    """Best-effort: start the OpenSSH Agent service, then a session agent."""
    if shutil.which("powershell"):
        try:
            subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "Start-Service ssh-agent -ErrorAction SilentlyContinue",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    agent_bin = shutil.which("ssh-agent")
    if agent_bin:
        try:
            proc = subprocess.run(
                [agent_bin],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        if proc.returncode == 0:
            env = parse_ssh_agent_exports(proc.stdout)
            if "SSH_AUTH_SOCK" in env:
                _apply_agent_env(env)
                return True, None
    if agent_running():
        return True, None
    return False, (
        "ssh-agent is not running — start the OpenSSH Authentication Agent "
        "service or run ssh-agent in your shell")


def start_ssh_agent() -> Tuple[bool, Optional[str]]:
    if sys.platform == "win32":
        return start_ssh_agent_windows()
    return start_ssh_agent_unix()


def ensure_ssh_agent(auto_start: bool) -> Tuple[str, Optional[str]]:
    """Ensure an agent is available when `auto_start` is enabled.

    Returns `(status, warning)` where status is one of:
      - ``running`` — already had a usable ``SSH_AUTH_SOCK``
      - ``started`` — idlegit started one this session
      - ``not_running`` — auto-start off and no agent
      - ``failed`` — auto-start on but could not start
    """
    if agent_running():
        return "running", None
    if not auto_start:
        return "not_running", None
    ok, err = start_ssh_agent()
    if ok:
        return "started", None
    return "failed", err


def agent_status_label() -> str:
    tools = ssh_tools_status()
    if not tools.has_ssh_agent:
        return "ssh-agent not on PATH"
    if agent_socket_path() is None:
        return "not running"
    if not tools.agent_running:
        return "socket stale (agent not responding)"
    sock = agent_socket_path()
    pid = agent_pid()
    parts = ["running"]
    if sock is not None:
        parts.append(str(sock))
    if pid is not None:
        parts.append(f"pid {pid}")
    return " · ".join(parts)


def keys_loaded_label() -> str:
    tools = ssh_tools_status()
    if not tools.has_ssh_add:
        return "ssh-add not on PATH"
    if not tools.agent_running:
        return "agent not running"
    n = tools.keys_loaded
    if n is None:
        return "unknown"
    if n == 0:
        return "none loaded"
    if n == 1:
        return "1 key loaded"
    return f"{n} keys loaded"


def git_user_email() -> str:
    """Best-effort `git config user.email` for key comments."""
    if shutil.which("git") is None:
        return ""
    try:
        proc = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def create_ed25519_keypair(
        key_path: Path,
        email: str,
        *,
        passphrase: str = "") -> Tuple[bool, str]:
    """Generate an ed25519 key at `key_path` and load it into the agent.

    Refuses to overwrite an existing private or public key file.
    """
    key_path = key_path.expanduser()
    conflict = key_path_conflict_message(str(key_path))
    if conflict:
        return False, conflict
    pub_path = public_key_path(key_path)

    keygen = shutil.which("ssh-keygen")
    if not keygen:
        return False, "ssh-keygen not found on PATH"

    comment = email.strip() or "idlegit"
    ssh_dir = key_path.parent
    try:
        ssh_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(ssh_dir, 0o700)
    except OSError as e:
        return False, f"could not create {ssh_dir}: {e}"

    cmd = [
        keygen,
        "-t", "ed25519",
        "-C", comment,
        "-f", str(key_path),
        "-N", passphrase,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "ssh-keygen failed").strip()
        return False, err or "ssh-keygen failed"

    try:
        os.chmod(key_path, 0o600)
        if pub_path.exists():
            os.chmod(pub_path, 0o644)
    except OSError:
        pass

    add_err = add_key_to_agent(key_path)
    if add_err:
        return True, (
            f"created {key_path} but could not ssh-add: {add_err}")
    return True, ""


def add_key_to_agent(key_path: Path) -> Optional[str]:
    """Load one private key into the running agent. Returns error text."""
    if not agent_running():
        return "ssh-agent is not running"
    add_bin = shutil.which("ssh-add")
    if not add_bin:
        return "ssh-add not found on PATH"
    key_path = key_path.expanduser()
    if not key_path.is_file():
        return f"no such key: {key_path}"
    try:
        proc = subprocess.run(
            [add_bin, str(key_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "ssh-add failed").strip()
    return None


def read_public_key(key_path: Path) -> Tuple[Optional[str], Optional[str]]:
    pub = public_key_path(key_path)
    try:
        text = pub.read_text(encoding="utf-8").strip()
    except OSError as e:
        return None, str(e)
    return text, None


def add_default_keys_to_agent() -> Tuple[int, List[str]]:
    """Try ssh-add on the usual default key paths. Returns (added, errors)."""
    ssh_dir = default_ssh_dir()
    candidates = [
        ssh_dir / "id_ed25519",
        ssh_dir / "id_rsa",
        ssh_dir / "id_ed25519_github",
    ]
    added = 0
    errors: List[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        err = add_key_to_agent(path)
        if err:
            if "already" in err.lower():
                added += 1
            else:
                errors.append(f"{path.name}: {err}")
        else:
            added += 1
    return added, errors
