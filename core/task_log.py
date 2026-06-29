"""Optional file logger for terminal task transitions.

Off by default. When `state.task_log_enabled` is on, every transition of
a task into a terminal status (ok / fail / warn) appends one line to the
configured path. `task_log_max_lines` caps the file's length; rotation
drops the oldest lines first so the most recent N stay visible.

The sink is wired from `idlegit.run()` onto `state.tasks.on_finished`
after the State + Config are loaded. Tasks fires the sink OUTSIDE its
own lock so a slow write (network mount, full disk) can't stall the
worker threads adding/updating rows.

Single-process assumption: idlegit runs as one process per user, so the
in-memory line-count cache here doesn't need cross-process coordination.
A second process writing to the same `tasks.log` would race; same hazard
as concurrent edits to `idlegit.conf` — out of scope."""
from __future__ import annotations

import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import user_state_dir
from .runtime.tasks import Task

# Default filename inside `user_state_dir()` when `task_log_path` in the
# conf is left empty. Kept here (not in config.py) so `resolve_task_log_path`
# can read it without pulling extra imports into the config module.
_DEFAULT_LOG_FILENAME = "tasks.log"

# Cached line count so rotation doesn't re-scan the file on every write.
# Invalidated whenever `_last_path` drifts (user edited task_log_path in
# the conf and reloaded) or rotation just truncated the file.
_log_lock = threading.Lock()
_line_count: int = -1
_last_path: Optional[Path] = None


def default_task_log_path() -> Path:
    """Path used when `task_log_path` in idlegit.conf is left empty.
    Falls under `user_state_dir()` so the log sits beside `idlegit.conf`
    and survives `pipx upgrade` / reinstalls."""
    return user_state_dir() / _DEFAULT_LOG_FILENAME


def resolve_task_log_path(raw: str) -> Path:
    """Resolve a raw conf string to an absolute Path. Empty / whitespace-
    only string → `default_task_log_path()`. A `~` prefix is expanded.
    A relative path is anchored at `user_state_dir()` so it doesn't
    accidentally land in whatever cwd idlegit happened to launch from."""
    s = (raw or "").strip()
    if not s:
        return default_task_log_path()
    p = Path(s).expanduser()
    if not p.is_absolute():
        p = user_state_dir() / p
    return p


def _format_timestamp(epoch: Optional[float] = None) -> str:
    """`YYYY-MM-DD HH:MM:SS` from a wall-clock epoch. Uses local time so
    the log reads naturally in the user's timezone — same convention as
    the sidebar's relative-time tags would resolve to."""
    if epoch is None:
        epoch = time.time()
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def format_task_line(task: Task) -> str:
    """Single log line: timestamp, padded status, label, optional message.

    Format:
      `2026-05-15 14:23:45  ok    smart-sync (3)`
      `2026-05-15 14:23:48  fail  ↳ align repo: push — non-fast-forward`

    Status column is 5 chars wide (the longest of `ok/fail/warn`) so labels
    line up. Trailing `\\n` belongs to the writer, not the formatter."""
    ts = _format_timestamp()
    status = (task.status or "?").ljust(5)
    label = (task.label or "").strip()
    msg = (task.message or "").strip()
    line = f"{ts}  {status}  {label}"
    if msg:
        line += f" — {msg}"
    return line


def _count_lines_on_disk(path: Path) -> int:
    """Count newline-terminated lines in `path`. Returns 0 when the file
    is missing or unreadable — we never raise here; the caller treats the
    log as best-effort and any error just leaves the cache invalidated."""
    try:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _ensure_count_loaded(path: Path) -> None:
    """Refresh `_line_count` if the cache is stale (uninitialised, or the
    configured path changed since the last write). Caller holds `_log_lock`."""
    global _line_count, _last_path
    if _last_path != path or _line_count < 0:
        _line_count = _count_lines_on_disk(path)
        _last_path = path


def _rotate_to_max_lines(path: Path, max_lines: int) -> int:
    """Trim `path` to its last `max_lines` lines. Reads the whole file,
    keeps the tail, atomically replaces. Returns the new line count.

    Atomic replace via `os.replace` (Path.replace under the hood) so a
    crash mid-rotation can't leave the log half-written — the user sees
    either the pre-rotation file or the post-rotation file, never a
    truncated middle. Caller holds `_log_lock`."""
    try:
        with open(path, "rb") as fh:
            lines = fh.readlines()
    except OSError:
        return 0
    if len(lines) <= max_lines:
        return len(lines)
    keep = lines[-max_lines:]
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.writelines(keep)
        tmp.replace(path)
    except OSError:
        # Best-effort: leave the file as-is, swallow. The cache will
        # re-sync on the next write via `_count_lines_on_disk`.
        try:
            tmp.unlink()
        except OSError:
            pass
        return _count_lines_on_disk(path)
    return len(keep)


def log_task_event(path: Path, max_lines: int, task: Task) -> bool:
    """Append `task`'s terminal-transition line to `path`. Rotates when
    the file passes `max_lines` (non-positive `max_lines` disables the
    cap). Creates the parent directory + the file on first write.

    Returns True iff the write succeeded. Failures are silent — the
    task panel is the source of truth and the log is best-effort; we
    don't want a logging hiccup to surface as a worker exception."""
    global _line_count, _last_path
    line = format_task_line(task) + "\n"
    with _log_lock:
        # Load the disk count BEFORE the append. If we deferred it, the
        # cache would re-read post-append (count == N+1) and we'd then
        # increment again — bug surfaced as "task_log_line_count == 8
        # after 7 writes" in the test fixture.
        _ensure_count_loaded(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            _line_count = -1
            return False
        _line_count += 1
        if max_lines > 0 and _line_count > max_lines:
            _line_count = _rotate_to_max_lines(path, max_lines)
        _last_path = path
        return True


def clear_task_log(path: Path) -> bool:
    """Truncate `path` to empty. Returns True iff successful (a missing
    file counts as already-cleared and returns True)."""
    global _line_count, _last_path
    with _log_lock:
        try:
            if path.exists():
                with open(path, "w", encoding="utf-8"):
                    pass
            _line_count = 0
            _last_path = path
            return True
        except OSError:
            _line_count = -1
            return False


def task_log_size_bytes(path: Path) -> int:
    """Bytes-on-disk for the log file, or 0 if missing / unreadable."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def task_log_line_count(path: Path) -> int:
    """Current line count, refreshing the cache on demand. Used by the
    app menu's size row so it doesn't have to re-scan a large file on
    every redraw — we only re-read when the cache is invalidated."""
    with _log_lock:
        _ensure_count_loaded(path)
        return _line_count


def format_size(n: int) -> str:
    """Human-friendly byte count: `0 B`, `512 B`, `1.2 KB`, `3.4 MB`.
    Capped at GB since a task log should never plausibly exceed that."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def open_task_log(path: Path) -> bool:
    """Open `path` in the user's default file handler via stdlib
    `webbrowser` (same mechanism `_open_in_browser` uses for URLs:
    `open` on macOS, `xdg-open`/`gio` on Linux, `start` on Windows).
    Returns True iff the OS reported it dispatched successfully."""
    try:
        return webbrowser.open(path.as_uri(), new=2)
    except Exception:  # noqa: BLE001 — any opener failure is non-fatal
        return False


def wire_task_log(state) -> None:
    """Attach the task-log writer to `state.tasks.on_finished`. Reads
    `state.task_log_path` + `state.task_log_max_lines` at call time via
    the lambda's closure, so subsequent path/cap changes on State are
    picked up without re-wiring. Also touches the log file so subsequent
    `Open log file` clicks land on something real, rather than reporting
    'does not exist yet' until the first terminal task fires."""
    state.tasks.on_finished = lambda task: log_task_event(
        state.task_log_path, state.task_log_max_lines, task)
    try:
        state.task_log_path.parent.mkdir(parents=True, exist_ok=True)
        state.task_log_path.touch(exist_ok=True)
    except OSError:
        # Path may live on a read-only mount or a missing volume —
        # treat the same as any other logger failure (best-effort).
        pass


def unwire_task_log(state) -> None:
    """Detach the sink. The file is intentionally left in place so
    historical entries stay readable after the user disables logging."""
    state.tasks.on_finished = None


def _reset_cache_for_tests() -> None:
    """Drop the cached line count + path. Tests that exercise multiple
    log files in the same process call this between fixtures so a stale
    cache from a previous test doesn't bleed into the next."""
    global _line_count, _last_path
    with _log_lock:
        _line_count = -1
        _last_path = None
