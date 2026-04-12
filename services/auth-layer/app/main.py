import os
import time
from typing import Literal

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
ASTRIXA_GATEWAY_TOKEN = os.getenv("ASTRIXA_GATEWAY_TOKEN", "astrixa-dev-token")

REQUEST_COUNT = Counter(
    "astrixa_auth_requests_total",
    "Total auth requests",
    ["endpoint", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "astrixa_auth_request_latency_seconds",
    "Auth request latency",
    ["endpoint"],
)
VERDICT_COUNT = Counter(
    "astrixa_auth_verdict_total",
    "Auth verdicts",
    ["decision"],
)

app = FastAPI(title="Astrixa Auth Layer", version="1.0.0")


def configure_telemetry() -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "auth-layer"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)))
    trace.set_tracer_provider(provider)


configure_telemetry()
FastAPIInstrumentor.instrument_app(app)


class AuthRequest(BaseModel):
    authorization: str | None = None
    required_scope: str = "llm:invoke"


class AuthVerdict(BaseModel):
    decision: Literal["allow", "deny"]
    reason_code: str
    subject: str | None = None
    scopes: list[str] = Field(default_factory=list)


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
    return {"status": "ok", "service": "auth-layer"}


@app.get("/readyz")
async def readyz():
    return {"status": "ready", "service": "auth-layer"}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/auth/validate", response_model=AuthVerdict)
async def validate_auth(payload: AuthRequest):
    authz = payload.authorization or ""
    prefix = "Bearer "
    if not authz.startswith(prefix):
        verdict = AuthVerdict(decision="deny", reason_code="missing_bearer_token")
    else:
        token = authz[len(prefix):].strip()
        if token != ASTRIXA_GATEWAY_TOKEN:
            verdict = AuthVerdict(decision="deny", reason_code="invalid_token")
        else:
            verdict = AuthVerdict(
                decision="allow",
                reason_code="ok",
                subject="astrixa-dev-client",
                scopes=["llm:invoke"],
            )

    VERDICT_COUNT.labels(decision=verdict.decision).inc()
    return verdict

