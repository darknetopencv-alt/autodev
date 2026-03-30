"""Main automation loop for unattended AI-driven development.

Faithfully ports the core loop from ``run-full-auto.sh`` (lines 609-815),
enhanced with:

- **Circuit breaker** (inspired by Ralph): early-exit on no-progress,
  repeated errors, or rate limits.
- **Atomic git commit** (inspired by GSD): auto-commit after each
  successful task for traceability and easy revert.
"""

from __future__ import annotations

import json
import re
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autodev.config import AutodevConfig
    from autodev.log import Logger

from autodev.backends import BackendResult, run_backend
from autodev.circuit_breaker import CircuitBreaker
from autodev.env import adjust_config_for_root, check_prerequisites, has_env_error, is_root
from autodev.gate import run_gate
from autodev.git_ops import (
    auto_commit,
    create_experiment_commit,
    is_git_repo,
    read_recent_git_history,
    revert_commit,
)
from autodev.task_audit import describe_task_contract, normalize_task_contracts
from autodev.heartbeat import Heartbeat
from autodev.progress import append_progress
from autodev.prompt import load_template, render_prompt
from autodev.reflection import (
    apply_task_reflection,
    build_success_learning_notes,
    record_iteration_history,
    reflect_failed_attempt,
)
from autodev.runtime_status import default_run_contract_fields, update_runtime_artifacts
from autodev.snapshot import diff_snapshots, snapshot_directories
from autodev.task_brief import write_idle_task_brief, write_task_brief
from autodev.task_formatting import task_identity_text
from autodev.task_state import normalize_bool, normalize_int
from autodev.task_store import (
    append_task_notes,
    find_task_in_data,
    get_next_task,
    get_recent_project_learning_summaries,
    get_task_counts,
    load_tasks,
    mark_task_blocked_in_file,
    mark_task_passed,
    reset_tasks,
    save_tasks,
)


# ---------------------------------------------------------------------------
# Attempt log naming
# ---------------------------------------------------------------------------

_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_id(task_id: str) -> str:
    """Make *task_id* safe for use in filenames."""
    return _SAFE_RE.sub("_", str(task_id))


def _attempt_log_path(config: AutodevConfig, task_id: str, attempt: int) -> Path:
    """Build the path for a per-attempt log file."""
    backend = config.backend.default
    safe_id = _sanitize_id(task_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"task_{safe_id}__attempt_{attempt}_{stamp}.log"
    log_dir = Path(config.files.attempt_log_subdir) / backend
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / name


def _timestamp_utc() -> str:
    """Return a compact UTC timestamp string for status messages."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _attempt_looks_rate_limited(attempt_log: Path, patterns: list[str]) -> bool:
    """Return True when the attempt log contains configured rate-limit markers."""
    if not patterns:
        return False
    try:
        content = attempt_log.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False
    return any(pattern.lower() in content for pattern in patterns)


def _relative_path_within(base: Path, path: Path) -> str | None:
    """Return a normalized relative path when *path* lives under *base*."""
    try:
        rel = path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return None
    return rel if rel and rel != "." else None


def _runtime_artifact_filters(config: AutodevConfig, code_dir: Path) -> tuple[set[str], tuple[str, ...]]:
    """Return exact file paths and directory prefixes for autodev runtime artifacts."""
    runtime_files: set[str] = set()
    runtime_dir_prefixes: list[str] = []

    for file_path in (Path(config.files.task_json), Path(config.files.progress)):
        rel = _relative_path_within(code_dir, file_path)
        if rel:
            runtime_files.add(rel)

    for dir_path in (Path(config.files.log_dir), Path(config.files.attempt_log_subdir)):
        rel = _relative_path_within(code_dir, dir_path)
        if rel:
            runtime_dir_prefixes.append(rel.rstrip("/") + "/")

    return runtime_files, tuple(dict.fromkeys(runtime_dir_prefixes))


def _filter_runtime_changed_files(
    changed_files: list[str],
    config: AutodevConfig,
    code_dir: Path,
) -> list[str]:
    """Drop autodev runtime artifacts from snapshot diffs before verification/commit."""
    runtime_files, runtime_dir_prefixes = _runtime_artifact_filters(config, code_dir)
    filtered: list[str] = []

    for path in changed_files:
        normalized = str(path).replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized in runtime_files:
            continue
        if any(
            normalized == prefix[:-1] or normalized.startswith(prefix)
            for prefix in runtime_dir_prefixes
        ):
            continue
        filtered.append(normalized)

    return filtered


# ---------------------------------------------------------------------------
# Runner result
# ---------------------------------------------------------------------------

class RunResult:
    """Summary of a full ``autodev run`` invocation."""

    def __init__(self) -> None:
        self.tasks_attempted: int = 0
        self.tasks_completed: int = 0
        self.tasks_blocked: int = 0
        self.interrupted: bool = False
        self.env_error: bool = False
        self.blocked_present: bool = False
        self.pending_remaining: int = 0
        self.current_epoch: int = 1
        self.max_epochs: int = 1

    @property
    def exit_code(self) -> int:
        if self.interrupted:
            return 130
        if self.env_error:
            return 1
        if self.blocked_present:
            return 2
        return 0


def _experiments_log_path(config: AutodevConfig) -> Path:
    return Path(config.files.log_dir) / "experiments.jsonl"


def _append_experiment_log(config: AutodevConfig, entry: dict) -> None:
    path = _experiments_log_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_recent_experiment_history(config: AutodevConfig, *, task_id: str, limit: int = 5) -> list[dict]:
    path = _experiments_log_path(config)
    if limit <= 0 or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    matches: list[dict] = []
    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if str(entry.get("task_id", "")).strip() != task_id:
            continue
        matches.append(entry)
        if len(matches) >= limit:
            break
    matches.reverse()
    return matches


def _format_metric_summary(metric_name: str, value: float | None) -> str:
    return "" if value is None else f"{metric_name}={value:g}"


def _load_tasks_or_fallback(task_json_path: Path, fallback: dict) -> dict:
    """Load the task store, falling back to the last in-memory snapshot on read errors."""
    try:
        return load_tasks(task_json_path)
    except (FileNotFoundError, ValueError):
        return fallback


def _task_runtime_scope(
    task_id: str,
    task_name: str,
    *,
    attempt: int = 0,
    max_attempts: int = 0,
    attempt_log: str = "",
    heartbeat_elapsed_seconds: int = 0,
) -> dict[str, object]:
    """Return the common active-task runtime fields for delivery-mode updates."""
    return {
        "current_task_id": task_id,
        "current_task_title": task_name,
        "current_attempt": attempt,
        "max_attempts": max_attempts,
        "attempt_log": attempt_log,
        "heartbeat_elapsed_seconds": heartbeat_elapsed_seconds,
    }


def _inactive_task_runtime_scope() -> dict[str, object]:
    """Return runtime fields for an idle runner with no active task selected."""
    return {
        "current_task_id": "",
        "current_task_title": "",
        "current_attempt": 0,
        "heartbeat_elapsed_seconds": 0,
        "attempt_log": "",
    }


def _idle_run_status_from_counts(counts: dict[str, int]) -> tuple[str, str]:
    """Return the idle runner status/message that matches the queue state."""
    if counts.get("pending", 0) > 0:
        return "waiting", "Preparing next task"
    if counts.get("blocked", 0) > 0:
        return "blocked", "No runnable tasks remain; blocked tasks require review"
    return "completed", "All tasks finished"


def _completion_outcome_text(gate_result) -> str:
    """Extract the normalized completion outcome label from a gate result."""
    return str(_gate_completion_attr(gate_result, "outcome", "") or "")


def _experiment_execution_context(
    task: dict,
    *,
    iteration: int,
    max_iterations: int,
    metric_name: str,
    baseline_metric: float | None,
    best_metric: float | None,
    no_improvement_streak: int,
    metric_goal_summary: str,
) -> dict[str, object]:
    return {
        "execution_mode": _task_execution_mode_label(task),
        "current_iteration": iteration,
        "max_iterations": max_iterations,
        "baseline_metric": _format_metric_summary(metric_name, baseline_metric),
        "best_metric": _format_metric_summary(metric_name, best_metric),
        "no_improvement_streak": no_improvement_streak,
        "metric_goal": metric_goal_summary,
    }


def _task_contract_fields(task: dict) -> dict[str, str]:
    return describe_task_contract(task)


def _task_execution_strategy(task: dict) -> str:
    return _task_contract_fields(task)["execution_strategy"]


def _task_execution_mode_label(task: dict) -> str:
    return _task_contract_fields(task)["execution_mode"]


def _task_completion_summary(task: dict) -> tuple[str, str, str]:
    contract = _task_contract_fields(task)
    return (
        contract["completion_kind"],
        contract["completion_name"],
        contract["completion_target_summary"],
    )


def _task_runtime_updates(task: dict, *, last_completion_outcome: str = "", **overrides: object) -> dict:
    completion_kind, completion_name, completion_target_summary = _task_completion_summary(task)
    updates = {
        "execution_mode": _task_execution_mode_label(task),
        "execution_strategy": _task_execution_strategy(task),
        "completion_kind": completion_kind,
        "completion_name": completion_name,
        "completion_target_summary": completion_target_summary,
        "last_completion_outcome": str(last_completion_outcome or ""),
    }
    updates.update(overrides)
    return updates


def _gate_completion_attr(gate_result, name: str, default=None):
    try:
        gate_values = vars(gate_result)
    except TypeError:
        gate_values = {}

    completion_result = gate_values.get("completion_result")
    if completion_result is not None:
        try:
            completion_values = vars(completion_result)
        except TypeError:
            completion_values = {}
        if name in completion_values:
            return completion_values[name]

    metric_result = gate_values.get("metric")
    if metric_result is not None:
        try:
            metric_values = vars(metric_result)
        except TypeError:
            metric_values = {}
        if name in metric_values:
            return metric_values[name]

    return default


def _run_experiment_task(
    *,
    task: dict,
    data: dict,
    config: AutodevConfig,
    logger: Logger,
    result: RunResult,
    template: str,
    task_json_path: Path,
    progress_path: Path,
    main_log_path: Path,
    code_dir: Path,
    backend_name: str,
    heartbeat_interval: int,
    watch_dirs: list[Path],
    ignore_dirs: set[str],
    ignore_path_globs: list[str],
    include_path_globs: list[str],
    interrupted_flag,
) -> None:
    task_id, task_name = task_identity_text(task)
    completion, execution = normalize_task_contracts(task)
    metric_name = str(completion.get("name") or "metric").strip() or "metric"
    max_iterations = max(1, int(execution.get("max_iterations", 1) or 1))
    rollback_on_regression = normalize_bool(execution.get("rollback_on_failure"), default=True)
    keep_on_equal = normalize_bool(execution.get("keep_on_equal"), default=False)
    no_improvement_threshold = max(
        1,
        int(execution.get("stop_after_no_improvement", max_iterations) or max_iterations),
    )
    invalid_result_threshold = max(
        1,
        int(execution.get("stop_after_invalid", 1) or 1),
    )
    commit_prefix = str(execution.get("commit_prefix") or "experiment").strip() or "experiment"
    metric_goal_summary = (
        f"{metric_name}, direction={str(completion.get('direction') or '').strip() or 'unspecified'}, "
        f"min_improvement={float(completion.get('min_improvement', 0) or 0):g}, "
        f"unchanged_tolerance={float(completion.get('unchanged_tolerance', 0) or 0):g}"
    )
    git_available = is_git_repo(code_dir)
    current_data = data
    kept_count = 0
    reverted_count = 0
    invalid_total = 0
    no_improvement_streak = 0
    completed_iterations = 0
    best_changed_files: list[str] = []
    successful_attempt = 0
    baseline_metric: float | None = None
    best_metric: float | None = None
    last_metric_value: float | None = None
    last_outcome = ""

    def _load_latest_data() -> dict:
        nonlocal current_data
        current_data = _load_tasks_or_fallback(task_json_path, current_data)
        return current_data

    def _block(reason: str, *, gate_result=None, changed_files: list[str] | None = None) -> None:
        latest_data = _load_latest_data()
        if mark_task_blocked_in_file(task_json_path, task_id, reason):
            result.tasks_blocked += 1
            result.blocked_present = True
        else:
            logger.error(f"Failed to mark task {task_id} as blocked – check task.json")
            result.env_error = True
            return
        append_progress(
            progress_path,
            task_id,
            task_name,
            status="blocked",
            changed_files=changed_files,
            gate_result=gate_result,
            block_reason=reason,
            summary=reason,
        )
        latest_data = _load_latest_data()
        update_runtime_artifacts(
            config,
            latest_data,
            run_updates=_experiment_run_updates(
                status="waiting",
                message=f"Task {task_id} blocked; preparing next task",
                **_inactive_task_runtime_scope(),
                max_attempts=max_iterations,
                current_iteration=completed_iterations,
            ),
            event={
                "status": "blocked",
                "task_id": task_id,
                "message": reason,
            },
        )

    def _experiment_run_updates(**overrides: object) -> dict:
        updates = _task_runtime_updates(
            task,
            last_completion_outcome=last_outcome,
            current_iteration=0,
            max_iterations=max_iterations,
            baseline_metric=_format_metric_summary(metric_name, baseline_metric),
            best_metric=_format_metric_summary(metric_name, best_metric),
            last_metric=_format_metric_summary(metric_name, last_metric_value),
            last_outcome=last_outcome,
            kept_count=kept_count,
            reverted_count=reverted_count,
            no_improvement_streak=no_improvement_streak,
        )
        updates.update(overrides)
        return updates

    if not git_available:
        _block(
            "Experiment mode requires a git repository; commit-before-compare and rollback are mandatory"
        )
        return

    logger.info(f"Experiment mode: baseline + up to {max_iterations} iteration(s)")
    update_runtime_artifacts(
        config,
        current_data,
        run_updates=_experiment_run_updates(
            status="validating",
            message=f"Collecting experiment baseline for {task_id}",
            **_task_runtime_scope(
                task_id,
                task_name,
                max_attempts=max_iterations,
            ),
        ),
        event={
            "status": "validating",
            "task_id": task_id,
            "message": f"Collecting experiment baseline for {task_name}",
        },
    )
    baseline_gate_result = run_gate(
        task,
        config,
        [],
        code_dir,
        baseline_metric=None,
        best_before=None,
        enforce_change_requirements=False,
    )
    baseline_metric_result = baseline_gate_result.metric
    if (
        baseline_gate_result.status != "passed"
        or baseline_metric_result is None
        or baseline_metric_result.value is None
        or _completion_outcome_text(baseline_gate_result) == "invalid"
    ):
        reason = (
            "; ".join(baseline_gate_result.errors[:3])
            if baseline_gate_result.errors
            else (
                baseline_metric_result.details
                if baseline_metric_result is not None and baseline_metric_result.details
                else "Experiment baseline failed"
            )
        )
        _block(f"Experiment baseline failed: {reason}", gate_result=baseline_gate_result)
        return

    baseline_metric = baseline_metric_result.value
    best_metric = baseline_metric
    last_metric_value = baseline_metric
    last_outcome = "baseline"
    best_gate_result = baseline_gate_result
    logger.info(f"Experiment baseline {metric_name}={baseline_metric:g}")
    _append_experiment_log(
        config,
        {
            "task_id": task_id,
            "iteration": 0,
            "metric_name": metric_name,
            "baseline_value": baseline_metric,
            "best_before": None,
            "measured_value": baseline_metric,
            "outcome": "baseline",
            "commit_sha": "",
            "reverted_sha": "",
            "notes": baseline_metric_result.details,
        },
    )
    update_runtime_artifacts(
        config,
        current_data,
        run_updates=_experiment_run_updates(
            status="running",
            message=f"Collected experiment baseline for {task_id}",
            **_task_runtime_scope(
                task_id,
                task_name,
                max_attempts=max_iterations,
            ),
            current_iteration=0,
        ),
    )

    for iteration in range(1, max_iterations + 1):
        if interrupted_flag():
            logger.warning("Received interrupt signal, stopping")
            result.interrupted = True
            return

        current_data = _load_latest_data()
        recent_experiment_history = _read_recent_experiment_history(
            config,
            task_id=task_id,
            limit=5,
        )
        recent_git_history = read_recent_git_history(code_dir, limit=5)

        logger.state("running", f"Experiment {task_id} iteration {iteration}/{max_iterations}")
        snap_before = snapshot_directories(
            watch_dirs,
            ignore_dirs=ignore_dirs,
            ignore_path_globs=ignore_path_globs,
            include_path_globs=include_path_globs,
            relative_to=code_dir,
        )
        write_task_brief(
            Path(config.files.task_brief),
            task,
            config,
            attempt=iteration,
            max_attempts=max_iterations,
            execution_context=_experiment_execution_context(
                task,
                iteration=iteration,
                max_iterations=max_iterations,
                metric_name=metric_name,
                baseline_metric=baseline_metric,
                best_metric=best_metric,
                no_improvement_streak=no_improvement_streak,
                metric_goal_summary=metric_goal_summary,
            ),
        )
        prompt = render_prompt(
            template,
            task,
            config,
            project_learning_notes=get_recent_project_learning_summaries(
                current_data,
                limit=config.reflection.prompt_learning_limit,
            ),
            execution_context=_experiment_execution_context(
                task,
                iteration=iteration,
                max_iterations=max_iterations,
                metric_name=metric_name,
                baseline_metric=baseline_metric,
                best_metric=best_metric,
                no_improvement_streak=no_improvement_streak,
                metric_goal_summary=metric_goal_summary,
            ),
            recent_experiment_history=recent_experiment_history,
            recent_git_history=recent_git_history,
        )
        attempt_log = _attempt_log_path(config, task_id, iteration)
        hb = Heartbeat(
            logger=logger,
            task_id=task_id,
            attempt=iteration,
            max_attempts=max_iterations,
            log_file=attempt_log,
            interval=heartbeat_interval,
            on_heartbeat=lambda elapsed, output_updating, task_snapshot=current_data, task_id=task_id, task_name=task_name, iteration=iteration, attempt_log=attempt_log: update_runtime_artifacts(
                config,
                task_snapshot,
                run_updates=_experiment_run_updates(
                    status="running",
                    message=(
                        f"Experiment {task_id} is streaming model output"
                        if output_updating
                        else f"Experiment {task_id} is waiting for model output"
                    ),
                    current_task_id=task_id,
                    current_task_title=task_name,
                    current_attempt=iteration,
                    max_attempts=max_iterations,
                    attempt_log=str(attempt_log),
                    heartbeat_elapsed_seconds=elapsed,
                    current_iteration=iteration,
                ),
            ),
        )
        hb.start()
        update_runtime_artifacts(
            config,
            current_data,
            run_updates=_experiment_run_updates(
                status="running",
                message=f"Executing experiment iteration {iteration} for {task_id}",
                current_task_id=task_id,
                current_task_title=task_name,
                current_attempt=iteration,
                max_attempts=max_iterations,
                attempt_log=str(attempt_log),
                heartbeat_elapsed_seconds=0,
                current_iteration=iteration,
            ),
            event={
                "status": "running",
                "task_id": task_id,
                "message": f"Started experiment iteration {iteration}/{max_iterations}: {task_name}",
            },
        )
        started_at = time.monotonic()
        try:
            backend_result: BackendResult = run_backend(
                backend_name,
                prompt,
                config,
                code_dir,
                attempt_log,
                main_log_path,
            )
        except Exception as exc:
            hb.stop()
            logger.error(f"Backend error: {exc}")
            backend_result = BackendResult(exit_code=1, log_file=attempt_log)

        hb.stop()
        logger.state(
            "validating",
            f"Backend finished: task={task_id} iteration={iteration}/{max_iterations} "
            f"exit={backend_result.exit_code} tee_exit={backend_result.tee_exit}",
        )
        update_runtime_artifacts(
            config,
            current_data,
            run_updates=_experiment_run_updates(
                status="validating",
                message=f"Validating experiment iteration {iteration} for {task_id}",
                current_task_id=task_id,
                current_task_title=task_name,
                current_attempt=iteration,
                max_attempts=max_iterations,
                attempt_log=str(attempt_log),
                current_iteration=iteration,
            ),
        )

        snap_after = snapshot_directories(
            watch_dirs,
            ignore_dirs=ignore_dirs,
            ignore_path_globs=ignore_path_globs,
            include_path_globs=include_path_globs,
            relative_to=code_dir,
        )
        raw_changed_files = diff_snapshots(snap_before, snap_after)
        changed_files = _filter_runtime_changed_files(raw_changed_files, config, code_dir)
        ignored_runtime_changes = len(raw_changed_files) - len(changed_files)
        if ignored_runtime_changes > 0:
            logger.info(f"Ignored {ignored_runtime_changes} autodev runtime artifact change(s)")
        if changed_files:
            logger.changed_files_summary(
                changed_files, config.verification.changed_files_preview_limit
            )

        if backend_result.exit_code == 130 or backend_result.tee_exit == 130:
            logger.warning("Received interrupt signal, stopping")
            result.interrupted = True
            return
        if backend_result.tee_exit != 0:
            logger.error(
                f"Log write failure (tee_exit={backend_result.tee_exit}), check disk space and permissions"
            )
            result.env_error = True
            return
        if backend_result.exit_code != 0 and has_env_error(attempt_log, config.env_errors.halt_patterns):
            logger.error("Detected environment/permission error – stopping (task NOT marked blocked)")
            logger.warning(
                "Fix the runtime environment and retry. Check API config, permission mode, and writable directories."
            )
            result.env_error = True
            return

        commit_sha = None
        reverted_sha = None
        best_before_metric = best_metric
        if changed_files:
            commit_sha = create_experiment_commit(
                code_dir,
                task_id,
                task_name,
                changed_files,
                commit_prefix=commit_prefix,
                logger=logger,
            )
            if commit_sha is None:
                _block(
                    (
                        f"Experiment iteration {iteration} could not create an experiment commit; "
                        "commit-before-compare is required"
                    ),
                    changed_files=changed_files,
                )
                return

        gate_result = None
        metric_result = None
        outcome = "invalid"
        summary = f"Experiment iteration {iteration} failed with exit={backend_result.exit_code}"

        if backend_result.exit_code == 0:
            if not changed_files:
                outcome = "invalid"
                summary = (
                    f"Experiment iteration {iteration}: backend succeeded but produced "
                    "no file changes; skipping gate (commit-before-compare contract)"
                )
            else:
                gate_result = run_gate(
                    task,
                    config,
                    changed_files,
                    code_dir,
                    baseline_metric=baseline_metric,
                    best_before=best_before_metric,
                )
                metric_result = gate_result.metric
                completion_outcome = _completion_outcome_text(gate_result)
                completion_details = str(_gate_completion_attr(gate_result, "details", "") or "")
                if gate_result.status != "passed":
                    logger.warning(f"Experiment verification failed for task {task_id}")
                    for err in gate_result.errors:
                        logger.warning(f"  - {err}")
                    outcome = completion_outcome or "invalid"
                    summary = "; ".join(gate_result.errors[:3]) if gate_result.errors else (completion_details or summary)
                elif metric_result is None or metric_result.value is None or completion_outcome == "invalid":
                    outcome = "invalid"
                    summary = completion_details or (
                        metric_result.details
                        if metric_result is not None and metric_result.details
                        else "Experiment metric result missing"
                    )
                else:
                    outcome = completion_outcome or (metric_result.outcome or "measured")
                    summary = (
                        f"Experiment iteration {iteration}: {metric_name}={metric_result.value:g} ({outcome})"
                    )

        revert_required = outcome == "unchanged" and not keep_on_equal
        block_after_iteration_reason = ""
        if outcome in {"invalid", "regressed"} and changed_files:
            if rollback_on_regression:
                revert_required = True
            else:
                block_after_iteration_reason = (
                    f"Experiment iteration {iteration} ended {outcome} with rollback_on_regression=false; "
                    "non-retained changes require manual review"
                )
        if revert_required and changed_files:
            if not commit_sha:
                _block(
                    f"Experiment iteration {iteration} has changes but no experiment commit was created before {outcome}",
                    gate_result=gate_result,
                    changed_files=changed_files,
                )
                return
            reverted_sha = revert_commit(code_dir, commit_sha, logger=logger)
            if reverted_sha is None:
                _block(
                    f"Experiment iteration {iteration} could not be reverted after {outcome}",
                    gate_result=gate_result,
                    changed_files=changed_files,
                )
                return
            reverted_count += 1

        if metric_result is not None and metric_result.value is not None:
            last_metric_value = metric_result.value
        last_outcome = outcome

        if outcome == "improved" and metric_result is not None and metric_result.value is not None:
            best_metric = metric_result.value
            best_gate_result = gate_result
            best_changed_files = changed_files
            successful_attempt = iteration
            kept_count += 1
            no_improvement_streak = 0
        elif outcome == "unchanged":
            if keep_on_equal and metric_result is not None and metric_result.value is not None:
                best_metric = metric_result.value
                best_gate_result = gate_result
                best_changed_files = changed_files
                successful_attempt = iteration
                kept_count += 1
                no_improvement_streak = 0
            else:
                no_improvement_streak += 1
        elif outcome == "regressed":
            no_improvement_streak += 1
        else:
            invalid_total += 1

        completed_iterations += 1
        _append_experiment_log(
            config,
            {
                "task_id": task_id,
                "iteration": iteration,
                "metric_name": metric_name,
                "baseline_value": baseline_metric,
                "best_before": best_before_metric,
                "measured_value": metric_result.value if metric_result is not None else None,
                "outcome": outcome,
                "commit_sha": commit_sha or "",
                "reverted_sha": reverted_sha or "",
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                "notes": summary,
            },
        )
        update_runtime_artifacts(
            config,
            current_data,
            run_updates=_experiment_run_updates(
                status="running",
                message=summary,
                current_task_id=task_id,
                current_task_title=task_name,
                current_attempt=iteration,
                max_attempts=max_iterations,
                attempt_log=str(attempt_log),
                heartbeat_elapsed_seconds=0,
                current_iteration=iteration,
            ),
        )

        if block_after_iteration_reason:
            _block(
                block_after_iteration_reason,
                gate_result=gate_result,
                changed_files=changed_files,
            )
            return
        if invalid_total >= invalid_result_threshold:
            _block(
                f"Experiment exceeded invalid-result threshold after iteration {iteration}: {summary}",
                gate_result=gate_result,
                changed_files=changed_files,
            )
            return
        if no_improvement_streak >= no_improvement_threshold:
            logger.info(
                f"Stopping experiment after {no_improvement_streak} non-improving iteration(s)"
            )
            break
        if iteration < max_iterations:
            time.sleep(config.run.delay_between_tasks)

    if kept_count == 0:
        _block(
            f"Experiment completed {completed_iterations} iteration(s) with no improvements kept "
            f"(reverted={reverted_count}, invalid={invalid_total})",
            gate_result=best_gate_result,
            changed_files=best_changed_files,
        )
        return

    latest_data = _load_latest_data()
    if not mark_task_passed(latest_data, task_id):
        logger.error(f"Failed to mark task {task_id} as completed – check task.json")
        result.env_error = True
        return

    latest_task = find_task_in_data(latest_data, task_id) or task
    result.tasks_completed += 1
    logger.state("completed", f"Task {task_id} completed")
    success_summary = (
        f"Experiment completed with baseline {metric_name}={baseline_metric:g}; "
        f"best {metric_name}={best_metric:g}; iterations={completed_iterations}; "
        f"kept={kept_count}; reverted={reverted_count}; invalid={invalid_total}"
    )
    success_learning_notes = [
        f"Experiment baseline {metric_name}={baseline_metric:g}; best kept {metric_name}={best_metric:g}.",
        f"Experiment outcomes kept={kept_count}, reverted={reverted_count}, invalid={invalid_total}.",
    ]
    append_task_notes(
        latest_task,
        "learning_notes",
        success_learning_notes,
        max_entries=config.reflection.max_learning_notes,
    )
    record_iteration_history(
        latest_data,
        task_id,
        attempt=successful_attempt or completed_iterations or 1,
        status="completed",
        backend_exit_code=0,
        changed_files=best_changed_files,
        summary=success_summary,
        verification_errors=[],
        max_attempt_history_entries=config.reflection.max_attempt_history_entries,
        max_project_learning_entries=config.reflection.max_project_learning_entries,
        learning_notes=success_learning_notes,
    )
    save_tasks(task_json_path, latest_data)
    append_progress(
        progress_path,
        task_id,
        task_name,
        status="completed",
        changed_files=best_changed_files,
        gate_result=best_gate_result,
        summary=success_summary,
        learning_notes=success_learning_notes,
    )
    update_runtime_artifacts(
        config,
        latest_data,
        run_updates=_experiment_run_updates(
            status="waiting",
            message=f"Completed {task_id}; preparing next task",
            current_task_id="",
            current_task_title="",
            current_attempt=0,
            max_attempts=max_iterations,
            heartbeat_elapsed_seconds=0,
            attempt_log="",
            current_iteration=completed_iterations,
        ),
        event={
            "status": "completed",
            "task_id": task_id,
            "message": f"Experiment completed: {task_name}",
        },
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(
    config: AutodevConfig,
    logger: Logger,
    *,
    dry_run: bool = False,
    epochs: int | None = None,
) -> RunResult:
    """Execute the main automation loop."""
    result = RunResult()

    # -- Signal handling: SIGINT / SIGTERM → graceful stop -------------------
    interrupted = False

    def _on_signal(signum: int, frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    prev_sigint = signal.signal(signal.SIGINT, _on_signal)
    prev_sigterm = signal.signal(signal.SIGTERM, _on_signal)

    try:
        result = _run_epochs(
            config,
            logger,
            dry_run=dry_run,
            interrupted_flag=lambda: interrupted,
            epochs=epochs,
        )
    finally:
        signal.signal(signal.SIGINT, prev_sigint)
        signal.signal(signal.SIGTERM, prev_sigterm)

    return result


def _run_epochs(
    config: AutodevConfig,
    logger: Logger,
    *,
    dry_run: bool,
    interrupted_flag,
    epochs: int | None,
) -> RunResult:
    """Run one or more workflow epochs."""
    overall = RunResult()
    max_epochs = max(1, int(epochs if epochs is not None else config.run.max_epochs))
    overall.max_epochs = max_epochs

    if dry_run and max_epochs > 1:
        logger.warning("DRY RUN only previews one epoch; forcing epochs=1")
        max_epochs = 1
        overall.max_epochs = 1

    for epoch in range(1, max_epochs + 1):
        overall.current_epoch = epoch
        if interrupted_flag():
            overall.interrupted = True
            break

        if max_epochs > 1:
            logger.info("")
            logger.info(f"Workflow epoch {epoch}/{max_epochs}")

        epoch_result = _run_loop(
            config,
            logger,
            dry_run=dry_run,
            interrupted_flag=interrupted_flag,
            current_epoch=epoch,
            max_epochs=max_epochs,
        )
        overall.tasks_attempted += epoch_result.tasks_attempted
        overall.tasks_completed += epoch_result.tasks_completed
        overall.tasks_blocked += epoch_result.tasks_blocked
        overall.pending_remaining = epoch_result.pending_remaining

        if epoch_result.interrupted or epoch_result.env_error:
            overall.interrupted = epoch_result.interrupted
            overall.env_error = epoch_result.env_error
            overall.blocked_present = epoch_result.blocked_present
            break

        task_json_path = Path(config.files.task_json)
        try:
            current_data = load_tasks(task_json_path)
        except (FileNotFoundError, ValueError) as exc:
            logger.error(f"Error reading tasks after epoch {epoch}: {exc}")
            overall.env_error = True
            break

        counts = get_task_counts(current_data)
        overall.pending_remaining = counts["pending"]

        if epoch >= max_epochs:
            overall.blocked_present = counts["blocked"] > 0
            break

        if counts["pending"] > 0:
            logger.info(
                f"Epoch {epoch} ended with {counts['pending']} pending task(s); "
                "continuing the current queue in the next epoch"
            )
            continue

        from autodev.plan import ReplanUnavailableError, replan_tasks_for_next_epoch

        logger.state("waiting", f"Replanning remaining work for epoch {epoch + 1}/{max_epochs}")
        try:
            next_data = replan_tasks_for_next_epoch(
                current_data,
                config,
                epoch=epoch,
            )
        except ReplanUnavailableError as exc:
            logger.warning(
                f"Skipping epoch {epoch + 1}/{max_epochs} replanning: {exc} "
                "Set run.max_epochs=1 or regenerate task.json with autodev plan to enable replanning."
            )
            overall.blocked_present = counts["blocked"] > 0
            break
        except RuntimeError as exc:
            logger.error(f"Failed to replan for next epoch: {exc}")
            overall.env_error = True
            break

        next_counts = get_task_counts(next_data)
        if next_counts["total"] == 0:
            logger.success("Replanning produced no further tasks; workflow is complete")
            overall.blocked_present = False
            overall.pending_remaining = 0
            break

        logger.info(
            f"Prepared epoch {epoch + 1}/{max_epochs} with {next_counts['total']} task(s)"
        )

    return overall


def _run_loop(
    config: AutodevConfig,
    logger: Logger,
    *,
    dry_run: bool,
    interrupted_flag,
    current_epoch: int = 1,
    max_epochs: int = 1,
) -> RunResult:
    result = RunResult()
    result.current_epoch = current_epoch
    result.max_epochs = max_epochs
    backend_name = config.backend.default
    task_json_path = Path(config.files.task_json)
    progress_path = Path(config.files.progress)
    task_brief_path = Path(config.files.task_brief)
    main_log_path = Path(config.files.log_dir) / "autodev.log"
    code_dir = Path(config.project.code_dir)
    max_retries = config.run.max_retries
    max_tasks = config.run.max_tasks
    heartbeat_interval = config.run.heartbeat_interval
    halt_patterns = config.env_errors.halt_patterns
    ignore_dirs = set(config.snapshot.ignore_dirs)
    ignore_path_globs = list(config.snapshot.ignore_path_globs)
    include_path_globs = list(config.snapshot.include_path_globs)
    watch_dirs = [
        Path(watch_dir) for watch_dir in config.snapshot.watch_dirs
    ] or [code_dir]

    # -- Pre-flight checks --------------------------------------------------
    if is_root():
        logger.warning("Running as root – adjusting configuration")
        adjust_config_for_root(config)

    prereq_errors = check_prerequisites(backend_name)
    if prereq_errors:
        for err in prereq_errors:
            logger.error(err)
        result.env_error = True
        return result

    # -- Ensure log directory exists ----------------------------------------
    main_log_path.parent.mkdir(parents=True, exist_ok=True)

    # -- Optionally reset tasks at start ------------------------------------
    if config.run.reset_tasks_on_start and current_epoch == 1:
        data = load_tasks(task_json_path)
        changed = reset_tasks(data)
        if changed:
            save_tasks(task_json_path, data)
            logger.info(f"Reset {changed} task field(s) to pending")

    # -- Load prompt template once ------------------------------------------
    template = load_template(config)

    try:
        startup_data = load_tasks(task_json_path)
    except (FileNotFoundError, ValueError) as exc:
        logger.error(f"Error reading tasks: {exc}")
        result.env_error = True
        return result

    logger.banner(config.project.name)
    logger.info(f"Backend: {backend_name}")
    logger.info(f"Max tasks: {max_tasks}, Max retries per task: {max_retries}")
    if dry_run:
        logger.warning("DRY RUN – no backend will be invoked")
    logger.info("")
    write_idle_task_brief(task_brief_path)

    update_runtime_artifacts(
        config,
        startup_data,
        run_updates={
            "status": "starting",
            "dry_run": dry_run,
            "started_at": _timestamp_utc(),
            "finished_at": "",
            "current_epoch": current_epoch,
            "max_epochs": max_epochs,
            "message": "Run initialized",
            "current_task_id": "",
            "current_task_title": "",
            "current_attempt": 0,
            "max_attempts": max_retries,
            "attempt_log": "",
            "heartbeat_elapsed_seconds": 0,
            **default_run_contract_fields(),
        },
        event={
            "status": "starting",
            "message": f"Run started with backend {backend_name}",
        },
    )

    # -- Main task loop -----------------------------------------------------
    dry_run_seen: set[str] = set()

    for task_number in range(1, max_tasks + 1):
        if interrupted_flag():
            logger.warning("Received interrupt signal, stopping")
            result.interrupted = True
            break

        # Read task file fresh each iteration (the backend may have mutated it)
        try:
            data = load_tasks(task_json_path)
        except (FileNotFoundError, ValueError) as exc:
            logger.error(f"Error reading tasks: {exc}")
            result.env_error = True
            break

        counts = get_task_counts(data)
        if counts["blocked"] > 0:
            result.blocked_present = True

        logger.queue_summary(
            total=counts["total"],
            completed=counts["completed"],
            blocked=counts["blocked"],
            pending=counts["pending"],
            running=0,
        )

        task = get_next_task(data)
        if task is None:
            idle_status, idle_message = _idle_run_status_from_counts(counts)
            logger.success(
                f"All tasks done! "
                f"(completed={counts['completed']}, blocked={counts['blocked']}, "
                f"total={counts['total']})"
            )
            update_runtime_artifacts(
                config,
                data,
                run_updates={
                    "status": idle_status,
                    "message": idle_message,
                    "finished_at": _timestamp_utc(),
                    "current_task_id": "",
                    "current_task_title": "",
                    "current_attempt": 0,
                    "heartbeat_elapsed_seconds": 0,
                    "attempt_log": "",
                    **default_run_contract_fields(),
                },
            )
            write_idle_task_brief(task_brief_path)
            break

        task_id, task_name = task_identity_text(task)
        result.tasks_attempted += 1

        logger.task_header(task_number, task_id, task_name)
        logger.state("pending", f"Queued task {task_id}: {task_name}")

        if dry_run:
            if task_id in dry_run_seen:
                logger.info("[DRY RUN] All pending tasks shown")
                break
            dry_run_seen.add(task_id)
            write_task_brief(
                task_brief_path,
                task,
                config,
                attempt=1,
                max_attempts=max_retries,
            )
            prompt = render_prompt(
                template,
                task,
                config,
                project_learning_notes=get_recent_project_learning_summaries(
                    data,
                    limit=config.reflection.prompt_learning_limit,
                ),
            )
            logger.state(
                "running",
                f"[DRY RUN] Would send prompt ({len(prompt)} chars) to {backend_name}",
            )
            logger.info(f"[DRY RUN] Prompt preview:\n{prompt[:500]}...")
            update_runtime_artifacts(
                config,
                data,
                run_updates=_task_runtime_updates(
                    task,
                    status="running",
                    message=f"Dry run preview for {task_id}",
                    **_task_runtime_scope(
                        task_id,
                        task_name,
                        attempt=1,
                        max_attempts=max_retries,
                    ),
                ),
                event={
                    "status": "running",
                    "task_id": task_id,
                    "message": f"Dry run preview for {task_name}",
                },
            )
            continue

        if _task_execution_strategy(task) == "iterative":
            _run_experiment_task(
                task=task,
                data=data,
                config=config,
                logger=logger,
                result=result,
                template=template,
                task_json_path=task_json_path,
                progress_path=progress_path,
                main_log_path=main_log_path,
                code_dir=code_dir,
                backend_name=backend_name,
                heartbeat_interval=heartbeat_interval,
                watch_dirs=watch_dirs,
                ignore_dirs=ignore_dirs,
                ignore_path_globs=ignore_path_globs,
                include_path_globs=include_path_globs,
                interrupted_flag=interrupted_flag,
            )
            if result.interrupted or result.env_error:
                break
            logger.info("")
            if not result.interrupted:
                time.sleep(config.run.delay_between_tasks)
            continue

        # -- Retry loop with circuit breaker --------------------------------
        task_success = False
        successful_attempt = 0
        success_changed_files: list[str] = []
        success_gate_result = None
        cb = CircuitBreaker(config.circuit_breaker, logger)

        for attempt in range(1, max_retries + 1):
            if interrupted_flag():
                logger.warning("Received interrupt signal, stopping")
                result.interrupted = True
                break

            # Circuit breaker pre-check
            if cb.is_tripped:
                logger.warning(
                    f"Circuit breaker open — skipping remaining retries "
                    f"({cb.trip_reason})"
                )
                break

            logger.state("running", f"Task {task_id} attempt {attempt}/{max_retries}")

            # Snapshot before
            snap_before = snapshot_directories(
                watch_dirs,
                ignore_dirs=ignore_dirs,
                ignore_path_globs=ignore_path_globs,
                include_path_globs=include_path_globs,
                relative_to=code_dir,
            )

            # Build prompt
            write_task_brief(
                task_brief_path,
                task,
                config,
                attempt=attempt,
                max_attempts=max_retries,
            )
            prompt = render_prompt(
                template,
                task,
                config,
                project_learning_notes=get_recent_project_learning_summaries(
                    data,
                    limit=config.reflection.prompt_learning_limit,
                ),
            )

            # Build attempt log path
            attempt_log = _attempt_log_path(config, task_id, attempt)

            # Start heartbeat
            hb = Heartbeat(
                logger=logger,
                task_id=task_id,
                attempt=attempt,
                max_attempts=max_retries,
                log_file=attempt_log,
                interval=heartbeat_interval,
                on_heartbeat=lambda elapsed, output_updating, task_snapshot=data, task_id=task_id, task_name=task_name, attempt=attempt, attempt_log=attempt_log, task_contract=task: update_runtime_artifacts(
                    config,
                    task_snapshot,
                    run_updates=_task_runtime_updates(
                        task_contract,
                        status="running",
                        message=(
                            f"Task {task_id} is streaming model output"
                            if output_updating
                            else f"Task {task_id} is waiting for model output"
                        ),
                        **_task_runtime_scope(
                            task_id,
                            task_name,
                            attempt=attempt,
                            max_attempts=max_retries,
                            attempt_log=str(attempt_log),
                            heartbeat_elapsed_seconds=elapsed,
                        ),
                    ),
                ),
            )
            hb.start()
            update_runtime_artifacts(
                config,
                data,
                run_updates=_task_runtime_updates(
                    task,
                    status="running",
                    message=f"Executing {task_id}: {task_name}",
                    **_task_runtime_scope(
                        task_id,
                        task_name,
                        attempt=attempt,
                        max_attempts=max_retries,
                        attempt_log=str(attempt_log),
                    ),
                ),
                event={
                    "status": "running",
                    "task_id": task_id,
                    "message": f"Started attempt {attempt}/{max_retries}: {task_name}",
                },
            )

            # Execute backend
            logger.state(
                "running",
                f"Started {backend_name}: task={task_id} attempt={attempt}/{max_retries}",
            )
            try:
                backend_result: BackendResult = run_backend(
                    backend_name,
                    prompt,
                    config,
                    code_dir,
                    attempt_log,
                    main_log_path,
                )
            except Exception as exc:
                hb.stop()
                logger.error(f"Backend error: {exc}")
                backend_result = BackendResult(exit_code=1, log_file=attempt_log)

            hb.stop()
            logger.state(
                "validating",
                f"Backend finished: task={task_id} attempt={attempt}/{max_retries} "
                f"exit={backend_result.exit_code} tee_exit={backend_result.tee_exit}"
            )
            update_runtime_artifacts(
                config,
                data,
                run_updates=_task_runtime_updates(
                    task,
                    status="validating",
                    message=f"Validating outputs for {task_id}",
                    **_task_runtime_scope(
                        task_id,
                        task_name,
                        attempt=attempt,
                        max_attempts=max_retries,
                        attempt_log=str(attempt_log),
                    ),
                ),
            )

            # Snapshot after
            snap_after = snapshot_directories(
                watch_dirs,
                ignore_dirs=ignore_dirs,
                ignore_path_globs=ignore_path_globs,
                include_path_globs=include_path_globs,
                relative_to=code_dir,
            )
            raw_changed_files = diff_snapshots(snap_before, snap_after)
            changed_files = _filter_runtime_changed_files(raw_changed_files, config, code_dir)
            ignored_runtime_changes = len(raw_changed_files) - len(changed_files)
            if ignored_runtime_changes > 0:
                logger.info(
                    f"Ignored {ignored_runtime_changes} autodev runtime artifact change(s)"
                )
            if changed_files:
                logger.changed_files_summary(
                    changed_files, config.verification.changed_files_preview_limit
                )

            # Check for interrupt (exit code 130)
            if backend_result.exit_code == 130 or backend_result.tee_exit == 130:
                logger.warning("Received interrupt signal, stopping")
                result.interrupted = True
                break

            # Check for tee/log failure
            if backend_result.tee_exit != 0:
                logger.error(
                    f"Log write failure (tee_exit={backend_result.tee_exit}), "
                    "check disk space and permissions"
                )
                result.env_error = True
                break

            # Check for environment / permission errors
            if backend_result.exit_code != 0:
                if has_env_error(attempt_log, halt_patterns):
                    logger.error(
                        "Detected environment/permission error – stopping "
                        "(task NOT marked blocked)"
                    )
                    logger.warning(
                        "Fix the runtime environment and retry. "
                        "Check API config, permission mode, and writable directories."
                    )
                    result.env_error = True
                    break

            # On backend success, run task completion verification
            gate_result = None
            backend_failed = backend_result.exit_code != 0
            if not backend_failed:
                gate_result = run_gate(task, config, changed_files, code_dir)
                if gate_result.status != "passed":
                    logger.warning(f"Verification failed for task {task_id}")
                    for err in gate_result.errors:
                        logger.warning(f"  - {err}")
                    backend_result = BackendResult(
                        exit_code=99,
                        log_file=attempt_log,
                        tee_exit=backend_result.tee_exit,
                    )

            # Feed result to circuit breaker (may pause on real backend rate limits)
            cb.record_attempt(
                exit_code=backend_result.exit_code,
                changed_files_count=len(changed_files),
                attempt_log=attempt_log,
                allow_rate_limit_pause=backend_failed,
            )

            # If backend succeeded (and verification passed)
            if backend_result.exit_code == 0:
                task_success = True
                successful_attempt = attempt
                success_changed_files = changed_files
                success_gate_result = gate_result
                break

            failure_summary = (
                "; ".join(gate_result.errors[:3])
                if gate_result is not None and gate_result.errors
                else f"Attempt {attempt} failed with exit={backend_result.exit_code}"
            )
            failure_learning_notes: list[str] = []

            latest_data = _load_tasks_or_fallback(task_json_path, data)
            latest_task = find_task_in_data(latest_data, task_id) or task

            should_refine = (
                config.reflection.enabled
                and normalize_int(latest_task.get("refinement_count", 0), default=0)
                < config.reflection.max_refinements_per_task
                and not (
                    backend_failed
                    and _attempt_looks_rate_limited(
                        attempt_log,
                        config.circuit_breaker.rate_limit_patterns,
                    )
                )
                and (
                    bool(changed_files)
                    or backend_result.exit_code == 99
                    or bool(gate_result and gate_result.errors)
                )
            )

            if should_refine:
                logger.state(
                    "waiting",
                    f"Reflecting on failed attempt {attempt}/{max_retries} for {task_id}",
                )
                update_runtime_artifacts(
                    config,
                    latest_data,
                    run_updates=_task_runtime_updates(
                        task,
                        last_completion_outcome=_completion_outcome_text(gate_result),
                        status="waiting",
                        message=f"Refining task guidance for {task_id}",
                        **_task_runtime_scope(
                            task_id,
                            task_name,
                            attempt=attempt,
                            max_attempts=max_retries,
                            attempt_log=str(attempt_log),
                        ),
                    ),
                )
                try:
                    reflection = reflect_failed_attempt(
                        task=latest_task,
                        config=config,
                        attempt=attempt,
                        max_retries=max_retries,
                        backend_exit_code=backend_result.exit_code,
                        changed_files=changed_files,
                        verification_errors=gate_result.errors if gate_result is not None else [],
                        attempt_log=attempt_log,
                    )
                    if apply_task_reflection(
                        latest_data,
                        task_id,
                        reflection,
                        max_learning_notes=config.reflection.max_learning_notes,
                    ):
                        failure_summary = reflection.summary or failure_summary
                        failure_learning_notes = reflection.learning_notes
                        logger.info(f"Updated task guidance for {task_id} after failed attempt")
                except RuntimeError as exc:
                    logger.warning(f"Task reflection skipped: {exc}")

            record_iteration_history(
                latest_data,
                task_id,
                attempt=attempt,
                status="failed",
                backend_exit_code=backend_result.exit_code,
                changed_files=changed_files,
                summary=failure_summary,
                verification_errors=gate_result.errors if gate_result is not None else [],
                max_attempt_history_entries=config.reflection.max_attempt_history_entries,
                max_project_learning_entries=config.reflection.max_project_learning_entries,
                learning_notes=failure_learning_notes,
            )

            if failure_learning_notes:
                refreshed_task = find_task_in_data(latest_data, task_id)
                if refreshed_task is not None:
                    append_task_notes(
                        refreshed_task,
                        "learning_notes",
                        failure_learning_notes,
                        max_entries=config.reflection.max_learning_notes,
                    )

            save_tasks(task_json_path, latest_data)
            data = latest_data
            refreshed_task = find_task_in_data(latest_data, task_id)
            if refreshed_task is not None:
                task = refreshed_task

            append_progress(
                progress_path,
                task_id,
                task_name,
                status="failed",
                changed_files=changed_files,
                gate_result=gate_result if changed_files else None,
                summary=failure_summary,
                learning_notes=failure_learning_notes,
            )

            # Log failed attempt
            logger.state(
                "retry",
                f"Task {task_id} attempt {attempt} failed (exit={backend_result.exit_code})"
            )
            if attempt < max_retries:
                update_runtime_artifacts(
                    config,
                    data,
                    run_updates=_task_runtime_updates(
                        task,
                        last_completion_outcome=_completion_outcome_text(gate_result),
                        status="retry_wait",
                        message=(
                            f"Task {task_id} failed (exit={backend_result.exit_code}); "
                            f"waiting {config.run.delay_between_tasks}s before retry"
                        ),
                        **_task_runtime_scope(
                            task_id,
                            task_name,
                            attempt=attempt,
                            max_attempts=max_retries,
                            attempt_log=str(attempt_log),
                        ),
                    ),
                    event={
                        "status": "retry",
                        "task_id": task_id,
                        "message": (
                            f"Attempt {attempt}/{max_retries} failed "
                            f"(exit={backend_result.exit_code})"
                        ),
                    },
                )
            if attempt < max_retries:
                time.sleep(config.run.delay_between_tasks)

        # -- After retry loop ends ------------------------------------------
        if result.interrupted or result.env_error:
            latest_data = _load_tasks_or_fallback(task_json_path, data)
            final_status = "interrupted" if result.interrupted else "env_error"
            final_message = (
                "Run interrupted by signal"
                if result.interrupted
                else "Run stopped due to environment or permission error"
            )
            update_runtime_artifacts(
                config,
                latest_data,
                run_updates=_task_runtime_updates(
                    task,
                    last_completion_outcome=_completion_outcome_text(gate_result),
                    status=final_status,
                    message=final_message,
                    finished_at=_timestamp_utc(),
                    **_inactive_task_runtime_scope(),
                ),
            )
            break

        if not task_success:
            # Build block reason — include circuit breaker info if tripped
            if cb.is_tripped:
                block_reason = (
                    f"Circuit breaker: {cb.trip_reason} "
                    f"({_timestamp_utc()})"
                )
            else:
                block_reason = (
                    f"Automated execution failed: {backend_name} failed "
                    f"{max_retries} consecutive attempt(s) "
                    f"({_timestamp_utc()})"
                )
            if mark_task_blocked_in_file(task_json_path, task_id, block_reason):
                logger.state("blocked", f"Task {task_id} marked as blocked, continuing to next")
                result.tasks_blocked += 1
                result.blocked_present = True
            else:
                logger.error(f"Failed to mark task {task_id} as blocked – check task.json")
                result.env_error = True
                break

            append_progress(
                progress_path,
                task_id,
                task_name,
                status="blocked",
                block_reason=block_reason,
            )
            latest_data = _load_tasks_or_fallback(task_json_path, data)
            update_runtime_artifacts(
                config,
                latest_data,
                run_updates=_task_runtime_updates(
                    task,
                    last_completion_outcome=_completion_outcome_text(gate_result),
                    status="waiting",
                    message=f"Task {task_id} blocked; preparing next task",
                    **_inactive_task_runtime_scope(),
                ),
                event={
                    "status": "blocked",
                    "task_id": task_id,
                    "message": block_reason,
                },
            )
        else:
            latest_data = _load_tasks_or_fallback(task_json_path, data)
            if not mark_task_passed(latest_data, task_id):
                logger.error(f"Failed to mark task {task_id} as completed – check task.json")
                result.env_error = True
                break

            latest_task = find_task_in_data(latest_data, task_id) or task
            logger.state("completed", f"Task {task_id} completed")
            result.tasks_completed += 1
            success_summary, success_learning_notes = build_success_learning_notes(
                latest_task,
                success_changed_files,
                success_gate_result,
                attempt=successful_attempt or 1,
            )
            append_task_notes(
                latest_task,
                "learning_notes",
                success_learning_notes,
                max_entries=config.reflection.max_learning_notes,
            )
            record_iteration_history(
                latest_data,
                task_id,
                attempt=successful_attempt or 1,
                status="completed",
                backend_exit_code=0,
                changed_files=success_changed_files,
                summary=success_summary,
                verification_errors=[],
                max_attempt_history_entries=config.reflection.max_attempt_history_entries,
                max_project_learning_entries=config.reflection.max_project_learning_entries,
                learning_notes=success_learning_notes,
            )
            save_tasks(task_json_path, latest_data)
            append_progress(
                progress_path,
                task_id,
                task_name,
                status="completed",
                changed_files=success_changed_files,
                gate_result=success_gate_result if success_changed_files else None,
                summary=success_summary,
                learning_notes=success_learning_notes,
            )
            update_runtime_artifacts(
                config,
                latest_data,
                run_updates=_task_runtime_updates(
                    task,
                    last_completion_outcome=_completion_outcome_text(success_gate_result),
                    status="waiting",
                    message=f"Completed {task_id}; preparing next task",
                    **_inactive_task_runtime_scope(),
                ),
                event={
                    "status": "completed",
                    "task_id": task_id,
                    "message": f"Task completed: {task_name}",
                },
            )

            # Atomic git commit (GSD-inspired)
            auto_commit(code_dir, task_id, task_name, success_changed_files, config, logger)

        logger.info("")
        if not result.interrupted:
            time.sleep(config.run.delay_between_tasks)

    # -- Summary ------------------------------------------------------------
    logger.info("")
    logger.info(
        f"Run complete: attempted={result.tasks_attempted}, "
        f"completed={result.tasks_completed}, blocked={result.tasks_blocked}"
    )

    try:
        final_data = load_tasks(task_json_path)
    except (FileNotFoundError, ValueError):
        final_data = startup_data
    final_counts = get_task_counts(final_data)
    result.pending_remaining = final_counts["pending"]
    result.blocked_present = final_counts["blocked"] > 0
    final_status = "completed"
    final_message = "Run finished"
    if result.interrupted:
        final_status = "interrupted"
        final_message = "Run interrupted by signal"
    elif result.env_error:
        final_status = "env_error"
        final_message = "Run stopped due to environment or permission error"
    elif result.blocked_present:
        final_status = "blocked"
        final_message = "Run finished with blocked tasks"
    update_runtime_artifacts(
        config,
        final_data,
        run_updates={
            "status": final_status,
            "message": final_message,
            "finished_at": _timestamp_utc(),
            "current_epoch": current_epoch,
            "max_epochs": max_epochs,
            **_inactive_task_runtime_scope(),
            **default_run_contract_fields(),
        },
        event={
            "status": final_status,
            "message": final_message,
        },
    )

    return result
