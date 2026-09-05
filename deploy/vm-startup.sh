#!/usr/bin/env bash
# ==============================================================================
# GCE startup script — runs as root on first boot.
# Installs system packages for the Second Brain Agent and creates /opt layout.
# The app code, venv, and systemd service are installed by deploy-vm.sh.
# ==============================================================================

set -e
exec > >(tee /var/log/secondbrain-provision.log) 2>&1

echo "[secondbrain] Provisioning..." 
apt-get update -y
apt-get install -y python3 python3-venv python3-pip

mkdir -p /opt/second-brain/data

# Sentinel the deploy script waits for
touch /opt/second-brain/.provisioned
echo "[secondbrain] Provisioning complete."