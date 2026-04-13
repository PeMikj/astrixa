#!/usr/bin/env python3
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request


GATEWAY_URL = "http://127.0.0.1:18080/v1/chat/completions"
GATEWAY_TOKEN = "astrixa-dev-token"
ROUTING_CONTAINER = "astrixa-routing-engine-1"
PROVIDER_REGISTRY_CONTAINER = "astrixa-provider-registry-1"
TARGET_PROVIDER_ID = "mock-echo-secondary"


def _run_docker_exec(container: str, python_code: str) -> str:
    result = subprocess.run(
        ["docker", "exec", container, "python", "-c", python_code],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _registry_exec(expression: str) -> str:
    return _run_docker_exec(PROVIDER_REGISTRY_CONTAINER, expression)


def _routing_exec(expression: str) -> str:
    return _run_docker_exec(ROUTING_CONTAINER, expression)


def _gateway_request(prompt: str) -> tuple[int, dict]:
    body = json.dumps(
        {
            "model": "mock-1",
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        GATEWAY_URL,
        data=body,
        method="POST",
        headers={
            "authorization": f"Bearer {GATEWAY_TOKEN}",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8")
        try:
            payload = json.loads(body_text)
        except json.JSONDecodeError:
            payload = {"raw": body_text}
        return exc.code, payload


def ensure_secondary_provider() -> None:
    code = r"""
import json
import urllib.error
import urllib.request

provider = {
    "provider_id": "mock-echo-secondary",
    "type": "mock",
    "base_url": "http://mock-llm:8080",
    "models": ["mock-1"],
    "priority": 90,
    "weight": 1,
    "enabled": True,
    "health_status": "healthy",
    "health_source": "manual",
    "health_check_url": "http://mock-llm:8080/healthz",
    "ejected_until": None,
    "auth_type": None,
    "api_key_env": None,
    "last_check_at": None,
    "consecutive_failures": 0,
    "last_error": None,
    "price": {"input_per_1k_tokens": 0.0, "output_per_1k_tokens": 0.0},
    "limits": {"rpm": None, "tpm": None},
}
request = urllib.request.Request(
    "http://127.0.0.1:8080/v1/providers",
    data=json.dumps(provider).encode("utf-8"),
    method="POST",
    headers={"content-type": "application/json"},
)
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        print(response.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    if exc.code == 409:
        print("exists")
    else:
        raise
"""
    _registry_exec(code)


def get_provider(provider_id: str) -> dict:
    code = rf"""
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8080/v1/providers/{provider_id}", timeout=10) as response:
    print(response.read().decode("utf-8"))
"""
    return json.loads(_registry_exec(code))


def post_feedback(provider_id: str, outcome: str, error_message: str | None = None) -> dict:
    payload = {
        "provider_id": provider_id,
        "latency_seconds": 0.25,
        "outcome": outcome,
        "error_message": error_message,
    }
    payload_json = json.dumps(payload)
    code = rf"""
import json
import urllib.request

payload = json.loads({json.dumps(payload_json)})
request = urllib.request.Request(
    "http://127.0.0.1:8080/v1/provider-feedback",
    data=json.dumps(payload).encode("utf-8"),
    method="POST",
    headers={{"content-type": "application/json"}},
)
with urllib.request.urlopen(request, timeout=10) as response:
    print(response.read().decode("utf-8"))
"""
    return json.loads(_routing_exec(code))


def assert_condition(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def ensure_provider_healthy(provider_id: str, timeout_seconds: float = 10.0) -> dict:
    post_feedback(provider_id, "success")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        state = get_provider(provider_id)
        if state["health_status"] == "healthy" and state["ejected_until"] is None:
            return state
        time.sleep(0.25)
    raise AssertionError(f"provider {provider_id} did not return to healthy before test start")


def main() -> None:
    report: dict[str, object] = {"provider_id": TARGET_PROVIDER_ID}
    ensure_secondary_provider()
    before_state = ensure_provider_healthy(TARGET_PROVIDER_ID)
    report["before"] = {
        "health_status": before_state["health_status"],
        "ejected_until": before_state["ejected_until"],
        "consecutive_failures": before_state["consecutive_failures"],
    }

    error_feedback = post_feedback(TARGET_PROVIDER_ID, "error", "automated resilience test failure")
    error_state = get_provider(TARGET_PROVIDER_ID)
    report["after_error"] = {
        "feedback": error_feedback,
        "health_status": error_state["health_status"],
        "health_source": error_state["health_source"],
        "ejected_until": error_state["ejected_until"],
        "last_error": error_state["last_error"],
        "consecutive_failures": error_state["consecutive_failures"],
    }
    assert_condition(error_state["health_status"] in {"degraded", "unhealthy"}, "provider did not degrade after error")
    assert_condition(error_state["health_source"] == "routing-feedback", "provider health source was not routing-feedback")
    assert_condition(error_state["ejected_until"] is not None, "provider was not ejected after error")
    assert_condition(error_state["last_error"] == "automated resilience test failure", "provider last_error mismatch")

    status_code, gateway_payload = _gateway_request("resilience continuity request")
    report["gateway_during_ejection"] = {
        "status_code": status_code,
        "provider": gateway_payload.get("provider"),
    }
    assert_condition(status_code == 200, "gateway did not remain available during provider ejection")

    success_feedback = post_feedback(TARGET_PROVIDER_ID, "success")
    success_state = get_provider(TARGET_PROVIDER_ID)
    report["after_success"] = {
        "feedback": success_feedback,
        "health_status": success_state["health_status"],
        "health_source": success_state["health_source"],
        "ejected_until": success_state["ejected_until"],
        "last_error": success_state["last_error"],
        "consecutive_failures": success_state["consecutive_failures"],
    }
    assert_condition(success_state["health_status"] == "healthy", "provider did not recover to healthy")
    assert_condition(success_state["ejected_until"] is None, "provider ejection was not cleared on recovery")
    assert_condition(success_state["last_error"] is None, "provider last_error was not cleared on recovery")

    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"resilience test failed: {exc}", file=sys.stderr)
        sys.exit(1)
