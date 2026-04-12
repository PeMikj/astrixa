# Load Tests

Load and throughput scenarios for gateway, routing, and provider behavior live here.

Current assets:

- [k6-smoke.js](/home/p/astrixa/tests/load/k6-smoke.js): basic authenticated load against `POST /v1/chat/completions`
- [http_benchmark.py](/home/p/astrixa/tests/load/http_benchmark.py): Python benchmark runner for environments without k6

Example:

```bash
k6 run tests/load/k6-smoke.js
```

Fallback:

```bash
python3 tests/load/http_benchmark.py
```
