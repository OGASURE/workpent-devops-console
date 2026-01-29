# app/auth.py
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional, Dict, Any

from app.db import db_one, db_exec, db_all

# ------------------------------------------------------------
# Optional FastAPI dependency:
# - If fastapi is installed: use real HTTPException/Request
# - If not: provide safe fallbacks so CLI scripts can run
# ------------------------------------------------------------
try:
    from fastapi import HTTPException, Request  # type: ignore
except ModuleNotFoundError:
    class HTTPException(Exception):
        def __init__(self, status_code: int = 500, detail: str = "Error"):
            self.status_code = status_code
            self.detail = detail
            super().__init__(f"HTTP {status_code}: {detail}")

    Request = object  # type: ignore


def _now() -> int:
    return int(time.time())


def _master_key() -> Optional[str]:
    # Global key (systemd env var). Works as "super-admin key".
    return os.environ.get("WORKPENT_API_KEY")


def ensure_seed_admin(master_key: Optional[str] = None) -> None:
    """
    Create a special 'admin' user and bind WORKPENT_API_KEY to it
    (only if not already present).
    """
    master_key = (master_key or _master_key() or "").strip()
    if not master_key:
        return

    admin = db_one("SELECT * FROM users WHERE id = ?", ("admin",))
    if not admin:
        db_exec(
            "INSERT INTO users (id, email, created_at, is_admin, status) VALUES (?, ?, ?, ?, ?)",
            ("admin", "admin@local", _now(), 1, "active"),
        )

    exists = db_one("SELECT * FROM api_keys WHERE key = ?", (master_key,))
    if not exists:
        db_exec(
            "INSERT INTO api_keys (key, user_id, created_at, label, last_used_at) VALUES (?, ?, ?, ?, ?)",
            (master_key, "admin", _now(), "system-master", None),
        )


def create_user(email: Optional[str] = None, *, is_admin: int = 0) -> Dict[str, Any]:
    user_id = uuid.uuid4().hex
    db_exec(
        "INSERT INTO users (id, email, created_at, is_admin, status) VALUES (?, ?, ?, ?, ?)",
        (user_id, email, _now(), int(is_admin), "active"),
    )
    return db_one("SELECT * FROM users WHERE id = ?", (user_id,)) or {"id": user_id}


def issue_api_key(user_id: str, label: str = "default") -> str:
    key = uuid.uuid4().hex
    db_exec(
        "INSERT INTO api_keys (key, user_id, created_at, label, last_used_at) VALUES (?, ?, ?, ?, ?)",
        (key, user_id, _now(), label, None),
    )
    return key


def get_user_by_key(x_api_key: Optional[str]) -> Optional[Dict[str, Any]]:
    if not x_api_key:
        return None

    row = db_one(
        """
        SELECT u.* FROM api_keys k
        LEFT JOIN users u ON u.id = k.user_id
        WHERE k.key = ?
        """,
        (x_api_key,),
    )
    if not row:
        return None

    if row.get("status") and row["status"] != "active":
        return None

    db_exec("UPDATE api_keys SET last_used_at = ? WHERE key = ?", (_now(), x_api_key))
    return row


def _request_ip(request: Any) -> Optional[str]:
    """
    Works for FastAPI Request objects, but won't crash in CLI tests.
    """
    try:
        client = getattr(request, "client", None)
        if client and getattr(client, "host", None):
            return client.host
    except Exception:
        pass
    return None


def log_event(
    request: Any,
    user_id: Optional[str],
    event: str,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Best-effort event logging. MUST NEVER break request flow.
    """
    try:
        ip = _request_ip(request)
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        # Keep schema: (ts, user_id, ip, event, meta_json)
        db_exec(
            "INSERT INTO events (ts, user_id, ip, event, meta_json) VALUES (?, ?, ?, ?, ?)",
            (_now(), user_id, ip, event, meta_json),
        )
    except Exception:
        # Fail-open: logging errors must not crash the app
        return


def require_user(request: Any, x_api_key: Optional[str]) -> Dict[str, Any]:
    """
    Accept:
      - master key from WORKPENT_API_KEY (super-admin)
      - any DB key from api_keys joined to active users
    """
    mk = (_master_key() or "").strip()
    if mk and x_api_key == mk:
        u = db_one("SELECT * FROM users WHERE id = ?", ("admin",))
        if u:
            return u

    user = get_user_by_key(x_api_key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user


def require_admin(user: Dict[str, Any]) -> None:
    if int(user.get("is_admin") or 0) != 1:
        raise HTTPException(status_code=403, detail="Admin required")


def recent_events(limit: int = 50) -> list[dict]:
    return db_all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),))

