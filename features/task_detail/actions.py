"""Task-detail key handling and worker dispatch."""
from __future__ import annotations

import curses
import webbrowser
from dataclasses import dataclass

from core.git_ops import cancel_run
from core.runtime.jobs import JobSpec, JobStatus, submit_job
from core.runtime.task_actions import task_action_projection
from core.runtime.threads import create_job_thread
from core.state.app import State
from core.runtime.tasks import Task

from .projection import dispatchable_targets, is_safe_browser_url


@dataclass(frozen=True)
class TaskDetailEffect:
    kind: str = "none"
    task: Task | None = None


NO_EFFECT = TaskDetailEffect()


def open_task_log_effect(task: Task) -> TaskDetailEffect:
    return TaskDetailEffect("open_task_log", task)


def handle_task_action_menu_key(state: State, key: int) -> TaskDetailEffect:
    menu = state.task_action_menu
    if menu is None:
        return NO_EFFECT

    if menu.sub_picker_open:
        handle_sub_picker_key(state, key)
        return NO_EFFECT

    if key in (27, 9):
        state.task_action_menu = None
        return NO_EFFECT
    if key == curses.KEY_UP and menu.items:
        menu.selected = (menu.selected - 1) % len(menu.items)
        return NO_EFFECT
    if key == curses.KEY_DOWN and menu.items:
        menu.selected = (menu.selected + 1) % len(menu.items)
        return NO_EFFECT
    if key in (10, 13, curses.KEY_ENTER) and menu.items:
        item = menu.items[menu.selected]
        if item.enabled:
            return dispatch_action(state, item.id)
    return NO_EFFECT


def dispatch_action(state: State, item_id: str) -> TaskDetailEffect:
    menu = state.task_action_menu
    if menu is None:
        return NO_EFFECT
    task = menu.task
    projection = task_action_projection(state, task)
    followup = projection.followup_record
    run_record = projection.run_record

    if item_id == "close":
        state.task_action_menu = None
        return NO_EFFECT

    if item_id == "remove":
        if projection.can_remove:
            state.tasks.remove(task)
        state.task_action_menu = None
        return NO_EFFECT

    if item_id == "open_in_browser" and run_record is not None and run_record.run_url:
        if is_safe_browser_url(run_record.run_url):
            open_in_browser(state, run_record.run_url)
        else:
            warning = state.tasks.add("open run URL")
            state.tasks.update(warning, "warn", "unsafe URL")
        return NO_EFFECT

    if item_id == "view_log":
        return open_task_log_effect(task)

    if item_id == "cancel_run" and run_record is not None and run_record.run_id:
        cancel_workflow_run(state, task)
        state.task_action_menu = None
        return NO_EFFECT

    if item_id == "cancel_pipeline":
        cancel_job = projection.job
        if cancel_job is not None:
            state.job_registry.request_cancel(cancel_job)
        state.task_action_menu = None
        return NO_EFFECT

    if item_id == "change_then_run":
        repo = followup.repo if followup else (
            run_record.repo if run_record else None)
        options = dispatchable_targets(repo)
        if not options:
            return NO_EFFECT
        menu.sub_picker_options = options
        menu.sub_picker_selected = 0
        menu.sub_picker_open = True
        return NO_EFFECT

    if item_id == "clear_then_run":
        clear_then_run(state)
        state.task_action_menu = None
        return NO_EFFECT

    return NO_EFFECT


def cancel_workflow_run(state: State, task: Task) -> None:
    run_record = state.workflow_runs.record_for_task(task)
    if run_record is None or run_record.run_id is None:
        return
    slug = run_record.slug or ""
    run_id = run_record.run_id
    repo_label = state.task_repo_label(run_record.repo) if run_record.repo else "?"
    workflow = run_record.workflow_name or "?"

    def cancel_worker(job) -> None:
        task_row = state.tasks.add(f"⊘ {repo_label}: cancel {workflow}")
        ok, message = cancel_run(slug, run_id)
        state.tasks.update(task_row, "ok" if ok else "fail", message)
        if not ok:
            state.job_registry.finish(job, JobStatus.FAIL, message)

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="workflow-cancel",
            label=f"cancel {workflow}",
            local_mutation=False,
        ),
        cancel_worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        task_row = state.tasks.add(f"⊘ {repo_label}: cancel {workflow}")
        state.tasks.update(task_row, "fail", job.message)


def handle_sub_picker_key(state: State, key: int) -> None:
    menu = state.task_action_menu
    if menu is None:
        return
    if key == 27:
        menu.sub_picker_open = False
        return
    if not menu.sub_picker_options:
        menu.sub_picker_open = False
        return
    if key == curses.KEY_UP:
        menu.sub_picker_selected = max(0, menu.sub_picker_selected - 1)
        return
    if key == curses.KEY_DOWN:
        menu.sub_picker_selected = min(
            len(menu.sub_picker_options) - 1, menu.sub_picker_selected + 1)
        return
    if key in (10, 13, curses.KEY_ENTER):
        chosen = menu.sub_picker_options[menu.sub_picker_selected]
        set_then_run(state, chosen)
        menu.sub_picker_open = False
        state.task_action_menu = None


def pending_child_ref(state: State) -> Task | None:
    menu = state.task_action_menu
    if menu is None:
        return None
    return menu.pending_child


def set_then_run(state: State, target: str) -> None:
    placeholder = pending_child_ref(state)
    if placeholder is None:
        return
    followup = state.workflow_followups.record_for_task(placeholder)
    if followup is None or followup.repo is None:
        return
    parent_workflow = followup.parent_workflow
    state.workflow_followups.update(followup.record_id, target=target)
    state.tasks.set_label(placeholder, f"  ↪ then run: {target}")
    state.tasks.update(
        placeholder, "pending",
        f"waiting on {parent_workflow}" if parent_workflow else "")


def clear_then_run(state: State) -> None:
    placeholder = pending_child_ref(state)
    if placeholder is None:
        return
    followup = state.workflow_followups.record_for_task(placeholder)
    if followup is None or followup.repo is None:
        return
    state.workflow_followups.update(followup.record_id, target="")
    state.tasks.update(placeholder, "warn", "cleared by user")


def open_in_browser(state: State, url: str) -> None:
    def worker(job) -> None:
        task_row = state.tasks.add(f"open {url}"[:60])
        try:
            opened = webbrowser.open(url, new=2)
        except Exception as exc:
            state.tasks.update(task_row, "warn", str(exc))
            state.job_registry.finish(job, JobStatus.WARN, str(exc))
            return
        if opened:
            state.tasks.update(task_row, "ok", "opened")
        else:
            state.tasks.update(task_row, "warn", "no browser available")
            state.job_registry.finish(
                job, JobStatus.WARN, "no browser available")

    def thread_factory(target, thread_name):
        return create_job_thread(target, thread_name)

    job, thread = submit_job(
        state.job_registry,
        JobSpec(
            kind="open-browser",
            label=f"open {url}"[:60],
            local_mutation=False,
        ),
        worker,
        thread_factory=thread_factory,
    )
    if thread is None:
        task_row = state.tasks.add(f"open {url}"[:60])
        state.tasks.update(task_row, "fail", job.message)
