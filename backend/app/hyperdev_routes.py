# app/hyperdev_routes.py

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------

APPS_BASE_DIR = Path("/srv/workpent/apps").resolve()

# How many lines we keep per job (avoid memory bloat)
JOB_MAX_LINES = 800

# File listing limits (avoid UI freeze)
LIST_MAX_FILES = 800
LIST_MAX_DEPTH = 8


# -------------------------------------------------------------------
# Utilities (SAFE path + file listing)
# -------------------------------------------------------------------

def safe_app_root(app_name: str) -> Path:
    """
    Resolve /srv/workpent/apps/<app_name> safely.
    Prevents path traversal (e.g. ../../etc).
    """
    root = (APPS_BASE_DIR / app_name).resolve()
    base = str(APPS_BASE_DIR) + "/"
    if not str(root).startswith(base):
        raise ValueError("Invalid app name/path traversal detected")
    return root


def list_files(root: Path, max_files: int = LIST_MAX_FILES, max_depth: int = LIST_MAX_DEPTH) -> List[str]:
    """
    Return a sorted list of files relative to root.
    Skips hidden folders and bulky dirs, and truncates for performance.
    """
    ignore_dirs = {
        ".git",
        "__pycache__",
        ".venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".ruff_cache",
        ".idea",
        ".vscode",
    }

    root = root.resolve()
    base_depth = len(root.parts)
    results: List[str] = []

    for p in root.rglob("*"):
        if len(results) >= max_files:
            results.append("... (truncated)")
            break

        # depth limit
        if (len(p.parts) - base_depth) > max_depth:
            continue

        # ignore bulky dirs
        if any(part in ignore_dirs for part in p.parts):
            continue

        # ignore hidden paths (but allow ".env" if you want—currently skipped)
        # if any(part.startswith(".") for part in p.parts):
        #     continue

        if p.is_file():
            try:
                results.append(str(p.relative_to(root)))
            except Exception:
                continue

    return sorted(results)


# -------------------------------------------------------------------
# Simple in-memory Job Engine
# -------------------------------------------------------------------

@dataclass
class Job:
    id: str
    app_name: str
    instruction: str
    status: str = "running"  # running | done | error
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    lines: List[str] = field(default_factory=list)
    cursor: int = 0  # last-read pointer for streaming
    error: Optional[str] = None


JOBS: Dict[str, Job] = {}
JOBS_LOCK = asyncio.Lock()


def _push(job: Job, line: str) -> None:
    job.lines.append(line)
    # trim
    if len(job.lines) > JOB_MAX_LINES:
        job.lines = job.lines[-JOB_MAX_LINES:]


async def _run_job(job: Job) -> None:
    """
    Executes the HyperDev instruction in a SAFE, minimal way.
    For now we support:
      - list files / show files / file list
    Everything else returns a safe stub "plan" so UI stays alive.
    """
    try:
        _push(job, f"[hello] job {job.id} (running)")
        _push(job, "• Job started")
        await asyncio.sleep(0.1)

        # Resolve app root
        root = safe_app_root(job.app_name)
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"App folder not found: {root}")

        _push(job, "• Received instruction")
        await asyncio.sleep(0.1)
        _push(job, "• Analyzing app context (safe stub)")
        await asyncio.sleep(0.2)

        instruction_lc = (job.instruction or "").strip().lower()

        # ---- Feature: LIST FILES ----
        if ("list files" in instruction_lc) or ("show files" in instruction_lc) or ("file list" in instruction_lc):
            _push(job, "• Collecting file list…")
            files = list_files(root)
            _push(job, "• Done.")
            _push(job, "")
            _push(job, "Files in app:")
            for f in files:
                _push(job, f"- {f}")

            _push(job, "")
            _push(job, "[final] status=done")
            job.status = "done"
            job.finished_at = time.time()
            return

        # ---- Default safe behavior for everything else (for now) ----
        _push(job, "• Drafting a build plan")
        await asyncio.sleep(0.25)
        _push(job, "• Done (no changes applied in Step 1.1)")
        _push(job, "• Job finished")
        _push(job, "")
        _push(job, "[final] status=done")
        job.status = "done"
        job.finished_at = time.time()

    except Exception as e:
        job.status = "error"
        job.error = str(e)
        job.finished_at = time.time()
        _push(job, f"[final] status=error")
        _push(job, f"Error: {job.error}")


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------

@router.post("/api/console/apps/{app_name}/hyperdev/run")
async def hyperdev_run(request: Request, app_name: str):
    """
    Starts a HyperDev job.
    Expected payload from UI can be flexible, but ideally:
      { "instruction": "list files in this app", "mode": "...", "provider": "..." }
    """
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        # allow empty / non-json
        payload = {}

    instruction = (payload.get("instruction") or payload.get("message") or payload.get("prompt") or "").strip()
    if not instruction:
        instruction = "list files in this app"

    job_id = uuid.uuid4().hex
    job = Job(id=job_id, app_name=app_name, instruction=instruction)

    async with JOBS_LOCK:
        JOBS[job_id] = job

    # run in background
    asyncio.create_task(_run_job(job))

    return JSONResponse(
        {
            "ok": True,
            "job_id": job_id,
            "status": "running",
            "app_name": app_name,
        }
    )


@router.get("/api/console/apps/{app_name}/hyperdev/stream/{job_id}")
async def hyperdev_stream(app_name: str, job_id: str):
    """
    Streams job output incrementally.
    UI can poll this endpoint.
    Returns new lines since last poll.
    """
    async with JOBS_LOCK:
        job = JOBS.get(job_id)

    if not job or job.app_name != app_name:
        return JSONResponse({"ok": False, "error": "job_not_found"}, status_code=404)

    # Return only "new" lines since last cursor.
    new_lines = job.lines[job.cursor :]
    job.cursor = len(job.lines)

    return JSONResponse(
        {
            "ok": True,
            "job_id": job.id,
            "status": job.status,
            "running": job.status == "running",
            "done": job.status in ("done", "error"),
            "error": job.error,
            "lines": new_lines,
        }
    )

