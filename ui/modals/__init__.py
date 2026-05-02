"""Modal dialogs. Each modal lives in its own self-contained module:
the dataclass type comes from `models`, but the open/draw/handle trio
that mediates between curses input and that dataclass is colocated
here. New modals should follow the same shape — copy any of these as
a template."""
from __future__ import annotations

from .action_menu import (
    draw_action_menu, handle_action_menu_key, open_action_menu,
)
from .align_heads_prompt import (
    draw_align_heads_prompt, handle_align_heads_prompt_key,
    open_align_heads_prompt,
)
from .branch_picker import (
    draw_branch_picker, handle_branch_picker_key, open_branch_picker,
)
from .reset_prompt import (
    draw_reset_prompt, handle_reset_prompt_key, open_reset_prompt,
)
from .task_detail import (
    draw_task_action_menu, handle_task_action_menu_key,
    open_task_action_menu,
)
from .workflow_picker import (
    draw_workflow_picker, handle_workflow_picker_key, open_workflow_picker,
)
from .workspace_creator import (
    commit_workspace_creator, draw_workspace_creator,
    handle_workspace_creator_key, open_workspace_creator,
    tick_creator_checks,
)
from .workspace_menu import (
    draw_workspace_menu, handle_workspace_menu_key, open_workspace_menu,
    tick_menu_path_checks,
)
from .workspaces_picker import (
    draw_workspaces_picker, handle_workspaces_picker_key,
    open_workspaces_picker,
)

__all__ = [
    "draw_action_menu", "handle_action_menu_key", "open_action_menu",
    "draw_align_heads_prompt", "handle_align_heads_prompt_key",
    "open_align_heads_prompt",
    "draw_branch_picker", "handle_branch_picker_key", "open_branch_picker",
    "draw_reset_prompt", "handle_reset_prompt_key", "open_reset_prompt",
    "draw_task_action_menu", "handle_task_action_menu_key",
    "open_task_action_menu",
    "draw_workflow_picker", "handle_workflow_picker_key",
    "open_workflow_picker",
    "commit_workspace_creator", "draw_workspace_creator",
    "handle_workspace_creator_key", "open_workspace_creator",
    "tick_creator_checks",
    "draw_workspace_menu", "handle_workspace_menu_key",
    "open_workspace_menu", "tick_menu_path_checks",
    "draw_workspaces_picker", "handle_workspaces_picker_key",
    "open_workspaces_picker",
]
