#!/usr/bin/env bash
# ==============================================================================
# Deploy Second Brain Agent to GCE e2-micro (Free Tier) — Docker-less VM
# ==============================================================================
#
# Prerequisites:
#   1. gcloud installed & authenticated:   gcloud auth login
#   2. Active project with billing:        gcloud config set project <PROJECT_ID>
#   3. Run:                                ./deploy/deploy-vm.sh
#
# Creates an Ubuntu 24.04 VM, installs the app + venv, and runs it as a
# systemd service. The SQLite data lives under /opt/second-brain/data on the
# persistent 30GB boot disk, so it survives restarts and updates.
# ==============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get project 2>/dev/null)}"
ZONE="${GCP_ZONE:-us-central1-a}"   # e2-micro free tier zone
INSTANCE_NAME="${GCE_INSTANCE_NAME:-second-brain-agent}"
MACHINE_TYPE="e2-micro"
DISK_SIZE=30
IMAGE_FAMILY="ubuntu-2404-lts-amd64"
IMAGE_PROJECT="ubuntu-os-cloud"

if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
    echo "ERROR: No GCP project set. Run: gcloud config set project <PROJECT_ID>"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"
TARBALL="/tmp/secondbrain-deploy.tar.gz"

echo "============================================================"
echo "🧠 Deploying Second Brain Agent to GCE (Docker-less)"
echo "============================================================"
echo "Project:   ${PROJECT_ID}"
echo "Zone:      ${ZONE}"
echo "Instance:  ${INSTANCE_NAME}"
echo "Machine:   ${MACHINE_TYPE} (${DISK_SIZE}GB boot disk)"
echo "============================================================"

# ── Step 1: Create the VM (idempotent) ────────────────────────────────────────
echo ""
echo "🖥️ Step 1: Ensuring VM exists..."
if gcloud compute instances describe "${INSTANCE_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" >/dev/null 2>&1; then
    echo "Instance already exists — updating it."
else
    gcloud compute instances create "${INSTANCE_NAME}" \
        --project="${PROJECT_ID}" \
        --zone="${ZONE}" \
        --machine-type="${MACHINE_TYPE}" \
        --boot-disk-size="${DISK_SIZE}GB" \
        --boot-disk-type=pd-standard \
        --image-family="${IMAGE_FAMILY}" \
        --image-project="${IMAGE_PROJECT}" \
        --metadata-from-file=startup-script="${SCRIPT_DIR}/vm-startup.sh"
    echo "VM created."
fi

# ── Step 2: Wait for SSH + startup script to finish ───────────────────────────
echo ""
echo "⏳ Step 2: Waiting for VM startup..."
for i in $(seq 1 60); do
    if gcloud compute ssh "${INSTANCE_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" \
        --command="test -f /opt/second-brain/.provisioned && echo READY" --quiet 2>/dev/null | grep -q READY; then
        echo "VM ready."
        break
    fi
    if [[ "$i" == 60 ]]; then
        echo "ERROR: VM did not become ready in time. Check: gcloud compute ssh ${INSTANCE_NAME} --zone=${ZONE}"
        exit 1
    fi
    sleep 10
done

# ── Step 3: Upload project code ───────────────────────────────────────────────
echo ""
echo "📦 Step 3: Uploading project code..."
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
    --exclude='.workspace' \
    .

gcloud compute scp "${TARBALL}" "${INSTANCE_NAME}:/tmp/" \
    --project="${PROJECT_ID}" --zone="${ZONE}" --quiet

gcloud compute ssh "${INSTANCE_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" --quiet -- \
    "sudo mkdir -p /opt/second-brain &&
     sudo tar -xzf /tmp/secondbrain-deploy.tar.gz -C /opt/second-brain &&
     sudo chown -R root:root /opt/second-brain &&
     rm -f /tmp/secondbrain-deploy.tar.gz"

# ── Step 4: Install deps + systemd service ────────────────────────────────────
echo ""
echo "🐍 Step 4: Installing dependencies and starting service..."
gcloud compute ssh "${INSTANCE_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" --quiet -- \
    "cd /opt/second-brain &&
     sudo python3 -m venv .venv &&
     sudo .venv/bin/pip install --upgrade pip -q &&
     sudo .venv/bin/pip install -r requirements.txt -q &&
     sudo bash -c 'cat > /etc/systemd/system/second-brain.service <<EOF
[Unit]
Description=Second Brain Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/second-brain
ExecStart=/opt/second-brain/.venv/bin/python -u main.py
Restart=always
RestartSec=15
MemoryHigh=550M
MemoryMax=750M
OOMPolicy=stop
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF'
     sudo systemctl daemon-reload &&
     sudo systemctl enable second-brain &&
     sudo systemctl restart second-brain"

# ── Step 5: Verify ────────────────────────────────────────────────────────────
echo ""
echo "✅ Step 5: Verifying service..."
sleep 8
gcloud compute ssh "${INSTANCE_NAME}" --project="${PROJECT_ID}" --zone="${ZONE}" --quiet -- \
    "systemctl is-active second-brain && journalctl -u second-brain -n 8 --no-pager | tail -8"

echo ""
echo "============================================================"
echo "✅ Deployment complete!"
echo ""
echo "  # View logs"
echo "  gcloud compute ssh ${INSTANCE_NAME} --zone=${ZONE} -- 'journalctl -fu second-brain'"
echo ""
echo "  # Restart the bot"
echo "  gcloud compute ssh ${INSTANCE_NAME} --zone=${ZONE} -- 'sudo systemctl restart second-brain'"
echo ""
echo "  # Stop the VM (data stays on disk)"
echo "  gcloud compute instances stop ${INSTANCE_NAME} --zone=${ZONE}"
echo ""
echo "  # Delete everything"
echo "  gcloud compute instances delete ${INSTANCE_NAME} --zone=${ZONE}"
echo "============================================================"