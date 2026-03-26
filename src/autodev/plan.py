"""Intent or PRD → task.json generation via configured AI CLI backend.

Inspired by prd-breakdown-execute and GSD's spec-driven approach.
Uses the configured backend CLI to turn developer intent or a PRD
into structured task.json entries, closing the gap between
"I have an idea" and "I have tasks ready for automation".
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autodev.config import AutodevConfig

from autodev.backends import build_backend_command
from autodev.task_formatting import task_identity_text
from autodev.task_state import normalize_block_reason, task_lifecycle_status
from autodev.task_audit import audit_generated_task_store
from autodev.task_store import ensure_task_store_defaults

# The prompt sent to the configured backend to turn intent into tasks.
_BREAKDOWN_PROMPT = """\
You are a task decomposition expert. Read the following project intent and \
break it down into a structured task list for AI-driven autonomous execution.

## Project Intent

{intent_content}

## Output Format

Output ONLY valid JSON (no markdown fences, no explanation) with this exact structure:

{{
  "project": "{project_name}",
  "tasks": [
    {{
      "id": "<priority>-<number>",
      "title": "<short descriptive title>",
      "description": "<1-2 sentence description of what to do>",
      "steps": ["<step 1>", "<step 2>", "..."],
      "docs": ["<relevant doc path if any>"],
      "verification": {{
        "path_patterns": ["<glob pattern for expected output files>"],
        "validate_commands": ["<command to verify, e.g. pytest, npm test>"]
      }},
      "completion": {{
        "kind": "boolean",
        "source": "gate",
        "success_when": "all_checks_pass"
      }},
      "execution": {{
        "strategy": "single_pass"
      }},
      "output": ["<expected output file paths>"]
    }}
  ]
}}

## Rules

1. Order tasks by dependency — earlier tasks should not depend on later ones.
2. Each task should be completable in a single AI session (not too large).
3. Use priority prefixes: P0 (critical/foundation), P1 (core features), P2 (polish/optional).
4. Include realistic verification rules: path_patterns for expected source/header outputs, validate_commands for verification.
5. Every task MUST define an observable completion contract in `completion`.
6. Use boolean completion (`kind = "boolean"`, `source = "gate"`, `success_when = "all_checks_pass"`) for normal implementation, bug-fix, integration, and delivery tasks.
7. Use numeric completion only when the task goal is a measurable metric (latency, throughput, score, benchmark result, etc.). Numeric completion must include `name`, `direction`, `source = "json_stdout"`, and `json_path`.
8. Every task MUST define `execution`. Use `{"strategy": "single_pass"}` for normal delivery work. Use `{"strategy": "iterative", ...}` only for bounded metric-driven optimization tasks.
9. If `execution.strategy = "iterative"`, then `completion.kind` must be `"numeric"`.
10. Do not use legacy `execution_mode` or `experiment` fields in newly generated tasks.
11. Keep steps concrete and actionable — the AI agent will execute them literally.
12. Aim for 3-8 steps per task. If a task needs more, split it.
13. For C++ or CUDA projects, prefer source-oriented path patterns such as `src/**/*.cpp`, `src/**/*.cu`, `include/**/*.hpp`, `include/**/*.cuh`, `tests/**`, `CMakeLists.txt`, and `CMakePresets.json`.
14. For C++ or CUDA projects, prefer out-of-source build commands like `cmake -S . -B build-<task>` or preset-driven commands such as `cmake --preset <name>` to avoid treating build artifacts as primary outputs.
15. Output ONLY the JSON, nothing else.
"""

_COCA_BREAKDOWN_PROMPT = """\
You are a task decomposition expert. Read the following approved COCA spec and \
turn it into a structured task list for AI-driven autonomous execution.

## Approved COCA Spec

{intent_content}

## Output Format

Output ONLY valid JSON (no markdown fences, no explanation) with this exact structure:

{{
  "project": "{project_name}",
  "tasks": [
    {{
      "id": "<priority>-<number>",
      "title": "<short descriptive title>",
      "description": "<1-2 sentence description tied to the COCA spec>",
      "steps": ["<step 1>", "<step 2>", "..."],
      "docs": ["<relevant doc path if any>"],
      "verification": {{
        "path_patterns": ["<glob pattern for expected output files>"],
        "validate_commands": ["<command to verify, e.g. pytest, npm test>"]
      }},
      "completion": {{
        "kind": "boolean",
        "source": "gate",
        "success_when": "all_checks_pass"
      }},
      "execution": {{
        "strategy": "single_pass"
      }},
      "output": ["<expected output file paths>"]
    }}
  ]
}}

## Rules

1. Preserve dependency order from the spec: foundation before feature work, feature work before polish.
2. Convert Context and Constraints into setup/foundation tasks only when they require executable work.
3. Convert Assertions into concrete verification rules and validation commands wherever possible.
4. Include the COCA spec path in each task's `docs` array when available.
5. Every task MUST define an observable completion contract in `completion`.
6. For normal build/feature/integration work, use boolean completion via `{"kind": "boolean", "source": "gate", "success_when": "all_checks_pass"}`.
7. When a COCA assertion describes a measurable metric target or measurable improvement, use numeric completion with `name`, `direction`, `source = "json_stdout"`, and `json_path`.
8. Every task MUST define `execution`. Use `{"strategy": "single_pass"}` by default. Use `{"strategy": "iterative", ...}` only for bounded metric-driven optimization loops.
9. If `execution.strategy = "iterative"`, then `completion.kind` must be `"numeric"`.
10. Do not use legacy `execution_mode` or `experiment` fields in newly generated tasks.
11. Keep each task completable in one AI session; split large implementation phases.
12. Use priority prefixes: P0 (critical/foundation), P1 (core features), P2 (polish/optional).
13. Keep steps concrete and actionable — the AI agent will execute them literally.
14. For C++ or CUDA projects, keep verification focused on source/header paths and explicit configure/build/test commands, not transient build outputs.
15. When a project likely uses CMake presets or a dedicated subdirectory build, prefer `validate_working_directory` or preset-driven commands instead of fragile inline `cd` chains.
16. Output ONLY the JSON, nothing else.
"""

class ReplanUnavailableError(RuntimeError):
    """Raised when the current task queue cannot be replanned for another epoch."""


_REPLAN_PROMPT = """\
You are refining the autonomous task queue for the next autodev workflow epoch.

## Original Planning Source

{planning_text}

## Source Document

{source_doc}

## Current Execution State

{execution_state}

## Project Learning Journal

{learning_journal}

## Output Format

Output ONLY valid JSON (no markdown fences, no explanation) with this exact structure:

{{
  "project": "{project_name}",
  "tasks": [
    {{
      "id": "<priority>-<number>",
      "title": "<short descriptive title>",
      "description": "<1-2 sentence description of the remaining work>",
      "steps": ["<step 1>", "<step 2>", "..."],
      "docs": ["<relevant doc path if any>"],
      "verification": {{
        "path_patterns": ["<glob pattern for expected output files>"],
        "validate_commands": ["<command to verify the task>"]
      }},
      "completion": {{
        "kind": "boolean",
        "source": "gate",
        "success_when": "all_checks_pass"
      }},
      "execution": {{
        "strategy": "single_pass"
      }},
      "output": ["<expected output file paths>"]
    }}
  ]
}}

## Rules

1. Generate tasks for the remaining work only.
2. Do not recreate already completed work unless a distinct follow-up task is truly needed.
3. If a prior task was blocked because it was poorly scoped, replace it with better-scoped tasks rather than copying it verbatim.
4. Keep the overall project goal unchanged.
5. Preserve each task's completion semantics. Do not weaken a measurable numeric goal into a boolean-only task, and do not loosen an existing completion target just to make replanning easier.
6. Use the learning journal and current execution state to improve task decomposition and verification quality.
7. Every replanned task MUST define an observable completion contract in `completion`.
8. Use boolean completion for normal delivery work and numeric completion only for measurable machine-readable metrics.
9. Every replanned task MUST define `execution`. Use `{"strategy": "single_pass"}` by default and `{"strategy": "iterative", ...}` only for bounded metric-driven optimization.
10. If `execution.strategy = "iterative"`, then `completion.kind` must be `"numeric"`.
11. Do not use legacy `execution_mode` or `experiment` fields in newly generated tasks.
12. Keep tasks small enough for one autonomous AI session.
13. Preserve strong verification; do not weaken checks just to make future tasks easier.
14. Output ONLY the JSON, nothing else.
"""

def generate_tasks_from_text(
    intent_text: str,
    config: AutodevConfig,
    output_path: Path | None = None,
    *,
    source_doc: str = "",
    source_kind: str = "intent",
    source_label: str = "",
) -> dict:
    """Generate a task.json structure from free-form intent text."""
    data, _ = generate_tasks_bundle_from_text(
        intent_text,
        config,
        output_path=output_path,
        source_doc=source_doc,
        source_name="intent",
        source_kind=source_kind,
        source_label=source_label,
    )
    return data


def generate_tasks_bundle_from_text(
    intent_text: str,
    config: AutodevConfig,
    output_path: Path | None = None,
    *,
    source_doc: str = "",
    source_name: str = "intent",
    source_kind: str = "intent",
    source_label: str = "",
) -> tuple[dict, Path | None]:
    """Generate a task.json structure from free-form intent text.

    Parameters
    ----------
    intent_text:
        Free-form developer intent, product brief, or requirements text.
    config:
        Loaded autodev configuration (used for project name and backend settings).
    output_path:
        Where to write the generated task.json.  If ``None``, uses
        ``config.files.task_json``.

    Returns
    -------
    dict
        The generated task data (same structure as task.json).

    Raises
    ------
    RuntimeError
        If the selected backend CLI fails or returns unparseable output.
    """
    intent_text = intent_text.strip()
    if not intent_text:
        raise RuntimeError("Planning input is empty. Provide intent text, a PRD file, or stdin.")

    project_name = config.project.name
    planning_text, planning_source_doc, generated_spec_path = _prepare_planning_input(
        intent_text,
        config,
        source_doc=source_doc,
        source_name=source_name,
    )
    prompt = _build_breakdown_prompt(
        planning_text,
        project_name=project_name,
        source_doc=planning_source_doc,
    )

    backend = config.backend.default
    raw_output = run_backend_prompt(
        prompt,
        config,
        command_label="plan",
    ).strip()
    json_text = _extract_json(raw_output)

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse {backend}'s output as JSON: {exc}\n"
            f"Raw output (first 500 chars): {raw_output[:500]}"
        )

    # Validate minimal structure
    if "tasks" not in data or not isinstance(data["tasks"], list):
        raise RuntimeError(
            f"{backend}'s output missing 'tasks' array. "
            f"Got keys: {list(data.keys())}"
        )

    # Ensure all tasks have required fields with defaults
    ensure_task_store_defaults(data)
    data["planning_source"] = _build_planning_source(
        source_kind=source_kind,
        source_label=source_label,
        input_text=intent_text,
        planning_text=planning_text,
        source_name=source_name,
        source_doc=source_doc,
        planning_source_doc=planning_source_doc,
        generated_spec_path=generated_spec_path,
    )
    source_doc_ref = _normalize_source_doc_ref(planning_source_doc, config)
    for task in data["tasks"]:
        task["docs"] = _merge_task_docs(task.get("docs"), source_doc_ref)
        verification = task.get("verification")
        if not isinstance(verification, dict):
            verification = task.get("gate", {})
        if not isinstance(verification, dict):
            verification = {}
        verification.pop("evidence_keys", None)
        task["verification"] = verification
        task.pop("gate", None)
        _normalize_generated_task_pending_state(task)

    audit_generated_task_store(data, context="Generated tasks failed audit")

    # Write output
    if output_path is None:
        output_path = Path(config.files.task_json)

    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return data, generated_spec_path


def generate_tasks(
    prd_path: Path,
    config: AutodevConfig,
    output_path: Path | None = None,
) -> dict:
    """Read a PRD file and generate a task.json structure via the configured backend."""
    return generate_tasks_from_text(
        prd_path.read_text(encoding="utf-8"),
        config,
        output_path=output_path,
        source_doc=str(prd_path),
        source_kind="file",
        source_label=prd_path.name,
    )


def generate_tasks_bundle(
    prd_path: Path,
    config: AutodevConfig,
    output_path: Path | None = None,
) -> tuple[dict, Path | None]:
    """Generate tasks from a file and return task data plus generated spec path."""
    return generate_tasks_bundle_from_text(
        prd_path.read_text(encoding="utf-8"),
        config,
        output_path=output_path,
        source_doc=str(prd_path),
        source_name=prd_path.stem,
        source_kind="file",
        source_label=prd_path.name,
    )


def _build_plan_command(
    prompt: str,
    config: AutodevConfig,
) -> tuple[list[str], dict[str, str] | None]:
    """Build the one-shot planner command for the configured backend."""
    backend = config.backend.default
    spec = build_backend_command(backend, prompt, config, for_plan=True)
    return spec.cmd, spec.env


def _build_breakdown_prompt(
    intent_text: str,
    *,
    project_name: str,
    source_doc: str = "",
) -> str:
    """Build the planner prompt, specialized for COCA specs when detected."""
    template = _COCA_BREAKDOWN_PROMPT if _looks_like_coca_spec(intent_text) else _BREAKDOWN_PROMPT
    prompt = template.replace("{intent_content}", intent_text).replace(
        "{project_name}", project_name
    )
    if source_doc:
        prompt += f"\n\n## Source Document\n\n{source_doc}\n"
    return prompt


def _looks_like_coca_spec(text: str) -> bool:
    """Return ``True`` when the input resembles a generated COCA spec."""
    lowered = text.strip().lower()
    return all(
        heading in lowered
        for heading in ("## context", "## outcome", "## constraints", "## assertions")
    )


def _prepare_planning_input(
    intent_text: str,
    config: AutodevConfig,
    *,
    source_doc: str = "",
    source_name: str = "intent",
) -> tuple[str, str, Path | None]:
    """Ensure planning uses a COCA spec, generating one first when needed."""
    if _looks_like_coca_spec(intent_text):
        return intent_text, source_doc, None

    from autodev.spec import generate_spec_from_text

    spec_path = generate_spec_from_text(
        intent_text,
        config,
        source_name=source_name,
    )
    return spec_path.read_text(encoding="utf-8"), str(spec_path), spec_path


def _normalize_source_doc_ref(source_doc: str, config: AutodevConfig) -> str:
    """Return a task-friendly document reference for a source spec/PRD path."""
    if not source_doc:
        return ""

    source_path = Path(source_doc)
    if not source_path.is_absolute():
        return source_doc

    code_dir = Path(config.project.code_dir).resolve()
    try:
        return str(source_path.resolve().relative_to(code_dir))
    except ValueError:
        return str(source_path)


def _merge_task_docs(docs: object, source_doc_ref: str) -> list[str]:
    """Ensure task docs is a list and prepend the source doc once when available."""
    items = [str(item) for item in docs] if isinstance(docs, list) else []
    if source_doc_ref and source_doc_ref not in items:
        items.insert(0, source_doc_ref)
    return items


def _normalize_generated_task_pending_state(task: dict) -> None:
    """Force freshly generated tasks back into the pending lifecycle state."""
    task["passes"] = False
    task["blocked"] = False
    task["block_reason"] = ""
    task["blocked_at"] = ""


def _build_planning_source(
    *,
    source_kind: str,
    source_label: str,
    input_text: str,
    planning_text: str,
    source_name: str,
    source_doc: str,
    planning_source_doc: str,
    generated_spec_path: Path | None,
) -> dict:
    """Build durable planning metadata for future workflow epochs."""
    return {
        "source_kind": source_kind,
        "source_label": source_label,
        "source_name": source_name,
        "input_text": input_text,
        "planning_text": planning_text,
        "source_doc": source_doc,
        "planning_source_doc": planning_source_doc,
        "generated_spec_path": str(generated_spec_path) if generated_spec_path else "",
    }


def _render_task_state_lines(tasks: list[dict], *, status: str) -> str:
    lines: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_status = task_lifecycle_status(task)
        if task_status != status:
            continue
        task_id, title = task_identity_text(task)
        block_reason = normalize_block_reason(task.get("block_reason"), strip=True)
        if block_reason and status == "blocked":
            lines.append(f"- {task_id}: {title} | reason: {block_reason}")
        else:
            lines.append(f"- {task_id}: {title}")
    return "\n".join(lines) if lines else f"- No {status} tasks"


def _render_learning_lines(entries: object, *, limit: int = 10) -> str:
    if not isinstance(entries, list):
        return "- No learning entries yet"
    lines: list[str] = []
    for entry in entries[-limit:]:
        if not isinstance(entry, dict):
            continue
        task_id = str(entry.get("task_id", "")).strip()
        summary = str(entry.get("summary", "")).strip()
        if not summary:
            continue
        lines.append(f"- {task_id}: {summary}" if task_id else f"- {summary}")
    return "\n".join(lines) if lines else "- No learning entries yet"


def _build_replan_prompt(
    *,
    planning_text: str,
    project_name: str,
    source_doc: str,
    execution_state: str,
    learning_journal: str,
) -> str:
    """Build the prompt used to create the next epoch task queue."""
    return (
        _REPLAN_PROMPT.replace("{planning_text}", planning_text)
        .replace("{project_name}", project_name)
        .replace("{source_doc}", source_doc or "(none)")
        .replace("{execution_state}", execution_state)
        .replace("{learning_journal}", learning_journal)
    )


def replan_tasks_for_next_epoch(
    current_data: dict,
    config: AutodevConfig,
    output_path: Path | None = None,
    *,
    epoch: int,
) -> dict:
    """Generate a fresh task queue for the next workflow epoch."""
    ensure_task_store_defaults(current_data)
    planning_source = current_data.get("planning_source", {})
    if not isinstance(planning_source, dict):
        raise ReplanUnavailableError(
            "task.json is missing planning_source metadata; run 'autodev plan' again."
        )

    planning_text = str(
        planning_source.get("planning_text") or planning_source.get("input_text") or ""
    ).strip()
    if not planning_text:
        raise ReplanUnavailableError("planning_source does not contain reusable planning text.")

    source_doc = str(planning_source.get("planning_source_doc") or planning_source.get("source_doc") or "")
    execution_state = "\n".join(
        [
            f"Epoch completed: {epoch}",
            "Completed tasks:",
            _render_task_state_lines(current_data.get("tasks", []), status="completed"),
            "",
            "Blocked tasks:",
            _render_task_state_lines(current_data.get("tasks", []), status="blocked"),
            "",
            "Pending tasks:",
            _render_task_state_lines(current_data.get("tasks", []), status="pending"),
        ]
    )
    learning_journal = _render_learning_lines(current_data.get("learning_journal"), limit=12)
    prompt = _build_replan_prompt(
        planning_text=planning_text,
        project_name=config.project.name,
        source_doc=source_doc,
        execution_state=execution_state,
        learning_journal=learning_journal,
    )
    raw_output = run_backend_prompt(
        prompt,
        config,
        command_label="replan",
    ).strip()
    json_text = _extract_json(raw_output)

    try:
        next_data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse replanning output as JSON: {exc}") from exc

    if "tasks" not in next_data or not isinstance(next_data["tasks"], list):
        raise RuntimeError("Replanning output missing 'tasks' array.")

    ensure_task_store_defaults(next_data)
    next_data["project"] = config.project.name
    next_data["planning_source"] = dict(planning_source)
    next_data["learning_journal"] = list(current_data.get("learning_journal", []))
    epoch_history = list(current_data.get("epoch_history", []))
    epoch_history.append(
        {
            "epoch": epoch,
            "completed": sum(
                1
                for task in current_data.get("tasks", [])
                if isinstance(task, dict) and task_lifecycle_status(task) == "completed"
            ),
            "blocked": sum(
                1
                for task in current_data.get("tasks", [])
                if isinstance(task, dict) and task_lifecycle_status(task) == "blocked"
            ),
            "pending": sum(
                1
                for task in current_data.get("tasks", [])
                if isinstance(task, dict) and task_lifecycle_status(task) == "pending"
            ),
        }
    )
    next_data["epoch_history"] = epoch_history

    planning_source_doc = str(planning_source.get("planning_source_doc") or "")
    source_doc_ref = _normalize_source_doc_ref(planning_source_doc, config)
    for task in next_data["tasks"]:
        if not isinstance(task, dict):
            continue
        task["docs"] = _merge_task_docs(task.get("docs"), source_doc_ref)
        _normalize_generated_task_pending_state(task)

    audit_generated_task_store(next_data, context="Replanned tasks failed audit")

    if output_path is None:
        output_path = Path(config.files.task_json)
    output_path.write_text(
        json.dumps(next_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return next_data


def run_backend_prompt(
    prompt: str,
    config: AutodevConfig,
    *,
    timeout: int | None = None,
    command_label: str = "plan",
) -> str:
    """Run a one-shot backend prompt and return stdout text."""
    backend = config.backend.default
    cmd, env = _build_plan_command(prompt, config)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path(config.project.code_dir)),
            env=env,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"{backend} CLI not found. Install it to use 'autodev {command_label}'."
        )
    except subprocess.TimeoutExpired:
        timeout_message = (
            f"{timeout // 60} minutes"
            if timeout is not None and timeout >= 60 and timeout % 60 == 0
            else f"{timeout} seconds"
        )
        raise RuntimeError(f"{backend} CLI timed out after {timeout_message}")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"{backend} CLI failed (exit={result.returncode}): {stderr}")

    return result.stdout


def _extract_json(text: str) -> str:
    """Extract JSON from text that may be wrapped in markdown code fences."""
    # Try raw first
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped

    # Strip ```json ... ``` fences
    lines = stripped.split("\n")
    in_fence = False
    json_lines: list[str] = []

    for line in lines:
        if line.strip().startswith("```") and not in_fence:
            in_fence = True
            continue
        if line.strip() == "```" and in_fence:
            in_fence = False
            continue
        if in_fence:
            json_lines.append(line)

    if json_lines:
        return "\n".join(json_lines)

    # Last resort — find the first { and last }
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1]

    return stripped
