import os
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from fastapi import APIRouter, HTTPException

APPS_BASE = os.environ.get("WORKPENT_APPS_BASE", "/srv/workpent/apps")

router = APIRouter(prefix="/apps/{app_name}/git", tags=["git"])


@dataclass
class GitResult:
    code: int
    stdout: str
    stderr: str


def get_app_path(app_name: str) -> str:
    app_path = os.path.join(APPS_BASE, app_name)
    if not os.path.isdir(app_path):
        raise HTTPException(status_code=404, detail=f"App '{app_name}' not found at {app_path}")
    return app_path


def run_git(app_path: str, args: List[str]) -> GitResult:
    """
    Run a git command in the given app_path and capture output.
    """
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=app_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="git is not installed on this server.")

    return GitResult(code=proc.returncode, stdout=proc.stdout.strip(), stderr=proc.stderr.strip())


def ensure_git_repo(app_path: str) -> None:
    """
    Make sure this folder is a git repo. We don't auto-init; we just validate.
    """
    res = run_git(app_path, ["rev-parse", "--is-inside-work-tree"])
    if res.code != 0 or res.stdout.strip() != "true":
        raise HTTPException(
            status_code=400,
            detail="This app is not a git repository yet. Run 'git init' and set a remote first.",
        )


@router.get("/status")
def git_status(app_name: str):
    """
    Basic git status for an app:
    - current branch
    - remote URL
    - short status (changed files)
    """
    app_path = get_app_path(app_name)
    ensure_git_repo(app_path)

    branch = run_git(app_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    remote = run_git(app_path, ["remote", "-v"])
    status_short = run_git(app_path, ["status", "--short"])

    return {
        "branch": branch.stdout or None,
        "remote": remote.stdout or None,
        "status": status_short.stdout or "",
        "ok": branch.code == 0 and remote.code == 0 and status_short.code == 0,
        "errors": {
            "branch": branch.stderr,
            "remote": remote.stderr,
            "status": status_short.stderr,
        },
    }


@router.get("/commits")
def git_commits(app_name: str, limit: int = 10):
    """
    Last N commits for the app.
    """
    app_path = get_app_path(app_name)
    ensure_git_repo(app_path)

    fmt = "%h|%an|%ad|%s"
    res = run_git(app_path, ["log", f"-{limit}", f"--pretty=format:{fmt}"])
    if res.code != 0:
        raise HTTPException(status_code=500, detail=res.stderr or "Failed to read git log.")

    commits = []
    if res.stdout:
        for line in res.stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commit_hash, author, date_str, msg = parts
                commits.append(
                    {
                        "hash": commit_hash,
                        "author": author,
                        "date": date_str,
                        "message": msg,
                    }
                )

    return {"items": commits}


class CommitRequestModel(dict):
    """
    Simple schema-like dict for request body parsing.
    """
    message: str
    stage_all: bool = True


@router.post("/commit")
def git_commit(app_name: str, payload: CommitRequestModel):
    """
    Stage and commit changes.
    """
    app_path = get_app_path(app_name)
    ensure_git_repo(app_path)

    message = (payload.get("message") or "").strip()
    stage_all = bool(payload.get("stage_all", True))

    if not message:
        raise HTTPException(status_code=400, detail="Commit message is required.")

    if stage_all:
        add_res = run_git(app_path, ["add", "."])
        if add_res.code != 0:
            raise HTTPException(status_code=500, detail=add_res.stderr or "git add failed.")

    commit_res = run_git(app_path, ["commit", "-m", message])
    if commit_res.code != 0:
        # Common case: nothing to commit
        if "nothing to commit" in commit_res.stderr.lower():
            return {
                "ok": False,
                "message": "Nothing to commit (working tree clean).",
                "stdout": commit_res.stdout,
                "stderr": commit_res.stderr,
            }
        raise HTTPException(status_code=500, detail=commit_res.stderr or "git commit failed.")

    return {
        "ok": True,
        "message": "Commit created.",
        "stdout": commit_res.stdout,
        "stderr": commit_res.stderr,
    }


class PushPullRequestModel(dict):
    """
    Simple schema-like request body for push/pull.
    """
    remote: Optional[str]
    branch: Optional[str]


def get_current_branch(app_path: str) -> str:
    res = run_git(app_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if res.code != 0 or not res.stdout:
        raise HTTPException(status_code=500, detail=res.stderr or "Failed to detect current branch.")
    return res.stdout.strip()


@router.post("/push")
def git_push(app_name: str, payload: PushPullRequestModel):
    """
    Push branch to remote (default: origin current-branch)
    """
    app_path = get_app_path(app_name)
    ensure_git_repo(app_path)

    remote = (payload.get("remote") or "origin").strip()
    branch = (payload.get("branch") or "").strip() or get_current_branch(app_path)

    push_res = run_git(app_path, ["push", remote, branch])

    if push_res.code != 0:
        raise HTTPException(status_code=500, detail=push_res.stderr or "git push failed.")

    return {"ok": True, "stdout": push_res.stdout, "stderr": push_res.stderr}


@router.post("/pull")
def git_pull(app_name: str, payload: PushPullRequestModel):
    """
    Pull branch from remote (default: origin current-branch)
    """
    app_path = get_app_path(app_name)
    ensure_git_repo(app_path)

    remote = (payload.get("remote") or "origin").strip()
    branch = (payload.get("branch") or "").strip() or get_current_branch(app_path)

    pull_res = run_git(app_path, ["pull", remote, branch])

    if pull_res.code != 0:
        raise HTTPException(status_code=500, detail=pull_res.stderr or "git pull failed.")

    return {"ok": True, "stdout": pull_res.stdout, "stderr": pull_res.stderr}


@router.get("/diff-summary")
def git_diff_summary(app_name: str):
    """
    Very small 'AI helper' stub: gives a human-readable summary of current diff.

    Later we can feed this into a real LLM to propose commit messages.
    """
    app_path = get_app_path(app_name)
    ensure_git_repo(app_path)

    diff_res = run_git(app_path, ["diff", "--stat"])
    if diff_res.code != 0:
        raise HTTPException(status_code=500, detail=diff_res.stderr or "git diff failed.")

    summary_lines = []
    if diff_res.stdout:
        for line in diff_res.stdout.splitlines():
            summary_lines.append(line.strip())

    return {
        "ok": True,
        "summary": summary_lines,
    }

