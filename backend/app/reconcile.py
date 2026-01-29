from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple
import subprocess

from app import registry

# -------------------------------------------------------------------
# Filesystem roots (keep in sync with main.py)
# -------------------------------------------------------------------
APPS_BASE = Path('/srv/workpent/apps')
DELETED_BASE = Path('/srv/workpent/deleted-apps')


APPS_BASE = Path("/srv/workpent/apps")
SYSTEMD_BASE = Path("/etc/systemd/system")

# Drift bitmask
FS_MISSING = 1
UNIT_MISSING = 4
WANTS_ORPHAN = 8
SYSTEMD_ACTIVE_WHEN_SHOULDNT = 16
SYSTEMD_INACTIVE_WHEN_SHOULD = 32
DELETED_BUT_FS_PRESENT = 64


def _systemd_unit_for_slug(slug: str) -> str:
    """
    Canonical unit filename for this app.

    IMPORTANT:
    Unit files live on disk as /etc/systemd/system/workpent-<slug>.service
    and systemctl expects the literal name with '-' (not systemd-escaped \\x2d).
    """
    return f"workpent-{slug}.service"


def _is_active(unit: str) -> str:
    try:
        res = subprocess.run(
            ["/bin/systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
        )
        out = (res.stdout or "").strip()
        return out or (res.stderr or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _find_wants_orphans(prefix: str = "workpent-") -> List[str]:
    removed = []
    for link in SYSTEMD_BASE.rglob(f"{prefix}*.service"):
        if not link.is_symlink():
            continue
        if link.name == "workpent-devops-console.service":
            continue
        target = link.resolve(strict=False)
        if not target.exists():
            removed.append(str(link))
    return removed


def build_report() -> Dict[str, Any]:
    rows = registry.list_apps()

    # detect filesystem folders that are NOT registered (security/drift)
    reg_slugs = {r["slug"] for r in rows}
    unregistered_dirs = []
    if APPS_BASE.exists():
        for d in APPS_BASE.iterdir():
            if d.is_dir() and d.name not in reg_slugs:
                unregistered_dirs.append(str(d))



    # global scan: orphan wants symlinks
    wants_orphans = _find_wants_orphans()

    apps_report: List[Dict[str, Any]] = []

    for r in rows:
        slug = r["slug"]
        desired = r.get("status") or "UNKNOWN"

        app_path = APPS_BASE / slug
        fs_present = app_path.is_dir()

        unit = _systemd_unit_for_slug(slug)
        unit_path = SYSTEMD_BASE / unit
        unit_present = unit_path.exists()

        active = _is_active(unit)

        drift = 0

        if desired == "DELETED":
            if fs_present:
                drift |= DELETED_BUT_FS_PRESENT
        else:
            if not fs_present:
                drift |= FS_MISSING

        # Unit expectations are Phase 1.5+ (after we generate units deterministically),
        # but we can still report missing unit if status is RUNNING.
        if desired == "RUNNING" and not unit_present:
            drift |= UNIT_MISSING

        if desired in ("STOPPED", "DELETED") and active == "active":
            drift |= SYSTEMD_ACTIVE_WHEN_SHOULDNT

        if desired == "RUNNING" and active != "active":
            drift |= SYSTEMD_INACTIVE_WHEN_SHOULD

        apps_report.append(
            {
                "slug": slug,
                "app_id": r.get("app_id"),
                "desired_status": desired,
                "observed_systemd": active,
                "fs_present": fs_present,
                "unit_name": unit,
                "unit_present": unit_present,
                "drift_flags": drift,
            }
        )

    apps_report.sort(key=lambda x: x["slug"])

    return {
        "apps": apps_report,
        "wants_orphans": wants_orphans,
        "unregistered_dirs": unregistered_dirs,
        "counts": {
            "apps_total": len(apps_report),
            "apps_with_drift": sum(1 for a in apps_report if a["drift_flags"] != 0),
            "wants_orphans": len(wants_orphans),
            "unregistered_dirs": len(unregistered_dirs),
        },
    }


def apply_observed(report: Dict[str, Any]) -> None:
    # Phase 2: enforcement (minimal)
    # If desired is STOPPED/DELETED but systemd is active, stop it.
    for a in report.get("apps", []):
        slug = a["slug"]
        desired = a.get("desired_status") or "UNKNOWN"
        unit = a.get("unit_name") or ""
        observed = a.get("observed_systemd") or "unknown"
        drift_flags = int(a.get("drift_flags", 0) or 0)

        if desired in ("STOPPED", "DELETED") and observed == "active" and unit:
            # best-effort stop; no interactive sudo
            try:
                subprocess.run(
                    ["sudo", "-n", "/bin/systemctl", "stop", unit],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except Exception:
                pass

        registry.update_observed(slug, observed, drift_flags)


def quarantine_unregistered(unregistered_dirs: list[str]) -> dict:
    """
    Move unregistered /srv/workpent/apps/* folders into deleted-apps/_quarantine.
    This is a safety repair action to eliminate invisible drift.
    """
    from pathlib import Path
    import shutil
    from datetime import datetime

    qbase = DELETED_BASE / "_quarantine"
    qbase.mkdir(parents=True, exist_ok=True)

    moved = []
    skipped = []

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    for d in unregistered_dirs:
        try:
            p = Path(d)
            name = p.name

            # paranoia guards
            if "/" in name or name in {".", ".."}:
                skipped.append({"path": d, "reason": "invalid_name"})
                continue
            if not p.exists() or not p.is_dir():
                skipped.append({"path": d, "reason": "not_a_dir"})
                continue

            dest = qbase / f"{name}-{ts}"
            shutil.move(str(p), str(dest))
            moved.append({"from": str(p), "to": str(dest)})
        except Exception as e:
            skipped.append({"path": d, "reason": f"error:{e}"})

    return {"moved": moved, "skipped": skipped}
