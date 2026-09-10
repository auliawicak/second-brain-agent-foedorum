# Second Brain Agent 🧠

A 24/7 personal AI assistant that lives in Telegram. It manages tasks, captures notes, sets reminders, curates news, reviews your week, and answers from your persistent memory — all on the **free tier** ($0).

It is built on **[opencode Hermes](https://opencode.ai)** (the messaging gateway that owns the Telegram bot) talking to a **local OpenAI-compatible model proxy** (`gateway/openai_proxy.py`, port `18080`) that relays every request to **OpenCode Zen free models** via the Responses API. A "second brain" CLI reads and writes an on-machine SQLite database, and Heres' job scheduler drives the daily/weekly briefs, reminders, condition checks, and maintenance.

> Everything is private and single-user: no inbound ports, token-gated dashboard, secrets in `.env`, data in local SQLite files you own.

---

## Table of Contents

- [How It Works](#how-it-works)
- [What It Does](#what-it-does)
- [Architecture Map](#architecture-map)
- [The Model Pick — free tier](#the-model-pick--free-tier)
  - [Text-fold workaround](#text-fold-workaround)
  - [Vision (images)](#vision-images)
  - [Voice (STT)](#voice-stt)
- [Scheduled Jobs](#scheduled-jobs)
- [Backups & Maintenance](#backups--maintenance)
- [Web Dashboard](#web-dashboard)
- [Data & Storage](#data--storage)
- [Project Structure](#project-structure)
- [Deployment (Google Cloud free tier)](#deployment-google-cloud-free-tier)
- [Setup from Scratch](#setup-from-scratch)
- [Operations](#operations)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Status & Roadmap](#status--roadmap)
- [Legacy note](#legacy-note)

---

## How It Works

1. **Hermes gateway** long-polls Telegram (outbound only; no webhook, no public port). Inbound text → agent turn; photos → aux vision; voice → local Whisper. It also runs the **8 cron jobs** below.
2. The agent uses the **`second-brain` skill** (`skills/second-brain/SKILL.md`) which teaches it to run the `secondbrain` CLI for every read/write — it never invents data.
3. Every model call goes to the **model proxy** (`http://127.0.0.1:18080`, model id `secondbrain-pool`), a thin OpenAI-compatible `/v1/chat/completions` server that:
   - opens a **thread session** (`x-opencode-session`) and calls the **Zen Responses API**;
   - **folds tool results into user text** (free-tier quirk: `function_call_output` is rejected, so Hermes' native tool-result envelope can't pass through);
   - tagged **image input** turns module the vision parts and forces `reasoning: {"effort": "minimal"}`.
4. Tool calls run against `/opt/second-brain/data/second_brain.db`.

```
Telegram ─► hermes-gateway (user systemd unit)
              │  config.yaml  model.provider=custom base_url=http://127.0.0.1:18080
              ▼
        model-proxy.service  (:18080) ──► OpenCode Zen ─ freelance Responses API
              │  text-fold + x-opencode-session + image relay
              ▼
        secondbrain CLI ──► SQLite (tasks/notes/reminders/preferences/facts/persona/news)
              │
              ▼
        GCS backup bucket (nightly)   +   dashboard (localhost:8765, token-gated)
```

## What It Does

- **"Add milk to the grocery list"** → `tasks add`
- **"Save this idea: solar-powered EV charging"** → `notes add` (tagged, FTS-searchable)
- **"Remind me tomorrow at 9am to call the dentist"** → `reminders set` (one-shot or recurring cron; the user keeps five daily prayer reminders — the skill treats them as sacred)
- **"What's the news today?"** → `news` → curated morning brief
- **"I prefer black coffee"** → `prefs save` / `facts remember` (injected into every future turn)
- **"Never schedule cardio in the evening"** → `corrections add` (durable, applied going forward)
- **Edit/delete anything** → `tasks update|delete`, `notes update|delete`
- **Persona** → `persona show/history/set/rollback` (voice & principles stored in the DB, editable at runtime)
- **What's on my plate / do I still have my Asr reminder? / did I note the primers?** → read commands + blunt yes/no answers

## Architecture Map

| Component | What it is |
|---|---|
| `hermes-gateway` (user unit) | **OpenCode Hermes** agent gateway; owns the bot `@foedorum_hermes_bot`, long-polls Telegram, runs cron, does STT/vision enrichment via aux lanes. Live config: `~/.hermes/config.yaml`, env `~/.hermes/.env` |
| `model-proxy.service` | `gateway/openai_proxy.py` — local OpenAI-compatible pool on `127.0.0.1:18080`, model id `secondbrain-pool`; the only model endpoint Hermes sees |
| Model backend | **OpenCode Zen free tier** (Responses API): `FAST_MODEL` / `DEEP_MODEL` pinned to `muse-spark-1.3-contributor-free` (the only free model passing the golden tool-set, 93%) |
| `secondbrain` CLI | `secondbrain/cli.py` + `agent/tools.py` — the agent's tool surface over SQLite |
| Scheduler | Hermes cron (8 jobs) in `~/.hermes/hermes-agent` config; job logic + schedules mirrored in `services/scheduler.py` |
| Scripted jobs | no-agent cron: `scripts/*.sh` → `reminders.sh`, `conditions.sh`, `maintenance.sh` |
| Backup | Nightly `maintenance.sh` → `gs://secondbrain-507714-backups` (60-day lifecycle) |
| Dashboard | `services/dashboard.py` — read-only, `127.0.0.1:8765`, bearer token, SSH tunnel (`scripts/dashboard-tunnel.sh`) |
| Legacy bot | old `main.py`/`bot/` stack kept for reference; **`second-brain.service` must stay disabled** (same-token conflict with Hermes) |

### The Model Pick — free tier

- Pool model: `secondbrain-pool` → Zen **Responses** API. `chat/completions` on Zen returns HTTP 500 for these models, so the proxy translates.
- **Text-fold**: Zen free rejects `function_call_output`, so Hermes' native tool-result envelope can't cross. The proxy folds tool results into synthetic user `<tool_result>` text and the assistant always carries a `content` list. This is why image tool-results also can't pass natively.
- **Vision (images)**: pool vision relay supports `input_image`; free model tested: **`muse-spark-1.3-contributor-free`** ("The image is solid blue" — correct). Hermes `auxiliary.vision` → `custom` provider → pool → description injected as text. (Other free models 500 on images.)
- **Voice (STT)**: **Groq Whisper** (`whisper-large-v3-turbo`) as the primary provider — free tier (2,000 req/day, 28.8k audio-sec/day), key `GROQ_API_KEY` in both `.env` files; `stt.provider: groq`. Local **`faster-whisper`** (`base`, CPU int8) stays installed as the passive fallback when the cloud key is unavailable.

## Scheduled Jobs

Hermes cron (UTC; VM local time). Jakarta = UTC+7. Schedules mirrored in `services/scheduler.py`.

| Job | Schedule (UTC) | Jakarta | Type |
|---|---|---|---|
| morning-brief | 0 23 * * * | 06:00 | agent, skill `second-brain` |
| evening-closeout | 0 14 * * * | 21:00 | agent, skill |
| nightly-consolidation | 15 17 * * * | 00:15 | agent, skill |
| weekly-review | 0 10 * * 5 | Fri 17:00 | agent, skill |
| persona-proposal | 0 21 L * * | 1st, 04:00 | agent, skill |
| reminders-fire | `* * * * *` | every minute | script `secondbrain/reminders.sh` (no agent) |
| conditions-check | `*/15 * * * *` | every 15 min | script `secondbrain/conditions.sh` (no agent) |
| nightly-maintenance | 0 20 * * * | 03:00 | script `secondbrain/maintenance.sh` (no agent) |

Deliveries go to `telegram:8481919074` (your allowlisted channel). Scripted jobs log `[SILENT] — skipping delivery` when there's nothing to say.

## Backups & Maintenance

- Nightly `maintenance.sh` runs as a Hermes cron (no-agent): retention prune, Markdown vault export, then **GCS upload** of `data/second_brain.db.gz` and `data/export/<date>.tar.gz` to `gs://secondbrain-507714-backups`.
- Bucket: `secondbrain-507714-backups` (us-central1, STANDARD) with a **60-day deletion lifecycle**. Service account `111024359843-compute@...` has bucket-scoped `roles/storage.objectAdmin`.
- Manual: `gsutil ls gs://secondbrain-507714-backups/db/`.

## Web Dashboard

Read-only view of the DB (tasks, notes, reminders, preferences, model usage, digests) on `http://127.0.0.1:8765`:

- `services/dashboard.py` (stdlib only), systemd unit `secondbrain-dashboard.service`, token from `.env` `DASHBOARD_TOKEN`.
- Endpoints: `/api/overview`, `/api/tasks`, `/api/notes`, `/api/reminders`, `/api/preferences`, `/api/model`, `/api/conversations?limit=N`, plus `/healthz`.
- Access: `bash scripts/dashboard-tunnel.sh` then open `http://127.0.0.1:8765/?token=<DASHBOARD_TOKEN>`.

## Data & Storage

| File | Contents |
|---|---|
| `data/second_brain.db` | tasks, notes (FTS5), reminders, conversations, daily digests, feedback, preferences (FTS5), corrections, persona (+versions), nudge_log, model_usage/health/heartbeat (WAL mode) |
| `data/scheduler_jobs.db` | (legacy APScheduler store; superseded by Hermes cron) |
| `data/export/` | nightly Markdown vault export |
| `data/tmp_backup/` | maintenance scratch |

## Project Structure

```
/opt/second-brain/
├── .env                    # secrets (git-ignored): bot token, Zen key, TIMEZONE, BACKUP_BUCKET, DASHBOARD_TOKEN
├── .env.example
├── README.md
├── main.py, config.py, bot/, tests/        # LEGACY second-brain.service stack (disabled; reference only)
├── agent/
│   ├── providers.py        # Zen Responses relay: text-fold, x-opencode-session, image relay
│   ├── tools.py            # secondbrain CLI tool wrappers (add/list/update/delete/…)
│   └── … (legacy router/health/registry from the old stack)
├── gateway/
│   └── openai_proxy.py     # model-proxy.service — OpenAI-compatible pool → Zen (:18080)
├── secondbrain/
│   └── cli.py              # the CLI the agent actually calls (tasks/notes/reminders/prefs/facts/corrections/persona/news)
├── services/
│   ├── scheduler.py        # mirror of the 8 Hermes cron jobs
│   ├── dashboard.py        # read-only dashboard (127.0.0.1:8765)
│   └── secondbrain-dashboard.service
├── scripts/
│   ├── dashboard-tunnel.sh # SSH tunnel helper
│   └── secondbrain/{reminders,conditions,maintenance}.sh   # no-agent cron scripts (deployed to ~/.hermes/scripts)
├── skills/second-brain/SKILL.md   # agent skill (mirrored to ~/.hermes/skills/)
├── docs/model_matrix.md    # model pool test evidence
└── deploy/                 # legacy VM deploy scripts
```

## Deployment (Google Cloud free tier)

Production runs on an **e2-micro** VM (always-free tier, project `secondbrain-507714`, zone `us-central1-a`) with a 30 GB boot disk. Bare-metal systemd; no Docker on the VM.

| Service | Unit | Notes |
|---|---|---|
| Model proxy | `model-proxy.service` | :18080 pool → Zen |
| Messaging | `hermes-gateway.service` (user) | Hermes agent + cron |
| Dashboard | `secondbrain-dashboard.service` | read-only, localhost |
| Legacy bot | `second-brain.service` | **disabled** (token conflict) |

```bash
# Vitals
gcloud compute ssh second-brain-agent --zone=us-central1-a -- systemctl status model-proxy secondbrain-dashboard
gcloud compute ssh second-brain-agent --zone=us-central1-a -- systemctl --user status hermes-gateway

# Dashboard tunnel
bash scripts/dashboard-tunnel.sh          # then http://127.0.0.1:8765/?token=…

# Cron view / receipts
gcloud compute ssh second-brain-agent --zone=us-central1-a -- hermes cron list
gcloud compute ssh second-brain-agent --zone=us-central1-a -- tail -50 ~/.hermes/logs/gateway.log
```

## Setup from Scratch

1. **Telegram**: create the bot with @BotFather, allowlist your user id (`TELEGRAM_USER_ID`) in `~/.hermes/.env`.
2. **Zen key**: free at [opencode.ai](https://opencode.ai) → `OPENCODE_ZEN_API_KEY` in `~/.hermes/.env` and `/opt/second-brain/.env`.
3. **Model proxy** (run as root or the deploy user):
   ```
   sudo systemctl enable --now model-proxy.service     # :18080 pool → Zen
   ```
4. **Hermes** (`~/.hermes/config.yaml`): `model.default: secondbrain-pool`, `provider: custom`, `base_url: http://127.0.0.1:18080`, `max_tokens: 4096`, `context_length: 128000`; `stt.enabled: true`, `stt.local.model: base`; `auxiliary.vision: {provider: custom, model: secondbrain-pool}`.
5. **Skill**: copy `skills/second-brain/SKILL.md` → `~/.hermes/skills/second-brain/SKILL.md`.
6. **Cron**: recreate the 8 jobs from the table (`hermes cron`); script jobs reference `~/.hermes/scripts/secondbrain/{reminders,conditions,maintenance}.sh`.
7. **STT**: `stt.provider: groq` (primary, `whisper-large-v3-turbo`); `GROQ_API_KEY` in `.env`; `faster-whisper` installed in the Hermes venv as local fallback.
8. **Backups**: set `BACKUP_BUCKET`, grant the compute SA bucket `roles/storage.objectAdmin`, add a 60-day lifecycle rule.
9. **Dashboard**: `DASHBOARD_TOKEN` in `.env`; enable `secondbrain-dashboard.service`.

## Operations

- **Update code**: commit on the VM (`cd /opt/second-brain && sudo git …`), restart affected services.
- **Logs**: `~/.hermes/logs/{agent,gateway}.log`; delivery receipts (e.g. `Job '52009a987088': delivered to telegram:8481919074 … message_id=47`).
- **e2-micro is burstable**: occasional slowdowns under load are expected, not a bug.
- **Voice latency**: first transcription pays ~11s model load; subsequent ones reuse the cached singleton (idle-unloaded after a timeout).

## Security

- Single-user: allowlist `TELEGRAM_USER_ID`; strangers rejected.
- Secrets only in `.env` files (git-ignored; never in commits).
- Dashboard bound to `127.0.0.1`, bearer-token auth, reachable only over SSH tunnel. No inbound ports on the VM.
- Zen key rotatable; proxy is localhost-bound.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Conflict: terminated by other getUpdates" | The legacy `second-brain.service` is polling the same token — keep it **disabled**. |
| Hero model 500s / empty outputs | Zen free tier: proxy must use Responses API + `x-opencode-session`; check `gateway/openai_proxy.py` and `model-proxy.service`. |
| Tool results missing on the free tier | Text-fold path required; do not switch Hermes back to native tool-result relay. |
| Images don't reach the model | Aux vision (custom provider → pool) required; native image tool-results are folded/destroyed by text-fold. |
| Voice notes fail | Check `GROQ_API_KEY` in both `.env` files and `stt.provider: groq` (fallback: `faster-whisper` importable in the Hermes venv, `stt.local.model: base`). Restart `hermes-gateway` after changing either. |
| Dashboard 401 | Wrong/missing `DASHBOARD_TOKEN`. |
| No backups | Check `BACKUP_BUCKET`, SA `roles/storage.objectAdmin`, and `maintenance.sh` output. |

## Status & Roadmap

- [x] Backups / Markdown vault export → GCS (nightly maintenance)
- [x] Learning loop (corrections → consolidated preferences → weekly review)
- [x] Persona as data (`/persona`, monthly proposal)
- [x] Voice-note input (local Whisper) and image input (Zen muse vision via pool)
- [x] Web dashboard (read-only, localhost + token)
- [x] Note/task editing (update/delete CLI + skill docs)
- [x] 8 Hermes cron jobs live and verified end-to-end (real delivery receipts)
- [ ] Multi-provider GA (add a second provider key — Groq/Gemini — to reach the ≥3-provider bar in `docs/model_matrix.md`)
- [ ] Webhook mode for non-VM hosts (optional; polling needs no inbound port)
- [x] Voice-note STT via Groq Whisper free tier (`whisper-large-v3-turbo`, local fallback)

## Legacy note

`main.py`, `bot/`, `agent/{brain,router,health,registry,context,prompts,confirmation}.py`, `docker-compose.yml`, `deploy/`, and `tests/` belong to the **previous Gemini-era stack** (python-telegram-bot + APScheduler). They are kept for reference and are **not** wired into the live system — the Hermes gateway + model proxy superseded them.