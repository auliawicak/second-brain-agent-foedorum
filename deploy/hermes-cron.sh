#!/usr/bin/env bash
# ==============================================================================
# Port the Second Brain's 8 APScheduler jobs to Hermes cron.
#
# Run ON the VM as the Hermes user (auliawicaksono). Each job uses the
# second-brain skill + CLI against /opt/second-brain, delivered to Telegram.
#
#   ./hermes-cron.sh                 # create any missing jobs
#   ./hermes-cron.sh --resume        # ALSO resume reminder/condition jobs
#                                    # (do this ONLY after the old bot is stopped)
#   ./hermes-cron.sh --list          # show current jobs
#
# Guardian: the port BLOCKS until TELEGRAM_BOT_TOKEN is set in ~/.hermes/.env,
# because Hermes validates the telegram connection when creating --deliver
# telegram jobs. Fill it in, then re-run (idempotent).
# ==============================================================================

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
HERMES_SCRIPTS="$HOME/.hermes/scripts/secondbrain"
JOB_PREFIX="secondbrain-"

mkdir -p "$HERMES_SCRIPTS"

# ── no-agent wrapper scripts (pure DB jobs, zero model cost) ───────────────
cat > "$HERMES_SCRIPTS/conditions.sh" <<'SH'
#!/usr/bin/env bash
cd /opt/second-brain && exec /opt/second-brain/.venv/bin/python -m secondbrain.cli conditions check
SH
cat > "$HERMES_SCRIPTS/reminders.sh" <<'SH'
#!/usr/bin/env bash
cd /opt/second-brain && exec /opt/second-brain/.venv/bin/python -m secondbrain.cli reminders fire-due
SH
cat > "$HERMES_SCRIPTS/maintenance.sh" <<'SH'
#!/usr/bin/env bash
cd /opt/second-brain && exec /opt/second-brain/.venv/bin/python -m secondbrain.cli maintenance run
SH
chmod +x "$HERMES_SCRIPTS"/*.sh

if ! grep -qE "^TELEGRAM_BOT_TOKEN=.+" "$HOME/.hermes/.env"; then
    echo "⚠️  TELEGRAM_BOT_TOKEN is not set in ~/.hermes/.env."
    echo "    Bootstrapped the script wrappers only. Set the token, then re-run."
    exit 0
fi

mkdir -p "$HOME/.hermes/cron" 2>/dev/null || true

create_if_missing() {
    local name="$1"; shift
    if hermes cron list 2>/dev/null | grep -q "  ${JOB_PREFIX}${name} "; then
        echo "  ~ ${JOB_PREFIX}${name} already exists"
        return
    fi
    hermes cron create --name "${JOB_PREFIX}${name}" "$@"
    echo "  + ${JOB_PREFIX}${name}"
}

echo "▶ Creating Hermes cron jobs (deliver: telegram, skill: second-brain):"

# Agent-driven jobs — the Hermes model reads data via the CLI and composes.
create_if_missing morning-brief \
    --deliver telegram --skill second-brain --workdir /opt/second-brain \
    "0 6 * * *" \
    "Morning brief: read the agenda (secondbrain agenda), today's reminders (secondbrain reminders list), pending tasks, and an overdue check. Compose ONE warm, terse brief in the persona's voice and send it."
create_if_missing evening-closeout \
    --deliver telegram --skill second-brain --workdir /opt/second-brain \
    "0 21 * * *" \
    "Evening check-in: run secondbrain tasks day-stats. Compose a short reflective note of what got done today and invite the user to tell you what to remember for tomorrow."
create_if_missing weekly-review \
    --deliver telegram --skill second-brain --workdir /opt/second-brain \
    "0 17 * * 5" \
    "Weekly review (Friday): pull recent notes (secondbrain notes recent 10), done tasks (secondbrain tasks list --status done), and this week's corrections (secondbrain corrections list-today). Compose a concise completed-vs-slipped summary with one observed pattern."
create_if_missing nightly-consolidation \
    --deliver telegram --skill second-brain --workdir /opt/second-brain \
    "15 0 * * *" \
    "Nightly consolidation: read today's corrections with secondbrain corrections list-today, merge those that are stable facts using secondbrain facts remember (supersede as needed). Reply with a one-line summary of what changed."
create_if_missing persona-proposal \
    --deliver telegram --skill second-brain --workdir /opt/second-brain \
    "0 4 1 * *" \
    "Monthly operating-principles proposal: review recent corrections and preferences. Propose 1-3 updated operating principles for the user's approval, ask if they want them applied via persona set."

# Script jobs — pure DB mechanics, no model inference.
create_if_missing condition-checks \
    --deliver telegram --no-agent \
    --script secondbrain/conditions.sh --workdir /opt/second-brain \
    "*/15 * * * *"
create_if_missing night-maintenance \
    --deliver telegram --no-agent \
    --script secondbrain/maintenance.sh --workdir /opt/second-brain \
    "0 3 * * *"
create_if_missing reminder-check \
    --deliver telegram --no-agent \
    --script secondbrain/reminders.sh --workdir /opt/second-brain \
    "* * * * *"

# These two would DOUBLE-deliver while the old bot is still running its own
# scheduler — pause until the switchover, then --resume.
if [[ "${1:-}" == "--resume" ]]; then
    echo "▶ Resuming reminder/condition jobs (old bot must already be stopped)."
    hermes cron resume "${JOB_PREFIX}reminder-check" || true
    hermes cron resume "${JOB_PREFIX}condition-checks" || true
else
    echo "▶ Pausing reminder-check + condition-checks (old bot still owns them)."
    hermes cron pause "${JOB_PREFIX}reminder-check" || true
    hermes cron pause "${JOB_PREFIX}condition-checks" || true
fi

if [[ "${1:-}" == "--list" ]]; then
    hermes cron list
fi
echo "✅ Done."