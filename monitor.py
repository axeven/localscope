#!/usr/bin/env python3
"""Edge-triggered alerter for localscope — the half that wakes you up.

Runs as a Hermes cron job with no_agent=True: empty stdout means nothing is
sent, so this is silent while everything is up and prints only on a
transition (up->down, down->up). A target that stays down does not re-alert
every tick; that is how an alert channel gets ignored.

Deliberately independent of the dashboard. It reads the same targets.json and
probes the targets itself rather than reading the dashboard's API, because a
bug or a crash in the dashboard would otherwise look exactly like "everything
is fine". The dashboard is on the target list, so a dead dashboard is its own
alert.

Exit code is always 0; the alert text is the payload, not the exit status.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import collectors
import status as status_mod
import targets_config

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / ".localscope-alert.state.json"

# Pseudo-target keys for box-level conditions that are not a single service.
DOCKER_KEY = "__docker_daemon__"


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        # Corrupt state must not silently disable alerting: say so, and start over.
        print("⚠️ localscope: alert state file was unreadable, rebuilt from scratch.")
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def fmt_down(target: dict, reason: str) -> str:
    where = []
    if target.get("project"):
        where.append(f"project {target['project']}")
    if target.get("container"):
        where.append(f"container {target['container']}")
    if target.get("unit"):
        where.append(f"unit {target['unit']}")
    if target.get("url"):
        where.append(target["url"])
    suffix = f" ({', '.join(where)})" if where else ""
    return f"🔴 DOWN: {target['name']}{suffix}\n   {reason}"


def main() -> None:
    cfg = targets_config.load()
    alert_cfg = cfg["alert"]

    containers = collectors.containers()
    units = collectors.user_units()
    daemon_up = collectors.docker_daemon_up()

    by_container = {c["name"]: c for c in containers}
    by_unit = {u["unit"]: u for u in units}
    all_targets = targets_config.expand(cfg, containers, units)
    targets = [t for t in all_targets if t["required"]]
    state = load_state()
    alerts: list[str] = []
    now = int(time.time())

    # --- box condition: no docker daemon means every container check is blind.
    # One alert for that, not one per container, and container-backed targets
    # are left 'unknown' instead of being reported as a fake storm of downs.
    suspended: list[str] = []
    if not daemon_up:
        suspended = [t["name"] for t in targets if t["container"]]
        prev = state.get(DOCKER_KEY) or {}
        if prev.get("up") is not False:
            detail = ""
            if suspended:
                shown = ", ".join(suspended[:8]) + (" …" if len(suspended) > 8 else "")
                detail = f"\n   checks suspended for {len(suspended)}: {shown}"
            alerts.append(
                "🔴 DOWN: docker daemon unreachable\n"
                "   Docker Desktop stopped, or the socket is not available." + detail
            )
        state[DOCKER_KEY] = {"up": False, "down_since": prev.get("down_since") or now, "checked_at": now}
    else:
        prev = state.get(DOCKER_KEY) or {}
        if prev.get("up") is False:
            mins = int((now - (prev.get("down_since") or now)) / 60)
            alerts.append(f"🟢 RECOVERED: docker daemon responds again (after {mins} min).")
        state.pop(DOCKER_KEY, None)

    # --- per-target edge detection
    for t in targets:
        name = t["name"]
        if t["container"] and not daemon_up:
            continue  # blind, not down — see above
        container = by_container.get(t["container"]) if t["container"] else None
        unit = by_unit.get(t["unit"]) if t["unit"] else None

        state_key = name
        st, reason = status_mod.evaluate_target(
            t,
            container,
            unit,
            timeout=alert_cfg["timeout_s"],
            retries=status_mod.ALERT_RETRIES,
            retry_delay=alert_cfg["retry_delay_s"],
        )
        prev = state.get(state_key) or {}
        was = prev.get("status")
        streak = int(prev.get("fail_streak") or 0)

        if st == "finished":
            # Neutral: not up, not a problem, and never an alert in either direction.
            state[state_key] = {"status": "finished", "checkretries": 0, "checked_at": now}
            continue

        if st == "down":
            streak += 1
            # A brand-new target that is already down alerts on its first check
            # ("first check" is a baseline, not a transition we can trust).
            threshold = alert_cfg["fail_after"] if was is not None else 1
            already_alerted = bool(prev.get("alerted"))
            if not already_alerted and streak >= threshold:
                tag = "" if was is not None else " (first check)"
                alerts.append(fmt_down(t, reason) + tag)
                already_alerted = True
            entry = {"status": "down", "fail_streak": streak, "alerted": already_alerted, "checked_at": now}
            entry["down_since"] = prev.get("down_since") or now
            state[state_key] = entry
        else:
            if was == "down":
                mins = int((now - (prev.get("down_since") or now)) / 60)
                dur = f" after {mins} min" if mins else ""
                alerts.append(f"🟢 RECOVERED: {name} — back up{dur}.")
            state[state_key] = {"status": "up", "fail_streak": 0, "alerted": False, "checked_at": now}

    # Drop state for targets that no longer exist. Otherwise re-adding the same
    # name weeks later inherits an old "down" and fires a phantom RECOVERED.
    known = {t["name"] for t in all_targets} | {DOCKER_KEY}
    for stale in [k for k in state if k not in known]:
        state.pop(stale, None)

    save_state(state)

    if not alerts:
        return  # silent tick: no message sent
    print(f"localscope — {len(alerts)} change(s)")
    print()
    for a in alerts:
        print(a)
    print()
    print(f"Dashboard: http://127.0.0.1:8090  ·  checked {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")


if __name__ == "__main__":
    main()
