"""Workspace settings — opened with Tab on the workspace selector
row. Lets the user edit the active workspace's folder list (add /
remove / rename) and override per-workspace settings against the
global idlegit.conf defaults. Saves immediately on every edit so a
crash mid-session doesn't lose intent."""
from __future__ import annotations

import curses
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from core.config import (
    TRUNCATION_MODES,
    WORKSPACE_OVERRIDE_TARGETS,
    WORKSPACE_OVERRIDE_TYPES,
    base_value_for_override,
    save_workspaces,
    state_attr_value_from_override,
)
from core.git_ops import discover_repos
from core.models import State, Workspace, WorkspaceDraft, WorkspaceMenu, WorkspaceMenuRow

from ..colors import (
    PAIR_DLG_CYAN, PAIR_DLG_FG, PAIR_DLG_FG_HINT_TEXT, PAIR_DLG_OK,
    PAIR_DLG_WARN,
)
from ..geometry import (
    draw_modal_fill, draw_scroll_overflow, modal_geometry, safe_addstr,
    truncate,
)
from ..hints import (
    KEY_BACKSPACE, KEY_ENTER, KEY_ESC, KEY_LEFT_RIGHT, KEY_SPACE,
    KEY_UP_DOWN, Hint, render_hints,
)


def _hints_edit_mode(menu: WorkspaceMenu) -> list:
    """Hints while a folder / add-folder row is in inline edit mode."""
    return [
        Hint("type", "edit path"),
        Hint(KEY_ENTER, "save path"),
        Hint(KEY_ESC, "cancel edit"),
    ]


def _hints_nav_mode(state, menu: WorkspaceMenu) -> list:
    """Hints while the modal is in row-nav mode. The Enter / Backspace
    descriptions adapt to whichever row kind is focused — folder rows
    open an edit field, add-folder is a creator-style entry, override
    rows toggle / cycle / clear."""
    hints: list = [Hint(KEY_UP_DOWN, "select")]
    if 0 <= menu.selected < len(menu.rows):
        row = menu.rows[menu.selected]
        if row.kind == "folder":
            hints.append(Hint(KEY_ENTER, "edit path"))
            ws = state.active_workspace
            n = len(ws.folders) if ws else 0
            if n > 1:
                hints.append(Hint(KEY_BACKSPACE, "remove folder"))
            else:
                hints.append(Hint(KEY_BACKSPACE,
                                  "(can't remove last folder)"))
        elif row.kind == "add_folder":
            hints.append(Hint(KEY_ENTER, "type a new folder path"))
        elif row.kind == "ignore_pattern":
            hints.append(Hint(KEY_ENTER, "edit pattern"))
            hints.append(Hint(KEY_BACKSPACE, "remove pattern"))
        elif row.kind == "add_ignore_pattern":
            hints.append(Hint(KEY_ENTER, "type a new ignore pattern"))
        elif row.kind == "clone":
            hints.append(Hint(KEY_ENTER, "open clone dialog"))
        elif row.kind == "save_ephemeral":
            hints.append(Hint(KEY_ENTER, "persist as workspace"))
        elif row.kind == "bool":
            hints.append(Hint(KEY_SPACE, "toggle"))
            hints.append(Hint(KEY_BACKSPACE, "clear override"))
        elif row.kind == "trunc_mode":
            hints.append(Hint(KEY_LEFT_RIGHT, "cycle mode"))
            hints.append(Hint(KEY_BACKSPACE, "clear override"))
        elif row.kind == "int":
            hints.append(Hint(KEY_LEFT_RIGHT, "adjust value"))
            hints.append(Hint(KEY_BACKSPACE, "clear override"))
    hints.append(Hint(KEY_ESC, "close"))
    return hints


def _draw_menu_hints(stdscr, state, menu: WorkspaceMenu, y: int, x: int,
                     w: int, attr: int) -> None:
    hints = (_hints_edit_mode(menu) if menu.editing
             else _hints_nav_mode(state, menu))
    render_hints(stdscr, y, x, w, hints, attr=attr)


# Schema-driven override-row list. Folder rows are layered in dynamically
# at open time (one per ws.folders entry plus a trailing "+ Add folder"
# sentinel) so the modal always reflects the current folder list; this
# block is the static settings-section schema, in display order.
_OVERRIDE_ROWS: Tuple[WorkspaceMenuRow, ...] = (
    # COMMIT — controls the commit pipeline that used to live as
    # toggle buttons on the main panel. The main panel no longer
    # carries these as quick-toggles; the workspace menu is the sole
    # place to flip them now.
    WorkspaceMenuRow(label="COMMIT", attr_name="", kind="header"),
    WorkspaceMenuRow(
        label="Auto-stage first",
        attr_name="default_auto_stage", kind="bool",
        hint_text="stage all changes before committing (default: on)"),
    WorkspaceMenuRow(
        label="Auto-push after",
        attr_name="default_auto_push", kind="bool",
        hint_text="push to upstream after a successful commit (default: on)"),
    WorkspaceMenuRow(
        label="Track actions by default",
        attr_name="track_actions_default", kind="bool",
        hint_text="poll GitHub Actions on new repos (default: on)"),
    # SMART-SYNC — Ctrl+S behaviour. align_heads gates the detached-
    # checkout handling; auto_ff gates the loser-FF step; prompt_for_
    # branch decides whether to ask the user which branch a detached
    # winner should push to (off → auto-resolve to origin/HEAD).
    WorkspaceMenuRow(label="SMART-SYNC", attr_name="", kind="header"),
    WorkspaceMenuRow(
        label="Align heads",
        attr_name="default_align_heads", kind="bool",
        hint_text="reattach a detached HEAD onto its branch before sync (default: on)"),
    WorkspaceMenuRow(
        label="Auto-FF",
        attr_name="default_auto_ff", kind="bool",
        hint_text="fast-forward the loser side of a sync when safe (default: on)"),
    WorkspaceMenuRow(
        label="Prompt for branch",
        attr_name="default_prompt_for_branch", kind="bool",
        hint_text="ask which branch a detached winner pushes to (default: on)"),
    WorkspaceMenuRow(
        label="Prevent silent merge (sync)",
        attr_name="default_prevent_smart_sync_silent_merge", kind="bool",
        hint_text="smart-sync: FF-only for losers — no auto merge commits (default: off)"),
    WorkspaceMenuRow(
        label="Auto-push submodule parent",
        attr_name="default_auto_push_submodule_parent", kind="bool",
        hint_text="after sync, commit+push any parent whose only dirt is the bumped submodule (default: on)"),
    WorkspaceMenuRow(
        label="Fetch on manual refresh",
        attr_name="fetch_on_manual_refresh", kind="bool",
        hint_text="Ctrl+R does `git fetch --all` per repo so ahead/behind is fresh; off keeps Ctrl+R instant + offline (default: off)"),
    WorkspaceMenuRow(label="DISPLAY", attr_name="", kind="header"),
    WorkspaceMenuRow(
        label="Name truncation",
        attr_name="name_truncation", kind="trunc_mode",
        hint_text="where to drop characters when a repo name overflows (default: middle)"),
    WorkspaceMenuRow(
        label="Branch truncation",
        attr_name="branch_truncation", kind="trunc_mode",
        hint_text="where to drop characters when a branch name overflows (default: middle)"),
    WorkspaceMenuRow(
        label="Task name truncation",
        attr_name="task_name_truncation", kind="trunc_mode",
        hint_text="where to drop characters when a task name overflows (default: middle)"),
    WorkspaceMenuRow(
        label="Name display max",
        attr_name="name_display_max", kind="int",
        min_value=0, max_value=200, step=2,
        hint_text="max width of the repo name column, in cells (default: 40)"),
    # -1 means "share the parent's cap" — that's the sentinel the
    # draw layer uses; values >= 0 truncate child rows independently.
    WorkspaceMenuRow(
        label="Child name display max",
        attr_name="child_name_display_max", kind="int",
        min_value=-1, max_value=200, step=2,
        hint_text="max width of sub-repo name column, -1 = inherit parent (default: -1)"),
    WorkspaceMenuRow(
        label="Branch display max",
        attr_name="branch_display_max", kind="int",
        min_value=0, max_value=80, step=1,
        hint_text="max width of the branch column, in cells (default: 12)"),
    WorkspaceMenuRow(
        label="Task name display max",
        attr_name="task_name_display_max", kind="int",
        min_value=0, max_value=80, step=1,
        hint_text="max width of the task-name column, in cells (default: 16)"),
    WorkspaceMenuRow(
        label="Max visible repo rows",
        attr_name="max_visible_repo_rows", kind="int",
        min_value=0, max_value=200, step=1,
        hint_text="how many repo rows to show, 0 = use full available height (default: 0)"),
    WorkspaceMenuRow(label="COMMIT SUGGESTIONS", attr_name="", kind="header"),
    WorkspaceMenuRow(
        label="Suggest added",
        attr_name="suggest_added", kind="int",
        min_value=0, max_value=99, step=1,
        hint_text="number of added files to suggest, 0 = unlimited (default: 5)"),
    WorkspaceMenuRow(
        label="Suggest updated",
        attr_name="suggest_updated", kind="int",
        min_value=0, max_value=99, step=1,
        hint_text="number of updated files to suggest, 0 = unlimited (default: 5)"),
    WorkspaceMenuRow(
        label="Suggest deleted",
        attr_name="suggest_deleted", kind="int",
        min_value=0, max_value=99, step=1,
        hint_text="number of deleted files to suggest, 0 = unlimited (default: 5)"),
)

# Modal sizing.
MODAL_W = 80
BODY_TARGET_ROWS = 14  # rows visible before scroll arrows kick in


# ---------- Open ----------------------------------------------------------


def _build_rows(ws: Workspace) -> List[WorkspaceMenuRow]:
    """Compose the modal's row list — folders section header, one row
    per existing folder, the "+ Add folder" sentinel, then the static
    overrides schema. Folder row `attr_name` carries the index back into
    `ws.folders` so the handler can edit the right entry."""
    rows: List[WorkspaceMenuRow] = []
    # When the workspace is ephemeral (auto-minted from cwd), expose
    # a one-shot promote-to-permanent row at the top so the user can
    # save it without leaving the menu. Persisted workspaces don't
    # need this — the row is hidden once the flag is cleared.
    if ws.ephemeral:
        rows.append(WorkspaceMenuRow(
            label="EPHEMERAL WORKSPACE", attr_name="", kind="header"))
        rows.append(WorkspaceMenuRow(
            label="+ Save as workspace…",
            attr_name="", kind="save_ephemeral"))
    rows.append(WorkspaceMenuRow(
        label="FOLDERS", attr_name="", kind="header"))
    for i in range(len(ws.folders)):
        rows.append(WorkspaceMenuRow(
            label=f"folder {i + 1}", attr_name=str(i), kind="folder"))
    rows.append(WorkspaceMenuRow(
        label="+ Add folder…", attr_name="", kind="add_folder"))
    rows.append(WorkspaceMenuRow(
        label="+ Clone repository…", attr_name="", kind="clone"))
    # FILE WATCH IGNORE: gitignore-style patterns that suppress
    # fs-watcher auto-refresh for matching paths. Empty by default;
    # the section header still shows so the user can find the entry
    # point ("+ Add pattern…") without scrolling through overrides.
    rows.append(WorkspaceMenuRow(
        label="FILE WATCH IGNORE", attr_name="", kind="header"))
    for i in range(len(ws.fs_watch_ignore)):
        rows.append(WorkspaceMenuRow(
            label=f"pattern {i + 1}", attr_name=str(i),
            kind="ignore_pattern"))
    rows.append(WorkspaceMenuRow(
        label="+ Add pattern…", attr_name="",
        kind="add_ignore_pattern",
        hint_text="gitignore syntax — *.log, build/**, !keep.log, "
                  "leading / anchors, trailing / for dirs"))
    rows.extend(_OVERRIDE_ROWS)
    return rows


def open_workspace_menu(state: State) -> None:
    """Build and install the workspace settings modal. Cursor lands on
    the first non-header row so the user starts on something
    interactive (typically the first folder)."""
    if not state.workspaces:
        return
    ws = state.active_workspace
    if ws is None:
        return
    rows = _build_rows(ws)
    drafts = [WorkspaceDraft(path_text=str(p)) for p in ws.folders]
    selected = 0
    for i, row in enumerate(rows):
        if row.kind != "header":
            selected = i
            break
    state.workspace_menu = WorkspaceMenu(
        rows=rows, selected=selected, scroll=0,
        path_drafts=drafts,
    )
    # Kick off an initial check on each existing folder so the user
    # sees a tick / count as soon as the modal opens. Background.
    for d in drafts:
        _kick_off_path_check(d)


def _rebuild_rows(state: State) -> None:
    """Re-derive the row list after a folder add/remove/rename. Keeps
    the cursor on a sensible row — same selection if still valid, else
    the first interactive row."""
    menu = state.workspace_menu
    ws = state.active_workspace
    if menu is None or ws is None:
        return
    old_kind = ""
    old_attr = ""
    if 0 <= menu.selected < len(menu.rows):
        old_kind = menu.rows[menu.selected].kind
        old_attr = menu.rows[menu.selected].attr_name
    menu.rows = _build_rows(ws)
    # Try to land on a row matching the previous kind+attr (e.g. the
    # same folder index). Falls back to the nearest interactive row.
    new_idx = -1
    for i, row in enumerate(menu.rows):
        if row.kind == old_kind and row.attr_name == old_attr:
            new_idx = i
            break
    if new_idx == -1:
        for i, row in enumerate(menu.rows):
            if _is_focusable(row):
                new_idx = i
                break
    if new_idx == -1:
        new_idx = 0
    menu.selected = new_idx


# ---------- Path validation worker ---------------------------------------


def _kick_off_path_check(draft: WorkspaceDraft) -> None:
    """Spawn a daemon thread that resolves `draft.path_text`, runs
    `discover_repos`, and stamps the result back. Mirrors the creator's
    live-validation worker so the menu shows the same tick / repo count
    next to each folder row."""
    text = draft.path_text.strip()
    if not text:
        draft.last_checked = draft.path_text
        draft.repo_count = -1
        draft.error = ""
        draft.checking = False
        return
    target = draft.path_text
    draft.checking = True

    def worker() -> None:
        repo_count = -1
        error = ""
        try:
            p = Path(text).expanduser()
            if not p.is_absolute():
                p = p.resolve()
            if not p.exists():
                error = "(no such folder)"
            elif not p.is_dir():
                error = "(not a folder)"
            else:
                try:
                    repos = discover_repos(p)
                except OSError as e:
                    error = f"(error: {e.strerror or e})"
                else:
                    repo_count = len(repos)
        except (OSError, RuntimeError) as e:
            error = f"(error: {e})"
        if draft.path_text == target:
            draft.repo_count = repo_count
            draft.error = error
            draft.last_checked = target
            draft.checking = False

    threading.Thread(target=worker, daemon=True).start()


def tick_menu_path_checks(state: State) -> bool:
    """Re-spawn discover_repos checks for any drift between
    `path_text` and `last_checked`. Returns True while any check is
    pending so the main loop can keep the spinner ticking. Called from
    the animation tick when the menu is open."""
    menu = state.workspace_menu
    if menu is None:
        return False
    any_checking = False
    for d in menu.path_drafts:
        if d.path_text != d.last_checked and not d.checking:
            _kick_off_path_check(d)
        if d.checking:
            any_checking = True
    return any_checking


# ---------- Effective-value helpers --------------------------------------


def _state_attr_for(row: WorkspaceMenuRow) -> str:
    return WORKSPACE_OVERRIDE_TARGETS.get(row.attr_name, row.attr_name)


def _read_value(state: State, row: WorkspaceMenuRow):
    return getattr(state, _state_attr_for(row), None)


def _write_value(state: State, row: WorkspaceMenuRow, value) -> None:
    setattr(state, _state_attr_for(row), value)
    ws = state.active_workspace
    if ws is None:
        return
    ws.overrides[row.attr_name] = value
    _persist(state)


def _clear_override(state: State, row: WorkspaceMenuRow) -> None:
    ws = state.active_workspace
    if ws is None or row.attr_name not in ws.overrides:
        return
    del ws.overrides[row.attr_name]
    cfg = state.base_config
    if cfg is not None:
        base = base_value_for_override(cfg, row.attr_name)
        if base is not None:
            setattr(state, _state_attr_for(row),
                    state_attr_value_from_override(row.attr_name, base))
    _persist(state)


def _is_overridden(state: State, row: WorkspaceMenuRow) -> bool:
    ws = state.active_workspace
    return ws is not None and row.attr_name in ws.overrides


def _persist(state: State) -> None:
    try:
        save_workspaces(state.workspaces, state.active_workspace_index)
    except OSError:
        pass


def _save_ephemeral_workspace(state: State) -> None:
    """Promote the active ephemeral workspace to a persisted one.
    Flips the `ephemeral` flag off so `save_workspaces` will include
    it on the next write, then writes the workspaces file
    immediately. Rebuilds the menu rows so the "Save as workspace…"
    entry disappears and the title brackets vanish.

    Surfaces a task confirming the action so the user sees feedback
    in the sidebar — silent state changes feel broken on a TUI."""
    ws = state.active_workspace
    if ws is None or not ws.ephemeral:
        return
    ws.ephemeral = False
    # Mirror the bracketed-name → bare-name flip onto the live State
    # field too, otherwise the title row keeps rendering "[name]"
    # until the next workspace switch.
    state.workspace_name = ws.display_name
    t = state.tasks.add(f"save workspace: {ws.name}")
    try:
        save_workspaces(state.workspaces, state.active_workspace_index)
    except OSError as e:
        # Roll back the flip on disk failure so a retried save can
        # try again rather than leaving an in-memory state that
        # disagrees with the persisted file.
        ws.ephemeral = True
        state.workspace_name = ws.display_name
        state.tasks.update(t, "fail", f"could not write: {e}")
        _rebuild_rows(state)
        return
    state.tasks.update(t, "ok", "added to idlegit.workspaces")
    _rebuild_rows(state)


# ---------- Folder-row mutation -----------------------------------------


def _commit_folder_edit(state: State, idx: int, raw: str) -> bool:
    """Set ws.folders[idx] (or append if idx == len) to the resolved
    Path of `raw`. Returns True if the edit was applied (text was
    non-empty); False otherwise (caller should treat as a no-op).

    `raw` is taken straight from the edit buffer; we expand `~` and
    resolve relative paths against CWD, the same as the creator wizard."""
    ws = state.active_workspace
    if ws is None:
        return False
    text = raw.strip()
    if not text:
        return False
    try:
        p = Path(text).expanduser()
        if not p.is_absolute():
            p = p.resolve()
    except (OSError, RuntimeError):
        return False
    if idx == len(ws.folders):
        ws.folders.append(p)
    else:
        ws.folders[idx] = p
    _persist(state)
    return True


def _commit_ignore_pattern_edit(state: State, idx: int, raw: str) -> bool:
    """Set or append `ws.fs_watch_ignore[idx]` to `raw` (trimmed),
    persist, and mirror the change onto `state.fs_watch_ignore` so
    the live watcher picks it up on the next event (RepoWatcher
    re-checks the patterns tuple per event and recompiles its
    PathSpec on change). Returns True if the edit applied; False on
    an empty buffer (caller treats as a no-op cancel)."""
    ws = state.active_workspace
    if ws is None:
        return False
    text = raw.strip()
    if not text:
        return False
    if idx == len(ws.fs_watch_ignore):
        ws.fs_watch_ignore.append(text)
    else:
        ws.fs_watch_ignore[idx] = text
    state.fs_watch_ignore = list(ws.fs_watch_ignore)
    _persist(state)
    return True


def _remove_ignore_pattern(state: State, idx: int) -> bool:
    """Drop `ws.fs_watch_ignore[idx]`, persist, mirror onto State.
    Unlike folders, there's no "can't remove the last" guard — an
    empty pattern list is the default and reverts the watcher to
    its un-filtered behaviour."""
    ws = state.active_workspace
    if ws is None:
        return False
    if idx < 0 or idx >= len(ws.fs_watch_ignore):
        return False
    del ws.fs_watch_ignore[idx]
    state.fs_watch_ignore = list(ws.fs_watch_ignore)
    _persist(state)
    return True


def _remove_folder(state: State, idx: int) -> bool:
    """Drop ws.folders[idx]. Refuses to remove the last folder (a
    workspace without any folders is meaningless) so the user can't
    paint themselves into a corner. Returns True when removal happened."""
    ws = state.active_workspace
    if ws is None:
        return False
    if idx < 0 or idx >= len(ws.folders):
        return False
    if len(ws.folders) <= 1:
        return False
    del ws.folders[idx]
    _persist(state)
    return True


# ---------- Edit-mode helpers --------------------------------------------


def _enter_edit_mode(menu: WorkspaceMenu, initial_text: str) -> None:
    menu.editing = True
    menu.edit_buffer = initial_text
    menu.edit_cursor = len(initial_text)


def _exit_edit_mode(menu: WorkspaceMenu) -> None:
    menu.editing = False
    menu.edit_buffer = ""
    menu.edit_cursor = 0


def _draft_for_row(menu: WorkspaceMenu,
                   row: WorkspaceMenuRow) -> Optional[WorkspaceDraft]:
    if row.kind != "folder":
        return None
    try:
        idx = int(row.attr_name)
    except ValueError:
        return None
    if 0 <= idx < len(menu.path_drafts):
        return menu.path_drafts[idx]
    return None


# ---------- Override-row edit operations --------------------------------


def _cycle_trunc(value: str, direction: int) -> str:
    if value not in TRUNCATION_MODES:
        return TRUNCATION_MODES[0]
    i = TRUNCATION_MODES.index(value)
    return TRUNCATION_MODES[(i + direction) % len(TRUNCATION_MODES)]


def _bump_int(row: WorkspaceMenuRow, value: int, direction: int) -> int:
    try:
        cur = int(value)
    except (TypeError, ValueError):
        cur = row.min_value
    nxt = cur + direction * row.step
    return max(row.min_value, min(row.max_value, nxt))


def _adjust(state: State, row: WorkspaceMenuRow, direction: int) -> None:
    cur = _read_value(state, row)
    if row.kind == "bool":
        return
    if row.kind == "trunc_mode":
        nxt = _cycle_trunc(str(cur or ""), direction)
    elif row.kind == "int":
        nxt = _bump_int(row, cur if cur is not None else row.min_value, direction)
    else:
        return
    if nxt != cur:
        _write_value(state, row, nxt)


def _toggle_bool(state: State, row: WorkspaceMenuRow) -> None:
    if row.kind != "bool":
        return
    cur = _read_value(state, row)
    _write_value(state, row, not bool(cur))


# ---------- Draw ----------------------------------------------------------


def _format_value(state: State, row: WorkspaceMenuRow) -> str:
    cur = _read_value(state, row)
    if row.kind == "bool":
        return "[x]" if cur else "[ ]"
    if row.kind == "trunc_mode":
        return f"‹ {cur or '?'} ›"
    if row.kind == "int":
        try:
            return f"‹ {int(cur)} ›"
        except (TypeError, ValueError):
            return "‹ ? ›"
    return str(cur)


def _folder_status_pair(draft: WorkspaceDraft) -> Tuple[str, int]:
    """Return (status_text, color_pair) for a folder row's right-hand
    badge — mirrors the creator's per-row tick/error rendering."""
    if draft.checking:
        return ("(checking…)", 0)
    text = draft.path_text.strip()
    if not text:
        return ("", 0)
    if draft.error:
        return (draft.error, PAIR_DLG_WARN)
    if draft.repo_count > 0:
        return (f"✓ {draft.repo_count} repo"
                f"{'s' if draft.repo_count != 1 else ''}", PAIR_DLG_OK)
    if draft.repo_count == 0:
        return ("(no repos)", PAIR_DLG_WARN)
    return ("", 0)


def _is_focusable(row: WorkspaceMenuRow) -> bool:
    return row.kind != "header"


def draw_workspace_menu(stdscr, state: State, sidebar_x: int) -> None:
    menu = state.workspace_menu
    if menu is None:
        return
    n_rows = len(menu.rows)
    body_h = max(3, min(BODY_TARGET_ROWS, n_rows))
    # blank-top (1) + title (1) + blank (1) + spacer/scroll-↑ (1)
    # + body + spacer/scroll-↓ (1) + blank (1) + hint-text (1)
    # + footer (1) + blank-bottom (1). The blank under the title
    # gives the header room to breathe; the blank above hint-text
    # separates the per-row tooltip from the body without colliding
    # with the scroll-down indicator.
    content_h = 1 + 1 + 1 + 1 + body_h + 1 + 1 + 1 + 1 + 1
    x, y, w, h = modal_geometry(stdscr, sidebar_x, MODAL_W, content_h)
    sb = curses.color_pair(PAIR_DLG_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    inner_w = w - 4

    ws = state.active_workspace
    title = (
        f"Workspace settings — {ws.display_name if ws else '(no workspace)'}")
    safe_addstr(stdscr, y + 1, inner_x, title[:inner_w],
                curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN))

    if menu.selected < menu.scroll:
        menu.scroll = menu.selected
    elif menu.selected >= menu.scroll + body_h:
        menu.scroll = menu.selected - body_h + 1
    menu.scroll = max(0, min(menu.scroll, max(0, n_rows - body_h)))

    # Right-hand value column reserves 28 cells (chevrons / [x] /
    # status badge); labels fill the rest minus the focus prefix.
    value_w = 28
    label_w = max(10, inner_w - value_w - 4)

    for i in range(body_h):
        idx = menu.scroll + i
        if idx >= n_rows:
            break
        row = menu.rows[idx]
        line_y = y + 4 + i
        focused = (idx == menu.selected)

        if row.kind == "header":
            # Section dividers render in muted cyan small-caps so the
            # eye can quickly find each block in the long row list.
            safe_addstr(stdscr, line_y, inner_x,
                        row.label.ljust(inner_w)[:inner_w],
                        curses.color_pair(PAIR_DLG_CYAN) | curses.A_DIM)
            continue

        if row.kind == "folder":
            _draw_folder_row(stdscr, line_y, inner_x, inner_w, label_w,
                             menu, row, focused, sb)
            continue

        if row.kind == "ignore_pattern":
            _draw_ignore_pattern_row(stdscr, line_y, inner_x, inner_w,
                                     state, menu, row, focused, sb)
            continue

        if row.kind in ("add_folder", "add_ignore_pattern",
                        "clone", "save_ephemeral"):
            prefix = "→ " if focused else "  "
            text = (prefix + row.label).ljust(inner_w)[:inner_w]
            attr = (curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
                    if focused else
                    curses.color_pair(PAIR_DLG_CYAN) | curses.A_DIM)
            if focused:
                attr |= curses.A_REVERSE
            safe_addstr(stdscr, line_y, inner_x, text, attr)
            continue

        # Override row.
        prefix = "→ " if focused else "  "
        label = (prefix + row.label).ljust(label_w)
        value_text = _format_value(state, row)
        overridden = _is_overridden(state, row)
        hint = "" if overridden else " (default)"

        attr = sb | curses.A_REVERSE if focused else sb
        safe_addstr(stdscr, line_y, inner_x, label, attr)

        value_x = inner_x + label_w + 2
        if row.kind == "bool":
            val_attr = (curses.color_pair(PAIR_DLG_OK) if _read_value(state, row)
                        else sb | curses.A_DIM)
        elif overridden:
            val_attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
        else:
            val_attr = sb
        if focused:
            val_attr |= curses.A_REVERSE
        safe_addstr(stdscr, line_y, value_x, value_text, val_attr)
        if hint:
            safe_addstr(stdscr, line_y,
                        value_x + len(value_text), hint,
                        sb | curses.A_DIM)

    if menu.scroll > 0:
        draw_scroll_overflow(stdscr, y + 3, inner_x, inner_w,
                             menu.scroll, "up", sb | curses.A_DIM)
    if menu.scroll + body_h < n_rows:
        below = n_rows - (menu.scroll + body_h)
        draw_scroll_overflow(stdscr, y + 4 + body_h, inner_x, inner_w,
                             below, "down", sb | curses.A_DIM)

    # Per-row explainer — sits two rows above the hints footer with a
    # blank row above and below, only when the focused row carries a
    # hint_text. Header rows aren't focusable, so we won't end up
    # rendering blank text on a section break.
    hint_text = ""
    if 0 <= menu.selected < n_rows:
        hint_text = menu.rows[menu.selected].hint_text
    if hint_text:
        # Mid-grey (`PAIR_DLG_FG_HINT_TEXT`) so the explainer reads a
        # shade lighter than the dim key labels in the hints footer
        # immediately below, but doesn't go all the way to bright
        # white — visually distinct as "this row's tooltip" vs
        # "global keymap reminders".
        safe_addstr(stdscr, y + h - 3, inner_x,
                    truncate(hint_text, inner_w, "end"),
                    curses.color_pair(PAIR_DLG_FG_HINT_TEXT))

    _draw_menu_hints(stdscr, state, menu, y + h - 2, inner_x, inner_w,
                     sb | curses.A_DIM)


def _draw_ignore_pattern_row(stdscr, line_y: int, inner_x: int,
                             inner_w: int, state: State,
                             menu: WorkspaceMenu, row: WorkspaceMenuRow,
                             focused: bool, sb: int) -> None:
    """Render one fs_watch_ignore pattern row — the pattern string on
    the left, in edit mode swapped for the live edit buffer. No badge
    (unlike folders, there's nothing to validate live — pathspec just
    consumes the line as a gitignore pattern)."""
    ws = state.active_workspace
    try:
        idx = int(row.attr_name)
    except ValueError:
        idx = -1
    current = (ws.fs_watch_ignore[idx]
               if ws is not None and 0 <= idx < len(ws.fs_watch_ignore)
               else "")
    prefix = "→ " if focused else "  "
    field_w = max(20, inner_w - len(prefix))

    if focused and menu.editing:
        text = menu.edit_buffer
        cur = max(0, min(menu.edit_cursor, len(text)))
        if len(text) <= field_w - 1:
            visible = text
            cur_x_off = cur
        else:
            half = (field_w - 1) // 2
            start = max(0, min(cur - half, len(text) - (field_w - 1)))
            visible = text[start:start + field_w - 1]
            cur_x_off = cur - start
        body = visible.ljust(field_w)
        attr = (curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
                | curses.A_UNDERLINE)
        safe_addstr(stdscr, line_y, inner_x, prefix, attr)
        safe_addstr(stdscr, line_y, inner_x + len(prefix), body, attr)
        try:
            stdscr.move(line_y, inner_x + len(prefix) + cur_x_off)
            curses.curs_set(2)
        except curses.error:
            pass
        return

    visible = truncate(current, field_w, "end")
    body = visible.ljust(field_w)
    attr = sb | curses.A_REVERSE if focused else sb
    safe_addstr(stdscr, line_y, inner_x,
                (prefix + body).ljust(inner_w)[:inner_w], attr)


def _draw_folder_row(stdscr, line_y: int, inner_x: int, inner_w: int,
                     label_w: int, menu: WorkspaceMenu,
                     row: WorkspaceMenuRow, focused: bool, sb: int) -> None:
    """Render one folder row — the path field on the left, a tick /
    repo-count badge on the right. When this row is in edit mode the
    field shows the live edit buffer plus a trailing `_` cursor."""
    draft = _draft_for_row(menu, row)
    prefix = "→ " if focused else "  "
    # Field width: take what the label column would have used + part
    # of the value column, leaving 16 cells for the status badge.
    badge_w = 16
    field_w = max(20, inner_w - len(prefix) - badge_w - 1)

    if focused and menu.editing:
        text = menu.edit_buffer
        # Live cursor render: append a `_` glyph at edit_cursor.
        cur = max(0, min(menu.edit_cursor, len(text)))
        if len(text) <= field_w - 1:
            visible = text
            cur_x_off = cur
        else:
            half = (field_w - 1) // 2
            start = max(0, min(cur - half, len(text) - (field_w - 1)))
            visible = text[start:start + field_w - 1]
            cur_x_off = cur - start
        body = visible.ljust(field_w)
        attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD | curses.A_UNDERLINE
        safe_addstr(stdscr, line_y, inner_x, prefix, attr)
        safe_addstr(stdscr, line_y, inner_x + len(prefix), body, attr)
        try:
            stdscr.move(line_y, inner_x + len(prefix) + cur_x_off)
            curses.curs_set(2)
        except curses.error:
            pass
    else:
        text = draft.path_text if draft is not None else ""
        visible = truncate(text, field_w, "middle")
        body = visible.ljust(field_w)
        if focused:
            attr = sb | curses.A_REVERSE
        else:
            attr = sb
        safe_addstr(stdscr, line_y, inner_x,
                    (prefix + body).ljust(inner_w)[:inner_w], attr)

    if draft is not None:
        status_text, status_pair = _folder_status_pair(draft)
        if status_text:
            badge_x = inner_x + len(prefix) + field_w + 1
            badge_attr = (curses.color_pair(status_pair) if status_pair
                          else sb | curses.A_DIM)
            if focused and not menu.editing:
                badge_attr |= curses.A_REVERSE
            safe_addstr(stdscr, line_y, badge_x,
                        status_text[:badge_w], badge_attr)


# ---------- Handle --------------------------------------------------------


def _move_selected(menu: WorkspaceMenu, direction: int) -> None:
    """Jump `selected` to the next focusable row in the requested
    direction (skipping headers). Wraps within bounds rather than
    around the list — wraparound on a long settings list felt
    disorienting in testing."""
    if not menu.rows:
        return
    n = len(menu.rows)
    new = menu.selected
    while True:
        new += direction
        if new < 0 or new >= n:
            return
        if _is_focusable(menu.rows[new]):
            menu.selected = new
            return


def _focused_row(menu: WorkspaceMenu) -> Optional[WorkspaceMenuRow]:
    if 0 <= menu.selected < len(menu.rows):
        return menu.rows[menu.selected]
    return None


def handle_workspace_menu_key(state: State, key: int) -> None:
    menu = state.workspace_menu
    if menu is None:
        return

    # ---- Edit mode: typing edits the path buffer ----
    if menu.editing:
        _handle_edit_key(state, menu, key)
        return

    if key in (27, 9):  # Esc or Tab — both close the modal
        state.workspace_menu = None
        return
    if not menu.rows:
        return

    if key == curses.KEY_UP:
        _move_selected(menu, -1)
        return
    if key == curses.KEY_DOWN:
        _move_selected(menu, +1)
        return
    if key == curses.KEY_PPAGE:
        for _ in range(5):
            _move_selected(menu, -1)
        return
    if key == curses.KEY_NPAGE:
        for _ in range(5):
            _move_selected(menu, +1)
        return
    if key == curses.KEY_HOME:
        for i, row in enumerate(menu.rows):
            if _is_focusable(row):
                menu.selected = i
                break
        return
    if key == curses.KEY_END:
        for i in range(len(menu.rows) - 1, -1, -1):
            if _is_focusable(menu.rows[i]):
                menu.selected = i
                break
        return

    row = _focused_row(menu)
    if row is None:
        return

    if row.kind == "folder":
        if key in (10, 13, curses.KEY_ENTER):
            draft = _draft_for_row(menu, row)
            _enter_edit_mode(menu, draft.path_text if draft else "")
            return
        if key in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_DC):
            try:
                idx = int(row.attr_name)
            except ValueError:
                return
            if _remove_folder(state, idx):
                # Drop the matching draft, then rebuild rows so the
                # subsequent folders renumber.
                if 0 <= idx < len(menu.path_drafts):
                    del menu.path_drafts[idx]
                _rebuild_rows(state)
            return
        return

    if row.kind == "add_folder":
        if key in (10, 13, curses.KEY_ENTER, ord(" ")):
            _enter_edit_mode(menu, "")
            return
        return

    if row.kind == "ignore_pattern":
        if key in (10, 13, curses.KEY_ENTER):
            ws = state.active_workspace
            if ws is None:
                return
            try:
                idx = int(row.attr_name)
            except ValueError:
                return
            current = (ws.fs_watch_ignore[idx]
                       if 0 <= idx < len(ws.fs_watch_ignore) else "")
            _enter_edit_mode(menu, current)
            return
        if key in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_DC):
            try:
                idx = int(row.attr_name)
            except ValueError:
                return
            if _remove_ignore_pattern(state, idx):
                _rebuild_rows(state)
            return
        return

    if row.kind == "add_ignore_pattern":
        if key in (10, 13, curses.KEY_ENTER, ord(" ")):
            _enter_edit_mode(menu, "")
            return
        return

    if row.kind == "clone":
        if key in (10, 13, curses.KEY_ENTER, ord(" ")):
            from .clone import open_clone_modal
            open_clone_modal(state)
            return
        return

    if row.kind == "save_ephemeral":
        if key in (10, 13, curses.KEY_ENTER, ord(" ")):
            _save_ephemeral_workspace(state)
            return
        return

    # Override row.
    if key == curses.KEY_LEFT:
        _adjust(state, row, -1)
        return
    if key == curses.KEY_RIGHT:
        _adjust(state, row, +1)
        return
    if key == ord(" "):
        _toggle_bool(state, row)
        return
    if key in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_DC):
        _clear_override(state, row)
        return
    if key in (10, 13, curses.KEY_ENTER):
        if row.kind == "bool":
            _toggle_bool(state, row)
        else:
            _adjust(state, row, +1)
        return


def _handle_edit_key(state: State, menu: WorkspaceMenu, key: int) -> None:
    """Dispatch keystrokes while a folder/add_folder row is in inline
    edit mode. Esc bails without persisting; Enter commits to ws.folders
    and refreshes the row list (so a fresh "+ Add folder" sentinel slots
    in below the new entry)."""
    if key == 27:
        _exit_edit_mode(menu)
        return

    text = menu.edit_buffer
    cur = max(0, min(menu.edit_cursor, len(text)))

    if key in (10, 13, curses.KEY_ENTER):
        row = _focused_row(menu)
        if row is None:
            _exit_edit_mode(menu)
            return
        ws = state.active_workspace
        if ws is None:
            _exit_edit_mode(menu)
            return
        # Determine target index + dispatch by row kind. Folder rows
        # carry their index in attr_name; the add_folder sentinel
        # commits at len(ws.folders). The ignore-pattern rows follow
        # the same pattern against ws.fs_watch_ignore.
        if row.kind == "folder":
            try:
                idx = int(row.attr_name)
            except ValueError:
                _exit_edit_mode(menu)
                return
        elif row.kind == "add_folder":
            idx = len(ws.folders)
        elif row.kind == "ignore_pattern":
            try:
                idx = int(row.attr_name)
            except ValueError:
                _exit_edit_mode(menu)
                return
            if not _commit_ignore_pattern_edit(state, idx, text):
                _exit_edit_mode(menu)
                return
            _exit_edit_mode(menu)
            _rebuild_rows(state)
            return
        elif row.kind == "add_ignore_pattern":
            idx = len(ws.fs_watch_ignore)
            if not _commit_ignore_pattern_edit(state, idx, text):
                _exit_edit_mode(menu)
                return
            _exit_edit_mode(menu)
            _rebuild_rows(state)
            return
        else:
            _exit_edit_mode(menu)
            return
        if not _commit_folder_edit(state, idx, text):
            _exit_edit_mode(menu)
            return
        # Sync the live drafts list so live checks rerun for the
        # mutated entry; rebuild rows so a new add_folder sentinel
        # lands below an append.
        if idx == len(menu.path_drafts):
            menu.path_drafts.append(WorkspaceDraft(path_text=text))
        else:
            menu.path_drafts[idx] = WorkspaceDraft(path_text=text)
        _kick_off_path_check(menu.path_drafts[idx])
        _exit_edit_mode(menu)
        _rebuild_rows(state)
        return

    if key == curses.KEY_LEFT:
        menu.edit_cursor = max(0, cur - 1)
        return
    if key == curses.KEY_RIGHT:
        menu.edit_cursor = min(len(text), cur + 1)
        return
    if key == curses.KEY_HOME or key == 1:
        menu.edit_cursor = 0
        return
    if key == curses.KEY_END or key == 5:
        menu.edit_cursor = len(text)
        return
    if key in (curses.KEY_BACKSPACE, 127, 8):
        if cur > 0:
            menu.edit_buffer = text[: cur - 1] + text[cur:]
            menu.edit_cursor = cur - 1
        return
    if key == curses.KEY_DC:
        if cur < len(text):
            menu.edit_buffer = text[:cur] + text[cur + 1:]
        return
    if 32 <= key < 127:
        menu.edit_buffer = text[:cur] + chr(key) + text[cur:]
        menu.edit_cursor = cur + 1
        return


# Keep these imports referenced so static analysers don't strip them.
_ = (List, Tuple, WORKSPACE_OVERRIDE_TYPES)
