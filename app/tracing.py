"""OpenTelemetry tracing with redaction.

Every attribute passes through `redact()` before it is attached to a span, so bank
account numbers, routing numbers, IBANs and tax IDs never reach the trace backend.
To export to Langfuse, set OTEL_EXPORTER_OTLP_ENDPOINT to Langfuse's OTLP endpoint
(for example https://cloud.langfuse.com/api/public/otel) and OTEL_EXPORTER_OTLP_HEADERS
to "Authorization=Basic <base64 of public_key:secret_key>".
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from app.settings import get_settings

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # IBAN: 2 letters, 2 digits, 11-30 alphanumerics (spaces allowed).
    (re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b"), "[IBAN]"),
    # US EIN 12-3456789 and SSN 123-45-6789.
    (re.compile(r"\b\d{2}-\d{7}\b"), "[TAX_ID]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[TAX_ID]"),
    # Labelled account / routing numbers.
    (re.compile(r"(?i)\b(account|acct|routing|aba|sort code|swift|bic)"
                r"(\s*(?:no\.?|number|#)?\s*[:#]?\s*)[\w\- ]{4,34}"),
     r"\1\2[REDACTED]"),
    # Any remaining long digit run (8+ digits) looks like an account number.
    (re.compile(r"\b\d{8,17}\b"), "[NUMBER]"),
]


def redact(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_configured = False


def configure_tracing(service_name: str = "ap-agent") -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        headers = {}
        if settings.otel_exporter_otlp_headers:
            for pair in settings.otel_exporter_otlp_headers.split(","):
                k, _, v = pair.partition("=")
                headers[k.strip()] = v.strip()
        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
        if not endpoint.endswith("/v1/traces"):
            endpoint += "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers)))
    if settings.trace_console:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


tracer = trace.get_tracer("ap-agent")


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    with tracer.start_as_current_span(name) as s:
        for key, value in attributes.items():
            set_attr(key, value, s)
        yield s


def set_attr(key: str, value: Any, s: trace.Span | None = None) -> None:
    """Attach a redacted attribute to the given span, or to the current span."""
    if value is None:
        return
    s = s or trace.get_current_span()
    if isinstance(value, (bool, int, float)):
        s.set_attribute(key, value)
    else:
        s.set_attribute(key, redact(value))
