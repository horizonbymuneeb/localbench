"""Server-credential presets + per-IP abuse guard (CONTRACT.md).

Presets: ``data/presets.json`` entries resolve ``base_url``/``model``/``api_key``
server-side; the key must never reach a response, an export or the sqlite file.

Rate limit: ``POST /api/runs`` is capped per client IP (default 6 runs / 600 s,
env ``LOCALBENCH_RATE_LIMIT`` re-read per request, ``0/600`` disables). An
in-process TestClient has no IP, so the rest of the suite is exempt; these tests
drive it through ``X-Forwarded-For``/``X-Real-IP``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from conftest import (
    TEST_DB,
    get_run,
    json_text,
    remember_token,
    run_payload,
    sqlite_dump_text,
    wait_for,
    wait_terminal,
)

PRESET_SECRET = "sk-test-preset-SECRET"

@pytest.fixture()
def fast_mock(mock_factory):
    """Tiny, fast mock: these tests create a lot of runs (rate-limit buckets)."""
    return mock_factory(ttft_ms=2.0, prefill_tps=4000.0, tps=600.0, draft_n=0)


TINY_CONFIG = {
    "contexts": [64],
    "concurrency": [1],
    "concurrency_context": 256,
    "max_tokens": 4,
    "warmup": 0,
    "enable_context_ladder": True,
    "enable_concurrency": False,
}

RATE_IP = "203.0.113.9"


def presets_path() -> Path:
    path = Path(os.environ["LOCALBENCH_DATA_DIR"]) / "presets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_presets(entries) -> Path:
    path = presets_path()
    path.write_text(json.dumps(entries))
    return path


def post_run_raw(api, payload, headers=None):
    return api.post("/api/runs", json=payload, headers=headers or {})


# ---------------------------------------------------------------------------
# presets: run without base_url / api_key / model
# ---------------------------------------------------------------------------


def test_run_from_preset_resolves_endpoint_and_hides_key(api, fast_mock):
    write_presets(
        [
            {
                "id": "shared-mock",
                "name": "Shared mock",
                "base_url": fast_mock.base_url,
                "model": "mock-qwen",
                "note": "server-side key",
                "api_key": PRESET_SECRET,
            }
        ]
    )
    response = post_run_raw(
        api,
        {"preset_id": "shared-mock", "label": "via preset", "config": TINY_CONFIG},
    )
    assert response.status_code == 200, response.text[:400]
    created = response.json()
    run_id = created["run_id"]
    remember_token(run_id, created["access_token"])

    run = wait_terminal(api, run_id)
    assert run["status"] == "done", run.get("error")
    # preset filled in the endpoint + model, and the run records where it came from
    assert run["preset_id"] == "shared-mock"
    assert run["model"] == "mock-qwen"
    assert run["label"] == "via preset"
    assert run["endpoint_host"] == fast_mock.base_url.split("//")[1].rsplit("/", 1)[0]
    assert run["results"]["context_points"][0]["ok"] is True

    # the server-side key must never leave the server
    stored = get_run(api, run_id)
    assert PRESET_SECRET not in json_text(stored)
    assert "api_key" not in json_text(stored)
    from conftest import headers_for

    assert (
        PRESET_SECRET
        not in api.get(
            "/api/runs/%s/export?format=json" % run_id, headers=headers_for(run_id)
        ).text
    )
    assert (
        PRESET_SECRET
        not in api.get(
            "/api/runs/%s/export?format=csv" % run_id, headers=headers_for(run_id)
        ).text
    )
    assert PRESET_SECRET not in json_text(api.get("/api/stats").json())
    assert PRESET_SECRET not in json_text(api.get("/api/presets").json())
    assert PRESET_SECRET not in sqlite_dump_text(TEST_DB)
    assert stored["agent"]["key"] is None


def test_preset_run_does_not_need_model_or_key(api, fast_mock):
    """A visitor may POST {"preset_id": ...} with nothing else."""
    write_presets(
        [
            {
                "id": "public-shared",
                "name": "Public shared",
                "base_url": fast_mock.base_url,
                "model": "mock-qwen",
                "api_key": PRESET_SECRET,
            }
        ]
    )
    response = post_run_raw(
        api, {"preset_id": "public-shared", "config": TINY_CONFIG}
    )
    assert response.status_code == 200, response.text[:400]
    created = response.json()
    remember_token(created["run_id"], created["access_token"])
    run = wait_terminal(api, created["run_id"])
    assert run["status"] == "done", run.get("error")
    assert run["label"] == "mock-qwen"  # label falls back to the resolved model
    assert run["preset_id"] == "public-shared"


def test_unknown_preset_id_returns_404(api):
    write_presets([])
    response = post_run_raw(api, {"preset_id": "no-such-preset"})
    assert response.status_code == 404, response.text[:300]
    body = response.json()
    assert body["ok"] is False
    assert "preset" in body["error"].lower()


def test_presets_endpoint_tolerates_missing_or_broken_file(api):
    path = presets_path()
    if path.exists():
        path.unlink()
    data = api.get("/api/presets").json()
    assert data == {"ok": True, "presets": []}
    path.write_text("{not valid json")
    assert api.get("/api/presets").json() == {"ok": True, "presets": []}
    path.unlink()


# ---------------------------------------------------------------------------
# rate limit: 6 runs / 600 s per client IP, 429 {ok:false,error:"rate_limited"}
# ---------------------------------------------------------------------------


def test_rate_limit_429_after_six_runs_from_one_ip(api, fast_mock, monkeypatch):
    monkeypatch.delenv("LOCALBENCH_RATE_LIMIT", raising=False)
    payload = run_payload(fast_mock.base_url, label="rate limited", config=TINY_CONFIG)
    headers = {"X-Forwarded-For": RATE_IP}

    statuses = []
    for index in range(7):
        response = post_run_raw(api, payload, headers)
        statuses.append(response.status_code)
        if response.status_code == 429:
            body = response.json()
            assert body["ok"] is False
            assert body["error"] == "rate_limited"
            assert isinstance(body["retry_after"], int)
            assert 1 <= body["retry_after"] <= 600
            assert response.headers.get("retry-after") == str(body["retry_after"])
            assert "run_id" not in body

    assert statuses == [200] * 6 + [429], statuses
    # rejections are counted (cumulative) and exposed without leaking anything
    stats = api.get("/api/stats").json()
    assert stats.get("rate_limited_total", 0) >= 1
    config = api.get("/api/config").json()["limits"]["rate_limit"]
    if "limited_total" in config:  # additive field, may lag one deploy
        assert config["limited_total"] >= 1


def test_rate_limit_spares_loopback_and_operator_access(api, fast_mock, monkeypatch):
    """The local box / operator access is never rate limited (CONTRACT.md)."""
    monkeypatch.delenv("LOCALBENCH_RATE_LIMIT", raising=False)
    payload = run_payload(fast_mock.base_url, label="loopback exempt", config=TINY_CONFIG)
    # 127.0.0.0/8 via X-Forwarded-For
    for _ in range(8):
        assert post_run_raw(api, payload, {"X-Forwarded-For": "127.0.0.1"}).status_code == 200
    # ::1 via X-Real-IP
    for _ in range(3):
        assert post_run_raw(api, payload, {"X-Real-IP": "::1"}).status_code == 200
    # IPv4-mapped loopback
    assert (
        post_run_raw(api, payload, {"X-Forwarded-For": "::ffff:127.0.0.1"}).status_code == 200
    )
    # ...while a public IP is still capped at 6
    public = {"X-Forwarded-For": "198.51.100.200"}
    statuses = [post_run_raw(api, payload, public).status_code for _ in range(7)]
    assert statuses == [200] * 6 + [429], statuses


def test_rate_limit_disabled_by_env_and_re_read_per_request(api, fast_mock, monkeypatch):
    monkeypatch.setenv("LOCALBENCH_RATE_LIMIT", "0/600")
    headers = {"X-Forwarded-For": "198.51.100.7"}
    payload = run_payload(fast_mock.base_url, label="rl disabled", config=TINY_CONFIG)
    # a request triggers the env re-read, then /api/config reports the live value
    assert post_run_raw(api, payload, headers).status_code == 200
    disabled = api.get("/api/config").json()["limits"]["rate_limit"]
    assert {"runs": 0, "window_s": 600}.items() <= disabled.items(), disabled
    for _ in range(3):
        assert post_run_raw(api, payload, headers).status_code == 200

    # restoring the default re-arms the limiter (fresh buckets after spec change)
    monkeypatch.delenv("LOCALBENCH_RATE_LIMIT")
    statuses = [post_run_raw(api, payload, headers).status_code for _ in range(7)]
    assert statuses == [200] * 6 + [429], statuses


def test_rate_limit_buckets_are_per_ip(api, fast_mock, monkeypatch):
    monkeypatch.delenv("LOCALBENCH_RATE_LIMIT", raising=False)
    payload = run_payload(fast_mock.base_url, label="rl buckets", config=TINY_CONFIG)

    for _ in range(6):
        assert post_run_raw(api, payload, {"X-Forwarded-For": "198.51.100.20"}).status_code == 200
    assert post_run_raw(api, payload, {"X-Forwarded-For": "198.51.100.20"}).status_code == 429
    # a different forwarded IP gets its own bucket
    assert post_run_raw(api, payload, {"X-Forwarded-For": "198.51.100.21"}).status_code == 200
    # X-Real-IP is honoured when X-Forwarded-For is absent
    assert post_run_raw(api, payload, {"X-Real-IP": "198.51.100.30"}).status_code == 200
    # X-Forwarded-For wins over X-Real-IP (first hop is what nginx sets)
    assert (
        post_run_raw(
            api,
            payload,
            {"X-Forwarded-For": "198.51.100.40", "X-Real-IP": "198.51.100.20"},
        ).status_code
        == 200
    )
    # TestClient has no IP ("testclient") -> exempt, so the suite keeps working
    for _ in range(2):
        assert post_run_raw(api, payload).status_code == 200


def test_rate_limited_requests_do_not_create_runs(api, fast_mock, monkeypatch):
    """A 429 must not create a run: count the accepted ones, not the requests."""
    monkeypatch.delenv("LOCALBENCH_RATE_LIMIT", raising=False)
    payload = run_payload(fast_mock.base_url, label="rl no run", config=TINY_CONFIG)
    headers = {"X-Forwarded-For": "198.51.100.50"}

    # clean counters: let everything from earlier tests finish and age out of active
    wait_for(
        lambda: api.get("/api/stats").json()["active"] == 0,
        timeout=60,
        what="the run queue to drain",
    )
    before = api.get("/api/stats").json()["finished"]

    responses = [post_run_raw(api, payload, headers) for _ in range(8)]
    statuses = [response.status_code for response in responses]
    assert statuses.count(429) == 2, statuses
    accepted = [
        response.json()["run_id"] for response in responses if response.status_code == 200
    ]
    assert len(accepted) == 6
    # a 429 body carries no run id
    for response in responses:
        if response.status_code == 429:
            assert "run_id" not in response.json()

    wait_for(
        lambda: api.get("/api/stats").json()["active"] == 0,
        timeout=60,
        what="the accepted runs to finish",
    )
    after = api.get("/api/stats").json()["finished"]
    # exactly the six accepted runs were created; the two 429s created nothing
    assert after - before == 6, (before, after)


def test_preset_and_forwarded_ip_interaction(api, fast_mock, monkeypatch):
    """A preset run from a forwarded IP counts against that IP's bucket."""
    monkeypatch.delenv("LOCALBENCH_RATE_LIMIT", raising=False)
    write_presets(
        [
            {
                "id": "rate-limit-preset",
                "name": "Rate limit preset",
                "base_url": fast_mock.base_url,
                "model": "mock-qwen",
                "api_key": PRESET_SECRET,
            }
        ]
    )
    headers = {"X-Forwarded-For": "198.51.100.60"}
    statuses = []
    for _ in range(7):
        statuses.append(
            post_run_raw(
                api, {"preset_id": "rate-limit-preset", "config": TINY_CONFIG}, headers
            ).status_code
        )
    assert statuses == [200] * 6 + [429], statuses
