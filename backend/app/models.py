# app/models.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass
class User:
    id: str
    email: Optional[str]
    created_at: int
    is_admin: int = 0
    status: str = "active"  # active|suspended|deleted


@dataclass
class ApiKey:
    key: str
    user_id: Optional[str]
    created_at: int
    label: Optional[str] = None
    last_used_at: Optional[int] = None


@dataclass
class Event:
    id: int
    ts: int
    user_id: Optional[str]
    ip: Optional[str]
    event: str
    meta_json: Optional[str] = None


def safe_meta(meta: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Keep it tiny + safe. We store JSON as text in sqlite.
    We'll serialize in auth/accounts to avoid importing json everywhere.
    """
    return None

