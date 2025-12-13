from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
from pathlib import Path
from shutil import copytree
import uuid
import time
import json
import subprocess
from datetime import datetime

router = APIRouter(prefix="/hyperdev", tags=["HyperDev"])

APPS_BASE = Path("/srv/workpent/apps")
ARCHETYPES_BASE = Path("/srv/workpent/devops-console/hyperdev/archetypes")
JOBS_DIR = Path("/srv/workpent/devops-console/hyperdev/jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)

JOBS = {}  # in-memory mirror (jobs are persisted to disk)


class GenerateRequest(BaseModel):
    prompt: str
    archetype: str
    app_name: str
    description: str | None = None

    # Optional Git remote + push
    repo_url: str | None = None   # e.g. git@github.com:ORG/REPO.git
    push: bool = False            # only used if repo_url is provided


def _job_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _save_job(job_id: str):
    """Persist a job snapshot to disk."""
    data = JOBS.get(job_id)
    if not data:
        return
    data["updated_at"] = _now_iso()
    _job_path(job_id).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _log(job_id: str, msg: str):
    JOBS[job_id]["logs"].append(msg)
    _save_job(job_id)


def _run(cmd: list[str], cwd: Path, job_id: str | None = None):
    """Run shell command safely and optionally log output."""
    p = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True
    )
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if job_id is not None:
        if out:
            _log(job_id, out)
        if err:
            _log(job_id, err)
    if p.returncode != 0:
        raise Exception(f"Command failed ({p.returncode}): {' '.join(cmd)}")


def _ensure_gitignore(app_dir: Path, archetype: str):
    gi = app_dir / ".gitignore"
    if gi.exists():
        return

    # simple defaults; we’ll expand per archetype later
    lines = [
        ".DS_Store",
        "__pycache__/",
        "*.pyc",
        "node_modules/",
        ".env",
        ".venv/",
        "dist/",
        "build/",
    ]
    # Chrome extensions often produce zipped builds or temp files
    if archetype == "chrome-extension":
        lines += [
            "*.zip",
            "*.crx",
            "*.pem",
        ]

    gi.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _init_git_repo(app_dir: Path, job_id: str, repo_url: str | None, push: bool):
    _log(job_id, "Initializing git repo…")

    # init
    _run(["git", "init"], cwd=app_dir, job_id=job_id)

    # ensure user identity exists (some servers don't have global config)
    # We set only if missing to avoid overriding your preferences.
    p_name = subprocess.run(["git", "config", "user.name"], cwd=str(app_dir), capture_output=True, text=True)
    p_email = subprocess.run(["git", "config", "user.email"], cwd=str(app_dir), capture_output=True, text=True)
    if not (p_name.stdout or "").strip():
        _run(["git", "config", "user.name", "Workpent HyperDev"], cwd=app_dir, job_id=job_id)
    if not (p_email.stdout or "").strip():
        _run(["git", "config", "user.email", "hyperdev@workpent.com"], cwd=app_dir, job_id=job_id)

    # add + commit
    _run(["git", "add", "."], cwd=app_dir, job_id=job_id)
    _run(["git", "commit", "-m", "Initial scaffold by HyperDev"], cwd=app_dir, job_id=job_id)

    # remote + push (optional)
    if repo_url and push:
        _log(job_id, f"Setting remote origin: {repo_url}")
        _run(["git", "remote", "remove", "origin"], cwd=app_dir, job_id=None)  # ignore errors
        subprocess.run(["git", "remote", "remove", "origin"], cwd=str(app_dir), capture_output=True, text=True)
        _run(["git", "remote", "add", "origin", repo_url], cwd=app_dir, job_id=job_id)

        # main branch
        _run(["git", "branch", "-M", "main"], cwd=app_dir, job_id=job_id)

        _log(job_id, "Pushing to origin/main…")
        _run(["git", "push", "-u", "origin", "main"], cwd=app_dir, job_id=job_id)

    _log(job_id, "Git setup complete ✅")


def run_generation(job_id: str, data: GenerateRequest):
    """Background generator (engine only, no UI here)."""
    try:
        JOBS[job_id]["status"] = "running"
        _save_job(job_id)

        _log(job_id, "Starting generation…")
        time.sleep(0.5)

        app_dir = APPS_BASE / data.app_name
        tpl_dir = ARCHETYPES_BASE / data.archetype / "template"

        if not tpl_dir.exists():
            raise Exception(f"Missing archetype template: {tpl_dir}")

        # create dir and prevent overwriting
        app_dir.mkdir(parents=True, exist_ok=True)
        if any(app_dir.iterdir()):
            raise Exception(f"App directory is not empty: {app_dir}")

        _log(job_id, f"Copying template: {data.archetype}")
        copytree(tpl_dir, app_dir, dirs_exist_ok=True)

        # replace placeholders
        desc = data.description or data.prompt
        for p in app_dir.rglob("*"):
            if p.is_dir():
                continue
            if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".ico"}:
                continue
            try:
                txt = p.read_text(encoding="utf-8")
            except Exception:
                continue
            txt = txt.replace("__APP_NAME__", data.app_name).replace("__APP_DESC__", desc)
            p.write_text(txt, encoding="utf-8")

        _log(job_id, "Template ready ✅")

        # Git init + commit + optional push
        _ensure_gitignore(app_dir, data.archetype)
        _init_git_repo(app_dir, job_id, data.repo_url, data.push)

        JOBS[job_id]["status"] = "done"
        _log(job_id, f"App scaffold created at {app_dir}")

    except Exception as e:
        JOBS[job_id]["status"] = "error"
        _log(job_id, str(e))


@router.post("/generate")
async def generate_app(data: GenerateRequest, bg: BackgroundTasks):
    job_id = str(uuid.uuid4())

    JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "logs": ["Job queued"],
        "app": data.app_name,
        "archetype": data.archetype,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    _save_job(job_id)

    bg.add_task(run_generation, job_id, data)
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str):
    # 1) in-memory
    if job_id in JOBS:
        return JOBS[job_id]

    # 2) disk fallback
    p = _job_path(job_id)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            JOBS[job_id] = data
            return data
        except Exception:
            return {"error": "Job found but failed to read JSON"}

    return {"error": "Job not found"}

