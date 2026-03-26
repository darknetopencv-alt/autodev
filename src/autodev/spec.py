"""Intent or PRD -> COCA spec generation via configured AI CLI backend."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autodev.config import AutodevConfig

from autodev.plan import run_backend_prompt

_COCA_PROMPT = """\
You are a senior product and software architect. Read the following project intent
and produce a clear, implementation-ready COCA spec.

## Project Name

{project_name}

## Intent

{intent_content}

## COCA

- Context: current state, users, systems, integrations, history, assumptions
- Outcome: what done looks like for users and stakeholders
- Constraints: requirements, boundaries, non-goals, dependencies
- Assertions: happy path, edge cases, error states, anti-behaviors, integrations

## Output Rules

1. Output ONLY markdown.
2. Do not include code fences around the whole document.
3. Keep the spec self-contained and concrete.
4. Do not include implementation code.
5. Where information is missing, make a clearly labeled assumption instead of leaving the spec vague.
6. Use this exact structure:

# <Feature Name> - COCA Spec

## Context

## Outcome

## Constraints

## Assertions
### Happy Path
### Edge Cases
### Error States
### Anti-Behaviors
### Integration
"""


def generate_spec_from_text(
    intent_text: str,
    config: AutodevConfig,
    output_path: Path | None = None,
    *,
    source_name: str = "intent",
) -> Path:
    """Generate a COCA spec markdown document from free-form intent text."""
    intent_text = intent_text.strip()
    if not intent_text:
        raise RuntimeError("Spec input is empty. Provide intent text, a PRD file, or stdin.")

    prompt = _COCA_PROMPT.format(
        project_name=config.project.name,
        intent_content=intent_text,
    )
    raw_output = run_backend_prompt(
        prompt,
        config,
        command_label="spec",
    )
    markdown = _extract_markdown(raw_output)

    if output_path is None:
        output_path = _default_spec_output_path(config, source_name)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    return output_path


def generate_spec(
    prd_path: Path,
    config: AutodevConfig,
    output_path: Path | None = None,
) -> Path:
    """Generate a COCA spec markdown document from an input file."""
    return generate_spec_from_text(
        prd_path.read_text(encoding="utf-8"),
        config,
        output_path=output_path,
        source_name=prd_path.stem,
    )


def _extract_markdown(text: str) -> str:
    """Extract markdown text, stripping optional surrounding fences."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _default_spec_output_path(config: AutodevConfig, source_name: str) -> Path:
    """Build the default output path for generated COCA specs."""
    slug = _slugify(source_name) or "intent"
    return Path(config.project.code_dir) / "docs" / "specs" / f"{slug}-coca-spec.md"


def _slugify(value: str) -> str:
    """Return a filesystem-safe lowercase slug."""
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")
