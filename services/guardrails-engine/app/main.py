import os
import re
import time
from typing import Any, Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field


OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

REQUEST_COUNT = Counter(
    "astrixa_guardrails_requests_total",
    "Total guardrails requests",
    ["endpoint", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "astrixa_guardrails_request_latency_seconds",
    "Guardrails request latency",
    ["endpoint"],
)
VERDICT_COUNT = Counter(
    "astrixa_guardrails_verdict_total",
    "Guardrails verdict count",
    ["decision", "reason_code"],
)

app = FastAPI(title="Astrixa Guardrails Engine", version="1.0.0")


def configure_telemetry() -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "guardrails-engine"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)))
    trace.set_tracer_provider(provider)


configure_telemetry()
FastAPIInstrumentor.instrument_app(app)


class GuardrailMessage(BaseModel):
    role: str
    content: str


class GuardrailRequest(BaseModel):
    model: str
    messages: list[GuardrailMessage]
    metadata: dict[str, Any] = Field(default_factory=dict)


class GuardrailVerdict(BaseModel):
    decision: Literal["allow", "block"]
    reason_code: str
    reasons: list[str] = Field(default_factory=list)
    policy_version: str = "guardrails.v1"


PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+previous\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+all\s+prior\s+rules", re.IGNORECASE),
    re.compile(r"reveal\s+(your|the)\s+(system|hidden)\s+prompt", re.IGNORECASE),
]

SECRET_LEAK_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"rp-ak-[a-zA-Z0-9]+"),
    re.compile(r"api[_-]?key\s*[:=]\s*\S+", re.IGNORECASE),
]


@app.middleware("http")
async def instrument_requests(request: Request, call_next):
    endpoint = request.url.path
    started = time.perf_counter()
    response: Response | None = None
    try:
        response = await call_next(request)
        return response
    finally:
        elapsed = time.perf_counter() - started
        status_code = str(response.status_code if response is not None else 500)
        REQUEST_COUNT.labels(endpoint=endpoint, status_code=status_code).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint).observe(elapsed)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "guardrails-engine"}


@app.get("/readyz")
async def readyz():
    return {"status": "ready", "service": "guardrails-engine"}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/guardrails/check", response_model=GuardrailVerdict)
async def check_guardrails(payload: GuardrailRequest):
    combined_text = "\n".join(message.content for message in payload.messages)
    reasons: list[str] = []

    for pattern in PROMPT_INJECTION_PATTERNS:
        if pattern.search(combined_text):
            reasons.append("prompt_injection_detected")
            break

    for pattern in SECRET_LEAK_PATTERNS:
        if pattern.search(combined_text):
            reasons.append("secret_pattern_detected")
            break

    if reasons:
        verdict = GuardrailVerdict(
            decision="block",
            reason_code=reasons[0],
            reasons=reasons,
        )
    else:
        verdict = GuardrailVerdict(
            decision="allow",
            reason_code="ok",
            reasons=[],
        )

    VERDICT_COUNT.labels(decision=verdict.decision, reason_code=verdict.reason_code).inc()
    return verdict

