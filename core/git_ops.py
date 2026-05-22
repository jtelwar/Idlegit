"""Everything that talks to git: subprocess wrappers, discovery,
synchronization, suggestion, LFS, working-tree state queries. No curses
imports here — these functions all run on background threads (or
synchronously at startup) and never touch the screen."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import (
    ChildRef, CommitEntry, FileChange, FileEntry, LFSCandidate, Repo,
    SubtreeSpec, TargetState,
)


def _find_embedded_gitlinks(base: Path,
                            repo_path: Path) -> List[str]:
    """Walk `base` looking for directories that contain a `.git` (dir
    or file — submodule gitfiles count). Returns each finding's path
    relative to `repo_path`, with forward slashes and no trailing
    slash. Does NOT recurse into found nested repos.

    Used by `safe_stage_all` to find embedded gitlinks under untracked
    directories (where `git status --porcelain` reports the *parent*
    untracked dir, not the deeper path containing the `.git`)."""
    found: List[str] = []
    if not base.is_dir():
        return found
    for root, dirs, files in os.walk(base):
        if ".git" in dirs or ".git" in files:
            rel = Path(root).relative_to(repo_path).as_posix()
            found.append(rel)
            # Don't recurse into the embedded repo's contents.
            dirs[:] = [d for d in dirs if d != ".git"]
            continue
    return found

# git status XY codes that indicate an unmerged path.
CONFLICT_CODES = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


def _iter_porcelain_z_entries(out: str):
    """Yield `(xy, path)` tuples from the output of
    `git status --porcelain=v1 -z`, correctly skipping the
    source-path follow-up token that `R` (rename) and `C` (copy)
    entries emit.

    `-z` mode encodes a rename as TWO NUL-separated tokens —
    `R<flag> destpath` followed by `srcpath` — and a naive
    `out.split("\\x00")` loop reads `srcpath` as a third row. That
    row has no `XY ` prefix, so a downstream `entry[3:]` slice would
    chop the first three characters of the source path (`idlegit.conf`
    → `egit.conf`) and pass the corrupt path to `git add`, producing
    `fatal: pathspec 'egit.conf' did not match any files`. Centralising
    the parser here keeps every status reader consistent — only one
    place to update if the porcelain format ever changes again."""
    parts = out.split("\x00")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 3:
            continue
        xy = entry[:2]
        path = entry[3:]
        # R/C entries carry an extra source-path token under -z;
        # consume it here so callers don't have to.
        if xy[0] in ("R", "C") or xy[1] in ("R", "C"):
            if i < len(parts):
                i += 1
        yield xy, path

# .git/<marker> files/dirs that mean a merge-like operation is in progress.
MERGE_MARKER_FILES = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
MERGE_MARKER_DIRS = ("rebase-merge", "rebase-apply")

DEFAULT_GIT_TIMEOUT_SECONDS = 120
DEFAULT_GH_TIMEOUT_SECONDS = 60
MAX_PARALLEL_GIT_JOBS = max(4, min(16, (os.cpu_count() or 4) * 2))


# ---------- Subprocess + small helpers -------------------------------------


def _git_env() -> dict:
    env = os.environ.copy()
    # Background workers cannot answer credential prompts. Failing fast
    # keeps the TUI from hanging behind a hidden git prompt.
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "Never")
    return env


def git(path: Path, args: List[str],
        timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS) -> Tuple[int, str, str]:
    """Run git with `cwd=path`. Resilient to a missing/inaccessible cwd
    (returns rc=1 with the OSError message instead of raising) so the
    caller can treat it as any other git failure — important when a repo
    folder is removed under us between refreshes. Calls are bounded and
    non-interactive so background workers can't hang forever behind a
    credential prompt, hook, or stalled remote."""
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_git_env(),
        )
    except OSError as e:
        return 1, "", str(e)
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", f"git timed out after {timeout:g}s"
    return p.returncode, p.stdout, p.stderr


def git_cancellable(
        path: Path, args: List[str],
        cancel_event: "Optional[object]" = None,
        timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
        poll_interval: float = 0.25) -> Tuple[int, str, str]:
    """Like `git()` but polls `cancel_event` (a `threading.Event`) while
    waiting on the subprocess so a user-initiated Cancel can terminate
    a long-running git operation (push, pull, fetch). Returns the same
    `(rc, stdout, stderr)` tuple; on cancel, `rc == 130` (the standard
    "terminated by signal" code) and stderr names the action.

    Uses `Popen` directly so we hold the handle for termination. Falls
    back to plain `git()` behaviour when `cancel_event` is None — no
    subprocess overhead for paths that don't need cancellation."""
    if cancel_event is None:
        return git(path, args, timeout=timeout)
    try:
        proc = subprocess.Popen(
            ["git", *args],
            cwd=str(path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_git_env(),
        )
    except OSError as e:
        return 1, "", str(e)
    deadline = time.monotonic() + timeout
    while True:
        try:
            rc = proc.wait(timeout=poll_interval)
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel_event.is_set():
            # Terminate first (SIGTERM), then kill if it doesn't yield.
            # Either way, drain pipes via communicate() so the buffers
            # don't deadlock.
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
            except OSError:
                pass
            try:
                _, err = proc.communicate(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                err = ""
            return 130, "", err or "cancelled"
        if time.monotonic() > deadline:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                _, err = proc.communicate(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                err = ""
            return 124, "", err or f"git timed out after {timeout:g}s"
    try:
        stdout = proc.stdout.read() if proc.stdout is not None else ""
        stderr = proc.stderr.read() if proc.stderr is not None else ""
    except OSError:
        stdout, stderr = "", ""
    return rc, stdout, stderr


def git_bounded_output(path: Path, args: List[str],
                       max_bytes: int,
                       timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS
                       ) -> Tuple[int, str, str, bool]:
    """Run git and capture at most `max_bytes` of stdout/stderr combined.

    Used by viewer-style code where the full command output can be very
    large. Returns (rc, out, err, truncated)."""
    try:
        p = subprocess.Popen(
            ["git", *args],
            cwd=str(path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_env(),
        )
        try:
            out_b, err_b = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out_b, err_b = p.communicate()
            return 124, "", f"git timed out after {timeout:g}s", False
    except OSError as e:
        return 1, "", str(e), False

    combined_len = len(out_b) + len(err_b)
    truncated = combined_len > max_bytes
    if truncated:
        remaining = max_bytes
        out_b = out_b[:remaining]
        remaining -= len(out_b)
        err_b = err_b[:max(0, remaining)]
    out = out_b.decode("utf-8", errors="replace")
    err = err_b.decode("utf-8", errors="replace")
    return p.returncode, out, err, truncated


def is_safe_ref_arg(ref: str) -> bool:
    """True when `ref` is safe to pass as a git/gh positional ref.

    argv-list subprocesses avoid shell injection, but git/gh can still
    parse leading-dash refs as options in many positions. Refuse those
    instead of trying to disambiguate every command's grammar."""
    return bool(ref) and not ref.startswith("-")


def first_line(text: str) -> str:
    """First non-blank line of `text`, stripped. Returns the sentinel
    `"(no output)"` when input is empty or whitespace-only — task-row
    messages prefer that to a blank cell when surfacing git's stderr."""
    if not text:
        return "(no output)"
    for ln in text.strip().splitlines():
        if ln.strip():
            return ln.strip()
    return "(no output)"


def canonicalize_url(url: str) -> str:
    """Normalize a git remote URL so HTTPS / SSH / trailing-slash variants match."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):
        url = url[4:].replace(":", "/", 1)
    elif "://" in url:
        url = url.split("://", 1)[1]
        if "@" in url.split("/", 1)[0]:
            host_and_path = url.split("/", 1)
            host = host_and_path[0].rsplit("@", 1)[-1]
            url = host + "/" + host_and_path[1] if len(host_and_path) > 1 else host
    return url.lower()


# ---------- Repo refresh (populates a Repo from git state) -----------------


def refresh_repo(repo: Repo) -> None:
    """Re-query every cached field on a Repo from its working tree."""
    repo.branch = ""
    repo.head = ""
    repo.upstream = None
    repo.remote_url = None
    repo.remote_url_raw = None
    repo.ahead = 0
    repo.behind = 0
    repo.staged = []
    repo.unstaged = []
    repo.untracked = []
    # Do NOT clear nested_subs here — link_siblings reads it to build
    # children rows. A concurrent refresh (fs_watcher while Ctrl+R skips
    # a locked repo) used to leave nested_subs empty mid-flight and
    # link_siblings would drop every submodule under that parent.
    # siblings + children are filled by link_siblings() after refresh.
    repo.error = ""
    repo.merging = False
    repo.conflict_paths = []
    # GitHub Actions workflows discovered locally — populated near the end
    # so it's reset even on error paths above.
    repo.workflows = discover_workflows_local(repo.path)

    rc, out, err = git(repo.path, ["rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        repo.error = "not a git work tree"
        return

    rc, out, _ = git(repo.path, ["branch", "--show-current"])
    repo.branch = out.strip() or "(detached)"

    rc, out, _ = git(repo.path, ["rev-parse", "HEAD"])
    if rc == 0:
        repo.head = out.strip()

    rc, out, _ = git(repo.path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    repo.upstream = out.strip() if rc == 0 and out.strip() else None

    rc, out, _ = git(repo.path, ["remote", "get-url", "origin"])
    if rc == 0 and out.strip():
        repo.remote_url_raw = out.strip()
        repo.remote_url = canonicalize_url(out.strip())

    if repo.upstream:
        rc, out, _ = git(repo.path, [
            "rev-list", "--count", "--left-right",
            f"{repo.upstream}...HEAD",
        ])
        if rc == 0:
            parts = out.split()
            if len(parts) == 2:
                try:
                    repo.behind = int(parts[0])
                    repo.ahead = int(parts[1])
                except ValueError:
                    pass

    rc, out, err = git(repo.path, ["status", "--porcelain=v1", "-z"])
    if rc != 0:
        repo.error = (err or "git status failed").strip().splitlines()[0]
        return
    for xy, p in _iter_porcelain_z_entries(out):
        if xy == "??":
            repo.untracked.append(p)
            continue
        if xy in CONFLICT_CODES:
            repo.merging = True
            repo.conflict_paths.append(p)
            continue
        x, y = xy[0], xy[1]
        if x != " ":
            repo.staged.append((x, p))
        if y != " ":
            repo.unstaged.append((y, p))

    # Detect mid-merge / mid-rebase / mid-cherry-pick / mid-revert via .git markers.
    rc, out, _ = git(repo.path, ["rev-parse", "--git-dir"])
    if rc == 0 and out.strip():
        git_dir = Path(out.strip())
        if not git_dir.is_absolute():
            git_dir = (repo.path / git_dir).resolve()
        if not repo.merging:
            for marker in MERGE_MARKER_FILES:
                if (git_dir / marker).exists():
                    repo.merging = True
                    break
        if not repo.merging:
            for marker in MERGE_MARKER_DIRS:
                if (git_dir / marker).is_dir():
                    repo.merging = True
                    break

    new_nested_subs: List[Tuple[str, Path]] = []
    if (repo.path / ".gitmodules").exists():
        rc, out, _ = git(repo.path, [
            "config", "-f", ".gitmodules",
            "--get-regexp", r"submodule\..+\.path",
        ])
        if rc == 0:
            for line in out.strip().splitlines():
                parts = line.split(maxsplit=1)
                if len(parts) != 2:
                    continue
                key, path_str = parts
                if not key.startswith("submodule.") or not key.endswith(".path"):
                    continue
                name = key[len("submodule."):-len(".path")]
                rc2, url_out, _ = git(repo.path, [
                    "config", "-f", ".gitmodules",
                    f"submodule.{name}.url",
                ])
                if rc2 != 0 or not url_out.strip():
                    continue
                sub_path = (repo.path / path_str.strip()).resolve()
                new_nested_subs.append(
                    (canonicalize_url(url_out.strip()), sub_path))
    repo.nested_subs = new_nested_subs

    # Nothing staged/untracked to commit — drop any queued message so the
    # UI doesn't keep showing an orphaned draft after refresh (Ctrl+R,
    # post-commit refresh, etc.). Skip while merge machinery is active —
    # the tree can look oddly quiet mid-merge without losing the need for
    # a eventual commit message.
    if not repo.error and not repo.is_dirty and not repo.merging:
        repo.message = ""


# ---------- Discovery + linkage --------------------------------------------


def discover_repos(workspace: Path) -> List[Repo]:
    """Return the workspace itself (if it's a git repo) plus every immediate
    child folder containing .git, sorted alphabetically. The folder this
    script lives in is included if (and only if) it's also a git repo —
    handy for managing idlegit's own checkout from idlegit itself.

    Hidden directories (dotfolders like `.github`, `.dotfiles`) are NOT
    excluded — GitHub treats `<owner>/.github` as a real repo (it hosts
    the org-/user-level README) and there's no reason to hide it from
    the workspace view. The membership criterion is "directory contains
    a `.git`", same as for non-hidden children. The workspace's own
    `.git` directory doesn't satisfy this (no nested `.git`), so it's
    safely skipped without needing a name filter."""
    repos: List[Repo] = []
    try:
        if (workspace / ".git").exists():
            repos.append(Repo(rel=".", path=workspace.resolve()))
    except OSError:
        return repos
    try:
        children = sorted(workspace.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return repos
    for child in children:
        try:
            if not child.is_dir():
                continue
            if (child / ".git").exists():
                repos.append(Repo(rel=child.name, path=child.resolve()))
        except OSError:
            continue
    return repos


# Serializes link_siblings calls so two supervisors finishing at the
# same time can't interleave. Without this, both calls would clear
# `r.children` and `r.siblings` at the start, then race-append the same
# ChildRefs into the same lists — producing duplicated submodule rows
# under the affected parents (the symptom: pushing on a canonical sub
# kicked off auto-sync, manual smart-sync ran on top of it, the row
# list now showed each child twice). Even with the atomic-swap pattern
# below, the lock is cheap insurance against two callers wasting the
# same `git` queries to compute the same answer.
_link_siblings_lock = threading.Lock()


def link_siblings(repos: List[Repo],
                  subtrees: Optional[List[SubtreeSpec]] = None) -> None:
    """For each tracked repo X, find every other tracked repo Y that
    contains X — either as a nested submodule (auto-detected from
    .gitmodules) or as a subtree (declared in idlegit.conf). Records:
        - X.siblings — list of (Y, nested_path) for submodule sync-after-push
        - Y.children — ChildRef entries (kind="submodule"/"subtree") for the
          indented rows below Y on the main screen
    The workspace root is skipped for submodule auto-discovery (its
    submodules are already top-level rows); subtrees are honored regardless.

    Concurrency: the function takes `_link_siblings_lock` so calls from
    different supervisor threads serialize. It also builds `children`
    and `siblings` in local dicts and atomically assigns them at the
    very end — the older "reset r.children = [], then append" pattern
    was race-prone (a second caller mid-flight could clear what the
    first had just appended, then both would interleave-append the same
    refs, producing duplicates)."""
    with _link_siblings_lock:
        _link_siblings_locked(repos, subtrees)


def _link_siblings_locked(repos: List[Repo],
                          subtrees: Optional[List[SubtreeSpec]]) -> None:
    url_to_repo = {r.remote_url: r for r in repos if r.remote_url}
    rel_to_repo = {r.rel: r for r in repos}

    # Build into local dicts; assign onto each Repo only at the end so
    # that another thread reading r.children mid-execution (during a
    # render, say) sees either the old snapshot or the new one — never
    # a partially-cleared list. Keyed by `id(repo)` because the Repo
    # dataclass has value-based `__eq__` and is therefore unhashable.
    new_children: Dict[int, List[ChildRef]] = {id(r): [] for r in repos}
    new_siblings: Dict[int, List[Tuple[Repo, Path]]] = {id(r): [] for r in repos}

    # Synthetic canonicals for submodule URLs that don't match any
    # tracked top-level repo (e.g. a submodule used by parents only,
    # never cloned standalone). Multiple parents pointing at the same
    # URL share one synthetic Repo so the user still sees both rows + a
    # working action menu, and we have a place to record drift between
    # them. Synthetic Repos are NOT added to `repos` / state.repos.
    synthetic_by_url: Dict[str, Repo] = {}

    def _make_synthetic(url: str, sub_path: Path) -> Repo:
        # Prefer the on-disk basename for the display name (preserves
        # original casing, e.g. "Upskill.Health.Unity"); fall back to a
        # URL-slug parse if for some reason the path is empty.
        name = sub_path.name
        if not name and url:
            m = re.search(r"[/:]([^/:]+?)(?:\.git)?$", url)
            if m:
                name = m.group(1)
        if not name:
            name = "submodule"
        synth = Repo(rel=name, path=sub_path, synthetic=True)
        # Synthetic Repos are created here, so they need their own
        # children/siblings buckets in the local dicts too — anything
        # treating them as a real Repo (sibling lookups, drift detect)
        # expects these to exist.
        new_children[id(synth)] = []
        new_siblings[id(synth)] = []
        return synth

    # Submodule references — discovered from each parent's .gitmodules.
    # Dedup at this stage too (paranoia + belt-and-braces): if a stale
    # `.gitmodules` somehow has the same submodule listed twice (same
    # URL + same path), only one ChildRef is produced.
    prev_submodule_msg: Dict[Tuple[int, str], str] = {}
    for parent in repos:
        for old in parent.children:
            if old.kind == "submodule":
                prev_submodule_msg[
                    (id(parent), str(old.nested_path.resolve()))
                ] = old.message

    submodule_refs: List[ChildRef] = []
    for parent in repos:
        if parent.rel == ".":
            continue
        seen_for_parent: set = set()
        for url, sub_path in parent.nested_subs:
            key = (url, sub_path)
            if key in seen_for_parent:
                continue
            seen_for_parent.add(key)
            target = url_to_repo.get(url)
            if target is None:
                if url:
                    target = synthetic_by_url.get(url)
                    if target is None:
                        target = _make_synthetic(url, sub_path)
                        synthetic_by_url[url] = target
                else:
                    target = _make_synthetic("", sub_path)
            if target is parent:
                continue
            new_siblings[id(target)].append((parent, sub_path))
            nk = (id(parent), str(sub_path.resolve()))
            prev_msg = prev_submodule_msg.get(nk, "")
            ref = ChildRef(
                repo=target, nested_path=sub_path, kind="submodule",
                message=prev_msg)
            new_children[id(parent)].append(ref)
            submodule_refs.append(ref)

    # Populate per-child state (HEAD, branch, dirty + the same
    # ahead/behind/upstream/merging fields a top-level Repo carries) in
    # parallel. The result is enough state for `child_state_color` to
    # paint the row's main dot with the same precedence as a top-level
    # Repo (dirty / diverged / behind / ahead / no-upstream / clean).
    def _populate(ref: ChildRef) -> None:
        rc, out, _ = git(ref.nested_path, ["rev-parse", "HEAD"])
        if rc == 0:
            ref.head = out.strip()
            if ref.repo.head:
                ref.in_sync = ref.head == ref.repo.head
            else:
                # Synthetic canonical — drift gets reconciled in the
                # post-pass below; default to in-sync until we know
                # there are 2+ checkouts to compare.
                ref.in_sync = True
        else:
            ref.in_sync = False
        rc, out, _ = git(ref.nested_path, ["branch", "--show-current"])
        ref.branch = (out.strip() or "(detached)") if rc == 0 else ""
        rc, out, _ = git(ref.nested_path, ["status", "--porcelain=v1"])
        ref.dirty = rc == 0 and bool(out.strip())

        rc, out, _ = git(ref.nested_path, [
            "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        ref.upstream = out.strip() if rc == 0 and out.strip() else None
        ref.ahead = 0
        ref.behind = 0
        if ref.upstream:
            rc, out, _ = git(ref.nested_path, [
                "rev-list", "--count", "--left-right",
                f"{ref.upstream}...HEAD",
            ])
            if rc == 0:
                parts = out.split()
                if len(parts) == 2:
                    try:
                        ref.behind = int(parts[0])
                        ref.ahead = int(parts[1])
                    except ValueError:
                        pass

        # mid-merge / mid-rebase markers in the nested checkout's .git
        ref.merging = False
        rc, out, _ = git(ref.nested_path, ["rev-parse", "--git-dir"])
        if rc == 0 and out.strip():
            git_dir = Path(out.strip())
            if not git_dir.is_absolute():
                git_dir = (ref.nested_path / git_dir).resolve()
            for marker in MERGE_MARKER_FILES:
                if (git_dir / marker).exists():
                    ref.merging = True
                    break
            if not ref.merging:
                for marker in MERGE_MARKER_DIRS:
                    if (git_dir / marker).is_dir():
                        ref.merging = True
                        break

    if submodule_refs:
        max_workers = min(len(submodule_refs), MAX_PARALLEL_GIT_JOBS)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(_populate, submodule_refs))

    for ref in submodule_refs:
        if not ref.dirty and not ref.merging:
            ref.message = ""

    # Drift detection for shared synthetic canonicals: when multiple
    # parents reference the same untracked submodule URL, treat the
    # most-common HEAD across their checkouts as the "canonical" and
    # flag laggards as out-of-sync (so they show a behind-coloured dot).
    # Reads from the LOCAL `new_children` so it sees the in-progress
    # state, not whatever was on `parent.children` from a previous call.
    for synthetic in synthetic_by_url.values():
        sib_refs = [ref for parent in repos
                    for ref in new_children[id(parent)]
                    if ref.repo is synthetic]
        if len(sib_refs) < 2:
            continue
        head_counts: Dict[str, int] = {}
        for r in sib_refs:
            if r.head:
                head_counts[r.head] = head_counts.get(r.head, 0) + 1
        if not head_counts:
            continue
        chosen = max(head_counts, key=head_counts.get)
        synthetic.head = chosen
        for r in sib_refs:
            r.in_sync = bool(r.head) and r.head == chosen

    # Subtree references — declared in idlegit.conf.
    for spec in subtrees or []:
        parent = rel_to_repo.get(spec.parent)
        source = rel_to_repo.get(spec.source)
        if parent is None or source is None or parent is source:
            continue
        nested_path = (parent.path / spec.prefix).resolve()
        ref = ChildRef(repo=source, nested_path=nested_path, kind="subtree")
        # No cheap drift signal for subtrees; leave in_sync at default True.
        new_children[id(parent)].append(ref)

    for child_list in new_children.values():
        child_list.sort(
            key=lambda c: (c.kind, c.repo.display_name.lower()))

    # Atomic swap. After this point any reader of `r.children` /
    # `r.siblings` sees the new snapshot in full. Synthetic Repos
    # have entries in the dicts too but aren't in `repos`, so they
    # only get touched if some caller has a direct reference.
    for r in repos:
        r.children = new_children[id(r)]
        r.siblings = new_siblings[id(r)]
    for synth in synthetic_by_url.values():
        synth.siblings = new_siblings[id(synth)]
        # children stays empty for a synthetic — they're leaf nodes.


# ---------- Sync helpers ---------------------------------------------------


def working_tree_signature(repo_path: Path) -> Tuple[Tuple[str, str], ...]:
    """Stable signature of every working-tree file that differs from HEAD
    or is untracked-not-ignored. Each entry is `(path, blob_hash)`; blob
    hash is the empty string for deletions. The signature is sorted so
    two checkouts with identical pending changes produce equal tuples,
    which lets the smart sync detect duplicate working trees that agents
    have produced across nested checkouts of the same submodule."""
    seen: dict = {}

    rc, out, _ = git(repo_path, ["diff", "HEAD", "--name-only", "-z"])
    if rc != 0:
        # Likely a fresh repo with no HEAD yet; fall through to untracked
        # so we still notice spurious files. An empty signature here just
        # excludes this checkout from dedup, which is the safe default.
        out = ""
    for p in out.split("\x00"):
        if not p or p in seen:
            continue
        full = repo_path / p
        if full.is_file():
            rc2, h, _ = git(repo_path, ["hash-object", "--", str(full)])
            seen[p] = h.strip() if rc2 == 0 else ""
        else:
            seen[p] = ""  # deletion (or directory replaced by symlink, etc.)

    rc, out, _ = git(repo_path, [
        "ls-files", "--others", "--exclude-standard", "-z",
    ])
    if rc == 0:
        for p in out.split("\x00"):
            if not p or p in seen:
                continue
            full = repo_path / p
            if full.is_file():
                rc2, h, _ = git(repo_path, ["hash-object", "--", str(full)])
                seen[p] = h.strip() if rc2 == 0 else ""

    return tuple(sorted(seen.items()))


def signature_mtime(repo_path: Path,
                    signature: Tuple[Tuple[str, str], ...]) -> float:
    """Latest mtime among files in the signature, used to pick the
    'most-recently-touched' winner among duplicate working trees. Missing
    or stat-failing files contribute nothing (don't drag the result down)."""
    latest = 0.0
    for p, _ in signature:
        try:
            mtime = (repo_path / p).stat().st_mtime
        except OSError:
            continue
        if mtime > latest:
            latest = mtime
    return latest


def sync_sibling(sibling_path: Path, branch: str) -> Tuple[bool, str]:
    """Fetch + checkout origin/<branch> in a sibling's nested submodule
    checkout so it lines up with what we just pushed.

    SAFETY (cardinal rule): refuses if the sibling's HEAD has commits
    that aren't already on origin/<branch>. Plain `git checkout
    origin/<branch>` from a detached HEAD with unique commits prints a
    stderr warning + returns 0, which means the unique commits get
    orphaned and any files unique to those commits vanish from the
    working tree. The user resolves manually (e.g. by branching from
    HEAD before re-running the sync)."""
    if not is_safe_ref_arg(branch):
        return False, f"unsafe branch name: {branch or '(empty)'}"
    rc, _, err = git(sibling_path, ["fetch", "origin"])
    if rc != 0:
        return False, f"fetch failed: {first_line(err)}"
    target_ref = f"origin/{branch}"
    rc, _, _ = git(sibling_path, [
        "merge-base", "--is-ancestor", "HEAD", target_ref,
    ])
    if rc != 0:
        return False, (f"HEAD has commits not on {target_ref} "
                       "— would orphan them; manual: `git checkout -b "
                       "<name>` to keep them, then re-run sync")
    rc, _, err = git(sibling_path, ["checkout", target_ref])
    if rc != 0:
        return False, f"checkout failed: {first_line(err)}"
    return True, "synced"


# ---------- Safe staging (replacement for `git add -A`) -------------------


def list_registered_submodule_paths(repo_path: Path) -> "set[str]":
    """Set of paths that are registered submodules in `repo_path`'s
    `.gitmodules`. Empty when there's no `.gitmodules` or the file can't
    be parsed. Used by `safe_stage_all` to refuse staging changes that
    would destroy a submodule pointer."""
    if not (repo_path / ".gitmodules").exists():
        return set()
    rc, out, _ = git(repo_path, [
        "config", "-f", ".gitmodules",
        "--get-regexp", r"submodule\..+\.path",
    ])
    if rc != 0:
        return set()
    paths: set = set()
    for line in out.strip().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            paths.add(parts[1].strip())
    return paths


def has_only_submodule_pointer_changes(repo_path: Path) -> bool:
    """True iff every change in `repo_path`'s working tree is a
    modification to a registered submodule's gitlink. False when the
    tree is clean (nothing to propagate), when any non-submodule path is
    dirty, or when any submodule path shows up as a deletion / addition
    / untracked entry (those need human attention — staging them would
    rewrite or destroy the gitlink).

    Smart-sync uses this as a precondition before auto-bumping a
    parent's submodule pointer after the canonical sync: if the parent
    has unrelated work in progress, propagation backs off and leaves
    the user to commit on their own."""
    submodule_paths = list_registered_submodule_paths(repo_path)
    if not submodule_paths:
        return False
    rc, out, _ = git(repo_path, ["status", "--porcelain=v1", "-z"])
    if rc != 0:
        return False
    parts = out.split("\x00")
    saw_pointer_change = False
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 3:
            continue
        xy = entry[:2]
        path_str = entry[3:]
        # `-z` emits the rename source as a separate NUL-chunk after
        # the status row — skip it so we don't reparse it as a status.
        if xy[0] in ("R", "C") or xy[1] in ("R", "C"):
            i += 1
        if xy == "??":
            # Any untracked path means there's non-submodule dirt to
            # worry about; bail without claiming propagation.
            return False
        if path_str not in submodule_paths:
            return False
        # The submodule path must be a pure modification ("M" only —
        # not addition / deletion / typechange / conflict / etc.).
        # Anything else should be human-resolved; auto-committing
        # could record a brand-new gitlink, unregister the submodule,
        # or land mid-merge state.
        x, y = xy[0], xy[1]
        if x not in (" ", "M") or y not in (" ", "M"):
            return False
        if x == " " and y == " ":
            return False
        saw_pointer_change = True
    return saw_pointer_change


def safe_stage_all(repo_path: Path) -> Tuple[bool, str]:
    """Stage every change in `repo_path` the way `git add -A` would,
    except REFUSE outright when doing so would commit one of these two
    classes of damage (the cardinal-rule failure modes that have already
    cost this user real files):

      1. **Submodule-pointer deletion.** A `D` entry on a path
         registered in `.gitmodules` — happens when the submodule's
         working directory is empty (e.g. after `git submodule deinit`)
         and `git add -A` then stages the gitlink's removal as a real
         deletion in the parent. Committing this destroys the link.

      2. **Stray gitlink.** An `??` or `A` entry on a directory that
         contains a `.git` AND is not registered in `.gitmodules`.
         Some other tool may have placed a nested checkout at an
         unintended path (e.g. via a buggy script that doubled a
         relative+absolute prefix); `git add -A` would happily commit
         a gitlink at that bogus location.

    On detection, returns (False, msg) and stages NOTHING — the user
    investigates manually (re-init the submodule, remove the stray
    `.git`, etc.) and re-runs. Only when no risky entries are present
    does the actual `git add -A` run.

    Returns (ok, error_msg). Empty msg on success."""
    submodule_paths = list_registered_submodule_paths(repo_path)

    rc, out, err = git(repo_path, ["status", "--porcelain=v1", "-z"])
    if rc != 0:
        return False, first_line(err) or "git status failed"

    refused: List[str] = []
    parts = out.split("\x00")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 3:
            continue
        xy = entry[:2]
        path_str = entry[3:]
        # Renames/copies emit an extra NUL-separated old-name chunk
        # under `-z`; skip it so we don't reparse it as a status row.
        if xy[0] in ("R", "C") or xy[1] in ("R", "C"):
            i += 1

        x, y = xy[0], xy[1]

        # 1. Refuse to stage submodule-pointer deletions.
        if (x == "D" or y == "D") and path_str in submodule_paths:
            refused.append(
                f"{path_str} (submodule pointer deletion — re-init the "
                "submodule or `git rm` it explicitly)")
            continue

        # 2. Refuse to stage stray gitlinks at unregistered paths.
        # Two flavours to catch: the entry itself IS a gitlink path,
        # OR the entry is an untracked DIRECTORY that contains an
        # embedded `.git` somewhere below. The second case is the
        # path-doubling failure mode — porcelain only reports the
        # top-level untracked dir, not the deeper embedded repo.
        if xy == "??" or x == "A" or y == "A":
            full = repo_path / path_str.rstrip("/")
            if (full / ".git").exists():
                normalized = path_str.rstrip("/")
                if normalized not in submodule_paths:
                    refused.append(
                        f"{normalized} (stray gitlink at unregistered "
                        "path — remove the nested .git or register it "
                        "in .gitmodules)")
                    continue
            elif xy == "??" and path_str.endswith("/"):
                for embedded in _find_embedded_gitlinks(full, repo_path):
                    if embedded not in submodule_paths:
                        refused.append(
                            f"{embedded} (stray gitlink at unregistered "
                            "path — remove the nested .git or register "
                            "it in .gitmodules)")
                if any(e for e in refused if e.startswith(path_str.rstrip("/"))):
                    continue

    if refused:
        return False, "refusing to stage: " + "; ".join(refused)

    rc, _, err = git(repo_path, ["add", "-A"])
    if rc != 0:
        return False, first_line(err) or "git add failed"
    return True, ""


def sync_subtree(parent_path: Path, prefix: str,
                 source_url: str, source_branch: str) -> Tuple[bool, str]:
    """Run `git subtree pull --squash` in the parent so the subtree's
    nested files catch up with the source repo's branch tip. NOTE: this
    creates a (squashed) merge commit in the parent — subtrees inherently
    can't be synced without one."""
    if not prefix:
        return False, "subtree prefix is empty"
    if not source_url:
        return False, "source repo has no remote URL"
    if source_url.startswith("-"):
        return False, "source repo remote URL looks like an option"
    if not is_safe_ref_arg(source_branch):
        return False, f"unsafe source branch: {source_branch or '(empty)'}"
    rc, status_out, _ = git(parent_path, ["status", "--porcelain=v1"])
    if rc != 0:
        return False, "parent status failed"
    if status_out.strip():
        return False, "parent has local changes"
    rc, _, err = git(parent_path, [
        "subtree", "pull", "--prefix=" + prefix,
        source_url, source_branch, "--squash",
    ])
    if rc != 0:
        return False, f"subtree pull failed: {first_line(err)}"
    return True, "subtree pulled"


# ---------- GitHub Actions integration (`gh` CLI) -------------------------
#
# Everything in this section is best-effort: if `gh` isn't on PATH or the
# repo isn't on github.com, the helpers return empty / a clean error and
# the rest of idlegit carries on as if the feature didn't exist.

import fnmatch  # noqa: E402
import json  # noqa: E402 — section-local convenience
import re  # noqa: E402
import shutil  # noqa: E402

from .models import WorkflowInfo, WorkflowInput  # noqa: E402


_GH_PATH: Optional[str] = shutil.which("gh")
_GH_SLUG_RE = re.compile(
    r"^(?:git@github\.com:|https?://(?:[^@/]+@)?github\.com/)"
    r"([^/]+)/([^/]+?)(?:\.git)?$"
)


def gh_available() -> bool:
    """True if the `gh` CLI is on PATH at idlegit startup. Cached at
    module-load time — no subprocess fork on every call."""
    return _GH_PATH is not None


def parse_github_slug(remote_url: Optional[str]) -> Optional[str]:
    """Extract `<owner>/<repo>` from a github.com remote URL (SSH or HTTPS,
    optionally with a user:token@ prefix and / or a .git suffix). Returns
    None for any non-github.com URL — the workflow features all gate on a
    non-None slug, so this is the single chokepoint."""
    if not remote_url:
        return None
    m = _GH_SLUG_RE.match(remote_url.strip())
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def gh(args: List[str],
       timeout: float = DEFAULT_GH_TIMEOUT_SECONDS) -> Tuple[int, str, str]:
    """Run `gh` with the given args. Mirrors `git()` — never raises; a
    missing CLI or OSError is reported as rc=1."""
    if _GH_PATH is None:
        return 1, "", "gh CLI not on PATH"
    try:
        p = subprocess.run(
            [_GH_PATH, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_git_env(),
        )
    except OSError as e:
        return 1, "", str(e)
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", f"gh timed out after {timeout:g}s"
    return p.returncode, p.stdout, p.stderr


def _parse_on_block(text: str) -> dict:
    """Best-effort parse of a workflow file's `on:` block. Returns a dict
    with: 'push' (bool), 'workflow_dispatch' (bool), 'push_branches'
    (List[str]), 'push_branches_ignore' (List[str]), 'push_tags'
    (List[str]), 'push_tags_ignore' (List[str]).

    Handles the three common YAML forms:
        on: push                                # scalar
        on: [push, pull_request]                # flow seq
        on:                                     # mapping
          push:
            branches: [a, b]
        on:                                     # mapping w/ block list
          push:
            tags:
            - 'v*.*.*'
    Tag filters matter because `on: push: tags: [...]` with no
    `branches:` means the workflow only fires on tag push — branch
    pushes don't trigger it. Anything more exotic (anchors,
    multi-document files, deeply mixed flow/block) falls through to
    "trigger detected" without branch / tag info, which keeps the
    predicate permissive rather than wrongly excluding."""
    result: dict = {
        "push": False,
        "workflow_dispatch": False,
        "push_branches": [],
        "push_branches_ignore": [],
        "push_tags": [],
        "push_tags_ignore": [],
    }
    lines = text.splitlines()

    on_idx: Optional[int] = None
    for i, raw in enumerate(lines):
        if raw.startswith(("on:", "on ")) and not raw.startswith((" ", "\t")):
            on_idx = i
            break
    if on_idx is None:
        return result

    on_line = lines[on_idx]
    suffix = on_line[on_line.index(":") + 1:].split("#", 1)[0].strip()

    # Form 1: `on: push` (or any single scalar trigger).
    if suffix and not suffix.startswith("[") and not suffix.startswith("{"):
        token = suffix.strip().strip('"\'')
        if token == "push":
            result["push"] = True
        elif token == "workflow_dispatch":
            result["workflow_dispatch"] = True
        return result

    # Form 2: `on: [push, ...]` flow seq.
    if suffix.startswith("["):
        inner = suffix.split("]", 1)[0][1:]
        tokens = [t.strip().strip('"\'') for t in inner.split(",") if t.strip()]
        result["push"] = "push" in tokens
        result["workflow_dispatch"] = "workflow_dispatch" in tokens
        return result

    # Form 3: nested mapping. Walk indented children until we hit a
    # column-0 line again (next top-level key or blank past EOF).
    children: List[Tuple[int, str]] = []
    j = on_idx + 1
    while j < len(lines):
        line = lines[j]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            j += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:  # next top-level key
            break
        children.append((indent, line))
        j += 1
    if not children:
        return result

    base_indent = min(ind for ind, _ in children)
    i = 0
    while i < len(children):
        indent, line = children[i]
        if indent != base_indent or ":" not in line:
            i += 1
            continue
        key, _, rest = line.strip().partition(":")
        key = key.strip()
        rest = rest.split("#", 1)[0].strip()

        if key == "workflow_dispatch":
            result["workflow_dispatch"] = True
            i += 1
            continue

        if key != "push":
            i += 1
            continue

        result["push"] = True
        # Walk push:'s sub-block for `branches:` / `branches-ignore:`
        # / `tags:` / `tags-ignore:`. Tags share the same shape as
        # branches — same inline-flow / inline-single / block-list
        # parsing applies, just into a different result bucket.
        sub = i + 1
        while sub < len(children) and children[sub][0] > indent:
            sub_indent, sub_line = children[sub]
            sub_stripped = sub_line.strip()
            if ":" not in sub_stripped:
                sub += 1
                continue
            sub_key, _, sub_rest = sub_stripped.partition(":")
            sub_key = sub_key.strip()
            sub_rest = sub_rest.split("#", 1)[0].strip()
            target_field: Optional[str] = None
            if sub_key == "branches":
                target_field = "push_branches"
            elif sub_key == "branches-ignore":
                target_field = "push_branches_ignore"
            elif sub_key == "tags":
                target_field = "push_tags"
            elif sub_key == "tags-ignore":
                target_field = "push_tags_ignore"
            if target_field is None:
                sub += 1
                continue
            # Inline form: branches: [a, b]
            if sub_rest.startswith("["):
                inner = sub_rest.split("]", 1)[0][1:]
                for t in inner.split(","):
                    val = t.strip().strip('"\'')
                    if val:
                        result[target_field].append(val)
                sub += 1
                continue
            # Single value inline: branches: master
            if sub_rest:
                result[target_field].append(sub_rest.strip('"\''))
                sub += 1
                continue
            # Block list form: items can sit at the SAME indent as the
            # parent key in YAML's compact style — e.g.:
            #     branches:
            #     - master
            # so we walk forward, accepting `- value` lines at indent
            # >= sub_indent, and bailing out as soon as we hit a non-list
            # line (that's a sibling key under push:).
            block = sub + 1
            while block < len(children):
                b_indent, b_line = children[block]
                if b_indent < sub_indent:
                    break
                b_stripped = b_line.strip()
                if not b_stripped.startswith("-"):
                    break
                val = b_stripped[1:].strip().strip('"\'')
                if val:
                    result[target_field].append(val)
                block += 1
            sub = block
        i = sub

    return result


def _parse_workflow_dispatch_inputs(text: str) -> "List[WorkflowInput]":
    """Best-effort scan for `on.workflow_dispatch.inputs` entries —
    returns each input as a `WorkflowInput(name, description,
    default)` in declaration order. Hand-rolled rather than pulling
    in PyYAML so the project keeps its zero-dependency promise; the
    parser handles the common shape:

        on:
          workflow_dispatch:
            inputs:
              version:
                description: 'Version to deploy'
                default: 'v1.0'
                required: true
                type: string

    Sub-fields it doesn't know about (type / required / options) are
    silently ignored — value handling on the dispatch side is
    generic (string), and required-but-empty inputs surface as a gh
    error rather than a parse-time concern. Returns [] when the
    workflow isn't dispatchable, the YAML is exotic enough to
    confuse the line scanner, or the `inputs:` block is absent."""
    lines = text.splitlines()

    on_idx: Optional[int] = None
    for i, raw in enumerate(lines):
        if raw.startswith(("on:", "on ")) and not raw.startswith((" ", "\t")):
            on_idx = i
            break
    if on_idx is None:
        return []

    def _walk_children(start_idx: int, parent_indent: int
                       ) -> List[Tuple[int, int, str]]:
        """Yield (line_idx, indent, line) for indented children of
        the line at start_idx (which sits at `parent_indent`).
        Stops at the next line whose indent is <= parent_indent.
        Skips blanks and comment-only lines."""
        out: List[Tuple[int, int, str]] = []
        j = start_idx + 1
        while j < len(lines):
            line = lines[j]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                j += 1
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= parent_indent:
                break
            out.append((j, indent, line))
            j += 1
        return out

    on_children = _walk_children(on_idx, 0)
    if not on_children:
        return []

    # Locate `workflow_dispatch:` directly under `on:`.
    base_indent = min(ind for _, ind, _ in on_children)
    wd_idx: Optional[int] = None
    wd_indent: int = 0
    for line_idx, indent, line in on_children:
        if indent != base_indent:
            continue
        stripped = line.strip()
        if stripped.startswith("workflow_dispatch:") or \
                stripped == "workflow_dispatch":
            wd_idx = line_idx
            wd_indent = indent
            break
    if wd_idx is None:
        return []

    # Then `inputs:` directly under `workflow_dispatch:`.
    wd_children = _walk_children(wd_idx, wd_indent)
    inputs_idx: Optional[int] = None
    inputs_indent: int = 0
    if wd_children:
        wd_base = min(ind for _, ind, _ in wd_children)
        for line_idx, indent, line in wd_children:
            if indent != wd_base:
                continue
            stripped = line.strip()
            if stripped.startswith("inputs:") or stripped == "inputs":
                inputs_idx = line_idx
                inputs_indent = indent
                break
    if inputs_idx is None:
        return []

    # Walk every input name (one per child at the inputs-base indent)
    # and pick up `description:` / `default:` from its sub-block.
    input_children = _walk_children(inputs_idx, inputs_indent)
    if not input_children:
        return []
    name_indent = min(ind for _, ind, _ in input_children)
    inputs: List[WorkflowInput] = []
    i = 0
    while i < len(input_children):
        line_idx, indent, line = input_children[i]
        if indent != name_indent:
            i += 1
            continue
        stripped = line.strip().split("#", 1)[0].strip()
        if not stripped.endswith(":"):
            # Skip exotic `name: <inline>` forms — inputs always
            # nest sub-fields under the name in real workflow YAML.
            i += 1
            continue
        name = stripped[:-1].strip().strip('"').strip("'")
        if not name:
            i += 1
            continue
        entry = WorkflowInput(name=name)
        # Sub-fields of this input — siblings deeper than name_indent
        # until the next sibling at name_indent.
        sub = i + 1
        while sub < len(input_children):
            sub_idx, sub_indent, sub_line = input_children[sub]
            if sub_indent <= name_indent:
                break
            sub_stripped = sub_line.strip()
            if ":" in sub_stripped:
                k, _, v = sub_stripped.partition(":")
                k = k.strip()
                v = v.split("#", 1)[0].strip()
                v = v.strip('"').strip("'")
                if k == "description":
                    entry.description = v
                elif k == "default":
                    entry.default = v
            sub += 1
        inputs.append(entry)
        i = sub if sub > i + 1 else i + 1
    return inputs


def discover_workflows_local(repo_path: Path) -> List[WorkflowInfo]:
    """Scan `<repo>/.github/workflows/*.{yml,yaml}` and return a
    WorkflowInfo per file. Workflow `name:` falls back to the filename.
    Trigger metadata is parsed via `_parse_on_block`, dispatch inputs
    via `_parse_workflow_dispatch_inputs`, so callers get everything
    they need to predict pushes and prompt for inputs without
    re-parsing the YAML themselves."""
    wf_dir = repo_path / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    found: List[WorkflowInfo] = []
    try:
        entries = sorted(wf_dir.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    for yml in entries:
        if not yml.is_file():
            continue
        if yml.suffix not in (".yml", ".yaml"):
            continue
        try:
            text = yml.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        wf_name = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("name:"):
                wf_name = stripped[len("name:"):].strip().strip('"').strip("'")
                if wf_name:
                    break
        if not wf_name:
            wf_name = yml.stem
        on_info = _parse_on_block(text)
        # Inputs are only meaningful when the workflow is
        # dispatchable — `_parse_workflow_dispatch_inputs` returns
        # [] either way, but skipping the call when not dispatchable
        # avoids walking the YAML for nothing.
        wf_inputs: List[WorkflowInput] = []
        if on_info["workflow_dispatch"]:
            wf_inputs = _parse_workflow_dispatch_inputs(text)
        found.append(WorkflowInfo(
            name=wf_name,
            path=str(yml.relative_to(repo_path)),
            state="",
            dispatchable=on_info["workflow_dispatch"],
            triggers_push=on_info["push"],
            push_branches=on_info["push_branches"],
            push_branches_ignore=on_info["push_branches_ignore"],
            push_tags=on_info["push_tags"],
            push_tags_ignore=on_info["push_tags_ignore"],
            inputs=wf_inputs,
        ))
    return found


def would_run_on_push(wf: WorkflowInfo, branch: str) -> bool:
    """Predict whether `wf` will fire on a push to `branch`. Considers:
      - whether the workflow declares `on: push` at all
      - GitHub workflow state (`disabled_*` short-circuits to False)
      - branches / branches-ignore glob patterns
      - tag-only filtering (`on: push: tags: […]` with no
        `branches:` only fires on tag pushes — branch pushes don't
        trigger it)
    `branch` matching uses fnmatch so the standard `feature/*` style
    patterns work. State is treated permissively when unknown (empty
    string = "we haven't queried gh yet"); only an explicit
    `disabled_*` marks the workflow as won't-run."""
    if not wf.triggers_push:
        return False
    if wf.state.startswith("disabled"):
        return False
    # Tag-only push filter: `tags:` is set but `branches:` isn't.
    # GitHub's semantics here are that a branch push never triggers
    # the workflow, regardless of branch name — so the predicate
    # short-circuits to False for the branch-push prediction.
    if (wf.push_tags or wf.push_tags_ignore) and not wf.push_branches:
        return False
    if wf.push_branches:
        if not any(fnmatch.fnmatchcase(branch, p) for p in wf.push_branches):
            return False
    for ignore in wf.push_branches_ignore:
        if fnmatch.fnmatchcase(branch, ignore):
            return False
    return True


def list_remote_workflow_states(slug: str) -> "dict[str, str]":
    """Return a mapping of workflow name → GitHub workflow state for
    every workflow in the repo, including disabled ones (`--all`).
    Returns {} on any failure so callers can treat it as "no remote
    info available" without special-casing."""
    rc, out, _ = gh([
        "workflow", "list",
        "--repo", slug,
        "--all",
        "--json", "name,state,path",
    ])
    if rc != 0:
        return {}
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, list):
        return {}
    return {row.get("name", ""): row.get("state", "")
            for row in data if isinstance(row, dict)}


def merge_remote_workflow_states(workflows: List[WorkflowInfo],
                                 slug: str) -> None:
    """Mutate `workflows` in place so each WorkflowInfo's `state` field
    reflects the GitHub-side state. Does nothing when the gh call fails
    or returns no rows. Workflows we can't match by name keep whatever
    state they had (empty string by default)."""
    states = list_remote_workflow_states(slug)
    if not states:
        return
    for wf in workflows:
        remote = states.get(wf.name, "")
        if remote:
            wf.state = remote


def list_recent_runs(slug: str, branch: str, commit: str,
                     limit: int = 20) -> List[dict]:
    """`gh run list` filtered to a specific branch + commit. Used by the
    post-push tracker to find the runs that just got triggered. Returns
    [] if `gh` is unavailable, the slug is wrong, or the response can't
    be parsed."""
    rc, out, _ = gh([
        "run", "list",
        "--repo", slug,
        "--branch", branch,
        "--commit", commit,
        "--limit", str(limit),
        "--json", "databaseId,name,workflowName,status,conclusion,createdAt,headSha,url",
    ])
    if rc != 0:
        return []
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def get_run_view(slug: str, run_id: int) -> Optional[dict]:
    """Detailed view of one run: status, conclusion, jobs (each with their
    own status + steps). Returns None on failure so the caller can skip
    polling rather than crashing."""
    rc, out, _ = gh([
        "run", "view", str(run_id),
        "--repo", slug,
        "--json", "status,conclusion,name,workflowName,jobs,url,headSha",
    ])
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None


def dispatch_workflow(slug: str, workflow_name: str,
                      ref: str,
                      inputs: "Optional[dict]" = None
                      ) -> Tuple[bool, str]:
    """Trigger a workflow_dispatch on the given branch. Surfaces the
    gh error (single line) if the workflow isn't dispatchable, the
    ref is invalid, or the user lacks permission.

    `inputs` (when supplied) is a `{name: value}` dict — non-empty
    entries are forwarded as `-F <name>=<value>`. Required-but-empty
    inputs aren't pre-checked here; we let GitHub return the
    canonical error so the user sees the same message they'd get on
    the web UI."""
    if not workflow_name or workflow_name.startswith("-"):
        return False, "unsafe workflow name"
    if not is_safe_ref_arg(ref):
        return False, "unsafe workflow ref"
    args = [
        "workflow", "run", workflow_name,
        "--repo", slug,
        "--ref", ref,
    ]
    if inputs:
        for name, value in inputs.items():
            if not name or name.startswith("-"):
                # Defence-in-depth: gh's argv parsing would already
                # reject these, but skipping here keeps the error
                # surface tight.
                continue
            if value == "":
                continue
            args.extend(["-F", f"{name}={value}"])
    rc, _, err = gh(args)
    if rc != 0:
        return False, first_line(err)
    return True, "dispatched"


def cancel_run(slug: str, run_id: int) -> Tuple[bool, str]:
    """`gh run cancel <run_id> --repo <slug>` — request that GitHub
    cancel an in-flight workflow run. Returns (ok, message). The
    existing `_poll_run` poller will pick up the resulting "cancelled"
    conclusion on its next iteration and flip the parent task to
    `warn`, so the caller doesn't need to do anything else."""
    rc, _, err = gh([
        "run", "cancel", str(run_id), "--repo", slug,
    ])
    if rc != 0:
        return False, first_line(err)
    return True, "cancellation requested"


# Cap on `gh run view --log` output so a 50MB log doesn't blow up the
# in-memory line list. The diff-viewer uses the same shape — see
# `_MAX_DIFF_LINES` over in `ui/modals/diff_viewer.py`.
_MAX_RUN_LOG_LINES = 50_000


def fetch_run_log(slug: str, run_id: int,
                  job_id: Optional[int] = None,
                  only_failed: bool = False
                  ) -> Tuple[bool, List[str], str]:
    """Fetch a workflow-run log via `gh run view`. Returns
    `(ok, lines, error)`. On rc != 0 the lines list is empty and
    `error` carries the first line of gh's stderr. Caps at
    `_MAX_RUN_LOG_LINES` to keep the viewer's list bounded — when the
    cap trips, an explicit truncation notice is appended as the last
    line so the user sees why they hit the end."""
    args = ["run", "view", str(run_id), "--repo", slug]
    if job_id is not None:
        args += ["--job", str(job_id)]
    args.append("--log-failed" if only_failed else "--log")
    rc, out, err = gh(args, timeout=60.0)
    if rc != 0:
        return False, [], first_line(err)
    lines = out.splitlines() if out else []
    if len(lines) > _MAX_RUN_LOG_LINES:
        lines = lines[:_MAX_RUN_LOG_LINES]
        lines.append(f"... (truncated at {_MAX_RUN_LOG_LINES} lines)")
    return True, lines, ""


# ---------- Target-state query (for the action menu) -----------------------


def query_target_state(path: Path, max_commits: int = 5) -> TargetState:
    """Snapshot the fields the action-menu header needs for a target
    checkout: current branch, upstream tracking ref, ahead/behind counts,
    origin URL, and the last `max_commits` commits on HEAD. Read-only —
    no git mutations. Empty/zero fields on any sub-query failure so the
    menu can still open against a detached / unborn / origin-less repo."""
    branch = ""
    rc, out, _ = git(path, ["branch", "--show-current"])
    if rc == 0:
        branch = out.strip() or "(detached)"

    upstream: Optional[str] = None
    rc, out, _ = git(path, [
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if rc == 0 and out.strip():
        upstream = out.strip()

    ahead = 0
    behind = 0
    if upstream:
        rc, out, _ = git(path, [
            "rev-list", "--count", "--left-right", f"{upstream}...HEAD"])
        if rc == 0:
            parts = out.split()
            if len(parts) == 2:
                try:
                    behind = int(parts[0])
                    ahead = int(parts[1])
                except ValueError:
                    pass

    rc, out, _ = git(path, ["remote", "get-url", "origin"])
    has_origin = rc == 0 and bool(out.strip())

    merging = False
    rc, out, _ = git(path, ["rev-parse", "--git-dir"])
    if rc == 0 and out.strip():
        gd = Path(out.strip())
        if not gd.is_absolute():
            gd = (path / gd).resolve()
        for marker in MERGE_MARKER_FILES:
            if (gd / marker).exists():
                merging = True
                break
        if not merging:
            for marker in MERGE_MARKER_DIRS:
                if (gd / marker).is_dir():
                    merging = True
                    break

    rc, out, _ = git(path, ["status", "--porcelain=v1"])
    dirty = rc == 0 and bool(out.strip())

    commits: List[str] = []
    rc, out, _ = git(path, [
        "log", f"-n{max_commits}", "--pretty=format:%h %s (%cr)"])
    if rc == 0 and out.strip():
        commits = out.strip().splitlines()

    return TargetState(
        branch=branch, upstream=upstream, ahead=ahead, behind=behind,
        has_origin=has_origin, merging=merging, dirty=dirty,
        recent_commits=commits,
    )


def query_working_tree(path: Path) -> List[FileEntry]:
    """Snapshot the working tree at `path` for the action-menu's tree
    pane: one FileEntry per changed/untracked file. Combines porcelain
    status (X/Y codes, untracked) with `git diff --numstat HEAD` so
    each row carries an insertion/deletion count when available."""
    entries: Dict[str, FileEntry] = {}
    rc, out, _ = git(path, ["status", "--porcelain=v1", "-z"])
    if rc != 0:
        return []
    for xy, p in _iter_porcelain_z_entries(out):
        if xy == "??":
            entries[p] = FileEntry(path=p, x=" ", y=" ", untracked=True)
            continue
        entries[p] = FileEntry(path=p, x=xy[0], y=xy[1])

    rc, out, _ = git(path, ["diff", "--numstat", "HEAD"])
    if rc != 0:
        rc, out, _ = git(path, ["diff", "--numstat"])
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            try:
                ins = int(parts[0]) if parts[0] != "-" else 0
                dels = int(parts[1]) if parts[1] != "-" else 0
            except ValueError:
                continue
            fe = entries.get(parts[2])
            if fe is not None:
                fe.inserted = ins
                fe.deleted = dels

    # Sort: staged first, then unstaged-modified, then untracked. Within
    # each band, alphabetical for predictability.
    def sort_key(fe: FileEntry) -> Tuple[int, str]:
        band = 2 if fe.untracked else (0 if fe.x != " " else 1)
        return (band, fe.path.lower())

    return sorted(entries.values(), key=sort_key)


def load_commits(path: Path, skip: int, count: int) -> Tuple[
        List[CommitEntry], bool]:
    """Page through `git log` for the action-menu's commits tab. Returns
    (entries, exhausted) — `exhausted` flips True when git returned
    fewer rows than asked, meaning we've walked back to the root."""
    rc, out, _ = git(path, [
        "log", f"--skip={skip}", f"-n{count}",
        "--pretty=format:%h%x09%cr%x09%s",
    ])
    if rc != 0 or not out.strip():
        return [], True
    rows: List[CommitEntry] = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        rows.append(CommitEntry(
            sha=parts[0], relative=parts[1], subject=parts[2]))
    return rows, len(rows) < count


def list_remotes(path: Path) -> List[Tuple[str, str]]:
    """Return [(name, url), ...] for every remote configured on the repo,
    in `git remote` order (which is usually configured order). Each
    remote appears once — `git remote -v` lists fetch + push variants;
    we keep the fetch URL since that's what the user typically edits."""
    rc, out, _ = git(path, ["remote"])
    if rc != 0:
        return []
    names = [ln.strip() for ln in out.splitlines() if ln.strip()]
    result: List[Tuple[str, str]] = []
    for name in names:
        rc2, url, _ = git(path, ["remote", "get-url", name])
        result.append((name, url.strip() if rc2 == 0 else ""))
    return result


def clone_repo(url: str, dest: Path, branch: str = "",
               recurse_submodules: bool = True) -> Tuple[bool, str]:
    """Run `git clone` into `dest`. `dest` must not already exist (or
    must be empty) — git refuses on a non-empty target. Returns
    (ok, message); message is the first non-empty stderr line on
    failure, or "" on success."""
    if not url or url.startswith("-"):
        return False, "remote URL looks like an option"
    args = ["clone"]
    if branch:
        if not is_safe_ref_arg(branch):
            return False, "branch name looks like an option"
        args.extend(["--branch", branch])
    if recurse_submodules:
        args.append("--recurse-submodules")
    args.extend(["--", url, str(dest)])
    # Run from the parent dir so a relative dest resolves predictably.
    parent = dest.parent if dest.parent.exists() else Path.cwd()
    rc, _, err = git(parent, args, timeout=600)
    if rc == 0:
        return True, ""
    return False, first_line(err)


def list_branches(path: Path) -> Tuple[List[str], str]:
    """Return (sorted unique branch names, current_branch). Local branches
    listed first; remote-tracking branches without a local counterpart
    come second (their `origin/` prefix stripped). HEAD is excluded."""
    current = ""
    rc, out, _ = git(path, ["branch", "--show-current"])
    if rc == 0:
        current = out.strip()

    rc, out, _ = git(path, [
        "branch", "-a", "--format=%(refname:short)"])
    if rc != 0:
        return [], current

    locals_seen: List[str] = []
    remote_only: List[str] = []
    have_local: set = set()
    for line in out.strip().splitlines():
        name = line.strip()
        if not name:
            continue
        if name.startswith("origin/HEAD"):
            continue
        if name.startswith("origin/"):
            short = name[len("origin/"):]
            if short and short not in have_local:
                if short not in remote_only:
                    remote_only.append(short)
        else:
            if name not in locals_seen:
                locals_seen.append(name)
                have_local.add(name)

    remote_only = [b for b in remote_only if b not in have_local]
    return locals_seen + remote_only, current


def list_stashes(path: Path) -> List[Tuple[str, str]]:
    """Return [(ref, message), ...] for every stash entry. `ref` is
    the stable form `stash@{N}` so the apply action gets a positional
    target that survives between calls; `message` is the
    human-friendly "On <branch>: <subject>" line.

    Empty list when there are no stashes (or the repo predates the
    `refs/stash` ref so `git stash list` returns nothing). Failures
    are coerced to an empty list — same shape as `list_branches`."""
    rc, out, _ = git(path, ["stash", "list", "--format=%gd%x09%gs"])
    if rc != 0:
        return []
    rows: List[Tuple[str, str]] = []
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("\t", 1)
        ref = parts[0].strip()
        msg = parts[1] if len(parts) > 1 else ""
        if not ref:
            continue
        rows.append((ref, msg))
    return rows


def list_tags_at(path: Path, sha: str) -> List[str]:
    """Return every tag pointing at `sha`, in `git tag` order. Refuses
    `-`-prefixed shas as a defence-in-depth (git's argv parsing already
    rejects them in this position) so the caller doesn't have to."""
    if not sha or sha.startswith("-"):
        return []
    rc, out, _ = git(path, ["tag", "--points-at", sha])
    if rc != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def query_commit_files(path: Path, sha: str) -> List[FileEntry]:
    """File-changes list for a single commit, in the same FileEntry
    shape `query_working_tree` returns so the review-pane file row
    renderer can display either without branching. `x` is the change
    status letter (M / A / D / R / …); `y` stays blank since commits
    don't carry a worktree-side state. Insertion / deletion counts
    come from `git show --numstat`."""
    if not sha or sha.startswith("-"):
        return []
    entries: Dict[str, FileEntry] = {}
    rc, name_out, _ = git(path, [
        "show", "--name-status", "--format=", sha,
    ])
    if rc != 0:
        return []
    for line in name_out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][0] if parts[0] else " "
        # Renames have shape `R100\told\tnew` — pick the new path so
        # the row reflects what's in the tree after the commit.
        path_s = parts[-1]
        if path_s in entries:
            continue
        entries[path_s] = FileEntry(path=path_s, x=status, y=" ")
    rc2, num_out, _ = git(path, [
        "show", "--numstat", "--format=", sha,
    ])
    if rc2 == 0:
        for line in num_out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            try:
                ins = int(parts[0]) if parts[0] != "-" else 0
                dels = int(parts[1]) if parts[1] != "-" else 0
            except ValueError:
                continue
            fe = entries.get(parts[-1])
            if fe is not None:
                fe.inserted = ins
                fe.deleted = dels
    return sorted(entries.values(), key=lambda f: f.path.lower())


def get_commit_details(path: Path, sha: str
                       ) -> Tuple[str, str, str, str]:
    """Return `(author, date, subject, body)` for `sha`. `date` uses
    git's --date=short (YYYY-MM-DD) for a stable display format.
    Empty tuple on failure / unsafe sha — caller treats blank fields
    as "not yet loaded" so the modal can render before this lands."""
    if not sha or sha.startswith("-"):
        return ("", "", "", "")
    # Use a NUL separator so multi-line bodies don't collide with
    # tab-separated fields.
    rc, out, _ = git(path, [
        "show", "-s",
        "--format=%an%x00%ad%x00%s%x00%b",
        "--date=short", sha,
    ])
    if rc != 0 or not out:
        return ("", "", "", "")
    parts = out.split("\x00", 3)
    while len(parts) < 4:
        parts.append("")
    return (parts[0], parts[1], parts[2], parts[3].rstrip("\n"))


def query_file_log(path: Path, file_path: str, *,
                   max_count: int = 200,
                   sha: str = "") -> List[str]:
    """Return one-line `<short-sha> <date> <subject>` rows for
    commits that touched `file_path`. When `sha` is supplied the
    log is scoped to commits reachable from that sha (useful when
    the diff viewer is opened against a specific commit). Empty
    list on failure / unsafe input."""
    if not file_path or file_path.startswith("-"):
        return []
    args = ["log", f"--max-count={max_count}",
            "--pretty=format:%h %ad %s", "--date=short"]
    if sha:
        if sha.startswith("-"):
            return []
        args.append(sha)
    args.extend(["--", file_path])
    rc, out, _ = git(path, args)
    if rc != 0 or not out:
        return []
    return out.splitlines()


def query_file_blame(path: Path, file_path: str, *,
                     sha: str = "") -> List[str]:
    """Return blame output lines for `file_path` (at `sha` if
    given). Each row is the standard `<short-sha> (<author> <date>
    <line-number>) <line content>` format git emits with
    `--abbrev=8`. Empty list when blame fails (binary file,
    untracked, etc.)."""
    if not file_path or file_path.startswith("-"):
        return []
    args = ["blame", "--abbrev=8"]
    if sha:
        if sha.startswith("-"):
            return []
        args.append(sha)
    args.extend(["--", file_path])
    rc, out, _ = git(path, args)
    if rc != 0:
        return []
    return out.splitlines()


def query_commit_reflog(path: Path, sha: str) -> List[str]:
    """Return HEAD reflog entries that mention `sha` as the
    resolved commit. Each row: `<short-sha> <reflog-selector>
    <reflog-message>`, e.g. `a1b2c3d HEAD@{2} reset: moving to
    HEAD~1`. Filtered client-side by matching the first
    space-separated token against the requested sha (full or
    short) — `git reflog` lacks a built-in filter and we'd rather
    do the comparison locally than hit the network. Empty list on
    failure or no matches."""
    if not sha:
        return []
    rc, out, _ = git(path, [
        "reflog", "--pretty=format:%h %gd %gs",
    ])
    if rc != 0 or not out:
        return []
    sha_short = sha[:8]
    matches: List[str] = []
    for line in out.splitlines():
        if not line:
            continue
        head = line.split(" ", 1)[0]
        if (head == sha_short or head == sha
                or sha.startswith(head) or head.startswith(sha_short)):
            matches.append(line)
    return matches


def commit_show_diff(path: Path, sha: str, file_path: str) -> List[str]:
    """Return the per-file diff lines for a commit — i.e. what
    `git show <sha> -- <path>` produces, split into lines. Used by
    the diff viewer when it's opened from the commit view modal."""
    if (not sha or sha.startswith("-")
            or not file_path or file_path.startswith("-")):
        return ["(unsafe sha or path)"]
    rc, out, err = git(path, [
        "show", sha, "--", file_path,
    ])
    if rc != 0 and not out:
        return [(err or "(no diff available)").strip()]
    return out.splitlines() if out else ["(no diff)"]


def list_remote_tracking_refs(path: Path) -> List[str]:
    """Return remote-tracking refs in their full form (e.g.
    `origin/main`, `upstream/dev`). Used by the set-upstream picker —
    `git branch --set-upstream-to=` wants the qualified ref, so we
    keep the prefix here rather than stripping it the way
    `list_branches` does."""
    rc, out, _ = git(path, ["branch", "-r", "--format=%(refname:short)"])
    if rc != 0:
        return []
    refs: List[str] = []
    for line in out.splitlines():
        ref = line.strip()
        if not ref or ref.endswith("/HEAD"):
            continue
        refs.append(ref)
    return sorted(refs)


# ---------- Commit-message suggestion --------------------------------------


def _collect_changes_at(path: Path,
                        staged: List[Tuple[str, str]],
                        unstaged: List[Tuple[str, str]],
                        untracked: List[str],
                        auto_stage: bool) -> List[FileChange]:
    """Build FileChange entries for a working tree at `path`. Status lists
    are passed in; refresh_repo caches them on the Repo, and ad-hoc scans
    (e.g. nested submodule checkouts) call `_scan_path_status` first."""
    changes: Dict[str, FileChange] = {}

    diff_stats: Dict[str, int] = {}
    rc, out, _ = git(path, ["diff", "--numstat", "HEAD"])
    if rc != 0:
        rc, out, _ = git(path, ["diff", "--numstat"])
    if rc == 0:
        for line in out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            try:
                ins = int(parts[0]) if parts[0] != "-" else 0
                dels = int(parts[1]) if parts[1] != "-" else 0
                diff_stats[parts[2]] = ins + dels
            except ValueError:
                continue

    def classify(status: str) -> str:
        if status == "A":
            return "added"
        if status == "D":
            return "deleted"
        return "modified"

    def weight_for(kind: str, p: str) -> float:
        if kind == "added":
            try:
                return float((path / p).stat().st_size)
            except OSError:
                return 0.0
        if kind == "modified":
            return float(diff_stats.get(p, 0))
        return 0.0

    sources: List[Tuple[str, str]] = list(staged)
    if auto_stage:
        sources += list(unstaged)

    for status, p in sources:
        if p in changes:
            continue
        kind = classify(status)
        changes[p] = FileChange(path=p, kind=kind, weight=weight_for(kind, p))

    if auto_stage:
        for p in untracked:
            if p in changes:
                continue
            changes[p] = FileChange(path=p, kind="added", weight=weight_for("added", p))

    return list(changes.values())


def _scan_path_status(path: Path) -> Tuple[
        List[Tuple[str, str]], List[Tuple[str, str]], List[str], bool]:
    """Run `git status --porcelain=v1 -z` at `path` and split entries into
    (staged, unstaged, untracked, has_conflicts). Used by the ad-hoc Tab-
    suggest path on nested submodule checkouts."""
    staged: List[Tuple[str, str]] = []
    unstaged: List[Tuple[str, str]] = []
    untracked: List[str] = []
    has_conflicts = False
    rc, out, _ = git(path, ["status", "--porcelain=v1", "-z"])
    if rc != 0:
        return staged, unstaged, untracked, has_conflicts
    for xy, p in _iter_porcelain_z_entries(out):
        if xy == "??":
            untracked.append(p)
            continue
        if xy in CONFLICT_CODES:
            has_conflicts = True
            continue
        x, y = xy[0], xy[1]
        if x != " ":
            staged.append((x, p))
        if y != " ":
            unstaged.append((y, p))
    return staged, unstaged, untracked, has_conflicts


def collect_changes(repo: Repo, auto_stage: bool) -> List[FileChange]:
    """Build the FileChange list driving the review screen and the
    commit-message suggester for `repo`. When `auto_stage` is True, the
    unstaged + untracked sides feed in alongside the staged set
    (matches the "stage all" pipeline); when False, only already-staged
    paths show up. Pure transform over the snapshot already captured
    on the Repo dataclass — no re-querying git here."""
    return _collect_changes_at(
        repo.path, repo.staged, repo.unstaged, repo.untracked, auto_stage)


def _format_suggestion(changes: List[FileChange],
                       max_added: int, max_updated: int, max_deleted: int) -> str:
    """Pick the top files per category and join them into the canonical
    'add: a, b; update: c, d; remove: e' string. Imperative tense to
    match git's commit-message convention ("add foo" reads as "this
    commit will add foo", not "this commit added foo")."""
    if not changes:
        return ""
    by_kind: Dict[str, List[FileChange]] = {"added": [], "modified": [], "deleted": []}
    for c in changes:
        by_kind[c.kind].append(c)
    for kind in by_kind:
        by_kind[kind].sort(key=lambda c: (-c.weight, c.path.lower()))

    caps = {"added": max_added, "modified": max_updated, "deleted": max_deleted}
    # Internal kind keys stay past-tense to avoid touching every FileChange
    # producer. Only the user-facing labels go imperative.
    label = {"added": "add", "modified": "update", "deleted": "remove"}
    parts: List[str] = []
    for kind in ("added", "modified", "deleted"):
        cap = caps[kind]
        if cap <= 0:
            continue
        picks = by_kind[kind][:cap]
        if not picks:
            continue
        names = [Path(c.path).name for c in picks]
        parts.append(f"{label[kind]}: {', '.join(names)}")
    return "; ".join(parts)


def suggest_commit_message(repo: Repo, *,
                           max_added: int, max_updated: int, max_deleted: int,
                           auto_stage: bool) -> str:
    """Build an 'add: a, b; update: c, d; remove: e' message from the top
    files of each kind, ranked by weight. Returns '' if there is nothing to
    commit or the repo is mid-merge."""
    if repo.merging:
        return ""
    return _format_suggestion(
        collect_changes(repo, auto_stage),
        max_added, max_updated, max_deleted)


def suggest_commit_message_at(path: Path, *,
                              max_added: int, max_updated: int, max_deleted: int,
                              auto_stage: bool) -> str:
    """Like `suggest_commit_message`, but scans the working tree at `path`
    fresh — used for nested submodule checkouts where the status is not
    cached on a Repo. Returns '' if status fails or unmerged paths exist."""
    staged, unstaged, untracked, conflicts = _scan_path_status(path)
    if conflicts:
        return ""
    return _format_suggestion(
        _collect_changes_at(path, staged, unstaged, untracked, auto_stage),
        max_added, max_updated, max_deleted)


def suggest_commit_message_for_paths(
        path: Path, paths: List[str], *,
        max_added: int, max_updated: int, max_deleted: int) -> str:
    """Like `suggest_commit_message_at`, but restricted to a specific
    set of paths — the files the user has checked on the review
    screen. Every checked file is treated as headed for the index at
    commit time (auto_stage semantics) regardless of its current x/y
    state, so the suggestion describes the commit that would actually
    land. Returns '' on failure or if there's nothing to suggest."""
    staged, unstaged, untracked, conflicts = _scan_path_status(path)
    if conflicts:
        return ""
    paths_set = set(paths)
    staged = [(s, p) for (s, p) in staged if p in paths_set]
    unstaged = [(s, p) for (s, p) in unstaged if p in paths_set]
    untracked = [p for p in untracked if p in paths_set]
    return _format_suggestion(
        _collect_changes_at(path, staged, unstaged, untracked,
                            auto_stage=True),
        max_added, max_updated, max_deleted)


# ---------- LFS helpers ----------------------------------------------------


def format_size(num_bytes: int) -> str:
    """Human-readable size for the LFS-warning rows on the review
    screen. Always MB or GB — the LFS threshold is MB-scaled so any
    value worth flagging lives in that band, and the narrower set of
    units keeps the column visually tidy."""
    mb = num_bytes / (1024 * 1024)
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


def format_time_ago(seconds: float) -> str:
    """Render a non-negative elapsed-seconds value as a compact "Ns / Nm
    ago" tag. Used in the sidebar so each task carries a recency hint that
    refreshes on every redraw. Negative inputs (clock skew, monotonic
    weirdness) are clamped to "now"."""
    if seconds < 1:
        return "now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def derive_lfs_pattern(path: str) -> str:
    """Best-effort LFS pattern. Use the file extension if it has one,
    otherwise fall back to the literal basename."""
    ext = Path(path).suffix
    if ext and len(ext) > 1:
        return f"*{ext}"
    return Path(path).name


def apply_lfs_tracking(cand: LFSCandidate) -> Tuple[bool, str]:
    """Add an LFS rule for this file, stage .gitattributes, and re-stage the
    file so the upcoming commit routes the blob through git-lfs."""
    repo = cand.repo
    pattern = derive_lfs_pattern(cand.path)
    rc, _, err = git(repo.path, ["lfs", "track", pattern])
    if rc != 0:
        return False, f"lfs track failed: {first_line(err)}"
    rc, _, err = git(repo.path, ["add", ".gitattributes"])
    if rc != 0:
        return False, f"add .gitattributes failed: {first_line(err)}"
    git(repo.path, ["rm", "--cached", "--ignore-unmatch", cand.path])
    rc, _, err = git(repo.path, ["add", cand.path])
    if rc != 0:
        return False, f"re-add failed: {first_line(err)}"
    return True, f"LFS-tracked via {pattern}"


def find_lfs_warnings(repo: Repo, auto_stage: bool,
                      threshold_bytes: int) -> List[Tuple[str, str]]:
    """Return [(path, size-str)] for files >= threshold_bytes that would be
    committed but aren't routed through git-lfs by .gitattributes. A
    threshold of 0 disables the check entirely."""
    if threshold_bytes <= 0:
        return []
    if auto_stage:
        candidates = [p for _, p in repo.staged]
        candidates += [p for _, p in repo.unstaged]
        candidates += list(repo.untracked)
    else:
        candidates = [p for _, p in repo.staged]

    warnings: List[Tuple[str, str]] = []
    seen: set = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        full = repo.path / path
        try:
            size = full.stat().st_size
        except OSError:
            continue
        if size < threshold_bytes:
            continue
        rc, out, _ = git(repo.path, ["check-attr", "filter", "--", path])
        is_lfs = rc == 0 and ": lfs" in out
        if not is_lfs:
            warnings.append((path, format_size(size)))
    return warnings
