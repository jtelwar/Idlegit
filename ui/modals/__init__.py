"""Modal dialogs. Each modal lives in its own self-contained module:
the dataclass type comes from `models`, but the open/draw/handle trio
that mediates between curses input and that dataclass is colocated
here. New modals should follow the same shape — copy any of these as
a template."""
from __future__ import annotations

from .action_menu import (
    draw_action_menu, handle_action_menu_key,
)
from features.action_menu.session import open_action_menu
from .align_heads_prompt import (
    draw_align_heads_prompt, handle_align_heads_prompt_key,
    open_align_heads_prompt,
)
from .branch_name_prompt import (
    draw_branch_name_prompt, handle_branch_name_prompt_key,
)
from features.branch_name_prompt.session import open_branch_name_prompt
from .branch_picker import (
    draw_branch_picker, handle_branch_picker_key,
)
from features.branch_picker.session import open_branch_picker
from .remote_branch_picker import (
    draw_remote_branch_picker, handle_remote_branch_picker_key,
)
from features.remote_branch_picker.session import open_remote_branch_picker
from .clone import (
    draw_clone_modal, handle_clone_modal_key,
)
from features.clone_modal.session import open_clone_modal
from .commit_msg_editor import (
    draw_commit_msg_editor, handle_commit_msg_editor_key,
)
from features.commit_msg_editor.session import open_commit_msg_editor
from .commit_view import (
    draw_commit_view_modal, handle_commit_view_modal_key,
)
from features.commit_view.session import open_commit_view_modal
from .help import (
    draw_help_screen, handle_help_screen_key, open_help_screen,
)
from .ssh_keygen import (
    draw_ssh_keygen_modal, handle_ssh_keygen_modal_key,
)
from features.ssh_keygen.session import open_ssh_keygen_modal
from .remotes import (
    draw_remotes_modal, handle_remotes_modal_key, open_remotes_modal,
)
from .detached_recovery_prompt import (
    draw_detached_recovery_prompt, handle_detached_recovery_prompt_key,
    open_detached_recovery_prompt,
)
from .diff_viewer import (
    draw_diff_viewer, handle_diff_viewer_key,
)
from features.diff_viewer.projection import any_tab_loading as _any_tab_loading
from features.diff_viewer.session import open_diff_viewer
from .reset_prompt import (
    draw_reset_prompt, handle_reset_prompt_key,
)
from features.reset_prompt.session import open_reset_prompt
from .task_detail import (
    draw_task_action_menu, handle_task_action_menu_key,
)
from features.task_detail.session import open_task_action_menu
from .task_log_viewer import (
    draw_task_log_viewer, handle_task_log_viewer_key,
)
from features.task_log_viewer.session import open_task_log_viewer
from .workflow_picker import (
    draw_workflow_picker, handle_workflow_picker_key,
)
from features.workflow_picker.session import open_workflow_picker
from .workspace_creator import (
    draw_workspace_creator, handle_workspace_creator_key,
)
from features.workspace_creator.actions import commit_workspace_creator
from features.workspace_creator.session import (
    open_workspace_creator,
    tick_creator_checks,
)
from .workspace_menu import (
    draw_workspace_menu, handle_workspace_menu_key,
)
from features.workspace_menu.session import (
    open_workspace_menu,
    tick_menu_path_checks,
)
from .workspace_switcher import (
    draw_workspace_switcher, handle_workspace_switcher_key,
)
from features.workspace_switcher.session import open_workspace_switcher
from .app_menu import (
    draw_app_menu, handle_app_menu_key, open_app_menu,
    tick_app_menu_update_check,
)

__all__ = [
    "draw_action_menu", "handle_action_menu_key", "open_action_menu",
    "draw_align_heads_prompt", "handle_align_heads_prompt_key",
    "open_align_heads_prompt",
    "draw_branch_name_prompt", "handle_branch_name_prompt_key",
    "open_branch_name_prompt",
    "draw_branch_picker", "handle_branch_picker_key", "open_branch_picker",
    "draw_remote_branch_picker", "handle_remote_branch_picker_key",
    "open_remote_branch_picker",
    "draw_clone_modal", "handle_clone_modal_key", "open_clone_modal",
    "draw_commit_msg_editor", "handle_commit_msg_editor_key",
    "open_commit_msg_editor",
    "draw_commit_view_modal", "handle_commit_view_modal_key",
    "open_commit_view_modal",
    "draw_help_screen", "handle_help_screen_key", "open_help_screen",
    "draw_ssh_keygen_modal", "handle_ssh_keygen_modal_key",
    "open_ssh_keygen_modal",
    "draw_remotes_modal", "handle_remotes_modal_key", "open_remotes_modal",
    "draw_detached_recovery_prompt", "handle_detached_recovery_prompt_key",
    "open_detached_recovery_prompt",
    "_any_tab_loading", "draw_diff_viewer", "handle_diff_viewer_key",
    "open_diff_viewer",
    "draw_reset_prompt", "handle_reset_prompt_key", "open_reset_prompt",
    "draw_task_action_menu", "handle_task_action_menu_key",
    "open_task_action_menu",
    "draw_task_log_viewer", "handle_task_log_viewer_key",
    "open_task_log_viewer",
    "draw_workflow_picker", "handle_workflow_picker_key",
    "open_workflow_picker",
    "commit_workspace_creator", "draw_workspace_creator",
    "handle_workspace_creator_key", "open_workspace_creator",
    "tick_creator_checks",
    "draw_workspace_menu", "handle_workspace_menu_key",
    "open_workspace_menu", "tick_menu_path_checks",
    "draw_app_menu", "handle_app_menu_key",
    "open_app_menu", "tick_app_menu_update_check",
    "draw_workspace_switcher", "handle_workspace_switcher_key",
    "open_workspace_switcher",
]
