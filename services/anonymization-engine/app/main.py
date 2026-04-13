import os
import re
import time
from typing import Any

import spacy
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
DEFAULT_ANONYMIZATION_MODE = os.getenv("ASTRIXA_DEFAULT_ANONYMIZATION_MODE", "on").lower()
DEFAULT_ANONYMIZATION_PROFILE = os.getenv("ASTRIXA_DEFAULT_ANONYMIZATION_PROFILE", "pii-lite").lower()

REQUEST_COUNT = Counter(
    "astrixa_anonymization_requests_total",
    "Total anonymization requests",
    ["endpoint", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "astrixa_anonymization_request_latency_seconds",
    "Anonymization request latency",
    ["endpoint"],
)
ENTITY_COUNT = Counter(
    "astrixa_anonymization_entities_total",
    "Detected anonymized entities",
    ["entity_type", "mode"],
)

app = FastAPI(title="Astrixa Anonymization Engine", version="1.0.0")
SPACY_MODEL_NAME = os.getenv("SPACY_MODEL", "en_core_web_sm")


def configure_telemetry() -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "anonymization-engine"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)))
    trace.set_tracer_provider(provider)


configure_telemetry()
FastAPIInstrumentor.instrument_app(app)

try:
    NLP = spacy.load(SPACY_MODEL_NAME)
except OSError:
    NLP = None


class Message(BaseModel):
    role: str
    content: str


class Replacement(BaseModel):
    token: str
    original: str
    entity_type: str
    mode: str


class AnonymizeRequest(BaseModel):
    model: str
    messages: list[Message]
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnonymizeResponse(BaseModel):
    decision: str = "anonymize"
    policy_version: str = "anonymization.v1"
    anonymization_mode: str = "on"
    anonymization_profile: str = "pii-lite"
    sanitized_messages: list[Message]
    replacements: list[Replacement] = Field(default_factory=list)
    entity_counts: dict[str, int] = Field(default_factory=dict)


class DeanonymizeRequest(BaseModel):
    body: Any
    replacements: list[Replacement] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeanonymizeResponse(BaseModel):
    decision: str = "restore"
    restored_body: Any
    replacement_count: int = 0


DETERMINISTIC_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("EMAIL", "regex", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("PHONE", "regex", re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?){1}\d{3}[-.\s]?\d{4}\b")),
    ("API_KEY", "regex", re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|rp-ak-[A-Za-z0-9]+)\b")),
    ("CARD", "regex", re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
    ("SSN", "regex", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
]

HEURISTIC_NER_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("PERSON", "local-ner", re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b")),
    ("ORG", "local-ner", re.compile(r"\b[A-Z][A-Za-z0-9&]+(?:\s+[A-Z][A-Za-z0-9&]+)*\s+(?:Inc|LLC|Ltd|Corp|Corporation|Company|Technologies|Systems)\b")),
    ("ADDRESS", "local-ner", re.compile(r"\b\d{1,5}\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}\s+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln)\b")),
]

STRICT_NO_RESTORE_TYPES = {"EMAIL", "PHONE", "API_KEY", "CARD", "SSN"}
ANONYMIZATION_PROFILES: dict[str, dict[str, set[str] | str]] = {
    "none": {
        "mode": "off",
        "include": set(),
        "exclude": set(),
        "restore_include": set(),
        "restore_exclude": set(),
    },
    "secrets-only": {
        "mode": "on",
        "include": {"API_KEY", "CARD", "SSN"},
        "exclude": set(),
        "restore_include": set(),
        "restore_exclude": {"API_KEY", "CARD", "SSN"},
    },
    "pii-lite": {
        "mode": "on",
        "include": {"EMAIL", "PHONE", "PERSON", "LOCATION"},
        "exclude": set(),
        "restore_include": set(),
        "restore_exclude": set(),
    },
    "pii-strict": {
        "mode": "on",
        "include": {"EMAIL", "PHONE", "PERSON", "ORG", "LOCATION", "ADDRESS", "API_KEY", "CARD", "SSN"},
        "exclude": set(),
        "restore_include": set(),
        "restore_exclude": {"EMAIL", "PHONE", "ADDRESS", "API_KEY", "CARD", "SSN"},
    },
    "outreach": {
        "mode": "on",
        "include": {"PERSON", "ORG", "EMAIL", "PHONE"},
        "exclude": set(),
        "restore_include": set(),
        "restore_exclude": {"EMAIL", "PHONE"},
    },
}


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
    return {"status": "ok", "service": "anonymization-engine"}


@app.get("/readyz")
async def readyz():
    return {"status": "ready", "service": "anonymization-engine"}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


def _token(entity_type: str, index: int) -> str:
    return f"__ASTRIXA_{entity_type}_{index:03d}__"


def _resolve_anonymization_mode(metadata: dict[str, Any]) -> str:
    mode = str(metadata.get("anonymization_mode") or DEFAULT_ANONYMIZATION_MODE).lower()
    return "off" if mode == "off" else "on"


def _resolve_anonymization_profile(metadata: dict[str, Any]) -> str:
    profile = str(metadata.get("anonymization_profile") or DEFAULT_ANONYMIZATION_PROFILE).lower()
    return profile if profile in ANONYMIZATION_PROFILES else DEFAULT_ANONYMIZATION_PROFILE


def _parse_entity_set(raw_value: Any) -> set[str]:
    if raw_value is None:
        return set()
    if isinstance(raw_value, str):
        return {item.strip().upper() for item in raw_value.split(",") if item.strip()}
    if isinstance(raw_value, (list, tuple, set)):
        return {str(item).strip().upper() for item in raw_value if str(item).strip()}
    return set()


def _resolve_entity_filters(metadata: dict[str, Any]) -> tuple[set[str] | None, set[str]]:
    include = _parse_entity_set(metadata.get("anonymization_entities_include"))
    exclude = _parse_entity_set(metadata.get("anonymization_entities_exclude"))
    return (include or None, exclude)


def _resolve_restore_filters(metadata: dict[str, Any]) -> tuple[set[str] | None, set[str]]:
    restore_include = _parse_entity_set(metadata.get("anonymization_restore_include"))
    restore_exclude = _parse_entity_set(metadata.get("anonymization_restore_exclude"))
    return (restore_include or None, restore_exclude)


def _effective_anonymization_config(metadata: dict[str, Any]) -> dict[str, Any]:
    profile_name = _resolve_anonymization_profile(metadata)
    profile = ANONYMIZATION_PROFILES[profile_name]

    explicit_mode = metadata.get("anonymization_mode")
    mode = (
        "off"
        if str(explicit_mode).lower() == "off"
        else ("on" if explicit_mode is not None else str(profile["mode"]))
    )

    include, exclude = _resolve_entity_filters(metadata)
    restore_include, restore_exclude = _resolve_restore_filters(metadata)

    return {
        "profile": profile_name,
        "mode": mode,
        "include": include if include is not None else (set(profile["include"]) or None),
        "exclude": exclude if exclude else set(profile["exclude"]),
        "restore_include": restore_include if restore_include is not None else (set(profile["restore_include"]) or None),
        "restore_exclude": restore_exclude if restore_exclude else set(profile["restore_exclude"]),
    }


def _entity_enabled(entity_type: str, include: set[str] | None, exclude: set[str]) -> bool:
    if include is not None and entity_type not in include:
        return False
    return entity_type not in exclude


def _apply_replacement(
    text: str,
    original: str,
    entity_type: str,
    mode: str,
    replacements: list[Replacement],
    counters: dict[str, int],
) -> str:
    if "ASTRIXA_" in original:
        return text
    seen_originals = {replacement.original: replacement.token for replacement in replacements}
    if original in seen_originals:
        return text.replace(original, seen_originals[original])
    counters[entity_type] = counters.get(entity_type, 0) + 1
    token = _token(entity_type, counters[entity_type])
    replacements.append(
        Replacement(
            token=token,
            original=original,
            entity_type=entity_type,
            mode=mode,
        )
    )
    ENTITY_COUNT.labels(entity_type=entity_type, mode=mode).inc()
    return text.replace(original, token)


def _replace_text(
    text: str,
    replacements: list[Replacement],
    counters: dict[str, int],
    include_entities: set[str] | None,
    exclude_entities: set[str],
) -> str:
    updated = text

    for entity_type, mode, pattern in DETERMINISTIC_PATTERNS:
        if not _entity_enabled(entity_type, include_entities, exclude_entities):
            continue
        for match in list(pattern.finditer(updated)):
            updated = _apply_replacement(updated, match.group(0), entity_type, mode, replacements, counters)

    if NLP is not None:
        doc = NLP(updated)
        allowed_labels = {
            "PERSON": "PERSON",
            "ORG": "ORG",
            "GPE": "LOCATION",
            "LOC": "LOCATION",
            "FAC": "LOCATION",
        }
        # Replace longer spans first to reduce nested partial replacements.
        spans = sorted(
            [
                (ent.text, allowed_labels[ent.label_])
                for ent in doc.ents
                if ent.label_ in allowed_labels and "ASTRIXA_" not in ent.text
            ],
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for original, entity_type in spans:
            if not _entity_enabled(entity_type, include_entities, exclude_entities):
                continue
            updated = _apply_replacement(updated, original, entity_type, "spacy-ner", replacements, counters)

    for entity_type, mode, pattern in HEURISTIC_NER_PATTERNS:
        if not _entity_enabled(entity_type, include_entities, exclude_entities):
            continue
        for match in list(pattern.finditer(updated)):
            updated = _apply_replacement(updated, match.group(0), entity_type, mode, replacements, counters)

    return updated


def _restore_value(
    value: Any,
    replacements: list[Replacement],
    policy_profile: str,
    restore_include: set[str] | None,
    restore_exclude: set[str],
) -> Any:
    if isinstance(value, dict):
        return {
            key: _restore_value(nested_value, replacements, policy_profile, restore_include, restore_exclude)
            for key, nested_value in value.items()
        }
    if isinstance(value, list):
        return [_restore_value(item, replacements, policy_profile, restore_include, restore_exclude) for item in value]
    if isinstance(value, str):
        restored = value
        for replacement in replacements:
            entity_type = replacement.entity_type.upper()
            should_restore = True
            if restore_include is not None and entity_type not in restore_include:
                should_restore = False
            if entity_type in restore_exclude:
                should_restore = False
            if policy_profile == "strict" and entity_type in STRICT_NO_RESTORE_TYPES:
                should_restore = False
            if not should_restore:
                restored = restored.replace(replacement.token, f"[REDACTED_{replacement.entity_type}]")
                continue
            restored = restored.replace(replacement.token, replacement.original)
        return restored
    return value


@app.post("/v1/anonymize", response_model=AnonymizeResponse)
async def anonymize(payload: AnonymizeRequest):
    config = _effective_anonymization_config(payload.metadata)
    anonymization_mode = config["mode"]
    if anonymization_mode == "off":
        return AnonymizeResponse(
            decision="bypass",
            policy_version="anonymization.v1.off",
            anonymization_mode="off",
            anonymization_profile=config["profile"],
            sanitized_messages=payload.messages,
            replacements=[],
            entity_counts={},
        )

    policy_profile = str(payload.metadata.get("policy_profile") or "balanced").lower()
    replacements: list[Replacement] = []
    counters: dict[str, int] = {}
    sanitized_messages = [
        Message(
            role=message.role,
            content=_replace_text(message.content, replacements, counters, config["include"], config["exclude"]),
        )
        for message in payload.messages
    ]
    entity_counts: dict[str, int] = {}
    for replacement in replacements:
        entity_counts[replacement.entity_type] = entity_counts.get(replacement.entity_type, 0) + 1
    return AnonymizeResponse(
        sanitized_messages=sanitized_messages,
        replacements=replacements,
        entity_counts=entity_counts,
        policy_version=f"anonymization.v1.{policy_profile}",
        anonymization_mode="on",
        anonymization_profile=config["profile"],
    )


@app.post("/v1/deanonymize", response_model=DeanonymizeResponse)
async def deanonymize(payload: DeanonymizeRequest):
    config = _effective_anonymization_config(payload.metadata)
    policy_profile = str(payload.metadata.get("policy_profile") or "balanced").lower()
    restored_body = _restore_value(
        payload.body,
        payload.replacements,
        policy_profile,
        config["restore_include"],
        config["restore_exclude"],
    )
    return DeanonymizeResponse(
        restored_body=restored_body,
        replacement_count=len(payload.replacements),
    )
