#!/usr/bin/env python3
"""Lead acceptance check: real OpenAI-compatible endpoint through the public URL.

Covers: run creation with per-run access token, SSE with ?token=, token-gated
reads, privacy (missing token -> 404, key never echoed), memory-only storage.

Usage:
  LB_KEY=<key> LB_BASE=http://127.0.0.1:21700 .venv/bin/python scripts/lead_real_check.py
  LB_PRESET=demo-preset .venv/bin/python scripts/lead_real_check.py   # server-credential preset
"""
import json
import os
import sys
import time

import httpx

BASE = os.environ.get("LB_BASE", "http://127.0.0.1:21700").rstrip("/")
KEY = os.environ.get("LB_KEY", "")
MODEL = os.environ.get("LB_MODEL", "poolside/laguna-s-2.1:free")
UPSTREAM = os.environ.get("LB_UPSTREAM", "http://127.0.0.1:8080/v1")
PRESET = os.environ.get("LB_PRESET", "")
CONTEXTS = [int(x) for x in os.environ.get("LB_CONTEXTS", "1024,4096").split(",")]
CONC = [int(x) for x in os.environ.get("LB_CONC", "1,2").split(",")]
MAX_TOKENS = int(os.environ.get("LB_MAX_TOKENS", "64"))

payload = {
    "label": os.environ.get("LB_LABEL", "lead real endpoint check"),
    "model": MODEL,
    "config": {
        "contexts": CONTEXTS,
        "concurrency": CONC,
        "concurrency_context": 1024,
        "max_tokens": MAX_TOKENS,
        "repeats": 1,
        "idle_sample_s": 0,
        "request_timeout_s": 180,
    },
}
if PRESET:
    payload["preset_id"] = PRESET
else:
    payload.update({"base_url": UPSTREAM, "api_key": KEY})

fail = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" · " + str(detail)) if detail else ""))
    if not cond:
        fail.append(name)


with httpx.Client(timeout=30) as c:
    r = c.post(f"{BASE}/api/runs", json=payload)
    r.raise_for_status()
    body = r.json()
    run_id, token = body["run_id"], body.get("access_token", "")
    print(f"run_id={run_id} token={'yes' if token else 'MISSING'}")
    check("post_returns_access_token", bool(token))
    hdr = {"X-Run-Token": token}

    check("read_without_token_404", c.get(f"{BASE}/api/runs/{run_id}").status_code == 404)
    check("read_with_bad_token_404", c.get(f"{BASE}/api/runs/{run_id}", headers={"X-Run-Token": "rt_bogus"}).status_code == 404)

    events = []
    t0 = time.time()
    with httpx.Client(timeout=httpx.Timeout(300, read=300)) as s:
        with s.stream("GET", f"{BASE}/api/runs/{run_id}/events", params={"token": token}) as resp:
            print("sse", resp.status_code, resp.headers.get("content-type"))
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    events.append(ev["type"])
                    if ev["type"] == "point":
                        p = ev.get("point", {})
                        print("  point", ev.get("kind"), {k: p.get(k) for k in ("ctx_requested", "prompt_tokens", "ttft_ms", "prefill_tps", "decode_tps", "agg_tps")})
                    elif ev["type"] in ("done",):
                        break
    print(f"sse events={len(events)} distinct={sorted(set(events))} elapsed={time.time()-t0:.1f}s")
    check("sse_no_token_404", httpx.get(f"{BASE}/api/runs/{run_id}/events", timeout=10).status_code == 404)
    check("sse_snapshot_first", bool(events) and events[0] == "snapshot")
    check("sse_done_last", bool(events) and events[-1] == "done")

    run = c.get(f"{BASE}/api/runs/{run_id}", headers=hdr).json()["run"]
    res = run["results"]
    print("status", run["status"], "error", run["error"], "preset", run.get("preset_id"))
    print("ttft", res["ttft_ms"], "decode", res["decode_tps"])
    for p in res["context_points"]:
        print(" ctx", {k: p.get(k) for k in ("ctx_requested", "prompt_tokens", "completion_tokens", "ttft_ms", "prefill_tps", "prefill_source", "decode_tps", "ok", "error")})
    for p in res["concurrency_points"]:
        print(" conc", {k: p.get(k) for k in ("n", "wall_s", "agg_tps", "per_req_tps", "ok", "error")})
    print("gpu", res["gpu"]["available"], "notes", res["notes"])

    blob = json.dumps(run)
    check("status_done", run["status"] == "done")
    check("context_points_present", len(res["context_points"]) > 0)
    check("key_not_leaked", not (KEY and KEY in blob))
    check("api_key_field_absent", "api_key" not in blob)

    stats = c.get(f"{BASE}/api/stats").json()
    print("stats", stats)
    check("stats_has_no_ids", "r_" not in json.dumps(stats))

    print("\nRESULT:", "ALL PASS" if not fail else f"{len(fail)} FAILED: {fail}")
    sys.exit(0 if not fail else 1)
