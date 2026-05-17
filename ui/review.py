"""Review targets: block model, async file listing, two-panel review UI."""
from __future__ import annotations

import curses
from dataclasses import dataclass
from typing import List, Optional, Tuple

from core.models import (
    ChildRef,
    FileEntry,
    LFSCandidate,
    Repo,
    ReviewBlock,
    State,
    ThenRunSelector,
    WorkflowToggle,
)
from core.config import APP_DISPLAY_NAME
from core.git_ops import (
    find_lfs_warnings,
    gh_available,
    parse_github_slug,
    query_working_tree,
    would_run_on_push,
)
from .colors import (
    PAIR_BRANCH,
    PAIR_ERR,
    PAIR_HEADER,
    PAIR_OK,
    PAIR_PASTEL_BLUE,
    PAIR_PASTEL_BLUE_ACTIVE,
    PAIR_PASTEL_GREEN,
    PAIR_PASTEL_GREEN_ACTIVE,
    PAIR_PASTEL_RED,
    PAIR_PASTEL_RED_ACTIVE,
    PAIR_PASTEL_YELLOW,
    PAIR_PASTEL_YELLOW_ACTIVE,
    PAIR_SB_CYAN_ACTIVE,
    PAIR_SB_FG,
    PAIR_SB_FG_ACTIVE,
    PAIR_SB_FG_DISABLED,
)
from .geometry import clamp_scroll, draw_scroll_overflow, safe_addstr
from .hints import (
    KEY_CTRL_K,
    KEY_ENTER,
    KEY_ESC,
    KEY_LEFT_RIGHT,
    KEY_SHIFT_TAB,
    KEY_SPACE,
    KEY_TAB,
    KEY_UP_DOWN,
    Hint,
    render_hints,
)
from .sidebar import SPINNER_FRAMES

def _block_for_repo(state: State, repo: Repo) -> ReviewBlock:
    """Build the per-repo review block for a top-level commit target.
    Picks up its LFS warnings, workflow toggles, and then-run
    selectors — same focusables the old single-list review surfaced
    — so the two-panel layout has all of them grouped under this
    repo's header instead of mixed with other repos'."""
    threshold_mb = state.lfs_warn_bytes // (1024 * 1024)
    block = ReviewBlock(
        label=repo.display_name,
        branch=repo.branch,
        target_path=repo.path,
        target_repo=repo,
        message=repo.message.strip(),
        merging=repo.merging,
        conflict_paths=list(repo.conflict_paths),
        has_origin=bool(repo.remote_url),
        upstream=repo.upstream,
        siblings_summary=", ".join(s[0].display_name for s in repo.siblings),
        auto_stage=state.auto_stage,
        auto_push=state.auto_push,
        threshold_mb=threshold_mb,
    )
    if state.auto_push:
        if repo.upstream:
            block.push_summary = f"push: yes → {repo.upstream}"
        else:
            block.push_summary = (
                f"push: yes (sets upstream → origin/{repo.branch})")
    else:
        block.push_summary = "push: no"
    if not repo.merging:
        warnings = find_lfs_warnings(
            repo, state.auto_stage, state.lfs_warn_bytes)
        for path, size in warnings:
            block.lfs_candidates.append(LFSCandidate(
                repo=repo, path=path, size_str=size))
        if (state.auto_push and gh_available() and repo.workflows
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
                block.workflow_toggles.append(WorkflowToggle(
                    repo=repo, workflow_name=wf.name))
                if dispatchable_options:
                    block.then_run_items.append(ThenRunSelector(
                        repo=repo, after_workflow=wf.name))
            if dispatchable_options:
                block.then_run_items.append(ThenRunSelector(
                    repo=repo, after_workflow=""))
    return block


def _block_for_child(state: State,
                     parent: Repo, ref: ChildRef) -> ReviewBlock:
    """Build the per-child review block for a nested submodule
    commit target. Children don't carry their own workflow toggles —
    those live on the canonical's top-level row — so this block is a
    simpler header + message + push-summary shape."""
    label = f"↳ {ref.repo.display_name} in {parent.display_name}"
    block = ReviewBlock(
        label=label,
        branch=ref.branch,
        target_path=ref.nested_path,
        target_parent=parent,
        target_child=ref,
        message=ref.message.strip(),
        is_child=True,
        auto_stage=state.auto_stage,
        auto_push=state.auto_push,
        threshold_mb=state.lfs_warn_bytes // (1024 * 1024),
    )
    if state.auto_push:
        targets = [ref.repo.display_name + " (top-level)"]
        for other_parent, other_path in ref.repo.siblings:
            if other_path != ref.nested_path:
                targets.append(
                    f"{ref.repo.display_name} in {other_parent.display_name}")
        block.siblings_summary = ", ".join(targets)
        block.push_summary = "push: yes (from nested checkout)"
    else:
        block.push_summary = "push: no"
    return block


def build_review_blocks(state: State) -> List[ReviewBlock]:
    """Per-repo / per-child review blocks for the two-panel review
    screen. Top-level repos with a queued message come first (in
    state.repos order), then submodule children (parent-by-parent).
    Empty when nothing has a message — the caller treats that as
    "nothing to review, just bail"."""
    blocks: List[ReviewBlock] = []
    for repo in state.repos:
        if repo.message.strip():
            blocks.append(_block_for_repo(state, repo))
    for parent in state.repos:
        for ref in parent.children:
            if ref.kind == "submodule" and ref.message.strip():
                blocks.append(_block_for_child(state, parent, ref))
    return blocks


def kick_off_review_files_load(blocks: List[ReviewBlock]) -> None:
    """Spawn one daemon thread per block to populate `block.files`
    via `query_working_tree`. Non-blocking — the review screen draws
    immediately with `files_loading=True` placeholders, and each pane
    fills in as its worker completes. Each worker checks
    `block.cancel_event` before mutating so closing the review
    mid-load drops the result on the floor."""
    import threading

    def loader(block: ReviewBlock) -> None:
        try:
            if block.cancel_event.is_set():
                return
            files: List[FileEntry] = query_working_tree(block.target_path)
            if block.cancel_event.is_set():
                return
            block.files = files
            # Seed per-file checkbox state. auto_stage on the block was
            # captured from state at build time; True checks every
            # change, False only checks files already staged at the
            # index (x != " "). User can override with Space afterward.
            if block.auto_stage:
                block.staged_paths = {fe.path: True for fe in files}
            else:
                block.staged_paths = {
                    fe.path: (fe.x != " " and not fe.untracked)
                    for fe in files
                }
        finally:
            block.files_loading = False

    for block in blocks:
        threading.Thread(target=loader, args=(block,), daemon=True).start()


# Sentinel for the "add tag" then-run option. Stored in the same
# string field as workflow names; the dispatch site checks for this
# value before falling through to `kick_off_manual_dispatch` and
# runs `git tag <name> <pushed_sha>` instead. Only offered for the
# after-push then-run (per-workflow then-runs stay
# workflows-or-none — chaining a tag onto an Actions run lands too
# late to be useful).
ADD_TAG_VALUE = "__add_tag__"

# Allowed characters in a tag name — same shape as branch names.
_VALID_TAG_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_./"
)

# Allowed characters in a workflow_dispatch input value. `gh
# workflow run -F key=value` is invoked via subprocess.argv (no
# shell interpolation) so this only needs to be wide enough to
# cover plausible input values: version strings ("v1.2.3"),
# environment names ("staging"), free-form notes, etc. Cap at
# printable ASCII to keep stray control bytes out of the buffer.
_VALID_INPUT_CHARS = frozenset(chr(c) for c in range(32, 127))


def _then_run_options(repo: Repo) -> List[str]:
    """Workflow names eligible as 'then run' targets for this repo —
    dispatchable + not disabled-on-github. Returned in the same order
    as repo.workflows so left/right cycling stays stable. The "add
    tag" sentinel rides at the end of every list — both the
    after-push selector and the per-workflow chains can resolve to
    "tag the commit that triggered this run"."""
    options = [w.name for w in repo.workflows
               if w.dispatchable and not w.state.startswith("disabled")]
    options.append(ADD_TAG_VALUE)
    return options


def _then_run_label(value: str) -> str:
    """Human-readable label for a then-run selection. Sentinels get
    pretty names; empty = "(none)"; everything else is rendered
    verbatim (workflow names)."""
    if value == ADD_TAG_VALUE:
        return "add tag"
    return value or "(none)"


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


# ---------- Then-run parameter pattern -----------------------------------
#
# Each then-run target can declare a list of parameters that the user
# fills in inline beneath the selector (one row per param, all at the
# same indent — one level deeper than the selector). The pattern is
# generic so it works for the "add tag" sentinel today, and is ready to
# back workflow_dispatch inputs (each input becomes a ParamSpec the
# review pane can render and the dispatch site can read).
#
# Storage is a generic two-level dict on Repo:
#   * after-push:   then_run_params_after_push[<param>] = value
#   * after-<wf>:   then_run_params_after_workflow[<wf>][<param>] = value




@dataclass(frozen=True)
class ThenRunParamSpec:
    """Description of one inline parameter row beneath a then-run
    selector. `name` is the storage key; `label` is the prefix
    rendered in front of the editable buffer (with a trailing
    space); `valid_chars` gates which printable characters get
    appended on keystroke; `refuse_leading_dash` mirrors the
    branch-name + tag-name guard used elsewhere so a typed value
    can never look like a `-x` git option."""
    name: str
    label: str
    valid_chars: frozenset
    refuse_leading_dash: bool = True


def _then_run_param_specs(value: str,
                          repo: Repo) -> "list[ThenRunParamSpec]":
    """Return the parameter specs for the action `value` resolves
    to.

    * `__add_tag__` sentinel → a single `tag` param (branch-name
      character set, leading-dash forbidden).
    * Workflow name → one spec per `workflow_dispatch.inputs` entry
      parsed from the YAML, with permissive ASCII content (free-form
      version strings, environment names, etc.) and `-` allowed
      anywhere since the value goes through `gh -F key=value` rather
      than as a positional ref.
    * Anything else (empty string / unknown workflow) → no params."""
    if value == ADD_TAG_VALUE:
        return [ThenRunParamSpec(
            name="tag", label="tag:",
            valid_chars=_VALID_TAG_CHARS,
            refuse_leading_dash=True)]
    if not value:
        return []
    for wf in repo.workflows:
        if wf.name != value or not wf.dispatchable:
            continue
        return [
            ThenRunParamSpec(
                name=inp.name,
                label=f"{inp.name}:",
                valid_chars=_VALID_INPUT_CHARS,
                refuse_leading_dash=False,
            )
            for inp in wf.inputs
        ]
    return []


def _then_run_param_value(selector: ThenRunSelector,
                          param_name: str) -> str:
    """Read one parameter's buffered value for the selector's slot.
    Empty string when never set."""
    if selector.after_workflow:
        return (selector.repo.then_run_params_after_workflow
                .get(selector.after_workflow, {})
                .get(param_name, ""))
    return selector.repo.then_run_params_after_push.get(param_name, "")


def _set_then_run_param_value(selector: ThenRunSelector,
                              param_name: str, value: str) -> None:
    """Persist one parameter's buffered value. Empty value clears
    the entry (and prunes the per-workflow inner dict so empty
    chains don't leave dead keys around)."""
    if selector.after_workflow:
        wf = selector.after_workflow
        repo = selector.repo
        bucket = repo.then_run_params_after_workflow.setdefault(wf, {})
        if value:
            bucket[param_name] = value
        else:
            bucket.pop(param_name, None)
            if not bucket:
                repo.then_run_params_after_workflow.pop(wf, None)
        return
    if value:
        selector.repo.then_run_params_after_push[param_name] = value
    else:
        selector.repo.then_run_params_after_push.pop(param_name, None)


def _find_param_spec(selector: ThenRunSelector,
                     param_name: str) -> "ThenRunParamSpec | None":
    """Lookup helper used by the inline-edit handler to grab the
    spec (and its `valid_chars` / leading-dash rule) for the
    currently-focused param row. None when the selector has been
    cycled away from its parameterised value before the edit
    landed."""
    for spec in _then_run_param_specs(
            _then_run_current(selector), selector.repo):
        if spec.name == param_name:
            return spec
    return None


def _then_run_selector_for(block: ReviewBlock,
                           after_workflow: str) -> ThenRunSelector:
    """Return the selector pinned to `after_workflow` on this block,
    creating it (and caching it on `block.then_run_items`) when none
    exists. Stable identity matters — focus comparisons rely on
    object identity, so the same key always returns the same
    selector instance even after a chain extends."""
    for sel in block.then_run_items:
        if sel.after_workflow == after_workflow:
            return sel
    sel = ThenRunSelector(repo=block.target_repo,
                          after_workflow=after_workflow)
    block.then_run_items.append(sel)
    return sel


def _walk_then_run_chain(block: ReviewBlock, start_workflow: str):
    """Yield `(selector, depth)` along the chain rooted at
    `start_workflow` (`""` for after-push). `depth` starts at 0 for
    the root selector and increments by 1 per chained step. The
    walk stops at the first empty / "__add_tag__" value, after
    yielding that terminal selector — those are the user-visible
    "trailing" slots where the chain hasn't been extended further."""
    seen: "set[str]" = set()
    cur = start_workflow
    depth = 0
    while cur is not None and cur not in seen:
        seen.add(cur)
        sel = _then_run_selector_for(block, cur)
        yield sel, depth
        value = _then_run_current(sel)
        if value == "" or value == ADD_TAG_VALUE:
            break
        cur = value
        depth += 1


def cycle_then_run(selector: ThenRunSelector, direction: int) -> None:
    """Cycle the selector's choice through the repo's dispatchable
    workflows + a '(none)' slot + the "add tag" sentinel. The same
    wheel is offered for after-push and per-workflow chains —
    tagging makes sense in both contexts (mark the commit just
    pushed; mark the commit a CI build covered)."""
    options = _then_run_options(selector.repo)
    if not options:
        _then_run_set(selector, "")
        return
    wheel = [""] + options
    current = _then_run_current(selector)
    try:
        i = wheel.index(current)
    except ValueError:
        i = 0
    i = (i + direction) % len(wheel)
    _then_run_set(selector, wheel[i])


# ---------- Two-panel review screen --------------------------------------


def _file_status_pair(x: str, y: str, pane_focused: bool = False) -> Optional[int]:
    """Map an XY porcelain status pair to a pastel colour pair,
    matching the action-menu's tree pane (delete > add > rename >
    modify). Returns None for plain rows that don't need an overlay."""
    pair = (x, y)
    if "U" in pair or pair == ("A", "A") or pair == ("D", "D"):
        return PAIR_PASTEL_RED_ACTIVE if pane_focused else PAIR_PASTEL_RED
    if "D" in pair:
        return PAIR_PASTEL_RED_ACTIVE if pane_focused else PAIR_PASTEL_RED
    if "A" in pair:
        return PAIR_PASTEL_GREEN_ACTIVE if pane_focused else PAIR_PASTEL_GREEN
    if "R" in pair:
        return PAIR_PASTEL_BLUE_ACTIVE if pane_focused else PAIR_PASTEL_BLUE
    if "M" in pair:
        return PAIR_PASTEL_YELLOW_ACTIVE if pane_focused else PAIR_PASTEL_YELLOW
    return None


def _review_spinner(state: State) -> str:
    """Same spinner glyph the sidebar / action-menu animations use,
    so every animated indicator on screen ticks in lockstep."""
    return SPINNER_FRAMES[state.spinner_frame % len(SPINNER_FRAMES)]


def _collect_review_focusables(
    blocks: List[ReviewBlock],
) -> List[Tuple[int, str, object]]:
    """Flatten every block's interactive items into one ordered list
    of `(block_idx, kind, item)`. `kind` is "suggest", "lfs",
    "toggle", "then_run", or "param_input". Up/Down on the left pane
    navigates this list; `block_idx` says which block's files the
    right pane should show.

    Order matches `_block_left_rows`: the per-block "suggest" entry
    (the message line — Left re-runs commit-message suggest scoped to
    that block's checked files) goes first; then LFS rows; each
    workflow toggle is immediately followed by its then-run chain;
    then-run-after-push chain comes last. Every then-run row is
    followed by one `param_input` row per parameter the action it
    points to declares (today only the `__add_tag__` sentinel
    declares a "tag" param; workflow_dispatch inputs would slot in
    here too)."""
    out: List[Tuple[int, str, object]] = []

    def emit_chain(block, start_workflow):
        for sel, _depth in _walk_then_run_chain(block, start_workflow):
            out.append((block_index, "then_run", sel))
            current = _then_run_current(sel)
            for spec in _then_run_param_specs(current, sel.repo):
                out.append((block_index, "param_input",
                            (sel, spec.name)))

    for block_index, block in enumerate(blocks):
        # Suggest focusable per block — non-merging blocks only, since
        # a merge-in-progress block can't accept a fresh commit
        # message anyway.
        if not block.merging:
            out.append((block_index, "suggest", block))
        for c in block.lfs_candidates:
            out.append((block_index, "lfs", c))
        for tog in block.workflow_toggles:
            out.append((block_index, "toggle", tog))
            emit_chain(block, tog.workflow_name)
        # Child (nested-submodule) blocks have no target_repo and no
        # workflow toggles, so they get no after-push then-run chain
        # either — skip the walk to avoid creating selectors with a
        # None repo, which would crash the moment we read their state.
        if block.target_repo is not None:
            emit_chain(block, "")
    return out


def _focused_block_idx(focusables: List[Tuple[int, str, object]],
                       focus: int, default: int = 0) -> int:
    if focus < 0 or focus >= len(focusables):
        return default
    return focusables[focus][0]


def _word_wrap(text: str, first_w: int, cont_w: int) -> List[str]:
    """Greedy word-wrap, breaking on whitespace. Words longer than a
    row are hard-broken at the row boundary so a 200-char URL doesn't
    silently truncate. Returns the list of wrapped lines, each at
    most `first_w` (line 0) or `cont_w` (lines 1+) chars wide."""
    if not text:
        return []
    if first_w <= 0:
        first_w = 1
    if cont_w <= 0:
        cont_w = 1
    words = text.split(" ")
    lines: List[str] = []
    current = ""
    cap = first_w
    for w in words:
        # Hard-break a word that's longer than the available width.
        while len(w) > cap:
            if current:
                lines.append(current)
                current = ""
                cap = cont_w
            lines.append(w[:cap])
            w = w[cap:]
        candidate = w if not current else current + " " + w
        if len(candidate) <= cap:
            current = candidate
        else:
            lines.append(current)
            current = w
            cap = cont_w
    if current:
        lines.append(current)
    return lines


def _wrap_message_lines(message: str, cap: int, max_w: int
                        ) -> List[str]:
    """Lay out a commit message across as many rows as needed for
    the review screen's left pane. End-truncates the FULL message
    when `cap > 0` and `len(message) > cap` (cap=0 disables the
    cap entirely). Continuation lines align under the opening
    quote; the closing quote sits on the last line, on its own
    line if the last chunk would otherwise overflow `max_w`."""
    if cap > 0 and len(message) > cap:
        message = message[: max(0, cap - 1)] + "…"
    if not message:
        return ['  message: ""']
    prefix = '  message: "'
    cont_indent = " " * len(prefix)
    text = message.replace("\n", " ").replace("\r", "")
    first_w = max(1, max_w - len(prefix))
    cont_w = max(1, max_w - len(cont_indent))
    chunks = _word_wrap(text, first_w, cont_w)
    if not chunks:
        return [prefix + '"']
    lines = [prefix + chunks[0]]
    for chunk in chunks[1:]:
        lines.append(cont_indent + chunk)
    last = lines[-1]
    if len(last) + 1 > max_w:
        # Pushing the closing quote here would overflow — drop it on
        # its own indented row instead. Reads as "open quote, body,
        # close quote on its own line".
        lines.append(cont_indent + '"')
    else:
        lines[-1] = last + '"'
    return lines


def _block_left_rows(
    block: ReviewBlock,
    focusables: List[Tuple[int, str, object]],
    focus: int, panel_focus: str, block_idx: int,
    inner_w: int, message_cap: int,
    state: Optional[State] = None,
) -> List[Tuple[str, int, bool]]:
    """Build the (text, attr, is_focused) tuples for ONE block on
    the left pane. Focus highlighting only kicks in when the left
    pane has the active focus — when the user has Shift+Tab'd over
    to the right pane, the rows render in their resting style so
    both panels can't claim focus at the same time. `inner_w` is
    the available pane width used to wrap multi-line content (the
    commit message); `message_cap` end-truncates the full message
    before wrapping (0 disables)."""
    rows: List[Tuple[str, int, bool]] = []
    header_attr = curses.A_BOLD | curses.color_pair(PAIR_BRANCH)
    rows.append((f"{block.label}  [{block.branch}]", header_attr, False))

    if block.merging:
        rows.append((
            "  ⚠ merge / rebase in progress — commit will be skipped",
            curses.color_pair(PAIR_ERR), False))
        for cp in block.conflict_paths:
            rows.append((f"      {cp}",
                         curses.color_pair(PAIR_ERR), False))
        return rows

    suggest_focused = (panel_focus == "left" and focus >= 0
                       and focusables[focus] == (block_idx, "suggest", block))
    suggest_attr = (curses.A_REVERSE if suggest_focused else 0)
    if block.suggesting and not block.message:
        spinner = (_review_spinner(state) if state is not None
                   else SPINNER_FRAMES[0])
        rows.append((f"  message: {spinner} generating…",
                     suggest_attr | curses.A_DIM, suggest_focused))
    elif block.message:
        wrapped = _wrap_message_lines(block.message, message_cap, inner_w)
        for i, line in enumerate(wrapped):
            rows.append((line, suggest_attr,
                         suggest_focused and i == 0))
    else:
        # No message yet — render a placeholder line so the suggest
        # focusable has something to highlight before the user types
        # a message or hits Left to generate one.
        rows.append(('  message: ""', suggest_attr | curses.A_DIM,
                     suggest_focused))
    push_line = f"  {block.push_summary}"
    arrow = push_line.rfind("→ ")
    if arrow != -1 and "yes" in push_line:
        val_attr = curses.color_pair(PAIR_BRANCH) | curses.A_DIM
        rows.append(([
            (push_line[:arrow + 2], curses.A_DIM),
            (push_line[arrow + 2:], val_attr),
        ], curses.A_DIM, False))
    else:
        rows.append((push_line, curses.A_DIM, False))
    if block.siblings_summary:
        val_attr = curses.color_pair(PAIR_BRANCH) | curses.A_DIM
        rows.append(([
            ("  sync: ", curses.A_DIM),
            (block.siblings_summary, val_attr),
        ], curses.A_DIM, False))

    if block.lfs_candidates:
        rows.append((
            f"  ⚠ files ≥{block.threshold_mb} MB not LFS-tracked — "
            "push will fail:",
            curses.color_pair(PAIR_ERR), False))
        for cand in block.lfs_candidates:
            is_focused = (panel_focus == "left" and focus >= 0
                          and focusables[focus] == (block_idx, "lfs", cand))
            check = "[x]" if cand.track else "[ ]"
            text = f"      {check}  {cand.path}  ({cand.size_str})"
            base = PAIR_OK if cand.track else PAIR_ERR
            attr = curses.color_pair(base)
            if is_focused:
                attr |= curses.A_REVERSE
            rows.append((text, attr, is_focused))

    def append_then_run(sel, indent_cols: int) -> None:
        """Render one selector + (optional) tag_input row. `indent_cols`
        is the column the label starts at — the chain walker
        increments this per step so chained then-runs nest visually
        beneath their parent."""
        is_focused = (panel_focus == "left" and focus >= 0
                      and focusables[focus] == (block_idx, "then_run", sel))
        indent = " " * indent_cols
        label = ("then run after push:" if sel.after_workflow == ""
                 else "then run:")
        current = _then_run_current(sel)
        text = f"{indent}{label} ‹ {_then_run_label(current)} ›"
        if is_focused:
            attr = curses.color_pair(PAIR_BRANCH) | curses.A_BOLD
        else:
            attr = curses.A_DIM
        rows.append((text, attr, is_focused))
        # When the action `current` resolves to declares parameters,
        # drop one indented editable row per parameter beneath. The
        # buffer lives on the repo's `then_run_params_*` dicts
        # (keyed by selector slot then by param name); typing while
        # the param_input focusable is selected modifies it, the
        # dispatch site reads the slot's params on completion of
        # the parent task and clears.
        param_indent = indent + "  "
        for spec in _then_run_param_specs(current, sel.repo):
            param_focused = (panel_focus == "left" and focus >= 0
                             and focusables[focus]
                             == (block_idx, "param_input",
                                 (sel, spec.name)))
            value = _then_run_param_value(sel, spec.name)
            cursor = "_" if param_focused else ""
            param_text = (f"{param_indent}{spec.label} "
                          f"{value}{cursor}")
            if param_focused:
                param_attr = (curses.color_pair(PAIR_BRANCH)
                              | curses.A_BOLD | curses.A_REVERSE)
            else:
                param_attr = curses.A_DIM
            rows.append((param_text, param_attr, param_focused))

    for tog in block.workflow_toggles:
        is_focused = (panel_focus == "left" and focus >= 0
                      and focusables[focus] == (block_idx, "toggle", tog))
        on = tog.repo.track_workflow.get(tog.workflow_name, False)
        check = "[x]" if on else "[ ]"
        text = f"  {check}  track action: {tog.workflow_name}"
        if on:
            attr = curses.color_pair(PAIR_OK)
        else:
            attr = curses.color_pair(PAIR_HEADER) | curses.A_DIM
        if is_focused:
            attr |= curses.A_REVERSE
        rows.append((text, attr, is_focused))
        # Walk the chain rooted at this tracked workflow. Base
        # indent of 8 cols matches the historical "        " prefix
        # for after-toggle then-runs; each chained step adds 2 cols
        # so a 4-level chain ends at column 14.
        for sel, depth in _walk_then_run_chain(block, tog.workflow_name):
            append_then_run(sel, indent_cols=8 + 2 * depth)

    # After-push chain — its root selector renders at column 2
    # ("then run after push:") and chained continuations step in
    # by 2 cols each. Child blocks carry no target_repo and aren't
    # offered a then-run row, matching _collect_review_focusables.
    if block.target_repo is not None:
        for sel, depth in _walk_then_run_chain(block, ""):
            append_then_run(sel, indent_cols=2 + 2 * depth)
    return rows


def _build_left_pane_rows(
    blocks: List[ReviewBlock],
    focusables: List[Tuple[int, str, object]],
    focus: int, panel_focus: str, inner_w: int,
    message_cap: int,
    state: Optional[State] = None,
) -> Tuple[List[Tuple[str, int]], int]:
    """Concatenate every block's rows into one flat (text, attr)
    list, with subtle divider lines between blocks. Returns
    (rows, focused_row_index) — the second value tells the caller
    which row index to keep visible when adjusting scroll.

    `inner_w` is the available pane width (commit messages wrap to
    fit it); `message_cap` end-truncates the full message before
    wrapping (0 disables the cap)."""
    rows: List[Tuple[str, int]] = []
    focused_row_idx = -1
    for bi, block in enumerate(blocks):
        block_rows = _block_left_rows(
            block, focusables, focus, panel_focus, bi,
            inner_w, message_cap, state=state)
        for text, attr, is_focused in block_rows:
            if is_focused:
                focused_row_idx = len(rows)
            rows.append((text, attr))
        if bi < len(blocks) - 1:
            rows.append(("─" * max(1, inner_w - 2), curses.A_DIM))
    return rows, focused_row_idx


def _draw_left_pane(stdscr, x: int, y: int, w: int, h: int,
                    rows: List[Tuple[str, int]], scroll: int) -> None:
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


def _render_review_file_row(stdscr, y: int, x: int, w: int,
                            fe: FileEntry, focused: bool,
                            checked: bool,
                            pane_focused: bool = False) -> None:
    """Render one right-pane row: checkbox + status code + path +
    ins/del counts. The checkbox reflects this file's `staged_paths`
    bit — Space toggles it; the commit pipeline reads it to decide
    what lands in the index. Unchecked rows render dim so the user
    can see at a glance which files would be left out of the
    commit."""
    p_green = PAIR_PASTEL_GREEN_ACTIVE if pane_focused else PAIR_PASTEL_GREEN
    p_red   = PAIR_PASTEL_RED_ACTIVE   if pane_focused else PAIR_PASTEL_RED
    code = "??" if fe.untracked else f"{fe.x}{fe.y}"
    stat_ins = f"+{fe.inserted}" if (fe.inserted or fe.deleted) else ""
    stat_del = f"-{fe.deleted}" if (fe.inserted or fe.deleted) else ""
    stat = f"{stat_ins} {stat_del}".strip()
    box = "[x]" if checked else "[ ]"
    left = f" {box} {code}  "
    pad = max(1, w - len(left) - len(stat) - 1)
    name = fe.path
    if len(name) > pad:
        name = name[: pad - 1] + "…"
    name = name.ljust(pad)
    full = f"{left}{name} {stat}"
    fill_attr = curses.color_pair(
        PAIR_SB_FG_ACTIVE if pane_focused else PAIR_SB_FG)
    if focused:
        safe_addstr(stdscr, y, x, full, fill_attr | curses.A_REVERSE)
        return
    # Dim the entire row when unchecked or untracked-and-not-staged so
    # the checked / will-be-committed files visually dominate.
    if not checked or fe.untracked:
        base = fill_attr | curses.A_DIM
    else:
        base = fill_attr
    safe_addstr(stdscr, y, x, full, base)
    if not fe.untracked and checked:
        pair_id = _file_status_pair(fe.x, fe.y, pane_focused)
        if pair_id is not None:
            # Status code lives at x + 1 + 3 + 1 = x + 5 (one space, the
            # 3-cell checkbox, one space).
            safe_addstr(stdscr, y, x + 5, code, curses.color_pair(pair_id))
    if stat and checked:
        stat_x = x + len(left) + pad + 1
        safe_addstr(stdscr, y, stat_x, stat_ins,
                    curses.color_pair(p_green))
        safe_addstr(stdscr, y, stat_x + len(stat_ins) + 1, stat_del,
                    curses.color_pair(p_red))


# Right-pane toolbar — three buttons right-aligned on the same row as
# the "Changes" panel header. Action buttons (`stage all`, `unstage
# all`) render bracketed and fire on Enter; the toggle button
# (`amend`) renders with a `[ ]` / `[X]` checkbox prefix and fires on
# either Enter or Space. Tuple shape: (button_id, label, kind).
TOOLBAR_KIND_ACTION = "action"
TOOLBAR_KIND_TOGGLE = "toggle"
_TOOLBAR_BUTTONS: "Tuple[Tuple[int, str, str], ...]" = (
    (0, "stage all", TOOLBAR_KIND_ACTION),
    (1, "unstage all", TOOLBAR_KIND_ACTION),
    (2, "amend", TOOLBAR_KIND_TOGGLE),
)
TOOLBAR_BUTTON_AMEND = 2  # exported so main_loop can scope Space


def is_toolbar_toggle(button_id: int) -> bool:
    """True when the toolbar button is a toggle (responds to Space
    + Enter); False for plain action buttons (Enter-only). Public so
    main_loop can scope Space without re-listing the button table."""
    for bid, _, kind in _TOOLBAR_BUTTONS:
        if bid == button_id:
            return kind == TOOLBAR_KIND_TOGGLE
    return False


def fire_toolbar_action(block: ReviewBlock, button_id: int) -> bool:
    """Apply the toolbar action to `block`. Returns True when state
    changed (caller can use this as a redraw cue), False when the
    button was a no-op (already-staged → stage-all, no unpushed
    commit → amend, etc.). Public so `main_loop` can dispatch the
    Space/Enter key without us re-importing the predicates."""
    if not _toolbar_available(block, button_id):
        return False
    if button_id == 0:  # stage all
        for fe in block.files:
            block.staged_paths[fe.path] = True
        return True
    if button_id == 1:  # unstage all
        for fe in block.files:
            block.staged_paths[fe.path] = False
        return True
    if button_id == TOOLBAR_BUTTON_AMEND:
        block.amend = not block.amend
        return True
    return False


def _block_unpushed_commits(block: Optional[ReviewBlock]) -> int:
    """`ahead` count for the block's target — top-level repo for plain
    blocks, the ChildRef for submodule blocks. Used to gate `amend`
    so we never offer to rewrite a published commit. Returns 0 when
    we don't have an authoritative answer (no target attached, no
    upstream configured), which keeps the toolbar conservative."""
    if block is None:
        return 0
    if block.target_child is not None:
        return max(0, getattr(block.target_child, "ahead", 0) or 0)
    if block.target_repo is not None:
        return max(0, getattr(block.target_repo, "ahead", 0) or 0)
    return 0


def _toolbar_available(block: Optional[ReviewBlock], button_id: int) -> bool:
    """`stage all` is available when at least one file is unchecked;
    `unstage all` is available when at least one file is checked;
    `amend` is available when the target has at least one local
    commit that hasn't been pushed yet (so amending doesn't rewrite
    shared history). Returns False when the block has no files
    loaded yet — the buttons stay disabled until we know the
    working-tree state."""
    if block is None:
        return False
    if button_id == 0:  # stage all
        if not block.files:
            return False
        return any(not block.staged_paths.get(fe.path, False)
                   for fe in block.files)
    if button_id == 1:  # unstage all
        if not block.files:
            return False
        return any(block.staged_paths.get(fe.path, False)
                   for fe in block.files)
    if button_id == TOOLBAR_BUTTON_AMEND:
        return _block_unpushed_commits(block) > 0
    return False


def _format_toolbar_button(label: str, kind: str,
                           on: bool = False) -> str:
    """Action buttons render as `[ stage all ]`; toggle buttons render
    with a checkbox prefix `[X] amend` / `[ ] amend` so the on/off
    state reads at a glance without having to compare colours."""
    if kind == TOOLBAR_KIND_TOGGLE:
        return f"[{'X' if on else ' '}] {label}"
    return f"[ {label} ]"


def _draw_right_toolbar(stdscr, y: int, x: int, w: int,
                        block: Optional[ReviewBlock],
                        pane_focused: bool, sb: int) -> None:
    """Right-align the toolbar at the trailing edge of the right pane
    on row `y`. The focused button (only when the pane has focus AND
    `block.toolbar_focus >= 0`) renders cyan-bold; available-but-
    unfocused buttons render dim white; unavailable buttons render
    in PAIR_SB_FG_DISABLED so the dead state reads at a glance
    without us having to grey out the brackets too."""
    if block is None or w <= 0:
        return
    pieces: "list[tuple[int, str]]" = []
    for bid, label, kind in _TOOLBAR_BUTTONS:
        on = (kind == TOOLBAR_KIND_TOGGLE
              and bid == TOOLBAR_BUTTON_AMEND
              and block.amend)
        pieces.append((bid, _format_toolbar_button(label, kind, on)))
    total = sum(len(p) for _, p in pieces) + max(0, len(pieces) - 1)
    if total > w:
        return  # not enough room — skip silently
    cur_x = x + w - total
    toolbar_focused = pane_focused and block.toolbar_focus >= 0
    for i, (bid, text) in enumerate(pieces):
        available = _toolbar_available(block, bid)
        is_focused = (toolbar_focused
                      and block.toolbar_focus == bid)
        if is_focused:
            attr = curses.color_pair(PAIR_SB_CYAN_ACTIVE) | curses.A_BOLD
        elif available:
            attr = sb | curses.A_DIM
        else:
            attr = curses.color_pair(PAIR_SB_FG_DISABLED) | curses.A_DIM
        safe_addstr(stdscr, y, cur_x, text, attr)
        cur_x += len(text)
        if i < len(pieces) - 1:
            cur_x += 1  # space between buttons


def _draw_right_pane(stdscr, x: int, y: int, w: int, h: int,
                     block: Optional[ReviewBlock],
                     panel_focus: str, state: State) -> None:
    """Right pane = working-tree files for the focused block. Header
    accents bright when the right pane has focus, dims otherwise so
    the user can see at a glance which side ↑/↓ steers."""
    if block is None or w <= 0 or h <= 0:
        return
    pane_focused = panel_focus == "right"
    fill_pair = PAIR_SB_FG_ACTIVE if pane_focused else PAIR_SB_FG
    fill_attr = curses.color_pair(fill_pair)
    dim_attr = fill_attr | curses.A_DIM
    fill = " " * w
    scr_h, _ = stdscr.getmaxyx()
    for fy in range(y, min(y + h, scr_h)):
        safe_addstr(stdscr, fy, x, fill, fill_attr)
    if pane_focused:
        header_attr = curses.color_pair(PAIR_SB_CYAN_ACTIVE) | curses.A_BOLD
    else:
        header_attr = fill_attr | curses.A_BOLD | curses.A_DIM
    if block.files_loading and not block.files:
        count_str = _review_spinner(state)
    else:
        count_str = str(len(block.files))
    header = f"{block.label}: {count_str} file(s)"
    safe_addstr(stdscr, y, x, header[:w], header_attr)

    line = y + 2
    list_h = max(0, h - (line - y))
    if list_h <= 0:
        return

    if block.files_loading and not block.files:
        safe_addstr(stdscr, line, x + 2,
                    f"{_review_spinner(state)} loading files…",
                    dim_attr)
        return
    if not block.files:
        safe_addstr(stdscr, line, x + 2, "(no changes)", dim_attr)
        return

    sel = block.file_selected
    block.file_scroll = clamp_scroll(
        sel, block.file_scroll, len(block.files), list_h)

    for slot in range(list_h):
        idx = block.file_scroll + slot
        if idx >= len(block.files):
            break
        fe = block.files[idx]
        focused = pane_focused and idx == sel
        checked = block.staged_paths.get(fe.path, False)
        _render_review_file_row(stdscr, line + slot, x, w, fe, focused,
                                checked, pane_focused)
    if block.file_scroll > 0:
        draw_scroll_overflow(stdscr, line, x, w,
                             block.file_scroll, "up", dim_attr)
    end = min(len(block.files), block.file_scroll + list_h)
    if end < len(block.files):
        below = len(block.files) - end
        draw_scroll_overflow(stdscr, line + list_h - 1, x, w,
                             below, "down", dim_attr)


def _review_hints(focusables: List[Tuple[int, str, object]],
                  focus: int, panel_focus: str,
                  blocks: Optional[List[ReviewBlock]] = None) -> List[Hint]:
    hints: List[Hint] = []
    if panel_focus == "left":
        hints.append(Hint(KEY_UP_DOWN, "select"))
        if 0 <= focus < len(focusables):
            _, kind, obj = focusables[focus]
            if kind == "suggest":
                hints.append(Hint("←", "suggest message (staged)"))
            elif kind == "lfs":
                hints.append(Hint(
                    KEY_SPACE,
                    "stop tracking" if obj.track else "track with LFS"))
            elif kind == "toggle":
                on = obj.repo.track_workflow.get(obj.workflow_name, False)
                hints.append(Hint(
                    KEY_SPACE,
                    "untrack workflow" if on else "track workflow"))
            elif kind == "param_input":
                # `obj` is `(selector, param_name)`. Look up the
                # spec so the hint mentions which parameter is in
                # focus and what character class is allowed.
                sel, param_name = obj
                spec = _find_param_spec(sel, param_name)
                if spec is not None:
                    hints.append(Hint(
                        "a-z, 0-9, /-_.",
                        f"type {param_name}"))
                else:
                    hints.append(Hint("type", f"edit {param_name}"))
            else:  # then_run
                hints.append(Hint(KEY_LEFT_RIGHT,
                                  "cycle then-run target"))
        hints.append(Hint(KEY_SHIFT_TAB, "files panel"))
        # Only advertise Enter once every block has finished its file
        # load — pressing it earlier would race the staging step and
        # fail with "nothing staged". Matches the gate in main_loop.
        if blocks is not None and not any(b.files_loading for b in blocks):
            hints.append(Hint(KEY_ENTER, "execute commits"))
    else:  # right
        # Pull the focused block once so we can swap hints between
        # toolbar focus (top of pane) and file-list focus.
        block = None
        if blocks is not None and 0 <= focus < len(focusables):
            bi = focusables[focus][0]
            if 0 <= bi < len(blocks):
                block = blocks[bi]
        if block is not None and block.toolbar_focus >= 0:
            hints.append(Hint(KEY_LEFT_RIGHT, "switch button"))
            hints.append(Hint(KEY_UP_DOWN, "files"))
            # Hint string mirrors the focused button's verb. Toggle
            # buttons (amend) accept Space + Enter; action buttons
            # (stage all / unstage all) accept Enter only.
            if block.toolbar_focus == TOOLBAR_BUTTON_AMEND:
                verb = "uncheck amend" if block.amend else "check amend"
                hints.append(Hint(f"{KEY_SPACE} / {KEY_ENTER}", verb))
            elif block.toolbar_focus == 0:
                hints.append(Hint(KEY_ENTER, "stage all"))
            elif block.toolbar_focus == 1:
                hints.append(Hint(KEY_ENTER, "unstage all"))
            hints.append(Hint(KEY_SHIFT_TAB, "back to repos"))
        else:
            hints.append(Hint(KEY_UP_DOWN, "select file"))
            # Space toggles the staged-for-commit checkbox on the
            # focused file. Hint string mirrors the current state
            # when we know the block; falls back to a neutral
            # "stage / unstage" otherwise.
            space_label = "toggle stage"
            if block is not None and 0 <= block.file_selected < len(block.files):
                fe = block.files[block.file_selected]
                on = block.staged_paths.get(fe.path, False)
                space_label = "unstage" if on else "stage"
            hints.append(Hint(KEY_SPACE, space_label))
            hints.append(Hint(KEY_TAB, "view diff"))
            hints.append(Hint(KEY_SHIFT_TAB, "back to repos"))
    # Ctrl+K resets every Repo's then-run state (after-push +
    # after-workflow chains + workflow-tracking opt-ins). Only
    # advertised when there's something to clear, so a workspace
    # with no chains set doesn't carry a redundant hint.
    if blocks is not None and _any_then_runs_set(blocks):
        hints.append(Hint(KEY_CTRL_K, "clear chains"))
    hints.append(Hint(KEY_ESC, "back"))
    return hints


def _any_then_runs_set(blocks: List[ReviewBlock]) -> bool:
    """True iff any review block's target repo has a then-run target,
    workflow tracking opt-in, or chained after-workflow target
    currently set. Drives the conditional `Ctrl+K clear chains` hint
    — no signal to clear → no hint."""
    seen: "set[int]" = set()
    for b in blocks:
        repo = b.target_repo if b.target_repo is not None else (
            b.target_child.repo if b.target_child is not None else None)
        if repo is None or id(repo) in seen:
            continue
        seen.add(id(repo))
        if repo.then_run_after_push:
            return True
        if repo.then_run_after_workflow:
            return True
        if any(repo.track_workflow.values()):
            return True
    return False


def draw_review(stdscr, state: State, blocks: List[ReviewBlock],
                focusables: List[Tuple[int, str, object]],
                focus: int, panel_focus: str,
                scroll: int) -> int:
    """Draw the two-panel review screen and return the (clamped)
    left-pane scroll the caller should keep going forward.

    Layout:
        ┌─ Review · N targets · auto-stage on · auto-push on ─┐
        │                                                       │
        │  block A header   │   block A files (header)          │
        │  ...              │   working-tree rows               │
        │  ── divider ──    │                                   │
        │  block B header   │                                   │
        │  ...              │                                   │
        │                                                       │
        │  hint line                                            │
        └───────────────────────────────────────────────────────┘
    """
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    # Workspace title bar — same shape as the main screen's row 0
    # (`Idlegit · <workspace name>`), MINUS the focus chevrons. The
    # review screen's workspace selector isn't navigable, just a
    # label, so the chevrons would be misleading.
    safe_addstr(stdscr, 0, 0, APP_DISPLAY_NAME,
                curses.A_BOLD | curses.color_pair(PAIR_HEADER))
    if state.workspace_name:
        safe_addstr(stdscr, 0, len(APP_DISPLAY_NAME), " · ", curses.A_DIM)
        ws_attr = curses.A_BOLD | curses.color_pair(PAIR_BRANCH)
        safe_addstr(stdscr, 0, len(APP_DISPLAY_NAME) + 3,
                    state.workspace_name, ws_attr)

    body_top = 4
    body_h = max(1, h - body_top - 2)
    left_w = max(40, int(w * 0.55))
    if left_w >= w - 12:
        left_w = max(20, w - 12)
    right_x = left_w + 1
    right_w = max(10, w - right_x - 1)

    # Panel title row — "Review" on the left, "Changes" on the right,
    # each cyan when its pane has focus and dim when it doesn't, matching
    # the "Repositories" / "Tasks" header treatment on the main screen.
    left_focused = panel_focus == "left"
    right_focused = panel_focus == "right"
    left_title_attr = (curses.color_pair(PAIR_BRANCH) | curses.A_BOLD
                       if left_focused else curses.A_DIM | curses.A_BOLD)
    right_title_attr = (curses.color_pair(PAIR_BRANCH) | curses.A_BOLD
                        if right_focused else curses.A_DIM | curses.A_BOLD)
    safe_addstr(stdscr, 2, 0, "Review", left_title_attr)
    sub = (f"{len(blocks)} target(s)  ·  "
           f"auto-stage: {'on' if state.auto_stage else 'off'}  ·  "
           f"auto-push: {'on' if state.auto_push else 'off'}")
    safe_addstr(stdscr, 2, len("Review") + 3, sub, curses.A_DIM)
    safe_addstr(stdscr, 2, right_x + 1, "Changes", right_title_attr)

    # Right-pane toolbar — `[ stage all ] [ unstage all ]` right-
    # aligned in the right-pane column range on the same row as the
    # "Changes" header. Operates on the currently focused block; the
    # buttons grey out when their action is a no-op.
    right_block = (blocks[_focused_block_idx(focusables, focus)]
                   if blocks else None)
    sb_attr = curses.color_pair(
        PAIR_SB_FG_ACTIVE if right_focused else PAIR_SB_FG)
    _draw_right_toolbar(stdscr, 2, right_x + 1, right_w,
                        right_block, right_focused, sb_attr)

    rows, focused_row = _build_left_pane_rows(
        blocks, focusables, focus, panel_focus, left_w,
        state.max_commit_message_length_in_review, state=state)
    if focused_row >= 0:
        if focused_row < scroll:
            scroll = focused_row
        elif focused_row >= scroll + body_h:
            scroll = focused_row - body_h + 1
    max_scroll = max(0, len(rows) - body_h)
    scroll = max(0, min(scroll, max_scroll))
    _draw_left_pane(stdscr, 0, body_top, left_w, body_h, rows, scroll)

    for row in range(body_h):
        safe_addstr(stdscr, body_top + row, left_w, "│", curses.A_DIM)

    block = blocks[_focused_block_idx(focusables, focus)] if blocks else None
    _draw_right_pane(stdscr, right_x + 1, body_top, right_w, body_h,
                     block, panel_focus, state)

    render_hints(stdscr, h - 1, 0, max(0, w - 1),
                 _review_hints(focusables, focus, panel_focus,
                               blocks=blocks),
                 attr=curses.A_DIM)
    curses.curs_set(0)
    stdscr.refresh()
    return scroll
