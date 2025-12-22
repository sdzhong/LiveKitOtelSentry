"""
Token Server for LiveKit with Sentry distributed tracing.

DISTRIBUTED TRACING FLOW:
1. Frontend sends request with sentry-trace, baggage, and x-session-id headers
2. Sentry SDK auto-continues the trace from these headers
3. Server injects trace context into LiveKit token metadata
4. Agent extracts this metadata to continue the same trace
"""

import datetime
import json
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from livekit import api
from pydantic import BaseModel

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

_BACKEND_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _BACKEND_DIR.parent
load_dotenv(_ROOT_DIR / ".env")
load_dotenv(_BACKEND_DIR / ".env")

sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "1.0")),
    environment=os.getenv("SENTRY_ENV", "dev"),
    send_default_pii=True,
    integrations=[FastApiIntegration(), StarletteIntegration()],
)

app = FastAPI(title="LiveKit Token Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_session_id_to_sentry(request: Request, call_next):
    """Set session_id tag for correlation across traces."""
    session_id = request.headers.get("x-session-id")
    if session_id:
        sentry_sdk.set_tag("session_id", session_id)
    return await call_next(request)


class TokenRequest(BaseModel):
    identity: str
    room: str
    name: Optional[str] = None
    ttl_seconds: Optional[int] = 3600


def _sentry_trace_to_w3c(trace_value: str) -> Optional[str]:
    """Convert sentry-trace format to W3C traceparent."""
    parts = trace_value.split("-")
    if len(parts) != 3:
        return None
    trace_id, span_id, sampled = parts
    if len(trace_id) != 32 or len(span_id) != 16:
        return None
    flags = "01" if sampled and sampled != "0" else "00"
    return f"00-{trace_id}-{span_id}-{flags}"


def _build_trace_metadata(session_id: Optional[str] = None) -> Optional[dict]:
    """Build metadata dict with trace context for LiveKit token."""
    traceparent = sentry_sdk.get_traceparent()
    baggage = sentry_sdk.get_baggage()

    metadata: dict[str, str] = {}
    if traceparent:
        metadata["sentry-trace"] = traceparent
        w3c = _sentry_trace_to_w3c(traceparent)
        if w3c:
            metadata["traceparent"] = w3c
    if baggage:
        metadata["baggage"] = baggage
    if session_id:
        metadata["session_id"] = session_id
    return metadata or None


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/fail")
def fail():
    """Test endpoint that always returns 500."""
    raise HTTPException(status_code=500, detail="Intentional failure")


@app.post("/token")
def create_token(body: TokenRequest, request: Request):
    """
    Create a LiveKit access token with trace context in metadata.
    The agent will extract this metadata to continue the distributed trace.
    """
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    if not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="Missing LiveKit credentials")

    session_id = request.headers.get("x-session-id")

    grants = api.VideoGrants(
        room_join=True,
        room=body.room,
        can_publish=True,
        can_subscribe=True,
    )

    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(body.identity)
        .with_name(body.name or body.identity)
        .with_grants(grants)
    )

    # Inject trace context into token metadata for the agent to continue the trace
    trace_metadata = _build_trace_metadata(session_id=session_id)
    if trace_metadata:
        token = token.with_metadata(json.dumps(trace_metadata))

    if body.ttl_seconds:
        token = token.with_ttl(datetime.timedelta(seconds=body.ttl_seconds))

    return {"token": token.to_jwt()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
