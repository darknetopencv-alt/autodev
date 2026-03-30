"""Task.json CRUD operations for unattended AI-driven development.

Provides loading, saving, querying, and mutating task data with robust
error handling.  All logic is ported from the proven shell/Python scripts
in the AR-Translator automation toolchain.
"""

from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from autodev.task_state import (
    normalize_block_reason,
    normalize_bool,
    normalize_int,
    task_has_final_status as _task_has_final_status,
    task_lifecycle_status,
    task_matches_id,
)
from autodev.task_audit import (
    legacy_execution_mode_from_execution,
    legacy_experiment_from_contracts,
    normalize_task_contracts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [str(item).strip() for item in value]
    return [item for item in items if item]


def _normalize_history_list(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def ensure_task_defaults(task: dict) -> dict:
    """Normalize one task entry in-place and return it."""
    task["passes"] = normalize_bool(task.get("passes"), default=False)
    task["blocked"] = normalize_bool(task.get("blocked"), default=False)
    if task["blocked"]:
        # Blocked is terminal and should take precedence over stale pass flags.
        task["passes"] = False
    task["block_reason"] = normalize_block_reason(task.get("block_reason"))
    task["description"] = str(task.get("description", "") or "")
    task["steps"] = _normalize_str_list(task.get("steps"))
    task["docs"] = _normalize_str_list(task.get("docs"))
    task["output"] = _normalize_str_list(task.get("output"))
    task["implementation_notes"] = _normalize_str_list(task.get("implementation_notes"))
    task["verification_notes"] = _normalize_str_list(task.get("verification_notes"))
    task["learning_notes"] = _normalize_str_list(task.get("learning_notes"))
    task["attempt_history"] = _normalize_history_list(task.get("attempt_history"))
    task["last_reflection_summary"] = str(task.get("last_reflection_summary", "") or "")
    task["refinement_count"] = normalize_int(task.get("refinement_count", 0), default=0)

    verification = task.get("verification")
    if not isinstance(verification, dict):
        verification = task.get("gate", {})
    if not isinstance(verification, dict):
        verification = {}
    verification.pop("evidence_keys", None)
    task["verification"] = verification
    task.pop("gate", None)

    completion, execution = normalize_task_contracts(task)
    task["completion"] = completion
    task["execution"] = execution
    task["execution_mode"] = legacy_execution_mode_from_execution(execution)

    legacy_experiment = legacy_experiment_from_contracts(completion, execution)
    if legacy_experiment is not None:
        task["experiment"] = legacy_experiment
    else:
        task.pop("experiment", None)
    return task


def ensure_task_store_defaults(data: dict) -> dict:
    """Normalize task-store metadata and task runtime fields in-place."""
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        data["tasks"] = []
        tasks = data["tasks"]

    planning_source = data.get("planning_source")
    data["planning_source"] = planning_source if isinstance(planning_source, dict) else {}
    data["learning_journal"] = _normalize_history_list(data.get("learning_journal"))
    data["epoch_history"] = _normalize_history_list(data.get("epoch_history"))
    for task in tasks:
        if isinstance(task, dict):
            ensure_task_defaults(task)
    return data


def merge_unique_strings(existing: object, incoming: object) -> list[str]:
    """Return a stable unique list containing existing items followed by incoming ones."""
    merged: list[str] = []
    seen: set[str] = set()
    for item in _normalize_str_list(existing) + _normalize_str_list(incoming):
        if item in seen:
            continue
        seen.add(item)
        merged.append(item)
    return merged


def append_task_notes(
    task: dict,
    field_name: str,
    notes: list[str],
    *,
    max_entries: int,
) -> None:
    """Append unique notes to a task note field, keeping only recent entries."""
    merged = merge_unique_strings(task.get(field_name), notes)
    task[field_name] = merged[-max_entries:] if max_entries > 0 else merged


def append_task_attempt_history(
    task: dict,
    entry: dict,
    *,
    max_entries: int,
) -> None:
    """Append one structured attempt-history record to a task."""
    history = _normalize_history_list(task.get("attempt_history"))
    history.append(entry)
    task["attempt_history"] = history[-max_entries:] if max_entries > 0 else history


def append_project_learning(data: dict, entry: dict, *, max_entries: int) -> None:
    """Append one project-level learning journal entry."""
    journal = _normalize_history_list(data.get("learning_journal"))
    journal.append(entry)
    data["learning_journal"] = journal[-max_entries:] if max_entries > 0 else journal


def get_recent_project_learning_summaries(data: dict, *, limit: int) -> list[str]:
    """Return short learning-summary strings for prompt injection."""
    journal = _normalize_history_list(data.get("learning_journal"))
    summaries: list[str] = []
    for entry in journal[-limit:]:
        task_id = str(entry.get("task_id", "")).strip()
        summary = str(entry.get("summary", "")).strip()
        if not summary:
            continue
        summaries.append(f"{task_id}: {summary}" if task_id else summary)
    return summaries


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


@contextmanager
def _file_lock(path: Path):
    """Advisory file lock for concurrent task.json access.

    Uses ``fcntl`` on POSIX and ``msvcrt`` on Windows.  Falls back to a
    no-op context manager when neither is available.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = lock_path.open("w")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fd.close()


def load_tasks(path: Path) -> dict:
    """Read and parse a task JSON file.

    Uses ``utf-8-sig`` encoding to transparently handle a leading BOM.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist (with a human-friendly message).
    ValueError
        If the file content is not valid JSON.
    """
    with _file_lock(path):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            raise FileNotFoundError(f"Task file not found: {path}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Invalid task data in {path}: root value must be a JSON object")
    ensure_task_store_defaults(data)
    return data


def save_tasks(path: Path, data: dict) -> None:
    """Write *data* as pretty-printed JSON to *path*.

    The output uses ``ensure_ascii=False`` (preserving non-ASCII
    characters), two-space indentation, and a trailing newline, matching
    the canonical format of the existing task files.
    """
    with _file_lock(path):
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def backup_task_file(path: Path) -> Path:
    """Create a timestamped backup of *path* and return the backup path.

    The backup is named ``<original>.bak.<timestamp>`` where the
    timestamp follows the ``YYYYMMDDTHHMMSSZ`` UTC format -- for
    example ``task.json.bak.20260304T120000Z``.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_suffix(path.suffix + f".bak.{stamp}")
    shutil.copy2(path, backup_path)
    cleanup_old_backups(path)
    return backup_path


def cleanup_old_backups(path: Path, keep: int = 5) -> list[Path]:
    """Remove old backups of *path*, keeping the *keep* most recent.

    Returns the list of removed backup paths.
    """
    pattern = path.name + ".bak.*"
    backups = sorted(
        path.parent.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    for old in backups[keep:]:
        old.unlink(missing_ok=True)
        removed.append(old)
    return removed


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _task_matches_any_id(task: dict, task_ids: set[str] | None) -> bool:
    return task_ids is None or any(task_matches_id(task, task_id) for task_id in task_ids)


def _clear_blocked_state(task: dict) -> int:
    """Clear blocked-related fields and return the number of field mutations."""
    changed = 0
    blocked = normalize_bool(task.get("blocked"), default=False)
    if blocked:
        changed += 1
    if task.get("blocked") is not False:
        task["blocked"] = False
    if normalize_block_reason(task.get("block_reason"), strip=True):
        task["block_reason"] = ""
        changed += 1
    if str(task.get("blocked_at", "") or "").strip():
        task["blocked_at"] = ""
        changed += 1
    return changed


def find_task(tasks: list[dict], task_id: str) -> dict | None:
    """Return the first task whose ``id`` matches *task_id*.

    Both sides are compared as ``str()`` so that callers can pass either
    an ``int`` or a ``str`` without worrying about type mismatches.
    """
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task_matches_id(task, task_id):
            return task
    return None


def find_task_in_data(data: dict, task_id: str) -> dict | None:
    """Return the matching task from a full task-store payload."""
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return None
    return find_task(tasks, task_id)


def get_next_task(data: dict, include_blocked: bool = False) -> dict | None:
    """Return the first pending (``passes=false``) task.

    A task is skipped when it is blocked **unless** *include_blocked* is
    ``True``.  Returns ``None`` when no eligible task remains.
    """
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return None

    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = task_lifecycle_status(task)
        if status == "completed":
            continue
        if status == "blocked" and not include_blocked:
            continue
        return task

    return None


def get_task_counts(data: dict) -> dict:
    """Return aggregate counts from the task list.

    Returns a dict with keys ``total``, ``completed``, ``blocked``, and
    ``pending``.
    """
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return {"total": 0, "completed": 0, "blocked": 0, "pending": 0}

    total = 0
    completed = 0
    blocked = 0

    for task in tasks:
        if not isinstance(task, dict):
            continue
        total += 1
        status = task_lifecycle_status(task)
        if status == "completed":
            completed += 1
        elif status == "blocked":
            blocked += 1

    return {
        "total": total,
        "completed": completed,
        "blocked": blocked,
        "pending": total - completed - blocked,
    }


def task_has_final_status(task: dict | None) -> bool:
    """Return ``True`` when the task is either passed or blocked."""
    return _task_has_final_status(task)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def mark_task_passed(data: dict, task_id: str) -> bool:
    """Set ``passes=True`` on the task identified by *task_id*.

    Clears blocked state fields so the final task state is internally
    consistent, then updates embedded statistics.
    Returns ``True`` if the task was found, ``False`` otherwise.
    """
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return False

    task = find_task(tasks, task_id)
    if task is None:
        return False

    ensure_task_defaults(task)
    task["passes"] = True
    _clear_blocked_state(task)
    update_statistics(data)
    return True


def mark_task_blocked(data: dict, task_id: str, reason: str) -> bool:
    """Mark a task as blocked with a reason and UTC timestamp.

    Sets ``blocked=True``, ``block_reason=reason``,
    ``blocked_at=<ISO 8601 UTC>``, and forces ``passes=False``.
    Updates the embedded statistics block (if present) afterwards.
    Returns ``True`` if the task was found, ``False`` otherwise.
    """
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return False

    task = find_task(tasks, task_id)
    if task is None:
        return False

    ensure_task_defaults(task)
    task["blocked"] = True
    task["block_reason"] = normalize_block_reason(reason)
    task["blocked_at"] = datetime.now(timezone.utc).isoformat()
    task["passes"] = False
    update_statistics(data)
    return True


def load_task_context(path: Path, task_id: str) -> tuple[dict, dict | None]:
    """Load task data and return the matching task object if present."""
    data = load_tasks(path)
    return data, find_task_in_data(data, task_id)


def mark_task_blocked_in_file(path: Path, task_id: str, reason: str) -> bool:
    """Load, block, and persist a task in one operation."""
    data = load_tasks(path)
    if not mark_task_blocked(data, task_id, reason):
        return False
    save_tasks(path, data)
    return True


def reset_tasks(
    data: dict,
    task_ids: set[str] | None = None,
    clear_blocked: bool = True,
) -> int:
    """Reset selected (or all) tasks and return the number of field changes.

    For every matching task:

    * ``passes`` is set to ``False``.
    * If *clear_blocked* is ``True``: ``blocked`` is set to ``False``
      and ``block_reason`` / ``blocked_at`` are cleared.

    When *task_ids* is ``None`` every task is reset.  Returns the total
    number of individual field mutations applied.
    """
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return 0

    changed = 0

    for task in tasks:
        if not isinstance(task, dict):
            continue

        if not _task_matches_any_id(task, task_ids):
            continue

        if normalize_bool(task.get("passes"), default=False):
            changed += 1
        if task.get("passes") is not False:
            task["passes"] = False

        if clear_blocked:
            changed += _clear_blocked_state(task)

    update_statistics(data)
    return changed


def retry_blocked_tasks(data: dict, task_ids: set[str] | None = None) -> int:
    """Reset blocked tasks back to pending and return the task count changed.

    Only tasks with ``blocked=True`` are touched. Matching tasks have:

    * ``blocked`` set to ``False``
    * ``block_reason`` cleared
    * ``blocked_at`` cleared
    * ``passes`` forced to ``False``

    When *task_ids* is ``None`` every blocked task is eligible. Returns the
    number of blocked tasks moved back to pending.
    """
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return 0

    retried = 0

    for task in tasks:
        if not isinstance(task, dict):
            continue

        if not _task_matches_any_id(task, task_ids):
            continue
        if not normalize_bool(task.get("blocked"), default=False):
            continue

        task["passes"] = False
        _clear_blocked_state(task)
        retried += 1

    update_statistics(data)
    return retried


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def update_statistics(data: dict) -> None:
    """Recalculate ``statistics.completed`` and ``statistics.blocked``.

    Only mutates *data* when a ``statistics`` key already exists (or
    when the data contains tasks, in which case a ``statistics`` dict is
    ensured).
    """
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return

    completed = 0
    blocked = 0

    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = task_lifecycle_status(task)
        if status == "completed":
            completed += 1
        elif status == "blocked":
            blocked += 1

    stats = data.get("statistics")
    if stats is None:
        stats = {}
        data["statistics"] = stats

    stats["completed"] = completed
    stats["blocked"] = blocked
