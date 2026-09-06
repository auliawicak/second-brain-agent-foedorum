# Second Brain Agent 🧠

A 24/7 personal AI assistant that lives in Telegram. It manages your tasks, remembers your notes and ideas, curates your daily news, sets reminders, reviews your week, and thinks through complex problems — powered by an LLM with tool-calling over a **model pool** (OpenCode Zen by default, $0), with a versioned **persona stored as data** so its tone is editable at runtime.

---

## Table of Contents

- [What It Does](#what-it-does)
- [Features](#features)
- [How It Works](#how-it-works)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
  - [1. Telegram Bot Token](#1-telegram-bot-token)
  - [2. API Keys](#2-api-keys)
  - [3. Configuration](#3-configuration)
- [Running Locally](#running-locally)
- [Deploying to the Cloud](#deploying-to-the-cloud)
  - [Option A: Google Cloud (free tier)](#option-a-google-cloud-free-tier)
  - [Option B: Docker / docker-compose](#option-b-docker--docker-compose)
- [Usage](#usage)
  - [Commands](#commands)
  - [Natural Language](#natural-language)
- [Scheduled Jobs](#scheduled-jobs)
- [Data & Storage](#data--storage)
- [Security](#security)
- [Operations & Maintenance](#operations--maintenance)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [License](#license)

---

## What It Does

Chat with your own always-on assistant on Telegram:

- **"Add milk to the grocery list"** → creates a task
- **"Save this idea: solar-powered EV charging"** → stores a tagged note
- **"Remind me tomorrow at 9am to call the dentist"** → schedules a reminder (even recurring ones)
- **"What's the news today?"** → fetches and curates a news digest
- **"Think through the pros and cons of moving to microservices"** → runs deep reasoning
- **"I prefer black coffee"** → learns and remembers the fact forever

Everything is private, single-user, and your data lives in local SQLite files you control.

---

## Features

### ✅ Task Management
Natural-language task capture with priority inference, due dates, categories, listing, and completion.

### 📝 Notes & Second Brain
Save notes with auto-tagged categories and full-text search (SQLite FTS5).

### ⏰ Reminders
One-shot or recurring reminders (cron expressions) checked every minute. Supports relative times ("in 30 minutes", "tomorrow 9am").

### 📰 Curated News
Daily morning digest at 06:00 from NewsAPI (with Google News RSS fallback), AI-curated with summaries and "Why This Matters".

### 🧠 Deep Thinking
`/think` mode for complex analysis, research, and multi-step reasoning with a longer output budget.

### 💬 Conversational with Tools
Free-text chat where the model actively calls tools (tasks, notes, reminders, news, preferences) instead of just talking.

### 🧠 Persistent Memory
- Conversation history (batched for context)
- Learned preferences/habits injected into every conversation
- Corrections/feedback captured inline (👍/👎 buttons) and consolidated into the weekly review

### 🎭 Persona as Data
Voice, principles, and mode rules live in SQLite as versioned snapshots, not in code. `/persona set voice <text>` changes the next reply with **no restart**, and `/persona rollback <v>` flips back to any prior version. A monthly job proposes principle updates that you Approve/Reject in Telegram.

### 📆 Unified Briefs & Condition Triggers
- **Morning brief** (06:00): today's tasks, reminders, overdue items + one curated news digest
- **Evening close-out** (21:00): today's captures/plan for tomorrow
- **Weekly review** (Fri 17:00): Done vs Slipped + one improvement pattern
- **Condition checks** (every 15 min): nudges on untouched overdue tasks, capture/execute imbalance, and stale notes — deduplicated once/day

### 🚦 Model Pool & Health
Multiple free providers with circuit-breaker failover, per-model RPM/RPD budgets, and 401/502-eviction; `/status` shows each model's live state.

### 🔐 Single-User
Restricted to your Telegram user ID — unauthorized users are politely rejected.

### ☁️ 24/7 Ready
Runs as a systemd service on a free Google Cloud VM, or via Docker.

---

## How It Works

1. **Telegram Bot** (`python-telegram-bot`) receives a message (long-polling; no webhooks, no public port needed).
2. **Second Brain agent** (`agent/brain.py`) assembles a system prompt from a core persona + a **persona-data block read fresh from SQLite every turn** (Voice → Principles → Mode rules, §8) + learned preferences + tool descriptions.
3. The model may reply with a `{"tool": "...", "args": {...}}` JSON block; the agent executes it against SQLite and feeds the result back — up to 15 tool iterations per turn. A router picks the cheapest healthy model per tier with circuit-breaker failover.
4. **APScheduler** runs 8 persistent jobs — morning brief, evening close-out, weekly review, condition checks, reminder check, nightly consolidation, maintenance, and the monthly persona proposal.

### Tool Registry

| Tool | Purpose |
|---|---|
| `add_task` | Create a task (priority, due date, category) |
| `list_tasks` | List tasks with filters (status, category, date) |
| `complete_task` | Mark a task done |
| `get_today_agenda` | Today's tasks + active reminders |
| `save_note` | Save a note with tags & category |
| `search_notes` | Full-text search of notes |
| `get_recent_notes` | Recent notes |
| `set_reminder` | One-shot or recurring (cron) reminder |
| `get_current_datetime` | Current time in the user's timezone |
| `save_preference` | Persist a learned habit/preference |
| `get_news` | Fetch raw news for curation |

`/think` uses a separate deep-reasoning tier with its own model.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ (async) |
| AI backend | Model pool — OpenCode Zen gateway (`https://opencode.ai/zen/v1`) by default, extensible to google/groq/openrouter/cerebras/github via env keys; circuit-breaker router in `agent/router.py` |
| Messaging | `python-telegram-bot` (long polling) |
| Scheduling | `APScheduler` (AsyncIOScheduler + SQLAlchemy job store) |
| Storage | `aiosqlite` (SQLite, WAL mode, FTS5 full-text search, versioned persona) |
| Validation | `pydantic` |
| News | `httpx` + `feedparser` (NewsAPI, Google News RSS) |
| Config | `python-dotenv` (`.env`) |

---

## Project Structure

```
.
├── main.py                    # Entry point — wires DB, agent, bot, scheduler
├── config.py                  # Central configuration (env-driven)
├── requirements.txt
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── .env.example               # Template for environment variables
├── agent/
│   ├── brain.py               # Model client, retries, tool-call loop
│   ├── router.py              # Model pool routing + failover
│   ├── health.py              # Per-model circuits, RPM/RPD budgets
│   ├── registry.py            # Registry of models/providers
│   ├── context.py             # Prompt assembly + size caps (persona block)
│   ├── prompts.py             # Core personas
│   ├── confirmation.py        # Tool-execution confirmation gate
│   └── tools.py               # Tool implementations + registry
├── bot/
│   ├── telegram_handler.py    # Command handlers + free-text routing
│   ├── formatters.py          # Markdown escaping, message splitting, /help
│   ├── feedback.py            # 👍/👎 inline feedback + edit detection
│   └── middleware.py          # authorized-only + error handling decorators
├── services/
│   ├── scheduler.py           # APScheduler jobs (8 persistent jobs, incl. close-out)
│   ├── brief.py               # Unified morning brief (§7.1)
│   ├── consolidation.py       # Weekly review + nightly consolidation
│   ├── triggers.py            # Condition checks (§7.4)
│   ├── persona.py             # Monthly persona proposal (§6.5)
│   ├── persona_control.py     # /persona commands (§8.2)
│   ├── messaging.py           # Proposal sender (Approve/Reject buttons)
│   ├── news.py                # NewsAPI + Google News RSS aggregator
│   ├── export.py              # Retention prune + Markdown vault export + GCS backup
│   └── alerts.py              # Owner alerting on alarms/circuit events
├── storage/
│   ├── database.py            # Async SQLite CRUD + FTS5 + WAL + persona
│   ├── migrations.py          # Numbered schema migrations (1..7)
│   └── models.py              # Pydantic models & enums
├── deploy/
│   ├── deploy-vm.sh           # GCE free-tier deployment (creates VM)
│   ├── update-vm.sh           # Push code updates to an existing VM
│   ├── vm-startup.sh          # GCE startup provisioning script
│   └── oracle/                # (optional) Oracle Cloud Always Free scripts
└── tests/                     # Unit + golden tool-set tests
```

---

## Prerequisites

- Python **3.11+** (Python 3.12 works best)
- A Telegram account
- An **OpenCode Zen API key** — free signup at [opencode.ai/auth](https://opencode.ai/auth)
- A **NewsAPI key** — free at [newsapi.org](https://newsapi.org) (100 requests/day)
- (Optional) Google Cloud account for free-tier deployment

---

## Setup

### 1. Telegram Bot Token

1. Open Telegram and message **@BotFather**.
2. Send `/newbot`, choose a name (e.g. *Second Brain*), and get your token (e.g. `123456:ABC-DEF...`).
3. Find your **Telegram user ID** using @userinfobot — this restricts the bot to you only.

### 2. API Keys

- **OpenCode Zen:** sign up at [opencode.ai/auth](https://opencode.ai/auth) → copy the API key (`sk-...`).
- **NewsAPI:** sign up at [newsapi.org](https://newsapi.org) → copy the key.

### 3. Configuration

```bash
cp .env.example .env
```

Fill in `.env`:

```ini
# === REQUIRED ===
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_USER_ID=your_digits_only_id
OPENCODE_ZEN_API_KEY=sk-...
NEWS_API_KEY=your_newsapi_key

# === OPTIONAL ===
FAST_MODEL=muse-spark-1.3-contributor-free
DEEP_MODEL=muse-spark-1.3-contributor-free
MODEL_API_URL=https://opencode.ai/zen/v1
TIMEZONE=Asia/Jakarta

# Model pool — provider keys (all optional; only present ones are used)
GOOGLE_AI_API_KEY=
GROQ_API_KEY=
CEREBRAS_API_KEY=
OPENROUTER_API_KEY=
GITHUB_MODELS_TOKEN=

# Unified brief / close-out / weekly review (Phase 7)
BRIEF_HOUR=6
BRIEF_MINUTE=0
CLOSEOUT_HOUR=21
CLOSEOUT_MINUTE=0
REVIEW_DAY=fri
REVIEW_HOUR=17
REVIEW_MINUTE=0

# Legacy slots (kept for backward compatibility; NEWS_DELIVERY_HOUR also
# seeds BRIEF_HOUR when unset)
NEWS_DELIVERY_HOUR=6
NEWS_DELIVERY_MINUTE=0
AGENDA_DELIVERY_HOUR=6
AGENDA_DELIVERY_MINUTE=30

DATABASE_PATH=./data/second_brain.db
LOG_LEVEL=INFO
```

> **Model note:** the default `muse-spark-1.3-contributor-free` is served by OpenCode Zen via the **Responses API** (`/zen/v1/responses`). The `chat/completions` endpoint returns HTTP 500 for this model, so the code deliberately uses `client.responses.*`.

---

## Running Locally

### With a virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -u main.py
```

### With Docker

```bash
docker compose up --build -d
# or
docker build -t second-brain .
docker run -d --name second-brain \
  -v "$(pwd)/data:/app/data" \
  --env-file .env \
  second-brain
```

You should see `🧠 Second Brain is LIVE!` in the logs, then message the bot on Telegram.

---

## Deploying to the Cloud

### Option A: Google Cloud (free tier)

Runs on an **e2-micro** VM (always free) with a 30 GB boot disk, as a systemd service. No Docker required on the VM.

```bash
# 1. Make sure gcloud is installed & authenticated
gcloud install            # brew install --cask google-cloud-sdk
gcloud auth login
gcloud config set project <PROJECT_ID>

# 2. Create a project + enable billing (free tier still requires a billing account)

# 3. Deploy (creates VM, uploads code, installs deps, starts service)
./deploy/deploy-vm.sh

# 4. Push code updates later
./deploy/update-vm.sh
```

The VM enforces free-tier limits: 1 e2-micro, 30 GB standard boot disk, us-central1 zone. After a fresh deploy the bot starts with a clean database — to carry over your local state, copy `data/` onto the VM and restart the service.

**Useful commands**

```bash
# Tail logs
gcloud compute ssh second-brain-agent --zone=us-central1-a -- 'journalctl -fu second-brain'

# Restart the bot
gcloud compute ssh second-brain-agent --zone=us-central1-a -- 'sudo systemctl restart second-brain'

# Stop / start the VM (data persists on the boot disk)
gcloud compute instances stop  second-brain-agent --zone=us-central1-a
gcloud compute instances start second-brain-agent --zone=us-central1-a

# Delete everything
gcloud compute instances delete second-brain-agent --zone=us-central1-a
```

### Option B: Docker / docker-compose

Any 24/7 host that can run Docker (a small VPS, a Raspberry Pi, etc.) works. Mount `data/` as a volume so the SQLite database persists across restarts.

---

## Usage

### Commands

| Command | Description | Example |
|---|---|---|
| `/start` | Welcome message | — |
| `/help` | Command reference | — |
| `/addtask` | Add a task | `/addtask Review PR by Friday` |
| `/tasks` | List pending tasks | — |
| `/done <id>` | Complete a task | `/done 3` |
| `/daily` | Today's agenda | — |
| `/save` | Save a note | `/save idea: solar EV charging at home` |
| `/notes` | Recent notes | — |
| `/search` | Full-text search | `/search machine learning` |
| `/news` | Curated news digest | — |
| `/remind` | Set a reminder | `/remind tomorrow 9am call dentist` |
| `/think` | Deep reasoning | `/think pros/cons of microservices` |
| `/status` | System status (models, counts, DB size, uptime) | — |
| `/persona show` | Show the active persona | — |
| `/persona set voice\|principles\|mode_rules <text>` | Edit a persona layer (live, no restart) | `/persona set voice Formal British English` |
| `/persona history` | List persona versions | — |
| `/persona rollback <v>` | Revert to a persona version | `/persona rollback 1` |

### Natural Language

You don't need commands — just talk:

> "Add milk to the grocery list"
> "Remind me in 30 minutes to stretch"
> "What's happening in the world of AI today?"
> "I started keto — set a reminder to prep lunch at 7am"
> "Remember that I prefer calls over messages"

The agent will call the right tools automatically.

---

## Scheduled Jobs

| Job | Schedule | What it does |
|---|---|---|
| Morning Brief | Daily `BRIEF_HOUR:00` (default 06:00) | Unified brief: today's tasks, reminders, overdue + one curated news digest (§7.1) |
| Evening Close-out | Daily `CLOSEOUT_HOUR:00` (default 21:00) | Captures made today + a one-line plan for tomorrow (§7.2) |
| Weekly Review | `REVIEW_DAY` `REVIEW_HOUR` (default Fri 17:00) | Done vs Slipped + one improvement pattern, from corrections/preferences (§7.3) |
| Condition Checks | Every 15 minutes | Nudges on untouched overdue tasks, capture/execute imbalance, stale notes — deduplicated once/day per entity (§7.4) |
| Reminder Check | Every minute | Fires due reminders; advances recurring ones to the next cron occurrence; also refreshes the heartbeat |
| Nightly Consolidation | Daily 00:15 | Consolidates unprocessed corrections into aggregated preferences (§5) |
| Maintenance | Daily 03:00 | Retention pruning, Markdown vault export, GCS backup |
| Persona Proposal | Monthly (1st, 04:00) | Drafts updated operating principles → send for Approve/Reject (§6.5) |

Exactly 8 jobs; the scheduler store is persisted and old jobs are purged on boot.

---

## Data & Storage

Everything lives in `data/` at the project root:

| File | Contents |
|---|---|
| `second_brain.db` | Tasks, notes, reminders, conversations, digests, feedback, preferences, corrections, `persona_versions`, `persona` (versioned voice/principles/mode-rules), `nudge_log` (WAL mode, FTS5) |
| `scheduler_jobs.db` | APScheduler's persistent job store |
| `agent_state/` | Reserved for future agent state |

Back them up by copying the directory while the bot is stopped (or use `sqlite3 ... .backup` for a hot copy). The DB files are intentionally git-ignored.

---

## Security

- **Single-user:** every handler filters on `TELEGRAM_USER_ID`; strangers get a polite "I only respond to my owner".
- **Secrets in `.env` only** — never committed (see `.gitignore`).
- No inbound ports are required on a deployed VM (Telegram long-polling is outbound-only), so default firewall settings work.
- The OpenCode Zen key can be rotated (or swapped for any OpenAI-compatible Responses endpoint) via `OPENCODE_ZEN_API_KEY` / `MODEL_API_URL`.

---

## Operations & Maintenance

- **Update code:** `./deploy/update-vm.sh` (re-uploads, reinstalls requirements, restarts; data preserved).
- **Logs:** `journalctl -fu second-brain` on the VM; local runs log to stdout.
- **Rate limits:** the agent retries on HTTP 429/503 with exponential backoff (`_generate_with_retry`).
- **e2-micro CPU throttle:** the free VM is burstable; occasional slowdowns under load are expected, not a bug.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Conflict: terminated by other getUpdates request` | Two instances are polling the same token (e.g. local + cloud). Stop one. |
| `SQLAlchemyJobStore requires SQLAlchemy installed` | Run `pip install -r requirements.txt` (SQLAlchemy is a required dep). |
| HTTP 500 from the model endpoint | Ensure `MODEL_API_URL` points at `/zen/v1` and code uses the Responses API path. |
| No news / NewsAPI errors | Verify `NEWS_API_KEY`; the bot falls back to Google News RSS automatically. |
| Bot not responding | Check `journalctl`/logs; confirm `TELEGRAM_USER_ID` matches your actual user ID. |
| Model role error (400) | Roles must be `user`/`assistant` — never `model` (a known Responses API gotcha). |

---

## Roadmap

- [x] Backups / export to Markdown (nightly maintenance + GCS)
- [x] Learning loop (corrections → consolidated preferences → weekly review)
- [x] Persona as data (`/persona`, monthly proposals)
- [ ] Voice-note and image input
- [ ] Webhook mode (no polling) for non-VM hosts
- [ ] Web UI dashboard for notes & tasks
- [ ] Note/task editing commands

---

## License

Distributed under the MIT License. See `LICENSE` for details.