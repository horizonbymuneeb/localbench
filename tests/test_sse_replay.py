"""Memory-only storage, per-run access tokens and TTL eviction — regression tests.

Covers the CONTRACT.md privacy model:
* ``POST /api/runs`` mints an ``access_token``; every run-scoped endpoint needs
  ``X-Run-Token`` (``?token=`` for EventSource) and answers the *same* 404 as an
  unknown id when it is missing/wrong (no existence oracle).
* ``GET /api/stats`` replaces the withdrawn ``GET /api/runs`` list (counters only).
* a finished run still in memory replays ``snapshot`` -> ``done``.
* with ``LOCALBENCH_PERSIST=0`` nothing is written to disk and runs disappear
  ``LOCALBENCH_TTL_S`` seconds after they finish (and after a restart).

The in-process tests run in persistence mode (``LOCALBENCH_DB`` is set by
conftest); the storage/TTL tests spawn real uvicorn processes with
``LOCALBENCH_PERSIST=0`` because that is the production shape.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from conftest import free_port, parse_sse_lines, run_payload, wait_http_ok

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


# --------------------------------------------------------------------------- #
# helpers (self-contained: the conftest helpers predate per-run tokens)
# --------------------------------------------------------------------------- #
def _create_run(client, base_url: str, label: str = "token test", **kwargs) -> Tuple[str, str]:
    response = client.post("/api/runs", json=run_payload(base_url, label=label, **kwargs))
    assert response.status_code == 200, (response.status_code, response.text[:400])
    data = response.json()
    assert data.get("ok") is True, data
    token = data.get("access_token")
    assert isinstance(token, str) and token.startswith("rt_") and len(token) >= 16, data
    return data["run_id"], token


def _get_run(client, run_id: str, token: Optional[str]) -> Any:
    headers = {"X-Run-Token": token} if token else {}
    return client.get("/api/runs/%s" % run_id, headers=headers)


def _wait_terminal(client, run_id: str, token: str, timeout: float = 60.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        response = _get_run(client, run_id, token)
        assert response.status_code == 200, (response.status_code, response.text[:300])
        last = response.json()["run"]
        if last.get("status") in ("done", "failed", "cancelled"):
            return last
        time.sleep(0.1)
    raise AssertionError("run %s did not finish (last status=%r)" % (run_id, last.get("status")))


def _events(client, run_id: str, token: Optional[str] = None) -> List[Dict[str, Any]]:
    url = "/api/runs/%s/events" % run_id
    if token is not None:
        url += "?token=%s" % token
    response = client.get(url)
    assert response.status_code == 200, (response.status_code, response.text[:300])
    return parse_sse_lines(response.iter_lines())


# --------------------------------------------------------------------------- #
# token enforcement (in-process, persistence mode)
# --------------------------------------------------------------------------- #
def test_run_token_is_minted_and_required(api, mock_server):
    run_id, token = _create_run(api, mock_server.base_url, label="token enforcement")
    finished = _wait_terminal(api, run_id, token)
    assert finished["status"] == "done"

    # correct token
    assert _get_run(api, run_id, token).status_code == 200
    # missing token -> identical 404 to an unknown id (no existence oracle)
    missing = _get_run(api, run_id, None)
    unknown = _get_run(api, "r_deadbeef", None)
    assert missing.status_code == 404 and unknown.status_code == 404
    assert missing.json() == unknown.json() == {"ok": False, "error": "run not found"}
    # wrong token
    assert _get_run(api, run_id, "rt_" + "x" * 24).status_code == 404
    # events: header, ?token=, and no token
    assert api.get("/api/runs/%s/events" % run_id).status_code == 404
    assert api.get(
        "/api/runs/%s/events" % run_id, headers={"X-Run-Token": token}
    ).status_code == 200
    assert api.get("/api/runs/%s/events?token=%s" % (run_id, token)).status_code == 200
    # cancel / delete / agent / export all need the token
    for method, path in (
        ("get", "/api/runs/%s/agent" % run_id),
        ("get", "/api/runs/%s/export?format=json" % run_id),
        ("post", "/api/runs/%s/cancel" % run_id),
        ("delete", "/api/runs/%s" % run_id),
    ):
        response = getattr(api, method)(path)
        assert response.status_code == 404, (method, path, response.status_code)


def test_tokens_are_per_run_not_shared(api, mock_server):
    run_a, token_a = _create_run(api, mock_server.base_url, label="isolation A")
    run_b, token_b = _create_run(api, mock_server.base_url, label="isolation B")
    _wait_terminal(api, run_a, token_a)
    _wait_terminal(api, run_b, token_b)
    assert token_a != token_b
    # A's token cannot read B and vice versa
    assert _get_run(api, run_b, token_a).status_code == 404
    assert _get_run(api, run_a, token_b).status_code == 404
    assert api.get("/api/runs/%s/events?token=%s" % (run_b, token_a)).status_code == 404


def test_finished_run_replays_snapshot_then_done(api, mock_server):
    run_id, token = _create_run(api, mock_server.base_url, label="finished replay")
    _wait_terminal(api, run_id, token)

    events = _events(api, run_id, token)
    assert events[0]["type"] == "snapshot", events[0]
    assert events[0]["run"]["id"] == run_id
    assert events[0]["run"]["status"] == "done"
    assert events[0]["run"]["results"]["context_points"]
    assert events[0]["run"]["overhead"]["ping_ms_p50"] > 0
    assert events[-1]["type"] == "done", events[-1]
    assert events[-1]["run"]["id"] == run_id


def test_agent_and_export_work_with_token(api, mock_server):
    run_id, token = _create_run(api, mock_server.base_url, label="agent/export token")
    _wait_terminal(api, run_id, token)
    headers = {"X-Run-Token": token}

    agent = api.get("/api/runs/%s/agent" % run_id, headers=headers)
    assert agent.status_code == 200, agent.text[:300]
    body = agent.json()
    assert body["agent_key"]
    assert "--run %s" % run_id in body["command"]
    assert body["status"] == "done" and body["run_finished"] is True

    export_json = api.get("/api/runs/%s/export?format=json" % run_id, headers=headers)
    assert export_json.status_code == 200
    assert export_json.json()["id"] == run_id
    assert "attachment" in export_json.headers.get("content-disposition", "")

    export_csv = api.get("/api/runs/%s/export?format=csv" % run_id, headers=headers)
    assert export_csv.status_code == 200
    assert "text/csv" in export_csv.headers.get("content-type", "")


def test_stats_replaces_the_run_list(api, mock_server):
    run_id, token = _create_run(api, mock_server.base_url, label="stats counters")
    _wait_terminal(api, run_id, token)

    stats = api.get("/api/stats")
    assert stats.status_code == 200
    body = stats.json()
    assert body["ok"] is True
    assert set(body) == {
        "ok",
        "active",
        "finished",
        "storage",
        "ttl_s",
        "rate_limited_total",
    }, body
    assert isinstance(body["active"], int) and isinstance(body["finished"], int)
    assert body["finished"] >= 1
    assert body["storage"] in ("memory", "sqlite")
    assert isinstance(body["ttl_s"], int) and body["ttl_s"] > 0
    assert isinstance(body["rate_limited_total"], int) and body["rate_limited_total"] >= 0
    # counters only: no ids, labels, models or endpoints anywhere in the body
    raw = json.dumps(body)
    for leak in (run_id, token, mock_server.base_url, "stats counters"):
        assert leak not in raw

    # the privacy-leaking list endpoint is gone
    assert api.get("/api/runs").status_code == 404


# --------------------------------------------------------------------------- #
# memory-only mode + TTL (real processes, production shape)
# --------------------------------------------------------------------------- #
def _start_server(env_extra: Dict[str, str], port: int, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("LOCALBENCH_DB", None)
    env.pop("LOCALBENCH_PERSIST", None)
    env.update(env_extra)
    log = log_path.open("wb")
    proc = subprocess.Popen(
        [
            str(VENV_PYTHON),
            "-m",
            "uvicorn",
            "backend.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    proc._localbench_log = log  # type: ignore[attr-defined]
    return proc


def _stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover
        proc.kill()
        proc.wait(timeout=10)
    log = getattr(proc, "_localbench_log", None)
    if log is not None:
        log.close()


def _post_and_finish(base: str, mock_base_url: str) -> Tuple[str, str, Dict[str, Any]]:
    import httpx

    with httpx.Client(timeout=60.0) as client:
        created = client.post(
            base + "/api/runs", json=run_payload(mock_base_url, label="memory mode run")
        )
        assert created.status_code == 200, created.text[:400]
        data = created.json()
        run_id, token = data["run_id"], data["access_token"]
        deadline = time.time() + 60.0
        doc: Dict[str, Any] = {}
        while time.time() < deadline:
            response = client.get(base + "/api/runs/" + run_id, headers={"X-Run-Token": token})
            assert response.status_code == 200, response.text[:300]
            doc = response.json()["run"]
            if doc.get("status") in ("done", "failed", "cancelled"):
                break
            time.sleep(0.1)
        assert doc.get("status") == "done", doc.get("status")
        return run_id, token, doc


@pytest.mark.skipif(not VENV_PYTHON.exists(), reason="venv python not available")
def test_memory_only_storage_ttl_and_restart(mock_server, tmp_path):
    """No sqlite file, memory-only log line, 404 after TTL, gone after restart."""
    import httpx

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env = {
        "LOCALBENCH_PERSIST": "0",
        "LOCALBENCH_TTL_S": "3",
        "LOCALBENCH_SWEEP_S": "1",
        "LOCALBENCH_DATA_DIR": str(data_dir),
        "LOCALBENCH_RATE_LIMIT": "0/600",
    }
    port = free_port()
    log_path = tmp_path / "memory.log"
    proc = _start_server(env, port, log_path)
    base = "http://127.0.0.1:%d" % port
    run_id = token = ""
    try:
        wait_http_ok(base + "/healthz")
        run_id, token, _doc = _post_and_finish(base, mock_server.base_url)

        # (a) nothing was written to disk, and the mode is logged
        assert list(data_dir.iterdir()) == [], list(data_dir.iterdir())
        assert "storage: memory-only ttl=3s" in log_path.read_text(errors="replace")

        # (b) readable during the TTL
        with httpx.Client(timeout=30.0) as client:
            assert client.get(
                base + "/api/runs/" + run_id, headers={"X-Run-Token": token}
            ).status_code == 200
            assert client.get(
                base + "/api/runs/%s/events?token=%s" % (run_id, token)
            ).status_code == 200
            stats = client.get(base + "/api/stats").json()
            assert stats["storage"] == "memory" and stats["finished"] >= 1

        # (c) evicted after the TTL -> 404 for run, events, export, agent
        deadline = time.time() + 15.0
        gone = False
        while time.time() < deadline:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(
                    base + "/api/runs/" + run_id, headers={"X-Run-Token": token}
                )
                if response.status_code == 404:
                    assert response.json() == {"ok": False, "error": "run not found"}
                    assert client.get(
                        base + "/api/runs/%s/events?token=%s" % (run_id, token)
                    ).status_code == 404
                    assert client.get(
                        base + "/api/runs/%s/export?format=json" % run_id,
                        headers={"X-Run-Token": token},
                    ).status_code == 404
                    assert client.get(
                        base + "/api/runs/%s/agent" % run_id,
                        headers={"X-Run-Token": token},
                    ).status_code == 404
                    gone = True
                    break
            time.sleep(0.5)
        assert gone, "run was never evicted after LOCALBENCH_TTL_S"
    finally:
        _stop_server(proc)

    # (d) after a restart the run is simply gone (nothing was persisted)
    port2 = free_port()
    log2 = tmp_path / "memory2.log"
    proc2 = _start_server(env, port2, log2)
    base2 = "http://127.0.0.1:%d" % port2
    try:
        wait_http_ok(base2 + "/healthz")
        with httpx.Client(timeout=10.0) as client:
            assert client.get(
                base2 + "/api/runs/" + run_id, headers={"X-Run-Token": token}
            ).status_code == 404
            stats = client.get(base2 + "/api/stats").json()
            assert stats["ok"] is True
            assert stats["active"] == 0 and stats["finished"] == 0
            assert stats["storage"] == "memory" and stats["ttl_s"] == 3
            assert isinstance(stats["rate_limited_total"], int)
        assert list(data_dir.iterdir()) == []
    finally:
        _stop_server(proc2)


@pytest.mark.skipif(not VENV_PYTHON.exists(), reason="venv python not available")
def test_rate_limit_limits_public_but_spares_loopback(mock_server, tmp_path):
    """Abuse guard: 6/600 per public IP, operator on loopback never counted."""
    import httpx

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env = {
        "LOCALBENCH_PERSIST": "0",
        "LOCALBENCH_TTL_S": "900",
        "LOCALBENCH_SWEEP_S": "30",
        "LOCALBENCH_DATA_DIR": str(data_dir),
        "LOCALBENCH_RATE_LIMIT": "6/600",
    }
    port = free_port()
    proc = _start_server(env, port, tmp_path / "ratelimit.log")
    base = "http://127.0.0.1:%d" % port
    public = {"X-Forwarded-For": "198.51.100.77"}
    body = {
        "label": "rate limit probe",
        "base_url": mock_server.base_url,
        "model": "mock-qwen",
        "api_key": "k",
        "config": {"contexts": [512], "concurrency": [1], "concurrency_context": 256,
                   "max_tokens": 8, "warmup": 0, "idle_sample_s": 0},
    }
    try:
        wait_http_ok(base + "/healthz")
        with httpx.Client(timeout=30.0) as client:
            codes = [
                client.post(base + "/api/runs", json=body, headers=public).status_code
                for _ in range(7)
            ]
            assert codes[:6] == [200] * 6, codes
            assert codes[6] == 429, codes
            rejected = client.post(base + "/api/runs", json=body, headers=public)
            assert rejected.json()["error"] == "rate_limited"
            assert isinstance(rejected.json()["retry_after"], int)
            assert rejected.json()["retry_after"] > 0

            # operator on loopback (no X-Forwarded-For) is exempt while the
            # public IP is limited
            direct = client.post(base + "/api/runs", json=body)
            assert direct.status_code == 200, direct.text[:300]
            assert direct.json().get("access_token", "").startswith("rt_")
            # ... and X-Real-IP loopback is exempt too
            real_ip = client.post(
                base + "/api/runs", json=body, headers={"X-Real-IP": "127.0.0.1"}
            )
            assert real_ip.status_code == 200, real_ip.text[:300]

            stats = client.get(base + "/api/stats").json()
            assert stats["rate_limited_total"] >= 2  # both rejected attempts counted
    finally:
        _stop_server(proc)
