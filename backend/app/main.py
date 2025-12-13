# paste that function in the right place, Ctrl+O, Enter, Ctrl+Xfrom __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests

from fastapi import FastAPI, HTTPException, Request, Form, Body
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    FileResponse,
    PlainTextResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.git_routes import router as git_router
from app.hyperdev_routes import router as hyperdev_router

# -------------------------------------------------------------------
# Paths / constants
# -------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

APPS_BASE = Path("/srv/workpent/apps")
SNAPSHOT_BASE = Path("/srv/workpent/snapshots")
DELETED_BASE = Path("/srv/workpent/deleted-apps")
IDEAS_DIR = Path("/srv/workpent/console-ideas")
IDEAS_FILE = IDEAS_DIR / "ideas.md"

for p in (APPS_BASE, STATIC_DIR, SNAPSHOT_BASE, DELETED_BASE, IDEAS_DIR):
    p.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Workpent HyperDev Console")
app.include_router(hyperdev_router)
app.include_router(git_router)

@app.get("/ai-test/groq")
async def ai_test_groq():
    from app import main as m
    result = m._groq_chat("Say: This is Groq test from /ai-test/groq.")
    return {"result": result}


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
@app.get("/create", response_class=HTMLResponse)
async def create_page(request: Request):
    return templates.TemplateResponse("create.html", {"request": request})
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def app_dir(app_name: str) -> Path:
    base = APPS_BASE / app_name
    if not base.is_dir():
        raise HTTPException(status_code=404, detail=f"App '{app_name}' not found")
    return base


def read_env_port(base: Path) -> int | None:
    env_file = base / ".env"
    if not env_file.is_file():
        return None
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("APP_PORT"):
                _, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                try:
                    return int(value)
                except ValueError:
                    return None
    except Exception:
        return None
    return None


def systemd_service_name(app_name: str) -> str:
    # All your app services follow this pattern
    return f"workpent-{app_name}.service"


def get_service_status(app_name: str) -> str:
    service = systemd_service_name(app_name)
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return "active"
        if result.stdout.strip() == "inactive":
            return "inactive"
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def get_app_list() -> List[Dict[str, Any]]:
    apps: List[Dict[str, Any]] = []
    if not APPS_BASE.exists():
        return apps

    for app_path in APPS_BASE.iterdir():
        if not app_path.is_dir():
            continue
        try:
            stat = app_path.stat()
            port = read_env_port(app_path)
            apps.append(
                {
                    "name": app_path.name,
                    "base_path": str(app_path),
                    "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "port": port,
                    "service": systemd_service_name(app_path.name),
                    "status": get_service_status(app_path.name),
                }
            )
        except Exception:
            # Skip unreadable dirs
            continue

    apps.sort(key=lambda a: a["name"])
    return apps


def ensure_under(base: Path, target: Path) -> None:
    base_real = base.resolve()
    target_real = target.resolve()
    if base_real == target_real:
        return
    try:
        target_real.relative_to(base_real)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")


def get_server_stats() -> Dict[str, Any]:
    hostname = socket.gethostname()
    try:
        ip = socket.gethostbyname(hostname)
    except Exception:
        ip = "unknown"

    # Disk
    try:
        usage = shutil.disk_usage("/")
        disk_total_gb = round(usage.total / (1024 ** 3), 2)
        disk_used_gb = round(usage.used / (1024 ** 3), 2)
        disk_free_gb = round(usage.free / (1024 ** 3), 2)
    except Exception:
        disk_total_gb = disk_used_gb = disk_free_gb = 0.0

    # Memory from /proc/meminfo (Linux)
    mem_total_mb = mem_used_mb = 0
    try:
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, val = line.split(":", 1)
                meminfo[key.strip()] = val.strip()
        if "MemTotal" in meminfo and "MemAvailable" in meminfo:
            total_kb = int(meminfo["MemTotal"].split()[0])
            avail_kb = int(meminfo["MemAvailable"].split()[0])
            mem_total_mb = round(total_kb / 1024)
            mem_used_mb = round((total_kb - avail_kb) / 1024)
    except Exception:
        pass

    return {
        "hostname": hostname,
        "ip": ip,
        "disk_total_gb": disk_total_gb,
        "disk_used_gb": disk_used_gb,
        "disk_free_gb": disk_free_gb,
        "mem_total_mb": mem_total_mb,
        "mem_used_mb": mem_used_mb,
    }


# -------------------------------------------------------------------
# Page
# -------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    apps = get_app_list()
    server_stats = get_server_stats()
    utc_now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "apps_json": json.dumps(apps),
            "server_stats_json": json.dumps(server_stats),
            "utc_now": utc_now,
        },
    )


# -------------------------------------------------------------------
# App list / server stats APIs
# -------------------------------------------------------------------

@app.get("/api/console/apps", response_class=JSONResponse)
async def api_apps_list():
    return {"node": socket.gethostname(), "apps": get_app_list()}


@app.get("/api/console/server/stats", response_class=JSONResponse)
async def api_server_stats():
    return get_server_stats()

@app.get("/git-war-room/{app_name}", response_class=HTMLResponse)
async def git_war_room_page(request: Request, app_name: str):
    return templates.TemplateResponse("git_war_room.html", {"request": request, "app_name": app_name})

# -------------------------------------------------------------------
# AI Agent / Ask-AI endpoints  (Groq + Cerebras, pluggable)
# -------------------------------------------------------------------

from pydantic import BaseModel

AI_CONFIG_DIR = BASE_DIR / ".hyperdev"
AI_CONFIG_DIR.mkdir(exist_ok=True)
AI_CONFIG_FILE = AI_CONFIG_DIR / "ai_config.json"

# Groq & Cerebras endpoints + default models
GROQ_API_BASE = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

CEREBRAS_API_BASE = "https://api.cerebras.ai/v1/chat/completions"
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "llama-3.3-70b")


def _groq_chat(prompt: str) -> str:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return "Groq API key missing on server (GROQ_API_KEY)."

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a helpful coding assistant running inside "
                    "Workpent HyperDev Console. Be concise but clear."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }

    try:
        resp = requests.post(
            GROQ_API_BASE, headers=headers, json=data, timeout=60
        )
        resp.raise_for_status()
        payload = resp.json()

        choices = payload.get("choices") or []
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                return content

        return f"[Groq] Unexpected response: {payload}"
    except Exception as e:
        return f"[Groq] Error: {e}"


def _cerebras_chat(prompt: str) -> str:
    api_key = os.environ.get("CEREBRAS_API_KEY")
    if not api_key:
        return "Cerebras API key missing on server (CEREBRAS_API_KEY)."

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = {
        "model": CEREBRAS_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a helpful coding assistant running inside "
                    "Workpent HyperDev Console. Be concise but clear."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }

    try:
        resp = requests.post(
            CEREBRAS_API_BASE, headers=headers, json=data, timeout=60
        )
        resp.raise_for_status()
        payload = resp.json()

        choices = payload.get("choices") or []
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                return content

        return f"[Cerebras] Unexpected response: {payload}"
    except Exception as e:
        return f"[Cerebras] Error: {e}"


def _pick_provider(explicit: str | None) -> str:
    """
    Decide which provider to use.

    Priority:
    1) explicit provider from request body ("groq" / "cerebras")
    2) AI_PROVIDER env ("groq" / "cerebras" / "auto")
    3) fall back to whichever key exists
    """
    if explicit:
        p = explicit.lower()
        if p in ("groq", "cerebras"):
            return p

    env_p = os.environ.get("AI_PROVIDER", "auto").lower()
    if env_p in ("groq", "cerebras"):
        return env_p

    # auto mode – choose based on which key exists
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    if os.environ.get("CEREBRAS_API_KEY"):
        return "cerebras"

    return "none"


async def _do_ai_ask(prompt: str, provider: str | None = None) -> str:
    """
    Core AI dispatcher used by all /ai/ask endpoints.
    """
    which = _pick_provider(provider)
    if which == "groq":
        return _groq_chat(prompt)
    if which == "cerebras":
        return _cerebras_chat(prompt)
    return "No AI provider configured on server."


class AskBody(BaseModel):
    prompt: str
    provider: str | None = None


@app.get("/api/console/ai/config")
async def api_get_ai_config():
    """
    Simple persisted config for the dashboard UI.
    DOES NOT control keys; those stay in systemd env vars.
    """
    if AI_CONFIG_FILE.exists():
        try:
            return json.loads(AI_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


@app.post("/api/console/ai/config")
async def api_save_ai_config(config: dict = Body(...)):
    AI_CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return {"ok": True}


@app.post("/api/console/ai/ask")
async def api_ai_ask(body: AskBody):
    """
    Main Ask-AI endpoint.

    Body:
      { "prompt": "...", "provider": "groq" | "cerebras" | null }

    If provider is omitted, we use AI_PROVIDER env / auto-detect.
    """
    reply = await _do_ai_ask(body.prompt, provider=body.provider)
    return {"ok": True, "reply": reply}


@app.post("/api/console/apps/{app_name}/ai/chat")
async def api_app_ai_chat(app_name: str, payload: dict = Body(...)):
    """
    What the app dashboard 'AI Agent (Mode A)' panel calls.

    The frontend currently sends:
      { "message": "...", "agent_name": "primary" }

    We accept both 'message' and 'prompt' just in case.
    """
    prompt = (payload.get("message") or payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is empty")

    reply = await _do_ai_ask(prompt)
    return {"ok": True, "reply": reply}

# -------------------------------------------------------------------
# Per-app AI endpoints used by the UI (shortlink-api etc.)
# -------------------------------------------------------------------

class AppChatBody(BaseModel):
    prompt: str


@app.get("/api/console/apps/{app_name}/ai/config")
async def api_app_ai_config(app_name: str):
    """
    Very simple per-app AI config so the UI stops 404'ing.
    For now we just tell the UI that it should use Groq via the
    console backend. These values are mostly informational.
    """
    return {
        "provider": "groq",
        "api_url": "https://api.groq.com/openai/v1/chat/completions",
        "api_key_env": "GROQ_API_KEY",
        "model": "llama-3.1-70b-versatile",
    }


@app.get("/api/console/apps/{app_name}/ui/config")
async def api_app_ui_config(app_name: str):
    """
    Placeholder UI config so the app dashboard AI panel doesn't error.
    """
    return {"ok": True}


@app.post("/api/console/apps/{app_name}/ai/chat")
async def api_app_ai_chat(app_name: str, body: AppChatBody):
    """
    What the app dashboard 'AI Agent (Mode A)' panel calls.
    We just forward the prompt into the same _do_ai_ask() core
    dispatcher that /api/console/ai/ask uses.
    """
    reply = await _do_ai_ask(body.prompt)
    return {"ok": True, "reply": reply}


@app.get("/ai-debug/env", response_class=JSONResponse)
async def ai_debug_env():
    """
    Debug endpoint: show what env vars the HyperDev process can see.
    """
    import os

    return {
        "AI_PROVIDER": os.environ.get("AI_PROVIDER"),
        "GROQ_API_KEY": os.environ.get("GROQ_API_KEY"),
        "CEREBRAS_API_KEY": os.environ.get("CEREBRAS_API_KEY"),
    }


# -------------------------------------------------------------------
# Lifecycle controls
# -------------------------------------------------------------------

def run_systemctl(app_name: str, action: str) -> str:
    service = systemd_service_name(app_name)
    try:
        result = subprocess.run(
            ["systemctl", action, service],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return f"{service} {action} OK"
        msg = result.stderr.strip() or result.stdout.strip() or "Unknown systemctl error"
        raise HTTPException(status_code=500, detail=msg)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/console/apps/{app_name}/start")
async def api_app_start(app_name: str):
    app_dir(app_name)
    msg = run_systemctl(app_name, "start")
    return {"ok": True, "message": msg}


@app.post("/api/console/apps/{app_name}/stop")
async def api_app_stop(app_name: str):
    app_dir(app_name)
    msg = run_systemctl(app_name, "stop")
    return {"ok": True, "message": msg}


@app.post("/api/console/apps/{app_name}/restart")
async def api_app_restart(app_name: str):
    app_dir(app_name)
    msg = run_systemctl(app_name, "restart")
    return {"ok": True, "message": msg}


# -------------------------------------------------------------------
# Health / logs / nginx tools
# -------------------------------------------------------------------

@app.get("/api/console/apps/{app_name}/health", response_class=JSONResponse)
async def api_app_health(app_name: str):
    base = app_dir(app_name)
    port = read_env_port(base)
    if not port:
        raise HTTPException(status_code=400, detail="APP_PORT not found in .env")

    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = resp.read(1024).decode("utf-8", errors="ignore")
        return {
            "ok": resp.status == 200,
            "status_code": resp.status,
            "reason": resp.reason,
            "body": body,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Health check error: {e}")
    finally:
        conn.close()


@app.get("/api/console/apps/{app_name}/logs", response_class=PlainTextResponse)
async def api_app_logs(app_name: str, lines: int = 80):
    app_dir(app_name)
    service = systemd_service_name(app_name)
    try:
        result = subprocess.run(
            ["journalctl", "-u", service, "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=result.stderr.strip())
        return result.stdout
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/console/nginx/test", response_class=JSONResponse)
async def api_nginx_test():
    try:
        result = subprocess.run(
            ["nginx", "-t"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        ok = result.returncode == 0
        output = (result.stderr or result.stdout).strip()
        return {"ok": ok, "output": output}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/console/nginx/reload", response_class=JSONResponse)
async def api_nginx_reload():
    try:
        result = subprocess.run(
            ["systemctl", "reload", "nginx"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        ok = result.returncode == 0
        msg = result.stderr.strip() or result.stdout.strip() or "Reload attempted"
        if not ok:
            raise HTTPException(status_code=500, detail=msg)
        return {"ok": True, "message": msg}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# Snapshots / soft delete / download
# -------------------------------------------------------------------

@app.post("/api/console/apps/{app_name}/snapshot", response_class=JSONResponse)
async def api_snapshot(app_name: str):
    base = app_dir(app_name)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    dest = SNAPSHOT_BASE / f"{app_name}-{ts}"
    try:
        shutil.copytree(base, dest)
        return {"ok": True, "message": f"Snapshot created at {dest}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Snapshot failed: {e}")


@app.post("/api/console/apps/{app_name}/restore", response_class=JSONResponse)
async def api_restore(app_name: str):
    base = app_dir(app_name)
    snaps = [
        p
        for p in SNAPSHOT_BASE.glob(f"{app_name}-*")
        if p.is_dir()
    ]
    if not snaps:
        raise HTTPException(status_code=404, detail="No snapshots found")
    snaps.sort(key=lambda p: p.stat().st_mtime)
    last = snaps[-1]

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    temp_move = base.parent / f"{app_name}_current_{ts}"
    deleted_target = DELETED_BASE / f"{app_name}-{ts}"

    try:
        shutil.move(base, temp_move)
        shutil.copytree(last, base)
        shutil.move(temp_move, deleted_target)
    except Exception as e:
        # try to roll back
        if not base.exists() and temp_move.exists():
            shutil.move(temp_move, base)
        raise HTTPException(status_code=500, detail=f"Restore failed: {e}")

    return {
        "ok": True,
        "restored_from": str(last),
        "moved_old_to": str(deleted_target),
    }


@app.post("/api/console/apps/{app_name}/soft-delete", response_class=JSONResponse)
async def api_soft_delete(app_name: str):
    base = app_dir(app_name)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    dest = DELETED_BASE / f"{app_name}-{ts}"
    try:
        shutil.move(base, dest)
        return {"ok": True, "message": f"Moved to {dest}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/console/apps/{app_name}/download")
async def api_download_app(app_name: str):
    base = app_dir(app_name)

    ts = datetime.utcnow().strftime("%Y%m%d")
    zip_name = f"{app_name}-{ts}.zip"
    tmp_dir = SNAPSHOT_BASE  # just use snapshots dir as scratch
    zip_path = tmp_dir / zip_name

    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(base):
            root_path = Path(root)
            # skip .venv
            if ".venv" in root_path.parts:
                continue
            for f in files:
                file_path = root_path / f
                rel = file_path.relative_to(base.parent)
                zf.write(file_path, rel)

    return FileResponse(
        path=str(zip_path),
        filename=zip_name,
        media_type="application/zip",
    )


# -------------------------------------------------------------------
# Files & inline editor
# -------------------------------------------------------------------

@app.get("/api/console/files/{app_name}/ls", response_class=JSONResponse)
async def api_files_ls(app_name: str, path: str = "."):
    base = app_dir(app_name)
    current = (base / path).resolve()
    ensure_under(base, current)
    if not current.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    items = []

    # parent / ..
    if current != base:
        parent_rel = current.parent.relative_to(base).as_posix() or "."
        items.append(
            {"name": "..", "path": parent_rel, "type": "folder"}
        )

    for p in current.iterdir():
        if p.name in {".git", ".venv", "__pycache__", ".DS_Store"}:
            continue
        t = "folder" if p.is_dir() else "file"
        rel = p.relative_to(base).as_posix()
        items.append({"name": p.name, "path": rel, "type": t})

    items.sort(key=lambda x: (x["type"] != "folder", x["name"]))
    return {"ok": True, "items": items, "current_path": path}


@app.get("/api/console/files/{app_name}/read", response_class=JSONResponse)
async def api_file_read(app_name: str, path: str):
    base = app_dir(app_name)
    target = (base / path).resolve()
    ensure_under(base, target)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not UTF-8 text")
    return {"ok": True, "path": path, "content": content}


@app.post("/api/console/files/{app_name}/save", response_class=JSONResponse)
async def api_file_save(app_name: str, path: str = Form(...), content: str = Form(...)):
    base = app_dir(app_name)
    target = (base / path).resolve()
    ensure_under(base, target)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "message": f"Saved {path}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# ENV editor
# -------------------------------------------------------------------

@app.get("/api/console/apps/{app_name}/env", response_class=JSONResponse)
async def api_env_get(app_name: str):
    base = app_dir(app_name)
    env_file = base / ".env"
    if not env_file.exists():
        return {
            "ok": True,
            "path": env_file.as_posix(),
            "content": "# .env not found – will be created on save\n",
        }
    try:
        content = env_file.read_text(encoding="utf-8")
        return {
            "ok": True,
            "path": env_file.as_posix(),
            "content": content,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/console/apps/{app_name}/env", response_class=JSONResponse)
async def api_env_save(app_name: str, content: str = Form(...)):
    base = app_dir(app_name)
    env_file = base / ".env"
    try:
        env_file.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "message": f"ENV saved to {env_file.as_posix()}",
            "path": env_file.as_posix(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# Git cockpit
# -------------------------------------------------------------------

def run_git(base: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """
    Run a git command inside an app folder, with SSH configured to auto-accept
    new host keys and use the oga user's SSH setup.

    This fixes 'Host key verification failed' when pushing from the console.
    """
    env = os.environ.copy()

    # Make sure HOME points at the oga user so SSH finds the right keys.
    # If your username ever changes, update this path.
    env.setdefault("HOME", "/home/oga")

    # Auto-accept new host keys (e.g. first time talking to github.com)
    env["GIT_SSH_COMMAND"] = "ssh -o StrictHostKeyChecking=accept-new"

    return subprocess.run(
        ["git", *args],
        cwd=base,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


@app.get("/api/console/apps/{app_name}/status/git", response_class=JSONResponse)
async def api_git_status(app_name: str):
    base = app_dir(app_name)
    if not (base / ".git").is_dir():
        return {
            "ok": False,
            "status": "(not a git repository)",
            "output": "",
        }
    try:
        res = run_git(base, ["status", "--short", "--branch"], timeout=10)
        if res.returncode != 0:
            return {"ok": False, "status": "error", "output": res.stderr.strip()}
        return {"ok": True, "status": "ok", "output": res.stdout}
    except Exception as e:
        return {"ok": False, "status": "exception", "output": str(e)}


@app.post("/api/console/apps/{app_name}/git/commit", response_class=JSONResponse)
async def api_git_commit(app_name: str, message: str = Form(...)):
    base = app_dir(app_name)
    if not message.strip():
        raise HTTPException(status_code=400, detail="Commit message is empty")
    try:
        res = run_git(base, ["commit", "-am", message], timeout=20)
        if res.returncode != 0:
            raise HTTPException(status_code=500, detail=res.stderr.strip() or res.stdout)
        return {"ok": True, "output": res.stdout}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/console/apps/{app_name}/git/push", response_class=JSONResponse)
async def api_git_push(app_name: str):
    base = app_dir(app_name)
    try:
        res = run_git(base, ["push", "origin", "main"], timeout=60)
        if res.returncode != 0:
            raise HTTPException(status_code=500, detail=res.stderr.strip() or res.stdout)
        return {"ok": True, "output": res.stdout}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/console/apps/{app_name}/git/pull", response_class=JSONResponse)
async def api_git_pull(app_name: str):
    base = app_dir(app_name)
    try:
        res = run_git(base, ["pull", "origin", "main"], timeout=60)
        if res.returncode != 0:
            raise HTTPException(status_code=500, detail=res.stderr.strip() or res.stdout)
        return {"ok": True, "output": res.stdout}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# Pip dependencies (simple helpers)
# -------------------------------------------------------------------

def venv_pip_path(base: Path) -> Path | None:
    pip_bin = base / ".venv" / "bin" / "pip"
    if pip_bin.is_file():
        return pip_bin
    return None


@app.get("/api/console/apps/{app_name}/pip/list", response_class=JSONResponse)
async def api_pip_list(app_name: str):
    base = app_dir(app_name)
    pip_bin = venv_pip_path(base)
    if not pip_bin:
        return {
            "ok": False,
            "output": "No .venv/bin/pip found – create venv first.",
        }
    try:
        res = subprocess.run(
            [str(pip_bin), "list"],
            cwd=base,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "ok": res.returncode == 0,
            "output": res.stdout or res.stderr,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/console/apps/{app_name}/pip/install-reqs", response_class=JSONResponse)
async def api_pip_install_reqs(app_name: str):
    base = app_dir(app_name)
    pip_bin = venv_pip_path(base)
    if not pip_bin:
        return {
            "ok": False,
            "output": "No .venv/bin/pip found – create venv first.",
        }

    req = base / "requirements.txt"
    if not req.is_file():
        return {"ok": False, "output": "requirements.txt not found in app root"}

    try:
        res = subprocess.run(
            [str(pip_bin), "install", "-r", "requirements.txt"],
            cwd=base,
            capture_output=True,
            text=True,
            timeout=600,
        )
        return {
            "ok": res.returncode == 0,
            "output": res.stdout or res.stderr,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# Idea capture (AI App Wizard)
# -------------------------------------------------------------------

@app.post("/api/console/ideas/save", response_class=JSONResponse)
async def api_save_idea(
    idea: str = Form(...),
    app_name: str = Form("general"),
):
    idea = idea.strip()
    if not idea:
        raise HTTPException(status_code=400, detail="Idea is empty")

    IDEAS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    if not IDEAS_FILE.exists():
        IDEAS_FILE.write_text("# HyperDev ideas log\n\n", encoding="utf-8")

    line = f"- [{timestamp}] ({app_name}) {idea}\n"
    with IDEAS_FILE.open("a", encoding="utf-8") as f:
        f.write(line)

    return {"ok": True, "message": "Idea saved."}

