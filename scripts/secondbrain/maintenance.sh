#!/usr/bin/env bash
# Nightly maintenance: retention prune + Markdown vault export + GCS backup.
# NOTE: cron workers run with a stripped PATH, and gcloud lives at /snap/bin.
export PATH="/snap/bin:/usr/local/bin:/usr/bin:/bin:${PATH}"
cd /opt/second-brain && exec /opt/second-brain/.venv/bin/python -m secondbrain.cli maintenance run