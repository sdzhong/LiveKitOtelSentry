"""
Token Server for LiveKit with Sentry distributed tracing.

DISTRIBUTED TRACING FLOW:
1. Frontend sends a request with sentry-trace, baggage, traceparent and x-session-id
2. Sentry SDK auto-continues the trace from those headers
3. Server injects its own trace context into the LiveKit token metadata
4. Agent extracts that metadata to continue the same trace

This service uses Sentry's *native* tracing (traces_sample_rate), unlike agent.py which
uses pure OTel + OTLPIntegration. Each service sticks to one span mechanism.
"""

import datetime
import json
import logging
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
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

_BACKEND_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _BACKEND_DIR.parent
load_dotenv(_ROOT_DIR / ".env")
load_dotenv(_BACKEND_DIR / ".env")

# uvicorn configures its own loggers but not the root one, so without this the INFO
# lines below never reach the terminal (they would still reach Sentry Logs).
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")
logger = logging.getLogger("token-server")
logger.setLevel(logging.INFO)

sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    # Native Sentry tracing is correct here: this service has no OTLPIntegration.
    traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "1.0")),
    environment=os.getenv("SENTRY_ENV", "dev"),
    release=os.getenv("SENTRY_RELEASE"),
    send_default_pii=True,
    integrations=[
        FastApiIntegration(),
        StarletteIntegration(),
        # Sends `logger.*` output to Sentry Logs, tagged with the active trace.
        # (`enable_logs=True` is deprecated as of sentry-sdk 2.68.1.)
        LoggingIntegration(capture_sentry_logs=True),
    ],
)

app = FastAPI(title="LiveKit Token Server")

# The React Native client does not need CORS at all; this is here for browser clients.
# Note that allow_origins=["*"] together with allow_credentials=True is rejected by
# browsers per the Fetch spec, so credentials stay off unless you pin real origins.
_cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_session_id_to_sentry(request: Request, call_next):
    """Correlate this request with the mobile app and the agent."""
    session_id = request.headers.get("x-session-id")
    if session_id:
        # Tag -> searchable on errors. Span attribute -> queryable in Explore > Traces.
        sentry_sdk.set_tag("session_id", session_id)
        sentry_sdk.set_attribute("session_id", session_id)
    return await call_next(request)


class TokenRequest(BaseModel):
    identity: str
    room: str
    name: Optional[str] = None
    ttl_seconds: Optional[int] = 3600


def _sentry_trace_to_w3c(trace_value: str) -> Optional[str]:
    """
    Convert Sentry's `sentry-trace` value into a W3C `traceparent`.

    The Python SDK has no `propagate_traceparent` option yet (the JavaScript, Java and
    Native SDKs do), so the conversion is done by hand here. Both carriers are written
    into the token metadata: the agent's composite propagator accepts either, and
    emitting both is what makes the W3C interop path demonstrable.

    See https://develop.sentry.dev/sdk/foundations/trace-propagation/
    """
    parts = trace_value.split("-")
    if len(parts) != 3:
        return None
    trace_id, span_id, sampled = parts
    if len(trace_id) != 32 or len(span_id) != 16:
        return None
    flags = "01" if sampled and sampled != "0" else "00"
    return f"00-{trace_id}-{span_id}-{flags}"


def _build_trace_metadata(session_id: Optional[str] = None) -> Optional[dict]:
    """Build the metadata dict carrying trace context into the LiveKit token."""
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
    """
    Test endpoint that always returns 500.

    StarletteIntegration's default `failed_request_status_codes` covers 5xx, so this
    surfaces as an Issue attached to the incoming trace.
    """
    logger.error("Intentional failure endpoint hit")
    raise HTTPException(status_code=500, detail="Intentional failure")


@app.post("/token")
def create_token(body: TokenRequest, request: Request):
    """
    Create a LiveKit access token with trace context in its metadata.
    The agent extracts that metadata to continue the distributed trace.
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

    trace_metadata = _build_trace_metadata(session_id=session_id)
    if trace_metadata:
        token = token.with_metadata(json.dumps(trace_metadata))

    if body.ttl_seconds:
        token = token.with_ttl(datetime.timedelta(seconds=body.ttl_seconds))

    logger.info(
        "Issued token for identity=%s room=%s (trace carriers: %s)",
        body.identity,
        body.room,
        ",".join(sorted(trace_metadata or {})) or "none",
    )
    return {"token": token.to_jwt()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
