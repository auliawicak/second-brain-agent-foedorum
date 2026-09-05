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

| Date | Model | Provider | Score | GA? |
|------|-------|----------|-------|-----|
| 2026-09-05 | muse-spark-1.3-contributor-free | zen | **29/30 (97%)** | yes |
| _pending_ | big-pickle | zen | — | — |
| _pending_ | gemini-2.5-flash | google | — | — |
| _pending_ | llama-3.3-70b-versatile | groq | — | — |

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
