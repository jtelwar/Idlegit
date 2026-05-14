#!/usr/bin/env python3
"""Idlegit installer.

Drives the checks-then-install pipeline: verifies the repo layout,
the local toolchain (git / gh / git-lfs), copies the app under
IDLEGIT_HOME, symlinks the launcher into IDLEGIT_BINDIR (preferring
the writable Homebrew bindir on macOS so the launcher lands on the
default PATH), merges new idlegit.conf keys into the user config,
and offers to add the bindir to PATH when it isn't already.

Output is colored when stderr is a TTY and `NO_COLOR` is unset:
green ✓ for OK, yellow ⚠ for missing-but-optional deps (with a
prompt), red ✗ for hard failures.

Run via the thin `./install` launcher at the repo root (which
checks the Python toolchain first). Same env vars and flags:

  -y / --yes                         Don't prompt (treat warns as accepted)
  IDLEGIT_HOME                       App directory
  IDLEGIT_BINDIR                     Launcher symlink directory
  IDLEGIT_CONFIG_DIR                 User config dir override
  IDLEGIT_INSTALL_ASSUME_YES=1       Same as --yes
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# This script lives in `scripts/` next to update.py + merge_config.py.
# `PROJECT_ROOT` is the repo root (one level up); that's where the
# runtime app files (config.py, idlegit.py, ui/, …) sit and where the
# template idlegit.conf lives. `SCRIPTS_DIR` is where the launcher +
# helper scripts (idlegit-update, merge_config.py, update.py) live.
SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
from core.config import APP_DISPLAY_NAME, VERSION  # noqa: E402


# ---------- ANSI colors --------------------------------------------------

# Honour NO_COLOR + TTY detection so piping the output to a file
# or a CI runner emits clean ASCII without escape sequences.
_USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _wrap(seq: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"{seq}{text}\033[0m"


def _bold(text: str) -> str:    return _wrap("\033[1m", text)
def _dim(text: str) -> str:     return _wrap("\033[2m", text)
def _red(text: str) -> str:     return _wrap("\033[31m", text)
def _green(text: str) -> str:   return _wrap("\033[32m", text)
def _yellow(text: str) -> str:  return _wrap("\033[33m", text)
def _cyan(text: str) -> str:    return _wrap("\033[36m", text)
def _magenta(text: str) -> str: return _wrap("\033[35m", text)


# ---------- Output primitives -------------------------------------------


def _hairline() -> str:
    """A divider that scales with the terminal but caps at 60 cells
    so the header doesn't sprawl on a wide window."""
    try:
        cols = shutil.get_terminal_size((80, 24)).columns
    except OSError:
        cols = 80
    return "━" * min(60, max(40, cols))


def header() -> None:
    """Welcome banner — magenta name + dim version, flanked by a
    heavy hairline. Mirrors how the loading screen renders the
    title row inside the app."""
    line = _hairline()
    print()
    print(_dim(line))
    print(f"  {_magenta(_bold(APP_DISPLAY_NAME))}  "
          f"{_dim('v' + VERSION)}{_dim('  installer')}")
    print(_dim(line))
    print()


def section(text: str) -> None:
    print(_bold(text))


def ok(text: str, detail: str = "") -> None:
    suffix = f"  {_dim(detail)}" if detail else ""
    print(f"  {_green('✓')} {text}{suffix}")


def warn(text: str, detail: str = "") -> None:
    suffix = f"  {_dim(detail)}" if detail else ""
    print(f"  {_yellow('⚠')} {text}{suffix}")


def info(text: str) -> None:
    print(f"    {_dim(text)}")


def err(text: str) -> None:
    print(f"  {_red('✗')} {text}", file=sys.stderr)


def die(text: str) -> "None":
    err(text)
    print()
    sys.exit(1)


def prompt_yes(text: str, assume_yes: bool) -> bool:
    """Y/n prompt that defaults to yes; non-interactive when
    `assume_yes` (--yes / IDLEGIT_INSTALL_ASSUME_YES) is set."""
    if assume_yes:
        info(f"{text}  (assuming yes, non-interactive)")
        return True
    try:
        ans = input(f"  {_yellow('?')} {text} [Y/n] ").strip().lower()
    except EOFError:
        return False
    return ans in ("", "y", "yes")


# ---------- Defaults ----------------------------------------------------


def _default_home() -> str:
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, "idlegit")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "idlegit")


def _default_bindir() -> str:
    """Prefer a Homebrew bindir on macOS so the launcher lands on
    PATH without dotfile munging; fall back to ~/.local/bin. On
    Windows there is no equivalent system-wide bindir convention,
    so go straight to ~/.local/bin (users on Windows should prefer
    `pipx install` — see README)."""
    if sys.platform != "win32":
        for candidate in ("/opt/homebrew/bin", "/usr/local/bin"):
            if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
                return candidate
    return os.path.join(os.path.expanduser("~"), ".local", "bin")


def _shell_rcfile() -> "tuple[Path | None, str]":
    """Return `(rcfile, hint)` for the current login shell. `hint`
    is set when we can't auto-edit (fish has its own command;
    unknown shells fall back to a printed export line). Windows
    has no Unix-style rc file to edit — we point the user at the
    pipx install flow instead."""
    if sys.platform == "win32":
        return None, "Windows detected — prefer `pipx install .` (see README)"
    shell = os.environ.get("SHELL", "")
    home = Path(os.path.expanduser("~"))
    if shell.endswith("zsh"):
        return home / ".zshrc", ""
    if shell.endswith("bash"):
        if sys.platform == "darwin":
            return home / ".bash_profile", ""
        return home / ".bashrc", ""
    if shell.endswith("fish"):
        return None, "fish detected — run: fish_add_path <bindir>"
    return None, "unknown shell — add the bindir to PATH manually"


# ---------- Pipeline ----------------------------------------------------


def parse_args(argv: "list[str]") -> bool:
    assume_yes = bool(os.environ.get("IDLEGIT_INSTALL_ASSUME_YES"))
    for arg in argv[1:]:
        if arg in ("-y", "--yes"):
            assume_yes = True
    return assume_yes


def check_repo_layout(summary: "list[str]") -> None:
    section("Repository layout")
    # (relpath-from-PROJECT_ROOT, kind). Tooling files live in
    # scripts/; everything else sits at the project root.
    required_files = [
        "idlegit", "idlegit.py", "idlegit.default.conf",
        "VERSION",
        "scripts/idlegit-update", "scripts/merge_config.py",
        "scripts/update.py",
    ]
    for name in required_files:
        if not (PROJECT_ROOT / name).is_file():
            die(f"missing \"{name}\" — run ./install from the "
                f"idlegit repo root (got PROJECT_ROOT={PROJECT_ROOT})")
    for pkg in ("core", "ui"):
        if not (PROJECT_ROOT / pkg).is_dir():
            die(f"missing {pkg}/ — run ./install from the idlegit repo root")
    ok("repo layout", str(PROJECT_ROOT))
    summary.append(f"Verified repo layout under {PROJECT_ROOT}")


def check_python(summary: "list[str]") -> None:
    section("Python toolchain")
    if sys.version_info[:2] < (3, 9):
        die(f"need Python 3.9 or newer (found: "
            f"Python {sys.version.split()[0]})")
    try:
        import curses  # noqa: F401
    except ImportError:
        die("the interpreter cannot import curses (required for the UI)")
    ok(f"Python {sys.version.split()[0]}",
       f"interpreter: {sys.executable}")
    summary.append(f"Python OK: Python {sys.version.split()[0]}")


def check_optional_tool(name: str, *, required: bool, hint_lines: list,
                        feature: str, assume_yes: bool,
                        summary: list) -> None:
    """Generic git / gh / git-lfs check. `required=True` for git
    (idlegit can't function without it); the others issue a warn +
    prompt when missing so a user without `gh` can still install."""
    path = shutil.which(name)
    if path:
        ok(name, f"→ {path}{('  (' + feature + ')') if feature else ''}")
        summary.append(f"{name} present")
        return
    label = "needed" if required else "optional"
    warn(f"{name} not on PATH",
         f"{label}{' — ' + feature if feature else ''}")
    for hint in hint_lines:
        info(hint)
    if not prompt_yes(f"Continue without {name}?", assume_yes):
        die("Aborted.")
    summary.append(
        f"{'Required' if required else 'Optional'}: {name} not installed")


def install_files(home: Path, summary: list) -> None:
    section(f"Install application → {home}")
    home.mkdir(parents=True, exist_ok=True)
    # Launchers + Python modules + the merge_config helper + the
    # VERSION sentinel that config.py reads at startup. Sources are
    # relative to PROJECT_ROOT (so `scripts/...` reaches into this
    # script's own folder); the install layout is flat — the basename
    # of `src_relpath` is what lands in `home`, unless dst_name is
    # set to override.
    # Tuple shape: (src_relpath, mode, optional_dst_name).
    files = [
        ("idlegit", 0o755, None),
        ("idlegit.default.conf", 0o644, None),
        ("idlegit.py", 0o644, None),
        ("VERSION", 0o644, None),
        ("scripts/idlegit-update", 0o755, None),
        ("scripts/update.py", 0o755, None),
        ("scripts/merge_config.py", 0o644, None),
    ]
    for src_relpath, mode, dst_name in files:
        src = PROJECT_ROOT / src_relpath
        dst = home / (dst_name or Path(src_relpath).name)
        shutil.copy2(src, dst)
        os.chmod(dst, mode)
    # Replace the core/ and ui/ trees wholesale so renames / removals
    # propagate. core/ is the domain layer (config + models + git_ops
    # + workers); ui/ is the curses surface.
    for pkg in ("core", "ui"):
        dst = home / pkg
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(PROJECT_ROOT / pkg, dst)
    ok("copied launcher + Python modules + ui/")
    summary.append(f"Installed / refreshed app files in {home}")

    # Strip any __pycache__ left over from prior runs in the source
    # tree (cp -R copies them through). They'll regenerate on first
    # launch.
    pyc_count = 0
    for d in home.rglob("__pycache__"):
        if d.is_dir():
            shutil.rmtree(d)
            pyc_count += 1
    if pyc_count:
        ok(f"stripped {pyc_count} __pycache__ dir(s)")
    else:
        ok("no __pycache__ to strip")
    summary.append("Stripped __pycache__ under install prefix")


def link_launcher(home: Path, bindir: Path, summary: list) -> Path:
    section(f"Link launchers → {bindir}")
    bindir.mkdir(parents=True, exist_ok=True)
    # `idlegit` is the primary launcher (returned for the summary
    # line at the bottom); `idlegit-update` rides along so the update
    # script lands on the user's PATH next to the app it updates.
    primary: "Path | None" = None
    for name in ("idlegit", "idlegit-update"):
        target = home / name
        link = bindir / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
        ok(f"symlink: {_dim(str(link))} → {_dim(str(target))}")
        summary.append(f"Symlink: {link}")
        if primary is None:
            primary = link
    assert primary is not None
    return primary


def merge_config(summary: list) -> None:
    section("Merge user config")
    merge_script = SCRIPTS_DIR / "merge_config.py"
    env = dict(os.environ)
    # PYTHONPATH points at PROJECT_ROOT so the helper can import
    # `config` (USER_STATE_DIR resolution) without us having to ship
    # a duplicate of the runtime path setup.
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    try:
        result = subprocess.run(
            [sys.executable, str(merge_script),
             str(PROJECT_ROOT / "idlegit.default.conf")],
            capture_output=True, text=True, env=env, check=False,
        )
    except OSError as e:
        die(f"config merge failed: {e}")
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        die("config merge failed")
    line = (result.stdout.strip() or result.stderr.strip()
            or "merge: nothing to merge")
    ok(line)
    summary.append(line)


def update_path(bindir: Path, assume_yes: bool, summary: list) -> None:
    section("PATH check")
    path_dirs = (os.environ.get("PATH") or "").split(os.pathsep)
    if str(bindir) in path_dirs:
        ok(f"PATH already includes {bindir}")
        summary.append(f"PATH already includes {bindir}")
        return
    warn(f"{bindir} is not on your PATH")
    rcfile, hint = _shell_rcfile()
    if rcfile is None:
        info(hint)
        info(f'export PATH="{bindir}:$PATH"')
        summary.append(f"Add {bindir} to PATH manually")
        return
    marker = "# added by idlegit installer"
    if rcfile.exists() and marker in rcfile.read_text():
        ok(f"{rcfile} already references the idlegit PATH entry")
        summary.append(f"{bindir} already added to {rcfile}")
        return
    if not prompt_yes(f"Append PATH update to {rcfile}?", assume_yes):
        info(f'add manually: export PATH="{bindir}:$PATH"')
        summary.append(f"Skipped PATH update; bindir = {bindir}")
        return
    with rcfile.open("a") as f:
        f.write(f"\n{marker}\nexport PATH=\"{bindir}:$PATH\"\n")
    ok(f"appended PATH update to {rcfile}")
    info(f"open a new terminal or run: source {rcfile}")
    summary.append(
        f"Added {bindir} to PATH in {rcfile} (open a new shell to activate)")


def print_summary(summary: list, home: Path, link: Path) -> None:
    print()
    print(f"{_green('✓')} {_bold('Done.')}")
    for line in summary:
        print(f"  {_dim('·')} {line}")
    print()
    print(f"  {_bold('Launcher:')}     {link}")
    print(f"  {_bold('App home:')}     {home}")
    rerun = f"{sys.executable} {home / 'merge_config.py'}"
    print(f"  {_bold('Re-merge:')}     {_dim(rerun)}")
    print()


def main(argv: "list[str] | None" = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    assume_yes = parse_args(argv)
    home = Path(os.environ.get("IDLEGIT_HOME") or _default_home())
    bindir = Path(os.environ.get("IDLEGIT_BINDIR") or _default_bindir())

    header()
    summary: "list[str]" = []
    check_repo_layout(summary)
    check_python(summary)
    check_optional_tool(
        "git", required=True,
        hint_lines=[
            "• macOS:  xcode-select --install   or   brew install git",
            "• Debian/Ubuntu:  sudo apt install git",
        ],
        feature="needed to use idlegit",
        assume_yes=assume_yes, summary=summary)
    check_optional_tool(
        "gh", required=False,
        hint_lines=[
            "• https://cli.github.com/   or   brew install gh",
        ],
        feature="GitHub Actions panel",
        assume_yes=assume_yes, summary=summary)
    check_optional_tool(
        "git-lfs", required=False,
        hint_lines=[
            "• https://git-lfs.com/   or   brew install git-lfs",
        ],
        feature="LFS review hints",
        assume_yes=assume_yes, summary=summary)
    print()
    install_files(home, summary)
    print()
    link = link_launcher(home, bindir, summary)
    print()
    merge_config(summary)
    print()
    update_path(bindir, assume_yes, summary)
    print_summary(summary, home, link)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
