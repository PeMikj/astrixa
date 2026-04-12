import os
import time
from collections import defaultdict
from threading import Lock
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


PROVIDER_REGISTRY_URL = os.getenv("PROVIDER_REGISTRY_URL", "http://provider-registry:8080")
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")

REQUEST_COUNT = Counter(
    "astrixa_routing_requests_total",
    "Total routing requests",
    ["endpoint", "status_code"],
)
ROUTING_LATENCY = Histogram(
    "astrixa_routing_request_latency_seconds",
    "Routing request latency",
    ["endpoint"],
)
PROVIDER_SELECTION_COUNT = Counter(
    "astrixa_routing_provider_selection_total",
    "Number of times each provider is selected",
    ["provider_id", "strategy"],
)
PROVIDER_OBSERVED_LATENCY = Histogram(
    "astrixa_routing_provider_observed_latency_seconds",
    "Observed upstream provider latency reported back to routing",
    ["provider_id"],
)
PROVIDER_HEALTH_EVENTS = Counter(
    "astrixa_routing_provider_health_events_total",
    "Provider health feedback events",
    ["provider_id", "outcome"],
)

_PROVIDER_STATS: dict[str, dict[str, float]] = {}
_LOCK = Lock()
_COOLDOWN_SECONDS = 30.0
_DEFAULT_LATENCY = 1.0

app = FastAPI(title="Astrixa Routing Engine", version="1.0.0")


def configure_telemetry() -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "routing-engine"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)))
    trace.set_tracer_provider(provider)


configure_telemetry()
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()


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
        ROUTING_LATENCY.labels(endpoint=endpoint).observe(elapsed)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "routing-engine"}


@app.get("/readyz")
async def readyz():
    return {"status": "ready", "service": "routing-engine"}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


def _provider_score(provider: dict[str, Any], now: float) -> tuple[float, float, int, str]:
    stats = _PROVIDER_STATS.get(provider["provider_id"], {})
    cooldown_until = stats.get("cooldown_until", 0.0)
    ema_latency = stats.get("ema_latency", _DEFAULT_LATENCY)
    error_count = int(stats.get("error_count", 0))
    registry_ejected_until = float(provider.get("ejected_until") or 0.0)
    is_cooling_down = cooldown_until > now or registry_ejected_until > now

    health_rank = {
        "healthy": 3,
        "degraded": 2,
        "unhealthy": 1,
    }.get(provider["health_status"], 0)
    effective_health = 0 if is_cooling_down else health_rank
    effective_priority = int(provider.get("priority", 0))
    effective_latency = ema_latency
    strategy = "latency-health-priority"
    return (effective_health, -effective_latency, effective_priority - error_count, strategy)


@app.post("/v1/route")
async def route_request(payload: dict[str, Any]):
    model = payload.get("model")
    if not model:
        raise HTTPException(status_code=400, detail="model is required")

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{PROVIDER_REGISTRY_URL}/v1/providers")
        response.raise_for_status()
        providers = response.json()["items"]

    candidates = [
        provider
        for provider in providers
        if provider["enabled"] and provider["health_status"] in {"healthy", "degraded"} and model in provider["models"]
    ]
    if not candidates:
        raise HTTPException(status_code=503, detail=f"no healthy provider for model '{model}'")

    now = time.time()
    scored_candidates = sorted(
        candidates,
        key=lambda provider: _provider_score(provider, now),
        reverse=True,
    )
    selected = scored_candidates[0]
    strategy = "latency-health-priority"
    PROVIDER_SELECTION_COUNT.labels(
        provider_id=selected["provider_id"],
        strategy=strategy,
    ).inc()
    return {
        "strategy": strategy,
        "provider": selected,
        "candidate_count": len(candidates),
    }


@app.post("/v1/provider-feedback")
async def provider_feedback(payload: dict[str, Any]):
    provider_id = payload.get("provider_id")
    if not provider_id:
        raise HTTPException(status_code=400, detail="provider_id is required")

    latency_seconds = float(payload.get("latency_seconds", 0.0))
    outcome = payload.get("outcome", "success")
    error_message = payload.get("error_message")
    now = time.time()

    with _LOCK:
        stats = _PROVIDER_STATS.setdefault(
            provider_id,
            {
                "ema_latency": _DEFAULT_LATENCY,
                "error_count": 0.0,
                "cooldown_until": 0.0,
            },
        )
        if latency_seconds > 0:
            stats["ema_latency"] = (0.7 * stats["ema_latency"]) + (0.3 * latency_seconds)
            PROVIDER_OBSERVED_LATENCY.labels(provider_id=provider_id).observe(latency_seconds)

        if outcome == "success":
            stats["error_count"] = max(0.0, stats["error_count"] - 1.0)
            stats["cooldown_until"] = 0.0
        else:
            stats["error_count"] += 1.0
            stats["cooldown_until"] = now + _COOLDOWN_SECONDS

    async with httpx.AsyncClient(timeout=10.0) as client:
        registry_response = await client.post(
            f"{PROVIDER_REGISTRY_URL}/v1/providers/{provider_id}/health",
            json={
                "provider_id": provider_id,
                "outcome": outcome,
                "latency_seconds": latency_seconds,
                "source": "routing-feedback",
                "cooldown_seconds": _COOLDOWN_SECONDS,
                "error_message": error_message,
            },
        )
        registry_response.raise_for_status()

    PROVIDER_HEALTH_EVENTS.labels(provider_id=provider_id, outcome=outcome).inc()
    return {
        "status": "ok",
        "provider_id": provider_id,
        "outcome": outcome,
    }
