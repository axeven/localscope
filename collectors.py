"""Host-level data collectors for localscope.

Read-only: shell out to docker / systemctl / ss and parse. No daemon SDKs, so
there is no client-vs-server version skew to keep in sync.

Every collector returns [] (never raises) when its tool is missing, so the
dashboard degrades to "fewer rows" instead of a 500.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
# docker's StartedAt carries nanosecond precision; datetime wants <=6 digits.
_NANOS = re.compile(r"(\.\d{6})\d+")

_MINUTE = 60


def _run(cmd, timeout=15):
    """Run a command, returning CompletedProcess or None if it could not run."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _iso(value):
    if not value or value.startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(_NANOS.sub(r"\1", value).replace("Z", "+00:00"))
    except ValueError:
        return None


def have_docker() -> bool:
    return shutil.which("docker") is not None


def docker_daemon_up() -> bool:
    """True only if the daemon answers.

    With Docker Desktop closed, `docker ps` exits non-zero with an empty stdout
    — indistinguishable from "no containers" unless you ask the daemon
    directly. That difference decides whether the alerter says "docker is
    unreachable" or fires a fake storm of individual downs.
    """
    if not have_docker():
        return False
    res = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=25)
    return bool(res and res.returncode == 0 and res.stdout.strip())


def have_systemd() -> bool:
    return shutil.which("systemctl") is not None


def containers() -> list[dict]:
    """One dict per container, including stopped ones.

    Keys: name, image, project, state, health, started_at, uptime_s, ports
    (list of "host_ip:host_port->container_port/tcp"), labels, working_dir.
    """
    if not have_docker():
        return []

    ids = _run(["docker", "ps", "-aq", "--no-trunc"])
    if ids is None or not ids.stdout.strip():
        return []

    raw = _run(["docker", "inspect", *ids.stdout.split()], timeout=40)
    if raw is None or not raw.stdout.strip():
        return []
    try:
        inspected = json.loads(raw.stdout)
    except json.JSONDecodeError:
        return []

    now = datetime.now(timezone.utc)
    out = []
    for c in inspected:
        state = c.get("State") or {}
        labels = (c.get("Config") or {}).get("Labels") or {}
        started = _iso(state.get("StartedAt"))
        health = (state.get("Health") or {}).get("Status")

        ports = []
        for container_port, bindings in ((c.get("NetworkSettings") or {}).get("Ports") or {}).items():
            bindings = [b for b in (bindings or []) if b]
            # Docker Desktop reports the IPv6 wildcard as a second binding for
            # the same container port; it is the same mapping, not a second one.
            has_v4_wildcard = any((b.get("HostIp") or "") == "0.0.0.0" for b in bindings)
            for b in bindings:
                host_ip = b.get("HostIp") or "0.0.0.0"
                if host_ip == "::" and has_v4_wildcard:
                    continue
                host_port = b.get("HostPort")
                ports.append(f"{host_ip}:{host_port}->{container_port}")

        out.append(
            {
                "name": (c.get("Name") or "").lstrip("/"),
                "image": (c.get("Config") or {}).get("Image"),
                "project": labels.get(COMPOSE_PROJECT_LABEL),
                "working_dir": labels.get(f"{COMPOSE_PROJECT_LABEL}.working_dir"),
                "state": state.get("Status"),          # running / exited / restarting
                "health": health,                       # healthy / unhealthy / None
                "exit_code": state.get("ExitCode"),
                "started_at": started,
                "finished_at": _iso(state.get("FinishedAt")),
                "uptime_s": int((now - started).total_seconds()) if started else None,
                "ports": ports,
                "labels": labels,
            }
        )
    out.sort(key=lambda r: (r["project"] or "~", r["name"]))
    return out


def user_units() -> list[dict]:
    """systemd --user service units that are loaded (running or not)."""
    if not have_systemd():
        return []

    res = _run(
        [
            "systemctl", "--user", "list-units", "--type=service", "--all",
            "--no-pager", "--no-legend", "--plain",
        ]
    )
    if res is None:
        return []

    units = []
    for line in res.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4 or not parts[0].endswith(".service"):
            continue
        unit, load, active, sub = parts[0], parts[1], parts[2], parts[3]
        units.append(
            {
                "unit": unit,
                "load": load,
                "active": active,      # active / inactive / failed
                "sub": sub,            # running / dead / failed
                "description": parts[4] if len(parts) > 4 else "",
            }
        )
    return units


def listening_ports() -> list[dict]:
    """TCP listeners, grouped by port: {port, addresses, process}."""
    ss = shutil.which("ss")
    if not ss:
        return []

    # -p is required for the process name; without it ss reports an empty one.
    res = _run([ss, "-tlnHp"])
    if res is None:
        return []

    grouped: dict[int, dict] = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        addr, _, port = parts[3].rpartition(":")
        try:
            port_i = int(port)
        except ValueError:
            continue
        proc = ""
        if "users:" in line:
            hit = re.search(r'users:\(\("([^"]+)"', line)
            proc = hit.group(1) if hit else ""
        entry = grouped.setdefault(port_i, {"port": port_i, "addresses": [], "process": ""})
        if addr and addr not in entry["addresses"]:
            entry["addresses"].append(addr)
        entry["process"] = entry["process"] or proc

    out = list(grouped.values())
    for e in out:
        e["addresses"].sort()
    out.sort(key=lambda r: r["port"])
    return out


def human_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    if seconds < _MINUTE:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // _MINUTE}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // _MINUTE:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"
