import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autodev.task_store import (
    ensure_task_store_defaults,
    get_next_task,
    get_task_counts,
    get_recent_project_learning_summaries,
    load_tasks,
    load_task_context,
    mark_task_blocked,
    mark_task_blocked_in_file,
    mark_task_passed,
    reset_tasks,
    retry_blocked_tasks,
    save_tasks,
    task_has_final_status,
)


class TaskStoreTests(unittest.TestCase):
    def test_load_tasks_rejects_non_object_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "task.json"
            path.write_text('["not", "a", "task-store"]\n', encoding="utf-8")

            with self.assertRaises(ValueError) as context:
                load_tasks(path)

            self.assertIn("root value must be a JSON object", str(context.exception))

    def test_load_task_context_returns_matching_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "task.json"
            save_tasks(
                path,
                {
                    "project": "demo",
                    "tasks": [
                        {"id": "P0-1", "title": "first", "passes": False, "blocked": False},
                        {"id": "P0-2", "title": "second", "passes": True, "blocked": False},
                    ],
                },
            )

            data, task = load_task_context(path, "P0-2")

            self.assertEqual(data["project"], "demo")
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task["title"], "second")
            self.assertTrue(task_has_final_status(task))

    def test_mark_task_blocked_in_file_persists_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "task.json"
            save_tasks(
                path,
                {
                    "project": "demo",
                    "tasks": [
                        {"id": "P0-1", "title": "first", "passes": False, "blocked": False}
                    ],
                },
            )

            changed = mark_task_blocked_in_file(path, "P0-1", "backend failed")
            _, task = load_task_context(path, "P0-1")

            self.assertTrue(changed)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertTrue(task["blocked"])
            self.assertEqual(task["block_reason"], "backend failed")
            self.assertTrue(task_has_final_status(task))

    def test_retry_blocked_tasks_only_resets_blocked_entries(self) -> None:
        data = {
            "project": "demo",
            "tasks": [
                {
                    "id": "P0-1",
                    "title": "blocked task",
                    "passes": False,
                    "blocked": True,
                    "block_reason": "gate failed",
                    "blocked_at": "2026-03-22T00:00:00+00:00",
                },
                {
                    "id": "P0-2",
                    "title": "completed task",
                    "passes": True,
                    "blocked": False,
                    "block_reason": "",
                    "blocked_at": "",
                },
                {
                    "id": "P0-3",
                    "title": "pending task",
                    "passes": False,
                    "blocked": False,
                    "block_reason": "",
                    "blocked_at": "",
                },
            ],
        }

        retried = retry_blocked_tasks(data)

        self.assertEqual(retried, 1)
        blocked_task = data["tasks"][0]
        self.assertFalse(blocked_task["blocked"])
        self.assertFalse(blocked_task["passes"])
        self.assertEqual(blocked_task["block_reason"], "")
        self.assertEqual(blocked_task["blocked_at"], "")
        self.assertTrue(data["tasks"][1]["passes"])
        self.assertFalse(data["tasks"][2]["blocked"])

    def test_retry_blocked_tasks_can_target_specific_ids(self) -> None:
        data = {
            "project": "demo",
            "tasks": [
                {
                    "id": "P0-1",
                    "title": "blocked task",
                    "passes": False,
                    "blocked": True,
                    "block_reason": "gate failed",
                    "blocked_at": "2026-03-22T00:00:00+00:00",
                },
                {
                    "id": "P0-2",
                    "title": "another blocked task",
                    "passes": False,
                    "blocked": True,
                    "block_reason": "rate limit loop",
                    "blocked_at": "2026-03-22T00:01:00+00:00",
                },
            ],
        }

        retried = retry_blocked_tasks(data, task_ids={"P0-2"})

        self.assertEqual(retried, 1)
        self.assertTrue(data["tasks"][0]["blocked"])
        self.assertFalse(data["tasks"][1]["blocked"])

    def test_mark_task_passed_clears_blocked_state(self) -> None:
        data = {
            "project": "demo",
            "tasks": [
                {
                    "id": "P0-1",
                    "title": "recoverable task",
                    "passes": False,
                    "blocked": True,
                    "block_reason": "temporary issue",
                    "blocked_at": "2026-03-22T00:00:00+00:00",
                }
            ],
        }

        changed = mark_task_passed(data, "P0-1")

        self.assertTrue(changed)
        task = data["tasks"][0]
        self.assertTrue(task["passes"])
        self.assertFalse(task["blocked"])
        self.assertEqual(task["block_reason"], "")
        self.assertEqual(task["blocked_at"], "")

    def test_mark_task_blocked_normalizes_reason_and_clears_passes(self) -> None:
        data = {
            "project": "demo",
            "tasks": [
                {
                    "id": "P0-2",
                    "title": "needs block",
                    "passes": "true",
                    "blocked": "no",
                    "block_reason": None,
                }
            ],
        }

        changed = mark_task_blocked(data, "P0-2", None)

        self.assertTrue(changed)
        task = data["tasks"][0]
        self.assertFalse(task["passes"])
        self.assertTrue(task["blocked"])
        self.assertEqual(task["block_reason"], "")
        self.assertTrue(task["blocked_at"])

    def test_reset_tasks_normalizes_string_status_fields_when_counting_changes(self) -> None:
        data = {
            "project": "demo",
            "tasks": [
                {
                    "id": "P0-3",
                    "title": "reset me",
                    "passes": "false",
                    "blocked": "no",
                    "block_reason": "",
                    "blocked_at": "",
                }
            ],
        }

        changed = reset_tasks(data)

        self.assertEqual(changed, 0)
        task = data["tasks"][0]
        self.assertFalse(task["passes"])
        self.assertFalse(task["blocked"])

    def test_ensure_task_store_defaults_adds_reflection_fields(self) -> None:
        data = {
            "project": "demo",
            "tasks": [
                {
                    "id": "P0-1",
                    "title": "first",
                    "gate": {
                        "path_patterns": ["src/*.py"],
                        "evidence_keys": ["legacy"],
                    },
                }
            ],
        }

        ensure_task_store_defaults(data)

        task = data["tasks"][0]
        self.assertIn("verification", task)
        self.assertNotIn("gate", task)
        self.assertEqual(task["verification"]["path_patterns"], ["src/*.py"])
        self.assertEqual(task["completion"], {"kind": "boolean", "source": "gate", "success_when": "all_checks_pass"})
        self.assertEqual(task["execution"], {"strategy": "single_pass"})
        self.assertEqual(task["execution_mode"], "delivery")
        self.assertNotIn("experiment", task)
        self.assertEqual(task["implementation_notes"], [])
        self.assertEqual(task["verification_notes"], [])
        self.assertEqual(task["learning_notes"], [])
        self.assertEqual(task["attempt_history"], [])
        self.assertEqual(task["refinement_count"], 0)
        self.assertEqual(data["learning_journal"], [])

    def test_ensure_task_store_defaults_normalizes_string_status_fields(self) -> None:
        data = {
            "project": "demo",
            "tasks": [
                {
                    "id": "P0-2",
                    "title": "normalize flags",
                    "passes": "true",
                    "blocked": "no",
                    "block_reason": None,
                }
            ],
        }

        ensure_task_store_defaults(data)

        task = data["tasks"][0]
        self.assertTrue(task["passes"])
        self.assertFalse(task["blocked"])
        self.assertEqual(task["block_reason"], "")

    def test_ensure_task_store_defaults_prefers_blocked_over_completed_for_conflicts(self) -> None:
        data = {
            "project": "demo",
            "tasks": [
                {
                    "id": "P0-3",
                    "title": "conflicted",
                    "passes": True,
                    "blocked": True,
                    "block_reason": "needs review",
                }
            ],
        }

        ensure_task_store_defaults(data)

        task = data["tasks"][0]
        self.assertFalse(task["passes"])
        self.assertTrue(task["blocked"])
        self.assertEqual(task["block_reason"], "needs review")

    def test_ensure_task_store_defaults_normalizes_legacy_experiment_into_completion_and_execution(self) -> None:
        data = {
            "project": "demo",
            "tasks": [
                {
                    "id": "P1-1",
                    "title": "Tune latency",
                    "description": "Improve the latency benchmark.",
                    "steps": ["Run benchmark", "Improve latency hot path"],
                    "verification": {"validate_commands": ["python3 bench.py"]},
                    "execution_mode": "experiment",
                    "experiment": {
                        "max_iterations": 5,
                        "rollback_on_regression": True,
                        "keep_on_equal": True,
                        "commit_prefix": "experiment",
                        "no_improvement_threshold": 2,
                        "invalid_result_threshold": 3,
                        "goal_metric": {
                            "name": "latency_ms",
                            "direction": "lower_is_better",
                            "source": "json_stdout",
                            "json_path": "$.metrics.latency_ms",
                            "min_improvement": 1.5,
                            "unchanged_tolerance": 0.25,
                        },
                    },
                }
            ],
        }

        ensure_task_store_defaults(data)

        task = data["tasks"][0]
        self.assertEqual(
            task["completion"],
            {
                "kind": "numeric",
                "source": "json_stdout",
                "name": "latency_ms",
                "direction": "lower_is_better",
                "json_path": "$.metrics.latency_ms",
                "min_improvement": 1.5,
                "unchanged_tolerance": 0.25,
            },
        )
        self.assertEqual(
            task["execution"],
            {
                "strategy": "iterative",
                "max_iterations": 5,
                "rollback_on_failure": True,
                "keep_on_equal": True,
                "commit_prefix": "experiment",
                "stop_after_no_improvement": 2,
                "stop_after_invalid": 3,
            },
        )
        self.assertEqual(task["execution_mode"], "experiment")
        self.assertEqual(task["experiment"]["goal_metric"]["name"], "latency_ms")
        self.assertEqual(task["experiment"]["goal_metric"]["json_path"], "$.metrics.latency_ms")

    def test_get_recent_project_learning_summaries_formats_entries(self) -> None:
        data = {
            "project": "demo",
            "learning_journal": [
                {"task_id": "P0-1", "summary": "Prefer CMake presets for validation"},
                {"task_id": "P0-2", "summary": "Track header paths instead of build outputs"},
            ],
            "tasks": [],
        }

        summaries = get_recent_project_learning_summaries(data, limit=2)

        self.assertEqual(
            summaries,
            [
                "P0-1: Prefer CMake presets for validation",
                "P0-2: Track header paths instead of build outputs",
            ],
        )

    def test_get_task_counts_normalizes_string_status_flags(self) -> None:
        data = {
            "project": "demo",
            "tasks": [
                {"id": "P0-1", "title": "done", "passes": "true", "blocked": False},
                {"id": "P0-2", "title": "stuck", "passes": False, "blocked": "yes"},
                {"id": "P0-3", "title": "next", "passes": "false", "blocked": ""},
            ],
        }

        counts = get_task_counts(data)

        self.assertEqual(
            counts,
            {
                "total": 3,
                "completed": 1,
                "blocked": 1,
                "pending": 1,
            },
        )

    def test_get_next_task_skips_completed_and_blocked_string_flags(self) -> None:
        data = {
            "project": "demo",
            "tasks": [
                {"id": "P0-1", "title": "done", "passes": "true", "blocked": False},
                {"id": "P0-2", "title": "blocked", "passes": False, "blocked": "yes"},
                {"id": "P0-3", "title": "next", "passes": False, "blocked": False},
            ],
        }

        task = get_next_task(data)

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task["id"], "P0-3")

    def test_get_next_task_can_include_blocked_string_flags(self) -> None:
        data = {
            "project": "demo",
            "tasks": [
                {"id": "P0-1", "title": "done", "passes": True, "blocked": False},
                {"id": "P0-2", "title": "blocked", "passes": False, "blocked": "yes"},
                {"id": "P0-3", "title": "next", "passes": False, "blocked": False},
            ],
        }

        task = get_next_task(data, include_blocked=True)

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task["id"], "P0-2")


if __name__ == "__main__":
    unittest.main()
