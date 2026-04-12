import os
import time

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
REQUEST_COUNT = Counter(
    "astrixa_agent_registry_requests_total",
    "Total agent registry requests",
    ["endpoint", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "astrixa_agent_registry_request_latency_seconds",
    "Agent registry request latency",
    ["endpoint"],
)

app = FastAPI(title="Astrixa Agent Registry", version="1.0.0")


def configure_telemetry() -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "agent-registry"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)))
    trace.set_tracer_provider(provider)


configure_telemetry()
FastAPIInstrumentor.instrument_app(app)


class AgentAuth(BaseModel):
    type: str = "bearer_token"


class AgentRecord(BaseModel):
    agent_id: str
    name: str
    description: str
    url: str
    version: str = "0.1.0"
    supported_methods: list[str] = Field(default_factory=list)
    auth: AgentAuth = Field(default_factory=AgentAuth)


class AgentPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    url: str | None = None
    version: str | None = None
    supported_methods: list[str] | None = None


AGENTS: dict[str, AgentRecord] = {}


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
    return {"status": "ok", "service": "agent-registry"}


@app.get("/readyz")
async def readyz():
    return {"status": "ready", "service": "agent-registry"}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/agents")
async def list_agents():
    return {"items": [agent.model_dump() for agent in AGENTS.values()]}


@app.post("/v1/agents", status_code=status.HTTP_201_CREATED)
async def create_agent(agent: AgentRecord):
    if agent.agent_id in AGENTS:
        raise HTTPException(status_code=409, detail="agent already exists")
    AGENTS[agent.agent_id] = agent
    return agent.model_dump()


@app.get("/v1/agents/{agent_id}")
async def get_agent(agent_id: str):
    agent = AGENTS.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return agent.model_dump()


@app.patch("/v1/agents/{agent_id}")
async def patch_agent(agent_id: str, patch: AgentPatch):
    existing = AGENTS.get(agent_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="agent not found")
    updated = existing.model_copy(update=patch.model_dump(exclude_none=True))
    AGENTS[agent_id] = updated
    return updated.model_dump()
