"""Load targets.json and expand it into the effective watch list.

The dashboard (app.py) and the alerter (monitor.py) both call expand(), so
"what is watched, how, and whether it pages you" is defined in exactly one
place. Expansion works in both directions:

  - explicit entries in targets.json win over discovery (they can set
    required/thresholds and give a url for a container that publishes none);
  - anything discovered (a container, a systemd unit) that an explicit entry
    names is *merged* into that entry instead of becoming a second row;
  - a container carrying the `localscope.required` label is required without
    any config edit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGETS_FILE = Path(os.environ.get("LOCALSCOPE_TARGETS") or HERE / "targets.json")

DEFAULTS = {
    "poller_interval_s": 30,
    "required_by_label": "localscope.required",
    "alert": {"fail_after": 2, "timeout_s": 10, "retry_delay_s": 3},
    # Session plumbing that is not a service you would ever act on.
    "systemd_ignore": ["dbus", "at-spi-dbus-bus", "gpg-agent", "pipewire", "wireplumber"],
}

TRUTHY = {"1", "true", "yes", "on"}


def _is_truthy(value) -> bool:
    return str(value).strip().lower() in TRUTHY if value is not None else False


def load(path: Path | None = None) -> dict:
    """Read targets.json, filling defaults. Raises on unreadable/invalid JSON."""
    path = path or TARGETS_FILE
    cfg = dict(DEFAULTS)
    cfg["alert"] = dict(DEFAULTS["alert"])
    if path.exists():
        loaded = json.loads(path.read_text())
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must contain a JSON object")
        alert = dict(cfg["alert"])
        alert.update(loaded.pop("alert", {}) or {})
        cfg.update(loaded)
        cfg["alert"] = alert
    cfg.setdefault("targets", [])
    if not isinstance(cfg["targets"], list):
        raise ValueError(f"{path}: 'targets' must be a list")
    return cfg


def _host_port(url: str) -> int | None:
    """Extract the port from http://host:port/... or host:port."""
    rest = url.split("://", 1)[-1].rstrip("/")
    authority = rest.split("/", 1)[0]
    _, _, port = authority.rpartition(":")
    try:
        return int(port)
    except ValueError:
        return None


def _probe_kind(target: dict) -> str:
    return target.get("kind", "http")


def expand(cfg: dict, containers: list[dict], units: list[dict]) -> list[dict]:
    """Return the effective target list.

    Each target: {name, kind, url, required, container, unit, project,
    discovered} where kind is one of http | docker | tcp | systemd.
    """
    required_label = cfg.get("required_by_label") or DEFAULTS["required_by_label"]
    by_container = {c["name"]: c for c in containers}
    by_unit = {u["unit"]: u for u in units}

    targets: list[dict] = []
    claimed_containers: set[str] = set()
    claimed_units: set[str] = set()

    for entry in cfg.get("targets", []):
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        t = {
            "name": entry["name"],
            "kind": _probe_kind(entry),
            "url": entry.get("url"),
            "container": entry.get("container"),
            "unit": entry.get("unit"),
            "project": None,
            "required": bool(entry.get("required", False)),
            "failure_threshold": entry.get("fail_after", cfg["alert"]["fail_after"]),
            "discovered": False,
        }

        # Merge a named container/unit so config rows and live rows are one row.
        if not t["container"] and t["url"]:
            port = _host_port(t["url"])
            if port:
                for c in containers:
                    if any(p.split("->")[0].endswith(f":{port}") for p in c["ports"]):
                        t["container"] = c["name"]
                        break
        elif t["kind"] == "docker" and not t["container"]:
            if t["name"] in by_container:
                t["container"] = t["name"]
        elif t["kind"] == "systemd" and not t["unit"]:
            for unit_name in (t["name"], f"{t['name']}.service"):
                if unit_name in by_unit:
                    t["unit"] = unit_name
                    break

        if t["container"]:
            claimed_containers.add(t["container"])
            t["project"] = (by_container.get(t["container"]) or {}).get("project")
        if t["unit"]:
            claimed_units.add(t["unit"])
        targets.append(t)

    # Discovery: every unclaimed container, required only when labelled so.
    for c in containers:
        if c["name"] in claimed_containers:
            continue
        labelled = _is_truthy(c["labels"].get(required_label))
        url = None
        for p in c["ports"]:
            host = p.partition("->")[0]
            url = url or f"http://127.0.0.1:{host.rpartition(':')[2]}/"
        targets.append(
            {
                "name": c["name"],
                "kind": "http" if url else "docker",
                "url": url,
                "container": c["name"],
                "unit": None,
                "project": c["project"],
                "required": labelled,
                "failure_threshold": cfg["alert"]["fail_after"],
                "discovered": True,
            }
        )

    ignore_units = {str(u).removesuffix(".service") for u in (cfg.get("systemd_ignore") or [])}
    for u in units:
        if u["unit"] in claimed_units or u["active"] != "active":
            continue
        if u["unit"].removesuffix(".service") in ignore_units:
            continue
        targets.append(
            {
                "name": u["unit"].removesuffix(".service"),
                "kind": "systemd",
                "url": None,
                "container": None,
                "unit": u["unit"],
                "project": None,
                "required": _is_truthy(cfg.get("systemd_required_default")),
                "failure_threshold": cfg["alert"]["fail_after"],
                "discovered": True,
            }
        )

    targets.sort(key=lambda t: (not t["required"], t.get("project") or "~", t["name"]))
    return targets
