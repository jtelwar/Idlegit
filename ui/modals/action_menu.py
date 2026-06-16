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

from core.models import (
    ActionMenu, ActionMenuItem, ActionSubmenuFrame, ChildRef,
    CommitEntry, FileEntry, Repo, State,
)
from core.git_ops import (
    gh_available, load_commits, parse_github_slug, query_target_state,
    query_working_tree,
)
from core.workers import kick_off_action

from ..colors import (
    PAIR_DLG_MAGENTA, PAIR_DLG_WARN, PAIR_DLG_ERR, PAIR_DLG_OK,
    PAIR_DLG_PASTEL_BLUE, PAIR_DLG_PASTEL_GREEN, PAIR_DLG_PASTEL_RED, PAIR_DLG_PASTEL_YELLOW,
    PAIR_DLG_CYAN, PAIR_DLG_FG,
)
from ..geometry import (
    draw_modal_fill, end_truncate, modal_geometry, safe_addstr, truncate,
)
from ..hints import (
    KEY_DOWN, KEY_ENTER, KEY_ESC, KEY_HOME, KEY_LEFT, KEY_LEFT_RIGHT,
    KEY_RIGHT, KEY_TAB, KEY_UP_DOWN, Hint, render_hints,
)
from ..sidebar import SPINNER_FRAMES
from .diff_viewer import handle_diff_viewer_key, open_diff_viewer


def _spinner_glyph(state: State) -> str:
    """Current spinner frame, picked from the same global tick the
    sidebar uses so every animated indicator in the app stays in sync."""
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


def _hints_action_focus(menu: ActionMenu) -> list:
    """Footer hints when the action items list has focus. Enter's
    description names the focused item; disabled items show why.
    Submenu rows show Right/Left navigation hints (Down-into-pane is
    swapped out when in a submenu since the bottom pane is owned by
    the top level only)."""
    items = _current_items(menu)
    selected = _current_selected(menu)
    hints = [Hint(KEY_UP_DOWN, "select")]
    if 0 <= selected < len(items):
        item = items[selected]
        if item.is_back:
            parent = "main menu"
            if len(menu.submenu_stack) >= 2:
                parent = menu.submenu_stack[-2].label
            hints.append(Hint(KEY_ENTER, f"back to {parent}"))
        elif item.id.startswith("remote:"):
            # Remote rows have three actions on shortcut keys —
            # Enter (set URL) is the most common, R renames, D
            # deletes (with confirm).
            hints.append(Hint(KEY_ENTER, "edit url"))
            hints.append(Hint("r", "rename"))
            hints.append(Hint("d", "delete"))
        elif item.has_submenu:
            hints.append(Hint(KEY_RIGHT, f"open {item.label} menu"))
            hints.append(Hint(KEY_ENTER, f"open {item.label} menu"))
        elif item.enabled:
            hints.append(Hint(KEY_ENTER, item.label))
        else:
            reason = f" ({item.reason})" if item.reason else ""
            hints.append(Hint(KEY_ENTER, f"unavailable{reason}"))
    if _in_submenu(menu):
        parent = (menu.submenu_stack[-2].label
                  if len(menu.submenu_stack) >= 2 else "main menu")
        hints.append(Hint(KEY_LEFT, f"back to {parent}"))
    else:
        hints.append(Hint(KEY_DOWN, "into pane"))
    hints.append(Hint(KEY_TAB, "close"))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


def _hints_pane_focus(menu: ActionMenu) -> list:
    """Footer hints when the bottom pane (working tree / commits) has
    focus. Tab swap is presented as ←/→ to match how the existing UI
    handles the same physical motion."""
    other_tab = "commits" if menu.pane_tab == "tree" else "working tree"
    hints = [
        Hint(KEY_UP_DOWN, "select"),
        Hint(KEY_LEFT_RIGHT, f"switch to {other_tab}"),
    ]
    if menu.pane_tab == "tree" and menu.tree_selected > 0:
        hints.append(Hint(KEY_TAB, "view diff"))
    hints.append(Hint(KEY_HOME, "back to actions"))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


def _draw_action_hints(stdscr, menu: ActionMenu, y: int, x: int,
                       w: int, attr: int) -> None:
    """Single call site keeps render_hints visibly used so the
    autoformatter doesn't strip it from the import block on
    subsequent edits. Confirm prompts and inline edit modes paint
    their own footer text instead of the regular hint list."""
    if menu.confirm_message:
        # Bold yellow strip — same treatment the standalone remotes
        # modal's confirm row uses, so the confirm UX reads the same
        # everywhere.
        from ..colors import PAIR_DLG_WARN
        text = menu.confirm_message
        safe_addstr(stdscr, y, x,
                    text[:max(0, w)],
                    curses.color_pair(PAIR_DLG_WARN) | curses.A_BOLD)
        return
    if menu.edit_field:
        from ..hints import KEY_BACKSPACE
        if menu.edit_field == "rename_remote":
            verb = "rename"
        elif menu.edit_field == "set_url_remote":
            verb = "set url"
        elif menu.edit_field == "add_remote_name":
            verb = "next: enter URL"
        elif menu.edit_field == "add_remote_url":
            verb = "add remote"
        else:
            verb = "save"
        hints = [
            Hint("type", "edit"),
            Hint(KEY_BACKSPACE, "delete char"),
            Hint(KEY_ENTER, verb),
            Hint(KEY_ESC, "cancel"),
        ]
        render_hints(stdscr, y, x, w, hints, attr=attr)
        return
    hints = (_hints_pane_focus(menu) if menu.pane_focus
             else _hints_action_focus(menu))
    render_hints(stdscr, y, x, w, hints, attr=attr)


# Map a porcelain XY status pair to a pastel colour pair. Ordered so
# the most-impactful change (delete > add > rename > modify) wins —
# a "DM" file (deleted-staged, then re-modified in-tree) reads as a
# delete first, which matches what the user is going to commit.
def _file_status_pair(x: str, y: str) -> Optional[int]:
    pair = (x, y)
    if "U" in pair or pair == ("A", "A") or pair == ("D", "D"):
        return PAIR_DLG_PASTEL_RED
    if "D" in pair:
        return PAIR_DLG_PASTEL_RED
    if "A" in pair:
        return PAIR_DLG_PASTEL_GREEN
    if "R" in pair:
        return PAIR_DLG_PASTEL_BLUE
    if "M" in pair:
        return PAIR_DLG_PASTEL_YELLOW
    return None


# Number of commits to load per page. The first page fires from a
# background thread the moment the modal is installed (so opening is
# instant on slow repos like a workspace root with many submodules);
# subsequent pages fire from a worker thread when the cursor scrolls
# within PREFETCH_THRESHOLD of the loaded end.
COMMITS_PAGE = 30
PREFETCH_THRESHOLD = 5

# Modal sizing.
MODAL_W = 90
PANE_TARGET_ROWS = 12  # rows visible in the bottom pane (cap)


# ---------- Open ----------------------------------------------------------


def _has_any_workflow(workflows_repo: Optional[Repo]) -> bool:
    """True iff `workflows_repo` is on a github.com remote AND has at
    least one workflow file discovered locally. Mirrors the gating used
    by the action-menu's "run a workflow…" item."""
    if workflows_repo is None or not gh_available():
        return False
    slug = parse_github_slug(workflows_repo.remote_url_raw)
    return bool(slug) and bool(workflows_repo.workflows)


def _run_workflow_reason(workflows_repo: Optional[Repo]) -> str:
    """Disabled-reason string for the "run a workflow…" item. Empty
    when workflows are available."""
    if _has_any_workflow(workflows_repo):
        return ""
    if workflows_repo is None or not gh_available():
        return "gh CLI / repo unavailable"
    if not parse_github_slug(workflows_repo.remote_url_raw):
        return "no github remote"
    return "no workflows in this repo"


def _build_main_items(branch_meta,
                      stash_count: int = 0,
                      remote_count: int = 0) -> List[ActionMenuItem]:
    """Main menu — repo-level actions plus four submenu openers
    (branch / actions / stashes / remotes). Anything that fans out
    into multiple sub-actions lives behind a submenu so the top
    level stays short and stable. The dynamic submenus (Stashes and
    Remotes) carry a `(N)` count in the opener label, queried at
    open / re-entry time."""
    has_origin = branch_meta["has_origin"]
    upstream = branch_meta["upstream"]
    merging = branch_meta["merging"]
    items: List[ActionMenuItem] = []
    # When a merge is already in progress the branch submenu is locked,
    # so surface the safe-merge resolver at the top level — this is the
    # discoverable way to step through and finish an existing conflicted
    # merge (whether idlegit or a bare `git merge` started it).
    if merging:
        items.append(ActionMenuItem(
            id="safe_merge", label="resolve merge conflicts…",
            enabled=True))
    items += [
        ActionMenuItem(
            id="fetch", label="fetch (all branches)",
            enabled=has_origin,
            reason="" if has_origin else "no origin"),
        ActionMenuItem(
            id="pull", label="pull",
            enabled=has_origin and upstream is not None and not merging,
            reason=("merging" if merging
                    else ("no upstream" if upstream is None
                          else ("" if has_origin else "no origin")))),
        ActionMenuItem(
            id="push", label="push",
            enabled=has_origin,
            reason="" if has_origin else "no origin"),
        ActionMenuItem(
            id="branch_submenu", label="branch",
            enabled=not merging,
            reason="" if not merging else "merging",
            has_submenu=True),
        ActionMenuItem(
            id="actions_submenu", label="actions",
            enabled=True, has_submenu=True),
        ActionMenuItem(
            id="stashes_submenu",
            label=f"stashes ({stash_count})",
            enabled=True, has_submenu=True),
        ActionMenuItem(
            id="remotes_submenu",
            label=f"remotes ({remote_count})",
            enabled=True, has_submenu=True),
    ]
    return items


def _back_item() -> ActionMenuItem:
    """Synthetic top-of-submenu row. Always selectable; pressing
    Enter (or Left) pops the current frame. The actual submenu name
    lives in the breadcrumb header above the items, so this row's
    label is just the literal word "back"."""
    return ActionMenuItem(id="back", label="back",
                          enabled=True, is_back=True)


def _build_branch_items(branch_meta) -> List[ActionMenuItem]:
    """Branch submenu — switch / save HEAD / merge / rename / set
    upstream. Each action carries its own enable/reason rules so the
    submenu stays self-explanatory even when half the items are
    blocked by detached HEAD or a merge in progress."""
    has_origin = branch_meta["has_origin"]
    merging = branch_meta["merging"]
    branch = branch_meta.get("branch") or ""
    detached = (not branch) or branch == "(detached)"
    return [
        _back_item(),
        ActionMenuItem(
            id="switch_branch", label="switch branch…",
            enabled=not merging,
            reason="" if not merging else "merging"),
        ActionMenuItem(
            id="checkout_remote_branch",
            label="checkout remote branch…",
            enabled=not merging,
            reason="" if not merging else "merging"),
        # Detached-HEAD recovery: park HEAD's commits on a fresh branch
        # so the user can push / merge them via the normal flows. Only
        # surfaced when actually detached — not useful otherwise.
        ActionMenuItem(
            id="branch_from_head", label="save HEAD to new branch…",
            enabled=detached and not merging,
            reason=("merging" if merging
                    else ("" if detached else "already on a branch"))),
        # FF-only merge of another branch into the current one. Refuses
        # on its own when a real merge would be needed; that's exactly
        # the safety we want here.
        ActionMenuItem(
            id="merge_branch", label="merge in branch…",
            enabled=(not detached) and (not merging),
            reason=("merging" if merging
                    else ("detached HEAD" if detached else ""))),
        # Safe-merge: step through conflicts block-by-block, pick a side
        # per conflict, then commit + (for submodules) sync. Enabled while
        # merging too — that's when it ADOPTS the in-progress merge and
        # resolves it ("resolve merge conflicts…").
        ActionMenuItem(
            id="safe_merge",
            label=("resolve merge conflicts…" if merging
                   else "safe-merge in branch…"),
            enabled=merging or (not detached),
            reason=("" if (merging or not detached) else "detached HEAD")),
        ActionMenuItem(
            id="rename_branch", label="rename branch…",
            enabled=(not detached) and (not merging),
            reason=("merging" if merging
                    else ("detached HEAD" if detached else ""))),
        ActionMenuItem(
            id="set_upstream", label="set upstream…",
            enabled=(not detached) and (not merging) and has_origin,
            reason=("merging" if merging
                    else ("detached HEAD" if detached
                          else ("" if has_origin else "no origin")))),
    ]


def _build_actions_items(branch_meta) -> List[ActionMenuItem]:
    """Actions submenu — soft reset and run-a-workflow. Both used to
    live on the main menu; grouped here so future actions-style
    operations (cancel a run, view recent runs, reflog viewer…) can
    join them without crowding the top level."""
    ahead = branch_meta.get("ahead", 0) if branch_meta else 0
    return [
        _back_item(),
        ActionMenuItem(
            id="soft_reset",
            label=f"soft reset ({ahead} unpushed)…",
            enabled=ahead > 0,
            reason="" if ahead > 0 else "no unpushed commits"),
        ActionMenuItem(
            id="run_workflow", label="run a workflow…",
            enabled=branch_meta["has_any_workflow"]
            if branch_meta else False,
            reason=(branch_meta["run_workflow_reason"]
                    if branch_meta else "")),
    ]


def _build_stashes_items(stashes: "list[tuple[str, str]]"
                         ) -> List[ActionMenuItem]:
    """Stashes submenu — back · new stash · ─── · stash@{0} … N.

    Each stash row's id is `stash:<ref>` so the dispatcher can pull
    the original ref back out at Enter time. Has-submenu marker
    causes Enter to push a per-stash frame with the apply action."""
    items: List[ActionMenuItem] = [
        _back_item(),
        ActionMenuItem(id="stash_create", label="new stash"),
        ActionMenuItem(id="sep_after_new", label="",
                       enabled=False, is_separator=True),
    ]
    for ref, msg in stashes:
        # Show the ref alongside the human message so the user has
        # both the index ("stash@{0}") and the descriptive line.
        label = f"{ref}  {msg}" if msg else ref
        items.append(ActionMenuItem(
            id=f"stash:{ref}", label=label, has_submenu=True))
    if not stashes:
        items.append(ActionMenuItem(
            id="stash_empty", label="(no stashes yet)",
            enabled=False))
    return items


def _build_remotes_items(remotes: "list[tuple[str, str]]"
                         ) -> List[ActionMenuItem]:
    """Remotes submenu — back · new remote · ─── · <remote rows>.

    Each remote row's id is `remote:<name>` so the dispatcher can
    pull the name back at Enter time. Pressing Enter on a remote
    row activates inline rename mode (the row's name becomes
    editable); the handler renders a confirm prompt before applying
    the rename. D triggers an inline delete (also confirmed)."""
    items: List[ActionMenuItem] = [
        _back_item(),
        ActionMenuItem(id="new_remote", label="new remote"),
        ActionMenuItem(id="sep_after_new", label="",
                       enabled=False, is_separator=True),
    ]
    for name, url in remotes:
        # The label shows both name and URL so users see the full
        # picture on the row. URL editing isn't exposed in this
        # iteration — rename is the inline action; full URL edits
        # would be a future per-remote sub-sub-menu.
        label = f"{name}: {url}" if url else name
        items.append(ActionMenuItem(
            id=f"remote:{name}", label=label))
    if not remotes:
        items.append(ActionMenuItem(
            id="remote_empty", label="(no remotes configured)",
            enabled=False))
    return items


def _build_stash_apply_items(ref: str) -> List[ActionMenuItem]:
    """Per-stash sub-sub-menu — currently just `apply`. We
    deliberately do NOT offer pop / drop here: removing a stash ref
    is a cardinal-rule violation. Apply leaves the entry in place so
    the user can re-attempt or inspect later."""
    return [
        _back_item(),
        ActionMenuItem(
            id=f"stash_apply:{ref}",
            label=f"apply {ref}",
            enabled=True),
    ]


def _in_submenu(menu: ActionMenu) -> bool:
    return bool(menu.submenu_stack)


def _current_items(menu: ActionMenu) -> List[ActionMenuItem]:
    """Items currently displayed — top-of-stack frame when in a
    submenu, the main menu items otherwise."""
    if menu.submenu_stack:
        return menu.submenu_stack[-1].items
    return menu.items


def _current_selected(menu: ActionMenu) -> int:
    if menu.submenu_stack:
        return menu.submenu_stack[-1].selected
    return menu.selected


def _set_current_selected(menu: ActionMenu, value: int) -> None:
    if menu.submenu_stack:
        menu.submenu_stack[-1].selected = value
    else:
        menu.selected = value


def _breadcrumb_segments(menu: ActionMenu) -> List[str]:
    """Path of names rendered in the breadcrumb above the items —
    `repo` first, then each pushed submenu's `label`. The trailing
    segment is the user's current location."""
    segs = ["repo"]
    for frame in menu.submenu_stack:
        segs.append(frame.label)
    return segs


def _first_actionable_index(items: List[ActionMenuItem]) -> int:
    """First selectable index, skipping back-rows and separators —
    the cursor's natural landing spot when entering a submenu."""
    for i, it in enumerate(items):
        if it.enabled and not it.is_back and not it.is_separator:
            return i
    # Fall back to the back row (always selectable) so the cursor at
    # least lands on something interactive.
    for i, it in enumerate(items):
        if it.enabled and not it.is_separator:
            return i
    return 0


def _push_submenu(menu: ActionMenu, name: str, label: str,
                  items: List[ActionMenuItem]) -> None:
    """Push a new submenu frame onto the stack. Cursor lands on the
    first real action (skipping the back row)."""
    menu.submenu_stack.append(ActionSubmenuFrame(
        name=name, label=label, items=items,
        selected=_first_actionable_index(items),
    ))


def _pop_submenu(menu: ActionMenu) -> None:
    """Drop the top submenu frame — Left or "back" pressed."""
    if menu.submenu_stack:
        menu.submenu_stack.pop()


def _state_label_for(branch_meta):
    """(label, color_pair) pair driving the modal's status badge.
    Same precedence as the existing UI: merging > diverged > dirty >
    behind > ahead > no-upstream > clean."""
    if branch_meta["merging"]:
        return "merging", PAIR_DLG_ERR
    if branch_meta["ahead"] > 0 and branch_meta["behind"] > 0:
        return "diverged", PAIR_DLG_ERR
    if branch_meta["dirty"]:
        return "dirty", PAIR_DLG_WARN
    if branch_meta["behind"] > 0:
        return "behind", PAIR_DLG_MAGENTA
    if branch_meta["ahead"] > 0:
        return "ahead", PAIR_DLG_CYAN
    if branch_meta["upstream"] is None:
        return "no upstream", 0
    return "clean", PAIR_DLG_OK


def _initial_meta_from_cache(target_repo: Optional[Repo],
                             target_child: Optional[ChildRef],
                             workflows_repo: Optional[Repo]) -> dict:
    """Build the same shape of metadata `_build_items`/`_state_label_for`
    consume, sourced entirely from values already cached on the Repo /
    ChildRef by `refresh_repo` / `link_siblings`. No git calls. The
    async loader replaces this with `query_target_state` output the
    moment that finishes."""
    if target_repo is not None:
        branch = target_repo.branch
        upstream = target_repo.upstream
        ahead = target_repo.ahead
        behind = target_repo.behind
        merging = target_repo.merging
        dirty = target_repo.is_dirty
        has_origin = bool(target_repo.remote_url)
    elif target_child is not None:
        branch = target_child.branch
        upstream = target_child.upstream
        ahead = target_child.ahead
        behind = target_child.behind
        merging = target_child.merging
        dirty = target_child.dirty
        # Children don't carry has_origin directly — assume True since
        # they were checked out from origin. The async query will
        # correct this in a moment if it's wrong.
        has_origin = True
    else:
        branch = ""
        upstream = None
        ahead = 0
        behind = 0
        merging = False
        dirty = False
        has_origin = False
    return {
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "merging": merging,
        "dirty": dirty,
        "has_origin": has_origin,
        "has_any_workflow": _has_any_workflow(workflows_repo),
        "run_workflow_reason": _run_workflow_reason(workflows_repo),
    }


def open_action_menu(state: State) -> None:
    """Build and install the ActionMenu modal for the focused row.
    No-op when the cursor is on the title-row workspace selector,
    since there's no repo to act on there.

    The modal is installed INSTANTLY using values already cached on
    the Repo / ChildRef (branch, upstream, ahead/behind, dirty…).
    Three background workers then refresh the state badge + items,
    populate the working-tree pane, and load the first commits page.
    Each pane shows a spinner-prefixed "loading…" line until its
    worker completes. This keeps Tab snappy on slow repos like a
    workspace root with many submodules — `git status` on the root
    can take a second or more, which used to block the keypress
    handler and made Tab feel like it needed multiple presses."""
    if state.on_workspace_row:
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

    workflows_repo = target_repo or (target_child.repo if target_child else None)
    meta = _initial_meta_from_cache(target_repo, target_child, workflows_repo)
    # Synchronous one-shot queries at open time so the dynamic
    # main-menu opener labels (`stashes (N)`, `remotes (N)`) reflect
    # the on-disk state. Both are fast even on large repos and
    # avoid re-running on every state refresh.
    from core.git_ops import list_stashes, list_remotes
    stashes = list_stashes(target_path)
    remotes_list = list_remotes(target_path)
    stash_count = len(stashes)
    remote_count = len(remotes_list)
    items = _build_main_items(meta, stash_count=stash_count,
                              remote_count=remote_count)
    initial = _first_actionable_index(items)
    state_label, state_pair = _state_label_for(meta)

    menu = ActionMenu(
        target_label=label,
        target_path=target_path,
        target_repo=target_repo,
        target_parent=target_parent,
        target_child=target_child,
        branch=meta["branch"],
        upstream=meta["upstream"],
        ahead=meta["ahead"],
        behind=meta["behind"],
        state_label=state_label,
        state_pair=state_pair,
        items=items,
        selected=initial,
        cached_meta=meta,
        stash_count=stash_count,
        stashes=stashes,
        remotes_list=remotes_list,
        remote_count=remote_count,
        state_loading=True,
        tree_loading=True,
        commits_loading=True,
    )
    state.action_menu = menu

    # Kick off all three async populators. Each one re-checks the
    # cancel_event before mutating the menu so closing the modal mid-
    # query is a clean no-op rather than a write to a dead struct.
    _kick_off_state_load(menu, workflows_repo)
    _kick_off_tree_load(menu)
    _kick_off_initial_commits(menu)


def _kick_off_state_load(menu: ActionMenu,
                         workflows_repo: Optional[Repo]) -> None:
    """Run `query_target_state` in a daemon thread and overwrite the
    menu's badge + items + branch metadata when it completes. This is
    the slow query for repos with many submodules — `git status` and
    its kin recurse into each one — so it's the most important to
    keep off the keypress handler."""
    path = menu.target_path

    def worker() -> None:
        try:
            if menu.cancel_event.is_set():
                return
            ts = query_target_state(path)
            if menu.cancel_event.is_set():
                return
            meta = {
                "branch": ts.branch,
                "upstream": ts.upstream,
                "ahead": ts.ahead,
                "behind": ts.behind,
                "merging": ts.merging,
                "dirty": ts.dirty,
                "has_origin": ts.has_origin,
                "has_any_workflow": _has_any_workflow(workflows_repo),
                "run_workflow_reason": _run_workflow_reason(workflows_repo),
            }
            menu.branch = ts.branch
            menu.upstream = ts.upstream
            menu.ahead = ts.ahead
            menu.behind = ts.behind
            menu.state_label, menu.state_pair = _state_label_for(meta)
            menu.cached_meta = meta
            menu.items = _build_main_items(
                meta, stash_count=menu.stash_count,
                remote_count=menu.remote_count)
            # If the user hasn't moved their cursor yet, re-snap to
            # the first enabled item — the cached snapshot may have
            # marked items enabled that the fresh query disables (or
            # vice versa).
            if menu.selected < len(menu.items) and not menu.items[
                    menu.selected].enabled:
                menu.selected = _first_actionable_index(menu.items)
            # Refresh the visible submenu's items too if it's one we
            # know how to rebuild from `meta`. Keeps disable/enable
            # reasons in sync (e.g. a fetch that lands while the user
            # is in the branch submenu re-evaluates "no upstream").
            if menu.submenu_stack:
                top = menu.submenu_stack[-1]
                if top.name == "branch":
                    top.items = _build_branch_items(meta)
                elif top.name == "actions":
                    top.items = _build_actions_items(meta)
                if top.selected < len(top.items):
                    sel_item = top.items[top.selected]
                    if not sel_item.enabled or sel_item.is_separator:
                        top.selected = _first_actionable_index(top.items)
        finally:
            menu.state_loading = False

    threading.Thread(target=worker, daemon=True).start()


def _kick_off_tree_load(menu: ActionMenu) -> None:
    """Run `query_working_tree` in a daemon thread and replace the
    tree_files list when done. Until then `tree_loading` is True
    and the pane shows a spinner."""
    path = menu.target_path

    def worker() -> None:
        try:
            if menu.cancel_event.is_set():
                return
            files = query_working_tree(path)
            if menu.cancel_event.is_set():
                return
            menu.tree_files = files
        finally:
            menu.tree_loading = False

    threading.Thread(target=worker, daemon=True).start()


def _kick_off_initial_commits(menu: ActionMenu) -> None:
    """First page of `git log` — same shape as `_kick_off_commits_page`
    but tagged as the initial load (the pane shows "loading commits…"
    instead of "loading more" when commits_full is still empty)."""
    path = menu.target_path

    def worker() -> None:
        try:
            if menu.cancel_event.is_set():
                return
            page, exhausted = load_commits(path, 0, COMMITS_PAGE)
            if menu.cancel_event.is_set():
                return
            menu.commits_full = page
            menu.commits_exhausted = exhausted
        finally:
            menu.commits_loading = False

    threading.Thread(target=worker, daemon=True).start()


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


def _scroll_for_cursor(text: str, cur: int,
                       width: int) -> "tuple[str, int]":
    """Crop `text` to `width` so the cursor stays visible. When the
    buffer fits, returns it unchanged with the cursor at its natural
    offset; when it doesn't, slides a window over the buffer and
    returns the cursor offset within that window. Mirrors the
    workspace_menu inline-edit helper so paste-of-long-URLs into the
    name/url cells doesn't push the cursor off-screen."""
    cur = max(0, min(cur, len(text)))
    width = max(1, width)
    if len(text) <= width - 1 or width <= 1:
        return text[: width], cur
    half = (width - 1) // 2
    start = max(0, min(cur - half, len(text) - (width - 1)))
    return text[start:start + width - 1], cur - start


def _place_inline_cursor(stdscr, y: int, x: int) -> None:
    """Move the terminal cursor to (y, x) and make it visible. Wrapped
    in try/except since `move` raises on out-of-bounds, which can
    happen during a resize race."""
    try:
        stdscr.move(y, x)
        curses.curs_set(2)
    except curses.error:
        pass


def draw_action_menu(stdscr, state: State, sidebar_x: int) -> None:
    menu = state.action_menu
    if menu is None:
        return

    items = _current_items(menu)
    selected = _current_selected(menu)
    # Reserve enough action rows to fit whichever list is longest
    # across all menu levels so the modal doesn't shrink/grow
    # visibly when the user enters or exits a submenu. Stash entries
    # plus the back/new-stash/separator chrome can outsize the main
    # menu, so include the cached stash count too.
    main_count = len(menu.items)
    branch_count = len(_build_branch_items(menu.cached_meta or {}))
    actions_count = len(_build_actions_items(menu.cached_meta or {}))
    stashes_count = len(_build_stashes_items(menu.stashes))
    remotes_count = len(_build_remotes_items(menu.remotes_list))
    n_items = max(main_count, branch_count, actions_count,
                  stashes_count, remotes_count)
    # Header (title + spacer + branch + upstream + sep) = 5 rows;
    # breadcrumb row (always reserved) = 1; actions = n_items rows;
    # separator = 1; tab header = 1; filter = 1; pane list = up to
    # PANE_TARGET_ROWS; footer hint = 1; padding = 2. Trailing +1
    # reserves a blank row below the footer for visual breathing —
    # the existing layout already has a blank above the title via
    # the leading "1" component.
    content_h = (1 + 1 + 1 + 1 + 1
                 + 1
                 + n_items + 1
                 + 1 + 1
                 + PANE_TARGET_ROWS
                 + 1 + 1
                 + 1)
    x, y, w, h = modal_geometry(stdscr, sidebar_x, MODAL_W, content_h)
    sb = curses.color_pair(PAIR_DLG_FG)
    draw_modal_fill(stdscr, x, y, w, h, sb)

    inner_x = x + 2
    inner_w = w - 4

    # Title row: repo name (cyan-bold) + middle-truncated full path
    # in brackets (dim) so users with multiple repository_folders can
    # tell which on-disk location this menu targets at a glance.
    # The repo name uses end-only truncation (no middle-truncation of
    # repo names — modal-wide rule); only the on-disk path keeps the
    # middle-truncation since the leaf folder is what users recognise.
    name = menu.target_label
    path_str = str(menu.target_path)
    name_clip = end_truncate(name, inner_w)
    safe_addstr(stdscr, y + 1, inner_x, name_clip,
                curses.A_BOLD | curses.color_pair(PAIR_DLG_CYAN))
    # Reserve room for "  [<path>]" — 4 cells of fixed chrome around
    # the truncated path. min length 3 so we never collapse to "[…]".
    avail = inner_w - len(name_clip) - 4
    if avail >= 3:
        path_trunc = truncate(path_str, avail, mode="middle")
        safe_addstr(stdscr, y + 1, inner_x + len(name_clip),
                    f"  [{path_trunc}]", sb | curses.A_DIM)

    line = y + 3
    branch_label = menu.branch or "(loading…)"
    branch_str = f"[{branch_label}]"
    safe_addstr(stdscr, line, inner_x, branch_str,
                curses.color_pair(PAIR_DLG_CYAN))
    if menu.state_loading:
        # Spinner + neutral "checking…" badge while query_target_state
        # is in flight. Matches the sidebar spinner so the user reads
        # the modal-level loading indicator the same way as everywhere
        # else in the app.
        spin = _spinner_glyph(state)
        safe_addstr(stdscr, line, inner_x + len(branch_str) + 1,
                    f"{spin} checking…", sb | curses.A_DIM)
    else:
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

    # Breadcrumb header — `repo › branch › Stashes`. Earlier
    # segments dim, current segment in accent cyan-bold so the user
    # always sees where they are. The row is always reserved (kept
    # blank on the main menu) so the modal layout doesn't shift when
    # the user pushes into a submenu.
    line += 1
    if menu.submenu_stack:
        segs = _breadcrumb_segments(menu)
        cx = inner_x
        sep = " › "
        for i, seg in enumerate(segs):
            is_last = (i == len(segs) - 1)
            if is_last:
                seg_attr = (curses.color_pair(PAIR_DLG_CYAN)
                            | curses.A_BOLD)
            else:
                seg_attr = sb | curses.A_DIM
            text = seg
            if cx + len(text) > inner_x + inner_w:
                text = end_truncate(text, inner_x + inner_w - cx)
            safe_addstr(stdscr, line, cx, text, seg_attr)
            cx += len(text)
            if not is_last:
                if cx + len(sep) > inner_x + inner_w:
                    break
                safe_addstr(stdscr, line, cx, sep, sb | curses.A_DIM)
                cx += len(sep)
    line += 1

    # Action items — `items` and `selected` come from the
    # main-or-submenu helpers so the same render path serves both.
    # The caret column 0 is reserved across every row: rows that
    # open a submenu paint a `›` here (always visible, dim when not
    # focused, bright when focused); regular focused rows paint the
    # focus arrow `→`; everything else gets blank padding so labels
    # align across the list. Separators render as a dim hairline.
    rendered = 0
    for i, item in enumerate(items):
        focused = (i == selected and not menu.pane_focus
                   and not item.is_separator)
        if item.is_separator:
            safe_addstr(stdscr, line, inner_x,
                        ("  " + "─" * max(1, inner_w - 4)
                         + "  ").ljust(inner_w),
                        sb | curses.A_DIM)
            line += 1
            rendered += 1
            continue
        # Column 0 caret / focus arrow.
        if item.has_submenu:
            col0 = "› "
        elif focused:
            col0 = "→ "
        else:
            col0 = "  "
        label = item.label
        if not item.enabled and item.reason:
            label = f"{label}  ({item.reason})"
        # Attribute selection — back rows render as dim cyan
        # breadcrumb-style, regardless of state.
        if item.is_back:
            attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_DIM
            if focused:
                attr |= curses.A_REVERSE
        elif focused and item.enabled:
            attr = sb | curses.A_REVERSE
        elif focused:
            attr = sb | curses.A_REVERSE | curses.A_DIM
        elif not item.enabled:
            attr = sb | curses.A_DIM
        else:
            attr = sb
        inline_editing = (focused and bool(menu.edit_field)
                          and item.id == menu.edit_target_id)
        is_remote_row = item.id.startswith("remote:")
        if is_remote_row:
            # Two-column render: name on the left, URL on the right.
            # Width auto-fits the longest cached remote name (capped
            # at half the row); the URL column takes the rest.
            remote_name = item.id.split(":", 1)[1]
            remote_url = ""
            for n, u in menu.remotes_list:
                if n == remote_name:
                    remote_url = u
                    break
            name_w = max(8, max(
                (len(n) for n, _ in menu.remotes_list), default=8))
            name_w = min(name_w, max(8, (inner_w - 4) // 2))
            sep = "  "
            name_x = inner_x + len(col0)
            url_x = name_x + name_w + len(sep)
            url_w = max(1, inner_w - (url_x - inner_x))
            # Background paint for the whole row first so attrs
            # stay contiguous when reverse-video kicks in.
            safe_addstr(stdscr, line, inner_x, " " * inner_w, attr)
            safe_addstr(stdscr, line, inner_x, col0, attr)
            # Determine per-column display + per-column attrs based
            # on which (if any) field is being edited inline. The
            # actively-edited cell carries a real terminal cursor —
            # see _place_inline_cursor below for placement.
            edit_cell_x = -1
            if inline_editing and menu.edit_field == "rename_remote":
                name_text = menu.edit_typed
                name_attr = sb | curses.A_REVERSE
                url_text = remote_url
                url_attr = attr
                edit_cell_x = name_x
            elif inline_editing and menu.edit_field == "set_url_remote":
                name_text = remote_name
                name_attr = attr
                url_text = menu.edit_typed
                url_attr = sb | curses.A_REVERSE
                edit_cell_x = url_x
            else:
                name_text = remote_name
                url_text = remote_url
                name_attr = attr
                url_attr = attr
            name_render = (name_text if inline_editing
                           and edit_cell_x == name_x
                           else end_truncate(name_text, name_w))
            url_render = (url_text if inline_editing
                          and edit_cell_x == url_x
                          else end_truncate(url_text, url_w))
            # Apply scroll-offset truncation on the active edit cell so
            # the cursor stays visible when the buffer outgrows the
            # column width.
            if inline_editing and edit_cell_x == name_x:
                name_render, edit_cur_off = _scroll_for_cursor(
                    name_text, menu.edit_cursor, name_w)
            elif inline_editing and edit_cell_x == url_x:
                url_render, edit_cur_off = _scroll_for_cursor(
                    url_text, menu.edit_cursor, url_w)
            else:
                edit_cur_off = 0
            safe_addstr(stdscr, line, name_x,
                        name_render.ljust(name_w), name_attr)
            safe_addstr(stdscr, line, url_x,
                        url_render.ljust(url_w), url_attr)
            if inline_editing:
                _place_inline_cursor(
                    stdscr, line, edit_cell_x + edit_cur_off)
        elif inline_editing:
            # Non-remote inline edits (add_remote_name /
            # add_remote_url) replace the whole row label with the
            # editable buffer cell. A real terminal cursor lands at
            # menu.edit_cursor — see _place_inline_cursor.
            if menu.edit_field == "add_remote_name":
                prefix_label = "name: "
            elif menu.edit_field == "add_remote_url":
                prefix_label = "url: "
            else:
                prefix_label = ""
            buf_w = max(1, inner_w - len(col0) - len(prefix_label))
            visible, cur_off = _scroll_for_cursor(
                menu.edit_typed, menu.edit_cursor, buf_w)
            cell = prefix_label + visible
            full = (col0 + cell).ljust(inner_w)
            safe_addstr(stdscr, line, inner_x, full[:inner_w],
                        sb | curses.A_REVERSE)
            _place_inline_cursor(
                stdscr, line,
                inner_x + len(col0) + len(prefix_label) + cur_off)
        else:
            # Whole-row paint so reverse-video stays contiguous,
            # then overlay the caret in its own attr when the row
            # isn't focused — caret stays dim cyan against the
            # dim/normal row, and the focused row's reverse-video
            # still reads cleanly.
            full = (col0 + label).ljust(inner_w)
            safe_addstr(stdscr, line, inner_x, full[:inner_w], attr)
            if item.has_submenu and not focused:
                caret_attr = (curses.color_pair(PAIR_DLG_CYAN)
                              | curses.A_DIM)
                safe_addstr(stdscr, line, inner_x, "›", caret_attr)
        line += 1
        rendered += 1
    # Pad any remaining reserved rows so the layout below the action
    # items doesn't shift between main and submenu views.
    for _ in range(n_items - rendered):
        safe_addstr(stdscr, line, inner_x, " " * inner_w, sb)
        line += 1

    # Bottom-pane separator
    safe_addstr(stdscr, line, inner_x, "─" * inner_w, sb | curses.A_DIM)
    line += 1

    # Tab header
    _draw_tab_header(stdscr, line, inner_x, inner_w, menu, state, sb)
    line += 1

    # Compute pane size: whatever's left between current line and the
    # footer hint row, capped at PANE_TARGET_ROWS + 1 (filter row).
    footer_y = y + h - 2
    pane_total = max(2, footer_y - line - 1)
    list_rows = pane_total - 1  # filter takes the first row

    _draw_pane(stdscr, line, inner_x, inner_w, list_rows, menu, state, sb)

    _draw_action_hints(stdscr, menu, footer_y, inner_x, inner_w,
                       sb | curses.A_DIM)

    # No inline edit active → make sure the terminal cursor stays
    # hidden. _place_inline_cursor turns it back on (and positions
    # it) inside the per-item draw when an edit is in progress.
    if not menu.edit_field:
        try:
            curses.curs_set(0)
        except curses.error:
            pass


def _draw_tab_header(stdscr, line: int, inner_x: int, inner_w: int,
                     menu: ActionMenu, state: State, sb: int) -> None:
    """Render the [ Working tree ] [ Recent commits ] tabs. Active tab
    gets cyan + bold; inactive is dim. When `pane_focus` is False the
    whole header drops a tone so the user can tell the action items
    have focus, not the pane.

    While the initial query for a tab is still in flight, that tab's
    count column shows a spinner instead of a number — keeps the
    label stable but tells the user the figure isn't final yet."""
    tree_count = (_spinner_glyph(state) if menu.tree_loading
                  else str(len(menu.tree_files)))
    commits_count = (_spinner_glyph(state)
                     if (menu.commits_loading and not menu.commits_full)
                     else str(len(menu.commits_full)))
    tabs = [("tree", "Working tree", tree_count),
            ("commits", "Recent commits", commits_count)]
    cur_x = inner_x
    for tid, label, count in tabs:
        active = (menu.pane_tab == tid)
        text = f" {label} ({count}) "
        if active and menu.pane_focus:
            attr = curses.color_pair(PAIR_DLG_CYAN) | curses.A_BOLD
        elif active:
            attr = sb | curses.A_BOLD
        else:
            attr = sb | curses.A_DIM
        safe_addstr(stdscr, line, cur_x, text, attr)
        cur_x += len(text) + 1
        if cur_x >= inner_x + inner_w:
            break
    # Loading hint for commits paging — only when we're paging on top
    # of an already-populated list. The empty-list case shows its own
    # "loading…" centered in the pane via _draw_commits_pane.
    if menu.commits_loading and menu.commits_full:
        msg = f"  {_spinner_glyph(state)} loading more"
        safe_addstr(stdscr, line, inner_x + inner_w - len(msg),
                    msg, sb | curses.A_DIM)


def _draw_pane(stdscr, line: int, inner_x: int, inner_w: int,
               list_rows: int, menu: ActionMenu, state: State,
               sb: int) -> None:
    """Render the filter row + filtered list for the active tab."""
    if menu.pane_tab == "tree":
        _draw_tree_pane(stdscr, line, inner_x, inner_w, list_rows,
                        menu, state, sb)
    else:
        _draw_commits_pane(stdscr, line, inner_x, inner_w, list_rows,
                           menu, state, sb)


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
                    list_rows: int, menu: ActionMenu, state: State,
                    sb: int) -> None:
    filter_focused = menu.pane_focus and menu.tree_selected == 0
    _draw_filter_row(stdscr, line, inner_x, inner_w,
                     menu.tree_filter, filter_focused, sb)
    line += 1

    # Initial-load placeholder. tree_loading stays True until
    # query_working_tree finishes; until then we show a spinner +
    # "loading working tree…" centred on the first list row so the
    # user sees Tab landed and the data is on its way.
    if menu.tree_loading and not menu.tree_files:
        safe_addstr(stdscr, line, inner_x + 2,
                    f"{_spinner_glyph(state)} loading working tree…",
                    sb | curses.A_DIM)
        return

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
                    curses.color_pair(PAIR_DLG_PASTEL_GREEN))
        safe_addstr(stdscr, y, stat_x + len(stat_ins) + 1, stat_del,
                    curses.color_pair(PAIR_DLG_PASTEL_RED))


def _draw_commits_pane(stdscr, line: int, inner_x: int, inner_w: int,
                       list_rows: int, menu: ActionMenu, state: State,
                       sb: int) -> None:
    filter_focused = menu.pane_focus and menu.commits_selected == 0
    _draw_filter_row(stdscr, line, inner_x, inner_w,
                     menu.commits_filter, filter_focused, sb)
    line += 1

    # Initial-load placeholder for commits. commits_loading is reused
    # across initial-load and pagination — the empty-list case here
    # implies the first page is still in flight (paginated loading
    # always has something already in commits_full).
    if menu.commits_loading and not menu.commits_full:
        safe_addstr(stdscr, line, inner_x + 2,
                    f"{_spinner_glyph(state)} loading commits…",
                    sb | curses.A_DIM)
        return

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
                curses.color_pair(PAIR_DLG_PASTEL_YELLOW))
    if rel:
        rel_x = x + len(head) + pad + 1
        safe_addstr(stdscr, y, rel_x, rel,
                    curses.color_pair(PAIR_DLG_PASTEL_BLUE))


# ---------- Handle --------------------------------------------------------


def handle_action_menu_key(state: State, key: int) -> None:
    menu = state.action_menu
    if menu is None:
        return

    # Diff viewer is a sub-modal of the action menu — route all keys to
    # it while it's open; Tab and Esc both close it.
    if state.diff_viewer is not None:
        handle_diff_viewer_key(state, key)
        return

    # Confirm prompt and inline edit modes intercept everything.
    # Both modes have their own Esc semantics (cancel mode, not
    # close modal), so they must run before the global Esc handler.
    if menu.confirm_message:
        _handle_confirm_key(state, menu, key)
        return
    if menu.edit_field:
        _handle_inline_edit_key(state, menu, key)
        return

    if key == 27:
        menu.cancel_event.set()
        state.action_menu = None
        return

    if key == curses.KEY_HOME:
        # Home is "go to the top": collapse all submenu frames and
        # snap to the first selectable main-menu entry.
        menu.pane_focus = False
        menu.submenu_stack.clear()
        menu.selected = _first_actionable_index(menu.items)
        return

    if menu.pane_focus:
        _handle_pane_key(state, menu, key)
        return

    # ---- Action-items navigation ----
    # Tab closes the modal entirely from the action list (the bottom
    # pane has its own Tab semantics — view diff).
    if key == 9:
        menu.cancel_event.set()
        state.action_menu = None
        return

    items = _current_items(menu)
    selected = _current_selected(menu)
    in_submenu = _in_submenu(menu)

    # Left arrow pops one submenu level. No-op on the main menu.
    if key == curses.KEY_LEFT and in_submenu:
        _exit_to_parent(menu)
        return

    # Right arrow on a has_submenu opener pushes the matching frame.
    if (key == curses.KEY_RIGHT and items
            and 0 <= selected < len(items)
            and items[selected].has_submenu
            and items[selected].enabled):
        _enter_submenu_for(menu, items[selected])
        return

    # R / D on a remote row activate rename / delete respectively.
    # Enter is reserved for the most common change (set URL), so
    # rename gets its own letter shortcut. D's confirm gate is
    # there because losing a remote ref is mildly destructive.
    if (key in (ord("r"), ord("R")) and items
            and 0 <= selected < len(items)
            and items[selected].id.startswith("remote:")):
        item = items[selected]
        name = item.id.split(":", 1)[1]
        url = ""
        for n, u in menu.remotes_list:
            if n == name:
                url = u
                break
        _begin_rename_remote(menu, name, url)
        return
    if (key in (ord("d"), ord("D")) and items
            and 0 <= selected < len(items)
            and items[selected].id.startswith("remote:")):
        item = items[selected]
        name = item.id.split(":", 1)[1]
        url = ""
        for n, u in menu.remotes_list:
            if n == name:
                url = u
                break
        _request_confirm(
            menu,
            f"Remove remote {name}? [y/N]",
            "remove_remote",
            {"name": name, "url": url})
        return

    if key == curses.KEY_UP and items:
        _set_current_selected(menu,
                              _step_selection(items, selected, -1))
        return
    if key == curses.KEY_DOWN and items:
        next_idx = _step_selection(items, selected, +1, no_wrap=True)
        if next_idx == selected and not in_submenu:
            # Down past the last selectable item on the main menu
            # drops focus into the bottom pane. Submenus don't have
            # this fall-through — the pane belongs to the top level.
            menu.pane_focus = True
            return
        _set_current_selected(menu, next_idx)
        return
    if key in (10, 13, curses.KEY_ENTER) and items:
        item = items[selected]
        if not item.enabled or item.is_separator:
            return
        if item.is_back:
            _exit_to_parent(menu)
            return
        if item.has_submenu:
            _enter_submenu_for(menu, item)
            return
        # Leaf action — dispatch.
        _dispatch_action(state, menu, item)


def _step_selection(items: List[ActionMenuItem], current: int,
                    direction: int, *, no_wrap: bool = False) -> int:
    """Move the cursor by `direction` (±1) over the items list,
    skipping non-selectable rows (separators, disabled, etc.). When
    `no_wrap` is True, returns `current` if the move would wrap —
    used by KEY_DOWN to fall off the end into the bottom pane on the
    main menu."""
    n = len(items)
    if n == 0:
        return current
    idx = current
    for _ in range(n):
        idx = idx + direction
        if no_wrap and (idx < 0 or idx >= n):
            return current
        idx %= n
        item = items[idx]
        if item.is_separator:
            continue
        return idx
    return current


def _exit_to_parent(menu: ActionMenu) -> None:
    """Pop one submenu frame and snap the parent's cursor onto the
    opener that led here, so users return where they came from."""
    if not menu.submenu_stack:
        return
    leaving = menu.submenu_stack[-1]
    _pop_submenu(menu)
    parent_items = _current_items(menu)
    target_id = ""
    if leaving.name in ("branch", "actions", "stashes", "remotes"):
        target_id = f"{leaving.name}_submenu"
    elif leaving.name.startswith("stash:"):
        target_id = f"stash:{leaving.name.split(':', 1)[1]}"
    if target_id:
        for i, it in enumerate(parent_items):
            if it.id == target_id:
                _set_current_selected(menu, i)
                return


def _enter_submenu_for(menu: ActionMenu, item: ActionMenuItem) -> None:
    """Push the submenu frame that corresponds to a has_submenu
    opener. Builds items dynamically — branch/actions read from the
    cached meta dict, stashes from the cached stash list, per-stash
    from the chosen ref."""
    meta = menu.cached_meta or {}
    if item.id == "branch_submenu":
        _push_submenu(menu, "branch", "branch", _build_branch_items(meta))
        return
    if item.id == "actions_submenu":
        _push_submenu(menu, "actions", "actions",
                      _build_actions_items(meta))
        return
    if item.id == "stashes_submenu":
        # Refresh the cached stash list every time the submenu opens
        # so a freshly-created stash shows up without needing the
        # whole modal to reopen.
        from core.git_ops import list_stashes
        menu.stashes = list_stashes(menu.target_path)
        menu.stash_count = len(menu.stashes)
        menu.items = _build_main_items(
            meta, stash_count=menu.stash_count,
            remote_count=menu.remote_count)
        _push_submenu(menu, "stashes", "stashes",
                      _build_stashes_items(menu.stashes))
        return
    if item.id == "remotes_submenu":
        # Refresh remotes on entry the same way stashes does — picks
        # up renames / additions made by an external git command.
        from core.git_ops import list_remotes
        menu.remotes_list = list_remotes(menu.target_path)
        menu.remote_count = len(menu.remotes_list)
        menu.items = _build_main_items(
            meta, stash_count=menu.stash_count,
            remote_count=menu.remote_count)
        _push_submenu(menu, "remotes", "remotes",
                      _build_remotes_items(menu.remotes_list))
        return
    if item.id.startswith("stash:"):
        ref = item.id.split(":", 1)[1]
        # Use the ref as the breadcrumb segment label so the user
        # sees `repo › stashes › stash@{0}`.
        _push_submenu(menu, f"stash:{ref}", ref,
                      _build_stash_apply_items(ref))
        return


def _begin_rename_remote(menu: ActionMenu, name: str, url: str) -> None:
    """Activate inline rename mode on a remote row. The current name
    is pre-filled in the buffer with the cursor at the end, so the
    user starts editing the existing value rather than retyping from
    scratch."""
    menu.edit_field = "rename_remote"
    menu.edit_typed = name
    menu.edit_cursor = len(name)
    menu.edit_pre_value = name
    menu.edit_target_id = f"remote:{name}"
    menu.edit_extra = {"url": url}


def _begin_set_url_remote(menu: ActionMenu, name: str,
                          url: str) -> None:
    """Activate inline URL-edit mode on a remote row. Pre-fills the
    buffer with the current URL — Enter on the remote row starts
    here (the more common edit), R re-routes to the rename path."""
    menu.edit_field = "set_url_remote"
    menu.edit_typed = url
    menu.edit_cursor = len(url)
    menu.edit_pre_value = url
    menu.edit_target_id = f"remote:{name}"
    menu.edit_extra = {"name": name, "old_url": url}


def _begin_new_remote_name(menu: ActionMenu) -> None:
    """Step 1 of the new-remote inline flow: capture the name."""
    menu.edit_field = "add_remote_name"
    menu.edit_typed = ""
    menu.edit_cursor = 0
    menu.edit_pre_value = ""
    menu.edit_target_id = "new_remote"
    menu.edit_extra = {}


def _begin_new_remote_url(menu: ActionMenu, name: str) -> None:
    """Step 2 of the new-remote flow: now that we have a name,
    capture the URL. Empty URL is invalid and refuses to advance."""
    menu.edit_field = "add_remote_url"
    menu.edit_typed = ""
    menu.edit_cursor = 0
    menu.edit_pre_value = ""
    menu.edit_target_id = "new_remote"
    menu.edit_extra = {"name": name}


def _cancel_inline_edit(menu: ActionMenu) -> None:
    menu.edit_field = ""
    menu.edit_typed = ""
    menu.edit_cursor = 0
    menu.edit_pre_value = ""
    menu.edit_target_id = ""
    menu.edit_extra = {}


def _request_confirm(menu: ActionMenu, message: str, action: str,
                     args: "dict[str, str]") -> None:
    """Show a y/N confirm strip and stash the action to run on Y."""
    menu.confirm_message = message
    menu.confirm_action = action
    menu.confirm_args = dict(args)


def _clear_confirm(menu: ActionMenu) -> None:
    menu.confirm_message = ""
    menu.confirm_action = ""
    menu.confirm_args = {}


def _apply_remote_op(state: State, menu: ActionMenu) -> None:
    """Build a single-op RemoteRow that matches the pending confirm
    action and hand it to `kick_off_remote_changes`. The same
    pipeline that powers the standalone remotes modal handles the
    operation order and task-label rendering."""
    from core.models import RemoteRow
    from core.workers import kick_off_remote_changes
    args = menu.confirm_args
    row: Optional[RemoteRow] = None
    if menu.confirm_action == "rename_remote":
        old = args.get("old", "")
        new = args.get("new", "")
        url = args.get("url", "")
        row = RemoteRow(
            original_name=old, original_url=url,
            name=new, url=url, is_new=False)
    elif menu.confirm_action == "set_url_remote":
        name = args.get("name", "")
        new_url = args.get("url", "")
        old_url = args.get("old_url", "")
        row = RemoteRow(
            original_name=name, original_url=old_url,
            name=name, url=new_url, is_new=False)
    elif menu.confirm_action == "remove_remote":
        old = args.get("name", "")
        url = args.get("url", "")
        row = RemoteRow(
            original_name=old, original_url=url,
            name=old, url=url, to_delete=True, is_new=False)
    elif menu.confirm_action == "add_remote":
        new_name = args.get("name", "")
        new_url = args.get("url", "")
        row = RemoteRow(
            original_name="", original_url="",
            name=new_name, url=new_url,
            to_delete=False, is_new=True)
    if row is None:
        return
    kick_off_remote_changes(
        state, [row],
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
    )
    # Update the cached remotes optimistically so the visible list
    # reflects the change — the worker's refresh will reconcile any
    # mismatch on the next loader tick.
    if menu.confirm_action == "rename_remote":
        old = args.get("old", "")
        new = args.get("new", "")
        url = args.get("url", "")
        menu.remotes_list = [(new if name == old else name, u)
                             for name, u in menu.remotes_list]
        if url and not any(u for n, u in menu.remotes_list if n == new):
            pass  # keep cached url as-is on mismatch
    elif menu.confirm_action == "set_url_remote":
        name = args.get("name", "")
        new_url = args.get("url", "")
        menu.remotes_list = [
            (n, new_url if n == name else u)
            for n, u in menu.remotes_list
        ]
    elif menu.confirm_action == "remove_remote":
        gone = args.get("name", "")
        menu.remotes_list = [(n, u) for n, u in menu.remotes_list
                             if n != gone]
        menu.remote_count = len(menu.remotes_list)
    elif menu.confirm_action == "add_remote":
        new_name = args.get("name", "")
        new_url = args.get("url", "")
        menu.remotes_list = list(menu.remotes_list) + [
            (new_name, new_url)]
        menu.remote_count = len(menu.remotes_list)
    # Re-render the live frame and main-menu opener label.
    if menu.submenu_stack and menu.submenu_stack[-1].name == "remotes":
        top = menu.submenu_stack[-1]
        top.items = _build_remotes_items(menu.remotes_list)
        if top.selected >= len(top.items):
            top.selected = _first_actionable_index(top.items)
    menu.items = _build_main_items(
        menu.cached_meta or {},
        stash_count=menu.stash_count,
        remote_count=menu.remote_count)


def _handle_inline_edit_key(state: State, menu: ActionMenu,
                            key: int) -> None:
    """Keystrokes while an inline editable field has focus.

    All four fields (rename / set-url / add-name / add-url) accept
    any printable ASCII so a pasted URL like
    `git@github.com:user/repo.git` lands intact regardless of which
    step the user happens to be on. ←/→/Home/End move the cursor;
    Backspace and Delete remove the char before/at the cursor;
    typing inserts at the cursor; Enter advances or fires the
    confirm prompt; Esc cancels the whole flow."""
    field = menu.edit_field
    text = menu.edit_typed
    cur = max(0, min(menu.edit_cursor, len(text)))
    if key == 27:  # Esc — cancel out of inline edit
        _cancel_inline_edit(menu)
        return
    if key in (10, 13, curses.KEY_ENTER):
        committed = text.strip()
        if not committed:
            return  # empty value — refuse to advance
        if field == "rename_remote":
            old = menu.edit_pre_value
            url = menu.edit_extra.get("url", "")
            if committed == old:
                _cancel_inline_edit(menu)
                return
            if committed.startswith("-"):
                return
            _cancel_inline_edit(menu)
            _request_confirm(
                menu,
                f"Rename remote {old} → {committed}? [y/N]",
                "rename_remote",
                {"old": old, "new": committed, "url": url})
            return
        if field == "set_url_remote":
            name = menu.edit_extra.get("name", "")
            old_url = menu.edit_extra.get("old_url", "")
            if committed == old_url:
                _cancel_inline_edit(menu)
                return
            if committed.startswith("-"):
                return
            _cancel_inline_edit(menu)
            _request_confirm(
                menu,
                f"Set {name} URL → {committed}? [y/N]",
                "set_url_remote",
                {"name": name, "url": committed, "old_url": old_url})
            return
        if field == "add_remote_name":
            if committed.startswith("-"):
                return
            # Refuse a name that already exists locally.
            if any(n == committed for n, _ in menu.remotes_list):
                return
            _begin_new_remote_url(menu, committed)
            return
        if field == "add_remote_url":
            if committed.startswith("-"):
                return
            name = menu.edit_extra.get("name", "")
            _cancel_inline_edit(menu)
            _request_confirm(
                menu,
                f"Add remote {name} → {committed}? [y/N]",
                "add_remote",
                {"name": name, "url": committed})
            return
        return
    if key == curses.KEY_LEFT:
        menu.edit_cursor = max(0, cur - 1)
        return
    if key == curses.KEY_RIGHT:
        menu.edit_cursor = min(len(text), cur + 1)
        return
    if key in (curses.KEY_HOME, 1):
        menu.edit_cursor = 0
        return
    if key in (curses.KEY_END, 5):
        menu.edit_cursor = len(text)
        return
    if key in (curses.KEY_BACKSPACE, 127, 8):
        if cur > 0:
            menu.edit_typed = text[: cur - 1] + text[cur:]
            menu.edit_cursor = cur - 1
        return
    if key == curses.KEY_DC:
        if cur < len(text):
            menu.edit_typed = text[:cur] + text[cur + 1:]
        return
    if 32 <= key < 127:
        # All fields accept any printable ASCII so paste of a full
        # remote URL (with `:` / `@` / `/`) survives intact even when
        # the user lands on the name step. Submit-time validators
        # (the `-` prefix check above + git's own remote add) catch
        # genuinely invalid values.
        menu.edit_typed = text[:cur] + chr(key) + text[cur:]
        menu.edit_cursor = cur + 1


def _handle_confirm_key(state: State, menu: ActionMenu,
                        key: int) -> None:
    """y/N strip dismisses on Esc, applies on y, cancels on n."""
    if key in (ord("y"), ord("Y")):
        _apply_remote_op(state, menu)
        _clear_confirm(menu)
        return
    if key in (ord("n"), ord("N"), 27):
        _clear_confirm(menu)
        return


def _dispatch_action(state: State, menu: ActionMenu,
                     item: ActionMenuItem) -> None:
    """Run a leaf action item. Each branch maps the item id to the
    right open-modal call or kick_off_action invocation. Generic
    branch_arg ids (`stash_apply:<ref>`) get split before dispatch."""
    if item.id == "switch_branch":
        from .branch_picker import open_branch_picker
        open_branch_picker(state)
        return
    if item.id == "checkout_remote_branch":
        from .remote_branch_picker import open_remote_branch_picker
        open_remote_branch_picker(state)
        return
    if item.id == "merge_branch":
        from .branch_picker import open_branch_picker
        open_branch_picker(state, mode="merge")
        return
    if item.id == "safe_merge":
        # If a merge is already in progress, adopt it (resolve the existing
        # conflicts); otherwise let the user pick a branch to safe-merge.
        from core.git_ops import merge_head_sha
        if merge_head_sha(menu.target_path) is not None:
            from core.workers import kick_off_safe_merge
            kick_off_safe_merge(
                state,
                target_label=menu.target_label,
                target_path=menu.target_path,
                target_repo=menu.target_repo,
                target_parent=menu.target_parent,
                merge_ref="")
            menu.cancel_event.set()
            state.action_menu = None
        else:
            from .branch_picker import open_branch_picker
            open_branch_picker(state, mode="safe_merge")
        return
    if item.id == "set_upstream":
        from .branch_picker import open_branch_picker
        open_branch_picker(state, mode="set_upstream")
        return
    if item.id == "branch_from_head":
        from .branch_name_prompt import open_branch_name_prompt
        open_branch_name_prompt(state)
        return
    if item.id == "rename_branch":
        from .branch_name_prompt import open_branch_name_prompt
        open_branch_name_prompt(state, mode="rename")
        return
    if item.id == "soft_reset":
        from .reset_prompt import open_reset_prompt
        open_reset_prompt(state)
        return
    if item.id == "run_workflow":
        from .workflow_picker import open_workflow_picker
        open_workflow_picker(state)
        return
    if item.id == "new_remote":
        _begin_new_remote_name(menu)
        return
    if item.id.startswith("remote:"):
        # Enter on a remote row activates inline URL edit (the more
        # common change). The in-place buffer is pre-filled with the
        # current URL — cursor at the end so the user can backspace
        # to clear or arrow to a position. Rename is on R; delete
        # is on D.
        name = item.id.split(":", 1)[1]
        url = ""
        for n, u in menu.remotes_list:
            if n == name:
                url = u
                break
        _begin_set_url_remote(menu, name, url)
        return
    if item.id == "stash_create":
        kick_off_action(
            state, "stash_create",
            target_label=menu.target_label,
            target_path=menu.target_path,
            target_repo=menu.target_repo,
            target_parent=menu.target_parent,
        )
        menu.cancel_event.set()
        state.action_menu = None
        return
    if item.id.startswith("stash_apply:"):
        ref = item.id.split(":", 1)[1]
        kick_off_action(
            state, "stash_apply",
            target_label=menu.target_label,
            target_path=menu.target_path,
            target_repo=menu.target_repo,
            target_parent=menu.target_parent,
            branch_arg=ref,
        )
        menu.cancel_event.set()
        state.action_menu = None
        return
    # Generic leaf action — fetch / pull / push / etc.
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
        _handle_tree_key(state, menu, key)
    else:
        _handle_commits_key(state, menu, key)
    _maybe_prefetch(menu)


def _handle_tree_key(state: State, menu: ActionMenu, key: int) -> None:
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
    if key == 9 and not on_filter:
        # Tab on a file row opens the diff viewer.
        idx = menu.tree_selected - 1
        if 0 <= idx < len(files):
            fe = files[idx]
            open_diff_viewer(
                state,
                target_path=menu.target_path,
                label=menu.target_label,
                file_path=fe.path,
                untracked=fe.untracked,
            )
        return
    if on_filter and _is_typing_key(key):
        if key in (curses.KEY_BACKSPACE, 127, 8):
            menu.tree_filter = menu.tree_filter[:-1]
        else:
            menu.tree_filter += chr(key)
        # Filter changed → clamp selection (still on filter row though).
        return


def _handle_commits_key(state: State, menu: ActionMenu,
                        key: int) -> None:
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
    if key == 9 and not on_filter:
        # Tab on a focused commit row pops the commit-view modal.
        # `commits_selected` is 1-indexed (0 = filter row); subtract
        # 1 to land on the commit list. Sub-modal of the action
        # menu — main loop dispatches keys to it before this menu.
        idx = menu.commits_selected - 1
        if 0 <= idx < len(commits):
            from .commit_view import open_commit_view_modal
            commit = commits[idx]
            open_commit_view_modal(
                state,
                target_path=menu.target_path,
                target_label=menu.target_label,
                sha=commit.sha,
                subject=commit.subject)
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
