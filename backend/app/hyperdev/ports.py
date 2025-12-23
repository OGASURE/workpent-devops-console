import re
import socket
import subprocess
from pathlib import Path
from typing import Set

APPS_DIR = Path("/srv/workpent/apps")

# Optional: reserve ports to avoid race conditions when creating apps concurrently
RESERVED_DIR = Path("/srv/workpent/devops-console/backend/.state/ports")
RESERVED_DIR.mkdir(parents=True, exist_ok=True)

PORT_MIN = 9500
PORT_MAX = 9999


def _is_listening_ss(port: int) -> bool:
    """
    True if ANY process is listening on TCP port (IPv4 or IPv6),
    using `ss -lntH` which is reliable on Ubuntu.

    We look for occurrences like:
      127.0.0.1:9511
      0.0.0.0:9511
      [::1]:9511
      [::]:9511
    """
    try:
        out = subprocess.check_output(["ss", "-lntH"], text=True)
    except Exception:
        # If ss fails, don't trust "free". Return False here and let fallback probe decide.
        return False

    # Match ":PORT " (space after port) in any line
    pat = re.compile(rf":{port}\s")
    return any(pat.search(line) for line in out.splitlines())


def _is_listening_probe(port: int) -> bool:
    """
    Fallback probe: checks common bind targets.

    Note:
    - connect_ex works when something is listening on that specific address.
    - A service listening on 0.0.0.0:PORT will accept on 127.0.0.1 too.
    - IPv6-only listeners might not be caught by IPv4 probe, so we probe both.
    """
    # IPv4 probe
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.05)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
    except Exception:
        pass

    # IPv6 probe (if supported)
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s6:
            s6.settimeout(0.05)
            # ::1 is IPv6 loopback
            if s6.connect_ex(("::1", port)) == 0:
                return True
    except Exception:
        pass

    return False


def _is_listening(port: int) -> bool:
    """
    Robust check:
    - Prefer ss (covers 0.0.0.0, IPv6, etc.)
    - Fallback to socket probing if ss isn't available
    """
    if _is_listening_ss(port):
        return True
    return _is_listening_probe(port)


def _allocated_ports_from_env() -> Set[int]:
    """
    Reads /srv/workpent/apps/*/.env and collects PORT=... values.
    So we don't reuse ports that were already assigned before.
    """
    ports: Set[int] = set()

    if not APPS_DIR.exists():
        return ports

    for env_path in APPS_DIR.glob("*/.env"):
        try:
            text = env_path.read_text(errors="ignore")
        except Exception:
            continue

        # Accept:
        # PORT=9511
        # PORT="9511"
        # PORT='9511'
        m = re.search(
            r"^\s*PORT\s*=\s*['\"]?(\d+)['\"]?\s*$",
            text,
            re.MULTILINE,
        )
        if m:
            ports.add(int(m.group(1)))

    return ports


def _reserved_ports() -> Set[int]:
    """
    Ports reserved by HyperDev during creation, to avoid two concurrent creates
    selecting the same port.
    """
    ports: Set[int] = set()
    try:
        for p in RESERVED_DIR.glob("*.lock"):
            try:
                ports.add(int(p.stem))
            except Exception:
                continue
    except Exception:
        pass
    return ports


def reserve_port(port: int) -> None:
    """
    Creates a reservation lock file for the port.
    Call this as soon as you pick a port in /create, BEFORE doing heavy work.
    """
    lock = RESERVED_DIR / f"{port}.lock"
    lock.write_text("reserved\n")


def release_port(port: int) -> None:
    """
    Removes reservation lock file.
    Call this if create fails, or after you finish and write the real .env.
    """
    lock = RESERVED_DIR / f"{port}.lock"
    try:
        lock.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def pick_free_port() -> int:
    """
    Picks a safe free port between 9500-9999:
    - not present in any existing app .env
    - not currently listening (IPv4/IPv6/0.0.0.0)
    - not reserved by a concurrent create
    """
    allocated = _allocated_ports_from_env()
    reserved = _reserved_ports()

    for port in range(PORT_MIN, PORT_MAX + 1):
        if port in allocated:
            continue
        if port in reserved:
            continue
        if _is_listening(port):
            continue
        return port

    raise RuntimeError(f"No free ports available in range {PORT_MIN}-{PORT_MAX}")

