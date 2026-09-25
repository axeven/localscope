"""localscope - a local web view of what is running on this machine.

Read-only. A background poller refreshes one cached snapshot every
poller_interval_s; the page and /api/services only ever read that cache, so
the UI stays instant and watched services are not probed per request.

Status semantics live in status.py, shared with the alerter (monitor.py).

Run:  venv/bin/python app.py     (or the localscope systemd user unit)
"""

from __future__ import annotations

import threading
import time
import urllib.request
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template

import collectors
import status as status_mod
import targets_config

HOST = "127.0.0.1"
PORT = 8090

app = Flask(__name__)

STATE: dict = {
    "generated_at": None,
    "interval_s": None,
    "services": [],
    "problems": [],
    "orphan_ports": [],
    "counts": {"up": 0, "down": 0, "finished": 0, "total": 0},
    "errors": [],
}
LOCK = threading.Lock()


def build_snapshot(cfg: dict) -> dict:
    now = datetime.now(timezone.utc)
    errors: list[str] = []

    if not collectors.have_docker():
        errors.append("docker CLI not found")
    elif not collectors.docker_daemon_up():
        errors.append("docker daemon unreachable (Docker Desktop stopped?)")
    containers = collectors.containers()
    units = collectors.user_units()
    ports = collectors.listening_ports()

    by_container = {c["name"]: c for c in containers}
    by_unit = {u["unit"]: u for u in units}
    targets = targets_config.expand(cfg, containers, units)

    published = {p.split("->")[0].split(":")[-1] for c in containers for p in c["ports"]}
    services = []

    for t in targets:
        container = by_container.get(t["container"]) if t["container"] else None
        unit = by_unit.get(t["unit"]) if t["unit"] else None

        st, reason = status_mod.evaluate_target(t, container, unit)

        running = container is not None and (container.get("state") == "running")
        uptime_s = container.get("uptime_s") if container else None
        if container and not running and container.get("finished_at"):
            uptime_s = None

        services.append(
            {
                "name": t["name"],
                "kind": t["kind"],
                "project": t["project"],
                "required": t["required"],
                "discovered": t["discovered"],
                "url": t["url"],
                "container": t["container"],
                "unit": t["unit"],
                "status": st,
                "up": st == "up",
                "reason": reason,
                "container_state": (container or {}).get("state"),
                "health": (container or {}).get("health"),
                "uptime_s": uptime_s,
                "uptime": collectors.human_uptime(uptime_s),
                "ports": (container or {}).get("ports") or [],
            }
        )

    orphans = [
        {
            "port": p["port"],
            "addresses": ", ".join(p["addresses"]),
            "process": p["process"],
        }
        for p in ports
        if str(p["port"]) not in published
    ]

    problems = [s for s in services if s["status"] == "down"]
    return {
        "generated_at": now.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "interval_s": cfg["poller_interval_s"],
        "services": services,
        "problems": problems,
        "orphan_ports": orphans,
        "counts": {
            "up": sum(1 for s in services if s["status"] == "up"),
            "down": len(problems),
            "finished": sum(1 for s in services if s["status"] == "finished"),
            "total": len(services),
        },
        "errors": errors,
    }


def poll_forever(cfg: dict) -> None:
    while True:
        started = time.monotonic()
        try:
            snapshot = build_snapshot(cfg)
        except Exception as e:  # noqa: BLE001 - never let the poller die
            snapshot = {
                "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
                "interval_s": cfg["poller_interval_s"],
                "services": [],
                "problems": [],
                "orphan_ports": [],
                "counts": {"up": 0, "down": 0, "finished": 0, "total": 0},
                "errors": [f"{type(e).__name__}: {e}"],
            }
        with LOCK:
            STATE.update(snapshot)
        time.sleep(max(2, cfg["poller_interval_s"] - (time.monotonic() - started)))


@app.get("/")
def index():
    with LOCK:
        snapshot = dict(STATE)
    return render_template("index.html", snap=snapshot)


@app.get("/api/services")
def api_services():
    with LOCK:
        return jsonify(dict(STATE))


def wait_until_serving(timeout: float = 10.0) -> bool:
    """Block until this app answers on its own port.

    The dashboard is one of its own targets, so polling before Flask has bound
    the port records a bogus DOWN for the dashboard on every restart — a wart
    that also makes a cron tick landing in that window count a failure.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://{HOST}:{PORT}/api/services", timeout=1).read(1)
            return True
        except Exception:  # noqa: BLE001 - not listening yet
            time.sleep(0.2)
    return False


def main() -> None:
    cfg = targets_config.load()
    # Serve in a thread and poll from the main one, so the poller can wait for
    # the port to be live before its first probe.
    threading.Thread(
        target=app.run,
        kwargs={"host": HOST, "port": PORT, "threaded": True},
        daemon=True,
    ).start()
    ready = wait_until_serving()
    print(
        f"localscope serving on http://{HOST}:{PORT}"
        if ready
        else "localscope: readiness check timed out; polling anyway",
        flush=True,
    )
    poll_forever(cfg)


if __name__ == "__main__":
    main()
