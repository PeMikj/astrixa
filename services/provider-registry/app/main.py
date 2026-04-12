import asyncio
import os
import sqlite3
import time
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, status
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
DB_PATH = os.getenv("PROVIDER_REGISTRY_DB_PATH", "/data/providers.db")
REQUEST_COUNT = Counter(
    "astrixa_provider_registry_requests_total",
    "Total provider registry requests",
    ["endpoint", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "astrixa_provider_registry_request_latency_seconds",
    "Provider registry request latency",
    ["endpoint"],
)
PROVIDER_HEALTH_STATE = Counter(
    "astrixa_provider_registry_health_transitions_total",
    "Provider health state transitions",
    ["provider_id", "health_status"],
)
HEALTH_CHECK_LATENCY = Histogram(
    "astrixa_provider_registry_health_check_latency_seconds",
    "Provider health check latency",
    ["provider_id"],
)
HEALTH_CHECK_FAILURES = Counter(
    "astrixa_provider_registry_health_check_failures_total",
    "Provider health check failures",
    ["provider_id"],
)

app = FastAPI(title="Astrixa Provider Registry", version="1.0.0")
_health_task: asyncio.Task | None = None


def configure_telemetry() -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "provider-registry"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)))
    trace.set_tracer_provider(provider)


configure_telemetry()
FastAPIInstrumentor.instrument_app(app)


class ProviderPrice(BaseModel):
    input_per_1k_tokens: float = 0.0
    output_per_1k_tokens: float = 0.0


class ProviderLimits(BaseModel):
    rpm: int | None = None
    tpm: int | None = None


class ProviderRecord(BaseModel):
    provider_id: str
    type: str
    base_url: str
    models: list[str]
    priority: int = 100
    weight: int = 1
    enabled: bool = True
    health_status: Literal["healthy", "degraded", "unhealthy"] = "healthy"
    health_source: Literal["default", "probe", "routing-feedback", "manual"] = "default"
    health_check_url: str | None = None
    ejected_until: float | None = None
    auth_type: str | None = None
    api_key_env: str | None = None
    last_check_at: float | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    price: ProviderPrice = Field(default_factory=ProviderPrice)
    limits: ProviderLimits = Field(default_factory=ProviderLimits)


class ProviderPatch(BaseModel):
    base_url: str | None = None
    models: list[str] | None = None
    priority: int | None = None
    weight: int | None = None
    enabled: bool | None = None
    health_status: Literal["healthy", "degraded", "unhealthy"] | None = None
    health_source: Literal["default", "probe", "routing-feedback", "manual"] | None = None
    health_check_url: str | None = None
    ejected_until: float | None = None
    auth_type: str | None = None
    api_key_env: str | None = None
    last_check_at: float | None = None
    consecutive_failures: int | None = None
    last_error: str | None = None


DEFAULT_PROVIDERS: dict[str, ProviderRecord] = {
    "mock-echo-primary": ProviderRecord(
        provider_id="mock-echo-primary",
        type="mock",
        base_url="http://mock-llm:8080",
        models=["mock-1", "mock-secure-1"],
        priority=100,
        weight=1,
        health_check_url="http://mock-llm:8080/healthz",
    )
}


def _maybe_add_openai_compatible_provider(
    provider_id: str,
    *,
    base_url_env: str,
    api_key_env: str,
    model_env: str,
    priority: int,
) -> None:
    base_url = os.getenv(base_url_env)
    api_key = os.getenv(api_key_env)
    model = os.getenv(model_env)
    if not base_url or not api_key or not model:
        return

    DEFAULT_PROVIDERS[provider_id] = ProviderRecord(
        provider_id=provider_id,
        type="openai-compatible",
        base_url=base_url,
        models=[model],
        priority=priority,
        weight=1,
        auth_type="bearer",
        api_key_env=api_key_env,
    )


_maybe_add_openai_compatible_provider(
    "aicohort-research",
    base_url_env="AICOHORT_BASE_URL",
    api_key_env="AICOHORT_API_KEY",
    model_env="AICOHORT_MODEL",
    priority=80,
)
_maybe_add_openai_compatible_provider(
    "mistral-primary",
    base_url_env="MISTRAL_BASE_URL",
    api_key_env="MISTRAL_API_KEY",
    model_env="MISTRAL_MODEL",
    priority=85,
)
PROVIDERS: dict[str, ProviderRecord] = {}


class ProviderHealthEvent(BaseModel):
    provider_id: str
    outcome: Literal["success", "error"]
    latency_seconds: float | None = None
    source: Literal["routing-feedback", "probe", "manual"] = "routing-feedback"
    cooldown_seconds: float = 30.0
    error_message: str | None = None


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
    return {"status": "ok", "service": "provider-registry"}


@app.get("/readyz")
async def readyz():
    return {"status": "ready", "service": "provider-registry"}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS providers (
            provider_id TEXT PRIMARY KEY,
            record_json TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _save_provider(provider: ProviderRecord) -> None:
    connection = _get_db()
    try:
        connection.execute(
            "INSERT OR REPLACE INTO providers (provider_id, record_json) VALUES (?, ?)",
            (provider.provider_id, provider.model_dump_json()),
        )
        connection.commit()
    finally:
        connection.close()


def _load_providers() -> dict[str, ProviderRecord]:
    connection = _get_db()
    try:
        rows = connection.execute("SELECT provider_id, record_json FROM providers").fetchall()
    finally:
        connection.close()

    if not rows:
        for provider in DEFAULT_PROVIDERS.values():
            _save_provider(provider)
        return dict(DEFAULT_PROVIDERS)

    loaded: dict[str, ProviderRecord] = {}
    for provider_id, record_json in rows:
        loaded[provider_id] = ProviderRecord.model_validate_json(record_json)
    for provider_id, provider in DEFAULT_PROVIDERS.items():
        if provider_id not in loaded:
            loaded[provider_id] = provider
            _save_provider(provider)
    return loaded


async def _probe_provider(provider: ProviderRecord) -> None:
    if not provider.health_check_url:
        return

    started = time.perf_counter()
    now = time.time()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(provider.health_check_url)
            response.raise_for_status()
        HEALTH_CHECK_LATENCY.labels(provider_id=provider.provider_id).observe(time.perf_counter() - started)
        previous_status = provider.health_status
        provider.health_status = "healthy"
        provider.health_source = "probe"
        provider.ejected_until = None
        provider.consecutive_failures = 0
        provider.last_error = None
        provider.last_check_at = now
        _save_provider(provider)
        if previous_status != provider.health_status:
            PROVIDER_HEALTH_STATE.labels(
                provider_id=provider.provider_id,
                health_status=provider.health_status,
            ).inc()
    except Exception as exc:
        HEALTH_CHECK_FAILURES.labels(provider_id=provider.provider_id).inc()
        provider.consecutive_failures += 1
        provider.last_check_at = now
        provider.last_error = str(exc)
        previous_status = provider.health_status
        provider.health_status = "degraded" if provider.consecutive_failures < 3 else "unhealthy"
        provider.health_source = "probe"
        provider.ejected_until = now if provider.health_status == "unhealthy" else provider.ejected_until
        _save_provider(provider)
        if previous_status != provider.health_status:
            PROVIDER_HEALTH_STATE.labels(
                provider_id=provider.provider_id,
                health_status=provider.health_status,
            ).inc()


async def _health_probe_loop() -> None:
    while True:
        await asyncio.gather(*(_probe_provider(provider) for provider in PROVIDERS.values()))
        await asyncio.sleep(15)


@app.on_event("startup")
async def startup_event() -> None:
    global _health_task, PROVIDERS
    PROVIDERS = _load_providers()
    _health_task = asyncio.create_task(_health_probe_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global _health_task
    if _health_task is not None:
        _health_task.cancel()
        _health_task = None


@app.get("/v1/providers")
async def list_providers():
    return {"items": [provider.model_dump() for provider in PROVIDERS.values()]}


@app.post("/v1/providers", status_code=status.HTTP_201_CREATED)
async def create_provider(provider: ProviderRecord):
    if provider.provider_id in PROVIDERS:
        raise HTTPException(status_code=409, detail="provider already exists")
    PROVIDERS[provider.provider_id] = provider
    _save_provider(provider)
    return provider.model_dump()


@app.get("/v1/providers/{provider_id}")
async def get_provider(provider_id: str):
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return provider.model_dump()


@app.patch("/v1/providers/{provider_id}")
async def patch_provider(provider_id: str, patch: ProviderPatch):
    existing = PROVIDERS.get(provider_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="provider not found")
    updated = existing.model_copy(update=patch.model_dump(exclude_none=True))
    PROVIDERS[provider_id] = updated
    _save_provider(updated)
    return updated.model_dump()


@app.post("/v1/providers/{provider_id}/health")
async def update_provider_health(provider_id: str, event: ProviderHealthEvent):
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="provider not found")

    now = time.time()
    previous_status = provider.health_status
    provider.last_check_at = now
    provider.health_source = event.source

    if event.outcome == "success":
        provider.health_status = "healthy"
        provider.ejected_until = None
        provider.consecutive_failures = max(0, provider.consecutive_failures - 1)
        provider.last_error = None
    else:
        provider.consecutive_failures += 1
        provider.last_error = event.error_message or "provider_error"
        provider.ejected_until = now + event.cooldown_seconds
        provider.health_status = "degraded" if provider.consecutive_failures < 3 else "unhealthy"

    PROVIDERS[provider_id] = provider
    _save_provider(provider)

    if previous_status != provider.health_status:
        PROVIDER_HEALTH_STATE.labels(
            provider_id=provider.provider_id,
            health_status=provider.health_status,
        ).inc()

    return provider.model_dump()
