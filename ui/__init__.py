"""Idlegit curses UI package.

Full-screen flows are split across ``loading`` (startup refresh),
``main_screen`` (repo workspace + modals stack), ``review`` (two-panel
review), and ``main_loop`` (keyboard routing + confirm sub-loop). This
module re-exports symbols so callers can keep using ``from ui import …``.

Low-level pieces live in ``colors``, ``geometry``, ``hints``, ``modals``,
and ``sidebar`` — also re-exported here.
"""
from __future__ import annotations

from .colors import (  # noqa: F401  (re-exported public API)
    PAIR_AHEAD, PAIR_BEHIND, PAIR_BRANCH, PAIR_DIRTY, PAIR_ERR,
    PAIR_HEADER, PAIR_HINT, PAIR_OK,
    PAIR_PASTEL_BLUE, PAIR_PASTEL_BLUE_ACTIVE,
    PAIR_PASTEL_GREEN, PAIR_PASTEL_GREEN_ACTIVE,
    PAIR_PASTEL_RED, PAIR_PASTEL_RED_ACTIVE,
    PAIR_PASTEL_YELLOW, PAIR_PASTEL_YELLOW_ACTIVE,
    PAIR_SB_CYAN, PAIR_SB_CYAN_ACTIVE, PAIR_SB_ERR, PAIR_SB_FG,
    PAIR_SB_FG_ACTIVE,
    PAIR_SB_OK, PAIR_SB_WARN, PAIR_TOGGLE_OFF, PAIR_TOGGLE_ON, PAIR_WARN,
    _state_color, child_state_color, init_colors, state_color,
)
from .geometry import (  # noqa: F401  (re-exported public API)
    SIDEBAR_W, SIDEBAR_W_NARROW, clamp_scroll, draw_modal_fill,
    field_visible, modal_geometry, safe_addstr, sidebar_geometry,
    truncate,
)
from .hints import (  # noqa: F401  (re-exported public API)
    KEY_BACKSPACE, KEY_CTRL_R, KEY_CTRL_S, KEY_DOWN, KEY_END, KEY_ENTER,
    KEY_ESC, KEY_HOME, KEY_LEFT, KEY_LEFT_RIGHT, KEY_RIGHT, KEY_SHIFT_TAB,
    KEY_SPACE, KEY_TAB, KEY_UP, KEY_UP_DOWN, Hint, fit_hints,
    render_hint, render_hints,
)
from .modals import (  # noqa: F401  (re-exported public API)
    commit_workspace_creator,
    draw_action_menu, draw_align_heads_prompt, draw_branch_name_prompt,
    draw_branch_picker, draw_remote_branch_picker,
    draw_clone_modal, draw_commit_msg_editor,
    draw_commit_view_modal,
    draw_detached_recovery_prompt,
    draw_diff_viewer, draw_help_screen, draw_remotes_modal,
    draw_reset_prompt, draw_ssh_keygen_modal,
    draw_task_action_menu, draw_task_log_viewer, draw_workflow_picker,
    draw_workspace_creator, draw_workspace_menu, draw_app_menu,
    draw_workspace_switcher,
    handle_action_menu_key, handle_align_heads_prompt_key,
    handle_branch_name_prompt_key, handle_branch_picker_key,
    handle_remote_branch_picker_key,
    handle_clone_modal_key, handle_commit_msg_editor_key,
    handle_commit_view_modal_key,
    handle_detached_recovery_prompt_key,
    handle_diff_viewer_key, handle_help_screen_key,
    handle_ssh_keygen_modal_key,
    handle_remotes_modal_key,
    handle_reset_prompt_key, handle_task_action_menu_key,
    handle_task_log_viewer_key,
    handle_workflow_picker_key, handle_workspace_creator_key,
    handle_workspace_menu_key, handle_app_menu_key,
    handle_workspace_switcher_key,
    open_action_menu, open_align_heads_prompt, open_branch_name_prompt,
    open_branch_picker, open_remote_branch_picker,
    open_clone_modal, open_commit_msg_editor,
    open_commit_view_modal, open_help_screen,
    open_detached_recovery_prompt,
    open_diff_viewer, open_remotes_modal,
    open_reset_prompt, open_task_action_menu, open_workflow_picker,
    open_workspace_creator, open_workspace_menu, open_app_menu,
    open_workspace_switcher,
    tick_app_menu_update_check, tick_creator_checks,
    tick_menu_path_checks,
)
from .sidebar import (  # noqa: F401  (re-exported public API)
    SPINNER_FRAMES, draw_sidebar,
)

from .loading import (  # noqa: F401  (re-exported public API)
    draw_workspace_loading, refresh_all_workspaces,
)
from .main_screen import (  # noqa: F401  (re-exported public API)
    draw_child_row,
    draw_main,
    draw_repo_row,
    draw_state_legend,
    show_no_repos_message,
)
from .review import (  # noqa: F401  (re-exported for callers who need review pieces)
    build_review_blocks,
    cycle_then_run,
    draw_review,
    kick_off_review_files_load,
)
from .main_loop import (  # noqa: F401  (re-exported public API)
    confirm_quit,
    ensure_cursor_visible,
    handle_confirm,
    handle_main_key,
    handle_safe_merge,
    handle_task_panel_key,
)
