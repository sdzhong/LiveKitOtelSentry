"""
LiveKit Voice Agent with OpenTelemetry tracing to Sentry.

DISTRIBUTED TRACING FLOW:
1. Frontend creates a session_id and requests a token from token_server
2. Token server injects trace context (traceparent, session_id) into LiveKit token metadata
3. Agent extracts trace context from participant metadata and continues the trace
4. Agent uses OTel SDK with OTLPIntegration to export spans to Sentry's OTLP endpoint
5. All spans are correlated in Sentry by session_id tag
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import attach
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

import sentry_sdk
from sentry_sdk.integrations.otlp import OTLPIntegration

from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    WorkerOptions,
    cli,
)
from livekit.plugins import deepgram, openai, silero

_BACKEND_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _BACKEND_DIR.parent
load_dotenv(_ROOT_DIR / ".env")
load_dotenv(_BACKEND_DIR / ".env")

logger = logging.getLogger("voice-agent")
logger.setLevel(logging.INFO)


def setup_sentry_with_otlp():
    """
    Initialize Sentry with OTLPIntegration.
    This automatically configures OTel span export to Sentry's OTLP endpoint.
    """
    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN"),
        environment=os.getenv("SENTRY_ENV", "dev"),
        send_default_pii=True,
        integrations=[OTLPIntegration()],
    )

    # Register the tracer provider with LiveKit so its internal spans also go to Sentry
    try:
        from livekit.agents.telemetry import set_tracer_provider as lk_set_tracer_provider
        lk_set_tracer_provider(trace.get_tracer_provider())
        logger.info("Registered OTel tracer provider with LiveKit")
    except ModuleNotFoundError:
        logger.info("LiveKit telemetry helper not available")


class _DictGetter:
    """TextMapGetter for extracting trace context from dict carriers."""

    @staticmethod
    def get(carrier, key):
        value = carrier.get(key)
        return [value] if value else []

    @staticmethod
    def keys(carrier):
        return list(carrier.keys())


def _extract_trace_context_from_metadata(metadata: str):
    """
    Extract W3C trace context and session_id from LiveKit participant metadata.
    Returns (otel_context, session_id).
    """
    if not metadata:
        return None, None
    try:
        data = json.loads(metadata)
    except Exception:
        return None, None

    carrier = {k: v for k, v in data.items() if isinstance(v, str) and v}
    if not carrier:
        return None, None

    getter = _DictGetter()
    ctx = TraceContextTextMapPropagator().extract(carrier, getter=getter)
    ctx = W3CBaggagePropagator().extract(carrier, context=ctx, getter=getter)
    return ctx, carrier.get("session_id")


class VoiceAssistant(Agent):
    def __init__(self):
        super().__init__(
            instructions=(
                "You are a friendly and helpful voice assistant. "
                "Keep your responses concise and conversational."
            ),
        )


async def entrypoint(ctx: JobContext):
    setup_sentry_with_otlp()

    tracer = trace.get_tracer("voice-agent")

    logger.info(f"Connecting to room: {ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    participant = await ctx.wait_for_participant()
    logger.info(f"Participant joined: {participant.identity}")

    # Extract trace context from token metadata (injected by token_server)
    metadata_raw = getattr(participant, "metadata", None)
    parent_ctx, session_id = _extract_trace_context_from_metadata(metadata_raw)

    # Set session_id for correlation across all traces in Sentry
    if session_id:
        sentry_sdk.set_tag("session_id", session_id)
        logger.info(f"Session ID: {session_id}")

    # Start agent session span, continuing the trace from the frontend
    span_attributes = {
        "livekit.room": ctx.room.name,
        "livekit.participant": participant.identity,
    }
    if session_id:
        span_attributes["session_id"] = session_id

    session_span = tracer.start_span(
        "lk_agent_session",
        kind=SpanKind.SERVER,
        context=parent_ctx,
        attributes=span_attributes,
    )
    attach(trace.set_span_in_context(session_span))
    ctx.add_shutdown_callback(lambda: session_span.end())

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=deepgram.STT(),
        llm=openai.LLM(model="gpt-4o"),
        tts=openai.TTS(voice="alloy"),
    )

    await session.start(agent=VoiceAssistant(), room=ctx.room)
    await session.say("Hello! How can I help you today?", allow_interruptions=True)


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            ws_url=os.getenv("LIVEKIT_URL"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET"),
        ),
    )
