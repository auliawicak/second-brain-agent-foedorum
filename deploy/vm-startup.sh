#!/usr/bin/env bash
# ==============================================================================
# GCE startup script — runs as root on first boot.
# Installs system packages for the Second Brain Agent, creates /opt layout,
# and configures a 1 GB swapfile (idempotent).
# The app code, venv, and systemd service are installed by deploy-vm.sh.
# ==============================================================================

set -e
exec > >(tee /var/log/secondbrain-provision.log) 2>&1

echo "[secondbrain] Provisioning..." 
apt-get update -y
apt-get install -y python3 python3-venv python3-pip

mkdir -p /opt/second-brain/data

# ── Swap (1 GB, idempotent) ────────────────────────────────────────────────
if [ ! -f /swapfile ]; then
  echo "[secondbrain] Creating 1 GB swapfile..."
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -w vm.swappiness=10
grep -q 'vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf

# Sentinel the deploy script waits for
touch /opt/second-brain/.provisioned
echo "[secondbrain] Provisioning complete."