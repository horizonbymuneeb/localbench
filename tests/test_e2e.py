"""True end-to-end test: real uvicorn + real mock server in subprocesses.

Proves the acceptance path from CONTRACT.md:
  * POST two labelled runs over HTTP, consume SSE with httpx,
  * both finish `done` with non-empty context/concurrency points,
  * they are comparable through GET /api/runs (labels + headline metrics),
  * the served frontend shell and agent script are reachable,
  * exports work over real HTTP.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

import httpx
import pytest

from conftest import MOCK_DIR, ROOT, free_port, parse_sse_lines, wait_http_ok

E2E_CONFIG: Dict[str, Any] = {
    "contexts": [1024, 4096, 8192],
    "concurrency": [1, 2],
    "concurrency_context": 2048,
    "max_tokens": 16,
    "repeats": 1,
    "warmup": 1,
    "idle_sample_s": 1,
    "enable_context_ladder": True,
    "enable_concurrency": True,
    "request_timeout_s": 120,
}


def _stop(proc: subprocess.Popen, name: str) -> str:
    if proc.poll() is not None:
        return ""
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=8)
    tail = ""
    if proc.stderr is not None:
        try:
            tail = proc.stderr.read().decode("utf-8", "replace")[-2000:]
        except Exception:
            pass
    if proc.returncode not in (0, -15):
        print("[e2e] %s exited rc=%s\n%s" % (name, proc.returncode, tail))
    return tail


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """Launch mock + uvicorn on ephemeral ports with a scratch sqlite DB."""
    tmp_dir = tmp_path_factory.mktemp("e2e")
    db_path = tmp_dir / "runs.db"
    mock_port = free_port()
    app_port = free_port()

    mock_proc = subprocess.Popen(
        [
            sys.executable,
            str(MOCK_DIR / "mock_openai.py"),
            "--port",
            str(mock_port),
            "--ttft-ms",
            "5",
            "--prefill-tps",
            "8000",
            "--tps",
            "300",
            "--ctx",
            "32768",
            "--model",
            "qwen3-27b",
            "--draft-n",
            "32",
            "--draft-accepted",
            "20",
        ],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    # Production shape: RAM only. The suite's own env asks for sqlite, so both
    # the DB path and the persist flag must be neutralised for the child.
    env = dict(os.environ)
    env.pop("LOCALBENCH_DB", None)
    env["LOCALBENCH_PERSIST"] = "0"
    env["LOCALBENCH_DATA_DIR"] = str(tmp_dir / "data")
    env["LOCALBENCH_TTL_S"] = "900"
    env["LOCALBENCH_MAX_RUNS"] = "2"
    env["LOCALBENCH_RATE_LIMIT"] = "6/600"
    env.pop("PYTHONPATH", None)

    app_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(app_port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    (tmp_dir / "data").mkdir(exist_ok=True)
    info = {
        "base": "http://127.0.0.1:%d" % app_port,
        "mock_base": "http://127.0.0.1:%d/v1" % mock_port,
        "mock_host": "127.0.0.1:%d" % mock_port,
        "data_dir": tmp_dir / "data",
        "app_proc": app_proc,
        "mock_proc": mock_proc,
    }
    try:
        wait_http_ok(info["mock_base"] + "/models", timeout=20)
        try:
            wait_http_ok(info["base"] + "/healthz", timeout=40)
        except AssertionError:
            stderr = ""
            if app_proc.stderr is not None:
                try:
                    app_proc.stderr.close()
                except Exception:
                    pass
            raise AssertionError(
                "uvicorn (%s/healthz) never became ready; db=%s%s"
                % (info["base"], db_path, stderr)
            )
    except Exception:
        _stop(app_proc, "uvicorn")
        _stop(mock_proc, "mock")
        raise

    try:
        yield info
    finally:
        _stop(app_proc, "uvicorn")
        _stop(mock_proc, "mock")


def consume_sse(base: str, run_id: str, token: str, timeout: float = 180.0) -> List[Dict[str, Any]]:
    """Follow /events over real HTTP (EventSource cannot set headers -> ?token=)."""
    events: List[Dict[str, Any]] = []
    url = "%s/api/runs/%s/events?token=%s" % (base, run_id, token)
    with httpx.stream("GET", url, timeout=timeout) as response:
        assert response.status_code == 200, response.status_code
        assert "text/event-stream" in response.headers.get("content-type", ""), response.headers
        for event in parse_sse_lines(response.iter_lines()):
            events.append(event)
            if event.get("type") == "done":
                break
    return events


def post_run(base: str, payload: Dict[str, Any]):
    """POST /api/runs -> (run_id, access_token) (CONTRACT.md per-run token)."""
    response = httpx.post(base + "/api/runs", json=payload, timeout=15)
    assert response.status_code == 200, response.text[:400]
    body = response.json()
    assert body["ok"] is True
    token = body.get("access_token")
    assert isinstance(token, str) and token.startswith("rt_"), body
    return body["run_id"], token


def get_run(base: str, run_id: str, token: Optional[str]) -> httpx.Response:
    headers = {"X-Run-Token": token} if token else {}
    return httpx.get("%s/api/runs/%s" % (base, run_id), headers=headers, timeout=15)


def test_e2e_two_labelled_runs_are_comparable(stack):
    """Two labelled runs over HTTP/SSE, each readable with its own token, comparable."""
    base = stack["base"]
    payload_common = {
        "base_url": stack["mock_base"],
        "api_key": "sk-e2e-secret-do-not-persist",
        "model": "qwen3-27b",
        "config": E2E_CONFIG,
    }
    labels = ["e2e baseline draft=0", "e2e MTP draft=3"]
    runs = []  # (run_id, token, run_doc)

    for label in labels:
        run_id, token = post_run(base, dict(payload_common, label=label))
        events = consume_sse(base, run_id, token)

        assert events, "no SSE events received"
        assert events[0]["type"] == "snapshot", events[0]
        assert events[0]["run"]["id"] == run_id
        assert events[-1]["type"] == "done", events[-1]
        done_run = events[-1]["run"]
        assert done_run["status"] == "done", done_run.get("error")
        assert done_run["label"] == label
        assert done_run["finished_at"] is not None

        # points streamed incrementally AND present in the final document
        streamed_kinds = [e.get("kind") for e in events if e["type"] == "point"]
        assert "context" in streamed_kinds and "concurrency" in streamed_kinds, streamed_kinds
        context_points = done_run["results"]["context_points"]
        concurrency_points = done_run["results"]["concurrency_points"]
        assert len(context_points) == 3, context_points
        assert len(concurrency_points) == 2, concurrency_points
        assert [p["ctx_requested"] for p in context_points] == [1024, 4096, 8192]
        assert all(p["ok"] for p in context_points), context_points
        assert all(p["ok"] for p in concurrency_points), concurrency_points
        assert [p["n"] for p in concurrency_points] == [1, 2]
        assert all(p["agg_tps"] > 0 for p in concurrency_points)
        assert all(p["prefill_tps"] > 0 for p in context_points)
        assert all(p["draft"]["n"] == 32 for p in context_points)
        runs.append((run_id, token, done_run))
        print(
            "[e2e] run_id=%s label=%r status=%s token=%s… ctx_points=%d conc_points=%d "
            "ttft_p50=%.1fms decode=%.1ftps"
            % (
                run_id,
                done_run["label"],
                done_run["status"],
                token[:6],
                len(context_points),
                len(concurrency_points),
                done_run["results"]["ttft_ms"]["p50"],
                done_run["results"]["decode_tps"]["p50"],
            )
        )

    # -- token isolation: no cross-run reads (same 404 as an unknown id) ----
    run_a, token_a, _doc_a = runs[0]
    run_b, token_b, _doc_b = runs[1]
    assert token_a != token_b
    assert get_run(base, run_b, token_a).status_code == 404
    assert get_run(base, run_a, token_b).status_code == 404
    missing = get_run(base, run_a, None)
    unknown = get_run(base, "r_deadbeef", None)
    assert missing.status_code == unknown.status_code == 404
    assert missing.json() == unknown.json() == {"ok": False, "error": "run not found"}

    # -- the old list endpoint is withdrawn; /api/stats replaces it --------
    assert httpx.get(base + "/api/runs", timeout=10).status_code == 404
    stats_response = httpx.get(base + "/api/stats", timeout=10)
    assert stats_response.status_code == 200
    stats = stats_response.json()
    assert {"ok", "active", "finished", "storage", "ttl_s"} <= set(stats), stats
    assert stats["storage"] == "memory"
    assert stats["ttl_s"] > 0
    assert stats["active"] == 0
    assert stats["finished"] >= 2
    stats_text = json.dumps(stats)
    for leak in (run_a, run_b, *labels, "qwen3-27b", stack["mock_host"]):
        assert leak not in stats_text, leak

    # -- nothing on disk: RAM only (CONTRACT.md "Storage model") -----------
    data_dir = stack["data_dir"]
    leftovers = [p.name for p in data_dir.rglob("*") if p.is_file()]
    assert leftovers == [], "memory-only mode wrote files: %r" % leftovers

    # -- comparable: same shape, both carry real headline numbers ----------
    by_label = {doc["label"]: doc for _rid, _tok, doc in runs}
    first, second = by_label[labels[0]], by_label[labels[1]]
    for doc in (first, second):
        assert doc["results"]["ttft_ms"]["p50"] > 0
        assert doc["results"]["decode_tps"]["p50"] > 0
        assert doc["results"]["prompt_tps"]["n"] >= 1
        assert doc["endpoint_host"] == stack["mock_host"]
        assert doc["model"] == "qwen3-27b"
    assert [p["ctx_requested"] for p in first["results"]["context_points"]] == [
        p["ctx_requested"] for p in second["results"]["context_points"]
    ]

    # -- export still works over HTTP with the token -----------------------
    json_export = httpx.get(
        "%s/api/runs/%s/export?format=json" % (base, run_a),
        headers={"X-Run-Token": token_a},
        timeout=10,
    )
    assert json_export.status_code == 200
    assert "attachment" in json_export.headers["content-disposition"]
    assert json.loads(json_export.text)["id"] == run_a
    csv_export = httpx.get(
        "%s/api/runs/%s/export?format=csv" % (base, run_b),
        headers={"X-Run-Token": token_b},
        timeout=10,
    )
    assert csv_export.status_code == 200
    assert csv_export.text.splitlines()[0].startswith("section,ctx_requested")
    assert "sk-e2e-secret-do-not-persist" not in json_export.text
    assert "sk-e2e-secret-do-not-persist" not in csv_export.text

    # -- healthz counts this stack's in-memory runs ------------------------
    healthz = httpx.get(base + "/healthz", timeout=10).json()
    assert healthz["ok"] is True
    assert isinstance(healthz["runs"], int) and healthz["runs"] >= 0
    stats = httpx.get(base + "/api/stats", timeout=10).json()
    assert stats["storage"] == "memory"
    assert stats["ttl_s"] > 0


def test_e2e_rate_limit_on_real_ip(stack):
    """Real uvicorn: 6 runs/600 s per X-Forwarded-For IP, then 429."""
    base = stack["base"]
    payload = {
        "base_url": stack["mock_base"],
        "model": "qwen3-27b",
        "label": "e2e rate limit",
        "config": {
            "contexts": [64],
            "concurrency": [1],
            "concurrency_context": 64,
            "max_tokens": 4,
            "warmup": 0,
            "enable_concurrency": False,
        },
    }
    headers = {"X-Forwarded-For": "203.0.113.77"}
    statuses = []
    for _ in range(7):
        response = httpx.post(base + "/api/runs", json=payload, headers=headers, timeout=15)
        statuses.append(response.status_code)
        if response.status_code == 429:
            body = response.json()
            assert body["ok"] is False and body["error"] == "rate_limited"
            assert 1 <= body["retry_after"] <= 600
            assert response.headers.get("retry-after") == str(body["retry_after"])
    assert statuses == [200] * 6 + [429], statuses


def test_e2e_discover_agent_script_and_frontend(stack):
    base = stack["base"]

    discover = httpx.post(
        base + "/api/discover",
        json={"base_url": stack["mock_base"], "api_key": "sk-e2e"},
        timeout=10,
    )
    assert discover.status_code == 200, discover.text[:300]
    models = discover.json()["models"]
    assert models[0]["id"] == "qwen3-27b"
    assert models[0]["context_length"] == 32768

    agent_script = httpx.get(base + "/bench_agent.py", timeout=10)
    if agent_script.status_code == 404:
        pytest.skip("agent/bench_agent.py not written yet")
    assert agent_script.status_code == 200
    assert "nvidia-smi" in agent_script.text

    healthz = httpx.get(base + "/healthz", timeout=10).json()
    assert healthz["ok"] is True
    assert isinstance(healthz["runs"], int) and healthz["runs"] >= 0
    stats = httpx.get(base + "/api/stats", timeout=10).json()
    assert stats["storage"] == "memory"
    assert stats["ttl_s"] > 0

    index = httpx.get(base + "/", timeout=10)
    if index.status_code == 404:
        pytest.skip("web/ frontend not written yet")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    html = index.text
    assert "<html" in html.lower()
    assert "LocalBench" in html

    for asset in ("/styles.css", "/app.js", "/vendor/chart.umd.min.js"):
        response = httpx.get(base + asset, timeout=10)
        assert response.status_code == 200, (asset, response.status_code)
        assert len(response.content) > 1000, asset
