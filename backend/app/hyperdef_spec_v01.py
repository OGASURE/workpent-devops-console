from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import yaml


# -------------------------
# Errors (compiler-style)
# -------------------------

@dataclass
class SpecError:
    path: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"path": self.path, "message": self.message}


# -------------------------
# Helpers
# -------------------------

def _is_dict(x: Any) -> bool:
    return isinstance(x, dict)

def _is_list(x: Any) -> bool:
    return isinstance(x, list)

def _canon_json(obj: Any) -> str:
    # Canonical JSON for stable hashing
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def _get(d: Dict[str, Any], key: str, path: str, errors: List[SpecError]) -> Any:
    if key not in d:
        errors.append(SpecError(path=f"{path}.{key}" if path else key, message="required"))
        return None
    return d.get(key)

def _check_no_unknown_keys(
    obj: Dict[str, Any],
    allowed: set,
    path: str,
    errors: List[SpecError],
) -> None:
    for k in obj.keys():
        if k not in allowed:
            errors.append(SpecError(path=f"{path}.{k}" if path else k, message="unknown field"))

def _check_enum(val: Any, allowed: List[str], path: str, errors: List[SpecError]) -> None:
    if val is None:
        return
    if val not in allowed:
        errors.append(SpecError(path=path, message=f"must be one of {allowed}"))

def _check_type(val: Any, typ, path: str, errors: List[SpecError]) -> None:
    if val is None:
        return
    if not isinstance(val, typ):
        errors.append(SpecError(path=path, message=f"must be {typ.__name__}"))

def _check_ref_secrets(val: Any, path: str, errors: List[SpecError]) -> None:
    if val is None:
        return
    if not isinstance(val, str) or not val.startswith("secrets:"):
        errors.append(SpecError(path=path, message='must be a secrets ref like "secrets:NAME"'))

def _default(d: Dict[str, Any], key: str, value: Any) -> None:
    if key not in d:
        d[key] = value


# -------------------------
# v0.1 Schema (strict)
# -------------------------

def validate_spec_v01(spec_yaml: str, strict: bool = True) -> Dict[str, Any]:
    """
    Returns:
      {
        ok: bool,
        errors: [{path,message}]? ,
        normalized: dict? ,
        spec_hash: str?
      }
    """
    errors: List[SpecError] = []

    try:
        raw = yaml.safe_load(spec_yaml)
    except Exception as e:
        return {"ok": False, "errors": [SpecError(path="", message=f"YAML parse error: {e}").to_dict()]}

    if not _is_dict(raw):
        return {"ok": False, "errors": [SpecError(path="", message="spec must be a YAML mapping/object").to_dict()]}

    # Top-level allowed keys (v0.1)
    top_allowed = {
        "spec_version",
        "app_type",
        "app",
        "ui_integration",
        "channels",
        "persona",
        "knowledge",
        "memory",
        "ai",
        "policies",
        "billing",
        "observability",
        "deployment_target",
    }
    if strict:
        _check_no_unknown_keys(raw, top_allowed, "", errors)

    # Required: spec_version + app_type
    sv = _get(raw, "spec_version", "", errors)
    at = _get(raw, "app_type", "", errors)
    if sv is not None:
        _check_type(sv, str, "spec_version", errors)
        if sv != "0.1":
            errors.append(SpecError(path="spec_version", message='must be "0.1"'))
    if at is not None:
        _check_type(at, str, "app_type", errors)
        if at != "whatsapp_bot_generator":
            errors.append(SpecError(path="app_type", message='must be "whatsapp_bot_generator"'))

    # -------------------------
    # app (required)
    # -------------------------
    app = _get(raw, "app", "", errors)
    if _is_dict(app):
        allowed = {"name", "description", "owner", "timezone", "locale"}
        if strict:
            _check_no_unknown_keys(app, allowed, "app", errors)
        name = _get(app, "name", "app", errors)
        if name is not None:
            _check_type(name, str, "app.name", errors)
        _default(app, "description", "")
        _default(app, "owner", "workpent")
        _default(app, "timezone", "UTC")
        _default(app, "locale", "en")
    else:
        if app is not None:
            errors.append(SpecError(path="app", message="must be object"))

    # -------------------------
    # ui_integration (optional)
    # -------------------------
    ui = raw.get("ui_integration")
    if ui is not None:
        if not _is_dict(ui):
            errors.append(SpecError(path="ui_integration", message="must be object"))
        else:
            allowed = {"mode", "description", "capabilities", "transport", "api_mapping", "guarantees"}
            if strict:
                _check_no_unknown_keys(ui, allowed, "ui_integration", errors)

            _default(ui, "mode", "external_client")
            _check_enum(ui.get("mode"), ["external_client"], "ui_integration.mode", errors)

            caps = ui.get("capabilities")
            if caps is not None:
                if not _is_dict(caps):
                    errors.append(SpecError(path="ui_integration.capabilities", message="must be object"))
            transport = ui.get("transport")
            if transport is not None:
                if not _is_dict(transport):
                    errors.append(SpecError(path="ui_integration.transport", message="must be object"))
            mapping = ui.get("api_mapping")
            if mapping is not None:
                if not _is_dict(mapping):
                    errors.append(SpecError(path="ui_integration.api_mapping", message="must be object"))

            guarantees = ui.get("guarantees")
            if guarantees is not None:
                if not _is_dict(guarantees):
                    errors.append(SpecError(path="ui_integration.guarantees", message="must be object"))

    # -------------------------
    # channels (required) — WhatsApp only
    # -------------------------
    channels = _get(raw, "channels", "", errors)
    if _is_dict(channels):
        allowed = {"whatsapp"}
        if strict:
            _check_no_unknown_keys(channels, allowed, "channels", errors)
        wa = _get(channels, "whatsapp", "channels", errors)
        if _is_dict(wa):
            allowed = {"provider", "webhook", "meta", "legal"}
            if strict:
                _check_no_unknown_keys(wa, allowed, "channels.whatsapp", errors)
            _default(wa, "provider", "meta_cloud_api")
            _check_enum(wa.get("provider"), ["meta_cloud_api"], "channels.whatsapp.provider", errors)

            webhook = _get(wa, "webhook", "channels.whatsapp", errors)
            if _is_dict(webhook):
                allowed = {"public_base_url", "path", "verify_token_ref"}
                if strict:
                    _check_no_unknown_keys(webhook, allowed, "channels.whatsapp.webhook", errors)
                pbu = _get(webhook, "public_base_url", "channels.whatsapp.webhook", errors)
                if pbu is not None:
                    _check_type(pbu, str, "channels.whatsapp.webhook.public_base_url", errors)
                _default(webhook, "path", "/webhooks/whatsapp")
                _check_ref_secrets(webhook.get("verify_token_ref"), "channels.whatsapp.webhook.verify_token_ref", errors)
            else:
                if webhook is not None:
                    errors.append(SpecError(path="channels.whatsapp.webhook", message="must be object"))

            meta = _get(wa, "meta", "channels.whatsapp", errors)
            if _is_dict(meta):
                allowed = {"phone_number_id_ref", "waba_id_ref", "access_token_ref"}
                if strict:
                    _check_no_unknown_keys(meta, allowed, "channels.whatsapp.meta", errors)
                _check_ref_secrets(meta.get("phone_number_id_ref"), "channels.whatsapp.meta.phone_number_id_ref", errors)
                if meta.get("waba_id_ref") is not None:
                    _check_ref_secrets(meta.get("waba_id_ref"), "channels.whatsapp.meta.waba_id_ref", errors)
                _check_ref_secrets(meta.get("access_token_ref"), "channels.whatsapp.meta.access_token_ref", errors)
            else:
                if meta is not None:
                    errors.append(SpecError(path="channels.whatsapp.meta", message="must be object"))

            legal = _get(wa, "legal", "channels.whatsapp", errors)
            if _is_dict(legal):
                allowed = {"mode", "terms_ack"}
                if strict:
                    _check_no_unknown_keys(legal, allowed, "channels.whatsapp.legal", errors)
                _default(legal, "mode", "cloud_api_webhook")
                _check_enum(legal.get("mode"), ["cloud_api_webhook"], "channels.whatsapp.legal.mode", errors)
                ta = _get(legal, "terms_ack", "channels.whatsapp.legal", errors)
                if ta is not None and ta is not True:
                    errors.append(SpecError(path="channels.whatsapp.legal.terms_ack", message="must be true"))
            else:
                if legal is not None:
                    errors.append(SpecError(path="channels.whatsapp.legal", message="must be object"))
        else:
            if wa is not None:
                errors.append(SpecError(path="channels.whatsapp", message="must be object"))
    else:
        if channels is not None:
            errors.append(SpecError(path="channels", message="must be object"))

    # -------------------------
    # persona (required)
    # -------------------------
    persona = _get(raw, "persona", "", errors)
    if _is_dict(persona):
        allowed = {"role", "tone", "constraints", "refusal_rules", "escalation"}
        if strict:
            _check_no_unknown_keys(persona, allowed, "persona", errors)

        role = _get(persona, "role", "persona", errors)
        if role is not None:
            _check_type(role, str, "persona.role", errors)
        _default(persona, "tone", "polite_professional")

        constraints = persona.get("constraints")
        if constraints is not None and not _is_list(constraints):
            errors.append(SpecError(path="persona.constraints", message="must be list of strings"))

        refusal = _get(persona, "refusal_rules", "persona", errors)
        if _is_list(refusal):
            for i, rr in enumerate(refusal):
                p = f"persona.refusal_rules[{i}]"
                if not _is_dict(rr):
                    errors.append(SpecError(path=p, message="must be object"))
                    continue
                allowed_rr = {"id", "description", "response_style"}
                if strict:
                    _check_no_unknown_keys(rr, allowed_rr, p, errors)
                _get(rr, "id", p, errors)
                _get(rr, "description", p, errors)
                _default(rr, "response_style", "brief_refusal_with_helpful_alt")
        else:
            if refusal is not None:
                errors.append(SpecError(path="persona.refusal_rules", message="must be list"))

        esc = persona.get("escalation")
        if esc is not None and not _is_dict(esc):
            errors.append(SpecError(path="persona.escalation", message="must be object"))
    else:
        if persona is not None:
            errors.append(SpecError(path="persona", message="must be object"))

    # -------------------------
    # memory (required)
    # -------------------------
    memory = _get(raw, "memory", "", errors)
    if _is_dict(memory):
        allowed = {"mode", "short_term", "long_term"}
        if strict:
            _check_no_unknown_keys(memory, allowed, "memory", errors)
        _default(memory, "mode", "short_term")
        _check_enum(memory.get("mode"), ["none", "short_term", "long_term"], "memory.mode", errors)
    else:
        if memory is not None:
            errors.append(SpecError(path="memory", message="must be object"))

    # -------------------------
    # ai (required)
    # -------------------------
    ai = _get(raw, "ai", "", errors)
    if _is_dict(ai):
        allowed = {"routing_policy", "primary", "fallback", "prompts", "generation"}
        if strict:
            _check_no_unknown_keys(ai, allowed, "ai", errors)
        _default(ai, "routing_policy", "primary_then_fallback")
        _check_enum(ai.get("routing_policy"), ["primary_then_fallback", "cost_aware", "latency_aware"], "ai.routing_policy", errors)

        primary = _get(ai, "primary", "ai", errors)
        if _is_dict(primary):
            allowed_p = {"provider", "ollama"}
            if strict:
                _check_no_unknown_keys(primary, allowed_p, "ai.primary", errors)
            prov = _get(primary, "provider", "ai.primary", errors)
            _check_enum(prov, ["ollama"], "ai.primary.provider", errors)
            ol = _get(primary, "ollama", "ai.primary", errors)
            if _is_dict(ol):
                allowed_ol = {"base_url", "model", "timeout_s"}
                if strict:
                    _check_no_unknown_keys(ol, allowed_ol, "ai.primary.ollama", errors)
                _get(ol, "base_url", "ai.primary.ollama", errors)
                _default(ol, "model", "llama3.1")
                _default(ol, "timeout_s", 45)
            else:
                if ol is not None:
                    errors.append(SpecError(path="ai.primary.ollama", message="must be object"))
        else:
            if primary is not None:
                errors.append(SpecError(path="ai.primary", message="must be object"))
    else:
        if ai is not None:
            errors.append(SpecError(path="ai", message="must be object"))

    # NOTE: We keep v0.1 validator minimal: we validate the critical rails.
    # Other sections remain accepted but may be lightly validated later.

    if errors:
        return {"ok": False, "errors": [e.to_dict() for e in errors]}

    # Normalization for hashing (strict stable)
    normalized = raw
    canon = _canon_json(normalized)
    spec_hash = _sha256(canon)

    return {"ok": True, "normalized": normalized, "spec_hash": spec_hash}
