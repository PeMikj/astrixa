#!/usr/bin/env python3
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


BASE_URL = os.getenv("ASTRIXA_BASE_URL", "http://127.0.0.1:18080")
TOKEN = os.getenv("ASTRIXA_GATEWAY_TOKEN", "astrixa-dev-token")
MODEL = os.getenv("ASTRIXA_MODEL", "mock-1")
TOTAL_REQUESTS = int(os.getenv("ASTRIXA_TOTAL_REQUESTS", "50"))
CONCURRENCY = int(os.getenv("ASTRIXA_CONCURRENCY", "10"))


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def make_request(_: int) -> tuple[bool, float, int]:
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "benchmark request from astrixa"}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/v1/chat/completions",
        data=body,
        method="POST",
        headers={
            "authorization": f"Bearer {TOKEN}",
            "content-type": "application/json",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            _ = response.read()
            return (200 <= response.status < 300, time.perf_counter() - started, response.status)
    except urllib.error.HTTPError as exc:
        _ = exc.read()
        return (False, time.perf_counter() - started, exc.code)
    except Exception:
        return (False, time.perf_counter() - started, 0)


def main() -> None:
    started = time.perf_counter()
    results: list[tuple[bool, float, int]] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = [executor.submit(make_request, i) for i in range(TOTAL_REQUESTS)]
        for future in as_completed(futures):
            results.append(future.result())

    total_time = time.perf_counter() - started
    latencies = [result[1] for result in results]
    successes = sum(1 for result in results if result[0])
    failures = len(results) - successes
    throughput = len(results) / total_time if total_time > 0 else 0.0
    codes: dict[int, int] = {}
    for _, _, code in results:
        codes[code] = codes.get(code, 0) + 1

    report = {
        "base_url": BASE_URL,
        "model": MODEL,
        "total_requests": len(results),
        "concurrency": CONCURRENCY,
        "successes": successes,
        "failures": failures,
        "throughput_rps": round(throughput, 2),
        "latency_avg_ms": round(statistics.mean(latencies) * 1000, 2) if latencies else 0.0,
        "latency_p50_ms": round(percentile(latencies, 0.50) * 1000, 2),
        "latency_p95_ms": round(percentile(latencies, 0.95) * 1000, 2),
        "status_codes": codes,
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

