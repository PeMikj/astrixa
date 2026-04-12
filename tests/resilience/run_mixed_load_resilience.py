#!/usr/bin/env python3
import json
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED


GATEWAY_URL = "http://127.0.0.1:18080/v1/chat/completions"
GATEWAY_TOKEN = "astrixa-dev-token"
ROUTING_CONTAINER = "astrixa-routing-engine-1"
PROVIDER_REGISTRY_CONTAINER = "astrixa-provider-registry-1"
TARGET_PROVIDER_ID = "mock-echo-secondary"
TOTAL_REQUESTS = 60
CONCURRENCY = 8
EJECTION_AFTER_SECONDS = 2.0
RECOVERY_AFTER_SECONDS = 5.0


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


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def gateway_request(index: int, started_at: float) -> dict:
    body = json.dumps(
        {
            "model": "mock-1",
            "messages": [{"role": "user", "content": f"mixed resilience request {index}"}],
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
    phase = "baseline"
    elapsed_from_start = time.perf_counter() - started_at
    if elapsed_from_start >= RECOVERY_AFTER_SECONDS:
        phase = "recovery"
    elif elapsed_from_start >= EJECTION_AFTER_SECONDS:
        phase = "failure_window"

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return {
                "ok": 200 <= response.status < 300,
                "status_code": response.status,
                "latency_seconds": time.perf_counter() - started,
                "phase": phase,
                "provider": payload.get("provider"),
            }
    except urllib.error.HTTPError as exc:
        _ = exc.read()
        return {
            "ok": False,
            "status_code": exc.code,
            "latency_seconds": time.perf_counter() - started,
            "phase": phase,
            "provider": None,
        }
    except Exception:
        return {
            "ok": False,
            "status_code": 0,
            "latency_seconds": time.perf_counter() - started,
            "phase": phase,
            "provider": None,
        }


def summarize(results: list[dict]) -> dict:
    latencies = [item["latency_seconds"] for item in results]
    return {
        "requests": len(results),
        "successes": sum(1 for item in results if item["ok"]),
        "failures": sum(1 for item in results if not item["ok"]),
        "status_codes": {
            str(code): sum(1 for item in results if item["status_code"] == code)
            for code in sorted({item["status_code"] for item in results})
        },
        "providers": {
            provider: sum(1 for item in results if item["provider"] == provider)
            for provider in sorted({item["provider"] for item in results if item["provider"]})
        },
        "latency_avg_ms": round(statistics.mean(latencies) * 1000, 2) if latencies else 0.0,
        "latency_p50_ms": round(percentile(latencies, 0.5) * 1000, 2),
        "latency_p95_ms": round(percentile(latencies, 0.95) * 1000, 2),
    }


def assert_condition(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    ensure_secondary_provider()
    pre_state = get_provider(TARGET_PROVIDER_ID)
    assert_condition(pre_state["health_status"] == "healthy", "secondary provider was not healthy before scenario start")

    results: list[dict] = []
    started_at = time.perf_counter()
    ejection_done = threading.Event()
    recovery_done = threading.Event()

    def fault_injector() -> None:
        time.sleep(EJECTION_AFTER_SECONDS)
        post_feedback(TARGET_PROVIDER_ID, "error", "mixed load resilience failure")
        ejection_done.set()
        time.sleep(max(0.0, RECOVERY_AFTER_SECONDS - EJECTION_AFTER_SECONDS))
        post_feedback(TARGET_PROVIDER_ID, "success")
        recovery_done.set()

    injector_thread = threading.Thread(target=fault_injector, daemon=True)
    injector_thread.start()

    next_index = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = set()
        while next_index < TOTAL_REQUESTS or futures:
            while next_index < TOTAL_REQUESTS and len(futures) < CONCURRENCY:
                futures.add(executor.submit(gateway_request, next_index, started_at))
                next_index += 1
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                results.append(future.result())

    injector_thread.join(timeout=15)
    assert_condition(ejection_done.is_set(), "ejection event was not completed")
    assert_condition(recovery_done.is_set(), "recovery event was not completed")

    degraded_state = get_provider(TARGET_PROVIDER_ID)
    if degraded_state["health_status"] != "healthy":
        post_feedback(TARGET_PROVIDER_ID, "success")
        degraded_state = get_provider(TARGET_PROVIDER_ID)

    by_phase: dict[str, list[dict]] = {"baseline": [], "failure_window": [], "recovery": []}
    for result in results:
        by_phase[result["phase"]].append(result)

    report = {
        "provider_id": TARGET_PROVIDER_ID,
        "scenario": {
            "total_requests": TOTAL_REQUESTS,
            "concurrency": CONCURRENCY,
            "ejection_after_seconds": EJECTION_AFTER_SECONDS,
            "recovery_after_seconds": RECOVERY_AFTER_SECONDS,
        },
        "overall": summarize(results),
        "baseline": summarize(by_phase["baseline"]),
        "failure_window": summarize(by_phase["failure_window"]),
        "recovery": summarize(by_phase["recovery"]),
        "final_provider_state": {
            "health_status": degraded_state["health_status"],
            "health_source": degraded_state["health_source"],
            "ejected_until": degraded_state["ejected_until"],
            "last_error": degraded_state["last_error"],
            "consecutive_failures": degraded_state["consecutive_failures"],
        },
    }

    assert_condition(report["overall"]["failures"] == 0, "mixed scenario produced failed gateway requests")
    assert_condition(report["failure_window"]["requests"] > 0, "no requests were captured during failure window")
    assert_condition(report["failure_window"]["successes"] == report["failure_window"]["requests"], "gateway continuity failed during provider ejection")
    assert_condition(report["final_provider_state"]["health_status"] == "healthy", "provider did not recover to healthy at scenario end")

    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"mixed resilience scenario failed: {exc}", file=sys.stderr)
        sys.exit(1)
