# Model pick evidence — OpenCode Zen free tier

Why the pool pins `muse-spark-1.3-contributor-free` (as of Sept 2026), plus the
known quirks of using Zen free models behind the Hermes model proxy
(`gateway/openai_proxy.py`).

## Tool-calling evaluation (30-utterance golden set, rough)

Run against the secondbrain CLI tool surface. Only **muse** cleared the bar:

| Date | Model (free) | Score | GA? | Notes |
|------|--------------|-------|-----|-------|
| 2026-09-06 | muse-spark-1.3-contributor-free | 28/30 (93%) | yes | sole active model; 97% on 09-05 |
| 2026-09-06 | big-pickle | — | no | HTTP 401 "Model is disabled" |
| 2026-09-06 | mimo-v2.5-free | 0/30 | no | never emits a tool call |
| 2026-09-06 | ling-3.0-flash-fin-free | 14/30 (47%) | no | shows prose, not tool calls |
| 2026-09-06 | nemotron-3-ultra-free | — | no | upstream consistently 502 |
| 2026-09-06 | nemotron-3.5-lightning-free | 23/30 (77%) | no | misses reminders/news |

The `tests/` golden harness was removed when the legacy bot stack was
superseded by Hermes; the CI-evidence rows above are retained for the record.

## Transport rules (why the proxy exists)

- Zen free models are served through the **Responses API**; plain
  `chat/completions` returns HTTP 500 for them.
- Free tier **rejects `function_call_output`**: Hermes' native tool-result
  envelope cannot pass through. The proxy **folds tool results into synthetic
  user text**; assistant items always carry a `content` list.
- Stable per-thread `x-opencode-session` header defeats the free tier's
  session 400s.
- Vision input travels as `input_image` parts; the pool forces
  `reasoning: {"effort": "minimal"}` on image turns.

## Verified media support (live tests, Sep 2026)

| Capability | Path | Result |
|---|---|---|
| Image (tool) | pool `/v1/chat/completions` with `image_url` data-URI → muse | "The image is solid blue." (correct) |
| Image (agent) | Hermes `auxiliary.vision` → custom provider → pool | vision tool returned the same description |
| Voice | Groq Whisper `whisper-large-v3-turbo` (free tier) | `{"success": true, "provider": "groq"}` — exact transcript |
| Voice (local fallback) | `faster-whisper` base (CPU int8) in the Hermes venv | load ~11s, 7s to transcribe a 6s clip, exact transcript |

Zen free models that 500 on images: `mimo-v2.5-free`, `ling-3.0-flash-fin-free`.

## Multi-provider GA (pending)

Free tier currently has exactly one usable model. GA of the "≥3 providers"
criterion is pending a second provider key (Groq/Gemini/OpenRouter). The
proxy/registry layout supports adding OpenAI-compatible providers; the pool
fails over per-circuit-breaker rules (breaker at 3 consecutive failures,
backoff `15m * 2^(failures-3)` capped at 2h; non-retryable 4xx opens 6h).