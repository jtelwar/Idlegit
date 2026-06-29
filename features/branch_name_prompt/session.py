"""Branch-name prompt session lifecycle."""
from __future__ import annotations

from core.git_ops import git
from core.runtime.jobs import JobSpec, submit_job
from core.state.app import State
from core.state.prompts import BranchNamePrompt


def open_branch_name_prompt(state: State, mode: str = "save_head") -> None:
    menu = state.action_menu
    if menu is None:
        return

    prompt = BranchNamePrompt(
        target_label=menu.target_label,
        target_path=menu.target_path,
        target_repo=menu.target_repo,
        target_parent=menu.target_parent,
        target_child=menu.target_child,
        default_name=("wip-head" if mode != "rename" else menu.branch),
        head_sha="",
        mode=("rename" if mode == "rename" else "save_head"),
        current_branch=(menu.branch if mode == "rename" else ""),
    )
    if prompt.mode == "rename":
        prompt.typed = prompt.current_branch
    state.branch_name_prompt = prompt
    kick_off_branch_name_prompt_prepare(state, prompt, initial_typed=prompt.typed)


def close_branch_name_prompt(state: State) -> None:
    state.branch_name_prompt = None


def kick_off_branch_name_prompt_prepare(
        state: State,
        prompt: BranchNamePrompt,
        *,
        initial_typed: str = "",
) -> bool:
    path = prompt.target_path

    def worker(_job) -> None:
        rc, head_out, _ = git(path, ["rev-parse", "HEAD"])
        sha = head_out.strip() if rc == 0 else ""
        rc, branch_out, _ = git(path, ["branch", "--show-current"])
        current = branch_out.strip() if rc == 0 else ""
        if state.branch_name_prompt is not prompt:
            return
        prompt.head_sha = sha
        prompt.current_branch = current
        if prompt.mode == "rename":
            prompt.default_name = current
            if prompt.typed == initial_typed:
                prompt.typed = current
            return
        sha8 = sha[:8] if sha else "head"
        prompt.default_name = f"wip-{sha8}"

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="branch-name-prompt-prepare",
            label=f"{prompt.target_label}: prepare branch prompt",
            local_mutation=False,
        ),
        worker,
    )
    return thread is not None and not job.terminal
