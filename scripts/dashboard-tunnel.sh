#!/usr/bin/env bash
# SSH tunnel to the second-brain dashboard (bound to localhost on the VM).
# Usage: bash scripts/dashboard-tunnel.sh [local_port]
set -euo pipefail

LOCAL_PORT="${1:-8765}"
VM_HOST="${SECOND_BRAIN_VM:-second-brain-agent}"
REMOTE_PORT="${DASHBOARD_PORT:-8765}"

echo "Tunnel: http://127.0.0.1:${LOCAL_PORT} -> ${VM_HOST}:${REMOTE_PORT}"
echo "Open the URL with ?token=... from your DASHBOARD_TOKEN."
exec ssh -N -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" "${VM_HOST}"