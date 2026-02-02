from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.hyperdef_spec_v01 import validate_spec_v01

router = APIRouter(prefix="/api/hyperdef", tags=["hyperdef"])


# -------------------------
# Request/Response models
# -------------------------

class ValidateRequest(BaseModel):
    spec_yaml: str = Field(..., description="HyperDef spec YAML string")


class ValidateResponse(BaseModel):
    ok: bool
    errors: Optional[List[Dict[str, str]]] = None
    spec_hash: Optional[str] = None


class BreedRequest(BaseModel):
    spec_yaml: str = Field(..., description="HyperDef spec YAML string")
    dry_run: bool = Field(True, description="v0.1: breed is plan-only; dry_run must be true")
    force: bool = Field(False, description="ignored in v0.1 plan-only; reserved for later")


class BreedResponse(BaseModel):
    ok: bool
    errors: Optional[List[Dict[str, str]]] = None
    spec_hash: Optional[str] = None
    breed_id: Optional[str] = None
    determinism: Optional[Dict[str, Any]] = None
    artifacts_plan: Optional[Dict[str, Any]] = None
    runtime_endpoints: Optional[Dict[str, Any]] = None


class DoctorAdviseRequest(BaseModel):
    spec_yaml: str = Field(..., description="HyperDef spec YAML string")
    context: Optional[Dict[str, Any]] = Field(default=None, description="Optional runtime context/event hints")


class DoctorAdviseResponse(BaseModel):
    ok: bool
    errors: Optional[List[Dict[str, str]]] = None
    spec_hash: Optional[str] = None
    advice: Optional[str] = None
    allowed_actions: Optional[List[str]] = None
    blocked_actions: Optional[List[str]] = None
    safe_mode: Optional[bool] = None


# -------------------------
# Helpers (v0.1)
# -------------------------

def _breed_id_from_hash(spec_hash: str) -> str:
    # Deterministic: same spec_hash -> same breed_id (v0.1)
    return f"breed_{spec_hash[:16]}"

def _determinism_v01(normalized: Dict[str, Any]) -> Dict[str, Any]:
    gen = ((normalized.get("ai") or {}).get("generation") or {})
    temp = gen.get("temperature", 0.2)
    seed = gen.get("seed", None)

    # v0.1 measurable-ish score: we reward fixed seed + low temperature + strict validation.
    score = 0.70
    reasons: List[str] = ["strict_schema_validation"]
    if seed is not None:
        score += 0.20
        reasons.append("fixed_seed")
    if isinstance(temp, (int, float)) and temp <= 0.3:
        score += 0.10
        reasons.append("low_temperature")

    score = min(0.99, max(0.0, score))

    frozen = [
        "spec_schema_v0.1",
        "whatsapp_cloud_api_webhook_model",
        "primary_provider_ordering",
        "file_layout_v0.1",
        "rate_limit_strategy_v0.1",
    ]
    ai_may_decide = [
        "wording_of_responses_within_persona",
        "kb_snippet_selection_within_top_k",
    ]

    return {
        "version": "0.1",
        "score": round(score, 2),
        "reasons": reasons,
        "frozen": frozen,
        "ai_may_decide": ai_may_decide,
    }

def _plan_v01(normalized: Dict[str, Any], breed_id: str) -> Dict[str, Any]:
    app = normalized.get("app") or {}
    name = app.get("name") or "app"
    # Plan only: describe what HyperDef will generate in later steps
    return {
        "app_name": name,
        "breed_id": breed_id,
        "artifacts": [
            {"path": "apps/<name>/services/wa_webhook/", "type": "service", "desc": "WhatsApp webhook receiver + verifier"},
            {"path": "apps/<name>/services/ai_gateway/", "type": "service", "desc": "Ollama-first inference + fallback routing"},
            {"path": "apps/<name>/services/policy_engine/", "type": "lib", "desc": "Persona + refusal + action policy enforcement"},
            {"path": "apps/<name>/services/kb/", "type": "lib", "desc": "KB loader + chunker + retrieval top_k"},
            {"path": "apps/<name>/services/memory/", "type": "lib", "desc": "Short-term memory window store"},
            {"path": "apps/<name>/runbook/README.md", "type": "doc", "desc": "How to deploy, configure, operate"},
            {"path": "apps/<name>/.env.example", "type": "config", "desc": "Secrets refs (no values)"},
            {"path": "apps/<name>/systemd/", "type": "ops", "desc": "Systemd unit templates (optional in v0.1)"},
            {"path": "apps/<name>/doctor/hooks/", "type": "ops", "desc": "Doctor event emission hooks"},
        ],
        "constraints": {
            "no_ui_shipped": True,
            "no_multitenant_dashboard": True,
            "legal_whatsapp_flow": "meta_cloud_api_webhook",
        },
    }

def _runtime_endpoints_v01(normalized: Dict[str, Any]) -> Dict[str, Any]:
    ch = (normalized.get("channels") or {}).get("whatsapp") or {}
    wh = ch.get("webhook") or {}
    return {
        "whatsapp_webhook": {
            "public_base_url": wh.get("public_base_url"),
            "path": wh.get("path", "/webhooks/whatsapp"),
            "method": "POST",
        },
        "metrics": {"path": ((normalized.get("observability") or {}).get("metrics") or {}).get("path", "/metrics")},
        "doctor_events": {"path": "/api/events", "note": "HyperDev Doctor ring buffer (console-level)"},
    }


# -------------------------
# Endpoints
# -------------------------

@router.post("/validate", response_model=ValidateResponse)
async def hyperdef_validate(request: Request) -> Dict[str, Any]:
    # Accept raw YAML/text (UI posts application/yaml). Keep parsing internal.
    raw = await request.body()
    spec_text = raw.decode("utf-8", errors="replace").strip()

    if not spec_text:
        return {"ok": False, "errors": [{"path": "body", "message": "empty request body"}]}

    out = validate_spec_v01(spec_text, strict=True)
    if out.get("ok"):
        return {"ok": True, "spec_hash": out.get("spec_hash")}
    return {"ok": False, "errors": out.get("errors")}


@router.post("/breed", response_model=BreedResponse)
async def hyperdef_breed(req: BreedRequest) -> Dict[str, Any]:
    # v0.1: plan-only; enforce dry_run semantics so nobody mistakes this for a generator.
    if req.dry_run is not True:
        return {"ok": False, "errors": [{"path": "dry_run", "message": "v0.1 requires dry_run=true (plan-only)"}]}

    out = validate_spec_v01(req.spec_yaml, strict=True)
    if not out.get("ok"):
        return {"ok": False, "errors": out.get("errors")}

    spec_hash = out["spec_hash"]
    normalized = out["normalized"]
    breed_id = _breed_id_from_hash(spec_hash)

    return {
        "ok": True,
        "spec_hash": spec_hash,
        "breed_id": breed_id,
        "determinism": _determinism_v01(normalized),
        "artifacts_plan": _plan_v01(normalized, breed_id),
        "runtime_endpoints": _runtime_endpoints_v01(normalized),
    }


@router.post("/doctor/advise", response_model=DoctorAdviseResponse)
async def hyperdef_doctor_advise(req: DoctorAdviseRequest) -> Dict[str, Any]:
    out = validate_spec_v01(req.spec_yaml, strict=True)
    if not out.get("ok"):
        return {"ok": False, "errors": out.get("errors")}

    normalized = out["normalized"]
    spec_hash = out["spec_hash"]

    policies = normalized.get("policies") or {}
    safe_mode = bool(policies.get("safe_mode", True))
    allowed = list((policies.get("allowed_actions") or []))
    blocked = list((policies.get("disallowed_actions") or []))

    # v0.1 advice is intentionally conservative and read-only.
    advice = (
        "v0.1 Doctor: Spec is valid. Next step is breed(plan-only) then implement runtime services. "
        "Keep safe_mode enabled and keep disallowed actions blocked. "
        "Confirm WhatsApp Cloud API webhook verification token and access token are provided via secrets."
    )

    # Optionally incorporate small context hints without executing anything
    ctx = req.context or {}
    if ctx.get("signal") == "ollama_unreachable":
        advice = (
            advice
            + " Signal: ollama_unreachable. Check tailscale connectivity and base_url reachability."
        )

    return {
        "ok": True,
        "spec_hash": spec_hash,
        "advice": advice,
        "safe_mode": safe_mode,
        "allowed_actions": allowed,
        "blocked_actions": blocked,
    }
