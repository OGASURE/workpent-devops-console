from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
from pathlib import Path
from shutil import copytree
import uuid
import time

router = APIRouter(prefix="/hyperdev", tags=["HyperDev"])

APPS_BASE = Path("/srv/workpent/apps")
ARCHETYPES_BASE = Path("/srv/workpent/devops-console/hyperdev/archetypes")
JOBS = {}  # in-memory job tracker (Phase 2 v0)


class GenerateRequest(BaseModel):
    prompt: str
    archetype: str
    app_name: str
    description: str | None = None


def run_generation(job_id: str, data: GenerateRequest):
    """Background generator (engine only, no UI here)."""
    try:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["logs"].append("Starting generation…")

        time.sleep(1)

        # Resolve paths
        app_dir = APPS_BASE / data.app_name
        tpl_dir = ARCHETYPES_BASE / data.archetype / "template"

        if not tpl_dir.exists():
            raise Exception(f"Missing archetype template: {tpl_dir}")

        # Create app dir and prevent overwrite
        app_dir.mkdir(parents=True, exist_ok=True)
        if any(app_dir.iterdir()):
            raise Exception(f"App directory is not empty: {app_dir}")

        JOBS[job_id]["logs"].append(f"Copying template: {data.archetype}")
        copytree(tpl_dir, app_dir, dirs_exist_ok=True)

        # Replace placeholders in text files
        desc = data.description or data.prompt
        for p in app_dir.rglob("*"):
            if p.is_dir():
                continue
            # Skip binaries (future-proof)
            if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".ico"}:
                continue
            try:
                txt = p.read_text(encoding="utf-8")
            except Exception:
                continue
            txt = txt.replace("__APP_NAME__", data.app_name).replace("__APP_DESC__", desc)
            p.write_text(txt, encoding="utf-8")

        JOBS[job_id]["logs"].append(f"App scaffold created at {app_dir}")
        JOBS[job_id]["status"] = "done"

    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["logs"].append(str(e))


@router.post("/generate")
async def generate_app(data: GenerateRequest, bg: BackgroundTasks):
    job_id = str(uuid.uuid4())

    JOBS[job_id] = {
        "status": "queued",
        "logs": ["Job queued"],
        "app": data.app_name
    }

    bg.add_task(run_generation, job_id, data)

    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str):
    return JOBS.get(job_id, {"error": "Job not found"})

