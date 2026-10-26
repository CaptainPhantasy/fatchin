#!/usr/bin/env bash
# uninstall.sh — stop and disable the gha-worker user unit, remove the unit
# copy. Deliberately KEEPS config, env file, and worker state; prints them
# and the manual removal commands.
set -euo pipefail

CONFIG_DIR="$HOME/.config/gha-worker"
ENV_FILE="$CONFIG_DIR/env"
SYSTEMD_DIR="$HOME/.config/systemd/user"
UNIT_NAME="gha-worker.service"
UNIT_DST="$SYSTEMD_DIR/$UNIT_NAME"

echo "== gha-worker uninstall =="

if systemctl --user list-unit-files "$UNIT_NAME" --no-legend 2>/dev/null | grep -q .; then
  echo "stopping and disabling $UNIT_NAME ..."
  systemctl --user disable --now "$UNIT_NAME" || true
else
  echo "unit not installed; nothing to stop or disable"
fi

if [ -e "$UNIT_DST" ]; then
  rm -f "$UNIT_DST"
  echo "removed unit copy: $UNIT_DST"
else
  echo "no unit copy at $UNIT_DST"
fi

systemctl --user daemon-reload 2>/dev/null || true

echo
echo "== deliberately KEPT (your data; remove by hand if you mean it) =="
echo "  config:   $CONFIG_DIR/config.toml"
echo "  env file: $ENV_FILE          (contains secrets; mode 600)"
if [ -f "$CONFIG_DIR/config.toml" ]; then
  state_dir="$(sed -n 's/^state_dir[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' "$CONFIG_DIR/config.toml" | head -1)"
  if [ -n "${state_dir:-}" ]; then
    echo "  state:    $state_dir        (jobs.json, watermarks.json, health.json, worker.lock)"
  fi
fi
echo
echo "== manual removal commands (only if you are sure) =="
echo "  rm \"$CONFIG_DIR/config.toml\""
echo "  rm \"$ENV_FILE\""
echo "  rm -rf \"\$HOME/.config/gha-worker\""
echo "  rm -rf \"<state_dir from config.toml>\""
echo "  rm -rf \"<worktree_root from config.toml>\""
