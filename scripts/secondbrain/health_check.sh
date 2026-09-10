#!/usr/bin/env bash
# Liveliness + failure watchdog (runs every 10 min, no-agent).
# Prints nothing when healthy -> Hermes stays silent. Output = alert/recovery.
export PATH="/snap/bin:/usr/local/bin:/usr/bin:/bin:${PATH}"
cd /opt/second-brain && exec /opt/second-brain/.venv/bin/python -m secondbrain.cli health check