#!/usr/bin/env bash
# ==============================================================================
# Push a new version of the Second Brain Agent to an existing GCE VM.
# Data in /opt/second-brain/data is preserved.
# ==============================================================================

set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get project 2>/dev/null)}"
ZONE="${GCP_ZONE:-us-central1-a}"
INSTANCE_NAME="${GCE_INSTANCE_NAME:-second-brain-agent}"

if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
    echo "ERROR: No GCP project set. Run: gcloud config set project <PROJECT_ID>"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
TARBALL="/tmp/secondbrain-deploy.tar.gz"

cd "${PROJECT_DIR}"
tar -czf "${TARBALL}" \
    --exclude='data/*' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='bot.log' \
    --exclude='bot_output.log' \
    --exclude='bot_pid.txt' \
    --exclude='.pytest_cache' \
    --exclude='tests' \
    --exclude='.venv' \
    .

gcloud compute scp "${TARBALL}" "${INSTANCE_NAME}:/tmp/" \
    --project="${PROJECT_ID}" --zone="${ZONE}" --quiet

gcloud compute ssh "${INSTANCE_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" --quiet -- \
    "sudo tar -xzf /tmp/secondbrain-deploy.tar.gz -C /opt/second-brain &&
     sudo chown -R root:root /opt/second-brain &&
     # Hermes Agent (second brain's future front-end) runs as this unprivileged
     # user; give it write access to the SQLite data so its CLI can mutate.
     sudo chown -R auliawicaksono:root /opt/second-brain/data &&
     sudo /opt/second-brain/.venv/bin/pip install -r /opt/second-brain/requirements.txt -q &&
     sudo systemctl restart second-brain &&
     rm -f /tmp/secondbrain-deploy.tar.gz"

echo "✅ Update pushed. Bot restarted."
gcloud compute ssh "${INSTANCE_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" --quiet -- \
    "journalctl -u second-brain -n 5 --no-pager | tail -5"