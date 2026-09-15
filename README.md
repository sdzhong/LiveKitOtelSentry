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

- Python 3.11+ (developed against 3.12)
- Node.js 20+
- Xcode 16.4+ with an iOS 15+ simulator (required by `@sentry/react-native` 8.x;
  verified on Xcode 16.4 with an iPhone 16 Pro simulator on iOS 18.2)
- CocoaPods (`gem install cocoapods`)

`backend/requirements.txt` pins `opentelemetry-distro[otlp]` on purpose. The pin is not
needed to get a working install — an unpinned resolve lands on the same versions — it
is there so an OTel upgrade is a deliberate edit rather than something that arrives
silently, because the gen_ai content bridge reaches through private OTel internals that
have moved before. The comment in the file has the details.

### Step 1: Get API Keys

| Service | URL | What you need |
|---------|-----|---------------|
| LiveKit Cloud | https://cloud.livekit.io | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` |
| OpenAI **or** OpenRouter | https://platform.openai.com/api-keys · https://openrouter.ai/keys | `OPENAI_API_KEY` **or** `OPENROUTER_API_KEY` |
| Deepgram | https://console.deepgram.com | `DEEPGRAM_API_KEY` |
| Sentry | https://sentry.io | Two `SENTRY_DSN`s — create **two** projects, one **Python** (for the backend) and one **React Native** (for the mobile app) |

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

1. The app opens in the iOS Simulator
2. **Tap the circle** — requests a token and connects to LiveKit. The circle is the
   connect/disconnect control; there is no separate connect button
3. Speak into the mic — the agent responds
4. **While still connected**, tap **"Call Backend (error)"** — triggers a backend 500.
   Staying connected is what puts the error in the conversation's trace instead of one of
   its own; see [Errors in the trace](#errors-in-the-trace)
5. Then look at:
   - **Explore → Traces** — the distributed trace, all three services in one tree
   - **AI → Agents → Conversations** — the transcript, grouped by `session_id`
   - **Issues** — the mobile `Error` and the token server's `HTTPException`

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

One oddity in the resulting tree: `invoke_agent` is a child of the `/token` span and
outlives it by the whole session, because the trace context travels in the JWT metadata
rather than in a request that stays open. Sentry renders it fine; it just looks strange in
a waterfall.

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

### Conversation content (input/output)

**TL;DR** — `agent.py` wraps Sentry's OTLP exporter so conversation content is copied from
span *events* into span *attributes*.

- **What breaks without it.** LiveKit writes message content as span events; Sentry's
  Conversations view reads it from span attributes. Same information, different field of
  the span payload — so the transcript arrives empty.
- **Why it is easy to miss.** Nothing errors. Spans, the agent → chat hierarchy, token
  counts and costs are all correct; only the message bodies are blank.
- **Why nothing else covers it.** Sentry ships gen_ai instrumentation that writes those
  attributes, but it only fires when the Sentry SDK instruments the LLM call — and here
  the agent creates spans through pure OTel by design.
- **Why at the exporter.** At `on_start` the events do not exist yet; by `on_end` the span
  is ended and its attributes are frozen. Export is the only point where the span is both
  complete and still changeable.
- **How long it is needed.** Version-skew glue. If LiveKit adopts the attribute
  convention, `_genai_content_attributes` returns nothing and the bridge becomes a no-op.

The rest of this section is the detail.

Token counts and the agent → chat hierarchy arrive on their own. The **message bodies do
not**, and the reason is a spec migration that LiveKit and Sentry sit on opposite sides of:

- LiveKit emits conversation content as span **events** — `gen_ai.system.message`,
  `gen_ai.user.message`, `gen_ai.choice`. Its own constants call these "OpenTelemetry
  GenAI event names (for structured logging)".
- Sentry's Conversations view reads span **attributes** — `gen_ai.input.messages` and
  `gen_ai.output.messages`, each a stringified array of message objects.

The split is deliberate in the spec's history, not an accident: content is bulky and often
sensitive, so the original convention put it in events, where it could be routed to a logs
pipeline and sampled or redacted independently of the span. The convention later
consolidated onto stringified-JSON attributes. You can see Sentry mid-migration in its own
constants, where `gen_ai.response.text` is already deprecated in favour of
`gen_ai.output.messages`.

Nothing in the stack closes that gap on its own. Sentry ships gen_ai instrumentation that
writes those attributes, but it only fires when the Sentry SDK instruments the LLM call —
and here the agent creates spans through pure OTel by design.

So `agent.py` bridges it. `_GenAIContentExporter` wraps the exporter `OTLPIntegration`
installs and copies content out of the events into the attributes Sentry reads, leaving
the original events untouched. It has to run at export: at `on_start` the events do not
exist yet, and by `on_end` the span is ended and its attributes are frozen.

Two guards come with it, because the failure mode is silent — spans keep exporting, only
the transcript goes missing:

- `_assert_livekit_event_contract()` checks the event-name map against `livekit-agents`'
  own `EVENT_GEN_AI_*` constants in both directions and raises on drift. `livekit-agents`
  is exact-pinned, so this can only fire on a deliberate upgrade.
- The bridge logs once when it actually **enriches** a span:
  `gen_ai content bridge enriched its first span (llm_request): 3 input / 1 output messages`
  That is the line to check after upgrading OTel or LiveKit. The install-time line only
  proves the wrapper is in place, not that content is flowing — if LiveKit ever moves to
  the attribute convention and stops emitting events, the bridge installs cleanly and
  enriches nothing.

Only `gen_ai.input.messages` and `gen_ai.output.messages` are set. The legacy
`gen_ai.response.text` and `gen_ai.request.messages` are deprecated in `sentry-sdk`:
Sentry derives both and serves them back as `responseText` / `requestMessages`, and a
value set for `gen_ai.response.text` is dropped on ingest — its own search API reports it
as an unknown attribute.

Conversations are grouped by `gen_ai.conversation.id`, which the agent stamps from
`session_id` onto **every** span rather than only AI ones. LiveKit opens `llm_request`
with no attributes and sets them afterwards, so gating at `on_start` would match only the
root span and miss every `gen_ai.chat` span. One consequence worth knowing: a conversation
can span several traces, because `session_id` outlives a single connect.

**Not ingested:** Sentry does not ingest OTLP metrics, so LiveKit's OTel metrics stay
local. (`gen_ai.response.model` was previously a gap here; `livekit-agents` 1.7.1 does
emit it, and it arrives on the `gen_ai.chat` spans.)

## Errors in the trace

Tap **"Call Backend (error)"** during a session and the error lands in the conversation's
trace, attached to the span that made the failing request. Three things are needed for
that, and each one is easy to get wrong.

**The error has to be captured at all.** `Sentry.startSpan` marks its span errored and
rethrows, but it never calls `captureException`. If the caller catches that rethrow — as
this app does, to show a status message — the error reaches Sentry nowhere, while the
span still shows as failed. `callBackendError` captures it explicitly, inside the span.

**The call has to run in the session's trace.** `connect()` deliberately forks a fresh
trace for the session with `scope.setPropagationContext`. Anything started outside that
scope inherits the propagation context created at app start instead, which is why button
presses used to collect in one long-lived trace unrelated to any conversation. The
session's trace is held in a ref for the life of the session and re-entered on each call;
when disconnected, the call keeps a trace of its own.

**The capture has to be bound to the span.** Hermes has no async context tracking, so the
span `startSpan` made active is already gone once execution resumes after an `await`. A
bare `captureException` there falls back to the scope's propagation context and attaches
the error to `lk.connect` — right trace, wrong span. `Sentry.withActiveSpan(span, ...)`
puts it on the `http.client` span where it belongs.

That last point generalises: **in React Native, any Sentry call after an `await` has lost
the active span.** Re-bind explicitly whenever the association matters.

The result is one trace per session:

```
lk.connect [lk.session]
  ├─ POST → /token → invoke_agent voice-assistant
  │                    └─ agent_session
  │                       ├─ user_turn / agent_turn …
  │                       └─ llm_request [gen_ai.chat]
  └─ debug.call_backend_fail → GET → /fail
```

Errors from both sides land in it: the mobile `Error`, the token server's
`HTTPException`, and its error-level log.

## Gotchas when testing

- **Fast Refresh cannot be trusted** for the Sentry wiring in `mobile/`. Cold-restart the
  app before judging a run:
  ```bash
  D=<simulator-udid>; B=org.reactjs.native.example.LiveKitVoiceAgent
  xcrun simctl terminate $D $B && xcrun simctl launch $D $B
  ```
- **Give Sentry a minute to ingest.** A trace queried seconds after a session ends can
  show a fraction of its spans — an incomplete trace looks identical to a broken one.
- **`BACKEND_URL` goes stale when you change networks.** Re-check
  `ipconfig getifaddr en0`, update `mobile/.env`, and restart Metro with `--reset-cache`.
- **`session_id` is generated once at app start**, so reconnecting without restarting the
  app groups the new session into the same Sentry conversation.

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
