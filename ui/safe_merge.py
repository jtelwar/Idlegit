"""Full-screen safe-merge conflict resolver.

A long, scrollable list the user steps through with ↑/↓ — each decision is a
simple left/right pick between two versions, labelled by repo/branch/commit
rather than bare ours/theirs. The two versions render in distinct colours and
the selector under them recolours to the chosen side. Enter is blocked until
every conflict has a choice; then a merge commit is created and a final
confirm screen offers push + an off-by-default "remove backup stash" box.

Pure rendering + in-memory navigation here — every git side effect runs in
`core.workers` (stash / merge / commit / push / sync), tracked in the task
panel. The sub-loop that owns the keyboard lives in `ui/main_loop.py`."""
from __future__ import annotations

import curses
from typing import List, Tuple

from core.config import APP_DISPLAY_NAME
from core.state.app import State
from core.state.safe_merge import ConflictFile, MergeSide, SafeMergeScreen

from .colors import (
    PAIR_BRANCH, PAIR_ERR, PAIR_HEADER, PAIR_OK,
    PAIR_PASTEL_BLUE, PAIR_PASTEL_GREEN, PAIR_PASTEL_YELLOW, PAIR_WARN,
)
from .geometry import safe_addstr
from .hints import (
    KEY_ENTER, KEY_ESC, KEY_LEFT_RIGHT, KEY_SPACE, KEY_UP_DOWN, Hint,
    render_hints,
)
from .sidebar import SPINNER_FRAMES

# Lines of shared context shown above/below each conflict hunk.
_CONTEXT_LINES = 2

# A row is either a plain "(text, attr)" or "([(seg, attr), …], attr)" for
# multi-colour lines — same shape the review pane's `_draw_left_pane` uses.
_Row = Tuple[object, int]


def _ours_attr(focused: bool) -> int:
    return curses.color_pair(PAIR_PASTEL_GREEN) | (
        curses.A_BOLD if focused else 0)


def _theirs_attr(focused: bool) -> int:
    return curses.color_pair(PAIR_PASTEL_BLUE) | (
        curses.A_BOLD if focused else 0)


def _side_caption(side: MergeSide) -> str:
    """`<remote> · <branch> @ <sha> (role)` — origin repo/branch/commit, with
    the bare ours/theirs role tucked in brackets at the end as requested."""
    pieces: List[str] = []
    if side.remote:
        pieces.append(side.remote)
    bc = side.branch or "?"
    if side.short_sha:
        bc = f"{bc} @ {side.short_sha}"
    pieces.append(bc)
    return f"{' · '.join(pieces)}  ({side.role})"


def _decided(cf: ConflictFile, hunk_index: int) -> bool:
    if hunk_index < 0:
        return bool(cf.whole_choice)
    return bool(cf.hunks[hunk_index].choice)


def _choice_of(cf: ConflictFile, hunk_index: int) -> str:
    if hunk_index < 0:
        return cf.whole_choice
    return cf.hunks[hunk_index].choice


# ---------- navigation + mutation (called from the sub-loop) ---------------


def all_decided(screen: SafeMergeScreen) -> bool:
    """True when every decision point has a chosen side. Manual files don't
    count as decisions, so they don't block this — the commit step gates on
    a clean index instead."""
    for fi, hi in screen.decisions:
        if not _decided(screen.files[fi], hi):
            return False
    return True


def decided_count(screen: SafeMergeScreen) -> int:
    return sum(1 for fi, hi in screen.decisions
               if _decided(screen.files[fi], hi))


def has_manual(screen: SafeMergeScreen) -> bool:
    return any(cf.kind == "manual" for cf in screen.files)


def focus_move(screen: SafeMergeScreen, delta: int) -> None:
    if not screen.decisions:
        return
    screen.focus = max(0, min(len(screen.decisions) - 1,
                              screen.focus + delta))


def focus_next_undecided(screen: SafeMergeScreen) -> None:
    """Jump focus to the first still-undecided decision, if any."""
    for i, (fi, hi) in enumerate(screen.decisions):
        if not _decided(screen.files[fi], hi):
            screen.focus = i
            return


def set_choice(screen: SafeMergeScreen, side: str) -> None:
    """Set the focused decision to `side` ("ours" | "theirs" | "both")."""
    if not screen.decisions:
        return
    fi, hi = screen.decisions[screen.focus]
    cf = screen.files[fi]
    if hi < 0:
        if side in ("ours", "theirs"):
            cf.whole_choice = side
    else:
        cf.hunks[hi].choice = side


# ---------- row building ---------------------------------------------------


def _hunk_context(cf: ConflictFile, part_index: int) -> Tuple[List[str],
                                                              List[str]]:
    """Last `_CONTEXT_LINES` lines of the preceding ctx part and first
    `_CONTEXT_LINES` of the following ctx part (the lines surrounding a
    hunk)."""
    pre: List[str] = []
    post: List[str] = []
    if part_index > 0:
        kind, val = cf.parts[part_index - 1]
        if kind == "ctx":
            pre = list(val)[-_CONTEXT_LINES:]  # type: ignore[arg-type]
    if part_index + 1 < len(cf.parts):
        kind, val = cf.parts[part_index + 1]
        if kind == "ctx":
            post = list(val)[:_CONTEXT_LINES]  # type: ignore[arg-type]
    return pre, post


def _selector_segments(screen: SafeMergeScreen, cf: ConflictFile,
                       hunk_index: int, focused: bool) -> List[Tuple[str,
                                                                     int]]:
    """The left/right selector: `[ ◀ ours ]   [ theirs ▶ ]` plus an optional
    `both`. The chosen side is bold + reverse; the others dim."""
    choice = _choice_of(cf, hunk_index)
    ours_on = choice == "ours"
    theirs_on = choice == "theirs"
    both_on = choice == "both"
    arrow = "▶ " if focused else "  "
    segs: List[Tuple[str, int]] = [(f"    {arrow}", curses.A_DIM)]

    ours_attr = curses.color_pair(PAIR_PASTEL_GREEN)
    if ours_on:
        ours_attr |= curses.A_BOLD | curses.A_REVERSE
    elif not choice:
        ours_attr |= curses.A_DIM
    segs.append(("◀ use ours ", ours_attr))

    segs.append(("  ", curses.A_DIM))

    theirs_attr = curses.color_pair(PAIR_PASTEL_BLUE)
    if theirs_on:
        theirs_attr |= curses.A_BOLD | curses.A_REVERSE
    elif not choice:
        theirs_attr |= curses.A_DIM
    segs.append((" use theirs ▶", theirs_attr))

    if hunk_index >= 0:
        # "both" only makes sense for text hunks (concatenate the sides).
        both_attr = curses.color_pair(PAIR_PASTEL_YELLOW)
        both_attr |= (curses.A_BOLD | curses.A_REVERSE if both_on
                      else curses.A_DIM)
        segs.append(("    b: both", both_attr))

    if not choice:
        segs.append(("   ← needs a choice",
                     curses.color_pair(PAIR_WARN) | curses.A_DIM))
    return segs


def _build_rows(screen: SafeMergeScreen,
                inner_w: int) -> Tuple[List[_Row], int]:
    """Build every body row for the resolve phase. Returns `(rows,
    focused_selector_row)` where the second value is the row index of the
    focused decision's selector (for scroll-to-view), or -1."""
    rows: List[_Row] = []
    focused_row = -1
    focused_decision = (screen.decisions[screen.focus]
                        if screen.decisions else None)

    def ctx_rows(lines: List[str]) -> None:
        for ln in lines:
            rows.append((f"      {ln.rstrip(chr(10)).rstrip(chr(13))}",
                         curses.A_DIM))

    for fi, cf in enumerate(screen.files):
        # File header.
        if cf.kind == "manual":
            glyph, hattr = "⚠", curses.color_pair(PAIR_WARN)
            note = f"  — {cf.note}; resolve manually (idlegit won't delete)"
        elif cf.kind == "binary":
            glyph, hattr = "◆", curses.color_pair(PAIR_BRANCH)
            note = f"  — {cf.note}; whole-file pick"
        else:
            glyph, hattr = "▾", curses.color_pair(PAIR_BRANCH)
            done = sum(1 for h in cf.hunks if h.choice)
            note = f"  ({done}/{len(cf.hunks)} resolved)"
        rows.append((f"{glyph} {cf.path}", hattr | curses.A_BOLD))
        rows.append((note, curses.A_DIM))

        if cf.kind == "manual":
            rows.append(("", 0))
            continue

        if cf.kind == "binary":
            fdec = focused_decision == (fi, -1)
            if fdec:
                focused_row = len(rows)
            rows.append((_selector_segments(screen, cf, -1, fdec), 0))
            rows.append(("", 0))
            continue

        # Text file: walk parts, rendering each hunk with surrounding ctx.
        for pi, (kind, val) in enumerate(cf.parts):
            if kind != "hunk":
                continue
            hi = val  # type: ignore[assignment]
            hunk = cf.hunks[hi]
            pre, post = _hunk_context(cf, pi)
            ctx_rows(pre)
            fdec = focused_decision == (fi, hi)

            rows.append((f"    ┌─ {_side_caption(screen.ours)}",
                         _ours_attr(fdec)))
            for ln in hunk.ours:
                rows.append((f"    │  {ln.rstrip(chr(10)).rstrip(chr(13))}",
                             curses.color_pair(PAIR_PASTEL_GREEN)))
            if not hunk.ours:
                rows.append(("    │  (empty)", curses.A_DIM))
            rows.append((f"    ├─ {_side_caption(screen.theirs)}",
                         _theirs_attr(fdec)))
            for ln in hunk.theirs:
                rows.append((f"    │  {ln.rstrip(chr(10)).rstrip(chr(13))}",
                             curses.color_pair(PAIR_PASTEL_BLUE)))
            if not hunk.theirs:
                rows.append(("    │  (empty)", curses.A_DIM))

            if fdec:
                focused_row = len(rows)
            rows.append((_selector_segments(screen, cf, hi, fdec), 0))
            ctx_rows(post)
            rows.append(("", 0))

    return rows, focused_row


def _draw_rows(stdscr, x: int, y: int, w: int, h: int,
               rows: List[_Row], scroll: int) -> None:
    for i in range(h):
        idx = scroll + i
        if idx >= len(rows):
            break
        text, attr = rows[idx]
        if isinstance(text, list):
            cx = x
            for seg_text, seg_attr in text:
                avail = max(0, w - (cx - x))
                if avail <= 0:
                    break
                safe_addstr(stdscr, y + i, cx, seg_text[:avail], seg_attr)
                cx += len(seg_text)
        else:
            safe_addstr(stdscr, y + i, x, text[:w], attr)


# ---------- top-level draw -------------------------------------------------


def _draw_titlebar(stdscr) -> None:
    safe_addstr(stdscr, 0, 0, APP_DISPLAY_NAME,
                curses.A_BOLD | curses.color_pair(PAIR_HEADER))
    safe_addstr(stdscr, 0, len(APP_DISPLAY_NAME), " · safe-merge",
                curses.A_DIM)


def _spinner(state: State) -> str:
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


def _draw_busy(stdscr, state: State, message: str) -> None:
    h, w = stdscr.getmaxyx()
    safe_addstr(stdscr, h // 2, max(0, (w - len(message) - 2) // 2),
                f"{_spinner(state)} {message}",
                curses.color_pair(PAIR_BRANCH))


def _draw_confirm(stdscr, screen: SafeMergeScreen) -> None:
    h, w = stdscr.getmaxyx()
    y = 4
    safe_addstr(stdscr, 2, 0, "Merge commit created", curses.A_BOLD
                | curses.color_pair(PAIR_OK))
    safe_addstr(stdscr, y, 2,
                f"{screen.commit_sha}  {screen.commit_subject}"[:w - 4],
                curses.color_pair(PAIR_PASTEL_YELLOW))
    y += 2

    rows: List[Tuple[str, str]] = []
    rows.append(("push", "push this merge commit"
                 + (f" → {screen.target_label}" if screen.target_label
                    else "")))
    rows.append(("stash", "remove backup stash"
                 + (f" ({screen.backup_stash_name})"
                    if screen.backup_stash_name else " (none created)")))
    rows.append(("finish", "Finish"))

    for i, (kind, label) in enumerate(rows):
        focused = screen.confirm_focus == i
        prefix = "▶ " if focused else "  "
        if kind == "push":
            box = "[x]" if screen.confirm_push else "[ ]"
            attr = (curses.color_pair(PAIR_OK) if screen.confirm_push
                    else curses.A_DIM)
            text = f"{prefix}{box} {label}"
        elif kind == "stash":
            disabled = not screen.backup_stash_name
            box = "[x]" if (screen.confirm_remove_stash
                            and not disabled) else "[ ]"
            attr = (curses.color_pair(PAIR_WARN)
                    if (screen.confirm_remove_stash and not disabled)
                    else curses.A_DIM)
            text = f"{prefix}{box} {label}"
        else:
            attr = (curses.color_pair(PAIR_BRANCH) | curses.A_BOLD
                    if focused else curses.A_BOLD)
            text = f"{prefix}[ {label} ]"
        if focused:
            attr |= curses.A_REVERSE
        safe_addstr(stdscr, y + i, 2, text[:w - 4], attr)

    if screen.is_tracked_submodule:
        safe_addstr(stdscr, y + len(rows) + 1, 2,
                    "tracked submodule — sibling checkouts and parent "
                    "pointers will be synced after push.",
                    curses.A_DIM)


def draw_safe_merge(stdscr, state: State) -> None:
    """Render the safe-merge screen for the current phase."""
    screen = state.safe_merge
    if screen is None:
        return
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    _draw_titlebar(stdscr)

    if screen.phase == "preparing":
        _draw_busy(stdscr, state,
                   "Preparing merge — stashing & detecting conflicts…")
        _footer(stdscr, [Hint(KEY_ESC, "cancel")])
        stdscr.refresh()
        return
    if screen.phase == "committing":
        _draw_busy(stdscr, state, "Writing resolutions & creating merge "
                   "commit…")
        _footer(stdscr, [])
        stdscr.refresh()
        return
    if screen.phase == "confirming":
        _draw_busy(stdscr, state, "Pushing & syncing…")
        _footer(stdscr, [])
        stdscr.refresh()
        return
    if screen.phase == "error":
        safe_addstr(stdscr, 2, 0, "Safe-merge stopped", curses.A_BOLD
                    | curses.color_pair(PAIR_ERR))
        safe_addstr(stdscr, 4, 2, screen.error[:w - 4],
                    curses.color_pair(PAIR_WARN))
        _footer(stdscr, [Hint(KEY_ESC, "dismiss")])
        stdscr.refresh()
        return
    if screen.phase == "confirm":
        _draw_confirm(stdscr, screen)
        _footer(stdscr, [
            Hint(KEY_UP_DOWN, "move"),
            Hint(KEY_SPACE, "toggle"),
            Hint(KEY_ENTER, "finish"),
            Hint(KEY_ESC, "keep commit, skip push"),
        ])
        stdscr.refresh()
        return

    # ---- resolve phase ----
    total = len(screen.decisions)
    done = decided_count(screen)
    merging = (f"merging {screen.theirs.branch} into {screen.ours.branch}"
               if screen.theirs.branch else "resolve conflicts")
    safe_addstr(stdscr, 2, 0, "Safe merge",
                curses.A_BOLD | curses.color_pair(PAIR_BRANCH))
    sub = (f"{screen.target_label}  ·  {merging}  ·  "
           f"{done}/{total} conflicts resolved"
           + (f"  ·  {len([c for c in screen.files if c.kind=='manual'])} "
              "manual" if has_manual(screen) else ""))
    safe_addstr(stdscr, 2, len("Safe merge") + 3, sub[:max(0, w - 14)],
                curses.A_DIM)
    if screen.status_note:
        safe_addstr(stdscr, 3, 0, screen.status_note[:w - 1],
                    curses.color_pair(PAIR_WARN))

    body_top = 5
    body_h = max(1, h - body_top - 1)
    rows, focused_row = _build_rows(screen, w - 1)
    if focused_row >= 0:
        if focused_row < screen.scroll:
            screen.scroll = focused_row
        elif focused_row >= screen.scroll + body_h:
            screen.scroll = focused_row - body_h + 1
    max_scroll = max(0, len(rows) - body_h)
    screen.scroll = max(0, min(screen.scroll, max_scroll))
    _draw_rows(stdscr, 0, body_top, w - 1, body_h, rows, screen.scroll)

    hints = [
        Hint(KEY_UP_DOWN, "next conflict"),
        Hint(KEY_LEFT_RIGHT, "pick ours / theirs"),
    ]
    # "both" only applies to text hunks; binary picks are whole-file.
    if screen.decisions and screen.decisions[screen.focus][1] >= 0:
        hints.append(Hint("b", "both"))
    if all_decided(screen) and not has_manual(screen) and total:
        hints.append(Hint(KEY_ENTER, "commit merge"))
    elif total:
        hints.append(Hint(KEY_ENTER, f"{total - done} left to decide"))
    hints.append(Hint(KEY_ESC, "leave merge in progress"))
    _footer(stdscr, hints)
    curses.curs_set(0)
    stdscr.refresh()


def _footer(stdscr, hints: List[Hint]) -> None:
    h, w = stdscr.getmaxyx()
    render_hints(stdscr, h - 1, 0, max(0, w - 1), hints, attr=curses.A_DIM)
