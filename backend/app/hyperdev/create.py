import re
import shutil
import subprocess
import time
from pathlib import Path

from app.hyperdev.ports import pick_free_port

APPS_DIR = Path("/srv/workpent/apps")
ARCHETYPES_DIR = Path("/srv/workpent/devops-console/hyperdev/archetypes")

ROOTCTL = "/usr/local/sbin/workpent-hyperdev-rootctl"

_slug_re = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")  # 2..63 chars


def run(cmd: list[str]) -> str:
    """Run a command and return combined stdout/stderr (for error messages)."""
    p = subprocess.run(cmd, text=True, capture_output=True)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{out}")
    return out


def _validate_slug(slug: str):
    if not _slug_re.match(slug):
        raise RuntimeError(
            "Invalid slug. Use lowercase letters, numbers, hyphen. Length 2..63. Example: tikaitik2"
        )


def _safe_remove(path: Path):
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except Exception:
        pass


def _sudo_rootctl(args: list[str]) -> str:
    """Call root helper with sudo (non-interactive)."""
    return run(["sudo", "-n", ROOTCTL] + args)


def _wait_for_health(url: str, timeout_s: int = 25) -> None:
    """Retry curl until OK or timeout."""
    t0 = time.time()
    last_err = ""
    while time.time() - t0 < timeout_s:
        try:
            run(["curl", "-fsS", url])
            return
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
    raise RuntimeError(f"Health check timed out after {timeout_s}s: {url}\nLast error:\n{last_err}")


def create_app(slug: str, name: str, archetype: str = "saas-web") -> dict:
    """
    Transactional app creation:
      - allocate port
      - create app dir
      - copy archetype template
      - write .env
      - render systemd unit (staged in app dir), then install via rootctl
      - render nginx snippet (staged in app dir), then install via rootctl
      - health-check with retries

    If anything fails: rollback (best effort) and remove app dir.
    """
    slug = (slug or "").strip()
    name = (name or "").strip()
    archetype = (archetype or "saas-web").strip()

    _validate_slug(slug)
    if not name:
        raise RuntimeError("Name cannot be empty")

    app_dir = APPS_DIR / slug
    if app_dir.exists():
        raise RuntimeError(f"App folder already exists: {app_dir}")

    template_dir = ARCHETYPES_DIR / archetype / "template"
    if not template_dir.exists():
        raise RuntimeError(f"Unknown archetype or missing template: {template_dir}")

    port = pick_free_port()

    service_name = f"workpent-{slug}.service"
    service_staging = app_dir / service_name
    nginx_staging = app_dir / f"{slug}.location.conf"

    systemd_installed = False
    nginx_installed = False

    try:
        # 1) Create app dir
        app_dir.mkdir(parents=True)

        # 2) Copy archetype
        shutil.copytree(template_dir, app_dir, dirs_exist_ok=True)

        # 3) Write .env (quotes => safe)
        (app_dir / ".env").write_text(f'APP_NAME="{name}"\nPORT={port}\n', encoding="utf-8")

        # 4) Render systemd unit into app folder
        service_template_path = app_dir / "systemd.service.template"
        if not service_template_path.exists():
            raise RuntimeError("Missing systemd.service.template in archetype template/")
        service_template = service_template_path.read_text(encoding="utf-8", errors="ignore")

        service_content = service_template.replace("__APP_SLUG__", slug)
        service_staging.write_text(service_content, encoding="utf-8")

        # 5) Install + start service
        _sudo_rootctl(["install_service", slug, str(service_staging)])
        systemd_installed = True

        # 6) Render nginx snippet into app folder
        nginx_template_path = app_dir / "nginx.location.conf.template"
        if not nginx_template_path.exists():
            raise RuntimeError("Missing nginx.location.conf.template in archetype template/")
        nginx_template = nginx_template_path.read_text(encoding="utf-8", errors="ignore")

        nginx_conf = (
            nginx_template
            .replace("__APP_SLUG__", slug)
            .replace("__APP_PORT__", str(port))
        )
        nginx_staging.write_text(nginx_conf, encoding="utf-8")

        # 7) Install nginx snippet + reload nginx
        _sudo_rootctl(["install_nginx_snippet", slug, str(nginx_staging)])
        nginx_installed = True
        _sudo_rootctl(["nginx_test_reload"])

        # 8) Health checks with retry
        _wait_for_health(f"http://127.0.0.1:{port}/health", timeout_s=30)
        _wait_for_health(f"http://127.0.0.1/{slug}/health", timeout_s=30)

        return {"slug": slug, "name": name, "port": port, "status": "created"}

    except Exception as e:
        # rollback best effort
        if nginx_installed:
            try:
                _sudo_rootctl(["remove_nginx_snippet", slug])
                _sudo_rootctl(["nginx_test_reload"])
            except Exception:
                pass

        if systemd_installed:
            try:
                _sudo_rootctl(["remove_service", slug])
            except Exception:
                pass

        _safe_remove(app_dir)

        raise RuntimeError(f"CREATE FAILED (rolled back): {e}")
