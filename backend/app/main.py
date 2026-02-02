from __future__ import annotations
from app import registry
from app import reconcile

from app import idea_sessions
import json
import os
import time
import shutil
import socket
import subprocess
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import Body, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.git_routes import router as git_router
from app.hyperdev_routes import router as hyperdev_router
from app.hyperdef_http import router as hyperdef_router
from app.jobs import job_store, spawn_job

# DB bootstrap + auth
from app.db import ensure_schema
from app.auth import ensure_seed_admin, require_user


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

# Routers (keep)
app.include_router(hyperdev_router)
app.include_router(git_router)
app.include_router(hyperdef_router)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _startup() -> None:
    ensure_schema()
    ensure_seed_admin()


# -------------------------------------------------------------------
# Auth / Gate
# -------------------------------------------------------------------

PROTECTED_PREFIXES = (
    "/api/console",
    "/api/hyperdef",
    "/hyperdev",
    "/git",
    "/git-war-room",
    "/ai-debug",
    "/ai-test",
)

PUBLIC_PATHS = {"/", "/create", "/docs", "/openapi.json", "/redoc"}
PUBLIC_PREFIXES = ("/static", "/public")


def _auth_required_for_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return False
    for pref in PUBLIC_PREFIXES:
        if path.startswith(pref):
            return False
    for pref in PROTECTED_PREFIXES:
        if path.startswith(pref):
            return True
    return False


@app.middleware("http")
async def api_key_gate(request: Request, call_next):
    """
    Gate protected paths if WORKPENT_API_KEY is configured.

    IMPORTANT: This gate uses app.auth.require_user() which accepts:
      - master key from WORKPENT_API_KEY
      - OR DB-issued API key

    So Nginx can inject the master key, but you can also use DB keys.
    """
    path = request.url.path

    # ✅ Allow trusted internal calls (systemd timers, local agents, localhost jobs)
    client_host = request.client.host if request.client else None
    if client_host in ("127.0.0.1", "::1"):
        return await call_next(request)

    # Public paths always pass
    if not _auth_required_for_path(path):
        return await call_next(request)

    mk = (os.environ.get("WORKPENT_API_KEY") or "").strip()
    if not mk:
        # Dev safety: if not configured, do not lock yourself out
        return await call_next(request)

    got = (request.headers.get("x-api-key") or "").strip()
    if not got:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    try:
        # This validates master key OR DB-issued key
        require_user(request, got)
    except Exception:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    return await call_next(request)


def require_api_key(request: Request, x_api_key: str | None) -> Dict[str, Any]:
    """
    Compatibility helper used by some endpoints that need a user dict.
    """
    mk = (os.environ.get("WORKPENT_API_KEY") or "").strip()
    if not mk:
        return {"id": "dev", "is_admin": 1}
    return require_user(request, x_api_key)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def app_dir(app_name: str) -> Path:
    # Phase 0 security boundary: app_name must exist in registry
    rec = registry.get_app_by_slug(app_name)
    if not rec or rec.get("status") == "DELETED":
        raise HTTPException(status_code=404, detail=f"App '{app_name}' not found (unregistered)")

    base = APPS_BASE / app_name
    if not base.is_dir():
        raise HTTPException(status_code=404, detail=f"App '{app_name}' not found on disk")
    return base


def ensure_under(base: Path, target: Path) -> None:
    base_real = base.resolve()
    target_real = target.resolve()
    if base_real == target_real:
        return
    try:
        target_real.relative_to(base_real)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")


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


def systemd_unit_for_app(app_name: str) -> str:
    """
    Canonical systemd unit name for an app.

    IMPORTANT:
    We intentionally DO NOT use systemd-escape here.
    Our unit files are stored on disk as:
      /etc/systemd/system/workpent-<slug>.service
    and systemctl expects the literal filename (with dashes), e.g.:
      workpent-lifecycle-test.service
    """
    return f"workpent-{app_name}.service"


def get_service_status(app_name: str) -> str:
    """
    Read status for UI list. This should not require sudo.
    """
    unit = systemd_unit_for_app(app_name)
    try:
        res = subprocess.run(
            ["/bin/systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
        )
        out = (res.stdout or "").strip()
        if out:
            return out
        err = (res.stderr or "").strip()
        return err or "unknown"
    except Exception:
        return "unknown"


def get_app_list() -> List[Dict[str, Any]]:
    # Phase 0: source of truth is SQLite registry, not filesystem
    rows = registry.list_apps()
    apps: List[Dict[str, Any]] = []
    for r in rows:
        slug = r.get("slug")
        apps.append(
            {
                "name": slug,
                "app_id": r.get("app_id"),
                "desired_status": r.get("status"),
                "base_path": str(APPS_BASE / slug),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
                "service": systemd_unit_for_app(slug),
                "status": get_service_status(slug),
                "observed_status": r.get("observed_status"),
                "observed_drift_flags": r.get("observed_drift_flags", 0),
                "observed_last_seen_at": r.get("observed_last_seen_at"),
            }
        )
    apps.sort(key=lambda a: a["name"])
    return apps

    for app_path in APPS_BASE.iterdir():
        if not app_path.is_dir():
            continue
        try:
            st = app_path.stat()
            port = read_env_port(app_path)
            apps.append(
                {
                    "name": app_path.name,
                    "base_path": str(app_path),
                    "created_at": datetime.fromtimestamp(st.st_ctime).isoformat(timespec="seconds"),
                    "updated_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                    "port": port,
                    "service": systemd_unit_for_app(app_path.name),
                    "status": get_service_status(app_path.name),
                }
            )
        except Exception:
            continue

    apps.sort(key=lambda a: a["name"])
    return apps


def get_server_stats() -> Dict[str, Any]:
    hostname = socket.gethostname()
    try:
        ip = socket.gethostbyname(hostname)
    except Exception:
        ip = "unknown"

    # Disk
    try:
        usage = shutil.disk_usage("/")
        disk_total_gb = round(usage.total / (1024**3), 2)
        disk_used_gb = round(usage.used / (1024**3), 2)
        disk_free_gb = round(usage.free / (1024**3), 2)
    except Exception:
        disk_total_gb = disk_used_gb = disk_free_gb = 0.0

    # Memory (/proc/meminfo)
    mem_total_mb = mem_used_mb = 0
    try:
        meminfo = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                meminfo[k.strip()] = v.strip()
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


def venv_pip_path(base: Path) -> Path | None:
    pip_bin = base / ".venv" / "bin" / "pip"
    return pip_bin if pip_bin.is_file() else None


# -------------------------------------------------------------------
# Pages
# -------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    apps = get_app_list()
    stats = get_server_stats()
    utc_now = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    # IMPORTANT: dashboard.html expects apps + stats available to JS.
    # Some past breakage came from JSON.parse('') (invalid JSON).
    # We always provide real JSON strings.
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "apps": apps,
            "stats": stats,
            "system_stats": stats,
            "utc_now": utc_now,
            # safe default so templates referencing {{ app.name }} don't explode
            "active_app": {"name": ""},
            "app": {"name": "", "status": "", "port": ""},
        },
    )


@app.get("/create", response_class=HTMLResponse)
async def create_page(request: Request):
    return templates.TemplateResponse("create.html", {"request": request})


@app.get("/git-war-room/{app_name}", response_class=HTMLResponse)
async def git_war_room_page(request: Request, app_name: str):
    return templates.TemplateResponse("git_war_room.html", {"request": request, "app_name": app_name})



# -------------------------------------------------------------------
# systemd drift repair (Phase 0)
# -------------------------------------------------------------------

def _systemd_sweep_orphan_wants() -> Dict[str, Any]:
    """
    Remove orphaned /etc/systemd/system/** symlinks for workpent-*.service where the target unit file is missing.
    This prevents "not-found inactive dead" ghosts after delete/restore cycles.
    """
    base = Path("/etc/systemd/system")
    removed = []
    kept = []
    changed = False

    for link in base.rglob("workpent-*.service"):
        try:
            if not link.is_symlink():
                continue
            if link.name == "workpent-devops-console.service":
                kept.append(str(link))
                continue
            try:
                target = link.resolve(strict=False)
            except Exception:
                target = None

            # If the resolved target doesn't exist -> orphan
            if target is None or not target.exists():
                link.unlink(missing_ok=True)
                removed.append(str(link))
                changed = True
            else:
                kept.append(str(link))
        except Exception:
            # be conservative: if anything unexpected happens, do not delete
            kept.append(str(link))

    if changed:
        subprocess.run(["/bin/systemctl", "daemon-reload"], capture_output=True, text=True, timeout=10)

    return {"removed": removed, "kept": kept, "daemon_reload": bool(changed)}


@app.post("/api/console/systemd/sweep-orphans", response_class=JSONResponse)
async def api_systemd_sweep_orphans():
    return _systemd_sweep_orphan_wants()


# -------------------------------------------------------------------
# Doctor UI (Phase 2)
# -------------------------------------------------------------------

@app.api_route("/doctor", methods=["GET","HEAD"])
async def doctor_page(request: Request):
    return templates.TemplateResponse("doctor.html", {"request": request})


# -------------------------------------------------------------------
# Reconciler (Phase 1)
# -------------------------------------------------------------------

@app.get("/api/console/reconcile/report", response_class=JSONResponse)
async def api_reconcile_report():
    return reconcile.build_report()


@app.post("/api/console/reconcile/run", response_class=JSONResponse)
async def api_reconcile_run():
    trace_id = uuid.uuid4().hex
    rep = reconcile.build_report()
    rep["trace_id"] = trace_id
    reconcile.apply_observed(rep)
    return rep


@app.post("/api/console/reconcile/quarantine-unregistered", response_class=JSONResponse)
async def api_reconcile_quarantine_unregistered():
    rep = reconcile.build_report()
    unreg = rep.get("unregistered_dirs") or []
    result = reconcile.quarantine_unregistered(unreg)
    # return fresh report after quarantine
    rep2 = reconcile.build_report()
    return {"quarantine": result, "report": rep2}


# -------------------------------------------------------------------
# App list / stats APIs
# -------------------------------------------------------------------

@app.get("/api/console/apps", response_class=JSONResponse)
async def api_apps_list():
    return {"node": socket.gethostname(), "apps": get_app_list()}


@app.get("/api/console/server/stats", response_class=JSONResponse)
async def api_server_stats():
    return get_server_stats()



# -------------------------------------------------------------------
# Doctor APIs (Phase 2 UI plumbing)
# NOTE: Doctor UI calls these as relative paths under /app/, nginx rewrites to /api/*
# -------------------------------------------------------------------

_DOCTOR_EVENTS: List[Dict[str, Any]] = []

# Actions exposed to the Doctor UI.
# "kind" maps to button class names in doctor.html (primary/success/danger/neutral).
_DOCTOR_ACTIONS: List[Dict[str, Any]] = [
    {"id": "reconcile_run", "label": "Run reconcile now", "kind": "primary"},
    {"id": "reconcile_report", "label": "Reconcile: view report", "kind": "neutral"},
    {"id": "nginx_test", "label": "Nginx: test config", "kind": "neutral"},
    {"id": "nginx_reload", "label": "Nginx: reload", "kind": "success"},
    {"id": "systemd_sweep_orphans", "label": "Systemd: sweep orphans", "kind": "danger"},
]

def _doctor_log(event: str, meta: Dict[str, Any] | None = None) -> None:
    _DOCTOR_EVENTS.append({
        "id": uuid.uuid4().hex,
        "ts": int(time.time()),
        "event": event,
        "meta": meta or {},
    })
    # keep last 300
    if len(_DOCTOR_EVENTS) > 300:
        del _DOCTOR_EVENTS[:-300]

def _err_str(e: Exception) -> str:
    # Render FastAPI HTTPException nicely (detail contains the real message)
    if isinstance(e, HTTPException):
        try:
            return str(e.detail)
        except Exception:
            return "HTTPException"
    return str(e)

@app.get("/api/actions", response_class=JSONResponse)
async def api_actions():
    return {"actions": _DOCTOR_ACTIONS}

@app.post("/api/action/{action_id}", response_class=JSONResponse)
async def api_action_run(action_id: str):
    """
    Doctor action runner.

    Strategy:
    - Log action_start/action_done/action_error into _DOCTOR_EVENTS (Doctor UI reads /api/events).
    - Bridge into existing console handlers (no self-HTTP calls, no duplicated logic).
    """
    trace_id = uuid.uuid4().hex
    _doctor_log("action_start", {"action_id": action_id, "trace_id": trace_id})

    try:
        # -----------------------------
        # reconcile
        # -----------------------------
        if action_id == "reconcile_run":
            rep = reconcile.build_report()
            rep["trace_id"] = trace_id
            reconcile.apply_observed(rep)

            _doctor_log("action_done", {
                "action_id": action_id,
                "trace_id": trace_id,
                "counts": rep.get("counts") or {},
            })
            return {"ok": True, "action_id": action_id, "trace_id": trace_id, "result": rep}

        if action_id == "reconcile_report":
            rep = await api_reconcile_report()
            # api_reconcile_report returns a dict; add trace_id for UI correlation
            if isinstance(rep, dict):
                rep["trace_id"] = trace_id

            _doctor_log("action_done", {
                "action_id": action_id,
                "trace_id": trace_id,
                "counts": (rep.get("counts") if isinstance(rep, dict) else {}) or {},
            })
            return {"ok": True, "action_id": action_id, "trace_id": trace_id, "result": rep}

        # -----------------------------
        # nginx
        # -----------------------------
        if action_id == "nginx_test":
            # IMPORTANT: api_nginx_test() must run nginx -t with sudo (see console handler)
            result = await api_nginx_test()
            _doctor_log("action_done", {
                "action_id": action_id,
                "trace_id": trace_id,
                "ok": bool((result or {}).get("ok")),
            })
            return {"ok": True, "action_id": action_id, "trace_id": trace_id, "result": result}
        
        if action_id == "nginx_reload":
            result = await api_nginx_reload()
            _doctor_log("action_done", {
                "action_id": action_id,
                "trace_id": trace_id,
                "ok": bool((result or {}).get("ok", True)),
            })
            return {"ok": True, "action_id": action_id, "trace_id": trace_id, "result": result}
        
        # -----------------------------
        # systemd
        # -----------------------------
        if action_id == "systemd_sweep_orphans":
            result = await api_systemd_sweep_orphans()
            _doctor_log("action_done", {
                "action_id": action_id,
                "trace_id": trace_id,
                "removed": len((result or {}).get("removed") or []),
                "kept": len((result or {}).get("kept") or []),
                "daemon_reload": bool((result or {}).get("daemon_reload")),
            })
            return {"ok": True, "action_id": action_id, "trace_id": trace_id, "result": result}

        # -----------------------------
        # unknown
        # -----------------------------
        _doctor_log("action_unknown", {"action_id": action_id, "trace_id": trace_id})
        return JSONResponse({"detail": "unknown action", "action_id": action_id, "trace_id": trace_id}, status_code=404)

    except Exception as e:
        _doctor_log("action_error", {"action_id": action_id, "trace_id": trace_id, "error": _err_str(e)})
        return JSONResponse(
            {"detail": "action failed", "action_id": action_id, "trace_id": trace_id, "error": _err_str(e)},
            status_code=500,
        )

@app.get("/api/events", response_class=JSONResponse)
async def api_events(limit: int = 80):
    limit = max(1, min(int(limit or 80), 500))
    return {"events": _DOCTOR_EVENTS[-limit:]}


# -------------------------------------------------------------------
# AI: Groq + Cerebras (pluggable)
# -------------------------------------------------------------------

AI_CONFIG_DIR = BASE_DIR / ".hyperdev"
AI_CONFIG_DIR.mkdir(exist_ok=True)
AI_CONFIG_FILE = AI_CONFIG_DIR / "ai_config.json"

GROQ_API_BASE = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

CEREBRAS_API_BASE = "https://api.cerebras.ai/v1/chat/completions"
OLLAMA_API_BASE = os.environ.get("OLLAMA_API_BASE", "http://127.0.0.1:11434")
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "llama-3.3-70b")


def _groq_chat(prompt: str) -> str:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return "Groq API key missing on server (GROQ_API_KEY)."

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    data = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": "You are a helpful coding assistant inside Workpent HyperDev Console."},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        resp = requests.post(GROQ_API_BASE, headers=headers, json=data, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        choices = payload.get("choices") or []
        if choices:
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

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    data = {
        "model": CEREBRAS_MODEL,
        "messages": [
            {"role": "system", "content": "You are a helpful coding assistant inside Workpent HyperDev Console."},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        resp = requests.post(CEREBRAS_API_BASE, headers=headers, json=data, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        choices = payload.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                return content
        return f"[Cerebras] Unexpected response: {payload}"
    except Exception as e:
        return f"[Cerebras] Error: {e}"


def _pick_provider(explicit: str | None) -> str:
    if explicit:
        p = explicit.lower()
        if p in ("groq", "cerebras"):
            return p

    env_p = os.environ.get("AI_PROVIDER", "auto").lower()
    if env_p in ("groq", "cerebras"):
        return env_p

    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    if os.environ.get("CEREBRAS_API_KEY"):
        return "cerebras"
    return "none"


async def _do_ai_ask(prompt: str, provider: str | None = None) -> str:
    which = _pick_provider(provider)
    if which == "groq":
        return _groq_chat(prompt)
    if which == "cerebras":
        return _cerebras_chat(prompt)
    return "No AI provider configured on server."


class AskBody(BaseModel):
    prompt: str
    provider: str | None = None


class AppChatBody(BaseModel):
    prompt: str


@app.get("/api/console/ai/config")
async def api_get_ai_config():
    if AI_CONFIG_FILE.exists():
        try:
            return json.loads(AI_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


@app.post("/api/console/ai/config")
async def api_save_ai_config(config: dict = Body(...)):
    AI_CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return {"ok": True}


@app.post("/api/console/ai/ask")
async def api_ai_ask(body: AskBody):
    reply = await _do_ai_ask(body.prompt, provider=body.provider)
    return {"ok": True, "reply": reply}

class AiHealthBody(BaseModel):
    provider: str | None = None
    api_url: str | None = None
    api_key: str | None = None   # optional direct key (UI can omit)
    api_key_env: str | None = None  # optional env var name like GROQ_API_KEY


@app.post("/api/console/ai/health")
async def api_ai_health(body: AiHealthBody):
    """
    Provider health checks (v1).
    - ollama: GET /api/version (fallback /api/tags)
    - groq/cerebras/openai/custom: OpenAI-compatible check via GET /models or /v1/models
    Notes:
      * provider selection is explicit; 'auto' uses server picker only for groq/cerebras.
      * custom requires api_url.
    """
    provider = (body.provider or "").strip().lower() or "auto"
    api_url = (body.api_url or "").strip()
    api_key = (body.api_key or "").strip()
    api_key_env = (body.api_key_env or "").strip()

    which = _pick_provider(provider)

    # Allow explicit selection even if auto-picker wouldn't choose it
    if provider in ("ollama", "groq", "cerebras", "openai", "custom"):
        which = provider

    def _get_key(default_env: str | None = None) -> str | None:
        if api_key:
            return api_key
        if api_key_env and os.environ.get(api_key_env):
            return os.environ.get(api_key_env)
        if default_env and os.environ.get(default_env):
            return os.environ.get(default_env)
        return None

    def _try_openai_models(base: str, key: str | None):
        base = (base or "").rstrip("/")
        headers = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        # Try both common shapes
        candidates = [base + "/v1/models", base + "/models"]
        last = None
        for url in candidates:
            try:
                r = requests.get(url, headers=headers, timeout=6)
                last = (url, r.status_code, (r.text or "")[:400])
                if r.ok:
                    # return a small summary (avoid huge payloads)
                    ct = (r.headers.get("content-type") or "")
                    if ct.startswith("application/json"):
                        j = r.json()
                        # OpenAI-style: {"data":[...]}
                        n = None
                        if isinstance(j, dict) and isinstance(j.get("data"), list):
                            n = len(j["data"])
                        return {"ok": True, "api_url": base, "url": url, "model_count": n, "details": j if n is None else {"model_count": n}}
                    return {"ok": True, "api_url": base, "url": url, "details": (r.text or "")[:400]}
            except Exception as e:
                last = (url, "exception", str(e)[:220])
        return {"ok": False, "api_url": base, "error": "models_check_failed", "details": last}

    # ---- OLLAMA ----
    if which == "ollama":
        base = api_url or os.environ.get("OLLAMA_API_BASE") or "http://127.0.0.1:11434"
        base = base.rstrip("/")
        try:
            r = requests.get(base + "/api/version", timeout=4)
            if r.ok:
                return {"ok": True, "provider": "ollama", "api_url": base, "details": r.json() if (r.headers.get("content-type","").startswith("application/json")) else (r.text or "")[:400]}
        except Exception as e:
            last_err = str(e)
        else:
            last_err = "unknown_error"

        try:
            r2 = requests.get(base + "/api/tags", timeout=5)
            if r2.ok:
                return {"ok": True, "provider": "ollama", "api_url": base, "details": r2.json() if (r2.headers.get("content-type","").startswith("application/json")) else (r2.text or "")[:400]}
            return {"ok": False, "provider": "ollama", "api_url": base, "error": f"HTTP {r2.status_code}", "details": (r2.text or "")[:400]}
        except Exception as e2:
            return {"ok": False, "provider": "ollama", "api_url": base, "error": str(e2)[:220], "details": last_err[:220]}

    # ---- GROQ ----
    if which == "groq":
        base = api_url or os.environ.get("GROQ_API_BASE") or "https://api.groq.com/openai/v1"
        key = _get_key("GROQ_API_KEY")
        if not key:
            return {"ok": False, "provider": "groq", "api_url": base, "error": "missing_api_key", "details": "Set GROQ_API_KEY (or pass api_key/api_key_env)."}
        out = _try_openai_models(base, key)
        out["provider"] = "groq"
        return out

    # ---- CEREBRAS ----
    if which == "cerebras":
        base = api_url or os.environ.get("CEREBRAS_API_BASE") or "https://api.cerebras.ai/v1"
        key = _get_key("CEREBRAS_API_KEY")
        if not key:
            return {"ok": False, "provider": "cerebras", "api_url": base, "error": "missing_api_key", "details": "Set CEREBRAS_API_KEY (or pass api_key/api_key_env)."}
        out = _try_openai_models(base, key)
        out["provider"] = "cerebras"
        return out

    # ---- OPENAI (legacy) ----
    if which == "openai":
        base = api_url or os.environ.get("OPENAI_API_BASE") or "https://api.openai.com/v1"
        key = _get_key("OPENAI_API_KEY")
        if not key:
            return {"ok": False, "provider": "openai", "api_url": base, "error": "missing_api_key", "details": "Set OPENAI_API_KEY (or pass api_key/api_key_env)."}
        out = _try_openai_models(base, key)
        out["provider"] = "openai"
        return out

    # ---- CUSTOM (OpenAI-compatible) ----
    if which == "custom":
        if not api_url:
            return {"ok": False, "provider": "custom", "api_url": None, "error": "missing_api_url", "details": "Provide api_url (e.g. http://host:port or https://...)"}
        key = _get_key(None)
        out = _try_openai_models(api_url, key if key else None)
        out["provider"] = "custom"
        return out

    return {"ok": False, "provider": which, "api_url": api_url or None, "error": "not_implemented"}


@app.get("/api/console/apps/{app_name}/ai/config")
async def api_app_ai_config(app_name: str):
    return {
        "provider": "groq",
        "api_url": GROQ_API_BASE,
        "api_key_env": "GROQ_API_KEY",
        "model": GROQ_MODEL,
    }


@app.get("/api/console/apps/{app_name}/ui/config")
async def api_app_ui_config(app_name: str):
    return {"ok": True}


@app.post("/api/console/apps/{app_name}/ai/chat")
async def api_app_ai_chat(app_name: str, payload: dict = Body(...)):
    prompt = (payload.get("message") or payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is empty")
    reply = await _do_ai_ask(prompt)
    return {"ok": True, "reply": reply}


@app.get("/ai-debug/env", response_class=JSONResponse)
async def ai_debug_env():
    return {
        "AI_PROVIDER": os.environ.get("AI_PROVIDER"),
        "GROQ_API_KEY": "set" if os.environ.get("GROQ_API_KEY") else None,
        "CEREBRAS_API_KEY": "set" if os.environ.get("CEREBRAS_API_KEY") else None,
    }


# -------------------------------------------------------------------
# Lifecycle controls (FIXED: spaces + sudo -n)
# -------------------------------------------------------------------

def run_systemctl(app_name: str, action: str) -> Dict[str, Any]:
    """
    Use systemd-escape for correct unit naming and sudo -n to avoid interactive auth.
    """
    unit = systemd_unit_for_app(app_name)

    # systemctl start/stop/restart requires root on many setups
    res = subprocess.run(
        ["sudo", "-n", "/bin/systemctl", action, unit],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if res.returncode != 0:
        msg = (res.stderr or "").strip() or (res.stdout or "").strip() or "Unknown systemctl error"
        raise HTTPException(status_code=500, detail=msg)

    # Return status after action
    status_res = subprocess.run(
        ["/bin/systemctl", "is-active", unit],
        capture_output=True,
        text=True,
        timeout=5,
    )
    state = (status_res.stdout or "").strip() or (status_res.stderr or "").strip() or "unknown"
    return {"unit": unit, "action": action, "state": state}


@app.post("/api/console/apps/{app_name}/start")
async def api_app_start(app_name: str):
    app_dir(app_name)
    return {"ok": True, **run_systemctl(app_name, "start")}


@app.post("/api/console/apps/{app_name}/stop")
async def api_app_stop(app_name: str):
    app_dir(app_name)
    return {"ok": True, **run_systemctl(app_name, "stop")}


@app.post("/api/console/apps/{app_name}/restart")
async def api_app_restart(app_name: str):
    app_dir(app_name)
    return {"ok": True, **run_systemctl(app_name, "restart")}


# -------------------------------------------------------------------
# Health / logs / nginx tools
# -------------------------------------------------------------------

@app.get("/api/console/apps/{app_name}/health", response_class=JSONResponse)
async def api_app_health(app_name: str):
    """
    Lightweight health probe for a managed app:
    - Reads APP_PORT from the app's .env
    - GET http://127.0.0.1:<port>/health
    """
    base = app_dir(app_name)
    port = read_env_port(base)
    if not port:
        raise HTTPException(status_code=400, detail="APP_PORT not found in .env")

    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = resp.read(2048).decode("utf-8", errors="ignore")
        ok = resp.status == 200
        return {
            "ok": ok,
            "status_code": resp.status,
            "reason": resp.reason,
            "body": body,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Health check error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


@app.get("/api/console/apps/{app_name}/logs", response_class=PlainTextResponse)
async def api_app_logs(app_name: str, lines: int = 80):
    """
    Tail systemd logs for an app unit. Uses sudo because journal access often requires it.
    """
    app_dir(app_name)  # validates app exists
    unit = systemd_unit_for_app(app_name)

    # clamp to prevent abuse
    try:
        lines = int(lines or 80)
    except Exception:
        lines = 80
    lines = max(1, min(lines, 500))

    res = subprocess.run(
        ["sudo", "-n", "/bin/journalctl", "-u", unit, "-n", str(lines), "--no-pager"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if res.returncode != 0:
        raise HTTPException(status_code=500, detail=(res.stderr or res.stdout).strip())
    return res.stdout


@app.post("/api/console/nginx/test", response_class=JSONResponse)
async def api_nginx_test():
    """
    Validate nginx config. Must run with elevated privileges because configs/certs/logs are root-owned.
    Uses non-interactive sudo (-n). If sudo is not permitted, this returns ok=false with output.
    """
    res = subprocess.run(
        ["sudo", "-n", "nginx", "-t"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    ok = res.returncode == 0
    output = (res.stderr or res.stdout).strip()
    return {"ok": ok, "output": output}


@app.post("/api/console/nginx/reload", response_class=JSONResponse)
async def api_nginx_reload():
    """
    Reload nginx via systemd (non-interactive sudo).
    """
    res = subprocess.run(
        ["sudo", "-n", "/bin/systemctl", "reload", "nginx"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip() or "Reload failed"
        raise HTTPException(status_code=500, detail=msg)
    msg = (res.stdout or res.stderr).strip() or "Reloaded"
    return {"ok": True, "message": msg}


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
    from app import registry

    # 1) must exist + be deleted
    try:
        registry.assert_deleted(app_name)
    except KeyError:
        raise HTTPException(status_code=404, detail="App not registered")
    except ValueError:
        raise HTTPException(status_code=409, detail="App is not deleted")

    # 2) locate latest deleted snapshot
    matches = sorted(DELETED_BASE.glob(f"{app_name}-*"))
    if not matches:
        raise HTTPException(status_code=404, detail="No deleted snapshot found")

    src = matches[-1]
    dest = APPS_BASE / app_name

    if dest.exists():
        raise HTTPException(status_code=409, detail="App directory already exists")

    # 3) restore filesystem
    shutil.move(src, dest)

    # 4) registry state → STOPPED
    registry.set_app_status(app_name, "STOPPED")

    return {
        "ok": True,
        "restored_from": str(src),
        "restored_to": str(dest),
        "status": "STOPPED",
    }


@app.post("/api/console/apps/{app_name}/soft-delete", response_class=JSONResponse)
async def api_soft_delete(app_name: str):
    # Requires registry membership (enforced by app_dir)
    base = app_dir(app_name)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    dest = DELETED_BASE / f"{app_name}-{ts}"

    # 1) stop + disable unit (best-effort)
    unit = systemd_unit_for_app(app_name)
    stop_res = subprocess.run(
        ["sudo", "-n", "/bin/systemctl", "stop", unit],
        capture_output=True, text=True, timeout=20
    )
    disable_res = subprocess.run(
        ["sudo", "-n", "/bin/systemctl", "disable", unit],
        capture_output=True, text=True, timeout=20
    )

    # 2) move to recycle bin
    try:
        shutil.move(base, dest)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Soft-delete move failed: {e}")

    # 3) sweep orphan wants symlinks (repairs common drift)
    sweep = _systemd_sweep_orphan_wants()

    # 4) mark registry status
    registry.set_app_status(app_name, "DELETED")

    return {
        "ok": True,
        "app_slug": app_name,
        "unit_name": unit,
        "moved_to": str(dest),
        "stop_rc": stop_res.returncode,
        "disable_rc": disable_res.returncode,
        "stop_out": (stop_res.stderr or stop_res.stdout).strip(),
        "disable_out": (disable_res.stderr or disable_res.stdout).strip(),
        "sweep": sweep,
    }


@app.get("/api/console/apps/{app_name}/download")
async def api_download_app(
    request: Request,
    app_name: str,
    x_api_key: str | None = Header(default=None),
):
    require_api_key(request, x_api_key)

    base = app_dir(app_name)

    ts = datetime.utcnow().strftime("%Y%m%d")
    zip_name = f"{app_name}-{ts}.zip"

    tmp_dir = Path("/tmp/workpent-zips")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    zip_path = tmp_dir / zip_name

    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in {".git", ".venv", "node_modules", "__pycache__"}]
            root_path = Path(root)
            for f in files:
                file_path = root_path / f
                rel = file_path.relative_to(base)
                zf.write(file_path, rel)

    if not zip_path.exists() or zip_path.stat().st_size < 200:
        raise HTTPException(status_code=500, detail="Zip build failed")

    return FileResponse(path=str(zip_path), filename=zip_name, media_type="application/zip")



# -------------------------------------------------------------------
# Idea Sessions (Phase 2)
# -------------------------------------------------------------------

async def _handle_idea_session_message(session_id: str, payload: Dict[str, Any]):
    content = (payload.get("content") or "").strip()
    if not content:
        return JSONResponse({"detail": "content is required"}, status_code=400)

    try:
        session = idea_sessions.add_user_reply(session_id, content)
        idea_sessions.save_session(session)
        return JSONResponse(session)
    except KeyError:
        return JSONResponse({"detail": "not found"}, status_code=404)


@app.post("/api/idea-sessions", response_class=JSONResponse)
async def create_idea_session(payload: Dict[str, Any]):
    idea = (payload.get("idea") or "").strip()
    if not idea:
        return JSONResponse({"detail": "idea is required"}, status_code=400)

    session = idea_sessions.create_session(idea)
    idea_sessions.save_session(session)
    return JSONResponse(session)


@app.get("/api/idea-sessions/{session_id}", response_class=JSONResponse)
async def get_idea_session(session_id: str):
    session = idea_sessions.get_session(session_id)
    if not session:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return JSONResponse(session)


@app.post("/api/idea-sessions/{session_id}/message", response_class=JSONResponse)
async def post_idea_session_message(session_id: str, payload: Dict[str, Any]):
    return await _handle_idea_session_message(session_id, payload)


# Compatibility alias (do NOT add logic here)
@app.post("/api/idea-sessions/{session_id}/reply", response_class=JSONResponse)
async def post_idea_session_reply(session_id: str, payload: Dict[str, Any]):
    return await _handle_idea_session_message(session_id, payload)


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

    items: List[Dict[str, Any]] = []
    if current != base:
        parent_rel = current.parent.relative_to(base).as_posix() or "."
        items.append({"name": "..", "path": parent_rel, "type": "folder"})

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
        return {"ok": True, "path": env_file.as_posix(), "content": "# .env not found – will be created on save\n"}
    try:
        content = env_file.read_text(encoding="utf-8")
        return {"ok": True, "path": env_file.as_posix(), "content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/console/apps/{app_name}/env", response_class=JSONResponse)
async def api_env_save(app_name: str, content: str = Form(...)):
    base = app_dir(app_name)
    env_file = base / ".env"
    try:
        env_file.write_text(content, encoding="utf-8")
        return {"ok": True, "message": f"ENV saved to {env_file.as_posix()}", "path": env_file.as_posix()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# Git cockpit (local git commands)
# -------------------------------------------------------------------

def run_git(base: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("HOME", "/home/oga")
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
        return {"ok": False, "status": "(not a git repository)", "output": ""}
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
    res = run_git(base, ["commit", "-am", message], timeout=30)
    if res.returncode != 0:
        raise HTTPException(status_code=500, detail=res.stderr.strip() or res.stdout.strip())
    return {"ok": True, "output": res.stdout}


@app.post("/api/console/apps/{app_name}/git/push", response_class=JSONResponse)
async def api_git_push(app_name: str):
    base = app_dir(app_name)
    res = run_git(base, ["push", "origin", "main"], timeout=120)
    if res.returncode != 0:
        raise HTTPException(status_code=500, detail=res.stderr.strip() or res.stdout.strip())
    return {"ok": True, "output": res.stdout}


@app.post("/api/console/apps/{app_name}/git/pull", response_class=JSONResponse)
async def api_git_pull(app_name: str):
    base = app_dir(app_name)
    res = run_git(base, ["pull", "origin", "main"], timeout=120)
    if res.returncode != 0:
        raise HTTPException(status_code=500, detail=res.stderr.strip() or res.stdout.strip())
    return {"ok": True, "output": res.stdout}


# -------------------------------------------------------------------
# Pip dependencies
# -------------------------------------------------------------------

@app.get("/api/console/apps/{app_name}/pip/list", response_class=JSONResponse)
async def api_pip_list(app_name: str):
    base = app_dir(app_name)
    pip_bin = venv_pip_path(base)
    if not pip_bin:
        return {"ok": False, "output": "No .venv/bin/pip found – create venv first."}

    res = subprocess.run([str(pip_bin), "list"], cwd=base, capture_output=True, text=True, timeout=60)
    return {"ok": res.returncode == 0, "output": res.stdout or res.stderr}


@app.post("/api/console/apps/{app_name}/pip/install-reqs", response_class=JSONResponse)
async def api_pip_install_reqs(app_name: str):
    base = app_dir(app_name)
    pip_bin = venv_pip_path(base)
    if not pip_bin:
        return {"ok": False, "output": "No .venv/bin/pip found – create venv first."}

    req = base / "requirements.txt"
    if not req.is_file():
        return {"ok": False, "output": "requirements.txt not found in app root"}

    res = subprocess.run(
        [str(pip_bin), "install", "-r", "requirements.txt"],
        cwd=base,
        capture_output=True,
        text=True,
        timeout=1200,
    )
    return {"ok": res.returncode == 0, "output": res.stdout or res.stderr}


# -------------------------------------------------------------------
# Jobs
# -------------------------------------------------------------------

class CreateServerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    region: str = Field(min_length=2, max_length=32)


class CreateServerResponse(BaseModel):
    job_id: str


class JobResponse(BaseModel):
    job_id: str
    status: str
    progress: float = 0.0
    message: str = ""
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class JobResponseDebug(JobResponse):
    error_type: Optional[str] = None
    error_trace: Optional[str] = None


@app.post("/api/console/server/create", response_model=CreateServerResponse)
async def api_create_server(req: CreateServerRequest):
    job = job_store.create()
    spawn_job(job.job_id, req.model_dump())
    return {"job_id": job.job_id}


@app.get("/api/console/jobs/{job_id}")
async def api_get_job(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    data = job.to_public_dict()
    debug_jobs = os.environ.get("DEBUG_JOBS", "").strip() == "1"
    if debug_jobs:
        return JobResponseDebug(**data)
    return JobResponse(**data)


# -------------------------------------------------------------------
# Idea capture
# -------------------------------------------------------------------

@app.post("/api/console/ideas/save", response_class=JSONResponse)
async def api_save_idea(idea: str = Form(...), app_name: str = Form("general")):
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

