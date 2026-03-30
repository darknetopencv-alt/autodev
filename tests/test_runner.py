import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autodev.backends import BackendResult
from autodev.config import load_config
from autodev.log import Logger
from autodev.runner import (
    RunResult,
    _attempt_log_path,
    _experiments_log_path,
    _filter_runtime_changed_files,
    run,
)
from autodev.task_store import load_tasks, save_tasks


class RunnerTests(unittest.TestCase):
    def test_run_result_exit_codes(self) -> None:
        result = RunResult()
        self.assertEqual(result.exit_code, 0)

        result.blocked_present = True
        self.assertEqual(result.exit_code, 2)

        result.env_error = True
        self.assertEqual(result.exit_code, 1)

        result.interrupted = True
        self.assertEqual(result.exit_code, 130)

    def test_attempt_log_path_uses_configured_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "demo"',
                        'code_dir = "."',
                        "",
                        "[files]",
                        'log_dir = "logs"',
                        'attempt_log_subdir = "custom-attempts"',
                        "",
                        "[backend]",
                        'default = "codex"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            cfg = load_config(config_path)
            path = _attempt_log_path(cfg, "P0-1", 2)

            self.assertIn("custom-attempts", str(path))
            self.assertIn("codex", str(path))
            self.assertTrue(path.name.startswith("task_P0-1__attempt_2_"))

    def test_run_triggers_replan_between_epochs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "demo"',
                        'code_dir = "."',
                        "",
                        "[files]",
                        'task_json = "task.json"',
                        'progress = "progress.txt"',
                        'log_dir = "logs"',
                        "",
                        "[run]",
                        "max_epochs = 2",
                    ]
                ),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            save_tasks(
                Path(cfg.files.task_json),
                {
                    "project": "demo",
                    "planning_source": {
                        "source_kind": "intent",
                        "planning_text": "Build a demo system",
                    },
                    "tasks": [
                        {"id": "P0-1", "title": "done", "passes": True, "blocked": False}
                    ],
                },
            )
            logger = Logger(log_file=Path(cfg.files.log_dir) / "autodev.log", use_color=False)

            first = RunResult()
            first.pending_remaining = 0
            first.blocked_present = False
            second = RunResult()
            second.pending_remaining = 0
            second.blocked_present = False

            with patch("autodev.runner._run_loop", side_effect=[first, second]) as mock_loop, patch(
                "autodev.plan.replan_tasks_for_next_epoch"
            ) as mock_replan:
                mock_replan.return_value = {
                    "project": "demo",
                    "planning_source": {"source_kind": "intent", "planning_text": "Build a demo system"},
                    "tasks": [{"id": "P1-1", "title": "next", "passes": False, "blocked": False}],
                }

                result = run(cfg, logger, dry_run=False, epochs=2)

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(mock_loop.call_count, 2)
            mock_replan.assert_called_once()

    def test_filter_runtime_changed_files_excludes_autodev_runtime_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "demo"',
                        'code_dir = "."',
                        "",
                        "[files]",
                        'task_json = "task.json"',
                        'progress = "progress.txt"',
                        'log_dir = "logs"',
                        'attempt_log_subdir = "attempts"',
                    ]
                ),
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            filtered = _filter_runtime_changed_files(
                [
                    "src/main.cpp",
                    "logs/autodev.log",
                    "logs/attempts/claude/task_P0-1.log",
                    "task.json",
                    "progress.txt",
                    "README.md",
                ],
                cfg,
                Path(cfg.project.code_dir),
            )

            self.assertEqual(filtered, ["src/main.cpp", "README.md"])

    def test_run_skips_replan_when_planning_source_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "demo"',
                        'code_dir = "."',
                        "",
                        "[files]",
                        'task_json = "task.json"',
                        'progress = "progress.txt"',
                        'log_dir = "logs"',
                        "",
                        "[run]",
                        "max_epochs = 2",
                    ]
                ),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            save_tasks(
                Path(cfg.files.task_json),
                {
                    "project": "demo",
                    "planning_source": {"source_kind": "intent"},
                    "tasks": [
                        {"id": "P0-1", "title": "done", "passes": True, "blocked": False}
                    ],
                },
            )
            logger = Logger(log_file=Path(cfg.files.log_dir) / "autodev.log", use_color=False)
            first = RunResult()
            first.pending_remaining = 0
            first.blocked_present = False

            with patch("autodev.runner._run_loop", return_value=first) as mock_loop, patch.object(
                logger, "warning"
            ) as mock_warning:
                result = run(cfg, logger, dry_run=False, epochs=2)

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(mock_loop.call_count, 1)
            self.assertTrue(any("Skipping epoch 2/2 replanning" in call.args[0] for call in mock_warning.call_args_list))

    def test_run_marks_task_completed_after_backend_success_without_backend_status_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "demo"',
                        'code_dir = "."',
                        "",
                        "[files]",
                        'task_json = "task.json"',
                        'progress = "progress.txt"',
                        'log_dir = "logs"',
                        "",
                        "[run]",
                        "max_tasks = 1",
                        "max_retries = 1",
                        "delay_between_tasks = 0",
                        "heartbeat_interval = 1",
                        "",
                        "[git]",
                        "auto_commit = false",
                    ]
                ),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            save_tasks(
                Path(cfg.files.task_json),
                {
                    "project": "demo",
                    "tasks": [
                        {
                            "id": "P0-1",
                            "title": "finish task",
                            "description": "do the thing",
                            "steps": ["step 1"],
                            "docs": [],
                            "passes": False,
                            "blocked": False,
                            "verification": {"validate_commands": []},
                        }
                    ],
                },
            )
            logger = Logger(log_file=Path(cfg.files.log_dir) / "autodev.log", use_color=False)

            with patch("autodev.runner.check_prerequisites", return_value=[]), patch(
                "autodev.runner.load_template", return_value="Execute {{task_id}}"
            ), patch("autodev.runner.render_prompt", return_value="run task"), patch(
                "autodev.runner.snapshot_directories", side_effect=[{}, {"src/main.py": "hash"}]
            ), patch(
                "autodev.runner.diff_snapshots", return_value=["src/main.py"]
            ), patch(
                "autodev.runner.run_backend",
                return_value=BackendResult(exit_code=0, log_file=Path(cfg.files.log_dir) / "attempt.log"),
            ), patch("autodev.runner.run_gate") as mock_gate, patch(
                "autodev.runner.update_runtime_artifacts"
            ), patch("autodev.runner.append_progress"), patch("autodev.runner.time.sleep"), patch(
                "autodev.runner.auto_commit"
            ) as mock_auto_commit:
                mock_gate.return_value.status = "passed"
                mock_gate.return_value.checks = []
                mock_gate.return_value.errors = []
                mock_gate.return_value.warnings = []

                result = run(cfg, logger, dry_run=False, epochs=1)

            data = load_tasks(Path(cfg.files.task_json))
            task = data["tasks"][0]
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(task["passes"])
            self.assertFalse(task["blocked"])
            mock_auto_commit.assert_called_once()
            self.assertEqual(mock_auto_commit.call_args.args[3], ["src/main.py"])

    def test_run_marks_blocked_only_queue_as_blocked_not_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "demo"',
                        'code_dir = "."',
                        "",
                        "[files]",
                        'task_json = "task.json"',
                        'progress = "progress.txt"',
                        'log_dir = "logs"',
                    ]
                ),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            save_tasks(
                Path(cfg.files.task_json),
                {
                    "project": "demo",
                    "tasks": [
                        {"id": "P0-1", "title": "stuck", "passes": False, "blocked": True},
                    ],
                },
            )
            logger = Logger(log_file=Path(cfg.files.log_dir) / "autodev.log", use_color=False)

            with patch("autodev.runner.check_prerequisites", return_value=[]), patch(
                "autodev.runner.load_template", return_value="unused"
            ), patch("autodev.runner.write_idle_task_brief"), patch(
                "autodev.runner.update_runtime_artifacts"
            ) as mock_update:
                result = run(cfg, logger, dry_run=False, epochs=1)

            self.assertEqual(result.exit_code, 2)
            run_updates = [call.kwargs["run_updates"] for call in mock_update.call_args_list]
            self.assertTrue(any(update["status"] == "blocked" for update in run_updates))
            self.assertFalse(
                any(
                    update["status"] == "completed" and update["message"] == "All tasks finished"
                    for update in run_updates
                )
            )

    def test_run_uses_waiting_status_after_task_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "demo"',
                        'code_dir = "."',
                        "",
                        "[files]",
                        'task_json = "task.json"',
                        'progress = "progress.txt"',
                        'log_dir = "logs"',
                        "",
                        "[run]",
                        "max_tasks = 1",
                        "max_retries = 1",
                        "delay_between_tasks = 0",
                        "heartbeat_interval = 1",
                        "",
                        "[git]",
                        "auto_commit = false",
                    ]
                ),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            save_tasks(
                Path(cfg.files.task_json),
                {
                    "project": "demo",
                    "tasks": [
                        {
                            "id": "P0-1",
                            "title": "finish task",
                            "description": "do the thing",
                            "steps": ["step 1"],
                            "docs": [],
                            "passes": False,
                            "blocked": False,
                            "verification": {"validate_commands": []},
                        }
                    ],
                },
            )
            logger = Logger(log_file=Path(cfg.files.log_dir) / "autodev.log", use_color=False)

            with patch("autodev.runner.check_prerequisites", return_value=[]), patch(
                "autodev.runner.load_template", return_value="Execute {{task_id}}"
            ), patch("autodev.runner.render_prompt", return_value="run task"), patch(
                "autodev.runner.snapshot_directories", side_effect=[{}, {"src/main.py": "hash"}]
            ), patch(
                "autodev.runner.diff_snapshots", return_value=["src/main.py"]
            ), patch(
                "autodev.runner.run_backend",
                return_value=BackendResult(exit_code=0, log_file=Path(cfg.files.log_dir) / "attempt.log"),
            ), patch("autodev.runner.run_gate") as mock_gate, patch(
                "autodev.runner.update_runtime_artifacts"
            ) as mock_update, patch("autodev.runner.append_progress"), patch(
                "autodev.runner.time.sleep"
            ), patch("autodev.runner.auto_commit"):
                mock_gate.return_value.status = "passed"
                mock_gate.return_value.checks = []
                mock_gate.return_value.errors = []
                mock_gate.return_value.warnings = []

                result = run(cfg, logger, dry_run=False, epochs=1)

            self.assertEqual(result.exit_code, 0)
            run_updates = [call.kwargs["run_updates"] for call in mock_update.call_args_list]
            self.assertTrue(
                any(
                    update["status"] == "waiting"
                    and update["message"] == "Completed P0-1; preparing next task"
                    for update in run_updates
                )
            )

    def test_run_does_not_pause_or_skip_reflection_on_verification_only_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "demo"',
                        'code_dir = "."',
                        "",
                        "[files]",
                        'task_json = "task.json"',
                        'progress = "progress.txt"',
                        'log_dir = "logs"',
                        "",
                        "[run]",
                        "max_tasks = 1",
                        "max_retries = 1",
                        "delay_between_tasks = 0",
                        "heartbeat_interval = 1",
                        "",
                        "[git]",
                        "auto_commit = false",
                    ]
                ),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            save_tasks(
                Path(cfg.files.task_json),
                {
                    "project": "demo",
                    "tasks": [
                        {
                            "id": "P0-1",
                            "title": "verification failure task",
                            "description": "do the thing",
                            "steps": ["step 1"],
                            "docs": [],
                            "passes": False,
                            "blocked": False,
                            "verification": {"validate_commands": []},
                        }
                    ],
                },
            )
            logger = Logger(log_file=Path(cfg.files.log_dir) / "autodev.log", use_color=False)
            gate_result = Mock(status="failed", checks=[], errors=["Too few changed files: 0 < 1"], warnings=[])
            reflection = Mock(summary="Need actual src changes", learning_notes=["Edit source files"], implementation_notes=[], verification_notes=[], steps=None, docs=None, output=None, verification={})

            with patch("autodev.runner.check_prerequisites", return_value=[]), patch(
                "autodev.runner.load_template", return_value="Execute {{task_id}}"
            ), patch("autodev.runner.render_prompt", return_value="run task"), patch(
                "autodev.runner.snapshot_directories", side_effect=[{}, {}]
            ), patch(
                "autodev.runner.diff_snapshots", return_value=[]
            ), patch(
                "autodev.runner.run_backend",
                return_value=BackendResult(exit_code=0, log_file=Path(cfg.files.log_dir) / "attempt.log"),
            ), patch("autodev.runner.run_gate", return_value=gate_result), patch(
                "autodev.runner.update_runtime_artifacts"
            ), patch("autodev.runner.append_progress"), patch("autodev.runner.time.sleep") as mock_sleep, patch(
                "autodev.runner.auto_commit"
            ), patch("autodev.runner.reflect_failed_attempt", return_value=reflection) as mock_reflect:
                attempt_log = Path(cfg.files.attempt_log_subdir) / "claude" / "task_P0-1__attempt_1_test.log"
                attempt_log.parent.mkdir(parents=True, exist_ok=True)
                attempt_log.write_text('{"message":"capacity metadata only"}\n', encoding="utf-8")

                result = run(cfg, logger, dry_run=False, epochs=1)

            self.assertEqual(result.exit_code, 2)
            mock_reflect.assert_called_once()
            mock_sleep.assert_called_once_with(0)

    def test_run_experiment_task_reverts_regression_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "demo"',
                        'code_dir = "."',
                        "",
                        "[files]",
                        'task_json = "task.json"',
                        'progress = "progress.txt"',
                        'log_dir = "logs"',
                        'attempt_log_subdir = "attempts"',
                        "",
                        "[run]",
                        "max_tasks = 1",
                        "max_retries = 1",
                        "delay_between_tasks = 0",
                        "heartbeat_interval = 1",
                        "",
                        "[git]",
                        "auto_commit = false",
                    ]
                ),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            save_tasks(
                Path(cfg.files.task_json),
                {
                    "project": "demo",
                    "tasks": [
                        {
                            "id": "P1-1",
                            "title": "tune latency",
                            "description": "reduce latency",
                            "steps": ["measure baseline", "make one focused optimization"],
                            "docs": [],
                            "passes": False,
                            "blocked": False,
                            "execution_mode": "experiment",
                            "verification": {"validate_commands": ["python3 -c pass"]},
                            "experiment": {
                                "max_iterations": 1,
                                "rollback_on_regression": True,
                                "keep_on_equal": False,
                                "commit_prefix": "experiment",
                                "no_improvement_threshold": 2,
                                "invalid_result_threshold": 2,
                                "goal_metric": {
                                    "name": "latency_ms",
                                    "direction": "lower_is_better",
                                    "source": "json_stdout",
                                    "json_path": "$.metrics.latency_ms",
                                    "min_improvement": 1,
                                    "unchanged_tolerance": 0,
                                },
                            },
                        }
                    ],
                },
            )
            logger = Logger(log_file=Path(cfg.files.log_dir) / "autodev.log", use_color=False)
            baseline_gate = Mock(status="passed", checks=[], errors=[], warnings=[])
            baseline_gate.metric = Mock(name="latency_ms", value=100.0, outcome="measured", details="baseline")
            regression_gate = Mock(status="passed", checks=[], errors=[], warnings=[])
            regression_gate.metric = Mock(name="latency_ms", value=110.0, outcome="regressed", details="slower")

            with patch("autodev.runner.check_prerequisites", return_value=[]), patch(
                "autodev.runner.load_template", return_value="Execute {{task_id}}"
            ), patch("autodev.runner.render_prompt", return_value="run task") as mock_render_prompt, patch(
                "autodev.runner.snapshot_directories", side_effect=[{}, {"src/main.py": "before"}, {"src/main.py": "after"}]
            ), patch(
                "autodev.runner.diff_snapshots", return_value=["src/main.py"]
            ), patch(
                "autodev.runner.run_backend",
                return_value=BackendResult(exit_code=0, log_file=Path(cfg.files.log_dir) / "attempt.log"),
            ), patch("autodev.runner.run_gate", side_effect=[baseline_gate, regression_gate]) as mock_gate, patch(
                "autodev.runner.is_git_repo", return_value=True
            ), patch(
                "autodev.runner.create_experiment_commit", return_value="abc123"
            ) as mock_commit, patch("autodev.runner.revert_commit", return_value="def456") as mock_revert, patch(
                "autodev.runner.update_runtime_artifacts"
            ), patch("autodev.runner.append_progress"), patch("autodev.runner.time.sleep"), patch(
                "autodev.runner.auto_commit"
            ) as mock_auto_commit, patch(
                "autodev.runner.read_recent_git_history",
                return_value=[{"commit_sha": "abc123", "subject": "experiment: tune latency", "committed_at": "2026-03-23T12:00:00Z"}],
            ):
                result = run(cfg, logger, dry_run=False, epochs=1)

            data = load_tasks(Path(cfg.files.task_json))
            task = data["tasks"][0]
            self.assertEqual(result.exit_code, 2)
            self.assertFalse(task["passes"])
            self.assertTrue(task["blocked"])
            self.assertIn("no improvements kept", task["block_reason"])
            self.assertEqual(mock_gate.call_count, 2)
            self.assertFalse(mock_gate.call_args_list[0].kwargs["enforce_change_requirements"])
            self.assertEqual(mock_gate.call_args_list[1].kwargs["best_before"], 100.0)
            mock_commit.assert_called_once()
            mock_revert.assert_called_once_with(Path(cfg.project.code_dir), "abc123", logger=logger)
            mock_auto_commit.assert_not_called()
            execution_context = mock_render_prompt.call_args.kwargs["execution_context"]
            self.assertEqual(execution_context["execution_mode"], "experiment")
            self.assertEqual(execution_context["current_iteration"], 1)
            self.assertEqual(execution_context["max_iterations"], 1)
            self.assertEqual(execution_context["baseline_metric"], "latency_ms=100")
            self.assertEqual(execution_context["best_metric"], "latency_ms=100")
            self.assertEqual(execution_context["metric_goal"], "latency_ms, direction=lower_is_better, min_improvement=1, unchanged_tolerance=0")
            self.assertEqual(mock_render_prompt.call_args.kwargs["recent_experiment_history"][0]["outcome"], "baseline")
            self.assertEqual(mock_render_prompt.call_args.kwargs["recent_git_history"][0]["commit_sha"], "abc123")

            experiments_log = _experiments_log_path(cfg)
            lines = [json.loads(line) for line in experiments_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines[0]["outcome"], "baseline")
            self.assertEqual(lines[1]["outcome"], "regressed")
            self.assertEqual(lines[1]["commit_sha"], "abc123")
            self.assertEqual(lines[1]["reverted_sha"], "def456")
            self.assertEqual(lines[1]["best_before"], 100.0)

    def test_run_experiment_task_with_improvement_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "demo"',
                        'code_dir = "."',
                        "",
                        "[files]",
                        'task_json = "task.json"',
                        'progress = "progress.txt"',
                        'log_dir = "logs"',
                        'attempt_log_subdir = "attempts"',
                        "",
                        "[run]",
                        "max_tasks = 1",
                        "max_retries = 1",
                        "delay_between_tasks = 0",
                        "heartbeat_interval = 1",
                        "",
                        "[git]",
                        "auto_commit = false",
                    ]
                ),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            save_tasks(
                Path(cfg.files.task_json),
                {
                    "project": "demo",
                    "tasks": [
                        {
                            "id": "P1-3",
                            "title": "tune latency",
                            "description": "reduce latency",
                            "steps": ["measure baseline", "optimize"],
                            "docs": [],
                            "passes": False,
                            "blocked": False,
                            "execution_mode": "experiment",
                            "verification": {"validate_commands": ["python3 -c pass"]},
                            "experiment": {
                                "max_iterations": 1,
                                "rollback_on_regression": True,
                                "keep_on_equal": False,
                                "commit_prefix": "experiment",
                                "no_improvement_threshold": 2,
                                "invalid_result_threshold": 2,
                                "goal_metric": {
                                    "name": "latency_ms",
                                    "direction": "lower_is_better",
                                    "source": "json_stdout",
                                    "json_path": "$.metrics.latency_ms",
                                    "min_improvement": 1,
                                    "unchanged_tolerance": 0,
                                },
                            },
                        }
                    ],
                },
            )
            logger = Logger(log_file=Path(cfg.files.log_dir) / "autodev.log", use_color=False)
            baseline_gate = Mock(status="passed", checks=[], errors=[], warnings=[])
            baseline_gate.metric = Mock(name="latency_ms", value=100.0, outcome="measured", details="baseline")
            improved_gate = Mock(status="passed", checks=[], errors=[], warnings=[])
            improved_gate.metric = Mock(name="latency_ms", value=90.0, outcome="improved", details="faster")

            with patch("autodev.runner.check_prerequisites", return_value=[]), patch(
                "autodev.runner.load_template", return_value="Execute {{task_id}}"
            ), patch("autodev.runner.render_prompt", return_value="run task"), patch(
                "autodev.runner.snapshot_directories", side_effect=[{}, {"src/main.py": "before"}, {"src/main.py": "after"}]
            ), patch(
                "autodev.runner.diff_snapshots", return_value=["src/main.py"]
            ), patch(
                "autodev.runner.run_backend",
                return_value=BackendResult(exit_code=0, log_file=Path(cfg.files.log_dir) / "attempt.log"),
            ), patch("autodev.runner.run_gate", side_effect=[baseline_gate, improved_gate]), patch(
                "autodev.runner.is_git_repo", return_value=True
            ), patch(
                "autodev.runner.create_experiment_commit", return_value="abc123"
            ), patch("autodev.runner.revert_commit") as mock_revert, patch(
                "autodev.runner.update_runtime_artifacts"
            ), patch("autodev.runner.append_progress"), patch("autodev.runner.time.sleep"), patch(
                "autodev.runner.auto_commit"
            ), patch(
                "autodev.runner.read_recent_git_history", return_value=[],
            ):
                result = run(cfg, logger, dry_run=False, epochs=1)

            data = load_tasks(Path(cfg.files.task_json))
            task = data["tasks"][0]
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(task["passes"])
            self.assertFalse(task["blocked"])
            mock_revert.assert_not_called()

    def test_run_experiment_task_blocks_when_regression_cannot_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_path = root / "autodev.toml"
            config_path.write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "demo"',
                        'code_dir = "."',
                        "",
                        "[files]",
                        'task_json = "task.json"',
                        'progress = "progress.txt"',
                        'log_dir = "logs"',
                        'attempt_log_subdir = "attempts"',
                        "",
                        "[run]",
                        "max_tasks = 1",
                        "max_retries = 1",
                        "delay_between_tasks = 0",
                        "heartbeat_interval = 1",
                        "",
                        "[git]",
                        "auto_commit = false",
                    ]
                ),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            save_tasks(
                Path(cfg.files.task_json),
                {
                    "project": "demo",
                    "tasks": [
                        {
                            "id": "P1-2",
                            "title": "tune throughput",
                            "description": "improve throughput",
                            "steps": ["measure baseline", "make one focused optimization"],
                            "docs": [],
                            "passes": False,
                            "blocked": False,
                            "execution_mode": "experiment",
                            "verification": {"validate_commands": ["python3 -c pass"]},
                            "experiment": {
                                "max_iterations": 1,
                                "rollback_on_regression": False,
                                "keep_on_equal": False,
                                "commit_prefix": "experiment",
                                "no_improvement_threshold": 2,
                                "invalid_result_threshold": 2,
                                "goal_metric": {
                                    "name": "throughput",
                                    "direction": "higher_is_better",
                                    "source": "json_stdout",
                                    "json_path": "$.metrics.throughput",
                                    "min_improvement": 1,
                                    "unchanged_tolerance": 0,
                                },
                            },
                        }
                    ],
                },
            )
            logger = Logger(log_file=Path(cfg.files.log_dir) / "autodev.log", use_color=False)
            baseline_gate = Mock(status="passed", checks=[], errors=[], warnings=[])
            baseline_gate.metric = Mock(name="throughput", value=100.0, outcome="measured", details="baseline")
            regression_gate = Mock(status="passed", checks=[], errors=[], warnings=[])
            regression_gate.metric = Mock(name="throughput", value=90.0, outcome="regressed", details="slower")

            with patch("autodev.runner.check_prerequisites", return_value=[]), patch(
                "autodev.runner.load_template", return_value="Execute {{task_id}}"
            ), patch("autodev.runner.render_prompt", return_value="run task"), patch(
                "autodev.runner.snapshot_directories", side_effect=[{}, {"src/main.py": "before"}, {"src/main.py": "after"}]
            ), patch(
                "autodev.runner.diff_snapshots", return_value=["src/main.py"]
            ), patch(
                "autodev.runner.run_backend",
                return_value=BackendResult(exit_code=0, log_file=Path(cfg.files.log_dir) / "attempt.log"),
            ), patch("autodev.runner.run_gate", side_effect=[baseline_gate, regression_gate]), patch(
                "autodev.runner.is_git_repo", return_value=True
            ), patch(
                "autodev.runner.create_experiment_commit", return_value="abc123"
            ), patch("autodev.runner.revert_commit") as mock_revert, patch(
                "autodev.runner.update_runtime_artifacts"
            ), patch("autodev.runner.append_progress"), patch("autodev.runner.time.sleep"), patch(
                "autodev.runner.auto_commit"
            ) as mock_auto_commit:
                result = run(cfg, logger, dry_run=False, epochs=1)

            data = load_tasks(Path(cfg.files.task_json))
            task = data["tasks"][0]
            self.assertEqual(result.exit_code, 2)
            self.assertFalse(task["passes"])
            self.assertTrue(task["blocked"])
            self.assertIn("rollback_on_regression=false", task["block_reason"])
            mock_revert.assert_not_called()
            mock_auto_commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
