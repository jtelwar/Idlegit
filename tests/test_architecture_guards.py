"""Static architecture guards for the foundation rewrite."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_PATHS = [
    ROOT / "core",
    ROOT / "ui",
    ROOT / "idlegit.py",
]
STATE_WORKSPACE_PRODUCTION_PATHS = [
    ROOT / "core",
    ROOT / "features",
    ROOT / "ui",
    ROOT / "idlegit.py",
]


def _python_files() -> list[Path]:
    files: list[Path] = []
    for path in PRODUCTION_PATHS:
        if path.is_file():
            files.append(path)
            continue
        files.extend(sorted(path.rglob("*.py")))
    return files


def _python_files_in(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            files.append(path)
            continue
        files.extend(sorted(path.rglob("*.py")))
    return files


def _call_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


def test_runtime_compatibility_shells_do_not_define_control_plane_types() -> None:
    guarded = [
        ROOT / "core" / "leases.py",
        ROOT / "core" / "jobs.py",
        ROOT / "core" / "thread_group.py",
        ROOT / "core" / "state" / "leases.py",
        ROOT / "core" / "state" / "tasks.py",
    ]
    offenders: list[str] = []
    for path in guarded:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                rel = path.relative_to(ROOT)
                offenders.append(f"{rel}:{node.lineno} {node.name}")
    assert offenders == []


def test_phase_2a_migrated_features_import_runtime_jobs_directly() -> None:
    guarded = [
        ROOT / "features" / "action_menu" / "loaders.py",
        ROOT / "features" / "branch_name_prompt" / "session.py",
        ROOT / "features" / "ssh_keygen" / "actions.py",
        ROOT / "features" / "task_detail" / "actions.py",
    ]
    offenders: list[str] = []
    for path in guarded:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "core.jobs":
                rel = path.relative_to(ROOT)
                offenders.append(f"{rel}:{node.lineno} core.jobs")
    assert offenders == []


def test_production_imports_jobs_from_runtime_package() -> None:
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        rel = path.relative_to(ROOT)
        if rel == Path("core/jobs.py"):
            continue
        if rel.parts[:2] == ("core", "runtime"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "core.jobs":
                offenders.append(f"{rel}:{node.lineno} core.jobs")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "jobs"
                and rel.parts[0] == "core"
            ):
                offenders.append(f"{rel}:{node.lineno} .jobs")
    assert offenders == []


def test_production_imports_claims_from_runtime_package() -> None:
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        rel = path.relative_to(ROOT)
        if rel == Path("core/leases.py"):
            continue
        if rel == Path("core/runtime/__init__.py"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "core.leases":
                offenders.append(f"{rel}:{node.lineno} core.leases")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "leases"
                and rel.parts[0] == "core"
            ):
                offenders.append(f"{rel}:{node.lineno} .leases")
    assert offenders == []


def test_production_imports_thread_helpers_from_runtime_package() -> None:
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        rel = path.relative_to(ROOT)
        if rel == Path("core/thread_group.py"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "core.thread_group"
            ):
                offenders.append(f"{rel}:{node.lineno} core.thread_group")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "thread_group"
                and rel.parts[0] == "core"
            ):
                offenders.append(f"{rel}:{node.lineno} .thread_group")
    assert offenders == []


def test_production_imports_task_projection_from_runtime_package() -> None:
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        rel = path.relative_to(ROOT)
        if rel == Path("core/state/tasks.py"):
            continue
        if rel.parts[:2] == ("core", "runtime"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "core.state.tasks"
            ):
                offenders.append(f"{rel}:{node.lineno} core.state.tasks")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module in {"state.tasks", "tasks"}
                and rel.parts[0] == "core"
            ):
                offenders.append(f"{rel}:{node.lineno} {node.module}")
    assert offenders == []


def test_migrated_job_paths_do_not_construct_threads_directly() -> None:
    guarded = [
        ROOT / "core" / "workers.py",
        ROOT / "features" / "action_menu" / "loaders.py",
        ROOT / "features" / "ssh_keygen" / "actions.py",
        ROOT / "features" / "task_detail" / "actions.py",
    ]
    offenders: list[str] = []
    for path in guarded:
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "Thread"
                and ast.unparse(func.value) == "threading"
            ):
                offenders.append(f"{rel}:{node.lineno} threading.Thread")
    assert offenders == []


def test_production_constructs_threads_only_in_runtime_threads() -> None:
    owner = Path("core/runtime/threads.py")
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        rel = path.relative_to(ROOT)
        if rel == owner:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "Thread"
                and ast.unparse(func.value) == "threading"
            ):
                offenders.append(f"{rel}:{node.lineno} threading.Thread")
    assert offenders == []


def test_production_does_not_call_removed_task_control_plane_apis() -> None:
    removed_names = {
        "repo_" + "has_active_job",
        "child_" + "has_active_job",
        "has_local_" + "mutation_jobs",
        "has_local_" + "mutation_for",
        "add_" + "mutation_claim",
        "release_" + "mutation_claim",
        "mutation_" + "claims_snapshot",
        "has_" + "mutation_claims",
        "has_" + "mutation_claim_for",
    }
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        found = sorted(_call_names(tree) & removed_names)
        if found:
            rel = path.relative_to(ROOT)
            offenders.append(f"{rel}: {', '.join(found)}")
    assert offenders == []


def test_job_helpers_have_no_task_metadata_callback_argument() -> None:
    callback_name = "task_" + "fallback"
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(arg.arg == callback_name for arg in node.args.args):
                    rel = path.relative_to(ROOT)
                    offenders.append(f"{rel}:{node.lineno} {node.name}")
            elif isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == callback_name:
                        rel = path.relative_to(ROOT)
                        offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []


def test_production_has_no_raw_refreshing_state() -> None:
    offenders: list[str] = []
    for path in _python_files():
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "refreshing":
                offenders.append(f"{rel}:{node.lineno} .refreshing")
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    targets: list[ast.expr] = []
                    if isinstance(item, ast.Assign):
                        targets.extend(item.targets)
                    elif isinstance(item, ast.AnnAssign):
                        targets.append(item.target)
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id == "refreshing":
                            offenders.append(
                                f"{rel}:{item.lineno} {node.name}.refreshing")
    assert offenders == []


def test_refreshing_flags_are_not_written_directly() -> None:
    offenders: list[str] = []
    for path in _python_files():
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets.extend(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets.append(node.target)
            elif isinstance(node, ast.AugAssign):
                targets.append(node.target)
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "refreshing":
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []


def test_row_selectors_do_not_read_raw_repo_lifecycle_fields() -> None:
    path = ROOT / "core" / "state" / "selectors.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_attrs = {
        "is_dirty",
        "dirty",
        "message",
        "error",
        "merging",
        "kind",
        "branch",
        "ahead",
        "behind",
        "upstream",
    }
    allowed_names = {"status"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr not in forbidden_attrs:
            continue
        if isinstance(node.value, ast.Name) and node.value.id in allowed_names:
            continue
        offenders.append(f"selectors.py:{node.lineno} .{node.attr}")
    assert offenders == []


def test_read_only_row_busy_selector_uses_store_membership() -> None:
    path = ROOT / "core" / "state" / "selectors.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "read_only_row_busy_active":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Attribute):
                continue
            source = ast.unparse(child)
            if source in {"state.repos", "repo.children"}:
                offenders.append(
                    f"selectors.py:{child.lineno} {node.name} {source}")
    assert offenders == []


def test_local_mutation_gates_do_not_collect_raw_repo_children() -> None:
    guarded = {
        ROOT / "core" / "workers.py": {"_workspace_has_local_mutation"},
        ROOT / "core" / "fs_watcher.py": {"on_event", "_on_timer"},
    }
    offenders: list[str] = []
    for path, function_names in guarded.items():
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in function_names:
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Attribute):
                    continue
                if child.attr != "children":
                    continue
                offenders.append(
                    f"{rel}:{child.lineno} {node.name} {ast.unparse(child)}")
    assert offenders == []


def test_row_state_helpers_use_store_membership_for_tree_rows() -> None:
    path = ROOT / "core" / "state" / "row_state.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "set_canonical_tree_refreshing":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Attribute):
                continue
            if child.attr in {"children", "siblings"}:
                offenders.append(
                    f"row_state.py:{child.lineno} {node.name} {ast.unparse(child)}")
    assert offenders == []


def test_main_screen_does_not_render_from_raw_repo_lifecycle_fields() -> None:
    path = ROOT / "ui" / "main_screen.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_attrs = {
        "is_dirty",
        "dirty",
        "message",
        "error",
        "merging",
        "kind",
        "branch",
        "ahead",
        "behind",
        "upstream",
        "in_sync",
    }
    allowed_names = {"status", "child_status", "row_state"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr not in forbidden_attrs:
            continue
        if isinstance(node.value, ast.Name) and node.value.id in allowed_names:
            continue
        if isinstance(node.value, ast.Name) and node.value.id == "curses":
            continue
        offenders.append(f"main_screen.py:{node.lineno} .{node.attr}")
    assert offenders == []


def test_main_screen_layout_uses_store_row_selectors() -> None:
    path = ROOT / "ui" / "main_screen.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    guarded_defs = {"_column_widths", "draw_main"}
    forbidden_sources = {"state.repos", "parent.children", "repo.children"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in guarded_defs:
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Attribute):
                continue
            source = ast.unparse(child)
            if source in forbidden_sources:
                offenders.append(
                    f"main_screen.py:{child.lineno} {node.name} {source}")
    assert offenders == []


def test_state_selection_and_message_properties_delegate_to_selectors() -> None:
    path = ROOT / "core" / "models.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    guarded_defs = {
        "selectable_rows",
        "total_rows",
        "current_repo",
        "current_child",
        "has_messages",
    }
    forbidden_attrs = {"children", "message", "is_dirty", "dirty"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in guarded_defs:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Attribute) and child.attr in forbidden_attrs:
                offenders.append(
                    f"models.py:{child.lineno} {node.name} .{child.attr}")
    assert offenders == []


def test_main_loop_does_not_read_raw_message_fields() -> None:
    path = ROOT / "ui" / "main_loop.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != "message":
            continue
        if isinstance(node.ctx, ast.Store):
            continue
        source = ast.unparse(node.value)
        if source.startswith("repo_row_state("):
            continue
        if source.startswith("child_row_state("):
            continue
        offenders.append(f"main_loop.py:{node.lineno} .message")
    assert offenders == []


def test_app_loop_row_activity_uses_store_row_selectors() -> None:
    path = ROOT / "idlegit.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "_row_activity_active":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Attribute):
                continue
            source = ast.unparse(child)
            if source in {"state.repos", "repo.children"}:
                offenders.append(
                    f"idlegit.py:{child.lineno} {node.name} {source}")
    assert offenders == []


def test_worker_and_review_message_paths_use_store_accessors() -> None:
    guarded = {
        ROOT / "core" / "workers.py": {
            "_suggest_into_repo",
            "_suggest_into_child",
            "kick_off_bulk_suggest",
            "kick_off_review_suggest",
            "kick_off_workers",
            "review_detached_targets",
        },
        ROOT / "ui" / "review.py": {
            "_block_for_repo",
            "_block_for_child",
            "build_review_blocks",
        },
    }
    forbidden_sources = {
        "repo",
        "child",
        "ref",
        "block.target_repo",
        "block.target_child",
    }
    offenders: list[str] = []
    for path, function_names in guarded.items():
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in function_names:
                continue
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Attribute)
                    and child.attr == "message"
                    and ast.unparse(child.value) in forbidden_sources
                ):
                    offenders.append(
                        f"{rel}:{child.lineno} {node.name} .message")
    assert offenders == []


def test_production_does_not_use_raw_repo_workflow_intent() -> None:
    owner_paths = {
        Path("core/state/store.py"),
        Path("core/state/review_drafts.py"),
    }
    forbidden_attrs = {
        "track_workflow",
        "then_run_after_push",
        "then_run_after_workflow",
        "then_run_params_after_push",
        "then_run_params_after_workflow",
    }
    allowed_sources = {"draft", "intent", "fallback_intent"}
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        rel = path.relative_to(ROOT)
        if rel in owner_paths:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr not in forbidden_attrs:
                continue
            source = ast.unparse(node.value)
            if source in allowed_sources:
                continue
            offenders.append(f"{rel}:{node.lineno} {source}.{node.attr}")
    assert offenders == []


def test_live_worker_reconcile_paths_do_not_inject_raw_refresh_repo() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    allowed_functions = {"kick_off_startup_refresh"}
    allowed_ranges = [
        (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in allowed_functions
    ]
    offenders: list[str] = []

    def in_allowed_range(lineno: int) -> bool:
        return any(start <= lineno <= end for start, end in allowed_ranges)

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            if in_allowed_range(child.lineno):
                continue
            for keyword in child.keywords:
                if keyword.arg != "refresh_fn":
                    continue
                value = keyword.value
                if isinstance(value, ast.Name) and value.id == "refresh_repo":
                    offenders.append(f"workers.py:{child.lineno} {node.name}")
    assert offenders == []


def test_migrated_worker_and_review_paths_use_store_row_selectors() -> None:
    guarded = {
        ROOT / "core" / "workers.py": {
            "kick_off_bulk_suggest",
            "kick_off_workers",
            "review_detached_targets",
            "_refresh_detached_review_target",
        },
        ROOT / "ui" / "review.py": {
            "build_review_blocks",
        },
    }
    forbidden = {
        "state.repos",
        "repo.children",
        "parent.children",
    }
    offenders: list[str] = []
    for path, function_names in guarded.items():
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in function_names:
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Attribute):
                    continue
                if ast.unparse(child) in forbidden:
                    offenders.append(
                        f"{rel}:{child.lineno} {node.name} {ast.unparse(child)}")
    assert offenders == []


def test_production_does_not_call_row_owned_refresh_locks() -> None:
    direct_lock_calls = {
        "try_acquire_refresh",
        "acquire_refresh",
        "release_refresh",
    }
    offenders: list[str] = []
    for path in _python_files():
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in direct_lock_calls:
                offenders.append(f"{rel}:{node.lineno} {func.attr}")
    assert offenders == []


def test_repo_projection_models_do_not_own_refresh_locks() -> None:
    path = ROOT / "core" / "state" / "repos.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_names = {
        "refresh_lock",
        "try_acquire_refresh",
        "acquire_refresh",
        "release_refresh",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in forbidden_names:
            offenders.append(f"core/state/repos.py:{node.lineno} {node.name}")
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in forbidden_names:
                offenders.append(
                    f"core/state/repos.py:{node.lineno} {node.target.id}")
    assert offenders == []


def test_repo_projection_models_do_not_own_workflow_intent() -> None:
    path = ROOT / "core" / "state" / "repos.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_names = {
        "track_workflow",
        "then_run_after_push",
        "then_run_after_workflow",
        "then_run_params_after_push",
        "then_run_params_after_workflow",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in forbidden_names:
                offenders.append(
                    f"core/state/repos.py:{node.lineno} {node.target.id}")
    assert offenders == []


def test_app_menu_does_not_own_thread_creation() -> None:
    path = ROOT / "ui" / "modals" / "app_menu.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "threading":
                    offenders.append(f"app_menu.py:{node.lineno} import threading")
        elif isinstance(node, ast.ImportFrom) and node.module == "threading":
            offenders.append(f"app_menu.py:{node.lineno} from threading import")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "Thread":
                offenders.append(f"app_menu.py:{node.lineno} threading.Thread")
            elif isinstance(func, ast.Name) and func.id == "Thread":
                offenders.append(f"app_menu.py:{node.lineno} Thread")
    assert offenders == []


def test_app_menu_does_not_own_job_or_persistence_primitives() -> None:
    path = ROOT / "ui" / "modals" / "app_menu.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {
        "core.jobs",
        "core.task_log",
        "core.fs_watcher",
        "core.ssh",
    }
    forbidden_calls = {
        "submit_job",
        "start_job_thread",
        "set_conf_value",
        "task_log_size_bytes",
        "task_log_line_count",
        "clear_task_log",
        "open_task_log",
        "reconcile_repo_watchers",
        "ensure_ssh_agent",
        "add_default_keys_to_agent",
        "ssh_tools_status",
        "agent_status_label",
        "keys_loaded_label",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(f"app_menu.py:{node.lineno} from {node.module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    offenders.append(f"app_menu.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                offenders.append(f"app_menu.py:{node.lineno} {func.attr}")
            elif isinstance(func, ast.Name) and func.id in forbidden_calls:
                offenders.append(f"app_menu.py:{node.lineno} {func.id}")
    assert offenders == []


def test_app_menu_ui_does_not_own_projection_or_status_session() -> None:
    path = ROOT / "ui" / "modals" / "app_menu.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_defs = {
        "app_section_rows",
        "auto_refresh_section_rows",
        "tasks_section_rows",
        "ssh_section_rows",
        "task_logging_section_rows",
        "workspaces_section_rows",
        "build_app_menu_rows",
        "rebuild_app_menu_rows",
        "tick_app_menu_update_check",
        "app_menu_status_needs_refresh",
    }
    forbidden_imports = {
        "AppMenuRow",
        "kick_off_app_menu_status_refresh",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(f"app_menu.py:{node.lineno} def {node.name}")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(f"app_menu.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "kick_off_app_menu_status_refresh":
                offenders.append(f"app_menu.py:{node.lineno} kick_off_app_menu_status_refresh")
            elif (isinstance(func, ast.Attribute)
                  and func.attr == "kick_off_app_menu_status_refresh"):
                offenders.append(f"app_menu.py:{node.lineno} kick_off_app_menu_status_refresh")
    assert offenders == []


def test_app_menu_ui_does_not_own_action_dispatch_or_key_behavior() -> None:
    path = ROOT / "ui" / "modals" / "app_menu.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_defs = {
        "_fire_app_action",
        "_fire_toggle_task_log",
        "_fire_create_ssh_key",
        "_fire_toggle_ssh_agent",
        "_fire_ssh_add_keys",
        "_fire_toggle_auto_refresh",
        "_fire_cycle_debounce",
        "_fire_toggle_periodic_refresh",
        "_fire_step_periodic_refresh",
        "_fire_cycle_auto_remove_completed",
        "_fire_open_task_log",
        "_fire_clear_task_log",
        "_move_selected",
    }
    forbidden_imports = {
        "kick_off_check_for_updates",
        "kick_off_open_task_log",
        "kick_off_clear_task_log",
        "kick_off_task_log_toggle",
        "kick_off_auto_refresh_toggle",
        "kick_off_auto_refresh_debounce_save",
        "kick_off_periodic_refresh_save",
        "kick_off_auto_remove_completed_save",
        "kick_off_ssh_agent_toggle",
        "kick_off_ssh_add_keys",
        "switch_workspace",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(f"app_menu.py:{node.lineno} def {node.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "core.workers":
                offenders.append(f"app_menu.py:{node.lineno} from core.workers")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(f"app_menu.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_imports:
                offenders.append(f"app_menu.py:{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_imports:
                offenders.append(f"app_menu.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_ssh_keygen_session_and_draw_do_not_prefill_synchronously() -> None:
    targets = [
        ROOT / "features" / "ssh_keygen" / "session.py",
        ROOT / "ui" / "modals" / "ssh_keygen.py",
    ]
    forbidden_calls = {"git_user_email", "default_ed25519_path"}
    offenders: list[str] = []
    for path in targets:
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_calls:
                offenders.append(f"{rel}:{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                offenders.append(f"{rel}:{node.lineno} {func.attr}")
    assert offenders == []


def test_ssh_keygen_ui_does_not_own_session_jobs_or_key_behavior() -> None:
    path = ROOT / "ui" / "modals" / "ssh_keygen.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {
        "threading",
        "core.jobs",
        "core.workers",
        "core.ssh",
    }
    forbidden_defs = {
        "open_ssh_keygen_modal",
        "_generate_blocked_reason",
        "_kick_off_generate",
        "_request_generate",
        "_cancel_empty_passphrase_confirm",
        "_handle_empty_passphrase_confirm",
    }
    forbidden_imports = {
        "JobSpec",
        "submit_job",
        "kick_off_ssh_keygen_prepare",
        "ensure_ssh_agent",
        "create_ed25519_keypair",
        "read_public_key",
        "key_path_conflict_message",
        "github_new_key_url",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(f"ssh_keygen.py:{node.lineno} def {node.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    offenders.append(f"ssh_keygen.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(f"ssh_keygen.py:{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        f"ssh_keygen.py:{node.lineno} import {alias.name}")
    assert offenders == []


def test_workspace_path_validation_jobs_are_not_owned_by_ui_modules() -> None:
    targets = [
        ROOT / "ui" / "modals" / "workspace_creator.py",
        ROOT / "ui" / "modals" / "workspace_menu.py",
    ]
    forbidden_modules = {"threading", "core.jobs", "core.git_ops", "core.workers"}
    forbidden_calls = {
        "Thread",
        "submit_job",
        "start_job_thread",
        "discover_repos",
    }
    offenders: list[str] = []
    for path in targets:
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden_modules:
                        offenders.append(f"{rel}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module in forbidden_modules:
                    offenders.append(f"{rel}:{node.lineno} from {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in forbidden_calls:
                    offenders.append(f"{rel}:{node.lineno} {func.id}")
                elif isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                    offenders.append(f"{rel}:{node.lineno} {func.attr}")
    assert offenders == []


def test_workspace_creator_ui_does_not_own_session_or_key_behavior() -> None:
    path = ROOT / "ui" / "modals" / "workspace_creator.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_defs = {
        "open_workspace_creator",
        "close_workspace_creator",
        "commit_workspace_creator",
        "tick_creator_checks",
        "_drafts_to_workspaces",
        "_kick_off_check",
        "_focused_draft",
        "_on_done_row",
        "_ensure_trailing_empty",
        "_move_to_field",
    }
    forbidden_imports = {
        "Workspace",
        "kick_off_workspace_path_check",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        f"workspace_creator.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(f"workspace_creator.py:{node.lineno} def {node.name}")
    assert offenders == []


def test_startup_loading_refresh_is_not_owned_by_ui_module() -> None:
    path = ROOT / "ui" / "loading.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {"threading", "core.jobs", "core.git_ops", "core.reconcile"}
    forbidden_calls = {
        "JobRegistry",
        "JobSpec",
        "Thread",
        "submit_job",
        "refresh_repo",
        "link_siblings",
        "refresh_repos_bounded",
        "reconcile_repos_bounded",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    offenders.append(f"loading.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(f"loading.py:{node.lineno} from {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_calls:
                offenders.append(f"loading.py:{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                offenders.append(f"loading.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_workspace_menu_does_not_own_workspace_persistence_writes() -> None:
    path = ROOT / "ui" / "modals" / "workspace_menu.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.config":
            for alias in node.names:
                if alias.name == "save_workspaces":
                    offenders.append(f"workspace_menu.py:{node.lineno} save_workspaces")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "save_workspaces":
                offenders.append(f"workspace_menu.py:{node.lineno} save_workspaces")
            elif isinstance(func, ast.Attribute) and func.attr == "save_workspaces":
                offenders.append(f"workspace_menu.py:{node.lineno} save_workspaces")
    assert offenders == []


def test_workspace_menu_ui_does_not_own_session_projection_or_key_behavior() -> None:
    path = ROOT / "ui" / "modals" / "workspace_menu.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_defs = {
        "_build_rows",
        "open_workspace_menu",
        "_rebuild_rows",
        "_kick_off_path_check",
        "tick_menu_path_checks",
        "_state_attr_for",
        "_read_value",
        "_write_value",
        "_clear_override",
        "_persist",
        "_save_ephemeral_workspace",
        "_commit_folder_edit",
        "_commit_ignore_pattern_edit",
        "_remove_ignore_pattern",
        "_remove_folder",
        "_enter_edit_mode",
        "_exit_edit_mode",
        "_draft_for_row",
        "_cycle_trunc",
        "_bump_int",
        "_adjust",
        "_toggle_bool",
        "_move_selected",
        "_focused_row",
        "_handle_edit_key",
    }
    forbidden_import_modules = {
        "core.config",
        "core.workers",
    }
    forbidden_import_names = {
        "Workspace",
        "kick_off_workspace_path_check",
        "kick_off_workspace_settings_save",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(f"workspace_menu.py:{node.lineno} def {node.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in forbidden_import_modules:
                offenders.append(f"workspace_menu.py:{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_import_names:
                    offenders.append(
                        f"workspace_menu.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_import_modules:
                    offenders.append(
                        f"workspace_menu.py:{node.lineno} import {alias.name}")
    assert offenders == []


def test_task_detail_ui_does_not_own_worker_git_or_action_behavior() -> None:
    path = ROOT / "ui" / "modals" / "task_detail.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {
        "threading",
        "webbrowser",
        "core.jobs",
        "core.git_ops",
    }
    forbidden_defs = {
        "open_task_action_menu",
        "_dispatchable_targets",
        "_is_terminal",
        "_is_pending_then_run",
        "_dispatch_action",
        "_handle_sub_picker_key",
        "_pending_child_ref",
        "_set_then_run",
        "_clear_then_run",
        "_open_in_browser",
    }
    forbidden_names = {
        "JobSpec",
        "JobStatus",
        "submit_job",
        "cancel_run",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(f"task_detail.py:{node.lineno} def {node.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    offenders.append(f"task_detail.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(f"task_detail.py:{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_names:
                    offenders.append(
                        f"task_detail.py:{node.lineno} import {alias.name}")
    assert offenders == []


def test_task_action_controls_use_runtime_projection_queries() -> None:
    guarded = [
        ROOT / "features" / "task_detail" / "session.py",
        ROOT / "features" / "task_detail" / "actions.py",
        ROOT / "ui" / "main_loop.py",
        ROOT / "ui" / "main_screen.py",
    ]
    forbidden_calls = {"children_of"}
    offenders: list[str] = []
    for path in guarded:
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "status":
                source = ast.unparse(node.value)
                if source in {"task", "t", "menu.task"}:
                    offenders.append(f"{rel}:{node.lineno} {source}.status")
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                offenders.append(f"{rel}:{node.lineno} {func.attr}")
    assert offenders == []


def test_commit_msg_editor_ui_does_not_own_session_or_key_behavior() -> None:
    path = ROOT / "ui" / "modals" / "commit_msg_editor.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_defs = {
        "_split_lines",
        "_cursor_to_row_col",
        "_row_col_to_cursor",
        "_holder_message",
        "_set_holder_message",
        "_focused_holder",
        "_holder_is_editable",
        "_holder_is_busy",
        "open_commit_msg_editor",
        "_wrap_logical_line",
        "_build_display_rows",
        "_cursor_display_position",
        "_wrap_width",
        "_move_display_vertical",
    }
    forbidden_imports = {
        "ChildRef",
        "CommitMsgEditor",
        "Repo",
        "child_row_state",
        "repo_row_state",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(f"commit_msg_editor.py:{node.lineno} def {node.name}")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        f"commit_msg_editor.py:{node.lineno} import {alias.name}")
    assert offenders == []


def test_commit_msg_editor_feature_uses_store_message_accessors() -> None:
    feature_dir = ROOT / "features" / "commit_msg_editor"
    offenders: list[str] = []
    for path in sorted(feature_dir.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = path.relative_to(ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "message":
                offenders.append(f"{rel}:{node.lineno} .message")
    assert offenders == []


def test_entrypoint_does_not_own_workspace_persistence_writes() -> None:
    path = ROOT / "idlegit.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.config":
            for alias in node.names:
                if alias.name == "save_workspaces":
                    offenders.append(f"idlegit.py:{node.lineno} save_workspaces")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "save_workspaces":
                offenders.append(f"idlegit.py:{node.lineno} save_workspaces")
            elif isinstance(func, ast.Attribute) and func.attr == "save_workspaces":
                offenders.append(f"idlegit.py:{node.lineno} save_workspaces")
    assert offenders == []


def test_switch_workspace_persistence_is_worker_owned() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "switch_workspace"
    )
    offenders: list[str] = []
    for node in ast.walk(function):
        if isinstance(node, ast.ImportFrom) and node.module == "core.config":
            for alias in node.names:
                if alias.name == "save_workspaces":
                    offenders.append(f"workers.py:{node.lineno} save_workspaces")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "save_workspaces":
                offenders.append(f"workers.py:{node.lineno} save_workspaces")
            elif isinstance(func, ast.Attribute) and func.attr == "save_workspaces":
                offenders.append(f"workers.py:{node.lineno} save_workspaces")
    assert offenders == []


def test_row_selectors_are_called_with_state() -> None:
    selector_names = {"repo_row_state", "child_row_state"}
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in selector_names:
                if len(node.args) < 2:
                    rel = path.relative_to(ROOT)
                    offenders.append(f"{rel}:{node.lineno} {func.id}")
    assert offenders == []


def test_row_selectors_do_not_read_raw_refreshing_flags() -> None:
    path = ROOT / "core" / "state" / "selectors.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "refreshing":
            offenders.append(f"core/state/selectors.py:{node.lineno}")
    assert offenders == []


def test_workers_do_not_read_raw_refreshing_flags() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "refreshing":
            offenders.append(f"core/workers.py:{node.lineno}")
    assert offenders == []


def test_app_loop_does_not_read_raw_refreshing_flags() -> None:
    path = ROOT / "idlegit.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "refreshing":
            offenders.append(f"idlegit.py:{node.lineno}")
    assert offenders == []


def test_row_state_helpers_are_called_with_state() -> None:
    helper_names = {
        "set_repo_refreshing",
        "set_child_refreshing",
        "set_canonical_tree_refreshing",
    }
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in helper_names:
                if len(node.args) < 3:
                    rel = path.relative_to(ROOT)
                    offenders.append(f"{rel}:{node.lineno} {func.id}")
    assert offenders == []


def test_row_state_helpers_are_owned_only_by_runtime_claims() -> None:
    owner_paths = {
        Path("core/runtime/claims.py"),
        Path("core/state/row_state.py"),
    }
    helper_names = {
        "set_repo_refreshing",
        "set_child_refreshing",
        "set_canonical_tree_refreshing",
    }
    offenders: list[str] = []
    for path in _python_files():
        rel = path.relative_to(ROOT)
        if rel in owner_paths:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in helper_names:
                offenders.append(f"{rel}:{node.lineno} {func.id}")
    assert offenders == []


def test_repo_and_child_rows_do_not_own_suggesting_state() -> None:
    path = ROOT / "core" / "models.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name not in {"Repo", "ChildRef"}:
            continue
        for item in node.body:
            if isinstance(item, ast.AnnAssign):
                target = item.target
                if isinstance(target, ast.Name) and target.id == "suggesting":
                    offenders.append(f"core/models.py:{item.lineno} {node.name}")
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id == "suggesting":
                        offenders.append(
                            f"core/models.py:{item.lineno} {node.name}")
    assert offenders == []


def test_fs_watcher_uses_refresh_claim_for_row_locks() -> None:
    path = ROOT / "core" / "fs_watcher.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    direct_lock_calls = {
        "try_acquire_refresh",
        "acquire_refresh",
        "release_refresh",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in direct_lock_calls:
            offenders.append(f"core/fs_watcher.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_worker_relink_publishes_explicit_topology_snapshot() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    target: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_state_link_siblings":
            target = node
            break
    assert target is not None

    calls = _call_names(target)
    assert "read_link_siblings_snapshot" in calls
    assert "replace_workspace_topology" in calls
    assert "apply_link_siblings_snapshot" in calls
    assert "publish_workspace_statuses" not in calls
    assert "link_siblings" not in calls


def test_fs_watcher_relink_publishes_explicit_topology_snapshot() -> None:
    path = ROOT / "core" / "fs_watcher.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    target: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_publish_link_snapshot":
            target = node
            break
    assert target is not None

    calls = _call_names(target)
    assert "read_link_siblings_snapshot" in calls
    assert "replace_workspace_topology" in calls
    assert "apply_link_siblings_snapshot" in calls
    assert "link_siblings" not in calls


def test_fs_watcher_does_not_read_raw_refreshing_flags() -> None:
    path = ROOT / "core" / "fs_watcher.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "refreshing":
            offenders.append(f"core/fs_watcher.py:{node.lineno}")
    assert offenders == []


def test_git_ops_does_not_read_raw_refreshing_flags() -> None:
    path = ROOT / "core" / "git_ops.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "refreshing":
            offenders.append(f"core/git_ops.py:{node.lineno}")
    assert offenders == []


def test_main_loop_does_not_read_modal_loader_fields_directly() -> None:
    path = ROOT / "idlegit.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden = {
        "state_loading",
        "inventory_loading",
        "tree_loading",
        "commits_loading",
        "tags_loading",
        "details_loading",
        "files_loading",
        "reflog_loading",
        "loading",
        "log_loading",
        "blame_loading",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            offenders.append(f"idlegit.py:{node.lineno} {node.attr}")
    assert offenders == []


def test_relink_busy_predicates_do_not_use_store_methods_directly() -> None:
    offenders: list[str] = []
    for path in _python_files():
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "busy_child_predicate":
                    continue
                value = keyword.value
                if (
                        isinstance(value, ast.Attribute)
                        and value.attr == "child_busy"):
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []


def test_pull_all_uses_refresh_claim_for_row_locks() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    function = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "kick_off_pull_all":
            function = node
            break
    assert function is not None
    direct_lock_calls = {
        "try_acquire_refresh",
        "acquire_refresh",
        "release_refresh",
    }
    offenders: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in direct_lock_calls:
            offenders.append(f"core/workers.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_safe_merge_uses_refresh_claim_for_row_locks() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    target_names = {"kick_off_safe_merge", "_safe_merge_release_locks"}
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in target_names
    ]
    assert {node.name for node in functions} == target_names
    direct_lock_calls = {
        "try_acquire_refresh",
        "acquire_refresh",
        "release_refresh",
    }
    offenders: list[str] = []
    for function in functions:
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in direct_lock_calls:
                offenders.append(
                    f"core/workers.py:{node.lineno} {function.name}.{func.attr}"
                )
    assert offenders == []


def test_action_worker_does_not_read_task_rows_for_job_outcome() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    function = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "kick_off_action":
            function = node
            break
    assert function is not None
    control_names = {"initial_task_ids", "new_tasks"}
    offenders: list[str] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and node.id in control_names:
            offenders.append(f"core/workers.py:{node.lineno} {node.id}")
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "snapshot":
            offenders.append(f"core/workers.py:{node.lineno} snapshot")
    assert offenders == []


def test_commit_batch_does_not_read_task_rows_for_job_outcome() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    function = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "kick_off_workers":
            function = node
            break
    assert function is not None
    control_names = {"finish_batch_from_tasks", "batch_tasks"}
    offenders: list[str] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and node.id in control_names:
            offenders.append(f"core/workers.py:{node.lineno} {node.id}")
            continue
        if isinstance(node, ast.FunctionDef) and node.name in control_names:
            offenders.append(f"core/workers.py:{node.lineno} {node.name}")
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "snapshot":
            offenders.append(f"core/workers.py:{node.lineno} snapshot")
    assert offenders == []


def test_smart_sync_lifecycle_does_not_read_header_task_status_for_job_outcome() -> None:
    path = ROOT / "core" / "smart_sync" / "lifecycle.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr != "status":
            continue
        value = node.value
        if isinstance(value, ast.Attribute) and value.attr == "header":
            offenders.append(f"core/smart_sync/lifecycle.py:{node.lineno} header.status")
    assert offenders == []


def test_safe_merge_jobs_do_not_read_presentation_state_for_job_outcome() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    target_names = {
        "kick_off_safe_merge",
        "kick_off_safe_merge_finalize",
        "kick_off_safe_merge_confirm",
    }
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in target_names
    ]
    assert {node.name for node in functions} == target_names
    offenders: list[str] = []

    def contains_presentation_read(node: ast.AST) -> bool:
        for child in ast.walk(node):
            if not isinstance(child, ast.Attribute):
                continue
            if child.attr in {"status", "message"}:
                if isinstance(child.value, ast.Name) and child.value.id == "header":
                    return True
            if child.attr in {"phase", "status_note"}:
                if isinstance(child.value, ast.Name) and child.value.id == "screen":
                    return True
        return False

    for function in functions:
        for node in ast.walk(function):
            if isinstance(node, ast.If) and contains_presentation_read(node.test):
                offenders.append(
                    f"core/workers.py:{node.lineno} {function.name} presentation if"
                )
                continue
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                    isinstance(func, ast.Attribute)
                    and func.attr == "finish"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "job_registry"
            ):
                continue
            for arg in node.args[1:]:
                if contains_presentation_read(arg):
                    offenders.append(
                        f"core/workers.py:{node.lineno} {function.name} finish arg"
                    )
    assert offenders == []


def test_review_detached_preflight_does_not_run_git_on_ui_thread() -> None:
    path = ROOT / "ui" / "main_loop.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_imports = {"core.git_ops"}
    forbidden_names = {
        "_build_recovery_prompt",
        "execute_detached_recovery",
        "refresh_repo",
        "git",
    }
    forbidden_functions = {
        "_next_detached_review_target",
        "_drive_modal_until_closed",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in forbidden_imports:
            offenders.append(f"ui/main_loop.py:{node.lineno} import {node.module}")
            continue
        if isinstance(node, ast.FunctionDef) and node.name in forbidden_functions:
            offenders.append(f"ui/main_loop.py:{node.lineno} def {node.name}")
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in forbidden_names:
            offenders.append(f"ui/main_loop.py:{node.lineno} call {func.id}")
    assert offenders == []


def test_branch_picker_ui_does_not_own_loader_jobs() -> None:
    path = ROOT / "ui" / "modals" / "branch_picker.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {"threading", "core.jobs", "core.workers", "core.git_ops"}
    forbidden_imports = {
        "git",
        "is_fast_forward_merge",
        "is_safe_ref_arg",
        "kick_off_action",
        "kick_off_branch_picker_load",
        "kick_off_safe_merge",
        "list_branches",
        "list_remote_tracking_refs",
    }
    forbidden_calls = {
        "Thread",
        "submit_job",
        "git",
        "is_fast_forward_merge",
        "is_safe_ref_arg",
        "kick_off_action",
        "kick_off_branch_picker_load",
        "kick_off_safe_merge",
        "list_branches",
        "list_remote_tracking_refs",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    offenders.append(
                        f"ui/modals/branch_picker.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(
                    f"ui/modals/branch_picker.py:{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        f"ui/modals/branch_picker.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_calls:
                offenders.append(
                    f"ui/modals/branch_picker.py:{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                offenders.append(
                    f"ui/modals/branch_picker.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_branch_picker_ui_does_not_own_session_or_key_behavior() -> None:
    path = ROOT / "ui" / "modals" / "branch_picker.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_defs = {
        "open_branch_picker",
        "close_branch_picker",
        "handle_create_row_key",
        "submit_create_branch",
        "submit_selected_branch",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(f"ui/modals/branch_picker.py:{node.lineno} def {node.name}")
    assert offenders == []


def test_branch_picker_does_not_own_async_loader_state() -> None:
    path = ROOT / "core" / "models.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden = {
        "branches",
        "current",
        "loading",
        "cancel_event",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "BranchPicker":
            continue
        for item in node.body:
            if isinstance(item, ast.AnnAssign):
                target = item.target
                if isinstance(target, ast.Name) and target.id in forbidden:
                    offenders.append(f"core/models.py:{item.lineno} {target.id}")
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id in forbidden:
                        offenders.append(
                            f"core/models.py:{item.lineno} {target.id}")
    assert offenders == []


def test_inline_refresh_does_not_read_manual_task_for_job_outcome() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    function = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "kick_off_inline_refresh":
            function = node
            break
    assert function is not None
    offenders: list[str] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Attribute) and node.attr in {"status", "message"}:
            if isinstance(node.value, ast.Name) and node.value.id == "manual_task":
                offenders.append(
                    f"core/workers.py:{node.lineno} manual_task.{node.attr}"
                )
                continue
        if isinstance(node, ast.Subscript):
            value = node.value
            if isinstance(value, ast.Name) and value.id == "refresh_outcome":
                slice_node = node.slice
                if isinstance(slice_node, ast.Constant) and slice_node.value == 1:
                    offenders.append(
                        f"core/workers.py:{node.lineno} refresh_outcome message slot"
                    )
            continue
        if not isinstance(node, ast.Compare):
            continue
        if not isinstance(node.left, ast.Subscript):
            continue
        left_value = node.left.value
        left_slice = node.left.slice
        if not (
                isinstance(left_value, ast.Name)
                and left_value.id == "refresh_outcome"
                and isinstance(left_slice, ast.Constant)
                and left_slice.value == 0
        ):
            continue
        if any(isinstance(comp, ast.Constant) and isinstance(comp.value, str)
               for comp in node.comparators):
            offenders.append(f"core/workers.py:{node.lineno} refresh_outcome string compare")
    assert offenders == []


def test_workflow_tracking_does_not_read_run_task_for_job_outcome() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    function = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "kick_off_workflow_tracking":
            function = node
            break
    assert function is not None
    offenders: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr not in {"status", "message"}:
            continue
        if isinstance(node.value, ast.Name) and node.value.id == "t":
            offenders.append(f"core/workers.py:{node.lineno} t.{node.attr}")
    assert offenders == []


def test_workflow_tracking_rows_use_bound_runtime_projection() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    guarded_functions = {
        "_poll_run",
        "kick_off_workflow_tracking",
        "kick_off_manual_dispatch",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in guarded_functions:
            continue
        saw_bridge = False
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name) and func.id == "JobTaskBridge":
                    saw_bridge = True
                if isinstance(func, ast.Name) and func.id == "submit_job":
                    offenders.append(f"core/workers.py:{child.lineno} submit_job")
                if isinstance(func, ast.Attribute):
                    source = ast.unparse(func.value)
                    if (
                            node.name == "_poll_run"
                            and source == "state.tasks"
                            and func.attr in {
                                "add", "update", "set_label", "clear_message",
                            }
                    ):
                        offenders.append(
                            f"core/workers.py:{child.lineno} state.tasks.{func.attr}")
        if node.name in {"_poll_run", "kick_off_workflow_tracking"} and not saw_bridge:
            offenders.append(f"core/workers.py:{node.lineno} missing JobTaskBridge")
    assert offenders == []


def test_commit_batch_uses_bound_runtime_task_projection() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    function = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "kick_off_workers":
            function = node
            break
    assert function is not None
    offenders: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "JobTaskBridge":
            continue
        if len(node.args) < 3:
            offenders.append(
                f"core/workers.py:{node.lineno} unbound JobTaskBridge")
            continue
        if ast.unparse(node.args[1]) != "state.job_registry":
            offenders.append(
                f"core/workers.py:{node.lineno} missing state.job_registry")
        if ast.unparse(node.args[2]) != "job":
            offenders.append(f"core/workers.py:{node.lineno} missing job")
    assert offenders == []


def test_task_rows_do_not_expose_metadata_control_plane() -> None:
    offenders: list[str] = []
    for path in _python_files():
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "TaskMetadata":
                offenders.append(f"{rel}:{node.lineno} TaskMetadata")
                continue
            if isinstance(node, ast.FunctionDef) and node.name in {
                    "set_meta", "get_meta"}:
                offenders.append(f"{rel}:{node.lineno} {node.name}")
                continue
            if isinstance(node, ast.Attribute) and node.attr in {
                    "set_meta", "get_meta"}:
                offenders.append(f"{rel}:{node.lineno} {node.attr}")
    assert offenders == []


def test_task_projection_is_not_owned_by_models_module() -> None:
    models_path = ROOT / "core" / "models.py"
    tree = ast.parse(models_path.read_text(), filename=str(models_path))
    forbidden_class_names = {"Task", "Tasks"}
    forbidden_assignments = {
        "TASK_AUTO_REMOVE_PROGRESS_SECONDS",
        "_TERMINAL_STATUSES",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in forbidden_class_names:
            offenders.append(f"core/models.py:{node.lineno} class {node.name}")
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets.append(node.target)
        for target in targets:
            if isinstance(target, ast.Name) and target.id in forbidden_assignments:
                offenders.append(f"core/models.py:{node.lineno} {target.id}")
    assert offenders == []


def test_smart_sync_cleanup_uses_bound_runtime_task_projection() -> None:
    path = ROOT / "core" / "smart_sync" / "runner.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "submit_job":
                    offenders.append(f"runner.py:{node.lineno} submit_job")
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "_kick_off_refresh_cleanup":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in {
                    "add", "update", "set_label", "clear_message", "remove"}:
                continue
            if ast.unparse(func.value) == "config.state.tasks":
                offenders.append(
                    f"runner.py:{child.lineno} config.state.tasks.{func.attr}")
    assert offenders == []


def test_production_imports_task_projection_from_state_tasks() -> None:
    production_roots = [
        ROOT / "core",
        ROOT / "features",
        ROOT / "ui",
        ROOT / "idlegit.py",
    ]
    files: list[Path] = []
    for root in production_roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(sorted(root.rglob("*.py")))
    forbidden_names = {"Task", "Tasks", "TASK_AUTO_REMOVE_PROGRESS_SECONDS"}
    offenders: list[str] = []
    for path in files:
        if path == ROOT / "core" / "models.py":
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imports_models = (
                node.module == "core.models"
                or (node.level == 1 and node.module == "models")
                or (node.level == 2 and node.module == "models")
            )
            if not imports_models:
                continue
            for alias in node.names:
                if alias.name in forbidden_names:
                    offenders.append(
                        f"{rel}:{node.lineno} {alias.name} from core.models")
    assert offenders == []


def test_runtime_registries_are_owned_by_state_modules() -> None:
    models_path = ROOT / "core" / "models.py"
    tree = ast.parse(models_path.read_text(), filename=str(models_path))
    forbidden_class_names = {
        "WorkflowRunRecord",
        "WorkflowRunRegistry",
        "WorkflowFollowupRecord",
        "WorkflowFollowupRegistry",
        "ViewLoadRecord",
        "ViewLoadRegistry",
        "ReviewDraftRecord",
        "ReviewDraftRegistry",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in forbidden_class_names:
            offenders.append(f"core/models.py:{node.lineno} class {node.name}")
    for rel_path in [
        Path("core/state/workflows.py"),
        Path("core/state/views.py"),
        Path("core/state/review_drafts.py"),
    ]:
        path = ROOT / rel_path
        state_tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(state_tree):
            if isinstance(node, ast.ImportFrom) and node.module in {
                    "core.models", ".models", "..models"}:
                offenders.append(f"{rel_path}:{node.lineno} imports models")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "core.models":
                        offenders.append(
                            f"{rel_path}:{node.lineno} imports core.models")
    assert offenders == []


def test_production_imports_runtime_registries_from_state_modules() -> None:
    production_roots = [
        ROOT / "core",
        ROOT / "features",
        ROOT / "ui",
        ROOT / "idlegit.py",
    ]
    files: list[Path] = []
    for root in production_roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(sorted(root.rglob("*.py")))
    forbidden_names = {
        "WorkflowRunRecord",
        "WorkflowRunRegistry",
        "WorkflowFollowupRecord",
        "WorkflowFollowupRegistry",
        "ViewLoadRecord",
        "ViewLoadRegistry",
        "ReviewDraftRecord",
        "ReviewDraftRegistry",
    }
    offenders: list[str] = []
    for path in files:
        if path == ROOT / "core" / "models.py":
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imports_models = (
                node.module == "core.models"
                or (node.level == 1 and node.module == "models")
                or (node.level == 2 and node.module == "models")
            )
            if not imports_models:
                continue
            for alias in node.names:
                if alias.name in forbidden_names:
                    offenders.append(
                        f"{rel}:{node.lineno} {alias.name} from core.models")
    assert offenders == []


def test_view_state_is_owned_by_state_views_module() -> None:
    models_path = ROOT / "core" / "models.py"
    tree = ast.parse(models_path.read_text(), filename=str(models_path))
    forbidden_class_names = {
        "DiffViewer",
        "TaskLogViewer",
        "CommitViewModal",
        "HelpPage",
        "HelpScreen",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in forbidden_class_names:
            offenders.append(f"core/models.py:{node.lineno} class {node.name}")
    path = ROOT / "core" / "state" / "views.py"
    views_tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(views_tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
                "core.models", ".models", "..models"}:
            offenders.append(f"core/state/views.py:{node.lineno} imports models")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "core.models":
                    offenders.append(
                        f"core/state/views.py:{node.lineno} imports core.models")
    assert offenders == []


def test_production_imports_view_state_from_state_views() -> None:
    production_roots = [
        ROOT / "core",
        ROOT / "features",
        ROOT / "ui",
        ROOT / "idlegit.py",
    ]
    files: list[Path] = []
    for root in production_roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(sorted(root.rglob("*.py")))
    forbidden_names = {
        "DiffViewer",
        "TaskLogViewer",
        "CommitViewModal",
        "HelpPage",
        "HelpScreen",
    }
    offenders: list[str] = []
    for path in files:
        if path == ROOT / "core" / "models.py":
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imports_models = (
                node.module == "core.models"
                or (node.level == 1 and node.module == "models")
                or (node.level == 2 and node.module == "models")
            )
            if not imports_models:
                continue
            for alias in node.names:
                if alias.name in forbidden_names:
                    offenders.append(
                        f"{rel}:{node.lineno} {alias.name} from core.models")
    assert offenders == []


def test_execution_registries_are_not_task_keyed() -> None:
    offenders: list[str] = []
    for path in _python_files():
        rel = path.relative_to(ROOT)
        text = path.read_text()
        if "WeakKeyDictionary" in text:
            offenders.append(f"{rel}: WeakKeyDictionary")
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name not in {"WorkflowRunRegistry", "WorkflowFollowupRegistry"}:
                continue
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "link_task":
                    offenders.append(f"{rel}:{item.lineno} {node.name}.link_task")
    assert offenders == []


def test_review_block_does_not_own_draft_intent_or_file_state() -> None:
    path = ROOT / "core" / "models.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden = {
        "files",
        "files_loading",
        "staged_paths",
        "message",
        "push",
        "amend",
        "workflow_toggles",
        "then_run_items",
        "cancel_event",
        "suggesting",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "ReviewBlock":
            continue
        for item in node.body:
            if isinstance(item, ast.AnnAssign):
                target = item.target
                if isinstance(target, ast.Name) and target.id in forbidden:
                    offenders.append(f"core/models.py:{item.lineno} {target.id}")
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id in forbidden:
                        offenders.append(f"core/models.py:{item.lineno} {target.id}")
    assert offenders == []


def test_diff_viewer_does_not_own_async_loader_state() -> None:
    path = ROOT / "core" / "models.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden = {
        "lines",
        "loading",
        "log_lines",
        "log_loading",
        "blame_lines",
        "blame_loading",
        "cancel_event",
        "lock",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "DiffViewer":
            continue
        for item in node.body:
            if isinstance(item, ast.AnnAssign):
                target = item.target
                if isinstance(target, ast.Name) and target.id in forbidden:
                    offenders.append(f"core/models.py:{item.lineno} {target.id}")
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id in forbidden:
                        offenders.append(f"core/models.py:{item.lineno} {target.id}")
    assert offenders == []


def test_diff_viewer_modal_does_not_own_loader_jobs() -> None:
    path = ROOT / "ui" / "modals" / "diff_viewer.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {"threading", "core.jobs", "core.workers"}
    forbidden_imports = {
        "git_bounded_output",
        "kick_off_diff_viewer_loads",
        "query_file_blame",
        "query_file_log",
    }
    forbidden_calls = {
        "Thread",
        "submit_job",
        "git_bounded_output",
        "kick_off_diff_viewer_loads",
        "query_file_blame",
        "query_file_log",
        "open",
    }
    forbidden_defs = {
        "open_diff_viewer",
        "close_diff_viewer",
        "_any_tab_loading",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    offenders.append(
                        f"ui/modals/diff_viewer.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(
                    f"ui/modals/diff_viewer.py:{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        f"ui/modals/diff_viewer.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(
                f"ui/modals/diff_viewer.py:{node.lineno} def {node.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_calls:
                offenders.append(
                    f"ui/modals/diff_viewer.py:{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                offenders.append(
                    f"ui/modals/diff_viewer.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_clone_modal_does_not_own_worker_dispatch_or_session() -> None:
    path = ROOT / "ui" / "modals" / "clone.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {"core.workers"}
    forbidden_imports = {"kick_off_clone"}
    forbidden_calls = {
        "kick_off_clone",
        "open",
        "iterdir",
    }
    forbidden_defs = {
        "open_clone_modal",
        "close_clone_modal",
        "_try_clone",
        "_enter_edit",
        "_commit_edit",
        "_cancel_edit",
        "_handle_typing",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(f"ui/modals/clone.py:{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        f"ui/modals/clone.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(f"ui/modals/clone.py:{node.lineno} def {node.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_calls:
                offenders.append(f"ui/modals/clone.py:{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                offenders.append(f"ui/modals/clone.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_task_log_viewer_does_not_own_async_loader_state() -> None:
    path = ROOT / "core" / "models.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden = {
        "lines",
        "loading",
        "error",
        "cancel_event",
        "lock",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "TaskLogViewer":
            continue
        for item in node.body:
            if isinstance(item, ast.AnnAssign):
                target = item.target
                if isinstance(target, ast.Name) and target.id in forbidden:
                    offenders.append(f"core/models.py:{item.lineno} {target.id}")
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id in forbidden:
                        offenders.append(f"core/models.py:{item.lineno} {target.id}")
    assert offenders == []


def test_task_log_viewer_modal_does_not_own_loader_jobs() -> None:
    path = ROOT / "ui" / "modals" / "task_log_viewer.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {"threading", "core.jobs", "core.workers"}
    forbidden_imports = {
        "fetch_run_log",
        "kick_off_task_log_load",
    }
    forbidden_calls = {
        "Thread",
        "submit_job",
        "fetch_run_log",
        "kick_off_task_log_load",
    }
    forbidden_defs = {
        "open_task_log_viewer",
        "close_task_log_viewer",
        "_set_scroll",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    offenders.append(
                        f"task_log_viewer.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(
                    f"task_log_viewer.py:{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        f"task_log_viewer.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(f"task_log_viewer.py:{node.lineno} def {node.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_calls:
                offenders.append(f"task_log_viewer.py:{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                offenders.append(f"task_log_viewer.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_review_ui_does_not_own_file_load_jobs() -> None:
    path = ROOT / "ui" / "review.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {"threading", "core.jobs"}
    forbidden_imports = {"query_working_tree"}
    forbidden_calls = {
        "Thread",
        "submit_job",
        "query_working_tree",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    offenders.append(f"ui/review.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(f"ui/review.py:{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(f"ui/review.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_calls:
                offenders.append(f"ui/review.py:{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                offenders.append(f"ui/review.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_commit_view_modal_does_not_own_async_loader_state() -> None:
    path = ROOT / "core" / "models.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden = {
        "tags_loading",
        "details_loading",
        "files_loading",
        "reflog_loading",
        "cancel_event",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "CommitViewModal":
            continue
        for item in node.body:
            if isinstance(item, ast.AnnAssign):
                target = item.target
                if isinstance(target, ast.Name) and target.id in forbidden:
                    offenders.append(f"core/models.py:{item.lineno} {target.id}")
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id in forbidden:
                        offenders.append(
                            f"core/models.py:{item.lineno} {target.id}")
    assert offenders == []


def test_commit_view_ui_does_not_own_session_or_action_behavior() -> None:
    path = ROOT / "ui" / "modals" / "commit_view.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {"core.workers"}
    forbidden_imports = {
        "kick_off_add_tag",
        "kick_off_load_commit_view",
        "ActionMenuItem",
    }
    forbidden_defs = {
        "_commit_view_load_ids",
        "_is_loading",
        "_tags_loading",
        "_files_loading",
        "_reflog_loading",
        "_build_action_items",
        "open_commit_view_modal",
        "_wrap_text",
        "_flow_badges",
        "_build_tab_header",
        "_close_modal",
        "_begin_add_tag",
        "_cancel_inline",
        "_request_confirm",
        "_clear_confirm",
        "_apply_pending",
        "_handle_confirm",
        "_handle_inline_edit",
        "_open_diff_for_focused_file",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(f"commit_view.py:{node.lineno} def {node.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(f"commit_view.py:{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        f"commit_view.py:{node.lineno} import {alias.name}")
    assert offenders == []


def test_action_menu_does_not_own_async_loader_state() -> None:
    path = ROOT / "core" / "models.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden = {
        "state_loading",
        "inventory_loading",
        "tree_loading",
        "commits_loading",
        "cancel_event",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "ActionMenu":
            continue
        for item in node.body:
            if isinstance(item, ast.AnnAssign):
                target = item.target
                if isinstance(target, ast.Name) and target.id in forbidden:
                    offenders.append(f"core/models.py:{item.lineno} {target.id}")
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id in forbidden:
                        offenders.append(
                            f"core/models.py:{item.lineno} {target.id}")
    assert offenders == []


def test_action_menu_modal_does_not_own_loader_jobs() -> None:
    path = ROOT / "ui" / "modals" / "action_menu.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {
        "threading",
        "core.jobs",
        "core.action_menu_projection",
    }
    forbidden_imports = {
        "gh_available",
        "parse_github_slug",
        "query_target_state",
        "query_working_tree",
        "load_commits",
        "list_stashes",
        "list_remotes",
    }
    forbidden_calls = {
        "Thread",
        "submit_job",
        "query_target_state",
        "query_working_tree",
        "load_commits",
        "list_stashes",
        "list_remotes",
        "_submit_action_menu_job",
    }
    forbidden_functions = {
        "_kick_off_state_load",
        "_kick_off_inventory_load",
        "_kick_off_tree_load",
        "_kick_off_initial_commits",
        "_submit_action_menu_job",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    offenders.append(
                        "ui/modals/action_menu.py:"
                        f"{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(
                    "ui/modals/action_menu.py:"
                    f"{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        "ui/modals/action_menu.py:"
                        f"{node.lineno} import {alias.name}")
        elif isinstance(node, ast.FunctionDef):
            if node.name in forbidden_functions:
                offenders.append(
                    f"ui/modals/action_menu.py:{node.lineno} {node.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_calls:
                offenders.append(
                    f"ui/modals/action_menu.py:{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                offenders.append(
                    f"ui/modals/action_menu.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_action_menu_modal_does_not_own_action_execution() -> None:
    path = ROOT / "ui" / "modals" / "action_menu.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {
        "core.git_ops",
        "core.workers",
    }
    forbidden_imports = {
        "RemoteRow",
        "kick_off_action",
        "kick_off_remote_changes",
        "kick_off_safe_merge",
        "merge_head_sha",
    }
    forbidden_calls = {
        "kick_off_action",
        "kick_off_remote_changes",
        "kick_off_safe_merge",
        "merge_head_sha",
        "_apply_remote_op",
        "_dispatch_action",
    }
    forbidden_functions = {
        "_begin_rename_remote",
        "_begin_set_url_remote",
        "_begin_new_remote_name",
        "_begin_new_remote_url",
        "_apply_remote_op",
        "_dispatch_action",
        "_handle_confirm_key",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    offenders.append(
                        "ui/modals/action_menu.py:"
                        f"{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(
                    "ui/modals/action_menu.py:"
                    f"{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        "ui/modals/action_menu.py:"
                        f"{node.lineno} import {alias.name}")
        elif isinstance(node, ast.FunctionDef):
            if node.name in forbidden_functions:
                offenders.append(
                    f"ui/modals/action_menu.py:{node.lineno} {node.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_calls:
                offenders.append(
                    f"ui/modals/action_menu.py:{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                offenders.append(
                    f"ui/modals/action_menu.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_action_menu_modal_does_not_own_menu_state_transitions() -> None:
    path = ROOT / "ui" / "modals" / "action_menu.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_imports = {
        "ActionSubmenuFrame",
        "begin_new_remote_url",
        "cancel_inline_edit",
        "request_confirm",
    }
    forbidden_functions = {
        "_build_stash_apply_items",
        "_in_submenu",
        "_current_items",
        "_current_selected",
        "_set_current_selected",
        "_breadcrumb_segments",
        "_first_actionable_index",
        "_push_submenu",
        "_pop_submenu",
        "_enter_submenu_for",
        "_exit_to_parent",
        "_handle_inline_edit_key",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        "ui/modals/action_menu.py:"
                        f"{node.lineno} import {alias.name}")
        elif isinstance(node, ast.FunctionDef):
            if node.name in forbidden_functions:
                offenders.append(
                    f"ui/modals/action_menu.py:{node.lineno} {node.name}")
    assert offenders == []


def test_action_menu_modal_does_not_own_key_behavior() -> None:
    path = ROOT / "ui" / "modals" / "action_menu.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_imports = {
        "begin_remote_rename_for_item",
        "dispatch_action_menu_item",
        "handle_action_menu_confirm_key",
        "handle_action_menu_inline_edit_key",
        "request_remote_delete_for_item",
        "enter_submenu_for",
        "exit_to_parent",
        "reset_to_main_menu",
        "set_current_selected",
        "step_selection",
    }
    forbidden_functions = {
        "_step_selection",
        "_handle_pane_key",
        "_handle_tree_key",
        "_handle_commits_key",
        "_is_typing_key",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        "ui/modals/action_menu.py:"
                        f"{node.lineno} import {alias.name}")
        elif isinstance(node, ast.FunctionDef):
            if node.name in forbidden_functions:
                offenders.append(
                    f"ui/modals/action_menu.py:{node.lineno} {node.name}")
    assert offenders == []


def test_action_menu_modal_does_not_own_session_lifecycle() -> None:
    path = ROOT / "ui" / "modals" / "action_menu.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_imports = {
        "action_menu_load_ids",
        "ensure_action_menu_load_ids",
        "initial_meta_from_cache",
        "kick_off_action_menu_loaders",
        "state_label_for",
    }
    forbidden_functions = {
        "open_action_menu",
        "_close_action_menu",
    }
    forbidden_assigns = {
        "action_menu",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        "ui/modals/action_menu.py:"
                        f"{node.lineno} import {alias.name}")
        elif isinstance(node, ast.FunctionDef):
            if node.name in forbidden_functions:
                offenders.append(
                    f"ui/modals/action_menu.py:{node.lineno} {node.name}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    if target.attr in forbidden_assigns:
                        offenders.append(
                            "ui/modals/action_menu.py:"
                            f"{node.lineno} assign {target.attr}")
    assert offenders == []


def test_branch_name_prompt_ui_does_not_own_git_or_action_dispatch() -> None:
    path = ROOT / "ui" / "modals" / "branch_name_prompt.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {
        "core.git_ops",
        "core.workers",
    }
    forbidden_imports = {
        "git",
        "kick_off_action",
        "open_branch_name_prompt",
        "close_branch_name_prompt",
        "kick_off_branch_name_prompt_prepare",
    }
    forbidden_functions = {
        "open_branch_name_prompt",
        "_open_branch_name_prompt",
        "_submit_branch_name_prompt",
    }
    forbidden_assigns = {
        "action_menu",
        "branch_name_prompt",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    offenders.append(
                        f"ui/modals/branch_name_prompt.py:"
                        f"{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(
                    f"ui/modals/branch_name_prompt.py:"
                    f"{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        f"ui/modals/branch_name_prompt.py:"
                        f"{node.lineno} import {alias.name}")
        elif isinstance(node, ast.FunctionDef):
            if node.name in forbidden_functions:
                offenders.append(
                    f"ui/modals/branch_name_prompt.py:"
                    f"{node.lineno} {node.name}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    if target.attr in forbidden_assigns:
                        offenders.append(
                            f"ui/modals/branch_name_prompt.py:"
                            f"{node.lineno} assign {target.attr}")
    assert offenders == []


def test_remote_branch_picker_does_not_own_async_loader_state() -> None:
    path = ROOT / "core" / "models.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden = {
        "refs",
        "loading",
        "cancel_event",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "RemoteBranchPicker":
            continue
        for item in node.body:
            if isinstance(item, ast.AnnAssign):
                target = item.target
                if isinstance(target, ast.Name) and target.id in forbidden:
                    offenders.append(f"core/models.py:{item.lineno} {target.id}")
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id in forbidden:
                        offenders.append(f"core/models.py:{item.lineno} {target.id}")
    assert offenders == []


def test_remote_branch_picker_ui_does_not_own_loader_jobs() -> None:
    path = ROOT / "ui" / "modals" / "remote_branch_picker.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {"threading", "core.jobs", "core.workers", "core.git_ops"}
    forbidden_imports = {
        "is_safe_ref_arg",
        "kick_off_action",
        "kick_off_remote_branch_picker_load",
        "list_remote_tracking_refs",
    }
    forbidden_calls = {
        "Thread",
        "is_safe_ref_arg",
        "kick_off_action",
        "kick_off_remote_branch_picker_load",
        "submit_job",
        "list_remote_tracking_refs",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    offenders.append(
                        "ui/modals/remote_branch_picker.py:"
                        f"{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(
                    "ui/modals/remote_branch_picker.py:"
                    f"{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        "ui/modals/remote_branch_picker.py:"
                        f"{node.lineno} import {alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_calls:
                offenders.append(
                    "ui/modals/remote_branch_picker.py:"
                    f"{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                offenders.append(
                    "ui/modals/remote_branch_picker.py:"
                    f"{node.lineno} {func.attr}")
    assert offenders == []


def test_remote_branch_picker_ui_does_not_own_session_or_key_behavior() -> None:
    path = ROOT / "ui" / "modals" / "remote_branch_picker.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_defs = {
        "open_remote_branch_picker",
        "close_remote_branch_picker",
        "submit_remote_branch",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(
                "ui/modals/remote_branch_picker.py:"
                f"{node.lineno} def {node.name}")
    assert offenders == []


def test_reset_prompt_ui_does_not_own_worker_dispatch_or_session() -> None:
    path = ROOT / "ui" / "modals" / "reset_prompt.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {"core.workers"}
    forbidden_imports = {"kick_off_action"}
    forbidden_defs = {
        "open_reset_prompt",
        "close_reset_prompt",
        "submit_reset_prompt",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(
                    f"ui/modals/reset_prompt.py:{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        f"ui/modals/reset_prompt.py:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(
                f"ui/modals/reset_prompt.py:{node.lineno} def {node.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_imports:
                offenders.append(
                    f"ui/modals/reset_prompt.py:{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_imports:
                offenders.append(
                    f"ui/modals/reset_prompt.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_workflow_picker_ui_does_not_own_worker_dispatch_or_session() -> None:
    path = ROOT / "ui" / "modals" / "workflow_picker.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {"core.workers"}
    forbidden_imports = {"kick_off_manual_dispatch"}
    forbidden_defs = {
        "open_workflow_picker",
        "close_workflow_picker",
        "submit_workflow_picker",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(
                    f"ui/modals/workflow_picker.py:{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        "ui/modals/workflow_picker.py:"
                        f"{node.lineno} import {alias.name}")
        elif isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(
                f"ui/modals/workflow_picker.py:{node.lineno} def {node.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_imports:
                offenders.append(
                    f"ui/modals/workflow_picker.py:{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_imports:
                offenders.append(
                    f"ui/modals/workflow_picker.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_workspace_switcher_ui_does_not_own_worker_dispatch_or_session() -> None:
    path = ROOT / "ui" / "modals" / "workspace_switcher.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    forbidden_modules = {"core.workers"}
    forbidden_imports = {"switch_workspace"}
    forbidden_defs = {
        "open_workspace_switcher",
        "close_workspace_switcher",
        "submit_workspace_switcher",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in forbidden_modules:
                offenders.append(
                    f"ui/modals/workspace_switcher.py:{node.lineno} from {node.module}")
            for alias in node.names:
                if alias.name in forbidden_imports:
                    offenders.append(
                        "ui/modals/workspace_switcher.py:"
                        f"{node.lineno} import {alias.name}")
        elif isinstance(node, ast.FunctionDef) and node.name in forbidden_defs:
            offenders.append(
                f"ui/modals/workspace_switcher.py:{node.lineno} def {node.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_imports:
                offenders.append(
                    f"ui/modals/workspace_switcher.py:{node.lineno} {func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_imports:
                offenders.append(
                    f"ui/modals/workspace_switcher.py:{node.lineno} {func.attr}")
    assert offenders == []


def test_workflow_followups_do_not_expose_workflow_run_handles() -> None:
    forbidden = {
        "slug",
        "run_id",
        "workflow_name",
        "job_id",
        "run_url",
        "latest_view",
    }
    offenders: list[str] = []
    for path in _python_files():
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                value = node.value
                if isinstance(value, ast.Name) and value.id in {"meta", "cm"}:
                    offenders.append(f"{rel}:{node.lineno} {value.id}.{node.attr}")
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "link_task":
                for keyword in node.keywords:
                    if keyword.arg in forbidden:
                        offenders.append(
                            f"{rel}:{node.lineno} link_task({keyword.arg})"
                        )
    assert offenders == []


def test_workspace_state_is_owned_by_state_workspaces_module() -> None:
    moved_names = {
        "SubtreeSpec",
        "Workspace",
        "WorkspaceCreator",
        "WorkspaceDraft",
        "WorkspaceMenu",
        "WorkspaceMenuRow",
        "WorkspaceSwitcher",
    }
    models_path = ROOT / "core" / "models.py"
    models_tree = ast.parse(models_path.read_text(), filename=str(models_path))
    offenders: list[str] = []
    for node in ast.walk(models_tree):
        if isinstance(node, ast.ClassDef) and node.name in moved_names:
            offenders.append(f"core/models.py:{node.lineno} class {node.name}")

    workspaces_path = ROOT / "core" / "state" / "workspaces.py"
    workspaces_tree = ast.parse(
        workspaces_path.read_text(), filename=str(workspaces_path))
    for node in ast.walk(workspaces_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.models":
            offenders.append(
                f"core/state/workspaces.py:{node.lineno} from core.models")
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "models":
            offenders.append(
                f"core/state/workspaces.py:{node.lineno} from .models")

    assert offenders == []


def test_production_imports_workspace_state_from_state_workspaces() -> None:
    moved_names = {
        "SubtreeSpec",
        "Workspace",
        "WorkspaceCreator",
        "WorkspaceDraft",
        "WorkspaceMenu",
        "WorkspaceMenuRow",
        "WorkspaceSwitcher",
    }
    allowed_reexports = {ROOT / "core" / "models.py"}
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        if path in allowed_reexports:
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imports_models = (
                node.module == "core.models"
                or (node.level in {1, 2} and node.module == "models")
            )
            if not imports_models:
                continue
            found = sorted(alias.name for alias in node.names if alias.name in moved_names)
            if found:
                offenders.append(f"{rel}:{node.lineno} {', '.join(found)}")
    assert offenders == []


def test_repo_state_is_owned_by_state_repos_module() -> None:
    moved_names = {
        "ChildRef",
        "Repo",
        "WorkflowInfo",
        "WorkflowInput",
    }
    models_path = ROOT / "core" / "models.py"
    models_tree = ast.parse(models_path.read_text(), filename=str(models_path))
    offenders: list[str] = []
    for node in ast.walk(models_tree):
        if isinstance(node, ast.ClassDef) and node.name in moved_names:
            offenders.append(f"core/models.py:{node.lineno} class {node.name}")

    repos_path = ROOT / "core" / "state" / "repos.py"
    repos_tree = ast.parse(repos_path.read_text(), filename=str(repos_path))
    for node in ast.walk(repos_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.models":
            offenders.append(f"core/state/repos.py:{node.lineno} from core.models")
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "models":
            offenders.append(f"core/state/repos.py:{node.lineno} from .models")

    assert offenders == []


def test_production_imports_repo_state_from_state_repos() -> None:
    moved_names = {
        "ChildRef",
        "Repo",
        "WorkflowInfo",
        "WorkflowInput",
    }
    composition_bridge = ROOT / "core" / "models.py"
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        if path == composition_bridge:
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imports_models = (
                node.module == "core.models"
                or (node.level in {1, 2} and node.module == "models")
            )
            if not imports_models:
                continue
            found = sorted(alias.name for alias in node.names if alias.name in moved_names)
            if found:
                offenders.append(f"{rel}:{node.lineno} {', '.join(found)}")
    assert offenders == []


def test_review_state_is_owned_by_state_review_module() -> None:
    moved_names = {
        "FileChange",
        "LFSCandidate",
        "ReviewBlock",
        "ThenRunSelector",
        "WorkflowToggle",
    }
    models_path = ROOT / "core" / "models.py"
    models_tree = ast.parse(models_path.read_text(), filename=str(models_path))
    offenders: list[str] = []
    for node in ast.walk(models_tree):
        if isinstance(node, ast.ClassDef) and node.name in moved_names:
            offenders.append(f"core/models.py:{node.lineno} class {node.name}")

    review_path = ROOT / "core" / "state" / "review.py"
    review_tree = ast.parse(review_path.read_text(), filename=str(review_path))
    for node in ast.walk(review_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.models":
            offenders.append(f"core/state/review.py:{node.lineno} from core.models")
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "models":
            offenders.append(f"core/state/review.py:{node.lineno} from .models")

    assert offenders == []


def test_production_imports_review_state_from_state_review() -> None:
    moved_names = {
        "FileChange",
        "LFSCandidate",
        "ReviewBlock",
        "ThenRunSelector",
        "WorkflowToggle",
    }
    composition_bridge = ROOT / "core" / "models.py"
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        if path == composition_bridge:
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imports_models = (
                node.module == "core.models"
                or (node.level in {1, 2} and node.module == "models")
            )
            if not imports_models:
                continue
            found = sorted(alias.name for alias in node.names if alias.name in moved_names)
            if found:
                offenders.append(f"{rel}:{node.lineno} {', '.join(found)}")
    assert offenders == []


def test_safe_merge_state_is_owned_by_state_safe_merge_module() -> None:
    moved_names = {
        "ConflictFile",
        "ConflictHunk",
        "MergeSide",
        "SafeMergeScreen",
    }
    models_path = ROOT / "core" / "models.py"
    models_tree = ast.parse(models_path.read_text(), filename=str(models_path))
    offenders: list[str] = []
    for node in ast.walk(models_tree):
        if isinstance(node, ast.ClassDef) and node.name in moved_names:
            offenders.append(f"core/models.py:{node.lineno} class {node.name}")

    safe_merge_path = ROOT / "core" / "state" / "safe_merge.py"
    safe_merge_tree = ast.parse(
        safe_merge_path.read_text(), filename=str(safe_merge_path))
    for node in ast.walk(safe_merge_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.models":
            offenders.append(
                f"core/state/safe_merge.py:{node.lineno} from core.models")
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "models":
            offenders.append(
                f"core/state/safe_merge.py:{node.lineno} from .models")

    assert offenders == []


def test_production_imports_safe_merge_state_from_state_safe_merge() -> None:
    moved_names = {
        "ConflictFile",
        "ConflictHunk",
        "MergeSide",
        "SafeMergeScreen",
    }
    composition_bridge = ROOT / "core" / "models.py"
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        if path == composition_bridge:
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imports_models = (
                node.module == "core.models"
                or (node.level in {1, 2} and node.module == "models")
            )
            if not imports_models:
                continue
            found = sorted(alias.name for alias in node.names if alias.name in moved_names)
            if found:
                offenders.append(f"{rel}:{node.lineno} {', '.join(found)}")
    assert offenders == []


def test_action_menu_state_is_owned_by_state_action_menu_module() -> None:
    moved_names = {
        "ActionMenu",
        "ActionMenuItem",
        "ActionSubmenuFrame",
        "CommitEntry",
        "FileEntry",
    }
    models_path = ROOT / "core" / "models.py"
    models_tree = ast.parse(models_path.read_text(), filename=str(models_path))
    offenders: list[str] = []
    for node in ast.walk(models_tree):
        if isinstance(node, ast.ClassDef) and node.name in moved_names:
            offenders.append(f"core/models.py:{node.lineno} class {node.name}")

    action_menu_path = ROOT / "core" / "state" / "action_menu.py"
    action_menu_tree = ast.parse(
        action_menu_path.read_text(), filename=str(action_menu_path))
    for node in ast.walk(action_menu_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.models":
            offenders.append(
                f"core/state/action_menu.py:{node.lineno} from core.models")
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "models":
            offenders.append(
                f"core/state/action_menu.py:{node.lineno} from .models")

    assert offenders == []


def test_production_imports_action_menu_state_from_state_action_menu() -> None:
    moved_names = {
        "ActionMenu",
        "ActionMenuItem",
        "ActionSubmenuFrame",
        "CommitEntry",
        "FileEntry",
    }
    composition_bridge = ROOT / "core" / "models.py"
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        if path == composition_bridge:
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imports_models = (
                node.module == "core.models"
                or (node.level in {1, 2} and node.module == "models")
            )
            if not imports_models:
                continue
            found = sorted(alias.name for alias in node.names if alias.name in moved_names)
            if found:
                offenders.append(f"{rel}:{node.lineno} {', '.join(found)}")
    assert offenders == []


def test_picker_state_is_owned_by_state_pickers_module() -> None:
    moved_names = {
        "BranchPicker",
        "RemoteBranchPicker",
        "WorkflowPicker",
    }
    models_path = ROOT / "core" / "models.py"
    models_tree = ast.parse(models_path.read_text(), filename=str(models_path))
    offenders: list[str] = []
    for node in ast.walk(models_tree):
        if isinstance(node, ast.ClassDef) and node.name in moved_names:
            offenders.append(f"core/models.py:{node.lineno} class {node.name}")

    pickers_path = ROOT / "core" / "state" / "pickers.py"
    pickers_tree = ast.parse(
        pickers_path.read_text(), filename=str(pickers_path))
    for node in ast.walk(pickers_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.models":
            offenders.append(f"core/state/pickers.py:{node.lineno} from core.models")
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "models":
            offenders.append(f"core/state/pickers.py:{node.lineno} from .models")

    assert offenders == []


def test_production_imports_picker_state_from_state_pickers() -> None:
    moved_names = {
        "BranchPicker",
        "RemoteBranchPicker",
        "WorkflowPicker",
    }
    composition_bridge = ROOT / "core" / "models.py"
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        if path == composition_bridge:
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imports_models = (
                node.module == "core.models"
                or (node.level in {1, 2} and node.module == "models")
            )
            if not imports_models:
                continue
            found = sorted(alias.name for alias in node.names if alias.name in moved_names)
            if found:
                offenders.append(f"{rel}:{node.lineno} {', '.join(found)}")
    assert offenders == []


def test_prompt_state_is_owned_by_state_prompts_module() -> None:
    moved_names = {
        "AlignHeadsPrompt",
        "BranchNamePrompt",
        "DetachedRecoveryPrompt",
        "ResetPrompt",
    }
    models_path = ROOT / "core" / "models.py"
    models_tree = ast.parse(models_path.read_text(), filename=str(models_path))
    offenders: list[str] = []
    for node in ast.walk(models_tree):
        if isinstance(node, ast.ClassDef) and node.name in moved_names:
            offenders.append(f"core/models.py:{node.lineno} class {node.name}")

    prompts_path = ROOT / "core" / "state" / "prompts.py"
    prompts_tree = ast.parse(
        prompts_path.read_text(), filename=str(prompts_path))
    for node in ast.walk(prompts_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.models":
            offenders.append(f"core/state/prompts.py:{node.lineno} from core.models")
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "models":
            offenders.append(f"core/state/prompts.py:{node.lineno} from .models")

    assert offenders == []


def test_production_imports_prompt_state_from_state_prompts() -> None:
    moved_names = {
        "AlignHeadsPrompt",
        "BranchNamePrompt",
        "DetachedRecoveryPrompt",
        "ResetPrompt",
    }
    composition_bridge = ROOT / "core" / "models.py"
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        if path == composition_bridge:
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imports_models = (
                node.module == "core.models"
                or (node.level in {1, 2} and node.module == "models")
            )
            if not imports_models:
                continue
            found = sorted(alias.name for alias in node.names if alias.name in moved_names)
            if found:
                offenders.append(f"{rel}:{node.lineno} {', '.join(found)}")
    assert offenders == []


def test_smart_sync_state_is_owned_by_state_smart_sync_module() -> None:
    moved_names = {"SmartSyncCheckout"}
    models_path = ROOT / "core" / "models.py"
    models_tree = ast.parse(models_path.read_text(), filename=str(models_path))
    offenders: list[str] = []
    for node in ast.walk(models_tree):
        if isinstance(node, ast.ClassDef) and node.name in moved_names:
            offenders.append(f"core/models.py:{node.lineno} class {node.name}")

    smart_sync_path = ROOT / "core" / "state" / "smart_sync.py"
    smart_sync_tree = ast.parse(
        smart_sync_path.read_text(), filename=str(smart_sync_path))
    for node in ast.walk(smart_sync_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.models":
            offenders.append(
                f"core/state/smart_sync.py:{node.lineno} from core.models")
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "models":
            offenders.append(
                f"core/state/smart_sync.py:{node.lineno} from .models")

    assert offenders == []


def test_production_imports_smart_sync_state_from_state_smart_sync() -> None:
    moved_names = {"SmartSyncCheckout"}
    composition_bridge = ROOT / "core" / "models.py"
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        if path == composition_bridge:
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imports_models = (
                node.module == "core.models"
                or (node.level in {1, 2} and node.module == "models")
            )
            if not imports_models:
                continue
            found = sorted(alias.name for alias in node.names if alias.name in moved_names)
            if found:
                offenders.append(f"{rel}:{node.lineno} {', '.join(found)}")
    assert offenders == []


def test_core_models_is_only_a_temporary_reexport_surface() -> None:
    models_path = ROOT / "core" / "models.py"
    tree = ast.parse(models_path.read_text(), filename=str(models_path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            offenders.append(f"core/models.py:{node.lineno} {node.name}")
    assert offenders == []


def test_production_imports_state_records_from_owning_modules_not_core_models() -> None:
    composition_bridge = ROOT / "core" / "models.py"
    offenders: list[str] = []
    for path in _python_files_in(STATE_WORKSPACE_PRODUCTION_PATHS):
        if path == composition_bridge:
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imports_models = (
                    node.module == "core.models"
                    or (node.level in {1, 2} and node.module == "models")
                )
                if imports_models:
                    offenders.append(f"{rel}:{node.lineno} from core.models")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "core.models":
                        offenders.append(f"{rel}:{node.lineno} import core.models")
    assert offenders == []


def test_workers_refresh_paths_publish_typed_repo_snapshots() -> None:
    path = ROOT / "core" / "workers.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr == "publish_repo_status":
            offenders.append(f"core/workers.py:{node.lineno} publish_repo_status")
    assert offenders == []


def test_store_message_writer_does_not_mutate_row_projection_messages() -> None:
    path = ROOT / "core" / "state" / "store.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    guarded = {"set_row_message"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in guarded:
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Attribute):
                continue
            if child.attr == "message" and isinstance(child.ctx, ast.Store):
                offenders.append(f"core/state/store.py:{child.lineno} .message")
    assert offenders == []


def test_refresh_applicators_do_not_mutate_row_projection_messages() -> None:
    path = ROOT / "core" / "git_ops.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    guarded = {"apply_repo_refresh_snapshot", "apply_child_refresh_snapshot"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in guarded:
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Attribute):
                continue
            if child.attr == "message" and isinstance(child.ctx, ast.Store):
                offenders.append(f"core/git_ops.py:{child.lineno} {node.name}")
    assert offenders == []
