from __future__ import annotations

import json
import time
import uuid
from typing import Dict, Any, List, Optional

# Optional persistence (sqlite via app.db)
try:
    from app.db import db_exec, db_one  # type: ignore
except Exception:
    db_exec = None
    db_one = None

# In-memory cache (fast path). DB persistence makes restarts safe.
_SESSIONS: Dict[str, Dict[str, Any]] = {}

_AGENT_QUESTIONS: List[str] = [
    "Who is the primary user and what is their main goal?",
    "Is this a web app, mobile app, or both?",
    "Do you need authentication? If yes, what type (email/password, Google, etc.)?",
    "What core features are absolutely required for v1?",
    "Any integrations (payments, email, calendar, files) needed in v1?",
    "Do you have mockups or examples you want it to resemble?",
]

def _db_enabled() -> bool:
    # default ON if db exists
    return (db_exec is not None) and (db_one is not None)

def _ensure_tables() -> None:
    if not _db_enabled():
        return
    db_exec(
        """
        CREATE TABLE IF NOT EXISTS idea_sessions (
          id TEXT PRIMARY KEY,
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          data_json TEXT NOT NULL
        )
        """
    )

def _persist(session: Dict[str, Any]) -> None:
    if not _db_enabled():
        return
    now = int(time.time())
    sid = session["id"]
    created_at = int(session.get("created_at") or now)
    data_json = json.dumps(session, ensure_ascii=False)
    db_exec(
        """
        INSERT INTO idea_sessions (id, created_at, updated_at, data_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          updated_at=excluded.updated_at,
          data_json=excluded.data_json
        """,
        (sid, created_at, now, data_json),
    )

def _load(session_id: str) -> Optional[Dict[str, Any]]:
    if not _db_enabled():
        return None
    row = db_one("SELECT data_json FROM idea_sessions WHERE id = ?", (session_id,))
    if not row:
        return None
    try:
        return json.loads(row["data_json"])
    except Exception:
        return None

# ------------------------------------------------------------
# Core session primitives
# ------------------------------------------------------------

def create_session(idea: str) -> Dict[str, Any]:
    _ensure_tables()

    session_id = str(uuid.uuid4())
    session: Dict[str, Any] = {
        "id": session_id,
        "idea": idea,
        "created_at": int(time.time()),
        "status": "interrogating",
        "messages": [],
        "question_index": 0,
    }

    # Start with the first real question from the deterministic list
    first = _next_agent_message(session)
    if first:
        session["messages"].append(first)

    _SESSIONS[session_id] = session
    _persist(session)
    return session

def get_session(session_id: str) -> Dict[str, Any] | None:
    # cache
    s = _SESSIONS.get(session_id)
    if s:
        return s
    # db
    s = _load(session_id)
    if s:
        _SESSIONS[session_id] = s
        return s
    return None

def save_session(session: Dict[str, Any]) -> None:
    session_id = session.get("id")
    if not session_id:
        raise ValueError("session.id is required")
    _SESSIONS[session_id] = session
    _persist(session)

# ------------------------------------------------------------
# Interrogation flow (deterministic for now)
# ------------------------------------------------------------

def add_user_reply(session_id: str, content: str) -> Dict[str, Any]:
    session = get_session(session_id)
    if not session:
        raise KeyError("Session not found")

    session["messages"].append({"role": "user", "content": content})

    nxt = _next_agent_message(session)
    if nxt:
        session["messages"].append(nxt)
        session["status"] = "interrogating"
    else:
        session["status"] = "planning"
        session["messages"].append({
            "role": "agent",
            "content": "Thanks — I have enough info. I will now draft a concrete plan for this app."
        })

    save_session(session)
    return session

def _next_agent_message(session: Dict[str, Any]) -> Dict[str, Any] | None:
    idx = int(session.get("question_index") or 0)
    if idx >= len(_AGENT_QUESTIONS):
        return None
    question = _AGENT_QUESTIONS[idx]
    session["question_index"] = idx + 1
    return {"role": "agent", "content": question}
