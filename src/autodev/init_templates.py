"""Template content and metadata for ``autodev init`` scaffolding."""

from __future__ import annotations

from dataclasses import dataclass, field


DEFAULT_TOML = """\
[project]
name = "{project_name}"
code_dir = "."
config_dir = "."

[backend]
default = "{default_backend}"

[backend.claude]
skip_permissions = true
permission_mode = "bypassPermissions"
output_format = "stream-json"
model = ""

[backend.codex]
model = ""
yolo = true
full_auto = false
dangerously_bypass_approvals_and_sandbox = true
ephemeral = false

[backend.gemini]
model = ""
yolo = true
approval_mode = ""
output_format = "text"
all_files = false
include_directories = ""
debug = false

[backend.opencode]
model = ""
format = "default"

[run]
max_retries = 3
max_tasks = 999
max_epochs = 1
heartbeat_interval = 20
delay_between_tasks = 2

[run.root_mode]
disable_skip_permissions = true
fallback_permission_mode = "default"

[files]
task_json = "task.json"
progress = "progress.txt"
execution_guide = "AGENT.md"
task_brief = "TASK.md"
log_dir = "logs"
attempt_log_subdir = "attempts"

[verification]
min_changed_files = 1
validate_commands = []
validate_timeout_seconds = 1800
validate_working_directory = ""
validate_environment = {}

[reflection]
enabled = true
max_refinements_per_task = 3
prompt_timeout_seconds = 180
log_tail_lines = 80
max_attempt_history_entries = 12
max_learning_notes = 20
max_project_learning_entries = 50
prompt_learning_limit = 6

[snapshot]
watch_dirs = ["."]
ignore_dirs = [".git", "build", "venv", "__pycache__", "node_modules", "logs"]
ignore_path_globs = ["build-*", "cmake-build-*", "out-*", "*.dir/*", "*.o", "*.obj", "*.so", "*.a", "*.dylib", "*.dll", "*.pdb", "*.exp", "*.lib", "*.ptx", "*.cubin", "*.fatbin", "task.json", "progress.txt", "TASK.md"]
include_path_globs = []

[env_errors]
halt_patterns = [
    "cannot be used with root/sudo privileges",
    "permission denied",
    "invalid api key",
    "unauthorized",
    "requires --verbose",
]

[circuit_breaker]
no_progress_threshold = 3
repeated_error_threshold = 3
rate_limit_cooldown = 300

[git]
auto_commit = true
commit_message_template = "autodev: {task_id} - {task_name}"
"""

DEFAULT_TASK_JSON = """\
{
  "project": "{project_name}",
  "learning_journal": [],
  "tasks": []
}
"""

DEFAULT_PROGRESS = """\
# {project_name} - Progress Log

This file is updated automatically by `autodev` after each task.

---
"""

DEFAULT_TASK_BRIEF = """\
# Current Task

No active task is being executed right now.

## Runtime Sources of Truth

- `task.json`
- `AGENT.md`
- `progress.txt`
"""

ROOT_EXECUTION_GUIDE = """\
# {project_name} - Agent Guide

This is the canonical, tool-neutral rule file for this project.

## Canonical Files

- `TASK.md` is the active task summary written by `autodev` during execution
- `task.json` is the runtime queue generated or refreshed by `autodev plan`
- `progress.txt` is the structured execution log
- `.skills/` is the canonical shared skill repository
- Tool-specific entry files live under `.claude/`, `.codex/`, `.gemini/`, and `.opencode/`

## Core Rules

- Read `TASK.md` first when it exists, then consult `task.json` for the full contract
- Use `.skills/coca-spec/SKILL.md` to clarify fuzzy requirements before planning
- Use `.skills/spec-driven-develop/SKILL.md` for large rewrites or multi-session work
- Use `.skills/autodev-runtime/references/task-lifecycle.md` for autodev runtime semantics
- Use `.skills/find-skills/SKILL.md` when the user wants to discover or install another skill
- Use `.skills/skill-creator/SKILL.md` when the user wants to create or improve a skill
- For PPTX generation tasks, read `.skills/ppt-orchestra-skill/SKILL.md` first for overall planning
- For PPTX slide generation, use the agents in `.skills/pptx-agents/` (cover, TOC, content, section-divider, summary)
- For PPTX styling, use `.skills/color-font-skill/SKILL.md` and `.skills/design-style-skill/SKILL.md`
- Prefer non-interactive, testable, reversible changes
- Avoid destructive git commands unless explicitly requested
- Keep task metadata accurate; `autodev` finalizes success and appends to `progress.txt`

## autodev Runtime Contract

When operating under `autodev`:

1. Treat `task.json` as the runtime queue generated or refreshed by `autodev plan`
2. Read `TASK.md` for the current-task summary and `task.json` for the full task contract
3. Execute non-interactively and make decisions autonomously
4. Validate your changes
5. If execution cannot be completed, leave a concise block reason in `task.json`
6. Keep task metadata accurate; `autodev` finalizes success and appends to `progress.txt`
"""

CORE_RULES = """\
# Core Rules

- The canonical shared rule file is `../AGENT.md`
- The canonical active task summary file is `../TASK.md`
- The canonical shared skills live in `../.skills/`
- Read `.skills/autodev-runtime/references/task-lifecycle.md` when operating under `autodev`
- If requirements are ambiguous or greenfield, load `.skills/coca-spec/SKILL.md`
- For large rewrites, migrations, or multi-session work, load `.skills/spec-driven-develop/SKILL.md`
- If the user wants to discover or install another skill, load `.skills/find-skills/SKILL.md`
- If the user wants to create or improve a skill, load `.skills/skill-creator/SKILL.md`
- Prefer non-interactive, testable, reversible changes
- On blockers, leave a concise reason that includes what failed and what you already tried
- Avoid destructive git commands unless explicitly requested
- Under `autodev`, keep task metadata accurate and let the runner finalize success/progress state
"""

SHARED_SPEC_SKILL = """\
---
name: spec-driven-develop
description: >-
  Lightweight spec-driven workflow for large rewrites, migrations, overhauls,
  or architecture-heavy work. Use when the task needs analysis, planning,
  progress tracking, and continuity across multiple sessions before coding.
version: 1.0.0
---

# Spec-Driven Develop

This project-local skill is intentionally simple and is adapted from the
`spec_driven_develop` methodology:
https://github.com/zhu1090093659/spec_driven_develop

Use it before coding when the task is large enough that direct implementation
would be risky or hard to resume later.

## Continuity First

Before doing anything else, check whether `docs/progress/MASTER.md` exists.

- If it exists: read it and continue from the recorded current status
- If it does not exist: start from Phase 0 below

## Phase 0: Clarify Intent

Capture:

- Scope
- Target end state
- Constraints
- Priorities

Write a short summary before continuing.

## Phase 1: Analyze

Create:

- `docs/analysis/project-overview.md`
- `docs/analysis/module-inventory.md`
- `docs/analysis/risk-assessment.md`

## Phase 2: Plan

Create:

- `docs/plan/task-breakdown.md`
- `docs/plan/dependency-graph.md`
- `docs/plan/milestones.md`

## Phase 3: Progress Tracking

Create:

- `docs/progress/MASTER.md`
- `docs/progress/phase-N-<name>.md`

Use the templates in `references/doc-templates.md`.

## Phase 4: Handoff to Execution

Once analysis and planning are done:

- Convert the approved plan into executable work items
- If using `autodev`, generate or refresh the runtime queue with `autodev plan`
- Keep `docs/progress/MASTER.md` updated after each meaningful step

## Rules

- Do not skip analysis for high-risk changes
- Keep documents concise and actionable
- Update progress documents immediately after work changes the active state
- New session means reading `docs/progress/MASTER.md` first
- Keep the generated `task.json` queue aligned with the approved execution order
"""

OPENCODE_INSTALL_DOC = """\
# Installing autodev Skills for OpenCode

Preferred:

```bash
autodev install-skills
```

Equivalent manual links:

```bash
mkdir -p ~/.config/opencode/skills
ln -s "$(pwd)/.opencode/skills/spec-driven-develop" ~/.config/opencode/skills/spec-driven-develop
ln -s "$(pwd)/.opencode/skills/coca-spec" ~/.config/opencode/skills/coca-spec
```

Restart OpenCode after linking the skills.

## What gets installed

- `spec-driven-develop`
- `coca-spec`

These skills come from the canonical shared repository in `.skills/`.
"""

GEMINI_INSTALL_DOC = """\
# Installing autodev Tooling for Gemini CLI

Preferred:

```bash
autodev install-skills
```

`autodev init` generates a project-local Gemini layout with:

- `.gemini/GEMINI.md` thin project guidance
- `.gemini/skills/` project-local link to `.skills/`
- `.gemini/commands/` custom command prompts
- `.gemini/settings.json` local Gemini settings
- `.gemini/extensions/autodev-local/` optional extension packaging

## Project-local commands

Gemini discovers project commands from `.gemini/commands/`.
After scaffolding, restart Gemini CLI in the project root and use:

- `/autodev:spec-dev`
- `/autodev:coca-spec`

## Optional local extension packaging

If you want to register the generated extension explicitly:

```bash
gemini extensions validate .gemini/extensions/autodev-local
gemini extensions link .gemini/extensions/autodev-local --consent
```

## What gets installed

- `spec-driven-develop`
- `coca-spec`
- `autodev-local` Gemini extension scaffold

These skills come from the canonical shared repository in `.skills/`.
"""

CODEX_INSTALL_DOC = """\
# Installing autodev Skills for Codex

Preferred:

```bash
autodev install-skills
```

Equivalent manual links:

```bash
mkdir -p ~/.agents/skills
ln -s "$(pwd)/.codex/skills/spec-driven-develop" ~/.agents/skills/spec-driven-develop
ln -s "$(pwd)/.codex/skills/coca-spec" ~/.agents/skills/coca-spec
```

Restart Codex after linking the skills.

## What gets installed

- `spec-driven-develop`
- `coca-spec`

These skills come from the canonical shared repository in `.skills/`.
"""

CLAUDE_PLUGIN_JSON = """\
{
  "name": "autodev-local-skills",
  "description": "Project-local autodev skills and guidance for Claude Code",
  "version": "1.0.0",
  "author": {
    "name": "autodev"
  },
  "license": "MIT",
  "skills": "./.skills/"
}
"""

CLAUDE_MARKETPLACE_JSON = """\
{
  "name": "autodev-local-skills",
  "displayName": "autodev Local Skills",
  "description": "Project-local autodev skills and guidance for Claude Code",
  "version": "1.0.0",
  "author": {
    "name": "autodev"
  },
  "license": "MIT",
  "skills": "./.skills/"
}
"""

CLAUDE_COMMAND = """\
---
description: Start the local spec-driven workflow for a large task
argument-hint: <task description>
---

# Spec-Driven Development

Use `.skills/spec-driven-develop/SKILL.md` for the current task: $ARGUMENTS
"""

CLAUDE_COCA_COMMAND = """\
---
description: Clarify a fuzzy request into a COCA spec before implementation
argument-hint: <feature or task>
---

# COCA Spec

Use `.skills/coca-spec/SKILL.md` for the current feature request: $ARGUMENTS
"""

GEMINI_COMMAND = """\
description = "Start the local spec-driven workflow for a large task"
prompt = \"\"\"
Use the project-local workflow below for the current task:

@{.skills/spec-driven-develop/SKILL.md}

Current request:
{{args}}
\"\"\"
"""

GEMINI_COCA_COMMAND = """\
description = "Clarify a fuzzy request into a COCA spec before implementation"
prompt = \"\"\"
Use the project-local workflow below for the current feature request:

@{.skills/coca-spec/SKILL.md}

Current request:
{{args}}
\"\"\"
"""

GEMINI_SETTINGS_JSON = """\
{
  "mcpServers": {}
}
"""

GEMINI_EXTENSION_JSON = """\
{
  "name": "autodev-local",
  "version": "1.0.0",
  "contextFileName": "GEMINI.md"
}
"""

GEMINI_EXTENSION_GUIDE = """\
# autodev Local Gemini Extension

This optional extension packages the same project-local `autodev` guidance for
Gemini CLI's extension system.

## Included pieces

- `commands/autodev/spec-dev.toml`
- `commands/autodev/coca-spec.toml`
- `GEMINI.md`

The canonical workflow content still lives under `AGENT.md` and `.skills/`.
"""

TOOL_GUIDE_TEMPLATE = """\
# {project_name} - {tool_label} Project Guide

Keep this file intentionally thin.

## Canonical Shared Guidance

- `../AGENT.md`
- `../TASK.md`
- `../.skills/`

## Always-On Rules

- `{tool_rules_path}`

If this repository is running under `autodev`, also treat `task.json` and
`progress.txt` as the runtime source of truth.
"""


@dataclass(frozen=True)
class ToolScaffold:
    root: str
    guide_name: str
    label: str
    command_files: dict[str, str] = field(default_factory=dict)


TOOL_SPECS: dict[str, ToolScaffold] = {
    "claude": ToolScaffold(
        root=".claude",
        guide_name="CLAUDE.md",
        label="Claude Code",
        command_files={
            "commands/spec-dev.md": CLAUDE_COMMAND,
            "commands/coca-spec.md": CLAUDE_COCA_COMMAND,
        },
    ),
    "codex": ToolScaffold(
        root=".codex",
        guide_name="AGENTS.md",
        label="Codex",
    ),
    "gemini": ToolScaffold(
        root=".gemini",
        guide_name="GEMINI.md",
        label="Gemini CLI",
        command_files={
            "commands/autodev/spec-dev.toml": GEMINI_COMMAND,
            "commands/autodev/coca-spec.toml": GEMINI_COCA_COMMAND,
            "settings.json": GEMINI_SETTINGS_JSON,
            "extensions/autodev-local/gemini-extension.json": GEMINI_EXTENSION_JSON,
            "extensions/autodev-local/GEMINI.md": GEMINI_EXTENSION_GUIDE,
            "extensions/autodev-local/commands/autodev/spec-dev.toml": GEMINI_COMMAND,
            "extensions/autodev-local/commands/autodev/coca-spec.toml": GEMINI_COCA_COMMAND,
        },
    ),
    "opencode": ToolScaffold(
        root=".opencode",
        guide_name="AGENTS.md",
        label="OpenCode",
    ),
}


def build_base_templates(default_backend: str = "codex") -> dict[str, str]:
    """Return the non-tool-specific files created by ``autodev init``."""
    return {
        "autodev.toml": DEFAULT_TOML.replace("{default_backend}", default_backend),
        "task.json": DEFAULT_TASK_JSON,
        "AGENT.md": ROOT_EXECUTION_GUIDE,
        "TASK.md": DEFAULT_TASK_BRIEF,
        "progress.txt": DEFAULT_PROGRESS,
    }


def build_shared_agent_templates() -> dict[str, str]:
    """Return shared non-skill templates created by ``autodev init``."""
    return {}


def build_tool_support_templates(tool: str) -> dict[str, str]:
    """Return extra support files that belong only to one tool."""
    if tool == "claude":
        return {
            ".claude-plugin/plugin.json": CLAUDE_PLUGIN_JSON,
            ".claude-plugin/marketplace.json": CLAUDE_MARKETPLACE_JSON,
        }
    if tool == "codex":
        return {
            ".codex/INSTALL.md": CODEX_INSTALL_DOC,
        }
    if tool == "gemini":
        return {
            ".gemini/INSTALL.md": GEMINI_INSTALL_DOC,
        }
    if tool == "opencode":
        return {
            ".opencode/INSTALL.md": OPENCODE_INSTALL_DOC,
        }
    return {}
