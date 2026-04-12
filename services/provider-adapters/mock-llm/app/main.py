import asyncio
import json
import os
import time
import uuid
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel


OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
REQUEST_COUNT = Counter(
    "astrixa_mock_provider_requests_total",
    "Total mock provider requests",
    ["endpoint", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "astrixa_mock_provider_request_latency_seconds",
    "Mock provider request latency",
    ["endpoint"],
)

app = FastAPI(title="Astrixa Mock LLM", version="1.0.0")


def configure_telemetry() -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "mock-llm"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)))
    trace.set_tracer_provider(provider)


configure_telemetry()
FastAPIInstrumentor.instrument_app(app)


class Message(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[Message]
    stream: bool = False


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
    return {"status": "ok", "service": "mock-llm"}


@app.get("/readyz")
async def readyz():
    return {"status": "ready", "service": "mock-llm"}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


def _build_output(messages: list[Message]) -> str:
    prompt = " ".join(message.content.strip() for message in messages if message.content.strip())
    return f"Astrixa mock provider response: {prompt}".strip()


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    content = _build_output(request.messages)

    if request.stream:
        async def event_stream() -> AsyncIterator[str]:
            for token in content.split():
                chunk = {
                    "id": f"mock_{uuid.uuid4().hex}",
                    "object": "chat.completion.chunk",
                    "model": request.model,
                    "choices": [{"delta": {"content": f"{token} "}}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.05)
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return JSONResponse(
        {
            "id": f"mock_{uuid.uuid4().hex}",
            "object": "chat.completion",
            "model": request.model,
            "output_text": content,
        }
    )
