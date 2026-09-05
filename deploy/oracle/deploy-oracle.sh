#!/usr/bin/env bash
# ==============================================================================
# Deploy Second Brain Agent to Oracle Cloud Always Free (Ampere A1, Ubuntu)
# ==============================================================================
#
# Prerequisites:
#   1. Oracle VM created (Ampere A1, Ubuntu) + SSH private key saved on this Mac
#   2. Set connection info:
#        export ORACLE_HOST='ubuntu@<PUBLIC_IP>'
#        export ORACLE_SSH='ssh -i ~/.ssh/oracle_secondbrain'
#      (skip ORACLE_SSH if launching an instance named 'secondbrain' copies the
#       key to the default location, e.g. ~/.ssh/id_ed25519)
#   3. STOP the local bot first — two bots polling one token conflict:
#        kill "$(cat bot_pid.txt)" 2>/dev/null || true
#   4. Run:  ./deploy/oracle/deploy-oracle.sh
# ==============================================================================
# Uploads code + data + .env, provisions the VM, and starts the bot under
# systemd. SQLite lives on the VM boot volume (/opt/second-brain/data) so it
# survives restarts.
# ==============================================================================

set -euo pipefail

ORACLE_HOST="${ORACLE_HOST:?Set ORACLE_HOST, e.g. export ORACLE_HOST='ubuntu@150.136.12.34'}"
ORACLE_SSH="${ORACLE_SSH:-ssh}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

echo "============================================================"
echo "🧠 Deploying Second Brain Agent to Oracle Cloud Always Free"
echo "============================================================"
echo "Host:      ${ORACLE_HOST}"
echo "SSH:       ${ORACLE_SSH}"
echo "Project:   ${PROJECT_DIR}"
echo "============================================================"

echo ""
echo "⏳ (1/4) Creating /opt/second-brain on VM..."
${ORACLE_SSH} "${ORACLE_HOST}" \
    "sudo mkdir -p /opt/second-brain/data && sudo chown -R \$(whoami) /opt/second-brain"

echo "📦 (2/4) Uploading code, data, and .env..."
rsync -az \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='bot.log' \
    --exclude='bot_output.log' \
    --exclude='bot_pid.txt' \
    --exclude='tests' \
    --exclude='.pytest_cache' \
    --exclude='.DS_Store' \
    --exclude='deploy' \
    "${PROJECT_DIR}/" \
    "${ORACLE_HOST}:/opt/second-brain/"

echo "🚀 (3/4) Running provisioner on the VM..."
rsync -az "${SCRIPT_DIR}/setup.sh" "${ORACLE_HOST}:/tmp/oracle-setup.sh"
${ORACLE_SSH} "${ORACLE_HOST}" "sudo bash /tmp/oracle-setup.sh"

echo "✅ (4/4) Verifying service..."
sleep 8
${ORACLE_SSH} "${ORACLE_HOST}" \
    "systemctl is-active second-brain && journalctl -u second-brain -n 25 --no-pager | tail -25"

echo ""
echo "============================================================"
echo "✅ Deployment complete!"
echo ""
echo "  # Follow logs"
echo "  ${ORACLE_SSH} ${ORACLE_HOST} 'journalctl -fu second-brain'"
echo ""
echo "  # Restart the bot"
echo "  ${ORACLE_SSH} ${ORACLE_HOST} 'sudo systemctl restart second-brain'"
echo "============================================================"