"""All curses rendering, modal openers, and keyboard handlers. Every
function here is called from the main thread; nothing in this file
blocks on git or kicks off workers directly — the workers module owns
the background pipeline."""
from __future__ import annotations

import curses
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from models import (
    ChildRef, LFSCandidate, Repo, State, ThenRunSelector, WorkflowToggle,
)
from config import CONFIG_FILE, DEFAULT_TRUNCATION_MODE
from git_ops import (
    find_lfs_warnings, gh_available, link_siblings,
    parse_github_slug, would_run_on_push,
)
from workers import (
    kick_off_bulk_suggest, kick_off_suggest_for, kick_off_workers,
    refresh_repo_with_remote_state,
)
# Color palette + state-dot helpers live in ui.colors so they can be
# imported from leaf modules (modals, sidebar) without pulling in the
# rest of this monolith. Re-exported here so external callers keep
# `from ui import PAIR_…, state_color, init_colors`-style imports.
from .colors import (  # noqa: F401  (re-exported public API)
    PAIR_AHEAD, PAIR_BEHIND, PAIR_BRANCH, PAIR_DIRTY, PAIR_ERR,
    PAIR_HEADER, PAIR_HINT, PAIR_OK, PAIR_SB_CYAN, PAIR_SB_ERR,
    PAIR_SB_FG, PAIR_SB_FG_ACTIVE, PAIR_SB_OK, PAIR_SB_WARN,
    PAIR_TOGGLE_OFF, PAIR_TOGGLE_ON, PAIR_WARN,
    _state_color, child_state_color, init_colors, state_color,
)
from .geometry import (  # noqa: F401  (re-exported public API)
    SIDEBAR_W, SIDEBAR_W_NARROW, draw_modal_fill, field_visible,
    modal_geometry, safe_addstr, sidebar_geometry, truncate,
)
# Modals — each one is self-contained in ui.modals.<name>; the package
# re-exports the public open/draw/handle trio. Imported here so callers
# of the package don't have to know which submodule owns which modal.
from .modals import (  # noqa: F401  (re-exported public API)
    draw_action_menu, draw_align_heads_prompt, draw_branch_picker,
    draw_reset_prompt, draw_task_action_menu, draw_workflow_picker,
    handle_action_menu_key, handle_align_heads_prompt_key,
    handle_branch_picker_key, handle_reset_prompt_key,
    handle_task_action_menu_key, handle_workflow_picker_key,
    open_action_menu, open_align_heads_prompt, open_branch_picker,
    open_reset_prompt, open_task_action_menu, open_workflow_picker,
)
# Right-hand task panel.
from .sidebar import (  # noqa: F401  (re-exported public API)
    SPINNER_FRAMES, draw_sidebar,
)


# ---------- Loading screen (startup only) ---------------------------------


def refresh_all(stdscr, repos: List[Repo], name_max: int,
                name_mode: str = DEFAULT_TRUNCATION_MODE,
                subtrees=None,
                header: str = "loading repos") -> None:
    """Refresh every repo in parallel, animating a spinner while it runs.
    Used at startup; runtime refreshes go through workers.kick_off_inline_refresh."""
    if not repos:
        return
    done = [False] * len(repos)

    def work(i: int) -> None:
        refresh_repo_with_remote_state(repos[i])
        done[i] = True

    curses.curs_set(0)
    with ThreadPoolExecutor(max_workers=len(repos)) as ex:
        futures = [ex.submit(work, i) for i in range(len(repos))]
        frame = 0
        while not all(f.done() for f in futures):
            draw_loading(stdscr, repos, done, name_max, name_mode, header,
                         SPINNER_FRAMES[frame % len(SPINNER_FRAMES)])
            curses.napms(80)
            frame += 1
        draw_loading(stdscr, repos, done, name_max, name_mode, header, "✓")
        curses.napms(120)
        for f in futures:
            f.result()  # surface thread exceptions if any
    link_siblings(repos, subtrees)


def draw_loading(stdscr, repos: List[Repo], done: List[bool],
                 name_max: int, name_mode: str,
                 header: str, spinner: str) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    completed = sum(done)
    total = len(repos)

    title = "idlegit"
    summary = f"{spinner}  {header} ({completed}/{total})"

    name_w = max(len(truncate(r.display_name, name_max, name_mode)) for r in repos)
    block_h = 4 + len(repos)
    top = max(1, (h - block_h) // 2)
    cx = w // 2

    safe_addstr(stdscr, top, max(0, cx - len(title) // 2), title,
                curses.A_BOLD | curses.color_pair(PAIR_HEADER))
    safe_addstr(stdscr, top + 2, max(0, cx - len(summary) // 2),
                summary, curses.color_pair(PAIR_BRANCH))

    list_left = max(0, cx - (name_w + 4) // 2)
    for i, repo in enumerate(repos):
        if done[i]:
            mark, attr = "✓", curses.color_pair(PAIR_OK)
        else:
            mark, attr = "·", curses.A_DIM
        safe_addstr(stdscr, top + 4 + i, list_left,
                    f"  {mark}  {truncate(repo.display_name, name_max, name_mode)}",
                    attr)

    stdscr.refresh()




# ---------- Main screen ---------------------------------------------------


def _body_height_for(state: State, h: int) -> int:
    """Height (in rows) available for the repo body. Reserves space for the
    title (1), toggles row (1) + blank (1), one blank line before hints,
    two hint lines, and the state legend (1) — 7 rows of chrome total."""
    chrome = 7
    avail = h - chrome
    if avail < 1:
        return 1
    if state.max_visible_repo_rows > 0:
        avail = min(avail, state.max_visible_repo_rows)
    return max(1, avail)


def _ensure_focused_visible(state: State, body_h: int, total_body: int) -> None:
    """Adjust state.body_scroll so the focused body row is on-screen."""
    if state.on_toggle:
        return
    body_idx = state.selected - 3  # 3 toggle rows precede the body
    if body_idx < 0 or body_idx >= total_body:
        return
    if body_idx < state.body_scroll:
        state.body_scroll = body_idx
    elif body_idx >= state.body_scroll + body_h:
        state.body_scroll = body_idx - body_h + 1
    state.body_scroll = max(0, min(state.body_scroll, max(0, total_body - body_h)))


def draw_main(stdscr, state: State) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    sidebar_x, sidebar_w = sidebar_geometry(w)
    main_w = sidebar_x

    body_h = _body_height_for(state, h)
    if main_w < 80 or h < 8:
        safe_addstr(stdscr, 0, 0, "terminal too small — resize and try again",
                    curses.color_pair(PAIR_ERR))
        stdscr.refresh()
        return

    safe_addstr(stdscr, 0, 0, "idlegit",
                curses.A_BOLD | curses.color_pair(PAIR_HEADER))
    if state.workspace_name:
        safe_addstr(stdscr, 0, len("idlegit"), " · ", curses.A_DIM)
        safe_addstr(stdscr, 0, len("idlegit") + 3, state.workspace_name,
                    curses.A_BOLD | curses.color_pair(PAIR_BRANCH))

    toggle_y = 2
    # "Repositories" header on the left of the toggles row, mirroring
    # the "Tasks" header in the sidebar. The accent (cyan) only lights
    # up when this panel has focus; otherwise it dims to match the
    # sidebar's inactive header.
    repos_active = state.focused_panel == "repos"
    # Active = cyan accent (matches the Tasks-panel header in the
    # sidebar). The magenta PAIR_HEADER is reserved for the title.
    repos_header_attr = (
        curses.color_pair(PAIR_BRANCH) | curses.A_BOLD if repos_active
        else curses.A_DIM | curses.A_BOLD)
    safe_addstr(stdscr, toggle_y, 2, "Repositories", repos_header_attr)

    # Right-align the toggles inside the main panel so they sit just
    # left of the sidebar boundary, above the commit-message column.
    # Three toggles: auto-stage / auto-push / align-heads. Each `draw_toggle`
    # writes "[x] label" so we budget label+4 cells per toggle plus a 2-cell
    # gap between them.
    toggles_w = (4 + 10) + 2 + (4 + 9) + 2 + (4 + 11)  # auto-stage + auto-push + align-heads
    panel_right = sidebar_x if sidebar_w > 0 else main_w
    toggles_x = max(2 + len("Repositories") + 4, panel_right - toggles_w - 2)
    draw_toggle(stdscr, toggle_y, toggles_x, "auto-stage", state.auto_stage,
                state.selected == 0)
    draw_toggle(stdscr, toggle_y, toggles_x + 16, "auto-push", state.auto_push,
                state.selected == 1)
    draw_toggle(stdscr, toggle_y, toggles_x + 31, "align-heads",
                state.align_heads, state.selected == 2)

    nm = state.name_display_max
    bm = state.branch_display_max
    nmode = state.name_truncation
    bmode = state.branch_truncation
    # Column widths must accommodate every visible row, including
    # submodule children. Children render at column 4 with a "↳ " glyph
    # (2 cells) so they need 4 extra cells of name budget compared to
    # parent rows. Without this allowance, the branch column overwrites
    # the tail of long child names and the configured truncation policy
    # never fires (it just looks like end-truncation by clipping).
    name_lengths = [len(truncate(r.display_name, nm, nmode))
                    for r in state.repos]
    branch_lengths = [len(f"[{truncate(r.branch, bm, bmode)}]")
                      for r in state.repos]
    for parent in state.repos:
        for ch in parent.children:
            name_lengths.append(
                4 + len(truncate(ch.repo.display_name, nm, nmode)))
            if ch.branch:
                branch_lengths.append(
                    len(f"[{truncate(ch.branch, bm, bmode)}]"))
    name_w = max(name_lengths) + 2
    branch_w = max(branch_lengths) + 2
    marker_w = 3
    field_x = 2 + name_w + branch_w + marker_w
    field_w = max(20, main_w - field_x - 2)

    base_y = 4
    body_rows = state.selectable_rows()[3:]  # drop the 3 toggle rows
    _ensure_focused_visible(state, body_h, len(body_rows))
    visible_start = state.body_scroll
    visible_end = min(len(body_rows), visible_start + body_h)

    spinner_char = SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]
    y_for_body: Dict[int, int] = {}
    for screen_i, body_idx in enumerate(range(visible_start, visible_end)):
        row = body_rows[body_idx]
        y = base_y + screen_i
        y_for_body[body_idx] = y
        full_idx = body_idx + 3  # 3 toggle rows precede body indices in selectable_rows
        focused = (state.selected == full_idx)
        if row[0] == "repo":
            row_cursor = state.field_cursor if focused else 0
            draw_repo_row(stdscr, y, row[1], focused,
                          name_w, branch_w, field_x, field_w,
                          nm, bm, nmode, bmode, row_cursor, spinner_char)
        else:  # child
            row_cursor = state.field_cursor if focused else 0
            draw_child_row(stdscr, y, row[2], focused,
                           name_w, branch_w, field_x, field_w,
                           nm, bm, nmode, bmode,
                           row_cursor, spinner_char)

    if visible_start > 0:
        safe_addstr(stdscr, base_y - 1, 2,
                    f"↑ {visible_start} more above", curses.A_DIM)
    if visible_end < len(body_rows):
        below = len(body_rows) - visible_end
        safe_addstr(stdscr, base_y + body_h, 2,
                    f"↓ {below} more below", curses.A_DIM)

    # Subtle focus marker at column 0 of the active row. Toggles share a
    # row so we mark it whenever any of the three toggles is selected;
    # for body rows we use the cached y from the render loop above.
    focus_y: Optional[int] = None
    if state.selected < 3:
        focus_y = toggle_y
    else:
        body_idx = state.selected - 3  # 3 toggle rows precede the body
        focus_y = y_for_body.get(body_idx)
    if focus_y is not None:
        safe_addstr(stdscr, focus_y, 0, "›",
                    curses.color_pair(PAIR_BRANCH) | curses.A_BOLD)

    hint_y = base_y + body_h + 1
    safe_addstr(stdscr, hint_y, 2,
                "↑/↓ navigate · Tab menu · Shift+Tab → tasks · Left/Shift+Left suggest · Enter review",
                curses.A_DIM)
    safe_addstr(stdscr, hint_y + 1, 2,
                "Space toggles · Ctrl+R refresh · Ctrl+S smart-sync · Esc clears / back / quits",
                curses.A_DIM)
    draw_state_legend(stdscr, hint_y + 2, 2)

    modal_active = (state.action_menu is not None
                    or state.branch_picker is not None
                    or state.reset_prompt is not None
                    or state.workflow_picker is not None
                    or state.align_heads_prompt is not None
                    or state.task_action_menu is not None)
    if state.action_menu is not None:
        draw_action_menu(stdscr, state, sidebar_x)
    if state.branch_picker is not None:
        draw_branch_picker(stdscr, state, sidebar_x)
    if state.reset_prompt is not None:
        draw_reset_prompt(stdscr, state, sidebar_x)
    if state.workflow_picker is not None:
        draw_workflow_picker(stdscr, state, sidebar_x)
    if state.align_heads_prompt is not None:
        draw_align_heads_prompt(stdscr, state, sidebar_x)
    if state.task_action_menu is not None:
        draw_task_action_menu(stdscr, state, sidebar_x)

    # Sidebar drawn LAST so it's always the freshest paint on screen —
    # avoids the resize artifacts where stale cells from the old layout
    # bleed through under the panel.
    if sidebar_w > 0:
        draw_sidebar(stdscr, state, sidebar_x, sidebar_w)

    cursor_set = False
    if not modal_active and not state.on_toggle:
        body_idx = state.selected - 3  # 3 toggle rows precede the body
        if 0 <= body_idx < len(body_rows) and body_idx in y_for_body:
            row = body_rows[body_idx]
            target = None
            if row[0] == "repo":
                target = row[1] if (row[1].is_dirty or row[1].message) else None
            elif row[0] == "child" and row[2].kind == "submodule":
                target = row[2] if (row[2].dirty or row[2].message) else None
            if target is not None:
                # field_w-1 leaves a single trailing cell as an
                # end-of-field cap; the message itself starts at
                # field_x so the cursor's home is the first
                # character (no inert leading column).
                inner_w = field_w - 1
                cur = max(0, min(state.field_cursor, len(target.message)))
                _, cur_in_visible = field_visible(
                    target.message, cur, inner_w, True)
                cur_x = field_x + cur_in_visible
                cur_y = y_for_body[body_idx]
                # Ask for a "very visible" hardware cursor — without the
                # extra cell-attribute overlay, which produced too much
                # contrast against the reversed-white field.
                try:
                    stdscr.move(cur_y, cur_x)
                    curses.curs_set(2)
                    cursor_set = True
                except curses.error:
                    pass
    if not cursor_set:
        curses.curs_set(0)

    stdscr.refresh()


def draw_state_legend(stdscr, y: int, x: int) -> None:
    items = [
        ("clean", curses.color_pair(PAIR_OK)),
        ("dirty", curses.color_pair(PAIR_DIRTY)),
        ("merging", curses.color_pair(PAIR_ERR)),
        ("ahead", curses.color_pair(PAIR_AHEAD)),
        ("behind", curses.color_pair(PAIR_BEHIND)),
        ("no upstream", curses.A_DIM),
        ("error", curses.color_pair(PAIR_ERR)),
    ]
    cur = x
    for label, attr in items:
        safe_addstr(stdscr, y, cur, "●", attr)
        safe_addstr(stdscr, y, cur + 2, label, curses.A_DIM)
        cur += 2 + len(label) + 2


def draw_toggle(stdscr, y: int, x: int, label: str, value: bool, focused: bool) -> None:
    box = "[x]" if value else "[ ]"
    pair = PAIR_TOGGLE_ON if value else PAIR_TOGGLE_OFF
    attr = curses.color_pair(pair)
    if focused:
        attr |= curses.A_REVERSE
    safe_addstr(stdscr, y, x, f"{box} {label}", attr)


def draw_repo_row(stdscr, y: int, repo: Repo, focused: bool,
                  name_w: int, branch_w: int, field_x: int, field_w: int,
                  name_max: int, branch_max: int,
                  name_mode: str, branch_mode: str,
                  field_cursor: int = 0,
                  spinner_char: str = " ") -> None:
    name_attr = curses.A_BOLD if focused else 0
    safe_addstr(stdscr, y, 2,
                truncate(repo.display_name, name_max, name_mode).ljust(name_w),
                name_attr)

    branch_str = f"[{truncate(repo.branch, branch_max, branch_mode)}]".ljust(branch_w)
    safe_addstr(stdscr, y, 2 + name_w, branch_str,
                curses.color_pair(PAIR_BRANCH))

    if repo.refreshing:
        safe_addstr(stdscr, y, 2 + name_w + branch_w,
                    f" {spinner_char} ", curses.color_pair(PAIR_BRANCH))
    else:
        _, state_attr = state_color(repo)
        safe_addstr(stdscr, y, 2 + name_w + branch_w, " ● ", state_attr)

    if repo.suggesting and not repo.message:
        inner_w = field_w - 1
        text = (f"{spinner_char} generating…").ljust(inner_w + 1)
        safe_addstr(stdscr, y, field_x, text,
                    curses.color_pair(PAIR_BRANCH) | curses.A_DIM)
    elif repo.is_dirty or repo.message:
        inner_w = field_w - 1
        visible, _ = field_visible(repo.message, field_cursor, inner_w, focused)
        field_text = visible.ljust(inner_w) + " "
        # Outline-only field styling: leaves the terminal background
        # untouched (so the hardware cursor stays readable on both light
        # and dark themes) and relies on a colored underline + the row's
        # focus arrow / bold name to signal which row is active.
        if focused:
            field_attr = (curses.color_pair(PAIR_BRANCH)
                          | curses.A_UNDERLINE | curses.A_BOLD)
        else:
            field_attr = curses.A_UNDERLINE | curses.A_DIM
        safe_addstr(stdscr, y, field_x, field_text, field_attr)


def draw_child_row(stdscr, y: int, child: ChildRef, focused: bool,
                   name_w: int, branch_w: int, field_x: int, field_w: int,
                   name_max: int, branch_max: int,
                   name_mode: str, branch_mode: str,
                   field_cursor: int = 0,
                   spinner_char: str = " ") -> None:
    glyph = "↳" if child.kind == "submodule" else "⊕"
    name_attr = curses.A_BOLD if focused else curses.A_DIM
    # Submodule glyph carries the sync-vs-canonical signal: green when
    # the nested HEAD matches the canonical, pink when it has drifted.
    # Subtree rows have no such relationship, so the glyph stays in the
    # row's normal name attribute.
    if child.kind == "submodule":
        glyph_attr = (curses.color_pair(PAIR_OK) if child.in_sync
                      else curses.color_pair(PAIR_BEHIND))
        if focused:
            glyph_attr |= curses.A_BOLD
    else:
        glyph_attr = name_attr
    safe_addstr(stdscr, y, 4, glyph, glyph_attr)
    safe_addstr(stdscr, y, 6,
                truncate(child.repo.display_name, name_max, name_mode),
                name_attr)
    if child.kind == "submodule":
        # Branch label in the same column as parent rows, but a dimmer
        # cyan to keep the visual hierarchy obvious at a glance.
        if child.branch:
            branch_str = (
                f"[{truncate(child.branch, branch_max, branch_mode)}]"
                .ljust(branch_w))
            safe_addstr(stdscr, y, 2 + name_w, branch_str,
                        curses.color_pair(PAIR_BRANCH) | curses.A_DIM)
        # Main state dot — same precedence as a top-level repo.
        _, state_attr = child_state_color(child)
        safe_addstr(stdscr, y, 2 + name_w + branch_w, " ● ", state_attr)
        if child.suggesting and not child.message:
            inner_w = field_w - 1
            text = (f"{spinner_char} generating…").ljust(inner_w + 1)
            safe_addstr(stdscr, y, field_x, text,
                        curses.color_pair(PAIR_BRANCH) | curses.A_DIM)
        elif child.dirty or child.message:
            inner_w = field_w - 1
            visible, _ = field_visible(
                child.message, field_cursor, inner_w, focused)
            field_text = visible.ljust(inner_w) + " "
            # Outline-only field styling: leaves the terminal background
            # untouched (so the hardware cursor stays readable on both
            # light and dark themes) and relies on a colored underline +
            # the row's focus arrow / bold name to signal active rows.
            if focused:
                field_attr = (curses.color_pair(PAIR_BRANCH)
                              | curses.A_UNDERLINE | curses.A_BOLD)
            else:
                field_attr = curses.A_UNDERLINE | curses.A_DIM
            safe_addstr(stdscr, y, field_x, field_text, field_attr)




# ---------- Confirm screen -------------------------------------------------


def build_confirm_lines(
    state: State,
) -> Tuple[List[Tuple[str, int]], List[LFSCandidate],
           List[WorkflowToggle], List["ThenRunSelector"]]:
    lines: List[Tuple[str, int]] = []
    lfs_candidates: List[LFSCandidate] = []
    wf_toggles: List[WorkflowToggle] = []
    then_run_items: List[ThenRunSelector] = []
    have_gh = gh_available()
    repos = [r for r in state.repos if r.message.strip()]
    child_targets: List[Tuple[Repo, ChildRef]] = []
    for parent in state.repos:
        for ref in parent.children:
            if ref.kind == "submodule" and ref.message.strip():
                child_targets.append((parent, ref))

    total = len(repos) + len(child_targets)
    lines.append((f"{total} target(s) to commit  ·  "
                  f"auto-stage: {'on' if state.auto_stage else 'off'}  ·  "
                  f"auto-push: {'on' if state.auto_push else 'off'}",
                  curses.A_DIM))
    lines.append(("", 0))

    threshold_mb = state.lfs_warn_bytes // (1024 * 1024)

    for repo in repos:
        header = f"{repo.display_name}  [{repo.branch}]"
        lines.append((header, curses.A_BOLD))

        if repo.merging:
            lines.append(("  ⚠ merge / rebase in progress — commit will be skipped",
                          curses.color_pair(PAIR_ERR)))
            lines.append(("    resolve conflicts and finish the operation, then re-run.",
                          curses.A_DIM))
            for cp in repo.conflict_paths:
                lines.append((f"      {cp}", curses.color_pair(PAIR_ERR)))
            lines.append(("", 0))
            continue

        lines.append((f'  message:  "{repo.message.strip()}"', 0))

        if state.auto_stage:
            files = [(s, p) for s, p in repo.staged]
            files += [(s, p) for s, p in repo.unstaged]
            files += [("?", p) for p in repo.untracked]
            stage_label = "stage:    "
        else:
            files = list(repo.staged)
            stage_label = "staged:   "

        if files:
            first = True
            for status, path in files:
                prefix = f"  {stage_label}" if first else "  " + " " * len(stage_label)
                lines.append((f"{prefix}{status}  {path}", curses.A_DIM))
                first = False
        else:
            lines.append(("  ⚠ no changes — will be skipped",
                          curses.color_pair(PAIR_WARN)))

        warnings = find_lfs_warnings(repo, state.auto_stage, state.lfs_warn_bytes)
        if warnings:
            lines.append((f"  ⚠ files ≥{threshold_mb} MB not LFS-tracked — push will fail:",
                          curses.color_pair(PAIR_ERR)))
            for path, size in warnings:
                cand = LFSCandidate(
                    repo=repo, path=path, size_str=size,
                    line_index=len(lines),
                )
                lfs_candidates.append(cand)
                lines.append(("", curses.color_pair(PAIR_ERR)))

        if state.auto_push:
            if repo.upstream:
                lines.append((f"  push:     yes → {repo.upstream}", 0))
            else:
                lines.append((f"  push:     yes (sets upstream → origin/{repo.branch})", 0))
            if repo.siblings:
                names = ", ".join(s[0].display_name for s in repo.siblings)
                lines.append((f"  sync:     {names}", 0))
        else:
            lines.append(("  push:     no", curses.A_DIM))

        # Per-workflow track-this-run toggles. Only emitted for workflows
        # that we can predict will actually fire on this push:
        #  - gh CLI must be available at startup
        #  - the repo's remote URL must parse as a github.com slug
        #  - the user is auto-pushing (no push → no run to track)
        #  - `would_run_on_push` must be True for the repo's current branch
        #    (push trigger present, branch matches)
        #  - the workflow's GitHub-side state isn't `disabled_*` (a
        #    disabled workflow won't fire on push, so there's no run
        #    to track and offering the toggle is misleading)
        # Each toggle's live state lives in repo.track_workflow[wf.name];
        # initialise it from the global default the first time we see it.
        if (state.auto_push and have_gh and repo.workflows
                and parse_github_slug(repo.remote_url_raw)):
            dispatchable_options = [
                w.name for w in repo.workflows
                if w.dispatchable and not w.state.startswith("disabled")
            ]
            for wf in repo.workflows:
                if not would_run_on_push(wf, repo.branch):
                    continue
                if wf.state.startswith("disabled"):
                    continue
                if wf.name not in repo.track_workflow:
                    repo.track_workflow[wf.name] = state.track_actions_default
                wf_toggles.append(WorkflowToggle(
                    repo=repo, workflow_name=wf.name,
                    line_index=len(lines),
                ))
                # Placeholder — render_workflow_toggle_line below paints
                # over this slot during draw.
                lines.append(("", 0))
                # Indented "then run" selector — fires when this tracked
                # workflow's run completes successfully. Only meaningful
                # if the repo has at least one dispatchable+active
                # workflow to chain to.
                if dispatchable_options:
                    then_run_items.append(ThenRunSelector(
                        repo=repo, after_workflow=wf.name,
                        line_index=len(lines),
                    ))
                    lines.append(("", 0))
            # Root-level "then run after push" — fires once the push
            # itself completes, independent of any tracked workflow run.
            if dispatchable_options:
                then_run_items.append(ThenRunSelector(
                    repo=repo, after_workflow="",
                    line_index=len(lines),
                ))
                lines.append(("", 0))

        lines.append(("", 0))

    for parent, ref in child_targets:
        header = f"↳ {ref.repo.display_name} in {parent.display_name}"
        lines.append((header, curses.A_BOLD))
        lines.append((f'  message:  "{ref.message.strip()}"', 0))
        lines.append((f'  path:     {ref.nested_path}', curses.A_DIM))
        if state.auto_push:
            lines.append(("  push:     yes (from nested checkout)", 0))
            other_targets = [ref.repo.display_name + " (top-level)"]
            for other_parent, other_path in ref.repo.siblings:
                if other_path != ref.nested_path:
                    other_targets.append(
                        f"{ref.repo.display_name} in {other_parent.display_name}")
            if other_targets:
                lines.append((f"  sync:     {', '.join(other_targets)}", 0))
        else:
            lines.append(("  push:     no", curses.A_DIM))
        lines.append(("  ⚠ if the nested checkout is in detached HEAD, the "
                      "commit will be skipped", curses.A_DIM))
        lines.append(("", 0))

    return lines, lfs_candidates, wf_toggles, then_run_items


def render_candidate_line(cand: LFSCandidate, focused: bool) -> Tuple[str, int]:
    check = "[x]" if cand.track else "[ ]"
    text = f"      {check}  {cand.path}  ({cand.size_str})"
    base = PAIR_OK if cand.track else PAIR_ERR
    attr = curses.color_pair(base)
    if focused:
        attr |= curses.A_REVERSE
    return text, attr


def render_workflow_toggle_line(toggle: WorkflowToggle,
                                focused: bool) -> Tuple[str, int]:
    """Render one workflow track-toggle line for the review screen. The
    'live' state is read straight from the repo so the dict is the source
    of truth; the cursor mutates it on Space."""
    on = toggle.repo.track_workflow.get(toggle.workflow_name, False)
    check = "[x]" if on else "[ ]"
    text = f"  {check}  track action: {toggle.workflow_name}"
    attr = curses.color_pair(PAIR_OK if on else PAIR_HEADER) | curses.A_DIM
    if on:
        attr = curses.color_pair(PAIR_OK)
    if focused:
        attr |= curses.A_REVERSE
    return text, attr


def _then_run_options(repo: Repo) -> List[str]:
    """Workflow names eligible as 'then run' targets for this repo —
    dispatchable + not disabled-on-github. Returned in the same order
    as repo.workflows so left/right cycling stays stable."""
    return [w.name for w in repo.workflows
            if w.dispatchable and not w.state.startswith("disabled")]


def _then_run_current(selector: ThenRunSelector) -> str:
    """Read the current then-run selection from the repo's memory dict."""
    if selector.after_workflow:
        return selector.repo.then_run_after_workflow.get(
            selector.after_workflow, "")
    return selector.repo.then_run_after_push


def _then_run_set(selector: ThenRunSelector, value: str) -> None:
    """Persist a then-run selection. Empty string means '(none)'."""
    if selector.after_workflow:
        if value:
            selector.repo.then_run_after_workflow[
                selector.after_workflow] = value
        else:
            selector.repo.then_run_after_workflow.pop(
                selector.after_workflow, None)
    else:
        selector.repo.then_run_after_push = value


def render_then_run_line(selector: ThenRunSelector,
                         focused: bool) -> Tuple[str, int]:
    """Render a 'then run' chain selector. Indented one level under a
    workflow toggle when `after_workflow` is set, otherwise sits at the
    repo's body indent as the post-push action's then-run."""
    indent = "        " if selector.after_workflow else "  "
    label = "then run:" if selector.after_workflow else "then run after push:"
    current = _then_run_current(selector) or "(none)"
    text = f"{indent}{label} ‹ {current} ›"
    if focused:
        attr = curses.color_pair(PAIR_BRANCH) | curses.A_BOLD
    else:
        attr = curses.A_DIM
    return text, attr


def cycle_then_run(selector: ThenRunSelector, direction: int) -> None:
    """Cycle the selector's choice through the repo's dispatchable
    workflows + a '(none)' slot. `direction` is +1 (right arrow) or
    -1 (left arrow)."""
    options = _then_run_options(selector.repo)
    if not options:
        _then_run_set(selector, "")
        return
    # Wheel layout: ["", option0, option1, ..., optionN-1]
    wheel = [""] + options
    current = _then_run_current(selector)
    try:
        i = wheel.index(current)
    except ValueError:
        i = 0  # current selection is no longer dispatchable; reset to none
    i = (i + direction) % len(wheel)
    _then_run_set(selector, wheel[i])


def draw_confirm(stdscr,
                 lines: List[Tuple[str, int]],
                 candidates: List[LFSCandidate],
                 wf_toggles: List[WorkflowToggle],
                 then_run_items: List[ThenRunSelector],
                 cursor: int,
                 scroll: int) -> int:
    """Returns max scroll value for clamping. `cursor` indexes a unified
    list of focusable items in this order: LFS candidates → workflow
    toggles → then-run selectors."""
    stdscr.erase()
    h, _ = stdscr.getmaxyx()
    safe_addstr(stdscr, 0, 0, "Review",
                curses.A_BOLD | curses.color_pair(PAIR_HEADER))

    body_top = 2
    body_h = max(1, h - body_top - 2)
    max_scroll = max(0, len(lines) - body_h)
    scroll = max(0, min(scroll, max_scroll))

    cand_at_line = {c.line_index: i for i, c in enumerate(candidates)}
    wf_at_line = {t.line_index: i for i, t in enumerate(wf_toggles)}
    tr_at_line = {s.line_index: i for i, s in enumerate(then_run_items)}
    n_cands = len(candidates)
    n_toggles = len(wf_toggles)

    for i in range(body_h):
        idx = scroll + i
        if idx >= len(lines):
            break
        if idx in cand_at_line:
            cand_idx = cand_at_line[idx]
            text, attr = render_candidate_line(
                candidates[cand_idx], focused=(cursor == cand_idx))
        elif idx in wf_at_line:
            wf_idx = wf_at_line[idx]
            focused = cursor == n_cands + wf_idx
            text, attr = render_workflow_toggle_line(
                wf_toggles[wf_idx], focused=focused)
        elif idx in tr_at_line:
            tr_idx = tr_at_line[idx]
            focused = cursor == n_cands + n_toggles + tr_idx
            text, attr = render_then_run_line(
                then_run_items[tr_idx], focused=focused)
        else:
            text, attr = lines[idx]
        safe_addstr(stdscr, body_top + i, 0, text, attr)

    if max_scroll > 0:
        safe_addstr(stdscr, h - 2, 0,
                    f"({scroll}/{max_scroll} lines scrolled)", curses.A_DIM)

    if candidates or wf_toggles or then_run_items:
        hint = ("↑/↓ select · Space toggle · ←/→ then-run · "
                "Enter execute · Esc back")
    else:
        hint = "Enter execute · Esc back · ↑/↓ scroll"
    safe_addstr(stdscr, h - 1, 0, hint, curses.A_DIM)

    curses.curs_set(0)
    stdscr.refresh()
    return max_scroll



# ---------- Main key handler ----------------------------------------------


def _focused_message_holder(state: State):
    """Return the Repo or ChildRef whose message field is currently
    editable, or None for toggle / subtree rows."""
    if state.on_toggle:
        return None
    if state.current_repo is not None:
        return state.current_repo
    cur_child = state.current_child
    if cur_child is not None and cur_child[1].kind == "submodule":
        return cur_child[1]
    return None


def _reset_field_cursor(state: State) -> None:
    """Park the cursor at the end of the focused row's message — runs after
    every selection change so each field starts in a familiar place."""
    holder = _focused_message_holder(state)
    state.field_cursor = len(holder.message) if holder is not None else 0


def _clamp_task_selection(state: State) -> None:
    """Keep state.task_selected within the current task list and within
    the visible window. Called after navigation + after the task list
    mutates (additions, removals, prunes)."""
    n = len(state.tasks.snapshot())
    if n == 0:
        state.task_selected = 0
        state.task_scroll = 0
        return
    state.task_selected = max(0, min(state.task_selected, n - 1))


def handle_task_panel_key(state: State, key: int) -> Optional[str]:
    """Key handling while the task panel has focus. Returns the same
    action sentinels as handle_main_key so the main loop's outer dispatch
    keeps working without special cases."""
    items = state.tasks.snapshot()
    n = len(items)

    if key == curses.KEY_BTAB or key == 27:
        # Shift+Tab toggles back; Esc also returns focus to the repo list
        # rather than triggering a quit.
        state.focused_panel = "repos"
        return None

    if key in (18, curses.KEY_F5):
        return "refresh"
    if key == 19:
        return "sync"

    if n == 0:
        return None

    if key == curses.KEY_UP:
        state.task_selected = max(0, state.task_selected - 1)
        return None
    if key == curses.KEY_DOWN:
        state.task_selected = min(n - 1, state.task_selected + 1)
        return None
    if key == curses.KEY_PPAGE:
        state.task_selected = max(0, state.task_selected - 10)
        return None
    if key == curses.KEY_NPAGE:
        state.task_selected = min(n - 1, state.task_selected + 10)
        return None
    if key == curses.KEY_HOME:
        state.task_selected = 0
        return None
    if key == curses.KEY_END:
        state.task_selected = n - 1
        return None

    if key == 9:  # Tab — open the task-detail modal on the focused row
        if 0 <= state.task_selected < n:
            open_task_action_menu(state, items[state.task_selected])
        return None

    if key in (10, 13, curses.KEY_ENTER):
        # Enter on a finished task removes it. Running tasks are kept so
        # the user can't accidentally drop something mid-flight.
        if 0 <= state.task_selected < n:
            t = items[state.task_selected]
            if t.status != "running":
                state.tasks.remove(t)
                _clamp_task_selection(state)
        return None
    return None


def handle_main_key(state: State, key: int) -> Optional[str]:
    if key == curses.KEY_RESIZE:
        return None

    # Shift+Tab toggles between repo list and task panel. We handle it
    # before the focus dispatch below so it works from either side.
    if key == curses.KEY_BTAB:
        state.focused_panel = (
            "tasks" if state.focused_panel == "repos" else "repos")
        if state.focused_panel == "tasks":
            _clamp_task_selection(state)
        return None

    if state.focused_panel == "tasks":
        return handle_task_panel_key(state, key)

    if key in (18, curses.KEY_F5):  # Ctrl+R or F5 — refresh state, prune tasks
        return "refresh"
    if key == 19:  # Ctrl+S — fetch + checkout every tracked sibling
        return "sync"

    if key == curses.KEY_UP:
        state.selected = (state.selected - 1) % state.total_rows
        _reset_field_cursor(state)
        return None
    if key == curses.KEY_DOWN:
        state.selected = (state.selected + 1) % state.total_rows
        _reset_field_cursor(state)
        return None

    if key in (10, 13, curses.KEY_ENTER):
        if state.on_toggle:
            if state.selected == 0:
                state.auto_stage = not state.auto_stage
            elif state.selected == 1:
                state.auto_push = not state.auto_push
            else:
                state.align_heads = not state.align_heads
            return None
        if state.has_messages:
            return "confirm"
        return None

    if key == 9:  # Tab — open per-row action menu
        open_action_menu(state)
        return None

    target_message_holder = _focused_message_holder(state)

    if key == 27:
        if state.on_toggle:
            return "confirm-quit" if state.has_messages else "quit"
        if target_message_holder is not None and target_message_holder.message:
            target_message_holder.message = ""
            state.field_cursor = 0
            return None
        return "confirm-quit" if state.has_messages else "quit"

    if state.on_toggle:
        if key == ord(" "):
            if state.selected == 0:
                state.auto_stage = not state.auto_stage
            elif state.selected == 1:
                state.auto_push = not state.auto_push
            else:
                state.align_heads = not state.align_heads
        return None

    if target_message_holder is None:
        return None  # subtree row or otherwise non-editable

    msg = target_message_holder.message
    cur = max(0, min(state.field_cursor, len(msg)))

    if key == curses.KEY_LEFT:
        if not msg:
            kick_off_suggest_for(state, target_message_holder)
            return None
        state.field_cursor = max(0, cur - 1)
        return None
    if key == curses.KEY_SLEFT and not msg:
        kick_off_bulk_suggest(state)
        return None

    if key == curses.KEY_RIGHT:
        state.field_cursor = min(len(msg), cur + 1)
        return None
    if key == curses.KEY_HOME or key == 1:  # Home or Ctrl+A
        state.field_cursor = 0
        return None
    if key == curses.KEY_END or key == 5:  # End or Ctrl+E
        state.field_cursor = len(msg)
        return None

    if key in (curses.KEY_BACKSPACE, 127, 8):
        if cur > 0:
            target_message_holder.message = msg[: cur - 1] + msg[cur:]
            state.field_cursor = cur - 1
        return None
    if key == curses.KEY_DC:  # forward delete
        if cur < len(msg):
            target_message_holder.message = msg[:cur] + msg[cur + 1:]
        return None
    if 32 <= key < 127:
        target_message_holder.message = msg[:cur] + chr(key) + msg[cur:]
        state.field_cursor = cur + 1
        return None
    return None


# ---------- Confirm sub-loop + quit confirmation --------------------------


def ensure_cursor_visible(line_index: int, scroll: int, body_h: int) -> int:
    """Return a new scroll value that keeps line_index on-screen."""
    if line_index < scroll:
        return line_index
    if line_index >= scroll + body_h:
        return max(0, line_index - body_h + 1)
    return scroll


def confirm_quit(stdscr, state: State) -> bool:
    """Show a 'Quit and discard N message(s)? [y/N]' prompt at the bottom of
    the main screen. Returns True if the user confirms, False to cancel."""
    draw_main(stdscr, state)
    h, _ = stdscr.getmaxyx()
    n = sum(1 for r in state.repos if r.message.strip())
    plural = "" if n == 1 else "s"
    prompt = f"Quit and discard {n} commit message{plural}? [y/N]"
    try:
        stdscr.move(h - 1, 0)
        stdscr.clrtoeol()
    except curses.error:
        pass
    safe_addstr(stdscr, h - 1, 2, prompt,
                curses.color_pair(PAIR_WARN) | curses.A_BOLD)
    curses.curs_set(0)
    stdscr.refresh()
    while True:
        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return True
        if key == -1:
            continue
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), 27, 10, 13, curses.KEY_ENTER):
            return False


def handle_confirm(stdscr, state: State) -> None:
    """Inner loop for the review screen. Returns when the user confirms or
    backs out; commits run async after Enter, so we just hand off and exit.

    Cursor model: one unified list of focusable items, sorted by
    `line_index` so Up/Down navigation matches what's visible on
    screen. Each item is `(kind, obj)` where kind is "lfs", "toggle",
    or "then_run"; Space flips boolean items, ←/→ cycles a then-run
    selector through its repo's dispatchable workflows."""
    lines, candidates, wf_toggles, then_run_items = build_confirm_lines(state)
    # Build one focus list ordered by visible line so a Down keystroke
    # always lands on the row directly below — toggles + then-run rows
    # were interleaved on screen but were previously cursored as two
    # separate blocks, which made Down "skip past" a then-run row and
    # come back to it later.
    focusables: List[Tuple[str, object]] = []
    for c in candidates:
        focusables.append(("lfs", c))
    for tog in wf_toggles:
        focusables.append(("toggle", tog))
    for s in then_run_items:
        focusables.append(("then_run", s))
    focusables.sort(key=lambda kv: kv[1].line_index)
    n_focus = len(focusables)
    cursor = 0 if n_focus else -1
    scroll = 0
    while True:
        if cursor >= 0:
            h, _ = stdscr.getmaxyx()
            body_h = max(1, h - 4)
            focus_line = focusables[cursor][1].line_index
            scroll = ensure_cursor_visible(focus_line, scroll, body_h)
        # `draw_confirm` keeps its existing interface (a single `cursor`
        # value mapped through n_cands / n_toggles to figure out which
        # item is focused). We derive that legacy index from the
        # currently-focused entry in our line-ordered focus list so the
        # highlight stays aligned with what `cursor` actually points at.
        n_cands = len(candidates)
        n_toggles = len(wf_toggles)
        legacy_cursor = -1
        if cursor >= 0:
            kind, obj = focusables[cursor]
            if kind == "lfs":
                legacy_cursor = candidates.index(obj)
            elif kind == "toggle":
                legacy_cursor = n_cands + wf_toggles.index(obj)
            else:  # then_run
                legacy_cursor = (
                    n_cands + n_toggles + then_run_items.index(obj))
        max_scroll = draw_confirm(
            stdscr, lines, candidates, wf_toggles, then_run_items,
            legacy_cursor, scroll)
        scroll = min(scroll, max_scroll)
        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return
        if key == curses.KEY_RESIZE:
            continue
        if key == 27:
            return
        if key in (10, 13, curses.KEY_ENTER):
            kick_off_workers(state, candidates)
            return  # async pipeline takes over the sidebar
        if key == ord(" ") and cursor >= 0:
            kind, obj = focusables[cursor]
            if kind == "lfs":
                obj.track = not obj.track
            elif kind == "toggle":
                cur = obj.repo.track_workflow.get(obj.workflow_name, False)
                obj.repo.track_workflow[obj.workflow_name] = not cur
            # Space on a then-run row is a no-op — use ←/→ to cycle.
            continue
        if (key in (curses.KEY_LEFT, curses.KEY_RIGHT)
                and cursor >= 0
                and focusables[cursor][0] == "then_run"):
            cycle_then_run(
                focusables[cursor][1],
                -1 if key == curses.KEY_LEFT else 1)
            continue
        if key == curses.KEY_UP:
            if cursor >= 0:
                cursor = max(0, cursor - 1)
            else:
                scroll = max(0, scroll - 1)
        elif key == curses.KEY_DOWN:
            if cursor >= 0:
                cursor = min(n_focus - 1, cursor + 1)
            else:
                scroll = min(max_scroll, scroll + 1)
        elif key == curses.KEY_PPAGE:
            scroll = max(0, scroll - 10)
        elif key == curses.KEY_NPAGE:
            scroll = min(max_scroll, scroll + 10)


# ---------- Initial empty-repo screen helper ------------------------------


def show_no_repos_message(stdscr, workspace: Path) -> None:
    """Used at startup if discovery finds no git repos under workspace."""
    safe_addstr(stdscr, 0, 0,
                f"no git repos found under {workspace}",
                curses.color_pair(PAIR_ERR))
    safe_addstr(stdscr, 2, 0,
                f"edit {CONFIG_FILE.name} to point at a different root, then re-run.",
                curses.A_DIM)
    stdscr.refresh()
    stdscr.timeout(-1)
    stdscr.getch()
