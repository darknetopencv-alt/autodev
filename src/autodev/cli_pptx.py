"""CLI handler for ``autodev pptx`` – generate a project report PPTX."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from autodev.cli_common import find_config, load_runtime_config
from autodev.plan import run_backend_prompt


_PPTX_PROMPT_TEMPLATE = """\
You are a professional presentation designer. Generate a project report \
PPTX for the project described below using PptxGenJS.

## Project Information

Project name: {project_name}
Project directory: {project_dir}

### README
{readme_content}

### Task Summary (from task.json)
{task_summary}

### Progress Log
{progress_content}

## Instructions

1. Read `.skills/ppt-orchestra-skill/SKILL.md` for the full PPT creation workflow.
2. Read `.skills/color-font-skill/SKILL.md` to select a color palette and font pairing.
3. Read the agent role files in `.skills/pptx-agents/` for slide-specific design guidance.
4. Design a 16:9 presentation with these slides:
   - Cover page (project name + date)
   - Table of Contents
   - Project Background (from README)
   - Completed Tasks (key achievements, 1-3 content slides)
   - Technical Highlights (architecture decisions)
   - Summary page (achievements + next steps)
5. Create `slides/` directory and generate one JS file per slide (`slide-01.js`, `slide-02.js`, ...).
6. Each JS file exports `createSlide(pres, theme)` following `slide-making-skill`.
7. Create `slides/compile.js` to import all slides and write `slides/output/presentation.pptx`.
8. Install pptxgenjs if needed: `npm install pptxgenjs`
9. Run `cd slides && node compile.js` to produce the final PPTX.
10. Verify with `python -m markitdown slides/output/presentation.pptx` — fix any placeholder text.
"""


def _read_file_safe(path: Path, max_chars: int = 3000) -> str:
    """Read a file's content, truncated to *max_chars*, or return a placeholder."""
    if not path.exists():
        return "(not found)"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated)"
        return text
    except OSError:
        return "(read error)"


def _summarize_tasks(task_json_path: Path) -> str:
    """Extract a compact task summary from task.json."""
    if not task_json_path.exists():
        return "(no task.json found)"
    try:
        data = json.loads(task_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "(failed to parse task.json)"

    tasks = data.get("tasks", [])
    lines: list[str] = []
    for t in tasks:
        tid = t.get("id", "?")
        title = t.get("title", "?")
        passed = t.get("passes", False)
        status = "✓ DONE" if passed else "PENDING"
        lines.append(f"- [{status}] {tid}: {title}")
    return "\n".join(lines) if lines else "(no tasks)"


def cmd_pptx(args: argparse.Namespace) -> int:
    """Handle ``autodev pptx``."""
    try:
        config = load_runtime_config(args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    project_dir = Path(config.project.code_dir).resolve()
    project_name = project_dir.name

    readme = _read_file_safe(project_dir / "README.md")
    task_summary = _summarize_tasks(project_dir / "task.json")
    progress = _read_file_safe(project_dir / "progress.txt", max_chars=2000)

    prompt = _PPTX_PROMPT_TEMPLATE.format(
        project_name=project_name,
        project_dir=str(project_dir),
        readme_content=readme,
        task_summary=task_summary,
        progress_content=progress,
    )

    print(f"autodev pptx: Generating project report for '{project_name}'...")
    print(f"  Backend: {config.backend.default}")
    print(f"  Output:  {project_dir}/slides/output/presentation.pptx")
    print()

    try:
        output = run_backend_prompt(
            prompt,
            config,
            timeout=600,
            command_label="pptx",
        )
        print()
        print("autodev pptx: Backend execution completed.")

        # Check if the output file was generated
        pptx_path = project_dir / "slides" / "output" / "presentation.pptx"
        if pptx_path.exists():
            size_kb = pptx_path.stat().st_size / 1024
            print(f"  ✓ PPTX generated: {pptx_path} ({size_kb:.1f} KB)")
        else:
            print(f"  ⚠ Output file not found at {pptx_path}")
            print("    The backend may need manual intervention to compile slides.")
            print(f"    Try: cd {project_dir}/slides && node compile.js")

        return 0
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
