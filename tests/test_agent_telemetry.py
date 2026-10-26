"""GPU telemetry agent API tests: key auth, ingest, aggregates, model_load_s."""

from __future__ import annotations

import time

import pytest

from conftest import get_run, headers_for, post_run, wait_terminal


def finished_run(api, mock_server, label="telemetry run"):
    run_id = post_run(api, mock_server.base_url, label=label)
    run = wait_terminal(api, run_id)
    assert run["status"] == "done", run.get("error")
    return run_id, run


def agent_key(api, run_id):
    response = api.get("/api/runs/%s/agent" % run_id, headers=headers_for(run_id))
    assert response.status_code == 200, response.text[:300]
    data = response.json()
    assert data["ok"] is True
    assert isinstance(data["agent_key"], str) and data["agent_key"].startswith("ak_")
    assert data["agent_connected"] is False or data["agent_connected"] is True
    return data


def test_agent_endpoint_returns_key_and_command(api, mock_server):
    run_id, _ = finished_run(api, mock_server)
    data = agent_key(api, run_id)
    command = data["command"]
    assert "bench_agent.py" in command
    assert run_id in command
    assert data["agent_key"] in command
    assert "--run" in command and "--key" in command
    # the endpoint never leaks the LLM api key; only the per-run agent key
    assert "sk-" not in command
    # a finished run's agent info also reports the terminal status
    if "status" in data:
        assert data["status"] == "done"
        assert data["run_finished"] is True


def test_telemetry_requires_valid_agent_key(api, mock_server):
    run_id, _ = finished_run(api, mock_server)
    sample = {"samples": [{"ts": time.time(), "vram_mb": 100, "ram_mb": 50, "gpu_util": 10,
                           "power_w": 40, "temp_c": 40, "phase": "idle"}]}
    assert api.post("/api/runs/%s/telemetry" % run_id, json=sample).status_code == 403
    assert api.post(
        "/api/runs/%s/telemetry" % run_id,
        json=sample,
        headers={"X-Agent-Key": "ak_wrong"},
    ).status_code == 403
    assert api.post(
        "/api/runs/r_doesnotexist/telemetry",
        json=sample,
        headers={"X-Agent-Key": "ak_x"},
    ).status_code == 404


def test_gpu_aggregates_idle_peak_avg_and_model_load(api, mock_server):
    run_id, run = finished_run(api, mock_server, label="gpu aggregates")
    key = agent_key(api, run_id)["agent_key"]

    idle_samples = [
        {"ts": 1699999999.0 + i, "vram_mb": 1000 + i, "ram_mb": 2000, "gpu_util": 10,
         "power_w": 50, "temp_c": 40, "phase": "idle"}
        for i in range(4)
    ]
    busy_samples = [
        {"ts": 1699999999.0 + 100 + i, "vram_mb": 18000 + i, "ram_mb": 3000, "gpu_util": 97,
         "power_w": 250, "temp_c": 71, "phase": "context:512"}
        for i in range(8)
    ]
    response = api.post(
        "/api/runs/%s/telemetry" % run_id,
        json={"samples": idle_samples + busy_samples},
        headers={"X-Agent-Key": key},
    )
    assert response.status_code == 200, response.text[:300]
    assert response.json()["received"] == 12

    response = api.post(
        "/api/runs/%s/telemetry" % run_id,
        json={"model_load_s": 12.4},
        headers={"X-Agent-Key": key},
    )
    assert response.status_code == 200

    run = get_run(api, run_id)
    gpu = run["results"]["gpu"]
    assert gpu["available"] is True
    assert gpu["samples_n"] == 12
    # idle = mean of the idle-phase baseline
    assert gpu["idle"]["vram_mb"] == pytest.approx(1001.5, abs=0.1)
    assert gpu["idle"]["gpu_util"] == pytest.approx(10.0)
    # peak = per-field max (downsample-safe)
    assert gpu["peak"]["vram_mb"] == pytest.approx(18007.0)
    assert gpu["peak"]["gpu_util"] == pytest.approx(97.0)
    assert gpu["peak"]["power_w"] == pytest.approx(250.0)
    # avg = mean over all samples
    assert gpu["avg"]["vram_mb"] == pytest.approx(12336.2, abs=1.0)
    assert gpu["idle"]["vram_mb"] < gpu["avg"]["vram_mb"] < gpu["peak"]["vram_mb"]
    assert run["results"]["model_load_s"] == pytest.approx(12.4)

    # gpu.available is what the History tab uses to flag runs with telemetry
    assert get_run(api, run_id)["results"]["gpu"]["available"] is True

    # telemetry ingest marks the agent as connected on the run document
    assert get_run(api, run_id)["agent"]["connected"] is True


def test_telemetry_ingest_while_running(api, mock_factory):
    """The live path: agent posts samples during the run; aggregates land at finish."""
    srv = mock_factory(ttft_ms=250.0, tps=80.0, prefill_tps=0.0, draft_n=0)
    config = {
        "contexts": [2048],
        "concurrency": [1],
        "concurrency_context": 1024,
        "max_tokens": 8,
        "warmup": 0,
        "enable_context_ladder": True,
        "enable_concurrency": True,
    }
    run_id = post_run(api, srv.base_url, label="live telemetry", config=config)
    key = agent_key(api, run_id)["agent_key"]

    posted = 0
    for batch in range(3):
        samples = [
            {"ts": time.time(), "vram_mb": 5000 + batch, "ram_mb": 1000, "gpu_util": 50,
             "power_w": 120, "temp_c": 60, "phase": "context"}
            for _ in range(4)
        ]
        response = api.post(
            "/api/runs/%s/telemetry" % run_id,
            json={"samples": samples},
            headers={"X-Agent-Key": key},
        )
        assert response.status_code == 200, response.text[:300]
        posted += response.json()["received"]

    run = wait_terminal(api, run_id, timeout=30)
    assert run["status"] == "done", run.get("error")
    if run["results"]["gpu"]["available"]:
        assert run["results"]["gpu"]["samples_n"] >= min(12, posted)
        assert run["agent"]["connected"] is True
    else:  # pragma: no cover - only if the run beat the first POST
        assert run["results"]["gpu"]["samples_n"] == 0


def test_telemetry_downsample_caps_samples(api, mock_server):
    """CONTRACT: server keeps <= 4000 samples per run (bucket-max downsample)."""
    run_id, _ = finished_run(api, mock_server, label="downsample")
    key = agent_key(api, run_id)["agent_key"]

    batch = {
        "samples": [
            {"ts": time.time() + i, "vram_mb": 1000 + (i % 100), "ram_mb": 500,
             "gpu_util": i % 100, "power_w": 100, "temp_c": 50, "phase": "context"}
            for i in range(512)
        ]
    }
    for _ in range(10):  # 5120 samples total
        response = api.post(
            "/api/runs/%s/telemetry" % run_id,
            json=batch,
            headers={"X-Agent-Key": key},
        )
        assert response.status_code == 200
        kept = response.json()["samples"]
        assert kept <= 4000

    run = get_run(api, run_id)
    gpu = run["results"]["gpu"]
    assert gpu["available"] is True
    assert gpu["samples_n"] <= 4000, gpu["samples_n"]
    assert gpu["peak"]["gpu_util"] == pytest.approx(99.0)  # peaks survive the cap
