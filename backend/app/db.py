# backend/app/db.py
from __future__ import annotations

import os
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple, List

# Store state under backend/.state (already used in your repo)
STATE_DIR = Path(__file__).resolve().parent.parent / ".state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("WORKPENT_DB_PATH") or (STATE_DIR / "workpent.sqlite3"))

_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> int:
    return int(time.time())


def db_exec(sql: str, params: Tuple[Any, ...] = ()) -> None:
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


def db_one(sql: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def db_all(sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


# ------------------------------------------------------------
# Schema (idempotent)
# ------------------------------------------------------------
def ensure_schema() -> None:
    # users, api_keys, events (your auth stack expects these)
    db_exec(
        """
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY,
          email TEXT,
          created_at INTEGER NOT NULL,
          is_admin INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'active'
        )
        """
    )
    db_exec(
        """
        CREATE TABLE IF NOT EXISTS api_keys (
          key TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          label TEXT,
          last_used_at INTEGER,
          FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    db_exec(
        """
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts INTEGER NOT NULL,
          user_id TEXT,
          ip TEXT,
          event TEXT NOT NULL,
          meta_json TEXT
        )
        """
    )

    # HyperDev jobs
    db_exec(
        """
        CREATE TABLE IF NOT EXISTS hyperdev_jobs (
          id TEXT PRIMARY KEY,
          app_name TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          status TEXT NOT NULL,
          mode TEXT NOT NULL,
          instruction TEXT NOT NULL,
          config_json TEXT,
          result_json TEXT
        )
        """
    )

    db_exec(
        """
        CREATE TABLE IF NOT EXISTS hyperdev_job_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id TEXT NOT NULL,
          ts INTEGER NOT NULL,
          level TEXT NOT NULL,
          message TEXT NOT NULL,
          data_json TEXT,
          FOREIGN KEY(job_id) REFERENCES hyperdev_jobs(id)
        )
        """
    )


# ------------------------------------------------------------
# HyperDev job helpers
# ------------------------------------------------------------
def create_job(
    job_id: str,
    app_name: str,
    mode: str,
    instruction: str,
    config: Optional[Dict[str, Any]] = None,
) -> None:
    db_exec(
        """
        INSERT INTO hyperdev_jobs (id, app_name, created_at, updated_at, status, mode, instruction, config_json, result_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            app_name,
            _now(),
            _now(),
            "queued",
            mode,
            instruction,
            json.dumps(config or {}, ensure_ascii=False),
            None,
        ),
    )


def set_job_status(job_id: str, status: str, result: Optional[Dict[str, Any]] = None) -> None:
    db_exec(
        """
        UPDATE hyperdev_jobs
        SET status = ?, updated_at = ?, result_json = COALESCE(?, result_json)
        WHERE id = ?
        """,
        (
            status,
            _now(),
            json.dumps(result, ensure_ascii=False) if result is not None else None,
            job_id,
        ),
    )


def append_job_event(
    job_id: str,
    level: str,
    message: str,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    db_exec(
        """
        INSERT INTO hyperdev_job_events (job_id, ts, level, message, data_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            job_id,
            _now(),
            level,
            message,
            json.dumps(data or {}, ensure_ascii=False) if data else None,
        ),
    )


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    return db_one("SELECT * FROM hyperdev_jobs WHERE id = ?", (job_id,))


def get_job_events_after(job_id: str, last_id: int) -> List[Dict[str, Any]]:
    return db_all(
        """
        SELECT * FROM hyperdev_job_events
        WHERE job_id = ? AND id > ?
        ORDER BY id ASC
        """,
        (job_id, int(last_id)),
    )

