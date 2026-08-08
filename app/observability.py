"""OpenTelemetry instrumentation for the pipeline, isolated in this module.

The application depends on OpenTelemetry only, never on a backend SDK: where the
traces land is a deployment decision (console while developing, any OTLP endpoint
— Langfuse, Phoenix, Datadog, ... — later) and not a code change.

Tracing is configured only when OBSERVABILITY_ENABLED=true. Without that setup
the OpenTelemetry API hands out non-recording spans, so every call here turns
into a no-op and the pipeline behaves exactly as before.

Only safe attributes are recorded: ids, counts, flags, filters and timings —
never the question, the prompts, the chunk contents, the API key or the database
URL.
"""

import json
from base64 import b64encode
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanProcessor,
)
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.re import parse_env_headers

from app.config import settings

TRACER_NAME = "app.rag"

OTLP_EXPORTER = "otlp"

# Where Langfuse receives OpenTelemetry traces, and the ingestion it should use.
LANGFUSE_OTEL_PATH = "/api/public/otel"
LANGFUSE_INGESTION_HEADER = "x-langfuse-ingestion-version"
LANGFUSE_INGESTION_VERSION = "4"

# OTLP endpoints are a base URL; each signal lives under its own path.
TRACES_PATH = "/v1/traces"

# Span names: one per pipeline step.
PIPELINE_SPAN = "ai.rag.pipeline"
QUERY_PLANNING_SPAN = "ai.rag.query_planning"
RETRIEVAL_SPAN = "ai.rag.retrieval"
RERANKING_SPAN = "ai.rag.reranking"
ANSWER_GENERATION_SPAN = "ai.rag.answer_generation"

# Attribute names live here so that no other file spells them out.
REQUEST_ID = "app.request_id"
FEATURE = "app.feature"
TENANT = "app.tenant"
PRODUCT = "app.product"
USE_RERANK = "app.use_rerank"
HAS_ANSWER = "app.has_answer"
NEEDS_CLARIFICATION = "app.needs_clarification"
SOURCES_COUNT = "app.sources_count"
NORMALIZED_QUESTION_LENGTH = "app.normalized_question_length"
DOC_TYPES = "app.doc_types"
PLAN = "app.plan"
EXACT_TERMS_COUNT = "app.exact_terms_count"
RETRIEVED_CHUNKS_COUNT = "app.retrieved_chunks_count"
FILTERS = "app.filters"
SELECTED_CHUNKS_COUNT = "app.selected_chunks_count"
TOTAL_TOKENS = "app.total_tokens"

# GenAI semantic conventions: the names the ecosystem already knows how to read.
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"


def duration_attribute(field: str) -> str:
    """How long a step took, named after the PipelineTimings field it fills."""
    return f"app.{field}"


_configured = False


def setup_tracing() -> None:
    """Configure the global tracer provider. Without it every span is a no-op."""
    global _configured
    if _configured or not settings.observability_enabled:
        return

    _configured = True
    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: settings.otel_service_name})
    )
    provider.add_span_processor(_build_processor())
    trace.set_tracer_provider(provider)


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


@contextmanager
def span(name: str) -> Iterator[Span]:
    """Open a span. Spans opened inside it become its children."""
    with get_tracer().start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as current:
        try:
            yield current
        except Exception as exc:
            # Error type and message only: no stack trace, no payload.
            current.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise


def set_attributes(span: Span, attributes: dict[str, Any]) -> None:
    """Record safe attributes. A value that does not exist is simply left out."""
    # With tracing disabled the span records nothing: leave without building anything.
    if not span.is_recording():
        return

    span.set_attributes(
        {
            name: _attribute_value(value)
            for name, value in attributes.items()
            if value is not None
        }
    )


def _attribute_value(value: Any) -> Any:
    # An attribute is a primitive or a sequence of primitives; a dict (the
    # retrieval filters) goes in as JSON.
    return json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value


def _build_processor() -> SpanProcessor:
    if settings.otel_traces_exporter == OTLP_EXPORTER:
        # Batched: the spans leave in one request, off the answer's critical path.
        return BatchSpanProcessor(_build_otlp_exporter())

    # Console: each span is printed as it ends, while you use the chat.
    return SimpleSpanProcessor(ConsoleSpanExporter())


def _build_otlp_exporter() -> OTLPSpanExporter:
    endpoint = settings.otel_exporter_otlp_endpoint
    headers = _configured_headers()

    # Langfuse is just an OTLP endpoint: with its keys filled in they decide the
    # destination, and nothing here imports a Langfuse SDK. Without them the generic
    # OTEL_EXPORTER_OTLP_* values are used as they are, for any OTLP backend.
    if settings.langfuse_public_key and settings.langfuse_secret_key:
        endpoint = settings.langfuse_host.rstrip("/") + LANGFUSE_OTEL_PATH
        headers.setdefault("authorization", f"Basic {_langfuse_credential()}")
        headers.setdefault(LANGFUSE_INGESTION_HEADER, LANGFUSE_INGESTION_VERSION)

    # Header names only: their values carry the credentials.
    names = ", ".join(sorted(headers)) or "none"
    print(f"Traces to {endpoint or 'the OTLP default endpoint'} (headers: {names}).")

    return OTLPSpanExporter(endpoint=_traces_url(endpoint), headers=headers or None)


def _configured_headers() -> dict[str, str]:
    if not settings.otel_exporter_otlp_headers:
        return {}

    # liberal: the standard asks for URL-encoded values, but an Authorization
    # header pasted by hand ("Basic abc...") has a space and would be dropped.
    return dict(parse_env_headers(settings.otel_exporter_otlp_headers, liberal=True))


def _langfuse_credential() -> str:
    keys = f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}"
    return b64encode(keys.encode()).decode()


def _traces_url(endpoint: str) -> str | None:
    # Without an endpoint the exporter falls back to its own OTEL_* defaults.
    if not endpoint:
        return None

    endpoint = endpoint.rstrip("/")
    return endpoint if endpoint.endswith(TRACES_PATH) else endpoint + TRACES_PATH
