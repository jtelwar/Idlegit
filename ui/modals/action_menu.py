"""Tab-on-row action menu — fetch/pull/push/switch-branch/soft-reset/
run-a-workflow against the focused repo or submodule child.

The bottom half of the modal is a tabbed pane: a working-tree view
(every changed/untracked file with status + ins/del counts) and a
commits view (paged in lazily as the user scrolls back). Each pane
has its own filter row at the top. Down off the last action item
moves focus into the pane; Up off the filter row moves it back.
Home jumps to the first action item from anywhere."""
from __future__ import annotations

import curses
import threading
from typing import List, Optional

from models import (
    ActionMenu, ActionMenuItem, ChildRef, CommitEntry, FileEntry,
    Repo, State,
)
from git_ops import (
    gh_available, load_commits, parse_github_slug, query_target_state,
    query_working_tree,
)
from workers import kick_off_action

from ..colors import (
    PAIR_AHEAD, PAIR_BEHIND, PAIR_BRANCH, PAIR_DIRTY, PAIR_ERR, PAIR_OK,
    PAIR_PASTEL_BLUE, PAIR_PASTEL_GREEN, PAIR_PASTEL_RED, PAIR_PASTEL_YELLOW,
    PAIR_SB_CYAN, PAIR_SB_FG,
)
from ..geometry import draw_modal_fill, modal_geometry, safe_addstr, truncate


# Map a porcelain XY status pair to a pastel colour pair. Ordered so
# the most-impactful change (delete > add > rename > modify) wins —
# a "DM" file (deleted-staged, then re-modified in-tree) reads as a
# delete first, which matches what the user is going to commit.
def _file_status_pair(x: str, y: str) -> Optional[int]:
    pair = (x, y)
    if "U" in pair or pair == ("A", "A") or pair == ("D", "D"):
        return PAIR_PASTEL_RED
    if "D" in pair:
        return PAIR_PASTEL_RED
    if "A" in pair:
        return PAIR_PASTEL_GREEN
    if "R" in pair:
        return PAIR_PASTEL_BLUE
    if "M" in pair:
        return PAIR_PASTEL_YELLOW
    return None


# Number of commits to load per page. First page fires synchronously
# inside open_action_menu; subsequent pages fire from a worker thread
# when the cursor scrolls within PREFETCH_THRESHOLD of the loaded end.
COMMITS_PAGE = 30
PREFETCH_THRESHOLD = 5

# Modal sizing.
MODAL_W = 90
PANE_TARGET_ROWS = 12  # rows visible in the bottom pane (cap)


# ---------- Open ----------------------------------------------------------


def open_action_menu(state: State) -> None:
    """Build and install the ActionMenu modal for the focused row."""
    if state.on_toggle:
        return
    cur_repo = state.current_repo
    cur_child = state.current_child

    target_repo: Optional[Repo] = None
    target_parent: Optional[Repo] = None
    target_child: Optional[ChildRef] = None
    if cur_repo is not None:
        label = cur_repo.display_name
        target_path = cur_repo.path
        target_repo = cur_repo
    elif cur_child is not None and cur_child[1].kind == "submodule":
        parent, child = cur_child
        label = f"↳ {child.repo.display_name} in {parent.display_name}"
        target_path = child.nested_path
        target_parent = parent
        target_child = child
    else:
        return

    ts = query_target_state(target_path)

    workflows_repo = target_repo or (target_child.repo if target_child else None)
    has_any_workflow = False
    if workflows_repo is not None and gh_available():
        slug = parse_github_slug(workflows_repo.remote_url_raw)
        if slug:
            has_any_workflow = bool(workflows_repo.workflows)
    if has_any_workflow:
        run_reason = ""
    elif workflows_repo is None or not gh_available():
        run_reason = "gh CLI / repo unavailable"
    elif workflows_repo is not None and not parse_github_slug(
            workflows_repo.remote_url_raw):
        run_reason = "no github remote"
    else:
        run_reason = "no workflows in this repo"

    items = [
        ActionMenuItem(
            id="fetch", label="fetch (all branches)",
            enabled=ts.has_origin,
            reason="" if ts.has_origin else "no origin"),
        ActionMenuItem(
            id="pull", label="pull",
            enabled=ts.has_origin and ts.upstream is not None and not ts.merging,
            reason=("merging" if ts.merging
                    else ("no upstream" if ts.upstream is None
                          else ("" if ts.has_origin else "no origin")))),
        ActionMenuItem(
            id="switch_branch", label="switch branch…",
            enabled=not ts.merging,
            reason="" if not ts.merging else "merging"),
        ActionMenuItem(
            id="soft_reset",
            label=f"soft reset ({ts.ahead} unpushed)…",
            enabled=ts.ahead > 0,
            reason="" if ts.ahead > 0 else "no unpushed commits"),
        ActionMenuItem(
            id="push", label="push",
            enabled=ts.has_origin,
            reason="" if ts.has_origin else "no origin"),
        ActionMenuItem(
            id="run_workflow", label="run a workflow…",
            enabled=has_any_workflow, reason=run_reason),
    ]

    initial = 0
    for i, it in enumerate(items):
        if it.enabled:
            initial = i
            break

    if ts.merging:
        state_label, state_pair = "merging", PAIR_ERR
    elif ts.ahead > 0 and ts.behind > 0:
        state_label, state_pair = "diverged", PAIR_ERR
    elif ts.dirty:
        state_label, state_pair = "dirty", PAIR_DIRTY
    elif ts.behind > 0:
        state_label, state_pair = "behind", PAIR_BEHIND
    elif ts.ahead > 0:
        state_label, state_pair = "ahead", PAIR_AHEAD
    elif ts.upstream is None:
        state_label, state_pair = "no upstream", 0
    else:
        state_label, state_pair = "clean", PAIR_OK

    tree_files = query_working_tree(target_path)
    commits, exhausted = load_commits(target_path, 0, COMMITS_PAGE)

    state.action_menu = ActionMenu(
        target_label=label,
        target_path=target_path,
        target_repo=target_repo,
        target_parent=target_parent,
        target_child=target_child,
        branch=ts.branch,
        upstream=ts.upstream,
        ahead=ts.ahead,
        behind=ts.behind,
        state_label=state_label,
        state_pair=state_pair,
        items=items,
        selected=initial,
        tree_files=tree_files,
        commits_full=commits,
        commits_exhausted=exhausted,
    )


# ---------- Filtering helpers ---------------------------------------------


def _filtered_tree(menu: ActionMenu) -> List[FileEntry]:
    if not menu.tree_filter:
        return menu.tree_files
    needle = menu.tree_filter.lower()
    return [f for f in menu.tree_files if needle in f.path.lower()]


def _filtered_commits(menu: ActionMenu) -> List[CommitEntry]:
    if not menu.commits_filter:
        return menu.commits_full
    needle = menu.commits_filter.lower()
    return [c for c in menu.commits_full
            if needle in c.subject.lower() or needle in c.sha.lower()]


# ---------- Lazy-load worker ----------------------------------------------


def _kick_off_commits_page(menu: ActionMenu) -> None:
    """Spawn a daemon thread to fetch the next page of commits and
    append to `menu.commits_full`. Gated by `commits_loading` so two
    near-simultaneous scroll events don't double-fire. The worker
    checks `menu.cancel_event` before invoking gh and again before
    mutating the menu, so closing the modal during a slow `git log`
    skips the subprocess work and the post-fetch list mutation."""
    if menu.commits_loading or menu.commits_exhausted:
        return
    if menu.cancel_event.is_set():
        return
    menu.commits_loading = True
    skip = len(menu.commits_full)
    path = menu.target_path

    def worker() -> None:
        try:
            if menu.cancel_event.is_set():
                return
            page, exhausted = load_commits(path, skip, COMMITS_PAGE)
            if menu.cancel_event.is_set():
                return
            menu.commits_full.extend(page)
            menu.commits_exhausted = exhausted
        finally:
            menu.commits_loading = False

    threading.Thread(target=worker, daemon=True).start()


def _maybe_prefetch(menu: ActionMenu) -> None:
    if menu.pane_tab != "commits" or not menu.pane_focus:
        return
    visible = _filtered_commits(menu)
    # selected==0 is the filter row; subtract 1 to compare against the
    # filtered list. Prefetch when the cursor is within PREFETCH_THRESHOLD
    # rows of the loaded end and we still have more to fetch.
    list_idx = max(0, menu.commits_selected - 1)
    if list_idx >= len(visible) - PREFETCH_THRESHOLD:
        _kick_off_commits_page(menu)


# ---------- Draw ----------------------------------------------------------


def draw_action_menu(stdscr, state: State, sidebar_x: int) -> None:
    menu = state.action_menu
    if menu is None:
        return

    n_items = len(menu.items)
    # Header (title + spacer + branch + upstream + sep) = 5 rows;
    # actions = n_items rows; separator = 1; tab header = 1; filter = 1;
    # pane list = up to PANE_TARGET_ROWS; footer hint = 1; padding = 2.
    content_h = (1 + 1 + 1 + 1 + 1
                 + n_items + 1
                 + 1 + 1
                 + PANE_TARGET_ROWS
                 + 1 + 1)
    x, y, w, h = modal_geometry(stdscr, sidebar_x, MODAL_W, content_h)
    sb = curses.color_pair(PAIR_SB_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    inner_w = w - 4

    # Title row: repo name (cyan-bold) + middle-truncated full path
    # in brackets (dim) so users with multiple repository_folders can
    # tell which on-disk location this menu targets at a glance.
    name = menu.target_label
    path_str = str(menu.target_path)
    name_clip = name[:inner_w]
    safe_addstr(stdscr, y + 1, inner_x, name_clip,
                curses.A_BOLD | curses.color_pair(PAIR_SB_CYAN))
    # Reserve room for "  [<path>]" — 4 cells of fixed chrome around
    # the truncated path. min length 3 so we never collapse to "[…]".
    avail = inner_w - len(name_clip) - 4
    if avail >= 3:
        path_trunc = truncate(path_str, avail, mode="middle")
        safe_addstr(stdscr, y + 1, inner_x + len(name_clip),
                    f"  [{path_trunc}]", sb | curses.A_DIM)

    line = y + 3
    branch_str = f"[{menu.branch}]"
    safe_addstr(stdscr, line, inner_x, branch_str,
                curses.color_pair(PAIR_BRANCH))
    state_attr = (curses.color_pair(menu.state_pair)
                  if menu.state_pair else (sb | curses.A_DIM))
    safe_addstr(stdscr, line, inner_x + len(branch_str) + 1,
                f"● {menu.state_label}", state_attr)

    line += 1
    if menu.upstream:
        meta = (f"upstream: {menu.upstream}  ·  "
                f"ahead {menu.ahead} / behind {menu.behind}")
    else:
        meta = "no upstream"
    safe_addstr(stdscr, line, inner_x, meta[:inner_w], sb | curses.A_DIM)

    line += 1
    safe_addstr(stdscr, line, inner_x, "─" * inner_w, sb | curses.A_DIM)

    # Action items
    line += 1
    for i, item in enumerate(menu.items):
        focused = (i == menu.selected and not menu.pane_focus)
        prefix = "→ " if focused else "  "
        label = item.label
        if not item.enabled and item.reason:
            label = f"{label}  ({item.reason})"
        if focused and item.enabled:
            attr = sb | curses.A_REVERSE
        elif focused:
            attr = sb | curses.A_REVERSE | curses.A_DIM
        elif not item.enabled:
            attr = sb | curses.A_DIM
        else:
            attr = sb
        safe_addstr(stdscr, line, inner_x,
                    (prefix + label).ljust(inner_w), attr)
        line += 1

    # Bottom-pane separator
    safe_addstr(stdscr, line, inner_x, "─" * inner_w, sb | curses.A_DIM)
    line += 1

    # Tab header
    _draw_tab_header(stdscr, line, inner_x, inner_w, menu, sb)
    line += 1

    # Compute pane size: whatever's left between current line and the
    # footer hint row, capped at PANE_TARGET_ROWS + 1 (filter row).
    footer_y = y + h - 1
    pane_total = max(2, footer_y - line - 1)
    list_rows = pane_total - 1  # filter takes the first row

    _draw_pane(stdscr, line, inner_x, inner_w, list_rows, menu, sb)

    # Footer hint
    if menu.pane_focus:
        hint = "↑/↓ select · ←/→ tab · Home menu · Esc back"
    else:
        hint = "↑/↓ select · Enter run · ↓ pane · Esc back"
    safe_addstr(stdscr, footer_y, inner_x, hint[:inner_w], sb | curses.A_DIM)


def _draw_tab_header(stdscr, line: int, inner_x: int, inner_w: int,
                     menu: ActionMenu, sb: int) -> None:
    """Render the [ Working tree ] [ Recent commits ] tabs. Active tab
    gets cyan + bold; inactive is dim. When `pane_focus` is False the
    whole header drops a tone so the user can tell the action items
    have focus, not the pane."""
    tabs = [("tree", "Working tree", len(menu.tree_files)),
            ("commits", "Recent commits", len(menu.commits_full))]
    cur_x = inner_x
    for tid, label, count in tabs:
        active = (menu.pane_tab == tid)
        text = f" {label} ({count}) "
        if active and menu.pane_focus:
            attr = curses.color_pair(PAIR_SB_CYAN) | curses.A_BOLD
        elif active:
            attr = sb | curses.A_BOLD
        else:
            attr = sb | curses.A_DIM
        safe_addstr(stdscr, line, cur_x, text, attr)
        cur_x += len(text) + 1
        if cur_x >= inner_x + inner_w:
            break
    # Loading hint for commits paging
    if menu.commits_loading:
        msg = "  …loading more"
        safe_addstr(stdscr, line, inner_x + inner_w - len(msg),
                    msg, sb | curses.A_DIM)


def _draw_pane(stdscr, line: int, inner_x: int, inner_w: int,
               list_rows: int, menu: ActionMenu, sb: int) -> None:
    """Render the filter row + filtered list for the active tab."""
    if menu.pane_tab == "tree":
        _draw_tree_pane(stdscr, line, inner_x, inner_w, list_rows, menu, sb)
    else:
        _draw_commits_pane(stdscr, line, inner_x, inner_w, list_rows, menu, sb)


def _draw_filter_row(stdscr, line: int, inner_x: int, inner_w: int,
                     filter_text: str, focused: bool, sb: int) -> None:
    """One-line filter input prefixed with a magnifier glyph."""
    icon = "🔍 "
    typed = filter_text or ""
    if focused:
        # Reverse-video the filter row + show a `_` cursor.
        body = (typed + "_").ljust(inner_w - len(icon))
        attr = sb | curses.A_REVERSE
    elif typed:
        body = typed.ljust(inner_w - len(icon))
        attr = sb
    else:
        body = "filter…".ljust(inner_w - len(icon))
        attr = sb | curses.A_DIM
    safe_addstr(stdscr, line, inner_x, icon, sb | curses.A_DIM)
    safe_addstr(stdscr, line, inner_x + len(icon), body[:inner_w - len(icon)], attr)


def _draw_tree_pane(stdscr, line: int, inner_x: int, inner_w: int,
                    list_rows: int, menu: ActionMenu, sb: int) -> None:
    filter_focused = menu.pane_focus and menu.tree_selected == 0
    _draw_filter_row(stdscr, line, inner_x, inner_w,
                     menu.tree_filter, filter_focused, sb)
    line += 1

    files = _filtered_tree(menu)
    if not files:
        msg = "(no changes)" if not menu.tree_filter else "(no matches)"
        safe_addstr(stdscr, line, inner_x + 2, msg, sb | curses.A_DIM)
        return

    # Selection-aware scroll: keep the selected row in view.
    sel_in_list = max(0, menu.tree_selected - 1)
    if sel_in_list < menu.tree_scroll:
        menu.tree_scroll = sel_in_list
    elif sel_in_list >= menu.tree_scroll + list_rows:
        menu.tree_scroll = sel_in_list - list_rows + 1
    if menu.tree_scroll > max(0, len(files) - list_rows):
        menu.tree_scroll = max(0, len(files) - list_rows)

    visible = files[menu.tree_scroll:menu.tree_scroll + list_rows]
    for i, fe in enumerate(visible):
        idx = menu.tree_scroll + i
        focused = (menu.pane_focus and menu.tree_selected == idx + 1)
        _draw_tree_row(stdscr, line + i, inner_x, inner_w, fe, focused, sb)


def _draw_tree_row(stdscr, y: int, x: int, inner_w: int, fe: FileEntry,
                   focused: bool, sb: int) -> None:
    """Render one working-tree row with pastel overlays on the status
    code and the +ins / -del numbers. The row is laid down first as a
    single-attr base (so reverse-video for the focused row stays
    contiguous), then the colored segments are over-painted in place
    when not focused."""
    code = "??" if fe.untracked else f"{fe.x}{fe.y}"
    stat_ins = f"+{fe.inserted}" if (fe.inserted or fe.deleted) else ""
    stat_del = f"-{fe.deleted}" if (fe.inserted or fe.deleted) else ""
    stat = f"{stat_ins} {stat_del}".strip()
    left = f" {code}  "                       # 5 chars: " XY  "
    pad = max(1, inner_w - len(left) - len(stat) - 1)
    name = fe.path
    if len(name) > pad:
        name = name[: pad - 1] + "…"
    name = name.ljust(pad)
    full = f"{left}{name} {stat}"

    if focused:
        safe_addstr(stdscr, y, x, full, sb | curses.A_REVERSE)
        return

    base = sb | curses.A_DIM if fe.untracked else sb
    safe_addstr(stdscr, y, x, full, base)

    # Overlay the status code. Untracked stays dim cyan-ish via the
    # base attr; everything else picks a per-status pastel pair.
    if not fe.untracked:
        pair_id = _file_status_pair(fe.x, fe.y)
        if pair_id is not None:
            safe_addstr(stdscr, y, x + 1, code, curses.color_pair(pair_id))

    # Overlay the diff stats — green for "+N", red for "-M".
    if stat:
        stat_x = x + len(left) + pad + 1
        safe_addstr(stdscr, y, stat_x, stat_ins,
                    curses.color_pair(PAIR_PASTEL_GREEN))
        safe_addstr(stdscr, y, stat_x + len(stat_ins) + 1, stat_del,
                    curses.color_pair(PAIR_PASTEL_RED))


def _draw_commits_pane(stdscr, line: int, inner_x: int, inner_w: int,
                       list_rows: int, menu: ActionMenu, sb: int) -> None:
    filter_focused = menu.pane_focus and menu.commits_selected == 0
    _draw_filter_row(stdscr, line, inner_x, inner_w,
                     menu.commits_filter, filter_focused, sb)
    line += 1

    commits = _filtered_commits(menu)
    if not commits:
        msg = ("(no commits on this branch yet)"
               if not menu.commits_filter else "(no matches)")
        safe_addstr(stdscr, line, inner_x + 2, msg, sb | curses.A_DIM)
        return

    sel_in_list = max(0, menu.commits_selected - 1)
    if sel_in_list < menu.commits_scroll:
        menu.commits_scroll = sel_in_list
    elif sel_in_list >= menu.commits_scroll + list_rows:
        menu.commits_scroll = sel_in_list - list_rows + 1
    if menu.commits_scroll > max(0, len(commits) - list_rows):
        menu.commits_scroll = max(0, len(commits) - list_rows)

    visible = commits[menu.commits_scroll:menu.commits_scroll + list_rows]
    for i, c in enumerate(visible):
        idx = menu.commits_scroll + i
        focused = (menu.pane_focus and menu.commits_selected == idx + 1)
        _draw_commit_row(stdscr, line + i, inner_x, inner_w, c, focused, sb)

    # Trailing footer when we've walked all the way back.
    if (menu.commits_exhausted and len(visible) < list_rows
            and not menu.commits_filter):
        tail_y = line + len(visible)
        safe_addstr(stdscr, tail_y, inner_x + 2,
                    "(reached root commit)", sb | curses.A_DIM)


def _draw_commit_row(stdscr, y: int, x: int, inner_w: int,
                     c: CommitEntry, focused: bool, sb: int) -> None:
    """Render one commit row with the SHA in pastel-yellow (matching
    git's own --abbrev colour) and the relative date in pastel-blue.
    The subject keeps default fg so it's the visual focus of the row."""
    sha = c.sha
    rel = f"({c.relative})" if c.relative else ""
    head = f"  {sha}  "
    pad = max(1, inner_w - len(head) - len(rel) - 1)
    subj = c.subject
    if len(subj) > pad:
        subj = subj[: pad - 1] + "…"
    subj = subj.ljust(pad)
    full = f"{head}{subj} {rel}"

    if focused:
        safe_addstr(stdscr, y, x, full, sb | curses.A_REVERSE)
        return

    safe_addstr(stdscr, y, x, full, sb)
    # Overlay the SHA (positions 2..2+len(sha)).
    safe_addstr(stdscr, y, x + 2, sha,
                curses.color_pair(PAIR_PASTEL_YELLOW))
    if rel:
        rel_x = x + len(head) + pad + 1
        safe_addstr(stdscr, y, rel_x, rel,
                    curses.color_pair(PAIR_PASTEL_BLUE))


# ---------- Handle --------------------------------------------------------


def handle_action_menu_key(state: State, key: int) -> None:
    menu = state.action_menu
    if menu is None:
        return

    if key == 27:
        menu.cancel_event.set()
        state.action_menu = None
        return

    if key == curses.KEY_HOME:
        # Jump to the first action item from anywhere.
        menu.pane_focus = False
        menu.selected = 0
        for i, it in enumerate(menu.items):
            if it.enabled:
                menu.selected = i
                break
        return

    if menu.pane_focus:
        _handle_pane_key(state, menu, key)
        return

    # ---- Action-items navigation ----
    if key == curses.KEY_UP and menu.items:
        menu.selected = (menu.selected - 1) % len(menu.items)
        return
    if key == curses.KEY_DOWN and menu.items:
        if menu.selected >= len(menu.items) - 1:
            # Down off the last item drops focus into the pane.
            menu.pane_focus = True
            return
        menu.selected += 1
        return
    if key in (10, 13, curses.KEY_ENTER) and menu.items:
        item = menu.items[menu.selected]
        if not item.enabled:
            return
        if item.id == "switch_branch":
            from .branch_picker import open_branch_picker
            open_branch_picker(state)
            return
        if item.id == "soft_reset":
            from .reset_prompt import open_reset_prompt
            open_reset_prompt(state)
            return
        if item.id == "run_workflow":
            from .workflow_picker import open_workflow_picker
            open_workflow_picker(state)
            return
        kick_off_action(
            state, item.id,
            target_label=menu.target_label,
            target_path=menu.target_path,
            target_repo=menu.target_repo,
            target_parent=menu.target_parent,
        )
        menu.cancel_event.set()
        state.action_menu = None


def _handle_pane_key(state: State, menu: ActionMenu, key: int) -> None:
    # Tab swap.
    if key == curses.KEY_LEFT:
        menu.pane_tab = "tree" if menu.pane_tab == "commits" else "commits"
        _maybe_prefetch(menu)
        return
    if key == curses.KEY_RIGHT:
        menu.pane_tab = "commits" if menu.pane_tab == "tree" else "tree"
        _maybe_prefetch(menu)
        return

    # Selection ↔ "above filter row" returns to the action items.
    if menu.pane_tab == "tree":
        _handle_tree_key(menu, key)
    else:
        _handle_commits_key(menu, key)
    _maybe_prefetch(menu)


def _handle_tree_key(menu: ActionMenu, key: int) -> None:
    files = _filtered_tree(menu)
    max_idx = len(files)  # 0 = filter, 1..len(files) = rows
    on_filter = (menu.tree_selected == 0)

    if key == curses.KEY_UP:
        if on_filter:
            menu.pane_focus = False
            return
        menu.tree_selected -= 1
        return
    if key == curses.KEY_DOWN:
        if menu.tree_selected < max_idx:
            menu.tree_selected += 1
        return
    if on_filter and _is_typing_key(key):
        if key in (curses.KEY_BACKSPACE, 127, 8):
            menu.tree_filter = menu.tree_filter[:-1]
        else:
            menu.tree_filter += chr(key)
        # Filter changed → clamp selection (still on filter row though).
        return


def _handle_commits_key(menu: ActionMenu, key: int) -> None:
    commits = _filtered_commits(menu)
    max_idx = len(commits)
    on_filter = (menu.commits_selected == 0)

    if key == curses.KEY_UP:
        if on_filter:
            menu.pane_focus = False
            return
        menu.commits_selected -= 1
        return
    if key == curses.KEY_DOWN:
        if menu.commits_selected < max_idx:
            menu.commits_selected += 1
        return
    if on_filter and _is_typing_key(key):
        if key in (curses.KEY_BACKSPACE, 127, 8):
            menu.commits_filter = menu.commits_filter[:-1]
        else:
            menu.commits_filter += chr(key)
        return


def _is_typing_key(key: int) -> bool:
    """Either a printable ASCII char or one of the backspace variants."""
    if key in (curses.KEY_BACKSPACE, 127, 8):
        return True
    return 32 <= key <= 126
