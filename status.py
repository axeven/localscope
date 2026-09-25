"""Probe and status semantics — one implementation, used by both sides.

app.py (the dashboard) and monitor.py (the alerter) call evaluate_target()
here. If the page and the alert disagreed about what "down" means you would
stop trusting both of them, so this is deliberately not duplicated.

Status vocabulary:
  up       probe passed
  down     probe failed / container exited non-zero / unit not active
  finished one-shot container that exited 0 — expected to be gone, never an alert
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request

# The dashboard polls again in ~30s, so a blip can be left for the next tick.
DASHBOARD_TIMEOUT_S = 4
DASHBOARD_RETRIES = 1
# The alerter runs every few minutes and pages a human: smooth the blip first.
ALERT_TIMEOUT_S = 10
ALERT_RETRIES = 2
ALERT_RETRY_DELAY_S = 3


def http_probe(url: str, timeout: float, retries: int, retry_delay: float) -> tuple[bool, str]:
    """(ok, reason). A reachable server answering <500 is up; 404 is still up."""
    last = ""
    for attempt in range(max(1, retries)):
        if attempt:
            time.sleep(retry_delay)
        req = urllib.request.Request(url, headers={"User-Agent": "localscope/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.status
                return (True, f"HTTP {code}") if code < 500 else (False, f"HTTP {code}")
        except urllib.error.HTTPError as e:
            return (True, f"HTTP {e.code}") if e.code < 500 else (False, f"HTTP {e.code}")
        except urllib.error.URLError as e:
            last = str(getattr(e, "reason", e))
        except Exception as e:  # noqa: BLE001 - surface anything unexpected
            last = f"{type(e).__name__}: {e}"
    return False, last or "no response"


def tcp_probe(url: str, timeout: float) -> tuple[bool, str]:
    """url here is host:port. A port that accepts a connection is up."""
    host, _, port = url.split("://", 1)[-1].rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=timeout):
            return True, f"tcp connect ok (: {port})".replace(": ", ":")
    except Exception as e:  # noqa: BLE001 - a refused/timed-out port is down
        return False, f"{type(e).__name__}: {e}"


def docker_status(container: dict | None) -> tuple[str, str]:
    """Container state, upgraded to the healthcheck verdict when one exists.

    'finished' is for one-shot containers that exited cleanly (compose init
    tasks): they are expected to be gone, so they must not page anyone, but
    they also must not be counted as up.
    """
    if container is None:
        return "down", "container not found"
    state = (container.get("state") or "unknown").lower()
    health = (container.get("health") or "").lower()
    exit_code = container.get("exit_code")

    if state == "running":
        if health == "unhealthy":
            return "down", "running but unhealthy"
        return "up", "healthy" if health == "healthy" else "running"
    if state == "exited" and exit_code == 0:
        return "finished", "one-shot task, exited 0"
    if state == "exited":
        return "down", f"exited with code {exit_code}"
    return "down", f"container {state}"


def systemd_status(unit: dict | None) -> tuple[str, str]:
    if unit is None:
        return "down", "unit not loaded"
    if unit["active"] == "active":
        return "up", f"active ({unit['sub']})"
    return "down", f"{unit['active']} ({unit['sub']})"


def evaluate_target(
    target: dict,
    container: dict | None,
    unit: dict | None,
    *,
    timeout: float = DASHBOARD_TIMEOUT_S,
    retries: int = DASHBOARD_RETRIES,
    retry_delay: float = 0.0,
) -> tuple[str, str]:
    """(status, reason) for one expanded target."""
    kind = target["kind"]

    if kind == "docker":
        return docker_status(container)
    if kind == "systemd":
        return systemd_status(unit)
    if kind == "tcp":
        if not target.get("url"):
            return "down", "no address configured"
        return tcp_probe(target["url"], timeout)

    # http (the default): probe, and fold container state in as a tiebreaker so
    # "exited with code 137" is what you read, not just "connection refused".
    if not target.get("url"):
        return "down", "no url configured"
    ok, reason = http_probe(target["url"], timeout, retries, retry_delay)
    if ok:
        return "up", reason
    if container is not None:
        cstatus, creason = docker_status(container)
        if cstatus == "down":
            return "down", f"{creason} / {reason}"
    return "down", reason
