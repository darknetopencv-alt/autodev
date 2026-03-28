"""Command-line entrypoint for autodev."""

from __future__ import annotations

import argparse
import sys

from autodev import __version__
from autodev.backends import get_backend_names
from autodev.cli_ops import (
    cmd_verify,
    cmd_plan,
    cmd_spec,
    cmd_status,
)
from autodev.cli_project import cmd_init, cmd_run
from autodev.cli_session import cmd_attach, cmd_list, cmd_stop
from autodev.cli_skills import cmd_skills_doctor, cmd_skills_list, cmd_skills_recommend
from autodev.cli_task import add_task_parser
from autodev.cli_tool import cmd_install_skills
from autodev.cli_pptx import cmd_pptx


def _add_run_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("run", help="Run the main automation loop")
    parser.add_argument("--backend", choices=list(get_backend_names()), help="AI backend")
    parser.add_argument("--max-tasks", type=int, help="Max tasks to process")
    parser.add_argument("--max-retries", type=int, help="Max retries per task")
    parser.add_argument("--epochs", type=int, help="Max workflow epochs (plan -> tasks -> dev)")
    parser.add_argument("--detach", action="store_true", help="Run in a background tmux session")
    parser.add_argument("--dry-run", action="store_true", help="Show what would run")
    parser.set_defaults(func=cmd_run)


def _add_init_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("init", help="Scaffold a new autodev project")
    parser.add_argument("directory", nargs="?", default=".", help="Target directory")
    parser.add_argument("--name", help="Project name")
    parser.add_argument(
        "--use",
        dest="use",
        default="codex",
        help="Single agent wrapper tool to scaffold; defaults to codex",
    )
    parser.set_defaults(func=cmd_init)


def _add_plan_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "plan",
        help="Primary planning command: generate task.json from intent text, stdin, or a requirements/spec file",
    )
    parser.add_argument(
        "prd_file",
        nargs="?",
        help="Legacy positional input: existing file path or inline intent text",
    )
    parser.add_argument(
        "-f",
        "--file",
        dest="input_file",
        help="Path to a PRD, requirements document, or COCA spec",
    )
    parser.add_argument(
        "--intent",
        help="Free-form project intent; autodev will generate a spec and task.json from it",
    )
    parser.add_argument("-o", "--output", help="Output task.json path (default: config)")
    parser.set_defaults(func=cmd_plan)


def _add_spec_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "spec",
        help="Explicitly generate a COCA spec from intent text, stdin, or a requirements/spec file",
    )
    parser.add_argument(
        "prd_file",
        nargs="?",
        help="Legacy positional input: existing file path or inline intent text",
    )
    parser.add_argument(
        "-f",
        "--file",
        dest="input_file",
        help="Path to a PRD, requirements document, or COCA spec",
    )
    parser.add_argument(
        "--intent",
        help="Free-form project intent; autodev will generate a COCA spec from it",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output markdown path (default: docs/specs/<source>-coca-spec.md)",
    )
    parser.set_defaults(func=cmd_spec)


def _add_verify_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "verify",
        help="Run task completion verification for a task",
    )
    parser.add_argument("task_id", help="Task ID to check")
    parser.add_argument("--changed-file", action="append", help="Changed file (can be repeated)")
    parser.add_argument("--evidence", action="append", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.set_defaults(func=cmd_verify)


def _add_status_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("status", help="Show project status summary")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.set_defaults(func=cmd_status)


def _add_install_skills_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "install-skills",
        help="Register project-local autodev skills for backend.default",
    )
    parser.set_defaults(func=cmd_install_skills)


def _add_skills_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("skills", help="List or recommend available skills")
    skills_subparsers = parser.add_subparsers(dest="skills_command", help="Skill commands")

    list_parser = skills_subparsers.add_parser("list", help="List available skills")
    list_parser.set_defaults(func=cmd_skills_list)

    recommend_parser = skills_subparsers.add_parser(
        "recommend",
        help="Recommend skills for a task or need",
    )
    recommend_parser.add_argument("query", help="Free-form task or need description")
    recommend_parser.add_argument("--limit", type=int, default=5, help="Max recommendations to show")
    recommend_parser.set_defaults(func=cmd_skills_recommend)

    doctor_parser = skills_subparsers.add_parser(
        "doctor",
        help="Diagnose project-local skill wiring and install state",
    )
    doctor_parser.set_defaults(func=cmd_skills_doctor)


def _add_list_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("list", help="List running autodev tmux sessions")
    parser.set_defaults(func=cmd_list)


def _add_attach_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("attach", help="Attach to a running autodev tmux session")
    parser.add_argument("session", help="Session name (from autodev list)")
    parser.set_defaults(func=cmd_attach)


def _add_stop_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("stop", help="Stop a running autodev tmux session")
    parser.add_argument("session", nargs="?", help="Session name (from autodev list)")
    parser.add_argument("--all", action="store_true", help="Stop all autodev sessions")
    parser.set_defaults(func=cmd_stop)


def _add_web_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("web", help="Launch the web dashboard for multi-project management")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    try:
        from autodev.web import cmd_web
        parser.set_defaults(func=cmd_web)
    except ImportError:
        def _web_missing(args: argparse.Namespace) -> int:
            print("Error: web dashboard requires FastAPI and uvicorn.", file=sys.stderr)
            print("Install with:  pip install autodev[web]", file=sys.stderr)
            return 1
        parser.set_defaults(func=_web_missing)


def _add_pptx_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "pptx",
        help="Generate a project report PPTX using AI and pptx-plugin skills",
    )
    parser.set_defaults(func=cmd_pptx)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser."""
    parser = argparse.ArgumentParser(
        prog="autodev",
        description="Unattended AI-driven development automation from intent to execution",
    )
    parser.add_argument("--version", action="version", version=f"autodev {__version__}")
    parser.add_argument("-c", "--config", help="Path to autodev.toml")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    _add_run_parser(subparsers)
    _add_init_parser(subparsers)
    add_task_parser(subparsers)
    _add_plan_parser(subparsers)
    _add_spec_parser(subparsers)
    _add_verify_parser(subparsers)
    _add_status_parser(subparsers)
    _add_install_skills_parser(subparsers)
    _add_skills_parser(subparsers)
    _add_list_parser(subparsers)
    _add_attach_parser(subparsers)
    _add_stop_parser(subparsers)
    _add_web_parser(subparsers)
    _add_pptx_parser(subparsers)
    return parser


def main() -> None:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
