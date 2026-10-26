#!/usr/bin/env bash
# One scoped, write-free fleet pass. Prints exactly what it would send.
#
# Usage: scripts/demo-dry-run.sh [owner/repository]
# Requires the GUI service and the worker supervisor to be running.
set -euo pipefail

STATE="${GHA_STATE:-$HOME/.local/state/gha-worker/state/fleet-v1/state.json}"
API="${GHA_API:-https://127.0.0.1:18465/api/fleet}"
HOST="${GHA_HOST:-fatchin.example.com:18465}"
OUTPUT="${GHA_OUTPUT:-$HOME/Documents/FatChin}"
REPO="${1:-example-owner/gha-worker}"
START="$(date -u +%FT%TZ)"

fleet_id() {
  curl -sk -H "Host: $HOST" "$API" -m 20 |
    python3 -c 'import sys,json; d=json.load(sys.stdin); f=d.get("fleets") or [{}]; print(f[0].get("id",""))'
}

echo "FatChin dry run — $REPO"
echo "mode: DRY RUN   agents: 2 GLM   budget: 8 calls"
echo

CURRENT="$(fleet_id)"
if [ -n "$CURRENT" ]; then
  curl -sk -X POST -H "Host: $HOST" -H 'Content-Type: application/json' \
    -m 30 -d "{\"action\":\"stop\",\"fleet_id\":\"$CURRENT\"}" "$API" >/dev/null
  sleep 6
fi

curl -sk -X POST -H "Host: $HOST" -H 'Content-Type: application/json' -m 60 \
  -d "{\"action\":\"start\",\"account\":\"floyd\",\"output_dir\":\"$OUTPUT\",\"workers\":2,\"deepseek_workers\":0,\"scope\":\"selected\",\"repos\":[\"$REPO\"],\"dry_run\":true,\"max_calls_total\":8}" \
  "$API" >/dev/null
echo "dispatched; other repositories are out of scope for this pass"
echo

for _ in $(seq 1 36); do
  python3 - "$STATE" "$REPO" "$START" <<'PY'
import json, sys
state, repo, start = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
fleet = list(state['fleets'].values())[0]
items = [i for i in state['items'].values()
         if i['repo'] == repo and (i.get('updated_at') or '') >= start]
status = [i['status'] for i in items]
print(f"[calls {fleet.get('calls_used', 0)}/{fleet.get('max_calls_total', 0)}] "
      f"queued {status.count('queued')}  running {status.count('running')}  "
      f"done {status.count('done')}")
for item in items:
    result = item.get('result') or {}
    actions = result.get('dry_run_actions') or []
    if item['status'] == 'done' and (item['kind'] in {'review_pr', 'review_code'}):
        findings = result.get('findings') or []
        head = (result.get('reviewed_head_sha') or item.get('head_sha') or '')[:10]
        print(f"reviewed {head}: {len(findings)} finding(s) — "
              + ("intended writes recorded" if actions else "nothing would be posted"))
    for action in actions[:3]:
        target = action.get('path') or action.get('branch') or action.get('repo') or ''
        print(f"  WOULD {str(action.get('kind')).upper():8} {target}")
PY
  if python3 -c "import json,sys; d=json.load(open('$STATE')); r='$REPO'; s='$START'; done=[i for i in d['items'].values() if i['repo']==r and i['kind'] in {'review_pr','review_code'} and i['status']=='done' and (i.get('updated_at') or '')>=s]; sys.exit(0 if done else 1)"; then
    echo
    echo "Dry run complete. No GitHub write was sent."
    CURRENT="$(fleet_id)"
    if [ -n "$CURRENT" ]; then
      curl -sk -X POST -H "Host: $HOST" -H 'Content-Type: application/json' \
        -m 30 -d "{\"action\":\"stop\",\"fleet_id\":\"$CURRENT\"}" "$API" >/dev/null
      echo "Fleet stopped."
    fi
    exit 0
  fi
  sleep 10
done

echo
echo "No review finished inside the demonstration window; the fleet stays in dry-run mode."
