from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path
import subprocess

router = APIRouter(prefix="/git", tags=["Git War Room"])

APPS_BASE = Path("/srv/workpent/apps")


def _app_dir(app_name: str) -> Path:
    p = APPS_BASE / app_name
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=404, detail=f"App not found: {app_name}")
    return p


def _run(cmd: list[str], cwd: Path) -> str:
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail={"cmd": " ".join(cmd), "stdout": out, "stderr": err, "code": p.returncode},
        )
    return out


def _is_repo(cwd: Path) -> bool:
    return (cwd / ".git").exists()


class CommitReq(BaseModel):
    message: str


class RemoteReq(BaseModel):
    url: str  # e.g. git@github.com:OGASURE/repo.git


class PushReq(BaseModel):
    branch: str | None = None


@router.get("/status/{app_name}")
def status(app_name: str):
    cwd = _app_dir(app_name)
    if not _is_repo(cwd):
        return {"app": app_name, "is_repo": False}

    branch = _run(["git", "branch", "--show-current"], cwd) or "main"
    porcelain = _run(["git", "status", "--porcelain"], cwd)
    return {"app": app_name, "is_repo": True, "branch": branch, "porcelain": porcelain}


@router.get("/log/{app_name}")
def log(app_name: str, n: int = 20):
    cwd = _app_dir(app_name)
    if not _is_repo(cwd):
        raise HTTPException(status_code=400, detail="Not a git repo")

    out = _run(["git", "log", f"--max-count={n}", "--oneline", "--decorate"], cwd)
    items = [x for x in out.splitlines() if x.strip()]
    return {"app": app_name, "items": items}


@router.get("/branches/{app_name}")
def branches(app_name: str):
    cwd = _app_dir(app_name)
    if not _is_repo(cwd):
        raise HTTPException(status_code=400, detail="Not a git repo")

    out = _run(["git", "branch", "-a"], cwd)
    lines = [x.strip() for x in out.splitlines() if x.strip()]
    current = None
    for ln in lines:
        if ln.startswith("* "):
            current = ln[2:].strip()
            break
    return {"app": app_name, "current": current, "branches": lines}


@router.post("/commit/{app_name}")
def commit(app_name: str, body: CommitReq):
    cwd = _app_dir(app_name)
    if not _is_repo(cwd):
        raise HTTPException(status_code=400, detail="Not a git repo")

    _run(["git", "add", "."], cwd)
    out = _run(["git", "commit", "-m", body.message], cwd)
    return {"app": app_name, "result": out}


@router.post("/set-remote/{app_name}")
def set_remote(app_name: str, body: RemoteReq):
    cwd = _app_dir(app_name)
    if not _is_repo(cwd):
        raise HTTPException(status_code=400, detail="Not a git repo")

    subprocess.run(["git", "remote", "remove", "origin"], cwd=str(cwd), capture_output=True, text=True)
    _run(["git", "remote", "add", "origin", body.url], cwd)
    return {"app": app_name, "origin": body.url}


@router.post("/push/{app_name}")
def push(app_name: str, body: PushReq = PushReq()):
    cwd = _app_dir(app_name)
    if not _is_repo(cwd):
        raise HTTPException(status_code=400, detail="Not a git repo")

    branch = body.branch or (_run(["git", "branch", "--show-current"], cwd) or "main")
    _run(["git", "push", "-u", "origin", branch], cwd)
    return {"app": app_name, "pushed": branch}


@router.post("/pull/{app_name}")
def pull(app_name: str):
    cwd = _app_dir(app_name)
    if not _is_repo(cwd):
        raise HTTPException(status_code=400, detail="Not a git repo")

    out = _run(["git", "pull", "--ff-only"], cwd)
    return {"app": app_name, "result": out}

