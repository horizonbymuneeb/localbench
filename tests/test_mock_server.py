"""Unit tests for mock/mock_openai.py itself.

These do not touch the backend at all: if the backend is still being written,
this module must stay green.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time

import httpx
import pytest

from conftest import MOCK_DIR, ROOT

SECRET_PROMPT = "Reply with the single word: ready."


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------


def test_models_exposes_context_length(mock_factory):
    srv = mock_factory(model="mock-qwen", ctx=32768)
    data = httpx.get(srv.base_url + "/models", timeout=5).json()
    assert isinstance(data.get("data"), list) and data["data"], data
    entry = data["data"][0]
    assert entry["id"] == "mock-qwen"
    assert entry["context_length"] == 32768
    assert entry["meta"]["n_ctx"] == 32768


def test_models_can_hide_context_length(mock_factory):
    srv = mock_factory(hide_context_length=True)
    entry = httpx.get(srv.base_url + "/models", timeout=5).json()["data"][0]
    assert "context_length" not in entry


# ---------------------------------------------------------------------------
# Prompt-token heuristic: usage.prompt_tokens == round(words / 0.75)
# ---------------------------------------------------------------------------


def test_prompt_token_heuristic_is_words_over_075(mock_factory):
    srv = mock_factory()
    words = ["alpha"] * 40  # 40 words -> round(40 / 0.75) = 53
    body = {
        "model": "mock-qwen",
        "messages": [{"role": "user", "content": " ".join(words)}],
        "max_tokens": 2,
    }
    data = httpx.post(srv.base_url + "/chat/completions", json=body, timeout=10).json()
    assert data["usage"]["prompt_tokens"] == round(40 / 0.75)
    assert data["usage"]["completion_tokens"] == 2
    assert data["usage"]["total_tokens"] == data["usage"]["prompt_tokens"] + 2


# ---------------------------------------------------------------------------
# Context overflow -> OpenAI-style 400
# ---------------------------------------------------------------------------


def test_context_overflow_returns_openai_error(mock_factory):
    srv = mock_factory(ctx=256)
    huge = "word " * 800  # 800 words -> 1067 tokens > 256
    body = {"model": "mock-qwen", "messages": [{"role": "user", "content": huge}], "max_tokens": 4}
    response = httpx.post(srv.base_url + "/chat/completions", json=body, timeout=10)
    assert response.status_code == 400, response.text[:300]
    payload = response.json()
    assert "error" in payload and payload["error"].get("message"), payload
    message = payload["error"]["message"].lower()
    assert "context" in message, payload
    assert payload["error"].get("code") == "context_length_exceeded"
    assert payload["error"].get("type") == "invalid_request_error"


# ---------------------------------------------------------------------------
# Streaming SSE shape
# ---------------------------------------------------------------------------


def stream_events(srv, prompt: str, max_tokens: int = 8) -> list:
    """Read the SSE stream and return parsed JSON chunks."""
    body = {
        "model": "mock-qwen",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
    }
    chunks = []
    with httpx.stream("POST", srv.base_url + "/chat/completions", json=body, timeout=15) as response:
        assert response.status_code == 200, response.status_code
        assert "text/event-stream" in response.headers.get("content-type", "")
        saw_done = False
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                saw_done = True
                break
            chunks.append(json.loads(data))
    assert saw_done, "stream must end with data: [DONE]"
    return chunks


def test_streaming_chunks_usage_and_timings(mock_factory):
    srv = mock_factory(draft_n=24, draft_accepted=15)
    chunks = stream_events(srv, SECRET_PROMPT, max_tokens=8)

    # role delta first, with no content (so it cannot be mistaken for a token)
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    assert not chunks[0]["choices"][0]["delta"].get("content")

    content = [c["choices"][0]["delta"].get("content") or "" for c in chunks]
    text = "".join(content)
    assert text.startswith("ready"), text  # deterministic reply for LocalBench prompts
    assert sum(1 for piece in content if piece) == 8

    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"]["completion_tokens"] == 8
    assert final["usage"]["prompt_tokens"] >= 1
    timings = final["timings"]
    for key in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms", "draft_n", "draft_n_accepted"):
        assert key in timings, (key, timings)
    assert timings["prompt_n"] == final["usage"]["prompt_tokens"]
    assert timings["predicted_n"] == 8
    assert timings["draft_n"] == 24 and timings["draft_n_accepted"] == 15
    assert timings["prompt_ms"] > 0 and timings["predicted_ms"] > 0


def test_non_streaming_body_matches_usage_and_timings(mock_factory):
    srv = mock_factory(draft_n=0, draft_accepted=0)
    body = {
        "model": "mock-qwen",
        "messages": [{"role": "user", "content": SECRET_PROMPT}],
        "max_tokens": 4,
        "stream": False,
    }
    data = httpx.post(srv.base_url + "/chat/completions", json=body, timeout=10).json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["message"]["content"].startswith("ready")
    usage = data["usage"]
    timings = data["timings"]
    assert usage["completion_tokens"] == 4
    assert timings["predicted_n"] == usage["completion_tokens"] == 4
    assert timings["prompt_n"] == usage["prompt_tokens"]
    # no speculative decoding requested -> engine should map this to draft: null
    assert timings["draft_n"] == 0 and timings["draft_n_accepted"] == 0


# ---------------------------------------------------------------------------
# Determinism / pacing (measured over a raw socket, no client buffering noise)
# ---------------------------------------------------------------------------


def stream_content_times(srv, prompt: str, max_tokens: int) -> list:
    """Raw-socket SSE read: wall-clock arrival of each content delta (seconds)."""
    body = json.dumps(
        {"model": "mock-qwen", "messages": [{"role": "user", "content": prompt}],
         "max_tokens": max_tokens, "stream": True}
    ).encode()
    sock = socket.create_connection((srv.host, srv.port), timeout=10)
    try:
        request = (
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: h\r\nContent-Type: application/json\r\n"
            b"Content-Length: %d\r\n\r\n" % len(body)
        ) + body
        t0 = time.perf_counter()
        sock.sendall(request)
        sock.settimeout(10)
        buffer = b""
        content_times = []
        while len(content_times) < max_tokens:
            data = sock.recv(65536)
            assert data, "connection closed early"
            buffer += data
            while b"\n\n" in buffer:
                chunk, buffer = buffer.split(b"\n\n", 1)
                if b'"content"' in chunk:
                    content_times.append(time.perf_counter() - t0)
        return content_times
    finally:
        sock.close()



def test_pacing_ttft_and_tps_are_accurate(mock_factory):
    srv = mock_factory(ttft_ms=40.0, tps=100.0)
    content_times = stream_content_times(srv, SECRET_PROMPT, 6)

    ttft_ms = content_times[0] * 1000
    assert 20.0 <= ttft_ms <= 400.0, "ttft %.1fms not near configured 40ms" % ttft_ms
    intervals = [(b - a) * 1000 for a, b in zip(content_times, content_times[1:])]
    mean_interval = sum(intervals) / len(intervals)
    # 100 tps -> 10 ms between content deltas (allow scheduler slack)
    assert 3.0 <= mean_interval <= 40.0, "mean interval %.1fms not near 10ms" % mean_interval


def test_prefill_tps_adds_prompt_processing_latency(mock_factory):
    """--prefill-tps makes TTFT grow with the prompt, so prefill math is measurable."""
    prompt = " ".join(["alpha"] * 40)  # 40 words -> 53 tokens
    srv = mock_factory(ttft_ms=10.0, tps=800.0, prefill_tps=1000.0)
    content_times = stream_content_times(srv, prompt, 4)
    expected_ms = 10.0 + round(40 / 0.75) / 1000.0 * 1000.0  # 10 + 53 ms
    ttft_ms = content_times[0] * 1000
    assert 0.6 * expected_ms <= ttft_ms <= 3.0 * expected_ms, (
        "ttft %.1fms, expected ~%.1fms (base 10ms + 53 tokens at 1000 tps)" % (ttft_ms, expected_ms)
    )

    # And the same latency is reported in llama.cpp timings.
    body = {"model": "mock-qwen", "messages": [{"role": "user", "content": prompt}], "max_tokens": 2}
    data = httpx.post(srv.base_url + "/chat/completions", json=body, timeout=10).json()
    assert abs(data["timings"]["prompt_ms"] - expected_ms) < 5.0, data["timings"]


# ---------------------------------------------------------------------------
# Importable + standalone
# ---------------------------------------------------------------------------


def test_importable_start_and_stop(mock_factory):
    srv = mock_factory()
    r = httpx.get(srv.root_url + "/healthz", timeout=5)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert srv.port > 0
    srv.stop()
    with pytest.raises(Exception):
        httpx.get(srv.root_url + "/healthz", timeout=0.5)


def test_standalone_cli_serves(tmp_path):
    """The CLI must print its URL and serve /v1/models."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    proc = subprocess.Popen(
        [
            sys.executable,
            str(MOCK_DIR / "mock_openai.py"),
            "--port",
            str(port),
            "--ttft-ms",
            "5",
            "--tps",
            "500",
            "--ctx",
            "4096",
            "--model",
            "mock-cli",
            "--draft-n",
            "8",
            "--draft-accepted",
            "6",
        ],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        from conftest import wait_http_ok

        payload = wait_http_ok("http://127.0.0.1:%d/v1/models" % port, timeout=15)
        assert payload["data"][0]["id"] == "mock-cli"
        assert payload["data"][0]["context_length"] == 4096
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
