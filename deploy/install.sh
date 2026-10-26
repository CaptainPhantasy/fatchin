#!/usr/bin/env bash
# Idempotent user-level install. Never runs as root, never starts a service,
# and never writes a real secret: it creates placeholder env files with mode 600.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="$HOME/.config/gha-worker"
SYSTEMD_DIR="$HOME/.config/systemd/user"

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 is required" >&2; exit 1; }
}

echo "== FatChin install =="
need python3
need git
need gh
if ! command -v bwrap >/dev/null 2>&1; then
  echo "WARNING: bubblewrap (bwrap) is missing. Commands and checks will be refused," >&2
  echo "         not run unconfined. Install it before running jobs." >&2
fi
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "ERROR: Python 3.11 or newer is required" >&2; exit 1; }

echo "installing the package from $ROOT"
python3 -m pip install --user -e "$ROOT"

mkdir -p "$CONFIG_DIR" "$SYSTEMD_DIR"

if [ ! -e "$CONFIG_DIR/config.toml" ]; then
  python3 -m ghagent.cli init-config --path "$CONFIG_DIR/config.toml" 2>/dev/null || \
    fatchin init-config --path "$CONFIG_DIR/config.toml"
else
  echo "config already present, left untouched: $CONFIG_DIR/config.toml"
fi

if [ ! -e "$CONFIG_DIR/env" ]; then
  cat > "$CONFIG_DIR/env" <<'EOF'
# FatChin environment. Permission 600. Placeholders only.
# Model endpoint key (auth_mode = "env"):
# GHA_MODEL_API_KEY=PASTE_MODEL_KEY_HERE
# GitHub: prefer `gh auth login`. If you cannot, set:
# GH_TOKEN=PASTE_GITHUB_TOKEN_HERE
# Optional lease broker (auth_mode = "broker-lease"):
# MCP_SECRET_BROKER_ADMIN_TOKEN=PASTE_BROKER_TOKEN_HERE
EOF
  chmod 600 "$CONFIG_DIR/env"
  echo "wrote placeholder env file: $CONFIG_DIR/env"
fi

if [ ! -e "$CONFIG_DIR/gui.env" ]; then
  cat > "$CONFIG_DIR/gui.env" <<'EOF'
# Set a long random value before binding the web interface beyond loopback.
# FATCHIN_ADMIN_TOKEN=PASTE_LONG_RANDOM_TOKEN_HERE
EOF
  chmod 600 "$CONFIG_DIR/gui.env"
  echo "wrote web token template: $CONFIG_DIR/gui.env"
fi

for unit in gha-worker.service gha-worker-gui.service; do
  dest="$SYSTEMD_DIR/$unit"
  if [ -e "$dest" ] && ! cmp -s "$ROOT/deploy/$unit" "$dest"; then
    backup="$dest.bak-$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p "$dest" "$backup"
    echo "backed up existing unit: $backup"
  fi
  cp "$ROOT/deploy/$unit" "$dest"
  echo "installed unit: $dest"
done

systemctl --user daemon-reload
systemctl --user enable gha-worker.service gha-worker-gui.service

cat <<EOF

== install complete ==
1. edit the config:   $CONFIG_DIR/config.toml
2. fill credentials:  $CONFIG_DIR/env   (chmod 600)
3. start the worker:  systemctl --user start gha-worker
4. open the browser:  http://127.0.0.1:18465  (after starting gha-worker-gui)

Before pointing it at real repositories, dispatch one fleet in Dry run and read
the recorded actions. See README.md.
EOF
