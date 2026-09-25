# localscope

A local, read-only web view of what is running on this machine — the `docker ps`
you actually want, plus everything that isn't in docker (systemd user units,
bare listening ports).

    cd ~/github/axeven/localscope
    ./venv/bin/python app.py        # http://127.0.0.1:8090

## How it works

    targets.json ──┐
                   ├─> targets_config.expand() ──┬─> app.py     poller -> cached snapshot -> page + /api/services
    docker labels ─┘                              └─> monitor.py independent probes -> edge-triggered alerts

A background poller (default 30s) refreshes one cached snapshot. The page and
`/api/services` only ever read that cache, so the UI is instant and watched
services are not probed once per request.

Nothing here writes to the things it watches: collectors only shell out to
`docker`, `systemctl --user` and `ss`.

## Statuses

| status | meaning |
|---|---|
| `up` | probe passed; for containers, running and (if defined) healthcheck healthy |
| `down` | probe failed, container exited non-zero, or unit is not active |
| `finished` | one-shot container that exited 0 (a compose init task) — not a problem |

A container with a published host port is checked over HTTP (status < 500
counts as up), so a container that is *Up* but whose app is broken shows as
DOWN. Containers with no published port can only be judged by container state,
which the dashboard states plainly rather than dressing up as health.

## Registering a service to watch (three tiers)

1. **Automatic** — every container, active systemd user unit and listening port
   appears within one poll. No config, nothing to remember.
2. **Label, no config edit** — put this on a compose service and it is adopted
   into the paging set as well as the dashboard:

       labels:
         localscope.required: "true"

   Both the dashboard and the alerter read docker labels, so `docker compose up -d`
   is the whole registration step.
3. **Explicit entry** in `targets.json` — needed for anything that cannot carry
   a label (a systemd unit, a bare port), and the place to override kind, url or
   thresholds:

       { "name": "my-api", "kind": "http", "url": "http://127.0.0.1:9100/healthz", "required": true }

   `kind` is one of `http` (probe a URL), `docker` (container state, upgraded to
   healthcheck when one exists), `tcp` (port accepts a connection), `systemd`
   (`systemctl --user is-active`). `required: true` means it pages you.

Config keys at the top of `targets.json`: `poller_interval_s`, `required_by_label`,
`systemd_ignore` (session plumbing that is not a service you would act on), and
`alert.fail_after` (consecutive failures before an alert).

## Down alerts

`monitor.py` is the half that wakes you up: a Hermes cron job (id
`340f8cb0ea9a`, `no_agent`, every 5 minutes, delivered to the Telegram channel
`-5452968665`) runs `~/.hermes/scripts/localscope-monitor.sh`, which execs this
repo's `monitor.py`. Empty stdout means no message is sent, so a healthy tick is
silent and costs nothing.

- 2 consecutive failures (`alert.fail_after`) before a target pages you, so a
  single blip does not.
- One message per transition: `up -> down` once, then silence however long it
  stays down; `down -> up` once on recovery. A target already down on its very
  first check alerts immediately, tagged `(first check)`.
- If the docker daemon is unreachable, ONE message names every container-backed
  check that just went blind — not one fake alert per service. Those targets are
  left `unknown` rather than reported as down, since you cannot judge them
  without the daemon.
- The dashboard is on the target list, so a dead dashboard is its own alert.
- State lives in `.localscope-alert.state.json` (gitignored) and is pruned, so
  removing a target and re-adding it later cannot fire a phantom RECOVERED.

Run it by hand exactly the way cron does:

    cd ~/github/axeven/localscope && ./venv/bin/python monitor.py

## Deployment

`localscope.service` (symlinked into `~/.config/systemd/user/`) runs the
dashboard with `Restart=always`, enabled, and `loginctl` linger is on so it
survives logout:

    systemctl --user status localscope      # is it up?
    systemctl --user restart localscope     # after changing app code
    journalctl --user -u localscope -n 50   # why did it die?

WSL prerequisites, as configured on this box: `/etc/wsl.conf` has
`systemd=true`, Docker Desktop has `AutoStart: true` and Ubuntu listed in
`IntegratedWslDistros`. The one thing config cannot prove is whether the distro
really comes up after a Windows reboot — if you want that guaranteed, add a
Windows Task Scheduler logon task running `wsl.exe -d Ubuntu true`.

## Files

- `collectors.py` — docker / systemd / ss readers, all read-only, all degrade to `[]`
- `targets_config.py` — loads targets.json, expands discovery + labels into the effective watch list
- `status.py` — probe + status semantics, shared by the dashboard and the alerter
- `app.py` — Flask app: poller thread, `/api/services`, one page
- `templates/index.html` — the table, plain fetch polling, no CDN and no build step
- `monitor.py` — the alerter: separate process, own state, edge-triggered
- `localscope.service` — systemd user unit
