# Operations

FatChin has two user services and one JSON state directory.

## Run the interface

```sh
fatchin-web --host 127.0.0.1 --port 18465 \
  --config ~/.config/gha-worker/config.toml \
  --env-file ~/.config/gha-worker/env
```

Binding beyond loopback requires an admin token:

```sh
export FATCHIN_ADMIN_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
fatchin-web --host 0.0.0.0 --port 18465
```

The browser prompts for the token once. `/api/*` requires it.

## Run the worker

```sh
fatchin run
```

The worker polls the configured repositories and drains the saved queue. The
fleet supervisor runs inside this process.

## systemd (optional)

`deploy/install.sh` installs `gha-worker.service` and `gha-worker-gui.service`
as user units. It enables them but does not start them.

```sh
systemctl --user start gha-worker gha-worker-gui
journalctl --user -u gha-worker -f
```

## State and recovery

The state directory holds `jobs.json`, `watermarks.json`, `health.json`, the
worker lock, and `fleet-v1/state.json`. Back it up before upgrades.

- A stopped worker leaves queued jobs in place.
- A corrupted state file is reported, not silently replaced with empty state.
- Fleet work is bounded by leases; an interrupted worker is reclaimed after its
  lease expires.
- Completed fleet items are pruned to the most recent 2,000 per fleet.

## HTTPS

Terminate TLS at a reverse proxy you trust, or pass `--tls-cert` and `--tls-key`
to `fatchin-web`. FatChin does not obtain certificates.
