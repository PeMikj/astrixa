#!/usr/bin/env python3
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ARTIFACTS_DIR = ROOT / "artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)


def run_json_command(name: str, command: list[str]) -> dict:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"{name} failed with exit code {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} did not return valid JSON\nstdout:\n{result.stdout}") from exc
    output_path = ARTIFACTS_DIR / f"{name}.json"
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def build_summary(reports: dict[str, dict]) -> str:
    benchmark = reports["benchmark"]
    ejection = reports["provider_ejection"]
    mixed = reports["mixed_resilience"]

    lines = [
        "# Astrixa Submission Suite",
        "",
        f"Generated at: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Benchmark",
        f"- requests: `{benchmark['total_requests']}`",
        f"- concurrency: `{benchmark['concurrency']}`",
        f"- success rate: `{benchmark['successes']}/{benchmark['total_requests']}`",
        f"- throughput: `{benchmark['throughput_rps']} req/s`",
        f"- avg latency: `{benchmark['latency_avg_ms']} ms`",
        f"- p50 latency: `{benchmark['latency_p50_ms']} ms`",
        f"- p95 latency: `{benchmark['latency_p95_ms']} ms`",
        "",
        "## Provider Ejection",
        f"- provider: `{ejection['provider_id']}`",
        f"- post-error health: `{ejection['after_error']['health_status']}`",
        f"- gateway continuity status: `{ejection['gateway_during_ejection']['status_code']}` via `{ejection['gateway_during_ejection']['provider']}`",
        f"- final health: `{ejection['after_success']['health_status']}`",
        "",
        "## Mixed Resilience",
        f"- total requests: `{mixed['overall']['requests']}`",
        f"- overall success rate: `{mixed['overall']['successes']}/{mixed['overall']['requests']}`",
        f"- failure-window success rate: `{mixed['failure_window']['successes']}/{mixed['failure_window']['requests']}`",
        f"- overall avg latency: `{mixed['overall']['latency_avg_ms']} ms`",
        f"- overall p95 latency: `{mixed['overall']['latency_p95_ms']} ms`",
        f"- final provider health: `{mixed['final_provider_state']['health_status']}`",
        "",
        "## Artifacts",
        f"- [benchmark.json]({(ARTIFACTS_DIR / 'benchmark.json').as_posix()})",
        f"- [provider_ejection.json]({(ARTIFACTS_DIR / 'provider_ejection.json').as_posix()})",
        f"- [mixed_resilience.json]({(ARTIFACTS_DIR / 'mixed_resilience.json').as_posix()})",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    reports = {
        "benchmark": run_json_command("benchmark", ["python3", str(ROOT / "load" / "http_benchmark.py")]),
        "provider_ejection": run_json_command(
            "provider_ejection",
            ["python3", str(ROOT / "resilience" / "run_provider_ejection.py")],
        ),
        "mixed_resilience": run_json_command(
            "mixed_resilience",
            ["python3", str(ROOT / "resilience" / "run_mixed_load_resilience.py")],
        ),
    }

    summary = build_summary(reports)
    summary_path = ARTIFACTS_DIR / "submission-suite-summary.md"
    summary_path.write_text(summary, encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"submission suite failed: {exc}", file=sys.stderr)
        sys.exit(1)
