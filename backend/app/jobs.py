# backend/app/jobs.py
from __future__ import annotations

import os
import time
import uuid
import traceback
from dataclasses import dataclass, field
from threading import Lock, Thread
from typing import Any, Dict, Optional, Literal

JobState = Literal["queued", "running", "success", "error"]


def is_debug_enabled() -> bool:
    """
    DEBUG_JOBS can be:
      1 / true / yes / on  => enabled
      anything else        => disabled
    """
    v = os.environ.get("DEBUG_JOBS", "").strip().lower()
    return v in ("1", "true", "yes", "on")


@dataclass
class Job:
    job_id: str
    status: JobState = "queued"
    progress: float = 0.0
    message: str = ""
    result: Optional[Dict[str, Any]] = None

    # error fields
    error: Optional[str] = None
    error_type: Optional[str] = None
    error_trace: Optional[str] = None

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_public_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "job_id": self.job_id,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "result": self.result,
            "error": self.error,
        }
        if is_debug_enabled():
            data["error_type"] = self.error_type
            data["error_trace"] = self.error_trace
        return data


class JobStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._jobs: Dict[str, Job] = {}

    def create(self) -> Job:
        job_id = uuid.uuid4().hex
        job = Job(job_id=job_id, status="queued", progress=0.0, message="Queued")
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def update(
        self,
        job_id: str,
        *,
        status: Optional[JobState] = None,
        progress: Optional[float] = None,
        message: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        error_type: Optional[str] = None,
        error_trace: Optional[str] = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return

            if status is not None:
                job.status = status
            if progress is not None:
                job.progress = max(0.0, min(1.0, float(progress)))
            if message is not None:
                job.message = message
            if result is not None:
                job.result = result

            if error is not None:
                job.error = error
            if error_type is not None:
                job.error_type = error_type
            if error_trace is not None:
                job.error_trace = error_trace

            job.updated_at = time.time()


job_store = JobStore()


def run_create_server_job(job_id: str, payload: Dict[str, Any]) -> None:
    """
    Replace this with REAL provisioning logic.
    For now: deterministic fake job with progress updates.
    """
    try:
        job_store.update(job_id, status="running", progress=0.05, message="Starting")

        # staged progress so UI can show movement
        steps = [
            (0.15, "Validating payload"),
            (0.30, "Allocating resources"),
            (0.55, "Provisioning server"),
            (0.75, "Configuring networking"),
            (0.90, "Finalizing"),
        ]
        for p, msg in steps:
            time.sleep(0.7)
            job_store.update(job_id, progress=p, message=msg)

        # fake "result"
        result = {
            "server_name": payload.get("name"),
            "region": payload.get("region"),
            "ip": "203.0.113.10",
        }

        job_store.update(
            job_id,
            status="success",
            progress=1.0,
            message="Done",
            result=result,
            error=None,
            error_type=None,
            error_trace=None,
        )

    except Exception as e:
        tb = traceback.format_exc()
        # Make error string include type like: "RuntimeError: forced test error"
        err_type = type(e).__name__
        err_str = f"{err_type}: {e}"
        job_store.update(
            job_id,
            status="error",
            message="Failed",
            error=err_str,
            error_type=err_type,
            error_trace=tb,
            progress=1.0,
        )


def spawn_job(job_id: str, payload: Dict[str, Any]) -> None:
    def _runner() -> None:
        # safety: never allow thread to die without updating job
        try:
            run_create_server_job(job_id, payload)
        except Exception as e:
            tb = traceback.format_exc()
            err_type = type(e).__name__
            err_str = f"{err_type}: {e}"
            job_store.update(
                job_id,
                status="error",
                message="Failed (thread crash)",
                error=err_str,
                error_type=err_type,
                error_trace=tb,
                progress=1.0,
            )

    Thread(target=_runner, daemon=True).start()

