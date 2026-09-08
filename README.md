# LiveKit Voice Agent with Sentry Distributed Tracing

A voice agent application demonstrating distributed tracing across a React Native
frontend, a Python token server, and a LiveKit voice agent — all correlated in Sentry.

The point of the repo is that **each service uses exactly one span mechanism**, and they
still end up in one trace:

| Service | Span mechanism | Exported via |
|---|---|---|
| `mobile/` (React Native) | Sentry SDK native tracing | Sentry SDK |
| `backend/token_server.py` (FastAPI) | Sentry SDK native tracing | Sentry SDK |
| `backend/agent.py` (LiveKit agent) | **pure OpenTelemetry SDK** | Sentry OTLP ingestion (`OTLPIntegration`) |

## Architecture

```
     SERVICE 1: FRONTEND          SERVICE 2: TOKEN SERVER         SERVICE 3: AGENT
     (React Native)               (Python/FastAPI)                (Python/LiveKit)
    ┌─────────────────┐          ┌─────────────────┐          ┌─────────────────┐
    │                 │  HTTP    │                 │          │                 │
    │   Sentry SDK    │─────────>│   Sentry SDK    │          │    OTel SDK     │
    │   creates spans │ POST     │   creates spans │          │  creates spans  │
    │                 │ /token   │                 │          │  + LiveKit's    │
    └────────┬────────┘          └────────┬────────┘          │  gen_ai.* spans │
             │                            │                   └────────┬────────┘
             │   sentry-trace                                          │
             │   baggage                  │ JWT metadata:              │
             │   traceparent  ◄── W3C     │  sentry-trace              │
             │                            │  traceparent               │
             │<───────────────────────────┘  session_id                │
             │                                                         │
             │   WebSocket via LiveKit Cloud                           │
             └────────────────────────────────────────────────────────>│
                                            composite propagator extracts
                                            either carrier shape
             │                            │                            │
             │ Sentry                     │ Sentry                     │ OTLP
             │ SDK API                    │ SDK API                    │ /v1/traces
             │                            │                            │ (+ /v1/logs)
             └────────────────────────────┼────────────────────────────┘
                                          │
                                          ▼
                        ┌──────────────────────────────────┐
                        │           SENTRY                 │
                        │                                  │
                        │  Explore > Traces  (trace_id)    │
                        │  Explore > Logs    (trace_id)    │
                        │  AI > Agents       (gen_ai.*)    │
                        │  Issues            (errors)      │
                        │                                  │
                        │  correlated by trace_id +        │
                        │  session_id                      │
                        └──────────────────────────────────┘
```

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 20+
- Xcode 16.4+ and iOS 15+ (required by `@sentry/react-native` 8.x)
- CocoaPods (`gem install cocoapods`)

### Step 1: Get API Keys

| Service | URL | What you need |
|---------|-----|---------------|
| LiveKit Cloud | https://cloud.livekit.io | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` |
| OpenAI **or** OpenRouter | https://platform.openai.com/api-keys · https://openrouter.ai/keys | `OPENAI_API_KEY` **or** `OPENROUTER_API_KEY` |
| Deepgram | https://console.deepgram.com | `DEEPGRAM_API_KEY` |
| Sentry | https://sentry.io | `SENTRY_DSN` (create **two** projects: Python + React Native) |

### Step 2: Backend Setup

```bash
cd backend

python -m venv venv
source venv/bin/activate

pip install -r requirements.txt

cp .env.example .env    # then fill it in
```

### Step 3: Mobile Setup

```bash
cd mobile

npm install
cd ios && pod install && cd ..

cp .env.example .env    # then fill it in
```

**Important:** for `BACKEND_URL`, use your machine's local IP (e.g. `192.168.1.x`), not
`localhost`. Find it with:
```bash
ipconfig getifaddr en0
```

`mobile/.env` is inlined at bundle time, so restart Metro with `--reset-cache` after
editing it.

### Step 4: Run the App

Open **3 terminal windows**:

**Terminal 1 — Token Server:**
```bash
cd backend && source venv/bin/activate
python token_server.py            # http://0.0.0.0:8000
```

**Terminal 2 — LiveKit Agent:**
```bash
cd backend && source venv/bin/activate
python agent.py dev               # connects to LiveKit Cloud, waits for participants
```

**Terminal 3 — React Native Metro:**
```bash
cd mobile
npx react-native start --reset-cache
```

### Step 5: Test It

1. App opens in the iOS Simulator
2. Tap **"Connect to Agent"** — requests a token, connects to LiveKit
3. Speak into the mic — the agent responds
4. Tap **"Call Backend (error)"** — triggers a backend 500 for the tracing demo
5. Open **Sentry → Explore → Traces** to see the distributed trace

## How Distributed Tracing Works

1. **Frontend** generates a `session_id` on app start
2. **Frontend** calls `/token`; the Sentry SDK attaches `sentry-trace`, `baggage`, and —
   because `propagateTraceparent` is on — a W3C `traceparent`
3. **Token Server** continues the trace and injects its own trace context
   (`sentry-trace`, `traceparent`, `baggage`, `session_id`) into the LiveKit token metadata
4. **Agent** extracts that context with a composite propagator that accepts either
   carrier shape, and continues the same trace
5. **All services** stamp `session_id` — on the agent, a span processor puts it on
   *every* span, including LiveKit's internal ones
6. **Sentry** receives spans from the Sentry SDK and from OTLP and correlates by
   `trace_id`

### Why there's no SDK conflict

Each service creates spans through one mechanism only. The agent's Sentry SDK handles
errors and logs; span creation and export belong entirely to OTel. This is not a style
preference — Sentry's docs are explicit that you must **not** set `traces_sample_rate` or
`traces_sampler` when using `OTLPIntegration`.

## AI Agent Monitoring

Open **Sentry → AI → Agents**: the voice sessions show up there with token usage, with no
AI-specific instrumentation in this repo.

That works because of two things meeting in the middle:

- `livekit-agents` >= 1.7 stamps OpenTelemetry GenAI semantic-convention attributes on
  its LLM spans — `gen_ai.operation.name="chat"`, `gen_ai.provider.name`,
  `gen_ai.request.model`, `gen_ai.usage.input_tokens` / `output_tokens` /
  `cache_read.input_tokens` / `reasoning_tokens`.
- Sentry's Relay derives the Sentry span op from those attributes:
  `gen_ai.operation.name` → `gen_ai.{operation}`. So a LiveKit `llm_request` span arrives
  as `span.op = gen_ai.chat`.

`agent.py` adds one thing on top: the root span declares itself the container span with
`gen_ai.operation.name = "invoke_agent"` and `gen_ai.agent.name`, so the
agent → chat hierarchy renders instead of orphaned LLM spans.

**Known gap:** Sentry's conventions list `gen_ai.response.model` as a MUST for LLM spans,
and LiveKit does not currently emit it. Sentry also does **not** ingest OTLP metrics, so
LiveKit's OTel metrics stay local.

## What's Sentry-current here

Notes on the specific Sentry OTel behaviours this repo depends on:

- **Install extra.** `OTLPIntegration` needs `sentry-sdk[opentelemetry-otlp]`
  (which pulls `opentelemetry-distro[otlp]`). The plain `[opentelemetry]` extra is not
  enough.
- **No native tracing alongside OTLP.** `traces_sample_rate` is deliberately absent from
  `agent.py`; sampling is OTel-side. It *is* set on the token server, which has no
  `OTLPIntegration`.
- **Provider before init.** `OTLPIntegration` reuses an already-registered
  `TracerProvider` and only creates a bare one if none exists — so `agent.py` registers
  its own provider (carrying a `Resource` with `service.name`) *before* `sentry_sdk.init`.
- **`setup_propagator=False` + composite propagator.** Left at its default, the
  integration replaces the global propagator with Sentry's own, which drops W3C
  `traceparent` extraction. `agent.py` installs a `CompositePropagator` of
  `TraceContextTextMapPropagator` + `W3CBaggagePropagator` + `SentryOTLPPropagator`
  instead. The option is also flagged for removal in the next major, so this is the
  forward-compatible path.
- **`capture_exceptions=True`.** Makes `span.record_exception(...)` also report to
  Sentry Issues. Repeats may be dropped by `DedupeIntegration`.
- **Trace ↔ error ↔ log linking.** The integration registers an *external propagation
  context* that reads the active OTel span, so Sentry errors and logs emitted from the
  agent carry the OTLP trace's `trace_id`/`span_id`.
- **Logs.** `enable_logs=True` is deprecated as of `sentry-sdk` 2.68.1; both Python
  services use `LoggingIntegration(capture_sentry_logs=True)` instead. The agent can also
  export raw OTel log records to Sentry's OTLP logs endpoint — set `SENTRY_OTLP_LOGS=1`.
- **`propagateTraceparent` on the client.** Available in the JavaScript/React Native,
  Java and Native SDKs (default `false`). The Python SDK does not have it yet, which is
  why `token_server.py` converts `sentry-trace` to `traceparent` by hand.
- **OTLP ingestion is in open beta.**

## Choosing an LLM provider

`livekit-plugins-openai` speaks the OpenAI API, and OpenRouter is API-compatible, so the
same plugin covers both — only the base URL and key change. The agent picks a provider
from whichever key is present:

| You have | LLM | STT | TTS |
|---|---|---|---|
| `OPENAI_API_KEY` | OpenAI | Deepgram | OpenAI |
| `OPENROUTER_API_KEY` | OpenRouter (`LLM.with_openrouter()`) | Deepgram | Deepgram |

**OpenRouter has no TTS**, which is why that route uses Deepgram for speech synthesis —
you already need a Deepgram key for STT, so it costs you nothing extra.

Set `LLM_PROVIDER` / `TTS_PROVIDER` to override the auto-detection, and `LLM_MODEL` to
pick a model (any OpenRouter slug works, e.g. `anthropic/claude-3.5-haiku`).

One wrinkle for the tracing demo: LiveKit derives `gen_ai.provider.name` from the
client's base-URL host, so OpenRouter sessions report `openrouter.ai` rather than a
canonical provider id. The spans still land in AI Agent Monitoring, since Sentry derives
the span op from `gen_ai.operation.name`.

## Environment Variables

### `backend/.env`

| Variable | Required | Notes |
|---|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | yes | LiveKit Cloud |
| `OPENAI_API_KEY` | one of | LLM + TTS from OpenAI |
| `OPENROUTER_API_KEY` | one of | LLM from OpenRouter; TTS then falls back to Deepgram |
| `DEEPGRAM_API_KEY` | yes | STT, and TTS on the OpenRouter route |
| `LLM_PROVIDER` / `TTS_PROVIDER` | no | `openai` \| `openrouter` / `openai` \| `deepgram`; auto-detected from the keys present |
| `LLM_MODEL` | no | `gpt-4o` on OpenAI, `openai/gpt-4o-mini` on OpenRouter |
| `TTS_VOICE`, `DEEPGRAM_TTS_MODEL` | no | defaults `alloy`, `aura-2-andromeda-en` |
| `SENTRY_DSN` | yes | Python project. OTLP endpoints are derived from it |
| `SENTRY_ENV` | no | default `dev` |
| `SENTRY_RELEASE` | no | also used as OTel `service.version` |
| `OTEL_SERVICE_NAME` | no | agent's `service.name`, default `voice-agent` |
| `SENTRY_TRACES_SAMPLE_RATE` | no | token server only, default `1.0` |
| `CORS_ALLOW_ORIGINS` | no | comma-separated, default `*` |
| `SENTRY_OTLP_LOGS` | no | `1` also exports OTel log records over OTLP |
| `PORT` | no | token server port, default `8000` |

### `mobile/.env`

| Variable | Required | Notes |
|---|---|---|
| `BACKEND_URL` | yes | token server, use your LAN IP |
| `LIVEKIT_URL` | yes | same `wss://` URL as the backend |
| `SENTRY_DSN` | yes | React Native project |
| `SENTRY_ENV` | no | default `dev` |
| `SENTRY_RELEASE` | no | default `livekit-voice-agent@<version>+<platform>` |
| `LK_ROOM` | no | default `voice-room` |

## Project Structure

```
├── backend/
│   ├── agent.py           # LiveKit voice agent (OTel SDK → Sentry OTLP)
│   ├── token_server.py    # FastAPI token server (Sentry SDK native tracing)
│   ├── requirements.txt
│   └── .env.example
├── mobile/
│   ├── index.js           # Sentry init + session_id + Sentry.wrap(App)
│   ├── src/
│   │   └── VoiceAgent.tsx # Main UI component + lk.connect trace
│   ├── ios/               # iOS native code (Xcode project)
│   ├── package.json
│   └── .env.example
└── README.md
```
