from __future__ import annotations
import uuid
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_DB_PATH = "/srv/workpent/registry/registry.db"

def _db_path() -> str:
    return os.environ.get("WORKPENT_REGISTRY_DB", DEFAULT_DB_PATH)

def connect() -> sqlite3.Connection:
    path = _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=5, isolation_level=None)  # autocommit
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    con.execute("PRAGMA busy_timeout=5000;")
    return con

def list_apps() -> List[Dict[str, Any]]:
    con = connect()
    try:
        rows = con.execute(
            """
            SELECT app_id, slug, status, template_id, template_version_id,
                   created_at, updated_at,
                   observed_status, observed_drift_flags, observed_last_seen_at
            FROM apps
            ORDER BY slug
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()

def get_app_by_slug(slug: str) -> Dict[str, Any] | None:
    con = connect()
    try:
        row = con.execute(
            """
            SELECT app_id, slug, status, template_id, template_version_id,
                   created_at, updated_at,
                   observed_status, observed_drift_flags, observed_last_seen_at
            FROM apps
            WHERE slug = ?
            """,
            (slug,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()

def set_app_status(slug: str, status: str) -> None:
    con = connect()
    try:
        con.execute(
            "UPDATE apps SET status=?, updated_at=datetime('now') WHERE slug=?",
            (status, slug),
        )
    finally:
        con.close()
def assert_deleted(slug: str) -> None:
    rec = get_app_by_slug(slug)
    if not rec:
        raise KeyError("App not registered")
    if rec.get("status") != "DELETED":
        raise ValueError("App is not deleted")

def update_observed(slug: str, observed_status: str | None, drift_flags: int) -> None:
    con = connect()
    try:
        con.execute(
            """
            UPDATE apps
            SET observed_status=?,
                observed_drift_flags=?,
                observed_last_seen_at=datetime('now'),
                updated_at=datetime('now')
            WHERE slug=?
            """,
            (observed_status, drift_flags, slug),
        )
    finally:
        con.close()


# -------------------------------------------------------------------
# Deterministic identity (UUIDv5)
# -------------------------------------------------------------------

def _platform_namespace() -> uuid.UUID:
    '''
    Stable namespace for this registry, derived from meta.registry_uuid.

    meta.registry_uuid is created once at DB init (32 hex chars). We convert it
    into a UUID and use it as the UUIDv5 namespace so IDs are deterministic
    per-registry.
    '''
    con = connect()
    try:
        row = con.execute("SELECT value FROM meta WHERE key='registry_uuid'").fetchone()
        if not row:
            raise RuntimeError("registry_uuid missing from meta table")
        hex32 = str(row[0]).strip()
        # registry_uuid stored as 32 hex chars (no dashes)
        return uuid.UUID(hex=hex32)
    finally:
        con.close()


def generate_app_id(slug: str) -> str:
    '''
    Deterministic app_id: uuid5(namespace, slug).
    Same slug -> same id forever (within this registry).
    '''
    slug = (slug or "").strip()
    if not slug:
        raise ValueError("slug is empty")
    return str(uuid.uuid5(_platform_namespace(), slug))


def ensure_app_id(slug: str, app_id: str | None) -> str:
    '''
    If DB row has a placeholder/empty app_id, compute deterministic one.
    '''
    if app_id and len(str(app_id)) >= 36 and str(app_id).count("-") == 4:
        return str(app_id)
    return generate_app_id(slug)
