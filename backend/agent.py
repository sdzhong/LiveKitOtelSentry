"""
LiveKit Voice Agent traced with pure OpenTelemetry, exported to Sentry over OTLP.

DISTRIBUTED TRACING FLOW:
1. Frontend creates a session_id and requests a token from token_server
2. Token server injects trace context (sentry-trace, baggage, traceparent, session_id)
   into the LiveKit token metadata
3. Agent extracts that trace context from the participant metadata and continues the trace
4. Agent exports spans via OTel -> Sentry's OTLP ingestion endpoint (OTLPIntegration)
5. Every span carries `session_id`, so all three services correlate in Sentry

WHY THERE IS NO SDK CONFLICT:
This process creates spans with the OTel SDK *only*. `traces_sample_rate` is
deliberately not set -- Sentry's docs are explicit that you must not enable Sentry
native tracing alongside OTLPIntegration. Sampling belongs to OTel here.

WHAT SHOWS UP IN SENTRY AI AGENT MONITORING:
livekit-agents >= 1.7 stamps OpenTelemetry GenAI semantic-convention attributes on its
LLM spans (gen_ai.operation.name, gen_ai.provider.name, gen_ai.request.model,
gen_ai.usage.*). Sentry's Relay derives the Sentry span op from those attributes
(gen_ai.operation.name -> `gen_ai.{operation}`), so those spans land in AI Agent
Monitoring without any AI-specific instrumentation on our side. The root span below
declares itself as the `gen_ai.invoke_agent` container so the agent -> chat hierarchy
renders correctly.
"""

import json
import logging
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import Context, attach, detach
from opentelemetry.propagate import extract, set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.textmap import default_getter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.otlp import OTLPIntegration, SentryOTLPPropagator

from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
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

AGENT_NAME = "voice-assistant"
INSTRUCTIONS = (
    "You are a friendly and helpful voice assistant. "
    "Keep your responses concise and conversational."
)

# Set once per job in `entrypoint`, read by the span processor below so that *every*
# span in the session carries session_id -- not just the root span we create ourselves.
# Note: asyncio copies the current context when a task is created, so tasks spawned
# before this is set (e.g. the initial room connection) will not carry the attribute.
_session_id: ContextVar[Optional[str]] = ContextVar("lk_session_id", default=None)

# Held so the shutdown callback can force_flush the batch processor directly, rather
# than going back through trace.get_tracer_provider() (which is typed as the API's
# proxy provider and would not be set if something else registered one first).
_tracer_provider: Optional[TracerProvider] = None


class SessionAttributeSpanProcessor(SpanProcessor):
    """Stamps the current session_id onto every span the process emits."""

    def on_start(self, span, parent_context: Optional[Context] = None) -> None:
        session_id = _session_id.get()
        if session_id:
            span.set_attribute("session_id", session_id)


def _setup_otlp_logs(dsn: str) -> None:
    """
    Optionally export OTel *log records* to Sentry's OTLP logs endpoint.

    Two caveats worth knowing:
      * OTLPIntegration only configures the traces exporter, and
        `sentry_sdk.consts.EndpointType` has no OTLP_LOGS member, so the logs URL is
        derived from the DSN by hand here.
      * `opentelemetry.sdk._logs` is still a private/experimental module upstream.

    Off by default; the Sentry SDK logs configured in `_init_telemetry` already cover
    the common case (and link to the active OTel span automatically).
    """
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from sentry_sdk.consts import VERSION
    from sentry_sdk.utils import Dsn

    parsed = Dsn(dsn)
    auth = parsed.to_auth(f"sentry.python/{VERSION}")
    endpoint = (
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        f"api/{parsed.project_id}/integration/otlp/v1/logs"
    )

    provider = LoggerProvider(resource=_otel_resource())
    provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=endpoint, headers={"X-Sentry-Auth": auth.to_header()})
        )
    )
    set_logger_provider(provider)
    logging.getLogger().addHandler(LoggingHandler(logger_provider=provider))
    logger.info("Exporting OTel log records to %s", endpoint)


def _otel_resource() -> Resource:
    """Identifies this process as its own service in Sentry."""
    return Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", "voice-agent"),
            "service.version": os.getenv("SENTRY_RELEASE", "dev"),
            "deployment.environment": os.getenv("SENTRY_ENV", "dev"),
        }
    )


def _init_telemetry() -> None:
    """
    Wire up OTel -> Sentry OTLP. Runs once per worker subprocess (see `_prewarm`).

    Ordering matters: OTLPIntegration's exporter setup reuses an already-registered
    TracerProvider and only creates a bare one if none exists, so we register ours
    (with a Resource and our span processor) *before* sentry_sdk.init.
    """
    global _tracer_provider

    provider = TracerProvider(resource=_otel_resource())
    provider.add_span_processor(SessionAttributeSpanProcessor())
    trace.set_tracer_provider(provider)
    _tracer_provider = provider

    dsn = os.getenv("SENTRY_DSN")
    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENV", "dev"),
        release=os.getenv("SENTRY_RELEASE"),
        send_default_pii=True,
        # NOTE: no traces_sample_rate / traces_sampler on purpose -- OTLPIntegration
        # owns span export, and mixing the two is explicitly unsupported.
        integrations=[
            # setup_propagator=False because the integration would otherwise *replace*
            # the global propagator with Sentry's own, which drops W3C traceparent
            # support. We install a composite below instead. The option is also flagged
            # for removal in the next major, so this is the forward-compatible path.
            OTLPIntegration(setup_propagator=False, capture_exceptions=True),
            # Sends `logger.*` output to Sentry Logs. Each log record picks up the
            # active OTel span's trace_id/span_id via the SDK's external propagation
            # context, which is what links logs to OTLP traces.
            # (`enable_logs=True` is deprecated as of sentry-sdk 2.68.1.)
            LoggingIntegration(capture_sentry_logs=True),
        ],
    )

    # Accept either carrier shape: W3C traceparent/baggage *or* sentry-trace/baggage.
    set_global_textmap(
        CompositePropagator(
            [
                TraceContextTextMapPropagator(),
                W3CBaggagePropagator(),
                SentryOTLPPropagator(),
            ]
        )
    )

    if dsn and os.getenv("SENTRY_OTLP_LOGS") == "1":
        _setup_otlp_logs(dsn)

    # Bind LiveKit's own tracer to our provider so its internal spans (including the
    # gen_ai.* LLM spans) export to Sentry too. LiveKit only emits through the provider
    # its tracer is bound to -- setting the global provider alone is not enough.
    from livekit.agents.telemetry import set_tracer_provider as lk_set_tracer_provider

    lk_set_tracer_provider(trace.get_tracer_provider())
    logger.info("OTel tracer provider registered with LiveKit and Sentry OTLP")


def _extract_trace_context(metadata: Optional[str]):
    """
    Extract trace context and session_id from LiveKit participant metadata.

    The global composite propagator handles the parsing, so this only has to turn the
    JSON blob into a flat string carrier.

    Returns (otel_context, session_id).
    """
    if not metadata:
        return None, None
    try:
        data = json.loads(metadata)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(data, dict):
        return None, None

    carrier = {k: v for k, v in data.items() if isinstance(v, str) and v}
    if not carrier:
        return None, None

    return extract(carrier, getter=default_getter), carrier.get("session_id")


def _build_llm():
    """
    LLM provider. Defaults to OpenRouter when OPENROUTER_API_KEY is set, otherwise
    OpenAI. Override explicitly with LLM_PROVIDER=openai|openrouter.

    OpenRouter is OpenAI-API-compatible, so this is the same plugin either way -- only
    the base_url and key change. Note that LiveKit derives gen_ai.provider.name from the
    client's base-URL host, so OpenRouter sessions report `openrouter.ai`.
    """
    provider = os.getenv("LLM_PROVIDER") or (
        "openrouter" if os.getenv("OPENROUTER_API_KEY") else "openai"
    )
    if provider == "openrouter":
        model = os.getenv("LLM_MODEL", "openai/gpt-4o-mini")
        logger.info("LLM: OpenRouter (%s)", model)
        return openai.LLM.with_openrouter(model=model)

    model = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4o")
    logger.info("LLM: OpenAI (%s)", model)
    return openai.LLM(model=model)


def _build_tts():
    """
    TTS provider. Defaults to OpenAI when OPENAI_API_KEY is set, otherwise Deepgram.
    Override explicitly with TTS_PROVIDER=openai|deepgram.

    OpenRouter does not offer TTS, so an OpenRouter-only setup needs Deepgram here
    (which is already required for STT).
    """
    provider = os.getenv("TTS_PROVIDER") or (
        "openai" if os.getenv("OPENAI_API_KEY") else "deepgram"
    )
    if provider == "deepgram":
        model = os.getenv("DEEPGRAM_TTS_MODEL", "aura-2-andromeda-en")
        logger.info("TTS: Deepgram (%s)", model)
        return deepgram.TTS(model=model)

    voice = os.getenv("TTS_VOICE", "alloy")
    logger.info("TTS: OpenAI (%s)", voice)
    return openai.TTS(voice=voice)


class VoiceAssistant(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=INSTRUCTIONS)


def _prewarm(proc: JobProcess) -> None:
    """Runs once per worker subprocess, before any job is dispatched."""
    _init_telemetry()
    # Load VAD once per process instead of once per session.
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    tracer = trace.get_tracer("voice-agent")

    logger.info("Connecting to room: %s", ctx.room.name)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    participant = await ctx.wait_for_participant()
    logger.info("Participant joined: %s", participant.identity)

    # Continue the trace started by the frontend and carried through the token server.
    parent_ctx, session_id = _extract_trace_context(getattr(participant, "metadata", None))

    if session_id:
        _session_id.set(session_id)
        sentry_sdk.set_tag("session_id", session_id)
        logger.info("Session ID: %s", session_id)

    session_span = tracer.start_span(
        f"invoke_agent {AGENT_NAME}",
        kind=SpanKind.SERVER,
        context=parent_ctx,
        attributes={
            # Marks this as the AI Agent Monitoring container span. Relay derives
            # sentry.op = gen_ai.invoke_agent from gen_ai.operation.name.
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": AGENT_NAME,
            "gen_ai.system_instructions": INSTRUCTIONS,
            "livekit.room": ctx.room.name,
            "livekit.participant": participant.identity,
        },
    )
    context_token = attach(trace.set_span_in_context(session_span))

    async def end_session_span(reason: str = "") -> None:
        if reason:
            session_span.set_attribute("lk.shutdown_reason", reason)
        session_span.end()
        detach(context_token)
        # Flush both pipelines before the process exits, or batched spans are lost.
        if _tracer_provider is not None:
            _tracer_provider.force_flush(5_000)
        sentry_sdk.flush(timeout=2.0)

    ctx.add_shutdown_callback(end_session_span)

    try:
        session = AgentSession(
            vad=ctx.proc.userdata.get("vad") or silero.VAD.load(),
            stt=deepgram.STT(),
            llm=_build_llm(),
            tts=_build_tts(),
        )

        await session.start(agent=VoiceAssistant(), room=ctx.room)
        await session.say("Hello! How can I help you today?", allow_interruptions=True)
    except Exception as exc:
        # record_exception also reports to Sentry Issues because the integration is
        # configured with capture_exceptions=True. Repeats may be dropped by
        # DedupeIntegration.
        session_span.record_exception(exc)
        session_span.set_status(Status(StatusCode.ERROR, str(exc)))
        logger.exception("Agent session failed")
        raise


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=_prewarm,
            ws_url=os.getenv("LIVEKIT_URL"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET"),
        ),
    )
