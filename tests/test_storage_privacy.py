"""Privacy/storage proofs for the memory-only model (CONTRACT.md "Storage model").

* The default (production) shape writes **nothing** to disk: no runs.db, no
  wal/shm, no exports. It also must not put labels, endpoints, api keys or run
  tokens into the server log.
* The suite itself runs the sqlite shape (``LOCALBENCH_PERSIST=1``) on a tmp DB,
  which must stay covered but never touch the repo's ``data/`` dir; persistence
  mode deliberately keeps rows past the TTL for debugging.
* In memory-only mode a finished run is evicted ``LOCALBENCH_TTL_S`` after it
  finishes, and then 404s on every run-scoped endpoint.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, Tuple

import httpx
import pytest

from conftest import (
    ROOT,
    TEST_DB,
    get_run,
    headers_for,
    post_run,
    sqlite_dump_text,
    wait_for,
    wait_terminal,
)

VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


# ---------------------------------------------------------------------------
# helpers: real uvicorn in the production (memory-only) shape
# ---------------------------------------------------------------------------


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
    proc._lb_log = log  # type: ignore[attr-defined]
    return proc


def _stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover
        proc.kill()
        proc.wait(timeout=10)
    log = getattr(proc, "_lb_log", None)
    if log is not None:
        log.close()


def _memory_env(tmp_path: Path, ttl_s: str = "900", sweep_s: str = "1") -> Dict[str, str]:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return {
        "LOCALBENCH_PERSIST": "0",
        "LOCALBENCH_TTL_S": ttl_s,
        "LOCALBENCH_SWEEP_S": sweep_s,
        "LOCALBENCH_DATA_DIR": str(data),
        "LOCALBENCH_RATE_LIMIT": "0/600",
    }


def _run_to_done(base: str, payload: Dict[str, object]) -> Tuple[str, str, Dict]:
    from conftest import free_port, run_payload  # noqa: F401  (import kept local)

    with httpx.Client(timeout=60.0) as client:
        created = client.post(base + "/api/runs", json=payload)
        assert created.status_code == 200, created.text[:400]
        body = created.json()
        run_id, token = body["run_id"], body["access_token"]
        deadline = time.time() + 60.0
        doc: Dict = {}
        while time.time() < deadline:
            response = client.get(
                base + "/api/runs/" + run_id, headers={"X-Run-Token": token}
            )
            assert response.status_code == 200, response.text[:300]
            doc = response.json()["run"]
            if doc.get("status") in ("done", "failed", "cancelled"):
                return run_id, token, doc
            time.sleep(0.1)
        raise AssertionError("run %s did not finish: %r" % (run_id, doc.get("status")))


MEMORY_CONFIG = {
    "contexts": [64],
    "concurrency": [1],
    "concurrency_context": 64,
    "max_tokens": 4,
    "warmup": 0,
    "enable_concurrency": False,
}


# ---------------------------------------------------------------------------
# 1) memory-only: nothing on disk, and the log leaks nothing
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not VENV_PYTHON.exists(), reason="venv python not available")
def test_memory_mode_writes_no_files_and_logs_no_secrets(mock_factory, tmp_path):
    from conftest import free_port, run_payload, wait_http_ok

    srv = mock_factory(ttft_ms=5.0, prefill_tps=4000.0)
    env = _memory_env(tmp_path, ttl_s="900")
    port = free_port()
    log_path = tmp_path / "server.log"
    proc = _start_server(env, port, log_path)
    base = "http://127.0.0.1:%d" % port
    secret_key = "sk-LEAKCHECK-%s" % uuid.uuid4().hex
    label = "secret-label-%s" % uuid.uuid4().hex
    try:
        wait_http_ok(base + "/healthz", timeout=40)
        payload = run_payload(
            srv.base_url,
            label=label,
            api_key=secret_key,
            config=MEMORY_CONFIG,
        )
        run_id, token, doc = _run_to_done(base, payload)
        assert doc["status"] == "done", doc.get("error")

        # (a) no benchmark data on disk — only the empty data dir
        data_dir = Path(env["LOCALBENCH_DATA_DIR"])
        assert [p.name for p in data_dir.rglob("*") if p.is_file()] == []

        # (b) the mode is announced, and no secret/endpoint/label reached the log
        log_text = log_path.read_text(errors="replace")
        assert "storage: memory-only ttl=900s" in log_text, log_text[-2000:]
        for leak in (secret_key, label, token, srv.base_url, srv.base_url.split("//")[1].rsplit("/", 1)[0]):
            assert leak not in log_text, "leaked %r into server log" % leak

        # (c) still readable in RAM right after finishing
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                base + "/api/runs/" + run_id, headers={"X-Run-Token": token}
            )
            assert response.status_code == 200
            stats = client.get(base + "/api/stats").json()
            assert stats["storage"] == "memory"
            assert stats["finished"] >= 1
    finally:
        _stop_server(proc)


# ---------------------------------------------------------------------------
# 2) TTL eviction in memory-only mode
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not VENV_PYTHON.exists(), reason="venv python not available")
def test_memory_mode_ttl_evicts_finished_run(mock_factory, tmp_path):
    from conftest import free_port, run_payload, wait_http_ok

    srv = mock_factory(ttft_ms=5.0, prefill_tps=4000.0)
    env = _memory_env(tmp_path, ttl_s="2", sweep_s="1")
    port = free_port()
    log_path = tmp_path / "ttl.log"
    proc = _start_server(env, port, log_path)
    base = "http://127.0.0.1:%d" % port
    try:
        wait_http_ok(base + "/healthz", timeout=40)
        run_id, token, doc = _run_to_done(
            base, run_payload(srv.base_url, label="ttl run", config=MEMORY_CONFIG)
        )
        assert doc["status"] == "done"
        headers = {"X-Run-Token": token}

        with httpx.Client(timeout=10.0) as client:
            assert client.get(base + "/api/runs/" + run_id, headers=headers).status_code == 200
            # wait for TTL + sweep without blocking on the automaton
            gone = False
            deadline = time.time() + 20.0
            while time.time() < deadline:
                response = client.get(base + "/api/runs/" + run_id, headers=headers)
                if response.status_code == 404:
                    assert response.json() == {"ok": False, "error": "run not found"}
                    gone = True
                    break
                time.sleep(0.4)
            assert gone, "run never evicted after LOCALBENCH_TTL_S=2"
            # every run-scoped endpoint is gone with it
            assert (
                client.get(
                    base + "/api/runs/%s/events?token=%s" % (run_id, token)
                ).status_code
                == 404
            )
            assert (
                client.get(
                    base + "/api/runs/%s/export?format=json" % run_id, headers=headers
                ).status_code
                == 404
            )
            assert (
                client.get(base + "/api/runs/%s/agent" % run_id, headers=headers).status_code
                == 404
            )
            stats = client.get(base + "/api/stats").json()
            assert stats["active"] == 0
            assert stats["finished"] == 0
        # still nothing on disk
        data_dir = Path(env["LOCALBENCH_DATA_DIR"])
        assert [p.name for p in data_dir.rglob("*") if p.is_file()] == []
    finally:
        _stop_server(proc)


# ---------------------------------------------------------------------------
# 3) persistence mode (test-only): tmp sqlite only, rows outlive the TTL
# ---------------------------------------------------------------------------


def test_persist_mode_uses_tmp_sqlite_and_keeps_rows(api, mock_server, monkeypatch):
    """LOCALBENCH_PERSIST=1 covers the sqlite store, but only on a tmp DB."""
    store = importlib.import_module("backend.store")
    assert store.backend_name() == "sqlite"
    # the suite's DB is a tmp file, never the repo's data/runs.db
    assert str(TEST_DB).startswith("/tmp/"), TEST_DB
    assert TEST_DB.exists(), "sqlite backend should have created %s" % TEST_DB
    assert TEST_DB != ROOT / "data" / "runs.db"

    run_id = post_run(api, mock_server.base_url, label="persist run")
    run = wait_terminal(api, run_id)
    assert run["status"] == "done"
    dump = sqlite_dump_text(TEST_DB)
    assert run_id in dump, "sqlite store did not retain the run"

    # persist mode keeps the row past the TTL (debugging), unlike memory mode
    monkeypatch.setenv("LOCALBENCH_TTL_S", "1")
    time.sleep(1.3)
    assert store.evict_expired() == [], "sqlite backend must not TTL-evict rows"
    assert store.get_run(run_id) is not None, "sqlite row vanished"
    assert run_id in sqlite_dump_text(TEST_DB)
    # the API still 404s: run tokens are memory-only, so an aged run cannot be
    # authorised again even though the debugging row survives on disk
    assert (
        api.get("/api/runs/%s" % run_id, headers=headers_for(run_id)).status_code == 404
    )
    stats = api.get("/api/stats").json()
    assert stats["storage"] == "sqlite"
    assert stats["ttl_s"] == 1
    assert run_id not in json.dumps(stats)
