"""Web dashboard server for multi-project autodev management.

Launch with ``autodev web`` to serve the dashboard at http://127.0.0.1:8080.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse
    import uvicorn
except ImportError:
    raise ImportError(
        "The web dashboard requires FastAPI and uvicorn.\n"
        "Install them with:  pip install autodev[web]"
    )

from pydantic import BaseModel


def create_app() -> FastAPI:
    """Create and return the FastAPI application."""

    app = FastAPI(title="autodev web dashboard")

    _dashboard_html = _load_dashboard_html()

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _dashboard_html

    @app.get("/api/projects")
    async def list_projects():
        project_dirs = _discover_projects()
        projects = _build_project_list(project_dirs)
        return {"projects": projects, "active_sessions": len(project_dirs)}

    @app.post("/api/projects")
    async def create_project(req: CreateProjectRequest):
        project_dir = Path(req.directory).expanduser().resolve()

        if project_dir.exists() and (project_dir / "autodev.toml").exists():
            # Already initialized — just plan and start
            pass
        else:
            project_dir.mkdir(parents=True, exist_ok=True)
            try:
                _run_autodev_cmd(
                    ["autodev", "init", ".", "--use", req.backend, "--name", req.name or project_dir.name],
                    cwd=project_dir,
                )
            except RuntimeError as e:
                raise HTTPException(500, f"项目初始化失败: {e}")

        if req.intent:
            def _plan_and_start():
                try:
                    _run_autodev_cmd(
                        ["autodev", "plan", "--intent", req.intent],
                        cwd=project_dir,
                        timeout=None,
                    )
                    if req.auto_start:
                        _run_autodev_cmd(
                            ["autodev", "run", "--detach"],
                            cwd=project_dir,
                        )
                except RuntimeError:
                    pass

            threading.Thread(target=_plan_and_start, daemon=True).start()

        # Remember this project for the dashboard
        _known_projects[req.name or project_dir.name] = project_dir

        return {"status": "created", "name": req.name or project_dir.name, "directory": str(project_dir)}

    @app.get("/api/projects/{name}/status")
    async def project_status(name: str):
        project_dir = _resolve_project(name)
        return _load_project_status(project_dir, name)

    @app.get("/api/projects/{name}/tasks")
    async def project_tasks(name: str):
        project_dir = _resolve_project(name)
        return _load_project_tasks(project_dir)

    @app.get("/api/projects/{name}/log")
    async def project_log(name: str, lines: int = 100):
        project_dir = _resolve_project(name)
        return _load_project_log(project_dir, lines)

    @app.post("/api/projects/{name}/start")
    async def start_project(name: str):
        project_dir = _resolve_project(name)
        try:
            _run_autodev_cmd(["autodev", "run", "--detach"], cwd=project_dir)
        except RuntimeError as e:
            raise HTTPException(500, f"启动失败: {e}")
        return {"status": "started", "name": name}

    @app.post("/api/projects/{name}/stop")
    async def stop_project(name: str):
        project_dir = _resolve_project(name)
        config = _load_project_config(project_dir)
        prefix = config.get("detach", {}).get("tmux_session_prefix", "autodev")
        session_name = f"{prefix}-{name}"

        from autodev.tmux_session import kill_session
        killed = kill_session(session_name)
        if not killed:
            raise HTTPException(404, f"未找到运行中的会话: {session_name}")
        return {"status": "stopped", "name": name}

    @app.get("/api/sessions")
    async def list_sessions():
        from autodev.tmux_session import list_autodev_sessions
        return {"sessions": list_autodev_sessions()}

    return app


# Projects we have seen during this server lifetime.
# Survives tmux session exits so the dashboard keeps showing them.
_known_projects: dict[str, Path] = {}


def _discover_projects() -> dict[str, Path]:
    """Discover projects from active tmux sessions and previously seen projects."""
    from autodev.tmux_session import list_autodev_sessions

    # Add projects from active tmux sessions
    for session in list_autodev_sessions():
        pane_path = session.get("pane_path", "")
        if not pane_path:
            continue
        project_dir = Path(pane_path)
        if not project_dir.is_dir():
            continue
        if not (project_dir / "autodev.toml").exists():
            continue
        _known_projects[project_dir.name] = project_dir

    # Return all known projects (prune deleted directories)
    to_remove = [k for k, v in _known_projects.items() if not v.is_dir()]
    for k in to_remove:
        del _known_projects[k]

    return dict(_known_projects)


def _resolve_project(name: str) -> Path:
    """Resolve a project by name."""
    project_dirs = _discover_projects()
    project_dir = project_dirs.get(name)
    if project_dir is None:
        raise HTTPException(404, f"项目 '{name}' 未找到")
    return project_dir


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class CreateProjectRequest(BaseModel):
    directory: str
    name: str = ""
    intent: str = ""
    backend: str = "codex"
    auto_start: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_dashboard_html() -> str:
    html_path = Path(__file__).parent / "web_dashboard.html"
    return html_path.read_text(encoding="utf-8")


def _build_project_list(project_dirs: dict[str, Path]) -> list[dict]:
    """Build project summary list from the discovered project directories."""
    from autodev.tmux_session import is_session_alive

    projects = []
    for name, entry in sorted(project_dirs.items()):
        config = _load_project_config(entry)
        prefix = config.get("detach", {}).get("tmux_session_prefix", "autodev")
        session_name = f"{prefix}-{name}"
        alive = is_session_alive(session_name)

        # Load runtime status if available
        status_path = entry / "logs" / "runtime-status.json"
        run_status = "idle"
        counts = {"total": 0, "completed": 0, "blocked": 0, "pending": 0, "running": 0}
        current_task = ""
        backend = config.get("backend", {}).get("default", "codex")

        if status_path.exists():
            try:
                status_data = json.loads(status_path.read_text(encoding="utf-8"))
                run_info = status_data.get("run", {})
                run_status = run_info.get("status", "idle")
                counts = status_data.get("counts", counts)
                if run_info.get("current_task_title"):
                    current_task = run_info["current_task_title"]
                backend = status_data.get("backend", backend)
            except (json.JSONDecodeError, OSError):
                pass

        # Check if planning is in progress
        task_json = entry / "task.json"
        if not task_json.exists() and (entry / "autodev.toml").exists():
            run_status = "planning"

        projects.append({
            "name": name,
            "path": str(entry),
            "backend": backend,
            "status": run_status,
            "session_alive": alive,
            "counts": counts,
            "current_task": current_task,
        })

    return projects


def _load_project_config(project_dir: Path) -> dict:
    """Load autodev.toml as raw dict (lightweight, no validation)."""
    toml_path = project_dir / "autodev.toml"
    if not toml_path.exists():
        return {}
    try:
        import sys
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib
        with open(toml_path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _load_project_status(project_dir: Path, name: str) -> dict:
    """Load full project status for detail view."""
    from autodev.tmux_session import is_session_alive

    config = _load_project_config(project_dir)
    prefix = config.get("detach", {}).get("tmux_session_prefix", "autodev")
    session_name = f"{prefix}-{name}"
    alive = is_session_alive(session_name)

    status_path = project_dir / "logs" / "runtime-status.json"
    if status_path.exists():
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = _default_status(name, config)
    else:
        data = _default_status(name, config)

    data["session_alive"] = alive
    return data


def _default_status(name: str, config: dict) -> dict:
    return {
        "project": name,
        "backend": config.get("backend", {}).get("default", "codex"),
        "run": {
            "status": "idle",
            "current_task_id": "",
            "current_task_title": "",
            "current_epoch": 1,
            "max_epochs": 1,
            "current_attempt": 0,
            "max_attempts": 0,
            "heartbeat_elapsed_seconds": 0,
            "updated_at": "",
        },
        "counts": {"total": 0, "completed": 0, "blocked": 0, "pending": 0, "running": 0},
        "tasks": [],
        "events": [],
    }


def _load_project_tasks(project_dir: Path) -> dict:
    """Load task.json for a project."""
    task_path = project_dir / "task.json"
    if not task_path.exists():
        return {"tasks": []}
    try:
        data = json.loads(task_path.read_text(encoding="utf-8"))
        tasks = data.get("tasks", [])
        return {
            "tasks": [
                {
                    "id": t.get("id", ""),
                    "title": t.get("title", t.get("name", "")),
                    "status": _task_display_status(t),
                }
                for t in tasks
            ]
        }
    except (json.JSONDecodeError, OSError):
        return {"tasks": []}


def _task_display_status(task: dict) -> str:
    """Determine display status for a task."""
    if task.get("blocked"):
        return "blocked"
    if task.get("passes"):
        return "completed"
    return "pending"


def _load_project_log(project_dir: Path, tail_lines: int = 100) -> dict:
    """Load last N lines of autodev.log."""
    log_path = project_dir / "logs" / "autodev.log"
    if not log_path.exists():
        return {"lines": []}
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        return {"lines": all_lines[-tail_lines:]}
    except OSError:
        return {"lines": []}


def _run_autodev_cmd(cmd: list[str], cwd: Path, timeout: int | None = 60) -> str:
    """Run an autodev CLI command synchronously."""
    env = dict(os.environ)
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"Command failed: {' '.join(cmd)}")
    return result.stdout


def cmd_web(args: Any) -> int:
    """Handle ``autodev web``."""
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8080)

    app = create_app()
    print(f"autodev 项目管理面板")
    print(f"  地址: http://{host}:{port}")
    print(f"  按 Ctrl+C 停止")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
