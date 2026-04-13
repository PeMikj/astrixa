import asyncio
import os
import time
import uuid
import json
from urllib.parse import urlsplit, urlunsplit
from typing import Any, AsyncIterator
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field


ROUTING_ENGINE_URL = os.getenv("ROUTING_ENGINE_URL", "http://routing-engine:8080")
GUARDRAILS_ENGINE_URL = os.getenv("GUARDRAILS_ENGINE_URL", "http://guardrails-engine:8080")
AUTH_LAYER_URL = os.getenv("AUTH_LAYER_URL", "http://auth-layer:8080")
ANONYMIZATION_ENGINE_URL = os.getenv("ANONYMIZATION_ENGINE_URL", "http://anonymization-engine:8080")
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
MLFLOW_TRACKING_URL = os.getenv("MLFLOW_TRACKING_URL", "").rstrip("/")
MLFLOW_EXPERIMENT_NAME = os.getenv("MLFLOW_EXPERIMENT_NAME", "Astrixa Gateway")
DEFAULT_ANONYMIZATION_MODE = os.getenv("ASTRIXA_DEFAULT_ANONYMIZATION_MODE", "on").lower()
DEFAULT_ANONYMIZATION_PROFILE = os.getenv("ASTRIXA_DEFAULT_ANONYMIZATION_PROFILE", "pii-lite").lower()
MAX_ACTIVE_REQUESTS = max(1, int(os.getenv("ASTRIXA_MAX_ACTIVE_REQUESTS", "200")))
DEFAULT_PROVIDER_MAX_CONCURRENCY = max(1, int(os.getenv("ASTRIXA_DEFAULT_PROVIDER_MAX_CONCURRENCY", "50")))
DEFAULT_SUBJECT_RATE_LIMIT_RPM = max(1, int(os.getenv("ASTRIXA_DEFAULT_SUBJECT_RATE_LIMIT_RPM", "120")))
PROVIDER_MAX_CONCURRENCY_JSON = os.getenv("ASTRIXA_PROVIDER_MAX_CONCURRENCY_JSON", "{}")
_MLFLOW_EXPERIMENT_ID: str | None = None

REQUEST_COUNT = Counter(
    "astrixa_gateway_requests_total",
    "Total gateway requests",
    ["endpoint", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "astrixa_gateway_request_latency_seconds",
    "Gateway request latency",
    ["endpoint"],
)
TTFT_LATENCY = Histogram(
    "astrixa_gateway_ttft_seconds",
    "Gateway time to first token/byte",
    ["provider_id"],
)
TPOT_LATENCY = Histogram(
    "astrixa_gateway_tpot_seconds",
    "Gateway time per output token",
    ["provider_id"],
)
INPUT_TOKENS = Counter(
    "astrixa_gateway_input_tokens_total",
    "Observed input tokens from provider usage",
    ["provider_id"],
)
OUTPUT_TOKENS = Counter(
    "astrixa_gateway_output_tokens_total",
    "Observed output tokens from provider usage",
    ["provider_id"],
)
REQUEST_COST_USD = Counter(
    "astrixa_gateway_request_cost_usd_total",
    "Observed upstream request cost in USD",
    ["provider_id"],
)
POLICY_PROFILE_COUNT = Counter(
    "astrixa_gateway_policy_profile_requests_total",
    "Gateway requests by applied security policy profile",
    ["policy_profile"],
)
ANONYMIZATION_MODE_COUNT = Counter(
    "astrixa_gateway_anonymization_mode_requests_total",
    "Gateway requests by applied anonymization mode",
    ["anonymization_mode"],
)
ANONYMIZATION_PROFILE_COUNT = Counter(
    "astrixa_gateway_anonymization_profile_requests_total",
    "Gateway requests by applied anonymization profile",
    ["anonymization_profile"],
)
ACTIVE_REQUESTS = Gauge(
    "astrixa_gateway_active_requests",
    "Currently active gateway requests",
)
PROVIDER_ACTIVE_REQUESTS = Gauge(
    "astrixa_gateway_provider_active_requests",
    "Currently active upstream requests per provider",
    ["provider_id"],
)
REJECTED_REQUESTS = Counter(
    "astrixa_gateway_rejected_requests_total",
    "Gateway request rejections by admission control reason",
    ["reason"],
)
RATE_LIMITED_REQUESTS = Counter(
    "astrixa_gateway_rate_limited_requests_total",
    "Gateway requests rejected by subject rate limiting",
    ["subject_type"],
)

app = FastAPI(title="Astrixa API Gateway", version="1.0.0")


def _load_provider_concurrency_limits() -> dict[str, int]:
    try:
        raw = json.loads(PROVIDER_MAX_CONCURRENCY_JSON)
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    limits: dict[str, int] = {}
    for provider_id, value in raw.items():
        try:
            limits[str(provider_id)] = max(1, int(value))
        except (TypeError, ValueError):
            continue
    return limits


PROVIDER_CONCURRENCY_LIMITS = _load_provider_concurrency_limits()


def configure_telemetry() -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "api-gateway"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)))
    trace.set_tracer_provider(provider)


configure_telemetry()
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()


class Message(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[Message]
    stream: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class _GatewayCapacityLease:
    def __init__(self, limiter: "_ActiveCapacityLimiter", provider_id: str | None = None) -> None:
        self._limiter = limiter
        self._provider_id = provider_id
        self._released = False

    async def bind_provider(self, provider_id: str) -> bool:
        if self._provider_id is not None:
            return self._provider_id == provider_id
        acquired = await self._limiter.acquire_provider(provider_id)
        if acquired:
            self._provider_id = provider_id
        return acquired

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._limiter.release(provider_id=self._provider_id)


class _ActiveCapacityLimiter:
    def __init__(self, *, global_limit: int, default_provider_limit: int, provider_limits: dict[str, int]) -> None:
        self._global_limit = global_limit
        self._default_provider_limit = default_provider_limit
        self._provider_limits = provider_limits
        self._lock = asyncio.Lock()
        self._active_global = 0
        self._active_by_provider: dict[str, int] = defaultdict(int)

    def _provider_limit(self, provider_id: str) -> int:
        return self._provider_limits.get(provider_id, self._default_provider_limit)

    async def acquire_global(self) -> _GatewayCapacityLease | None:
        async with self._lock:
            if self._active_global >= self._global_limit:
                return None
            self._active_global += 1
            ACTIVE_REQUESTS.set(self._active_global)
            return _GatewayCapacityLease(self)

    async def acquire_provider(self, provider_id: str) -> bool:
        async with self._lock:
            if self._active_by_provider[provider_id] >= self._provider_limit(provider_id):
                return False
            self._active_by_provider[provider_id] += 1
            PROVIDER_ACTIVE_REQUESTS.labels(provider_id=provider_id).set(self._active_by_provider[provider_id])
            return True

    async def release(self, *, provider_id: str | None = None) -> None:
        async with self._lock:
            self._active_global = max(0, self._active_global - 1)
            ACTIVE_REQUESTS.set(self._active_global)
            if provider_id is not None:
                current = max(0, self._active_by_provider[provider_id] - 1)
                self._active_by_provider[provider_id] = current
                PROVIDER_ACTIVE_REQUESTS.labels(provider_id=provider_id).set(current)


class _SubjectRateLimiter:
    def __init__(self, *, requests_per_minute: int) -> None:
        self._requests_per_minute = requests_per_minute
        self._window_seconds = 60.0
        self._lock = asyncio.Lock()
        self._entries: dict[str, deque[float]] = defaultdict(deque)

    async def allow(self, subject_key: str) -> tuple[bool, int, float]:
        now = time.time()
        async with self._lock:
            entries = self._entries[subject_key]
            threshold = now - self._window_seconds
            while entries and entries[0] <= threshold:
                entries.popleft()
            if len(entries) >= self._requests_per_minute:
                retry_after = max(1.0, entries[0] + self._window_seconds - now)
                return (False, self._requests_per_minute, retry_after)
            entries.append(now)
            return (True, self._requests_per_minute, self._window_seconds)


CAPACITY_LIMITER = _ActiveCapacityLimiter(
    global_limit=MAX_ACTIVE_REQUESTS,
    default_provider_limit=DEFAULT_PROVIDER_MAX_CONCURRENCY,
    provider_limits=PROVIDER_CONCURRENCY_LIMITS,
)
SUBJECT_RATE_LIMITER = _SubjectRateLimiter(requests_per_minute=DEFAULT_SUBJECT_RATE_LIMIT_RPM)


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
    return {"status": "ok", "service": "api-gateway"}


@app.get("/readyz")
async def readyz():
    return {"status": "ready", "service": "api-gateway"}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


async def _resolve_provider(payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(f"{ROUTING_ENGINE_URL}/v1/route", json=payload)
        response.raise_for_status()
        return response.json()


async def _report_provider_feedback(
    provider_id: str,
    latency_seconds: float,
    outcome: str,
    error_message: str | None = None,
) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{ROUTING_ENGINE_URL}/v1/provider-feedback",
                json={
                    "provider_id": provider_id,
                    "latency_seconds": latency_seconds,
                    "outcome": outcome,
                    "error_message": error_message,
                },
            )
    except httpx.HTTPError:
        pass


async def _check_guardrails(payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(f"{GUARDRAILS_ENGINE_URL}/v1/guardrails/check", json=payload)
        response.raise_for_status()
        return response.json()


async def _check_response_guardrails(
    *,
    provider_id: str,
    body: Any,
    stream: bool,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{GUARDRAILS_ENGINE_URL}/v1/guardrails/check-response",
            json={
                "provider_id": provider_id,
                "body": body,
                "stream": stream,
                "metadata": metadata,
            },
        )
        response.raise_for_status()
        return response.json()


async def _check_auth(authorization: str | None, agent_id: str | None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{AUTH_LAYER_URL}/v1/auth/validate",
            json={
                "authorization": authorization,
                "required_scope": "llm:invoke",
                "agent_id": agent_id,
            },
        )
        response.raise_for_status()
        return response.json()


async def _anonymize_request(payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(f"{ANONYMIZATION_ENGINE_URL}/v1/anonymize", json=payload)
        response.raise_for_status()
        return response.json()


async def _deanonymize_body(body: Any, replacements: list[dict[str, Any]], metadata: dict[str, Any]) -> Any:
    if not replacements:
        return body
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{ANONYMIZATION_ENGINE_URL}/v1/deanonymize",
            json={"body": body, "replacements": replacements, "metadata": metadata},
        )
        response.raise_for_status()
        return response.json()["restored_body"]


def _provider_endpoint(base_url: str, api_path: str) -> str:
    parts = urlsplit(base_url.rstrip("/"))
    normalized_base_path = parts.path.rstrip("/")
    normalized_api_path = api_path if api_path.startswith("/") else f"/{api_path}"

    if normalized_base_path and normalized_api_path.startswith(f"{normalized_base_path}/"):
        final_path = normalized_api_path
    elif normalized_base_path == normalized_api_path:
        final_path = normalized_api_path
    else:
        final_path = f"{normalized_base_path}{normalized_api_path}" if normalized_base_path else normalized_api_path

    return urlunsplit((parts.scheme, parts.netloc, final_path, "", ""))


async def _sanitize_sse_chunk(
    raw_chunk: bytes,
    provider_id: str,
    request_id: str,
    replacements: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> bytes:
    try:
        text = raw_chunk.decode("utf-8")
    except UnicodeDecodeError:
        return raw_chunk

    sanitized_parts: list[str] = []
    for part in text.split("\n\n"):
        if not part:
            continue
        if not part.startswith("data: "):
            sanitized_parts.append(part)
            continue
        payload = part[6:]
        if payload.strip() == "[DONE]":
            sanitized_parts.append(part)
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            sanitized_parts.append(part)
            continue
        verdict = await _check_response_guardrails(
            provider_id=provider_id,
            body=parsed,
            stream=True,
            metadata={"request_id": request_id, **metadata},
        )
        if verdict["decision"] == "block":
            error_event = {
                "error": {
                    "type": "response_guardrail_block",
                    "reason_code": verdict["reason_code"],
                    "policy_version": verdict["policy_version"],
                }
            }
            return f"data: {json.dumps(error_event)}\n\ndata: [DONE]\n\n".encode("utf-8")
        restored_body = await _deanonymize_body(verdict["sanitized_body"], replacements, metadata)
        sanitized_parts.append(f"data: {json.dumps(restored_body)}")

    if not sanitized_parts:
        return raw_chunk
    return ("\n\n".join(sanitized_parts) + "\n\n").encode("utf-8")


def _extract_usage_metrics(payload: dict[str, Any]) -> tuple[int, int, float]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return (0, 0, 0.0)

    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)
    cost = float(usage.get("cost", 0.0) or 0.0)
    return (input_tokens, output_tokens, cost)


def _observe_usage_metrics(provider_id: str, payload: dict[str, Any], latency_seconds: float) -> None:
    input_tokens, output_tokens, cost = _extract_usage_metrics(payload)
    if input_tokens > 0:
        INPUT_TOKENS.labels(provider_id=provider_id).inc(input_tokens)
    if output_tokens > 0:
        OUTPUT_TOKENS.labels(provider_id=provider_id).inc(output_tokens)
        TPOT_LATENCY.labels(provider_id=provider_id).observe(latency_seconds / max(output_tokens, 1))
    if cost > 0:
        REQUEST_COST_USD.labels(provider_id=provider_id).inc(cost)


def _header_value(value: Any, default: str = "unknown") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _subject_rate_key(auth_verdict: dict[str, Any], request_payload: dict[str, Any]) -> str:
    metadata = request_payload.get("metadata", {})
    agent_id = metadata.get("agent_id")
    if agent_id:
        return f"agent:{agent_id}"
    subject = auth_verdict.get("subject")
    if subject:
        return f"subject:{subject}"
    subject_type = auth_verdict.get("subject_type") or "unknown"
    return f"{subject_type}:anonymous"


def _rejection_response(
    *,
    status_code: int,
    error_type: str,
    reason_code: str,
    auth_verdict: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> JSONResponse:
    headers = {
        "X-Astrixa-Auth-Decision": _header_value(auth_verdict.get("decision"), "unknown"),
        "X-Astrixa-Auth-Subject-Type": _header_value(auth_verdict.get("subject_type")),
    }
    if extra_headers:
        headers.update(extra_headers)
    payload = {
        "error": {
            "type": error_type,
            "reason_code": reason_code,
        }
    }
    if extra_payload:
        payload["error"].update(extra_payload)
    return JSONResponse(payload, status_code=status_code, headers=headers)


async def _mlflow_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=3.0) as client:
        response = await client.request(
            method,
            f"{MLFLOW_TRACKING_URL}{path}",
            json=payload,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()


async def _get_or_create_mlflow_experiment_id() -> str | None:
    global _MLFLOW_EXPERIMENT_ID
    if not MLFLOW_TRACKING_URL:
        return None
    if _MLFLOW_EXPERIMENT_ID is not None:
        return _MLFLOW_EXPERIMENT_ID
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"{MLFLOW_TRACKING_URL}/api/2.0/mlflow/experiments/get-by-name",
                params={"experiment_name": MLFLOW_EXPERIMENT_NAME},
            )
        if response.status_code == 200:
            experiment = response.json().get("experiment") or {}
            _MLFLOW_EXPERIMENT_ID = experiment.get("experiment_id")
            return _MLFLOW_EXPERIMENT_ID
        if response.status_code != 404:
            return None
        created = await _mlflow_request(
            "POST",
            "/api/2.0/mlflow/experiments/create",
            {"name": MLFLOW_EXPERIMENT_NAME},
        )
        _MLFLOW_EXPERIMENT_ID = created.get("experiment_id")
        return _MLFLOW_EXPERIMENT_ID
    except httpx.HTTPError:
        return None


async def _log_mlflow_run(
    *,
    request_id: str,
    request_payload: dict[str, Any],
    provider: dict[str, Any],
    route: dict[str, Any],
    auth_verdict: dict[str, Any],
    total_latency_seconds: float,
    outcome: str,
    ttft_seconds: float | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    error_message: str | None = None,
) -> None:
    experiment_id = await _get_or_create_mlflow_experiment_id()
    if experiment_id is None:
        return

    timestamp_ms = int(time.time() * 1000)
    agent_id = request_payload.get("metadata", {}).get("agent_id")
    try:
        created = await _mlflow_request(
            "POST",
            "/api/2.0/mlflow/runs/create",
            {
                "experiment_id": experiment_id,
                "run_name": request_id,
                "tags": [
                    {"key": "service", "value": "api-gateway"},
                    {"key": "provider_id", "value": str(provider.get("provider_id", "unknown"))},
                    {"key": "routing_strategy", "value": str(route.get("strategy", "unknown"))},
                    {"key": "auth_subject", "value": str(auth_verdict.get("subject") or "unknown")},
                    {"key": "auth_subject_type", "value": str(auth_verdict.get("subject_type") or "unknown")},
                    {"key": "agent_id", "value": str(agent_id or "none")},
                    {"key": "outcome", "value": outcome},
                ],
            },
        )
        run_id = ((created.get("run") or {}).get("info") or {}).get("run_id")
        if not run_id:
            return

        metrics = [
            {"key": "total_latency_seconds", "value": float(total_latency_seconds), "timestamp": timestamp_ms, "step": 0},
            {"key": "input_tokens", "value": float(input_tokens), "timestamp": timestamp_ms, "step": 0},
            {"key": "output_tokens", "value": float(output_tokens), "timestamp": timestamp_ms, "step": 0},
            {"key": "cost_usd", "value": float(cost_usd), "timestamp": timestamp_ms, "step": 0},
        ]
        if ttft_seconds is not None:
            metrics.append({"key": "ttft_seconds", "value": float(ttft_seconds), "timestamp": timestamp_ms, "step": 0})
        if output_tokens > 0:
            metrics.append(
                {
                    "key": "tpot_seconds",
                    "value": float(total_latency_seconds / max(output_tokens, 1)),
                    "timestamp": timestamp_ms,
                    "step": 0,
                }
            )

        params = [
            {"key": "model", "value": str(request_payload.get("model", ""))},
            {"key": "stream", "value": str(bool(request_payload.get("stream", False))).lower()},
            {"key": "provider_type", "value": str(provider.get("type", "unknown"))},
        ]
        if error_message:
            params.append({"key": "error_message", "value": error_message[:500]})

        await _mlflow_request(
            "POST",
            "/api/2.0/mlflow/runs/log-batch",
            {"run_id": run_id, "metrics": metrics, "params": params, "tags": []},
        )
        await _mlflow_request(
            "POST",
            "/api/2.0/mlflow/runs/update",
            {
                "run_id": run_id,
                "status": "FINISHED" if outcome == "success" else "FAILED",
                "end_time": timestamp_ms,
            },
        )
    except httpx.HTTPError:
        return


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest, raw_request: Request):
    lease = await CAPACITY_LIMITER.acquire_global()
    if lease is None:
        REJECTED_REQUESTS.labels(reason="global_concurrency").inc()
        return _rejection_response(
            status_code=503,
            error_type="admission_rejected",
            reason_code="global_capacity_exhausted",
            auth_verdict={"decision": "unknown", "subject_type": "unknown"},
            extra_headers={
                "Retry-After": "1",
                "X-Astrixa-Admission-Decision": "reject",
                "X-Astrixa-Admission-Reason": "global_capacity_exhausted",
            },
        )

    streaming_response = False
    try:
        request_payload = request.model_dump()
        agent_id = raw_request.headers.get("x-astrixa-agent-id") or request.metadata.get("agent_id")
        if agent_id and "agent_id" not in request_payload["metadata"]:
            request_payload["metadata"]["agent_id"] = agent_id
        auth_verdict = await _check_auth(raw_request.headers.get("authorization"), agent_id)
        if auth_verdict["decision"] != "allow":
            return _rejection_response(
                status_code=401,
                error_type="auth_denied",
                reason_code=auth_verdict["reason_code"],
                auth_verdict=auth_verdict,
                extra_headers={"X-Astrixa-Auth-Reason": auth_verdict["reason_code"]},
            )

        subject_key = _subject_rate_key(auth_verdict, request_payload)
        allowed, limit, retry_after = await SUBJECT_RATE_LIMITER.allow(subject_key)
        if not allowed:
            RATE_LIMITED_REQUESTS.labels(subject_type=_header_value(auth_verdict.get("subject_type"))).inc()
            REJECTED_REQUESTS.labels(reason="subject_rate_limit").inc()
            return _rejection_response(
                status_code=429,
                error_type="rate_limit_exceeded",
                reason_code="subject_rate_limit_exceeded",
                auth_verdict=auth_verdict,
                extra_headers={
                    "Retry-After": str(int(retry_after)),
                    "X-Astrixa-Admission-Decision": "reject",
                    "X-Astrixa-Admission-Reason": "subject_rate_limit_exceeded",
                    "X-Astrixa-RateLimit-Key": subject_key,
                    "X-Astrixa-RateLimit-Limit": str(limit),
                },
                extra_payload={"subject_key": subject_key, "limit_rpm": limit},
            )

        policy_profile = (
            raw_request.headers.get("x-astrixa-policy-profile")
            or auth_verdict.get("policy_profile")
            or request_payload["metadata"].get("policy_profile")
            or "balanced"
        )
        request_payload["metadata"]["policy_profile"] = str(policy_profile).lower()
        POLICY_PROFILE_COUNT.labels(policy_profile=request_payload["metadata"]["policy_profile"]).inc()

        anonymization_mode = (
            raw_request.headers.get("x-astrixa-anonymization-mode")
            or request_payload["metadata"].get("anonymization_mode")
            or DEFAULT_ANONYMIZATION_MODE
        )
        request_payload["metadata"]["anonymization_mode"] = "off" if str(anonymization_mode).lower() == "off" else "on"
        ANONYMIZATION_MODE_COUNT.labels(anonymization_mode=request_payload["metadata"]["anonymization_mode"]).inc()

        anonymization_profile = (
            raw_request.headers.get("x-astrixa-anonymization-profile")
            or auth_verdict.get("anonymization_profile")
            or request_payload["metadata"].get("anonymization_profile")
            or DEFAULT_ANONYMIZATION_PROFILE
        )
        request_payload["metadata"]["anonymization_profile"] = str(anonymization_profile).lower()
        ANONYMIZATION_PROFILE_COUNT.labels(
            anonymization_profile=request_payload["metadata"]["anonymization_profile"]
        ).inc()

        for header_name, metadata_key in (
            ("x-astrixa-anonymization-include", "anonymization_entities_include"),
            ("x-astrixa-anonymization-exclude", "anonymization_entities_exclude"),
            ("x-astrixa-anonymization-restore-include", "anonymization_restore_include"),
            ("x-astrixa-anonymization-restore-exclude", "anonymization_restore_exclude"),
        ):
            header_value = raw_request.headers.get(header_name)
            if header_value:
                request_payload["metadata"][metadata_key] = header_value

        verdict = await _check_guardrails(request_payload)
        if verdict["decision"] != "allow":
            return _rejection_response(
                status_code=403,
                error_type="guardrail_block",
                reason_code=verdict["reason_code"],
                auth_verdict=auth_verdict,
                extra_headers={
                    "X-Astrixa-Guardrails-Decision": verdict["decision"],
                    "X-Astrixa-Guardrails-Reason": verdict["reason_code"],
                    "X-Astrixa-Guardrails-Policy": verdict["policy_version"],
                },
                extra_payload={
                    "reasons": verdict["reasons"],
                    "policy_version": verdict["policy_version"],
                },
            )

        anonymization = await _anonymize_request(request_payload)
        provider_payload = dict(request_payload)
        provider_payload["messages"] = anonymization["sanitized_messages"]
        replacements = anonymization.get("replacements", [])

        route = await _resolve_provider(provider_payload)
        provider = route["provider"]
        if not await lease.bind_provider(provider["provider_id"]):
            REJECTED_REQUESTS.labels(reason="provider_concurrency").inc()
            return _rejection_response(
                status_code=503,
                error_type="admission_rejected",
                reason_code="provider_capacity_exhausted",
                auth_verdict=auth_verdict,
                extra_headers={
                    "Retry-After": "1",
                    "X-Astrixa-Admission-Decision": "reject",
                    "X-Astrixa-Admission-Reason": "provider_capacity_exhausted",
                    "X-Astrixa-Provider": provider["provider_id"],
                },
                extra_payload={"provider_id": provider["provider_id"]},
            )

        completion_url = _provider_endpoint(provider["base_url"], "/v1/chat/completions")
        request_id = f"chatcmpl_{uuid.uuid4().hex}"
        upstream_headers: dict[str, str] = {}

        api_key_env = provider.get("api_key_env")
        auth_type = provider.get("auth_type")
        if api_key_env and auth_type == "bearer":
            api_key = os.getenv(api_key_env)
            if not api_key:
                raise HTTPException(status_code=500, detail=f"missing provider credential env var: {api_key_env}")
            upstream_headers["Authorization"] = f"Bearer {api_key}"

        if request.stream:
            async def event_stream() -> AsyncIterator[bytes]:
                started = time.perf_counter()
                outcome = "success"
                first_chunk_at: float | None = None
                approx_output_tokens = 0
                error_message: str | None = None
                try:
                    async with httpx.AsyncClient(timeout=None) as client:
                        async with client.stream(
                            "POST",
                            completion_url,
                            headers=upstream_headers,
                            json=provider_payload,
                        ) as upstream:
                            if upstream.status_code >= 400:
                                outcome = "error"
                                body = await upstream.aread()
                                error_message = body.decode("utf-8")
                                await _report_provider_feedback(
                                    provider["provider_id"],
                                    time.perf_counter() - started,
                                    outcome,
                                    error_message,
                                )
                                await _log_mlflow_run(
                                    request_id=request_id,
                                    request_payload=request_payload,
                                    provider=provider,
                                    route=route,
                                    auth_verdict=auth_verdict,
                                    total_latency_seconds=time.perf_counter() - started,
                                    outcome=outcome,
                                    error_message=error_message,
                                )
                                raise HTTPException(status_code=upstream.status_code, detail=error_message)
                            async for chunk in upstream.aiter_bytes():
                                if first_chunk_at is None:
                                    first_chunk_at = time.perf_counter()
                                    TTFT_LATENCY.labels(provider_id=provider["provider_id"]).observe(first_chunk_at - started)
                                approx_output_tokens += max(chunk.count(b"content"), 1)
                                yield await _sanitize_sse_chunk(
                                    chunk,
                                    provider["provider_id"],
                                    request_id,
                                    replacements,
                                    request_payload["metadata"],
                                )
                    total_latency = time.perf_counter() - started
                    if first_chunk_at is None:
                        TTFT_LATENCY.labels(provider_id=provider["provider_id"]).observe(total_latency)
                    if approx_output_tokens > 0:
                        TPOT_LATENCY.labels(provider_id=provider["provider_id"]).observe(total_latency / approx_output_tokens)
                    await _report_provider_feedback(
                        provider["provider_id"],
                        total_latency,
                        outcome,
                    )
                    await _log_mlflow_run(
                        request_id=request_id,
                        request_payload=request_payload,
                        provider=provider,
                        route=route,
                        auth_verdict=auth_verdict,
                        total_latency_seconds=total_latency,
                        outcome=outcome,
                        ttft_seconds=(first_chunk_at - started) if first_chunk_at is not None else total_latency,
                        output_tokens=approx_output_tokens,
                        error_message=error_message,
                    )
                finally:
                    await lease.release()

            headers = {
                "X-Astrixa-Auth-Decision": auth_verdict["decision"],
                "X-Astrixa-Auth-Subject-Type": _header_value(auth_verdict.get("subject_type")),
                "X-Astrixa-Provider": provider["provider_id"],
                "X-Astrixa-Strategy": route["strategy"],
                "X-Astrixa-Request-Id": request_id,
                "X-Astrixa-Guardrails-Decision": verdict["decision"],
                "X-Astrixa-Guardrails-Policy": verdict["policy_version"],
                "X-Astrixa-Anonymization-Applied": "true" if replacements else "false",
                "X-Astrixa-Anonymization-Decision": anonymization["decision"],
                "X-Astrixa-Anonymization-Mode": request_payload["metadata"]["anonymization_mode"],
                "X-Astrixa-Anonymization-Profile": anonymization["anonymization_profile"],
                "X-Astrixa-Anonymization-Policy": anonymization["policy_version"],
                "X-Astrixa-Anonymization-Replacements": str(len(replacements)),
                "X-Astrixa-Policy-Profile": request_payload["metadata"]["policy_profile"],
                "X-Astrixa-Admission-Decision": "allow",
                "X-Astrixa-RateLimit-Key": subject_key,
                "X-Astrixa-RateLimit-Limit": str(limit),
            }
            streaming_response = True
            return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)

        started = time.perf_counter()
        outcome = "success"
        async with httpx.AsyncClient(timeout=60.0) as client:
            upstream = await client.post(
                completion_url,
                headers=upstream_headers,
                json=provider_payload,
            )
            if upstream.status_code >= 400:
                outcome = "error"
                await _report_provider_feedback(
                    provider["provider_id"],
                    time.perf_counter() - started,
                    outcome,
                    upstream.text,
                )
                await _log_mlflow_run(
                    request_id=request_id,
                    request_payload=request_payload,
                    provider=provider,
                    route=route,
                    auth_verdict=auth_verdict,
                    total_latency_seconds=time.perf_counter() - started,
                    outcome=outcome,
                    error_message=upstream.text,
                )
                raise HTTPException(status_code=upstream.status_code, detail=upstream.text)
            upstream_payload = upstream.json()
        total_latency = time.perf_counter() - started
        TTFT_LATENCY.labels(provider_id=provider["provider_id"]).observe(total_latency)
        response_verdict = await _check_response_guardrails(
            provider_id=provider["provider_id"],
            body=upstream_payload,
            stream=False,
            metadata={"request_id": request_id, **request_payload["metadata"]},
        )
        if response_verdict["decision"] == "block":
            await _log_mlflow_run(
                request_id=request_id,
                request_payload=request_payload,
                provider=provider,
                route=route,
                auth_verdict=auth_verdict,
                total_latency_seconds=total_latency,
                outcome="error",
                error_message=response_verdict["reason_code"],
            )
            return JSONResponse(
                {
                    "error": {
                        "type": "response_guardrail_block",
                        "reason_code": response_verdict["reason_code"],
                        "policy_version": response_verdict["policy_version"],
                    }
                },
                status_code=502,
                headers={
                    "X-Astrixa-Auth-Decision": auth_verdict["decision"],
                    "X-Astrixa-Auth-Subject-Type": _header_value(auth_verdict.get("subject_type")),
                    "X-Astrixa-Provider": provider["provider_id"],
                    "X-Astrixa-Strategy": route["strategy"],
                    "X-Astrixa-Request-Id": request_id,
                    "X-Astrixa-Guardrails-Decision": verdict["decision"],
                    "X-Astrixa-Guardrails-Policy": verdict["policy_version"],
                    "X-Astrixa-Response-Guardrails-Decision": response_verdict["decision"],
                    "X-Astrixa-Response-Guardrails-Policy": response_verdict["policy_version"],
                    "X-Astrixa-Anonymization-Applied": "true" if replacements else "false",
                    "X-Astrixa-Anonymization-Decision": anonymization["decision"],
                    "X-Astrixa-Anonymization-Mode": request_payload["metadata"]["anonymization_mode"],
                    "X-Astrixa-Anonymization-Profile": anonymization["anonymization_profile"],
                    "X-Astrixa-Anonymization-Policy": anonymization["policy_version"],
                    "X-Astrixa-Anonymization-Replacements": str(len(replacements)),
                    "X-Astrixa-Admission-Decision": "allow",
                    "X-Astrixa-RateLimit-Key": subject_key,
                    "X-Astrixa-RateLimit-Limit": str(limit),
                },
            )
        payload = await _deanonymize_body(response_verdict["sanitized_body"], replacements, request_payload["metadata"])
        _observe_usage_metrics(provider["provider_id"], payload, total_latency)
        await _report_provider_feedback(provider["provider_id"], total_latency, outcome)
        input_tokens, output_tokens, cost = _extract_usage_metrics(payload)
        await _log_mlflow_run(
            request_id=request_id,
            request_payload=request_payload,
            provider=provider,
            route=route,
            auth_verdict=auth_verdict,
            total_latency_seconds=total_latency,
            outcome=outcome,
            ttft_seconds=total_latency,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )

        payload["id"] = request_id
        payload["provider"] = provider["provider_id"]
        return JSONResponse(
            payload,
            headers={
                "X-Astrixa-Auth-Decision": auth_verdict["decision"],
                "X-Astrixa-Auth-Subject-Type": _header_value(auth_verdict.get("subject_type")),
                "X-Astrixa-Provider": provider["provider_id"],
                "X-Astrixa-Strategy": route["strategy"],
                "X-Astrixa-Request-Id": request_id,
                "X-Astrixa-Guardrails-Decision": verdict["decision"],
                "X-Astrixa-Guardrails-Policy": verdict["policy_version"],
                "X-Astrixa-Response-Guardrails-Decision": response_verdict["decision"],
                "X-Astrixa-Response-Guardrails-Policy": response_verdict["policy_version"],
                "X-Astrixa-Anonymization-Applied": "true" if replacements else "false",
                "X-Astrixa-Anonymization-Decision": anonymization["decision"],
                "X-Astrixa-Anonymization-Mode": request_payload["metadata"]["anonymization_mode"],
                "X-Astrixa-Anonymization-Profile": anonymization["anonymization_profile"],
                "X-Astrixa-Anonymization-Policy": anonymization["policy_version"],
                "X-Astrixa-Anonymization-Replacements": str(len(replacements)),
                "X-Astrixa-Policy-Profile": request_payload["metadata"]["policy_profile"],
                "X-Astrixa-Admission-Decision": "allow",
                "X-Astrixa-RateLimit-Key": subject_key,
                "X-Astrixa-RateLimit-Limit": str(limit),
            },
        )
    finally:
        if not streaming_response:
            await lease.release()
