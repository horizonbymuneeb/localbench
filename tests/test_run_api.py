"""End-to-end-ish API tests through FastAPI's TestClient against the mock engine.

Everything here asserts CONTRACT.md, not backend internals: the run JSON is the
interface the frontend codes against.
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from pathlib import Path

import pytest

from conftest import (
    FAST_CONFIG,
    TEST_DB,
    collect_sse,
    create_run,
    events_url,
    free_port,
    get_run,
    headers_for,
    json_text,
    post_run,
    remember_token,
    run_payload,
    sqlite_dump_text,
    token_for,
    wait_for,
    wait_terminal,
)

TERMINAL = ("done", "failed", "cancelled")


def ok_points(run):
    return [p for p in run["results"]["context_points"] if p.get("ok")]


def identity_prefill(point):
    """CONTRACT: prefill_tps = prompt_tokens / (prompt_ms/1000)."""
    return round(point["prompt_tokens"] / (point["prompt_ms"] / 1000.0), 2)


# ---------------------------------------------------------------------------
# health / config / presets / discover
# ---------------------------------------------------------------------------


def test_healthz(api):
    response = api.get("/healthz")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert isinstance(data["runs"], int) and data["runs"] >= 0
    assert data["uptime_s"] >= 0


def test_config_limits(api):
    data = api.get("/api/config").json()
    assert data["ok"] is True
    assert isinstance(data["version"], str) and data["version"]
    assert data["gpu_agent_enabled"] is True
    expected_cap = int(os.environ.get("LOCALBENCH_MAX_RUNS", "2"))
    assert data["limits"]["max_concurrent_runs"] == expected_cap
    assert data["limits"]["max_ctx"] == 131072
    # abuse guard config (CONTRACT.md "Abuse guard"); additive fields allowed
    rate_limit = data["limits"]["rate_limit"]
    assert {"runs", "window_s"} <= set(rate_limit), rate_limit
    assert isinstance(rate_limit["runs"], int) and isinstance(rate_limit["window_s"], int)
    if "limited_total" in rate_limit:
        assert isinstance(rate_limit["limited_total"], int)
        assert rate_limit["limited_total"] >= 0
    # the suite disables the limiter so tests stay order-independent
    assert rate_limit["runs"] == 0
    assert rate_limit["window_s"] == 600


def test_presets_shape_and_no_secret(api):
    """CONTRACT.md: {id,name,base_url,model,note,has_credential}; api_key never leaves."""
    secret = "sk-preset-shape-SECRET"
    presets_file = Path(os.environ["LOCALBENCH_DATA_DIR"]) / "presets.json"
    presets_file.write_text(
        json.dumps(
            [
                {
                    "id": "shared-mock",
                    "name": "Shared mock",
                    "base_url": "http://127.0.0.1:1/v1",
                    "model": "mock-qwen",
                    "note": "server-side key",
                    "api_key": secret,
                },
                {
                    "name": "Implicit Id Preset",
                    "base_url": "http://example.com/v1",
                    "model": "m",
                    "note": "",
                },
            ]
        )
    )
    data = api.get("/api/presets").json()
    assert data["ok"] is True
    assert len(data["presets"]) == 2
    for preset in data["presets"]:
        assert set(preset) == {"id", "name", "base_url", "model", "note", "has_credential"}, preset
        assert "api_key" not in preset
    by_id = {preset["id"]: preset for preset in data["presets"]}
    assert by_id["shared-mock"]["has_credential"] is True
    assert by_id["shared-mock"]["name"] == "Shared mock"
    assert by_id["shared-mock"]["model"] == "mock-qwen"
    # id is the name slugified when the entry has no explicit id
    assert by_id["implicit-id-preset"]["has_credential"] is False
    assert secret not in json.dumps(data)
    assert secret not in api.get("/api/presets").text


def test_discover_success_against_mock(api, mock_server):
    secret = "sk-discover-secret"
    response = api.post(
        "/api/discover", json={"base_url": mock_server.base_url, "api_key": secret}
    )
    assert response.status_code == 200, response.text[:300]
    data = response.json()
    assert data["ok"] is True
    assert data["latency_ms"] >= 0
    model = data["models"][0]
    assert model["id"] == "mock-qwen"
    assert model["context_length"] == 131072
    # never echo the key back
    assert secret not in response.text
    assert "api_key" not in response.text


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://localhost:8080/v1",
        "http://169.254.169.254/v1",
        "http://[::1]:8080/v1",
        "http://metadata.google.internal/v1",
        "ftp://example.com/v1",
        "file:///etc/passwd",
    ],
)
def test_discover_rejects_ssrf(api, bad_url):
    response = api.post("/api/discover", json={"base_url": bad_url, "api_key": "sk-x"})
    assert response.status_code == 400, (bad_url, response.status_code, response.text[:200])
    data = response.json()
    assert data["ok"] is False
    assert "base_url" in data["error"].lower()


def test_discover_upstream_down_returns_502(api):
    dead = free_port()
    response = api.post(
        "/api/discover", json={"base_url": "http://127.0.0.1:%d/v1" % dead, "api_key": "sk-x"}
    )
    assert response.status_code == 502, response.text[:300]
    assert response.json()["ok"] is False


def test_create_run_rejects_ssrf(api):
    response = api.post("/api/runs", json=run_payload("http://169.254.169.254/v1"))
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_create_run_requires_model(api, mock_server):
    payload = run_payload(mock_server.base_url)
    payload["model"] = "   "
    response = api.post("/api/runs", json=payload)
    assert response.status_code == 400
    assert response.json()["ok"] is False


# ---------------------------------------------------------------------------
# run lifecycle: queued -> running -> done, plus all measurement identities
# ---------------------------------------------------------------------------


def test_run_lifecycle_and_measurement_math(api, mock_server):
    secret = "sk-live-run-secret"
    started = time.perf_counter()
    run_id = post_run(
        api, mock_server.base_url, label="lifecycle run", api_key=secret
    )
    # The POST must return immediately: the model call is a background job.
    assert time.perf_counter() - started < 5.0

    first = get_run(api, run_id)
    assert first["status"] in ("queued", "running"), first["status"]
    assert first["config"]["contexts"] == [512, 1024]
    assert first["agent"] == {"key": None, "connected": False}

    run = wait_terminal(api, run_id)
    assert run["status"] == "done", run.get("error")
    assert run["error"] is None
    assert run["created_at"] > 0
    assert run["started_at"] >= run["created_at"]
    assert run["finished_at"] >= run["started_at"]
    assert run["progress"]["phase"] == "finish"
    assert run["progress"]["pct"] == 100

    # -- overhead ----------------------------------------------------------
    overhead = run["overhead"]
    assert overhead["samples"] == 5
    assert overhead["ping_ms_p50"] > 0

    # -- context ladder ----------------------------------------------------
    results = run["results"]
    points = results["context_points"]
    assert [p["ctx_requested"] for p in points] == [512, 1024]
    assert all(p["ok"] is True for p in points), points
    assert all(p["max_tokens"] == 8 for p in points)
    assert all(p["completion_tokens"] == 8 for p in points)

    for point in points:
        # ttft_corrected_ms = ttft_ms - overhead p50 (floored at 0); prompt_ms
        # follows it (the engine floors prompt_ms at 0.5 so it never divides by 0).
        expected_corrected = max(0.0, point["ttft_ms"] - overhead["ping_ms_p50"])
        assert point["ttft_corrected_ms"] == pytest.approx(expected_corrected, abs=0.02)
        assert point["prompt_ms"] == pytest.approx(max(point["ttft_corrected_ms"], 0.5), abs=0.01)
        # the synthetic prompt is calibrated to land on the rung's token count
        assert point["prompt_tokens"] == pytest.approx(point["ctx_requested"], rel=0.05)
        # prefill identity for whichever source the engine trusted
        assert point["prompt_tokens"] > 0
        if point.get("prefill_source") == "server":
            # mock publishes llama.cpp timings.prompt_per_second; it is used verbatim
            assert point["prefill_tps"] == pytest.approx(point["prefill_tps_server"], rel=1e-6)
            assert point["prefill_tps_server"] > 2500.0
            assert point["prompt_ms_server"] > 0
        else:
            assert point["prefill_tps"] == pytest.approx(identity_prefill(point), rel=1e-3)
        # decode speed should be in the ballpark of the mock's pacing (600 tps)
        assert 200.0 < point["decode_tps"] < 2000.0, point
        # draft stats mapped from llama.cpp timings
        assert point["draft"] == {
            "n": 24,
            "accepted": 15,
            "acceptance_rate": pytest.approx(0.625),
        }
        # and the measured prefill should track the mock's modelled rate (4000)
        assert point["prefill_tps"] is not None
        assert 2500.0 < point["prefill_tps"] < 8000.0, point
        assert point["cache_bust"] is True

    # ladder rungs must report more prompt tokens for larger contexts
    assert points[1]["prompt_tokens"] > points[0]["prompt_tokens"]

    # -- concurrency -------------------------------------------------------
    concurrency_points = results["concurrency_points"]
    assert [p["n"] for p in concurrency_points] == [1, 2]
    for point in concurrency_points:
        assert point["ok"] is True, point
        assert point["wall_s"] > 0
        assert point["completion_tokens"] == 8 * point["n"]
        # agg_tps = total completion tokens / wall clock
        assert point["agg_tps"] == pytest.approx(
            round(point["completion_tokens"] / point["wall_s"], 2), rel=0.02
        )
        assert point["per_req_tps"] > 0
        assert 0 < point["ttft_p50"] <= point["ttft_p95"]

    # -- aggregates (mean/p50/p95/min/max/n per CONTRACT.md) ---------------
    context_ttfts = [p["ttft_ms"] for p in points]
    agg = results["ttft_ms"]
    assert agg["n"] == len(points)
    assert agg["min"] == pytest.approx(min(context_ttfts), abs=0.02)
    assert agg["max"] == pytest.approx(max(context_ttfts), abs=0.02)
    assert agg["mean"] == pytest.approx(sum(context_ttfts) / len(context_ttfts), abs=0.02)
    assert agg["min"] <= agg["p50"] <= agg["p95"] <= agg["max"]

    prefill_agg = results["prompt_tps"]
    prefill_values = [p["prefill_tps"] for p in points if p["prefill_tps"] is not None]
    assert prefill_values, "expected measurable prefill numbers"
    assert prefill_agg["n"] == len(prefill_values)
    assert prefill_agg["min"] == pytest.approx(min(prefill_values), rel=0.01)
    assert prefill_agg["max"] == pytest.approx(max(prefill_values), rel=0.01)

    decode_agg = results["decode_tps"]
    assert decode_agg["n"] == len(points) and decode_agg["mean"] > 0
    total_agg = results["total_s"]
    assert total_agg["n"] == len(points) and total_agg["max"] > 0

    # -- token totals ------------------------------------------------------
    all_ok = points + concurrency_points
    assert results["tokens"]["prompt"] == sum(p["prompt_tokens"] for p in all_ok)
    assert results["tokens"]["completion"] == sum(p["completion_tokens"] for p in all_ok)

    # -- gpu / notes -------------------------------------------------------
    assert results["gpu"]["available"] is False
    assert results["gpu"]["samples_n"] == 0
    assert results["model_load_s"] is None
    assert any("agent" in note.lower() for note in results["notes"])

    # -- privacy: the old run list is withdrawn, counters replace it -------
    assert api.get("/api/runs").status_code == 404
    stats = api.get("/api/stats").json()
    assert stats["ok"] is True
    assert {"ok", "active", "finished", "storage", "ttl_s"} <= set(stats), stats
    if "rate_limited_total" in stats:
        assert isinstance(stats["rate_limited_total"], int)
        assert stats["rate_limited_total"] >= 0
    assert stats["finished"] >= 1
    assert stats["storage"] == "sqlite"  # in-process suite runs PERSIST=1
    # counters only: no ids, labels, models or endpoint hosts leak through
    stats_text = json.dumps(stats)
    for leak in (run_id, "lifecycle run", "mock-qwen", run["endpoint_host"]):
        assert leak not in stats_text, leak
    assert run["label"] == "lifecycle run"
    assert run["endpoint_host"] == mock_server.base_url.split("//")[1].rsplit("/", 1)[0]
    assert run["preset_id"] is None
    # headline values the old summary exposed are derivable from the run itself
    assert results["ttft_ms"]["p50"] > 0
    assert results["decode_tps"]["p50"] > 0


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


def test_sse_events_and_replay(api, mock_server):
    secret = "sk-sse-secret"
    run_id = post_run(api, mock_server.base_url, label="sse run", api_key=secret)

    events = collect_sse(api, run_id)
    assert events[0]["type"] == "snapshot", events[0]
    assert events[0]["run"]["id"] == run_id
    assert events[-1]["type"] == "done", events[-1]
    assert events[-1]["run"]["status"] == "done"

    kinds = [e.get("kind") for e in events if e["type"] == "point"]
    assert kinds.count("context") == 2, kinds
    assert kinds.count("concurrency") == 2, kinds
    assert any(e["type"] == "progress" for e in events)
    # no secrets in the stream
    assert secret not in json.dumps(events)
    assert all(e["run"].get("agent", {}).get("key") is None for e in events if "run" in e)

    # A client reconnecting after completion gets snapshot(complete)+done.
    replay = collect_sse(api, run_id)
    assert replay[0]["type"] == "snapshot"
    assert replay[-1]["type"] == "done"
    replayed_run = replay[0]["run"]
    assert replayed_run["status"] == "done"
    assert len(replayed_run["results"]["context_points"]) == 2
    assert len(replayed_run["results"]["concurrency_points"]) == 2


def test_decode_not_measured_below_min_completion_tokens(api, mock_server):
    """CONTRACT.md: <8 completion tokens -> decode_tps null (+note), no 0.0 rows."""
    config = {
        "contexts": [512],
        "concurrency": [1],
        "concurrency_context": 256,
        "max_tokens": 4,
        "warmup": 0,
        "enable_context_ladder": True,
        "enable_concurrency": True,
    }
    run_id = post_run(api, mock_server.base_url, label="decode floor", config=config)
    run = wait_terminal(api, run_id)
    assert run["status"] == "done", run.get("error")

    point = run["results"]["context_points"][0]
    assert point["ok"] is True
    assert point["completion_tokens"] == 4
    assert point["decode_tps"] is None, point["decode_tps"]

    concurrency_point = run["results"]["concurrency_points"][0]
    assert concurrency_point["completion_tokens"] == 4
    assert concurrency_point["per_req_tps"] is None, concurrency_point["per_req_tps"]

    notes = " ".join(run["results"]["notes"]).lower()
    assert "decode" in notes and "measurable" in notes, run["results"]["notes"]
    # aggregation must drop the unmeasurable samples instead of averaging zeros
    assert run["results"]["decode_tps"]["n"] == 0, run["results"]["decode_tps"]
    assert run["results"]["decode_tps"]["mean"] == 0


# ---------------------------------------------------------------------------
# context ladder failure handling
# ---------------------------------------------------------------------------


def test_ladder_skips_rungs_above_model_context(api, mock_factory):
    srv = mock_factory(ctx=1024, prefill_tps=4000.0)
    config = {
        "contexts": [512, 2048, 4096],
        "concurrency": [1],
        "concurrency_context": 256,
        "max_tokens": 4,
        "warmup": 0,
        "enable_context_ladder": True,
        "enable_concurrency": False,
    }
    run_id = post_run(api, srv.base_url, label="ladder skip", config=config)
    run = wait_terminal(api, run_id)

    assert run["status"] == "done", run.get("error")
    points = run["results"]["context_points"]
    assert [p["ctx_requested"] for p in points] == [512, 2048, 4096]
    assert points[0]["ok"] is True, points[0]
    for point in points[1:]:
        assert point["ok"] is False
        assert point["error"] == "exceeds_model_context", point
        assert point["prompt_tokens"] == 0
    assert any("context_length" in note for note in run["results"]["notes"])


def test_ladder_400_marks_context_exceeded_and_skips_higher(api, mock_factory):
    srv = mock_factory(ctx=1024, hide_context_length=True, prefill_tps=4000.0)
    config = {
        "contexts": [512, 2048, 4096],
        "concurrency": [1],
        "concurrency_context": 256,
        "max_tokens": 4,
        "warmup": 0,
        "enable_context_ladder": True,
        "enable_concurrency": False,
    }
    run_id = post_run(api, srv.base_url, label="ladder 400", config=config)
    run = wait_terminal(api, run_id)

    assert run["status"] == "done", run.get("error")
    points = run["results"]["context_points"]
    assert points[0]["ok"] is True
    assert points[1]["ok"] is False and points[1]["error"] == "context_exceeded"
    # higher rungs are skipped, not attempted again
    assert points[2]["ok"] is False and points[2]["error"] == "context_exceeded"


def test_run_with_only_failing_points_is_failed(api, mock_factory):
    srv = mock_factory(ctx=64, hide_context_length=True)
    config = {
        "contexts": [1024],
        "concurrency": [1],
        "max_tokens": 4,
        "warmup": 0,
        "enable_context_ladder": True,
        "enable_concurrency": False,
    }
    run_id = post_run(api, srv.base_url, label="all fail", config=config)
    run = wait_terminal(api, run_id)
    assert run["status"] == "failed", run
    assert run["error"] and "fail" in run["error"].lower()
    assert run["results"]["context_points"][0]["ok"] is False


# ---------------------------------------------------------------------------
# concurrency cap + queueing
# ---------------------------------------------------------------------------


def test_two_run_cap_and_fifo_queue(api, mock_factory):
    srv = mock_factory(ttft_ms=150.0, tps=100.0, prefill_tps=0.0, draft_n=0)
    config = {
        "contexts": [512],
        "concurrency": [1],
        "concurrency_context": 256,
        "max_tokens": 4,
        "warmup": 0,
        "enable_context_ladder": True,
        "enable_concurrency": False,
    }
    # let any queued runs from earlier tests drain first (clean timing window)
    wait_for(
        lambda: api.get("/api/stats").json()["active"] == 0,
        timeout=60,
        what="the run queue to drain",
    )
    run_ids = [
        post_run(api, srv.base_url, label="cap run %d" % i, config=config) for i in range(4)
    ]

    saw_queued = False
    deadline = time.time() + 60
    while time.time() < deadline:
        statuses = [get_run(api, rid)["status"] for rid in run_ids]
        if "queued" in statuses:
            saw_queued = True
        if all(status in TERMINAL for status in statuses):
            break
        time.sleep(0.05)

    assert saw_queued, "expected at least one run queued behind the 2-run cap"

    final = [get_run(api, rid) for rid in run_ids]
    assert all(run["status"] == "done" for run in final), [run["status"] for run in final]
    # FIFO: earlier runs started no later than later ones
    starts = [run["started_at"] for run in final]
    assert starts == sorted(starts), starts

    # cap proof from the run documents: at no instant did 3 executions overlap
    intervals = [(run["started_at"], run["finished_at"]) for run in final]
    max_overlap = 0
    for start, _finish in intervals:
        covering = sum(1 for s2, f2 in intervals if s2 <= start < f2)
        max_overlap = max(max_overlap, covering)
    assert max_overlap <= 2, (
        "max_concurrent_runs=2 was exceeded: %d overlapping executions, intervals=%r"
        % (max_overlap, intervals)
    )


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_running_run(api, mock_factory):
    srv = mock_factory(ttft_ms=250.0, tps=80.0, prefill_tps=0.0, draft_n=0)
    config = {
        "contexts": [4096],
        "concurrency": [1],
        "concurrency_context": 2048,
        "max_tokens": 16,
        "warmup": 0,
        "enable_context_ladder": True,
        "enable_concurrency": True,
    }
    run_id = post_run(api, srv.base_url, label="cancel me", config=config)
    wait_for(
        lambda: get_run(api, run_id)["status"] == "running",
        timeout=10,
        what="run to start",
    )
    response = api.post("/api/runs/%s/cancel" % run_id, headers=headers_for(run_id))
    assert response.status_code == 200 and response.json()["ok"] is True

    run = wait_terminal(api, run_id, timeout=10)
    assert run["status"] == "cancelled", run
    assert run["finished_at"] is not None
    replay = collect_sse(api, run_id)
    assert replay[-1]["type"] == "done"
    assert replay[-1]["run"]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# persistence / secrets
# ---------------------------------------------------------------------------


def test_api_key_never_persisted(api, mock_server, store_mod):
    secret = "sk-DO-NOT-PERSIST-%d" % time.time_ns()
    run_id = post_run(api, mock_server.base_url, label="secret run", api_key=secret)
    run = wait_terminal(api, run_id)
    assert run["status"] == "done"

    stored = get_run(api, run_id)
    assert secret not in json_text(stored)
    assert stored["agent"]["key"] is None
    assert "api_key" not in json_text(stored)

    stats = api.get("/api/stats").json()
    assert secret not in json_text(stats)
    export = api.get("/api/runs/%s/export?format=json" % run_id, headers=headers_for(run_id))
    assert secret not in export.text

    dump = sqlite_dump_text(TEST_DB)
    assert dump, "expected the run to be persisted to %s" % TEST_DB
    assert secret not in dump, "api_key leaked into the sqlite database"
    assert "ak_" in dump  # the per-run agent key lives in its own column, by design


def test_delete_run(api, mock_server):
    run_id = post_run(api, mock_server.base_url, label="delete me")
    wait_terminal(api, run_id)
    response = api.delete("/api/runs/%s" % run_id, headers=headers_for(run_id))
    assert response.status_code == 200 and response.json()["ok"] is True
    assert api.get("/api/runs/%s" % run_id, headers=headers_for(run_id)).status_code == 404


def test_unknown_run_is_404(api):
    assert api.get("/api/runs/r_deadbeef").status_code == 404
    assert api.get("/api/runs/r_deadbeef/agent").status_code == 404
    assert api.post("/api/runs/r_deadbeef/cancel").status_code == 404
    assert api.delete("/api/runs/r_deadbeef").status_code == 404
    assert api.get("/api/runs/r_deadbeef/export?format=csv").status_code == 404


# ---------------------------------------------------------------------------
# export content
# ---------------------------------------------------------------------------


def test_export_json_and_csv_content(api, mock_server):
    run_id = post_run(api, mock_server.base_url, label="export run")
    run = wait_terminal(api, run_id)
    assert run["status"] == "done"
    context_points = run["results"]["context_points"]
    concurrency_points = run["results"]["concurrency_points"]

    json_export = api.get("/api/runs/%s/export?format=json" % run_id, headers=headers_for(run_id))
    assert json_export.status_code == 200
    assert "application/json" in json_export.headers["content-type"]
    assert "attachment" in json_export.headers["content-disposition"]
    assert ("%s.json" % run_id) in json_export.headers["content-disposition"]
    exported = json.loads(json_export.text)
    assert exported["id"] == run_id
    assert exported["label"] == "export run"
    assert len(exported["results"]["context_points"]) == len(context_points)

    csv_export = api.get("/api/runs/%s/export?format=csv" % run_id, headers=headers_for(run_id))
    assert csv_export.status_code == 200
    assert "text/csv" in csv_export.headers["content-type"]
    assert "attachment" in csv_export.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(csv_export.text)))
    header = rows[0]
    for column in ("section", "ctx_requested", "prompt_tokens", "ttft_ms", "prefill_tps",
                   "decode_tps", "agg_tps", "ok", "draft_n", "acceptance_rate",
                   "prefill_source"):
        assert column in header, column

    by_section = {name: [] for name in ("context", "concurrency")}
    for row in rows[1:]:
        assert len(row) == len(header), (
            "CSV row has %d cells but the header has %d -> columns are misaligned: %r"
            % (len(row), len(header), row)
        )
        record = dict(zip(header, row))
        if record["section"] in by_section:
            by_section[record["section"]].append(record)

    assert len(by_section["context"]) == len(context_points)
    assert len(by_section["concurrency"]) == len(concurrency_points)
    first = by_section["context"][0]
    assert first["ctx_requested"] == "512"
    assert first["ok"] == "True"
    assert int(first["prompt_tokens"]) > 0
    assert float(first["prefill_tps"]) > 0
    assert first["prefill_source"] in ("server", "derived", "unreliable")
    assert first["draft_n"] == "24"
    assert first["draft_accepted"] == "15"
    assert float(first["acceptance_rate"]) == pytest.approx(0.625, abs=0.001)
    assert by_section["concurrency"][0]["n"] == "1"
    assert float(by_section["concurrency"][0]["agg_tps"]) > 0

    assert (
        api.get(
            "/api/runs/%s/export?format=xml" % run_id, headers=headers_for(run_id)
        ).status_code
        == 400
    )


def test_strip_secrets_helper(store_mod):
    doc = {
        "api_key": "sk-x",
        "nested": {"token": "abc", "authorization": "Bearer z", "keep": 1},
        "results": {"notes": ["ok"]},
    }
    stripped = store_mod.strip_secrets(doc)
    assert stripped == {"nested": {"keep": 1}, "results": {"notes": ["ok"]}}
