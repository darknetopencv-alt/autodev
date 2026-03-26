import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autodev.backends import BackendResult
from autodev.backends.codex import run_codex
from autodev.backends.gemini import run_gemini
from autodev.config import load_config
from autodev.plan import (
    ReplanUnavailableError,
    _build_breakdown_prompt,
    _build_plan_command,
    _build_replan_prompt,
    _render_task_state_lines,
    _looks_like_coca_spec,
    generate_tasks_bundle_from_text,
    generate_tasks_from_text,
    replan_tasks_for_next_epoch,
    run_backend_prompt,
)
from autodev.spec import generate_spec_from_text


def _load_temp_config(text: str):
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        config_path = root / "autodev.toml"
        config_path.write_text(text.strip() + "\n", encoding="utf-8")
        return load_config(config_path), root


class BackendAndPlanTests(unittest.TestCase):
    def test_detects_coca_spec_by_expected_headings(self) -> None:
        self.assertTrue(
            _looks_like_coca_spec(
                "# Demo - COCA Spec\n\n## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n"
            )
        )
        self.assertFalse(_looks_like_coca_spec("Build me a todo app with auth"))

    def test_generate_tasks_from_text_rejects_empty_input(self) -> None:
        cfg, _ = _load_temp_config(
            """
            [project]
            name = "demo"
            code_dir = "."
            """
        )

        with self.assertRaises(RuntimeError):
            generate_tasks_from_text("   ", cfg)

    def test_render_task_state_lines_omits_blank_block_reason_text(self) -> None:
        rendered = _render_task_state_lines(
            [
                {
                    "id": "P1-1",
                    "title": "Blocked integration",
                    "passes": False,
                    "blocked": True,
                    "block_reason": "   ",
                }
            ],
            status="blocked",
        )

        self.assertEqual(rendered, "- P1-1: Blocked integration")

    def test_build_plan_command_for_claude(self) -> None:
        cfg, _ = _load_temp_config(
            """
            [project]
            name = "demo"
            code_dir = "."

            [backend]
            default = "claude"

            [backend.claude]
            model = "sonnet"
            """
        )

        cmd, env = _build_plan_command("hello", cfg)

        self.assertEqual(cmd, ["claude", "-p", "hello", "--output-format", "text", "--model", "sonnet"])
        self.assertIsNone(env)

    def test_build_claude_command_uses_dangerously_skip_permissions(self) -> None:
        cfg, _ = _load_temp_config(
            """
            [project]
            name = "demo"
            code_dir = "."

            [backend]
            default = "claude"

            [backend.claude]
            skip_permissions = true
            permission_mode = "bypassPermissions"
            output_format = "stream-json"
            """
        )

        from autodev.backends.claude import build_claude_command

        spec = build_claude_command("hello", cfg)

        self.assertEqual(
            spec.cmd,
            [
                "claude",
                "-p",
                "hello",
                "--dangerously-skip-permissions",
                "--permission-mode",
                "bypassPermissions",
                "--output-format",
                "stream-json",
                "--verbose",
            ],
        )

    def test_build_breakdown_prompt_uses_coca_template_for_coca_specs(self) -> None:
        prompt = _build_breakdown_prompt(
            "# Demo - COCA Spec\n\n## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n",
            project_name="demo",
            source_doc="docs/specs/demo-coca-spec.md",
        )

        self.assertIn("Approved COCA Spec", prompt)
        self.assertIn("Include the COCA spec path", prompt)
        self.assertIn("docs/specs/demo-coca-spec.md", prompt)
        self.assertIn('"completion"', prompt)
        self.assertIn('"execution"', prompt)
        self.assertIn("observable completion contract", prompt)
        self.assertIn("Do not use legacy `execution_mode` or `experiment` fields", prompt)

    def test_build_replan_prompt_requires_completion_and_execution_contracts(self) -> None:
        prompt = _build_replan_prompt(
            planning_text="# Demo - COCA Spec\n\n## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n",
            project_name="demo",
            source_doc="docs/specs/demo-coca-spec.md",
            execution_state="Completed tasks:\n- P0-1",
            learning_journal="- P0-1: prefer source verification",
        )

        self.assertIn('"completion"', prompt)
        self.assertIn('"execution"', prompt)
        self.assertIn("Preserve each task's completion semantics", prompt)
        self.assertIn("observable completion contract", prompt)
        self.assertIn("Do not use legacy `execution_mode` or `experiment` fields", prompt)

    def test_generate_spec_from_text_rejects_empty_input(self) -> None:
        cfg, _ = _load_temp_config(
            """
            [project]
            name = "demo"
            code_dir = "."
            """
        )

        with self.assertRaises(RuntimeError):
            generate_spec_from_text("   ", cfg)

    def test_generate_tasks_from_text_injects_source_doc_into_task_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                """
                [project]
                name = "demo"
                code_dir = "."
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            with patch("autodev.spec.run_backend_prompt") as mock_spec_prompt, patch(
                "autodev.plan.run_backend_prompt"
            ) as mock_prompt:
                mock_spec_prompt.return_value = (
                    "# Build Something - COCA Spec\n\n"
                    "## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n"
                )
                mock_prompt.return_value = """
                {
                  "project": "demo",
                  "tasks": [
                    {
                      "id": "P0-1",
                      "title": "Setup app",
                      "description": "Create the initial application structure.",
                      "steps": ["Create the source package", "Add the main entry module"],
                      "docs": ["architecture.md"],
                      "verification": {
                        "path_patterns": ["src/**/*.py"],
                        "validate_commands": ["pytest -q"]
                      },
                      "output": ["src/app.py"]
                    },
                    {
                      "id": "P1-1",
                      "title": "Build feature",
                      "description": "Implement the main feature flow from the spec.",
                      "steps": ["Add the feature module", "Cover the feature with tests"],
                      "docs": ["docs/specs/feature-coca-spec.md"],
                      "verification": {
                        "path_patterns": ["src/**/*.py", "tests/**/*.py"],
                        "validate_commands": ["pytest -q"]
                      },
                      "output": ["src/feature.py", "tests/test_feature.py"]
                    }
                  ]
                }
                """

                data = generate_tasks_from_text(
                    "# Feature - COCA Spec\n\n## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n",
                    cfg,
                    output_path=root / "task.json",
                    source_doc=str(root / "docs" / "specs" / "feature-coca-spec.md"),
                )

            self.assertEqual(
                data["tasks"][0]["docs"],
                ["docs/specs/feature-coca-spec.md", "architecture.md"],
            )
            self.assertEqual(
                data["tasks"][1]["docs"],
                ["docs/specs/feature-coca-spec.md"],
            )
            self.assertEqual(
                data["tasks"][0]["completion"],
                {"kind": "boolean", "source": "gate", "success_when": "all_checks_pass"},
            )
            self.assertEqual(data["tasks"][0]["execution"], {"strategy": "single_pass"})
            self.assertEqual(
                data["tasks"][1]["completion"],
                {"kind": "boolean", "source": "gate", "success_when": "all_checks_pass"},
            )
            self.assertEqual(data["tasks"][1]["execution"], {"strategy": "single_pass"})

    def test_generate_tasks_bundle_from_text_generates_spec_first_for_plain_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                """
                [project]
                name = "demo"
                code_dir = "."
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            with patch("autodev.spec.run_backend_prompt") as mock_spec_prompt, patch(
                "autodev.plan.run_backend_prompt"
            ) as mock_plan_prompt:
                mock_spec_prompt.return_value = (
                    "# Billing Dashboard - COCA Spec\n\n"
                    "## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n"
                )
                mock_plan_prompt.return_value = """
                {
                  "project": "demo",
                  "tasks": [
                    {
                      "id": "P0-1",
                      "title": "Setup billing dashboard",
                      "description": "Create the initial billing dashboard shell.",
                      "steps": ["Add the dashboard module", "Add a basic dashboard test"],
                      "docs": [],
                      "verification": {
                        "path_patterns": ["src/**/*.py", "tests/**/*.py"],
                        "validate_commands": ["pytest -q"]
                      },
                      "output": ["src/billing/dashboard.py", "tests/test_billing_dashboard.py"]
                    }
                  ]
                }
                """

                data, spec_path = generate_tasks_bundle_from_text(
                    "Build a billing dashboard for team admins",
                    cfg,
                    output_path=root / "task.json",
                    source_name="billing-dashboard",
                )

            self.assertEqual(
                spec_path,
                root / "docs" / "specs" / "billing-dashboard-coca-spec.md",
            )
            assert spec_path is not None
            self.assertTrue(spec_path.exists())
            self.assertEqual(
                data["tasks"][0]["docs"],
                ["docs/specs/billing-dashboard-coca-spec.md"],
            )
            self.assertEqual(
                data["tasks"][0]["completion"],
                {"kind": "boolean", "source": "gate", "success_when": "all_checks_pass"},
            )
            self.assertEqual(data["tasks"][0]["execution"], {"strategy": "single_pass"})
            self.assertEqual(data["planning_source"]["source_name"], "billing-dashboard")
            self.assertEqual(data["planning_source"]["source_kind"], "intent")
            self.assertEqual(
                data["planning_source"]["generated_spec_path"],
                str(root / "docs" / "specs" / "billing-dashboard-coca-spec.md"),
            )
            self.assertIn("COCA Spec", data["planning_source"]["planning_text"])

    def test_generate_tasks_bundle_from_text_does_not_force_plan_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                """
                [project]
                name = "demo"
                code_dir = "."
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            with patch("autodev.spec.run_backend_prompt") as mock_spec_prompt, patch(
                "autodev.plan.run_backend_prompt"
            ) as mock_plan_prompt:
                mock_spec_prompt.return_value = (
                    "# Billing Dashboard - COCA Spec\n\n"
                    "## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n"
                )
                mock_plan_prompt.return_value = """
                {
                  "project": "demo",
                  "tasks": [
                    {
                      "id": "P0-1",
                      "title": "Setup billing dashboard",
                      "description": "Create the initial billing dashboard shell.",
                      "steps": ["Add the dashboard module"],
                      "docs": [],
                      "verification": {
                        "path_patterns": ["src/**/*.py"],
                        "validate_commands": ["pytest -q"]
                      },
                      "output": ["src/billing/dashboard.py"]
                    }
                  ]
                }
                """

                generate_tasks_bundle_from_text(
                    "Build a billing dashboard for team admins",
                    cfg,
                    output_path=root / "task.json",
                    source_name="billing-dashboard",
                )

        self.assertNotIn("timeout", mock_plan_prompt.call_args.kwargs)

    def test_generate_tasks_bundle_from_text_normalizes_generated_pending_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                """
                [project]
                name = "demo"
                code_dir = "."
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            with patch("autodev.spec.run_backend_prompt") as mock_spec_prompt, patch(
                "autodev.plan.run_backend_prompt"
            ) as mock_plan_prompt:
                mock_spec_prompt.return_value = (
                    "# Billing Dashboard - COCA Spec\n\n"
                    "## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n"
                )
                mock_plan_prompt.return_value = """
                {
                  "project": "demo",
                  "tasks": [
                    {
                      "id": "P0-1",
                      "title": "Setup billing dashboard",
                      "description": "Create the initial billing dashboard shell.",
                      "steps": ["Add the dashboard module"],
                      "docs": [],
                      "passes": true,
                      "blocked": true,
                      "block_reason": "planner guessed wrong",
                      "blocked_at": "2026-03-26T00:00:00+00:00",
                      "verification": {
                        "path_patterns": ["src/**/*.py"],
                        "validate_commands": ["pytest -q"]
                      },
                      "output": ["src/billing/dashboard.py"]
                    }
                  ]
                }
                """

                data, _ = generate_tasks_bundle_from_text(
                    "Build a billing dashboard for team admins",
                    cfg,
                    output_path=root / "task.json",
                    source_name="billing-dashboard",
                )

        self.assertFalse(data["tasks"][0]["passes"])
        self.assertFalse(data["tasks"][0]["blocked"])
        self.assertEqual(data["tasks"][0]["block_reason"], "")
        self.assertEqual(data["tasks"][0]["blocked_at"], "")

    def test_generate_tasks_from_text_rejects_weak_generated_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                """
                [project]
                name = "demo"
                code_dir = "."
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            with patch("autodev.plan.run_backend_prompt") as mock_prompt:
                mock_prompt.return_value = """
                {
                  "project": "demo",
                  "tasks": [
                    {
                      "id": "P0-1",
                      "title": "Weak task",
                      "verification": {
                        "path_patterns": ["logs/**/*.log"]
                      }
                    }
                  ]
                }
                """

                with self.assertRaisesRegex(RuntimeError, "missing description"):
                    generate_tasks_from_text(
                        "# Demo - COCA Spec\n\n## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n",
                        cfg,
                        output_path=root / "task.json",
                    )

    def test_replan_tasks_for_next_epoch_uses_planning_source_and_learning_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                """
                [project]
                name = "demo"
                code_dir = "."
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            current_data = {
                "project": "demo",
                "planning_source": {
                    "source_kind": "intent",
                    "source_label": "inline intent",
                    "source_name": "intent",
                    "input_text": "Build a billing dashboard",
                    "planning_text": "# Billing Dashboard - COCA Spec\n\n## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n",
                    "planning_source_doc": "docs/specs/billing-dashboard-coca-spec.md",
                },
                "learning_journal": [
                    {
                        "task_id": "P0-1",
                        "summary": "Prefer source-oriented verification and explicit CMake commands.",
                    }
                ],
                "tasks": [
                    {"id": "P0-1", "title": "Foundation", "passes": True, "blocked": False},
                    {
                        "id": "P1-1",
                        "title": "Blocked integration",
                        "passes": False,
                        "blocked": True,
                        "block_reason": "verification failed",
                    },
                ],
            }

            with patch("autodev.plan.run_backend_prompt") as mock_prompt:
                mock_prompt.return_value = """
                {
                  "project": "demo",
                  "tasks": [
                    {
                      "id": "P2-1",
                      "title": "Polish billing charts",
                      "description": "Improve the chart presentation for the remaining dashboard work.",
                      "steps": ["Refine chart rendering", "Update dashboard chart tests"],
                      "docs": [],
                      "verification": {
                        "path_patterns": ["src/**/*.py", "tests/**/*.py"],
                        "validate_commands": ["pytest -q"]
                      },
                      "output": ["src/billing/charts.py", "tests/test_billing_charts.py"]
                    }
                  ]
                }
                """

                next_data = replan_tasks_for_next_epoch(current_data, cfg, epoch=1)

            self.assertEqual(next_data["tasks"][0]["id"], "P2-1")
            self.assertEqual(
                next_data["tasks"][0]["docs"],
                ["docs/specs/billing-dashboard-coca-spec.md"],
            )
            self.assertEqual(next_data["planning_source"]["source_kind"], "intent")
            self.assertEqual(next_data["epoch_history"][0]["epoch"], 1)
            self.assertEqual(
                next_data["tasks"][0]["completion"],
                {"kind": "boolean", "source": "gate", "success_when": "all_checks_pass"},
            )
            self.assertEqual(next_data["tasks"][0]["execution"], {"strategy": "single_pass"})

    def test_replan_tasks_for_next_epoch_does_not_force_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                """
                [project]
                name = "demo"
                code_dir = "."
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            current_data = {
                "project": "demo",
                "planning_source": {
                    "source_kind": "intent",
                    "source_label": "inline intent",
                    "source_name": "intent",
                    "input_text": "Build a billing dashboard",
                    "planning_text": "# Billing Dashboard - COCA Spec\n\n## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n",
                    "planning_source_doc": "docs/specs/billing-dashboard-coca-spec.md",
                },
                "learning_journal": [],
                "tasks": [],
            }

            with patch("autodev.plan.run_backend_prompt") as mock_prompt:
                mock_prompt.return_value = """
                {
                  "project": "demo",
                  "tasks": [
                    {
                      "id": "P2-1",
                      "title": "Polish billing charts",
                      "description": "Improve the chart presentation.",
                      "steps": ["Refine chart rendering"],
                      "docs": [],
                      "verification": {
                        "path_patterns": ["src/**/*.py"],
                        "validate_commands": ["pytest -q"]
                      },
                      "output": ["src/billing/charts.py"]
                    }
                  ]
                }
                """

                replan_tasks_for_next_epoch(current_data, cfg, epoch=1)

        self.assertNotIn("timeout", mock_prompt.call_args.kwargs)

    def test_replan_tasks_for_next_epoch_normalizes_generated_pending_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                """
                [project]
                name = "demo"
                code_dir = "."
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            current_data = {
                "project": "demo",
                "planning_source": {
                    "source_kind": "intent",
                    "source_label": "inline intent",
                    "source_name": "intent",
                    "input_text": "Build a billing dashboard",
                    "planning_text": "# Billing Dashboard - COCA Spec\n\n## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n",
                    "planning_source_doc": "docs/specs/billing-dashboard-coca-spec.md",
                },
                "learning_journal": [],
                "tasks": [],
            }

            with patch("autodev.plan.run_backend_prompt") as mock_prompt:
                mock_prompt.return_value = """
                {
                  "project": "demo",
                  "tasks": [
                    {
                      "id": "P2-1",
                      "title": "Polish billing charts",
                      "description": "Improve the chart presentation.",
                      "steps": ["Refine chart rendering"],
                      "docs": [],
                      "passes": true,
                      "blocked": true,
                      "block_reason": "leftover status",
                      "blocked_at": "2026-03-26T00:00:00+00:00",
                      "verification": {
                        "path_patterns": ["src/**/*.py"],
                        "validate_commands": ["pytest -q"]
                      },
                      "output": ["src/billing/charts.py"]
                    }
                  ]
                }
                """

                next_data = replan_tasks_for_next_epoch(current_data, cfg, epoch=1)

        self.assertFalse(next_data["tasks"][0]["passes"])
        self.assertFalse(next_data["tasks"][0]["blocked"])
        self.assertEqual(next_data["tasks"][0]["block_reason"], "")
        self.assertEqual(next_data["tasks"][0]["blocked_at"], "")

    def test_replan_tasks_for_next_epoch_requires_reusable_planning_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                """
                [project]
                name = "demo"
                code_dir = "."
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            current_data = {
                "project": "demo",
                "planning_source": {
                    "source_kind": "intent",
                    "source_label": "inline intent",
                },
                "tasks": [],
            }

            with self.assertRaises(ReplanUnavailableError):
                replan_tasks_for_next_epoch(current_data, cfg, epoch=1)

    def test_replan_tasks_for_next_epoch_rejects_weak_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                """
                [project]
                name = "demo"
                code_dir = "."
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            current_data = {
                "project": "demo",
                "planning_source": {
                    "source_kind": "intent",
                    "source_label": "inline intent",
                    "source_name": "intent",
                    "input_text": "Build a billing dashboard",
                    "planning_text": "# Billing Dashboard - COCA Spec\n\n## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n",
                },
                "tasks": [],
            }

            with patch("autodev.plan.run_backend_prompt") as mock_prompt:
                mock_prompt.return_value = """
                {
                  "project": "demo",
                  "tasks": [
                    {
                      "id": "P2-1",
                      "title": "Weak replan",
                      "verification": {
                        "path_patterns": ["logs/**/*.log"]
                      }
                    }
                  ]
                }
                """

                with self.assertRaisesRegex(RuntimeError, "missing description"):
                    replan_tasks_for_next_epoch(current_data, cfg, epoch=1)

    def test_generate_tasks_from_text_normalizes_legacy_gate_to_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                """
                [project]
                name = "demo"
                code_dir = "."
                """.strip()
                + "\n",
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            with patch("autodev.spec.run_backend_prompt") as mock_spec_prompt, patch(
                "autodev.plan.run_backend_prompt"
            ) as mock_prompt:
                mock_spec_prompt.return_value = (
                    "# Build Something - COCA Spec\n\n"
                    "## Context\nA\n\n## Outcome\nB\n\n## Constraints\nC\n\n## Assertions\nD\n"
                )
                mock_prompt.return_value = """
                {
                  "project": "demo",
                  "tasks": [
                    {
                      "id": "P0-1",
                      "title": "Normalize verification",
                      "description": "Normalize legacy gate fields into verification.",
                      "steps": ["Map the legacy gate fields to verification"],
                      "docs": [],
                      "gate": {
                        "path_patterns": ["src/*.py"],
                        "evidence_keys": ["legacy_signal"],
                        "validate_commands": ["pytest"]
                      }
                    }
                  ]
                }
                """

                data = generate_tasks_from_text(
                    "Build something",
                    cfg,
                    output_path=root / "task.json",
                )

            task = data["tasks"][0]
            self.assertIn("verification", task)
            self.assertNotIn("gate", task)
            self.assertEqual(task["verification"]["path_patterns"], ["src/*.py"])
            self.assertEqual(task["verification"]["validate_commands"], ["pytest"])

    def test_build_plan_command_for_codex(self) -> None:
        cfg, _ = _load_temp_config(
            """
            [project]
            name = "demo"
            code_dir = "."

            [backend]
            default = "codex"

            [backend.codex]
            model = "gpt-5-codex"
            yolo = true
            ephemeral = true
            """
        )

        cmd, env = _build_plan_command("hello", cfg)

        self.assertEqual(cmd, ["codex", "exec", "--model", "gpt-5-codex", "--yolo", "--ephemeral", "hello"])
        self.assertIsNone(env)

    def test_build_plan_command_for_opencode(self) -> None:
        cfg, _ = _load_temp_config(
            """
            [project]
            name = "demo"
            code_dir = "."

            [backend]
            default = "opencode"

            [backend.opencode]
            model = "gpt-4.1"
            permissions = '{"read":"allow"}'
            log_level = "debug"
            """
        )

        cmd, env = _build_plan_command("hello", cfg)

        self.assertEqual(cmd, ["opencode", "run", "hello", "--model", "gpt-4.1"])
        self.assertIsNotNone(env)
        assert env is not None
        self.assertEqual(env["OPENCODE_PERMISSION"], '{"read":"allow"}')
        self.assertEqual(env["OPENCODE_LOG_LEVEL"], "debug")

    def test_build_plan_command_for_gemini(self) -> None:
        cfg, _ = _load_temp_config(
            """
            [project]
            name = "demo"
            code_dir = "."

            [backend]
            default = "gemini"

            [backend.gemini]
            model = "gemini-2.5-pro"
            yolo = true
            """
        )

        cmd, env = _build_plan_command("hello", cfg)

        self.assertEqual(cmd, ["gemini", "-p", "hello", "--model", "gemini-2.5-pro"])
        self.assertIsNone(env)

    def test_run_codex_builds_expected_exec_command(self) -> None:
        cfg, root = _load_temp_config(
            """
            [project]
            name = "demo"
            code_dir = "."

            [backend]
            default = "codex"

            [backend.codex]
            model = "gpt-5-codex"
            yolo = true
            ephemeral = true
            """
        )

        with patch("autodev.backends.codex.execute_with_tee") as mock_execute:
            mock_execute.return_value = BackendResult(
                exit_code=0,
                log_file=root / "attempt.log",
            )

            run_codex(
                "do work",
                cfg,
                root,
                root / "attempt.log",
                root / "main.log",
            )

        mock_execute.assert_called_once()
        cmd = mock_execute.call_args.args[0]
        self.assertEqual(
            cmd,
            [
                "codex",
                "exec",
                "--model",
                "gpt-5-codex",
                "--yolo",
                "--ephemeral",
                "do work",
            ],
        )

    def test_run_codex_supports_legacy_split_flags_when_yolo_disabled(self) -> None:
        cfg, root = _load_temp_config(
            """
            [project]
            name = "demo"
            code_dir = "."

            [backend]
            default = "codex"

            [backend.codex]
            model = "gpt-5-codex"
            yolo = false
            full_auto = true
            dangerously_bypass_approvals_and_sandbox = true
            """
        )

        with patch("autodev.backends.codex.execute_with_tee") as mock_execute:
            mock_execute.return_value = BackendResult(
                exit_code=0,
                log_file=root / "attempt.log",
            )

            run_codex(
                "do work",
                cfg,
                root,
                root / "attempt.log",
                root / "main.log",
            )

        cmd = mock_execute.call_args.args[0]
        self.assertEqual(
            cmd,
            [
                "codex",
                "exec",
                "--model",
                "gpt-5-codex",
                "--full-auto",
                "--dangerously-bypass-approvals-and-sandbox",
                "do work",
            ],
        )

    def test_run_gemini_builds_expected_exec_command(self) -> None:
        cfg, root = _load_temp_config(
            """
            [project]
            name = "demo"
            code_dir = "."

            [backend]
            default = "gemini"

            [backend.gemini]
            model = "gemini-2.5-pro"
            yolo = true
            output_format = "json"
            all_files = true
            include_directories = "src,tests"
            debug = true
            """
        )

        with patch("autodev.backends.gemini.execute_with_tee") as mock_execute:
            mock_execute.return_value = BackendResult(
                exit_code=0,
                log_file=root / "attempt.log",
            )

            run_gemini(
                "do work",
                cfg,
                root,
                root / "attempt.log",
                root / "main.log",
            )

        cmd = mock_execute.call_args.args[0]
        self.assertEqual(
            cmd,
            [
                "gemini",
                "-p",
                "do work",
                "--model",
                "gemini-2.5-pro",
                "--yolo",
                "--output-format",
                "json",
                "--all-files",
                "--include-directories",
                "src,tests",
                "--debug",
            ],
        )

    def test_generate_spec_from_text_writes_default_coca_markdown(self) -> None:
        cfg, root = _load_temp_config(
            """
            [project]
            name = "demo"
            code_dir = "."

            [backend]
            default = "claude"
            """
        )

        with patch("autodev.spec.run_backend_prompt") as mock_prompt:
            mock_prompt.return_value = "# Demo Feature - COCA Spec\n\n## Context\n\nHello\n"

            output_path = generate_spec_from_text("Build a demo feature", cfg, source_name="Demo Feature")

        self.assertEqual(output_path, root / "docs" / "specs" / "demo-feature-coca-spec.md")
        self.assertTrue(output_path.exists())
        self.assertIn("COCA Spec", output_path.read_text(encoding="utf-8"))

    def test_generate_spec_from_text_does_not_force_timeout(self) -> None:
        cfg, _ = _load_temp_config(
            """
            [project]
            name = "demo"
            code_dir = "."

            [backend]
            default = "claude"
            """
        )

        with patch("autodev.spec.run_backend_prompt") as mock_prompt:
            mock_prompt.return_value = "# Demo Feature - COCA Spec\n\n## Context\n\nHello\n"

            generate_spec_from_text("Build a demo feature", cfg, source_name="Demo Feature")

        self.assertNotIn("timeout", mock_prompt.call_args.kwargs)

    def test_run_backend_prompt_defaults_to_no_timeout(self) -> None:
        cfg, _ = _load_temp_config(
            """
            [project]
            name = "demo"
            code_dir = "."

            [backend]
            default = "codex"
            """
        )

        result = unittest.mock.Mock(returncode=0, stdout="ok", stderr="")
        with patch("autodev.plan._build_plan_command", return_value=(["codex", "exec", "hello"], None)), patch(
            "autodev.plan.subprocess.run", return_value=result
        ) as mock_run:
            output = run_backend_prompt("hello", cfg)

        self.assertEqual(output, "ok")
        self.assertIsNone(mock_run.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
