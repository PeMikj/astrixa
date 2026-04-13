import os
import time
import json
from typing import Literal

import httpx
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
AGENT_REGISTRY_URL = os.getenv("AGENT_REGISTRY_URL", "http://agent-registry:8080")
STATIC_TOKENS_JSON = os.getenv("ASTRIXA_STATIC_TOKENS_JSON", "")

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
    agent_id: str | None = None


class AuthVerdict(BaseModel):
    decision: Literal["allow", "deny"]
    reason_code: str
    subject: str | None = None
    subject_type: Literal["client", "agent", "service"] | None = None
    scopes: list[str] = Field(default_factory=list)
    policy_profile: str = "balanced"
    anonymization_profile: str = "pii-lite"


def _load_static_tokens() -> dict[str, dict]:
    default_tokens = {
        ASTRIXA_GATEWAY_TOKEN: {
            "subject": "astrixa-dev-client",
            "subject_type": "client",
            "scopes": ["llm:invoke"],
            "policy_profile": "balanced",
            "anonymization_profile": "pii-lite",
        }
    }
    if not STATIC_TOKENS_JSON.strip():
        return default_tokens
    try:
        configured = json.loads(STATIC_TOKENS_JSON)
    except json.JSONDecodeError:
        return default_tokens
    if not isinstance(configured, dict):
        return default_tokens
    return {**default_tokens, **configured}


STATIC_TOKENS = _load_static_tokens()


async def _validate_agent_token(token: str, agent_id: str, required_scope: str) -> AuthVerdict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{AGENT_REGISTRY_URL}/v1/agents/{agent_id}")
            if response.status_code == 404:
                return AuthVerdict(decision="deny", reason_code="agent_not_found")
            response.raise_for_status()
            agent = response.json()
    except httpx.HTTPError:
        return AuthVerdict(decision="deny", reason_code="agent_registry_unavailable")

    auth = agent.get("auth") or {}
    token_env = auth.get("token_env")
    allowed_scopes = list(auth.get("scopes") or [])
    if not token_env:
        return AuthVerdict(decision="deny", reason_code="agent_auth_not_configured")
    expected_token = os.getenv(token_env, "").strip()
    if not expected_token:
        return AuthVerdict(decision="deny", reason_code="agent_token_env_missing")
    if token != expected_token:
        return AuthVerdict(decision="deny", reason_code="invalid_agent_token")
    if required_scope not in allowed_scopes:
        return AuthVerdict(decision="deny", reason_code="insufficient_scope")
    return AuthVerdict(
        decision="allow",
        reason_code="ok",
        subject=agent_id,
        subject_type="agent",
        scopes=allowed_scopes,
        policy_profile=str(agent.get("policy_profile") or "balanced"),
        anonymization_profile=str(agent.get("anonymization_profile") or "pii-lite"),
    )


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
        static_token = STATIC_TOKENS.get(token)
        if static_token is not None:
            scopes = list(static_token.get("scopes") or [])
            if payload.required_scope not in scopes:
                verdict = AuthVerdict(decision="deny", reason_code="insufficient_scope")
            else:
                verdict = AuthVerdict(
                    decision="allow",
                    reason_code="ok",
                    subject=static_token.get("subject"),
                    subject_type=static_token.get("subject_type", "client"),
                    scopes=scopes,
                    policy_profile=str(static_token.get("policy_profile") or "balanced"),
                    anonymization_profile=str(static_token.get("anonymization_profile") or "pii-lite"),
                )
        elif payload.agent_id:
            verdict = await _validate_agent_token(token, payload.agent_id, payload.required_scope)
        else:
            verdict = AuthVerdict(decision="deny", reason_code="invalid_token")

    VERDICT_COUNT.labels(decision=verdict.decision).inc()
    return verdict
