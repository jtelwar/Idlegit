#!/usr/bin/env python3
"""Idlegit updater — checks the GitHub Releases API and re-runs the
installer against a newer source tarball when one is available.

Pipeline mirrors install.py's status-row style so the two tools read
as one. Network access is stdlib-only (urllib + json + tarfile),
so no `gh` / curl / tar dependency on the user's box.

  1. Read installed VERSION from config.py.
  2. GET https://api.github.com/repos/{GITHUB_REPO}/releases/latest.
  3. Parse the tag (vX.Y.Z) and compare against VERSION.
  4. If a newer release exists, download `tarball_url`, extract it
     to a temp dir, and exec install.py from inside that tarball
     with the same env (so IDLEGIT_HOME / IDLEGIT_BINDIR /
     IDLEGIT_CONFIG_DIR carry through). The installer's
     merge_config step handles user-config preservation — we
     don't touch ~/.config/idlegit ourselves.

Flags:
  -y / --yes        skip the "Apply update?" prompt
  --check           print version status and exit (no install)
  --force           run the install even if VERSION >= latest tag
                    (useful when reinstalling the same release)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# In the dev tree this script sits in `scripts/`, with config.py one
# level up at the project root. After install.py runs, every file
# lands flat in IDLEGIT_HOME, so config.py is right next to update.py
# instead. Adding both candidates to sys.path keeps the import
# working in either layout without us probing the filesystem.
for _candidate in (ROOT, ROOT.parent):
    sys.path.insert(0, str(_candidate))
from core.config import APP_DISPLAY_NAME, GITHUB_REPO, VERSION  # noqa: E402


# ---------- ANSI colors --------------------------------------------------
# Same heuristic as install.py: colour only when stdout is a TTY and
# NO_COLOR isn't set. Keeps piped output / CI logs ASCII-clean.
_USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _wrap(seq: str, text: str) -> str:
    return f"{seq}{text}\033[0m" if _USE_COLOR else text


def _bold(text: str) -> str:    return _wrap("\033[1m", text)
def _dim(text: str) -> str:     return _wrap("\033[2m", text)
def _red(text: str) -> str:     return _wrap("\033[31m", text)
def _green(text: str) -> str:   return _wrap("\033[32m", text)
def _yellow(text: str) -> str:  return _wrap("\033[33m", text)
def _cyan(text: str) -> str:    return _wrap("\033[36m", text)
def _magenta(text: str) -> str: return _wrap("\033[35m", text)


# ---------- Output primitives -------------------------------------------


def _hairline() -> str:
    try:
        cols = shutil.get_terminal_size((80, 24)).columns
    except OSError:
        cols = 80
    return "━" * min(60, max(40, cols))


def header() -> None:
    line = _hairline()
    print(_dim(line))
    print(f"  {_magenta(_bold(APP_DISPLAY_NAME))} "
          f"{_dim('updater')} {_dim('v' + VERSION)}")
    print(_dim(line))


def section(text: str) -> None:
    print()
    print(f"{_cyan('▸')} {_bold(text)}")


def ok(text: str, detail: str = "") -> None:
    suffix = f" {_dim(detail)}" if detail else ""
    print(f"  {_green('✓')} {text}{suffix}")


def warn(text: str, detail: str = "") -> None:
    suffix = f" {_dim(detail)}" if detail else ""
    print(f"  {_yellow('⚠')} {text}{suffix}")


def info(text: str) -> None:
    print(f"  {_dim('•')} {text}")


def err(text: str) -> None:
    print(f"  {_red('✗')} {text}", file=sys.stderr)


def die(text: str) -> "None":
    err(text)
    sys.exit(1)


def prompt_yes(text: str, assume_yes: bool) -> bool:
    """y/N prompt — returns True on `y`/`yes`, False on anything else.
    `assume_yes` short-circuits without reading from stdin so non-tty
    runs (CI, piped invocations) don't hang."""
    if assume_yes:
        info(f"{text} {_dim('[auto-yes]')}")
        return True
    try:
        ans = input(f"  {_yellow('?')} {text} [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


# ---------- Args --------------------------------------------------------


def parse_args(argv: "list[str]") -> "tuple[bool, bool, bool]":
    """Return (assume_yes, check_only, force). Same `-y` semantics as
    install.py; `--check` is read-only; `--force` reinstalls the
    latest tag even when VERSION already matches."""
    assume_yes = bool(os.environ.get("IDLEGIT_INSTALL_ASSUME_YES"))
    check_only = False
    force = False
    for arg in argv[1:]:
        if arg in ("-y", "--yes"):
            assume_yes = True
        elif arg == "--check":
            check_only = True
        elif arg == "--force":
            force = True
        elif arg in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            die(f"unknown argument: {arg}")
    return assume_yes, check_only, force


# ---------- Version comparison ------------------------------------------


def _parse_version(text: str) -> "tuple[int, ...]":
    """Best-effort `vX.Y.Z[.…]` → tuple of ints. Strips a leading `v`
    and tolerates pre-release suffixes (e.g. `1.0.0-rc1`) by reading
    only the dotted-int prefix. Raises ValueError when no leading
    int part is present so the caller can fall back to a string
    compare."""
    s = text.strip().lstrip("vV")
    # Cut at the first non-version separator so `1.2.3-rc1` → `1.2.3`.
    head = []
    for ch in s:
        if ch.isdigit() or ch == ".":
            head.append(ch)
        else:
            break
    parts = "".join(head).split(".")
    out = tuple(int(p) for p in parts if p)
    if not out:
        raise ValueError(f"no version digits in {text!r}")
    return out


def _compare_versions(installed: str, latest: str) -> int:
    """Return -1 if installed < latest, 0 equal, 1 ahead. Falls back
    to a lexical compare when either side fails to parse so we don't
    crash on a non-standard tag — the user just sees "can't tell"."""
    try:
        a = _parse_version(installed)
        b = _parse_version(latest)
    except ValueError:
        if installed == latest:
            return 0
        return -1 if installed < latest else 1
    if a == b:
        return 0
    return -1 if a < b else 1


# ---------- GitHub Releases ---------------------------------------------


_API_TIMEOUT = 15  # seconds — releases endpoint is small + fast.
_USER_AGENT = f"idlegit-updater/{VERSION} (+https://github.com/{GITHUB_REPO})"


def _http_get(url: str, *, accept: str = "application/json") -> bytes:
    """GET wrapper that adds the headers GitHub expects (User-Agent
    is mandatory; Accept pins the API version) and propagates a
    descriptive error message on failure."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": accept,
    })
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        die(f"HTTP {e.code} from {url}: {e.reason}")
    except urllib.error.URLError as e:
        die(f"network error reaching {url}: {e.reason}")
    except OSError as e:
        die(f"I/O error reaching {url}: {e}")
    return b""  # unreachable — die() exits


def fetch_latest_release() -> "dict":
    """Return the parsed JSON for `/releases/latest`. Bails out with
    a clear error if the repo has no releases yet (404 from the API)
    or the response shape doesn't carry a tag."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    raw = _http_get(url, accept="application/vnd.github+json")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        die(f"unparseable release response: {e}")
    if not isinstance(data, dict) or "tag_name" not in data:
        die("release response missing tag_name — has the repo cut "
            "any releases yet?")
    return data


# ---------- Download + extract ------------------------------------------


def _download(url: str, dest: Path) -> None:
    """Stream the tarball straight to disk so a large release doesn't
    have to materialise in memory."""
    info(f"download → {_dim(str(dest))}")
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, \
                dest.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
    except urllib.error.HTTPError as e:
        die(f"download failed: HTTP {e.code} {e.reason}")
    except urllib.error.URLError as e:
        die(f"download failed: {e.reason}")
    except OSError as e:
        die(f"download failed: {e}")


def _extract(tarball: Path, dest: Path) -> Path:
    """Extract `tarball` under `dest` and return the single top-level
    directory inside (GitHub's source tarballs nest everything under
    a `<owner>-<repo>-<sha>` folder). Refuses to extract entries
    whose paths escape `dest` — defence-in-depth against a hostile
    archive even though we trust github.com."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            members = tf.getmembers()
            for m in members:
                target = (dest / m.name).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    die(f"refusing tarball entry outside dest: {m.name}")
            # `filter='data'` is the safe default introduced in 3.12;
            # older Pythons fall back to plain extractall (still
            # bounded by the per-member check above).
            try:
                tf.extractall(dest, filter="data")  # type: ignore[arg-type]
            except TypeError:
                tf.extractall(dest)
    except tarfile.TarError as e:
        die(f"extract failed: {e}")
    children = [c for c in dest.iterdir() if c.is_dir()]
    if len(children) != 1:
        die(f"unexpected tarball layout — {len(children)} top-level "
            "directories; expected exactly one")
    return children[0]


# ---------- Run install.py from the extracted tree ---------------------


def _run_installer(install_dir: Path, assume_yes: bool) -> None:
    """Hand off to the installer that ships inside the new release
    tarball. We always run the *new* install.py (not the one bundled
    with the running update.py) so install-pipeline changes ride
    along with the release. Forwards IDLEGIT_* env vars + `-y`.

    Resolution: current layout is `scripts/install.py`; older releases
    had `install.py` at the tarball root, so fall back there for
    backward compatibility when a user updates from a very old build."""
    installer = install_dir / "scripts" / "install.py"
    if not installer.is_file():
        installer = install_dir / "install.py"  # legacy layout
    if not installer.is_file():
        die(f"install.py not found in tarball ({install_dir})")
    cmd = [sys.executable, str(installer)]
    if assume_yes:
        cmd.append("-y")
    info(f"exec {_dim(' '.join(cmd))}")
    print()  # blank line so installer's own header reads clean
    rc = subprocess.run(cmd, cwd=install_dir).returncode
    if rc != 0:
        die(f"installer exited with status {rc}")


# ---------- Pipeline ---------------------------------------------------


def main(argv: "list[str] | None" = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    assume_yes, check_only, force = parse_args(argv)

    header()

    section("Check for updates")
    info(f"installed: {_bold('v' + VERSION)}")
    info(f"repo: {_dim(GITHUB_REPO)}")
    release = fetch_latest_release()
    latest_tag = str(release["tag_name"])
    info(f"latest:    {_bold(latest_tag)}")

    cmp = _compare_versions(VERSION, latest_tag)
    if cmp == 0 and not force:
        ok("already on the latest release")
        return 0
    if cmp > 0 and not force:
        warn("installed version is ahead of latest release",
             f"v{VERSION} > {latest_tag}")
        info("nothing to do (pass --force to reinstall the latest tag)")
        return 0
    if check_only:
        if cmp < 0:
            warn("update available", f"v{VERSION} → {latest_tag}")
        return 0

    if cmp < 0:
        section(f"Apply update {_bold('v' + VERSION)} → {_bold(latest_tag)}")
    else:
        section(f"Reinstall {_bold(latest_tag)} (--force)")

    if not prompt_yes("Download and install?", assume_yes):
        info("aborted")
        return 0

    tarball_url = release.get("tarball_url")
    if not tarball_url:
        die("release JSON has no tarball_url")

    # Use a fresh temp dir so we don't collide with leftovers from a
    # previous interrupted run; always clean up on exit.
    with tempfile.TemporaryDirectory(prefix="idlegit-update-") as tmp:
        tmp_path = Path(tmp)
        tarball = tmp_path / f"{latest_tag}.tar.gz"
        _download(tarball_url, tarball)
        ok(f"downloaded {_dim(_human_size(tarball))}")
        extract_dir = tmp_path / "src"
        install_dir = _extract(tarball, extract_dir)
        ok(f"extracted → {_dim(str(install_dir))}")
        _run_installer(install_dir, assume_yes=True)

    section("Done")
    ok(f"updated to {_bold(latest_tag)}")
    print()
    print(_green(_bold(f"Updated to {APP_DISPLAY_NAME} {latest_tag}")))
    return 0


def _human_size(path: Path) -> str:
    try:
        n = path.stat().st_size
    except OSError:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} GiB"


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        err("interrupted")
        sys.exit(130)
