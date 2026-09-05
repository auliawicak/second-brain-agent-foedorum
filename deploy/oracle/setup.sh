#!/usr/bin/env bash
# ==============================================================================
# Oracle Cloud Always Free (Ampere A1 / Ubuntu) — app provisioner.
# Run as root on the VM:   sudo bash /tmp/oracle-setup.sh
#
# Installs Python + venv + dependencies and a systemd service for the
# Second Brain Agent. Code, data, and .env are expected at /opt/second-brain
# (uploaded by deploy-oracle.sh before this runs).
# ==============================================================================

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/second-brain}"

echo "[secondbrain] Installing system packages..."
apt-get update -y
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip

cd "${APP_DIR}"

echo "[secondbrain] Creating virtualenv..."
rm -rf .venv
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q

echo "[secondbrain] Installing Python dependencies..."
.venv/bin/pip install -r requirements.txt -q

echo "[secondbrain] Ensuring data directory..."
mkdir -p data

echo "[secondbrain] Writing systemd unit..."
cat > /etc/systemd/system/second-brain.service <<EOF
[Unit]
Description=Second Brain Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python -u main.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable second-brain
systemctl restart second-brain

echo "[secondbrain] Service started. Follow logs with:"
echo "  journalctl -fu second-brain"