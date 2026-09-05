# Model pool matrix — golden tool-set results

Records the `tools`-tier golden test outcome per model/provider, along with
static limits used by the router (budget/cooldown). The `tools` tier reaches
GA at **>= 90%** across the full gated set on **>= 3 providers**.

Run a single provider:

```
.venv/bin/python tests/golden_tools.py --model <id> --provider <label> [--json]
```

## Gated set (30 utterances)
English + Indonesian, covering: `add_task`, `set_reminder`, `list_tasks`,
`complete_task`, `get_today_agenda`, `save_note`, `search_notes`,
`get_recent_notes`, `get_current_datetime`, `save_preference`, `get_news`.

## Results

| Date | Model | Provider | Score | GA? | Notes |
|------|-------|----------|-------|-----|-------|
| 2026-09-05 | muse-spark-1.3-contributor-free | zen | **29/30 (97%)** | yes | sole `tools`-tier model |
| 2026-09-06 | big-pickle | zen | — | no | HTTP 401 "Model is disabled" (not on this account/plan) |
| 2026-09-06 | mimo-v2.5-free | zen | 0/30 (0%) | no | never emits a tool call; heavy free-tier rate limiting |
| 2026-09-06 | ling-3.0-flash-fin-free | zen | 14/30 (47%) | no | mostly prose instead of a JSON tool block |
| 2026-09-06 | nemotron-3-ultra-free | zen | — | no | upstream consistently 502 "Service temporarily overloaded" |
| 2026-09-06 | nemotron-3.5-lightning-free | zen | 23/30 (77%) | no | misses reminders/news + frequent timeouts (`__error__`) |
| _pending_ | big-pickle (retest) | zen | — | — | only if it becomes enabled on this plan |
| _pending_ | gemini-2.5-flash | google | — | — | needs GEMINI_API_KEY |
| _pending_ | llama-3.3-70b-versatile | groq | — | — | needs GROQ_API_KEY |

### Acceptance status

Every free model currently available on OpenCode Zen has been tested. Only
**muse** meets the >= 90% GA bar, so the `tools` tier runs on muse alone while
mimo/ling/lightning serve as `chat`/`classify` fallbacks. The >= 3 **providers**
GA criterion is **not yet satisfied**: all zen models share one provider key,
and per owner decision no other provider is being onboarded for now. The
criterion can be satisfied later by adding GEMINI_API_KEY / GROQ_API_KEY (2
more providers) — the golden harness and failover path are ready for them.

## Static registry snapshot (compiled Sept 2026)

| Provider | Base URL | API style |
|----------|----------|-----------|
| zen | https://opencode.ai/zen/v1 | responses (muse) + chat_completions |
| google | https://generativelanguage.googleapis.com/v1beta/openai/ | chat_completions |
| groq | https://api.groq.com/openai/v1 | chat_completions |
| openrouter | https://openrouter.ai/api/v1 | chat_completions |
| cerebras | https://api.cerebras.ai/v1 | chat_completions |
| github | https://models.inference.ai.azure.com | chat_completions |

Budget/cooldown rules (Phase 1): breaker opens at 3 consecutive failures with
backoff `15m * 2^(failures-3)` capped at 2h; non-retryable (400/401/403)
opens 6h; RPD at >= 80% deprioritized; RPM tracked in-memory.
