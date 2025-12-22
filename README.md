# LiveKit Voice Agent with Sentry Distributed Tracing

A voice agent application demonstrating distributed tracing across React Native frontend, Python backend, and LiveKit agent using Sentry.

## Architecture

```
     SERVICE 1: FRONTEND          SERVICE 2: TOKEN SERVER         SERVICE 3: AGENT
     (React Native)               (Python/FastAPI)                (Python/LiveKit)
    ┌─────────────────┐          ┌─────────────────┐          ┌─────────────────┐
    │                 │  HTTP    │                 │          │                 │
    │   Sentry SDK    │─────────>│   Sentry SDK    │          │   OTel SDK      │
    │   creates spans │ POST     │   creates spans │          │   creates spans │
    │                 │ /token   │                 │          │                 │
    └────────┬────────┘          └────────┬────────┘          └────────┬────────┘
             │                            │                            │
             │                            │ JWT with                   │
             │                            │ trace metadata             │
             │<───────────────────────────┘                            │
             │                                                         │
             │   WebSocket via LiveKit Cloud                           │
             │   (metadata: traceparent, session_id)                   │
             └────────────────────────────────────────────────────────>│
                                                        extracts trace context
             │                            │                            │
             │ Sentry                     │ Sentry                     │ OTLP
             │ SDK API                    │ SDK API                    │ Protocol
             │                            │                            │
             └────────────────────────────┼────────────────────────────┘
                                          │
                                          ▼
                               ┌────────────────────┐
                               │   SENTRY BACKEND   │
                               │                    │
                               │  Correlates by     │
                               │  trace_id &        │
                               │  session_id        │
                               └────────────────────┘
```

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+
- Xcode 15+ (for iOS)
- CocoaPods (`gem install cocoapods`)

### Step 1: Get API Keys

You'll need accounts and API keys from:

| Service | URL | What you need |
|---------|-----|---------------|
| LiveKit Cloud | https://cloud.livekit.io | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` |
| OpenAI | https://platform.openai.com/api-keys | `OPENAI_API_KEY` |
| Deepgram | https://console.deepgram.com | `DEEPGRAM_API_KEY` |
| Sentry | https://sentry.io | `SENTRY_DSN` (create **two** projects: Python + React Native) |

### Step 2: Backend Setup

```bash
cd backend

python -m venv venv
source venv/bin/activate

pip install -r requirements.txt

# Create .env from template
cp .env.example .env
```

### Step 3: Mobile Setup

```bash
cd mobile

npm install

cd ios && pod install && cd ..

# Create .env from template
cp .env.example .env
```

**Important:** For `BACKEND_URL`, use your machine's local IP (e.g., `192.168.1.x`), not `localhost`. Find it with:
```bash
ipconfig getifaddr en0 
```

### Step 4: Run the App

Open **3 terminal windows**:

**Terminal 1 - Token Server:**
```bash
cd backend
source venv/bin/activate
python token_server.py
# Runs on http://0.0.0.0:8000
```

**Terminal 2 - LiveKit Agent:**
```bash
cd backend
source venv/bin/activate
python agent.py dev
# Connects to LiveKit Cloud, waits for participants
```

**Terminal 3 - React Native Metro:**
```bash
cd mobile
npx react-native start --reset-cache
```

### Step 5: Test It

1. App opens in iOS Simulator
2. Tap **"Connect to Agent"** - requests token, connects to LiveKit
3. Speak into the mic - agent responds via AI
4. Tap **"Call /fail"** - triggers a backend error for tracing demo
5. Open **Sentry → Performance → Traces** to see distributed traces

## How Distributed Tracing Works

1. **Frontend** generates a `session_id` on app start
2. **Frontend** calls `/token` with `sentry-trace` headers (auto-attached by Sentry SDK)
3. **Token Server** continues the trace and injects trace context into LiveKit token metadata
4. **Agent** extracts `traceparent` from participant metadata and continues the trace
5. **All services** tag spans with `session_id` for correlation
6. **Sentry** receives spans from both Sentry SDK API and OTLP, correlates by `trace_id`

### Why No SDK Conflict?

Each service uses **only one** span creation mechanism:
- Frontend + Token Server: Sentry SDK (traces via Sentry API)
- Agent: OTel SDK via `OTLPIntegration` (traces via OTLP)

The Agent's Sentry SDK handles errors only (no `traces_sample_rate`), while OTel handles all span creation.

## Project Structure

```
├── backend/
│   ├── agent.py           # LiveKit voice agent (OTel SDK → Sentry OTLP)
│   ├── token_server.py    # FastAPI token server (Sentry SDK)
│   ├── requirements.txt
│   └── .env.example       # Backend env template
├── mobile/
│   ├── index.js           # Sentry init + session_id generation
│   ├── src/
│   │   └── VoiceAgent.tsx # Main UI component
│   ├── ios/               # iOS native code (Xcode project)
│   ├── package.json
│   └── .env.example       # Mobile env template
└── README.md
```