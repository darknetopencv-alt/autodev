import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autodev.web import _load_project_status, _load_project_tasks


class WebStatusTests(unittest.TestCase):
    def test_load_project_tasks_uses_runtime_snapshot_for_running_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "autodev.toml").write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "demo"',
                        'code_dir = "."',
                        "",
                        "[files]",
                        'task_json = "task.json"',
                        'log_dir = "logs"',
                        "",
                        "[backend]",
                        'default = "codex"',
                    ]
                ),
                encoding="utf-8",
            )
            (root / "task.json").write_text(
                """
{
  "project": "demo",
  "tasks": [
    {"id": "P0-1", "title": "foundation", "passes": true, "blocked": false},
    {"id": "P0-2", "title": "build api", "passes": false, "blocked": false}
  ]
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            logs = root / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "runtime-status.json").write_text(
                """
{
  "run": {
    "status": "running",
    "current_task_id": "P0-2",
    "current_task_title": "build api"
  },
  "events": []
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            tasks = _load_project_tasks(root)["tasks"]

            self.assertEqual(
                tasks,
                [
                    {"id": "P0-1", "title": "foundation", "status": "completed"},
                    {"id": "P0-2", "title": "build api", "status": "running"},
                ],
            )

    def test_load_project_status_rebuilds_snapshot_when_runtime_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "autodev.toml").write_text(
                "\n".join(
                    [
                        "[project]",
                        'name = "demo"',
                        'code_dir = "."',
                        "",
                        "[files]",
                        'task_json = "task.json"',
                        'log_dir = "logs"',
                        "",
                        "[backend]",
                        'default = "codex"',
                    ]
                ),
                encoding="utf-8",
            )
            (root / "task.json").write_text(
                """
{
  "project": "demo",
  "tasks": [
    {"id": "P0-1", "title": "done", "passes": "true", "blocked": false},
    {"id": "P0-2", "title": "stuck", "passes": false, "blocked": "yes"},
    {"id": "P0-3", "title": "next", "passes": "false", "blocked": ""}
  ]
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with patch("autodev.tmux_session.is_session_alive", return_value=False):
                status = _load_project_status(root, "demo")

            self.assertEqual(
                status["counts"],
                {
                    "total": 3,
                    "completed": 1,
                    "blocked": 1,
                    "pending": 1,
                    "running": 0,
                },
            )
            self.assertEqual(
                [task["status"] for task in status["tasks"]],
                ["completed", "blocked", "pending"],
            )
            self.assertFalse(status["session_alive"])


if __name__ == "__main__":
    unittest.main()
