#!/usr/bin/env python3
"""Deterministic OpenAI-compatible mock server for LocalBench tests.

Stdlib only (http.server + ThreadingHTTPServer). No network, no GPU.

Routes
------
GET  /v1/models                    -> OpenAI model list, each entry carries
                                      ``context_length`` (and llama.cpp-style
                                      ``meta.n_ctx``/``n_ctx_train``).
POST /v1/chat/completions          -> streaming SSE (default) or JSON.
GET  /healthz                      -> {"ok": true, ...} readiness probe.
GET  /                             -> tiny JSON describing the mock.

Determinism
-----------
* Token accounting is a pure function of the request text:
  ``usage.prompt_tokens = round(words / 0.75)`` (min 1) -- see ``count_tokens``.
  The same number is used for the context-overflow guard and in ``usage``.
* Streaming content is a fixed token cycle, so the same request always yields
  the same text (``ready`` first when the prompt ends with LocalBench's
  ``Reply with the single word: ready.`` marker).
* Timing is *paced*: the first content delta arrives ``--ttft-ms`` after the
  request is read, then one content delta every ``1/--tps`` seconds.  A client
  that measures decode speed between the first and the last content delta
  therefore measures approximately ``--tps``.
* Every completion carries llama.cpp-style ``timings``:
  ``prompt_n, prompt_ms, predicted_n, predicted_ms, draft_n, draft_n_accepted``
  with ``draft_n=0`` meaning "no speculative decoding" (engine maps it to
  ``draft: null``).

Context overflow
----------------
When the counted prompt exceeds ``--ctx`` the server answers HTTP 400 with an
OpenAI-style error body whose message says "exceeds the available context
size" and ``code: context_length_exceeded`` (llama.cpp wording), so engines
can map it to ``error:"context_exceeded"``.

Importable use (pytest)::

    from mock_openai import start_mock_server
    srv = start_mock_server(ttft_ms=10, tps=400, ctx=8192)
    srv.base_url          # 'http://127.0.0.1:54321/v1'
    srv.port
    srv.stop()

Standalone use::

    python3 mock/mock_openai.py --port 8099 --ttft-ms 120 --tps 45 \\
        --ctx 32768 --draft-n 64 --draft-accepted 40 --model qwen3-27b
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Deterministic token heuristic
# ---------------------------------------------------------------------------

TOKENS_PER_WORD = 0.75  # LocalBench contract: prompt_tokens ~= words / 0.75

#: Fixed pseudo-completion vocabulary; index modulo len() keeps it deterministic.
_COMPLETION_CYCLE: Tuple[str, ...] = (
    " the",
    " of",
    " and",
    " to",
    " a",
    " in",
    " that",
    " is",
    " for",
    " with",
    " on",
    " as",
)

#: Marker appended by LocalBench synthetic prompts (see CONTRACT.md).
READY_MARKER = "Reply with the single word: ready."


def count_words(text: str) -> int:
    """Number of whitespace-separated words in *text*."""
    return len(text.split())


def count_tokens(text: str) -> int:
    """Deterministic prompt-token count: ``round(words / 0.75)``, at least 1."""
    return max(1, int(round(count_words(text) / TOKENS_PER_WORD)))


def message_text(content: Any) -> str:
    """Flatten an OpenAI ``messages[].content`` value into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part.get("content"), str):
                    parts.append(part["content"])
        return " ".join(parts)
    return str(content)


def prompt_of(messages: Any) -> str:
    """Join chat messages into one string (the thing the heuristic counts)."""
    if not isinstance(messages, list):
        return ""
    return "\n".join(message_text(m.get("content")) for m in messages if isinstance(m, dict))


def completion_pieces(prompt: str, n_tokens: int) -> List[str]:
    """Deterministic list of *n_tokens* content deltas for *prompt*."""
    if n_tokens <= 0:
        return []
    pieces: List[str] = []
    first = "ready" if READY_MARKER in prompt else "ok"
    pieces.append(first)
    i = 0
    while len(pieces) < n_tokens:
        pieces.append(_COMPLETION_CYCLE[i % len(_COMPLETION_CYCLE)])
        i += 1
    return pieces


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class MockConfig:
    """Runtime knobs for the mock; all values are plain attrs so tests can tweak."""

    def __init__(
        self,
        model: str = "mock-local",
        ctx: int = 131072,
        ttft_ms: float = 50.0,
        tps: float = 80.0,
        prefill_tps: float = 0.0,
        draft_n: int = 0,
        draft_accepted: int = 0,
        default_max_tokens: int = 32,
        max_completion_tokens: int = 1024,
        quiet: bool = True,
        extra_models: Optional[List[str]] = None,
        hide_context_length: bool = False,
    ) -> None:
        self.model = model
        self.ctx = int(ctx)
        self.ttft_ms = float(ttft_ms)
        self.tps = max(1.0, float(tps))
        self.prefill_tps = max(0.0, float(prefill_tps))
        self.draft_n = max(0, int(draft_n))
        self.draft_accepted = max(0, int(draft_accepted))
        if self.draft_accepted > self.draft_n:
            self.draft_accepted = self.draft_n
        self.default_max_tokens = max(1, int(default_max_tokens))
        self.max_completion_tokens = max(1, int(max_completion_tokens))
        self.quiet = bool(quiet)
        self.extra_models = list(extra_models or [])
        self.hide_context_length = bool(hide_context_length)
        self._lock = threading.Lock()
        self._seq = 0

    def next_completion_id(self) -> str:
        with self._lock:
            self._seq += 1
            return "chatcmpl-mock-%04d" % self._seq

    def ttft_seconds(self, prompt_tokens: int) -> float:
        """Delay before the first content token: base TTFT + prompt prefill.

        ``--prefill-tps 0`` (default) gives the constant ``--ttft-ms`` delay.
        A positive rate models a real GPU: prompt processing takes
        ``prompt_tokens / prefill_tps`` seconds, which is what makes LocalBench's
        ``prefill_tps = prompt_tokens / prompt_ms`` field measurable.
        """
        delay = self.ttft_ms / 1000.0
        if self.prefill_tps > 0:
            delay += prompt_tokens / self.prefill_tps
        return delay

    def models(self) -> List[Dict[str, Any]]:
        ids = [self.model] + [m for m in self.extra_models if m != self.model]
        out = []
        for mid in ids:
            entry: Dict[str, Any] = {
                "id": mid,
                "object": "model",
                "created": 1699999999,
                "owned_by": "localbench-mock",
            }
            if not self.hide_context_length:
                entry["context_length"] = self.ctx
                entry["meta"] = {"n_ctx": self.ctx, "n_ctx_train": self.ctx}
            out.append(entry)
        return out

    def timings(self, prompt_tokens: int, completion_tokens: int) -> Dict[str, Any]:
        """llama.cpp-shaped timings for one completion."""
        prompt_ms = self.ttft_seconds(prompt_tokens) * 1000.0
        predicted_ms = 0.0 if completion_tokens <= 1 else 1000.0 * (completion_tokens - 1) / self.tps
        return {
            "prompt_n": int(prompt_tokens),
            "prompt_ms": round(prompt_ms, 3),
            "prompt_per_token_ms": round(prompt_ms / prompt_tokens, 6) if prompt_tokens else 0.0,
            "prompt_per_second": round(1000.0 * prompt_tokens / prompt_ms, 3) if prompt_ms else 0.0,
            "predicted_n": int(completion_tokens),
            "predicted_ms": round(predicted_ms, 3),
            "predicted_per_token_ms": round(predicted_ms / completion_tokens, 6) if completion_tokens else 0.0,
            "predicted_per_second": round(1000.0 * completion_tokens / predicted_ms, 3) if predicted_ms else 0.0,
            "draft_n": int(self.draft_n),
            "draft_n_accepted": int(self.draft_accepted),
        }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


def _sleep_until(deadline: float) -> None:
    """Sleep until ``time.perf_counter() >= deadline`` (coarse, re-checked)."""
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.25))


class _ClientGone(Exception):
    """Raised when the client hung up mid-stream (e.g. a cancelled run)."""


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalBenchMock/1.0"

    # -- plumbing ---------------------------------------------------------

    @property
    def config(self) -> MockConfig:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib name
        if not self.config.quiet:
            sys.stderr.write("[mock] %s - %s\n" % (self.address_string(), fmt % args))

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _send_error_body(self, status: int, message: str, code: str = "invalid_request_error") -> None:
        self._send_json(
            status,
            {"error": {"message": message, "type": "invalid_request_error", "param": None, "code": code}},
        )

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/v1/models", "/models"):
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": self.config.models(),
                    "models": self.config.models(),  # some clients read "models"
                },
            )
            return
        if path in ("/healthz", "/health"):
            self._send_json(200, {"ok": True, "model": self.config.model, "ctx": self.config.ctx})
            return
        if path == "/":
            self._send_json(
                200,
                {
                    "ok": True,
                    "mock": "localbench",
                    "model": self.config.model,
                    "ctx": self.config.ctx,
                    "ttft_ms": self.config.ttft_ms,
                    "tps": self.config.tps,
                },
            )
            return
        self._send_error_body(404, "unknown path: %s" % self.path, "not_found")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._read_json()
            self._send_error_body(404, "unknown path: %s" % self.path, "not_found")
            return

        payload = self._read_json()
        if not isinstance(payload, dict):
            self._send_error_body(400, "request body must be a JSON object", "invalid_json")
            return

        messages = payload.get("messages")
        prompt = prompt_of(messages)
        prompt_tokens = count_tokens(prompt)

        # --- context guard -------------------------------------------------
        if prompt_tokens > self.config.ctx:
            self._send_error_body(
                400,
                "the request exceeds the available context size (%d tokens, n_ctx=%d), "
                "try increasing it" % (prompt_tokens, self.config.ctx),
                "context_length_exceeded",
            )
            return

        # --- completion size ----------------------------------------------
        req_max = payload.get("max_tokens")
        if req_max is None:
            req_max = payload.get("max_completion_tokens")
        try:
            n_tokens = int(req_max) if req_max is not None else self.config.default_max_tokens
        except (TypeError, ValueError):
            n_tokens = self.config.default_max_tokens
        n_tokens = max(1, min(n_tokens, self.config.max_completion_tokens))

        pieces = completion_pieces(prompt, n_tokens)
        completion_tokens = len(pieces)
        timings = self.config.timings(prompt_tokens, completion_tokens)

        try:
            if payload.get("stream"):
                self._stream(payload, pieces, prompt_tokens, completion_tokens, timings)
            else:
                self._complete(payload, pieces, prompt_tokens, completion_tokens, timings)
        except (_ClientGone, BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return  # client went away; nothing to report

    # -- responses --------------------------------------------------------

    def _base_chunk(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": self.config.next_completion_id(),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": payload.get("model") or self.config.model,
        }

    def _stream(
        self,
        payload: Dict[str, Any],
        pieces: List[str],
        prompt_tokens: int,
        completion_tokens: int,
        timings: Dict[str, Any],
    ) -> None:
        t_start = time.perf_counter()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def emit(obj: Dict[str, Any]) -> None:
            try:
                self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                raise _ClientGone()

        # Role-only delta goes out immediately (carries no content, so it is not
        # counted as the first token by LocalBench).
        role_chunk = self._base_chunk(payload)
        role_chunk["choices"] = [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
        emit(role_chunk)

        # First content delta after base TTFT + modelled prefill.
        ttft_s = self.config.ttft_seconds(prompt_tokens)
        token_period = 1.0 / self.config.tps
        for i, piece in enumerate(pieces):
            deadline = t_start + ttft_s + i * token_period
            _sleep_until(deadline)
            chunk = self._base_chunk(payload)
            chunk["choices"] = [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]
            emit(chunk)

        final = self._base_chunk(payload)
        final["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        final["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        final["timings"] = timings
        emit(final)
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            raise _ClientGone()

    def _complete(
        self,
        payload: Dict[str, Any],
        pieces: List[str],
        prompt_tokens: int,
        completion_tokens: int,
        timings: Dict[str, Any],
    ) -> None:
        total_s = self.config.ttft_seconds(prompt_tokens) + completion_tokens / self.config.tps
        _sleep_until(time.perf_counter() + total_s)
        self._send_json(
            200,
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": payload.get("model") or self.config.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "".join(pieces)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
                "timings": timings,
            },
        )


# ---------------------------------------------------------------------------
# Server factory / lifecycle
# ---------------------------------------------------------------------------


class MockOpenAIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(
            exc, (_ClientGone, BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
        ):
            return
        super().handle_error(request, client_address)

    def __init__(self, address: Tuple[str, int], config: MockConfig) -> None:
        self.config = config
        super().__init__(address, MockHandler)


class MockServerHandle:
    """Handle returned by :func:`start_mock_server` / :func:`serve_in_thread`."""

    def __init__(self, server: MockOpenAIServer, thread: threading.Thread) -> None:
        self.server = server
        self.thread = thread
        self.host, self.port = server.server_address[0], server.server_address[1]

    @property
    def base_url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "", "::") else self.host
        return "http://%s:%d/v1" % (host, self.port)

    @property
    def root_url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "", "::") else self.host
        return "http://%s:%d" % (host, self.port)

    @property
    def config(self) -> MockConfig:
        return self.server.config

    def stop(self, timeout: float = 5.0) -> None:
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass
        self.thread.join(timeout=timeout)

    def __enter__(self) -> "MockServerHandle":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()


def make_server(
    host: str = "127.0.0.1",
    port: int = 0,
    config: Optional[MockConfig] = None,
    **config_kwargs: Any,
) -> MockOpenAIServer:
    """Create (but do not start) the mock server."""
    if config is None:
        config = MockConfig(**config_kwargs)
    return MockOpenAIServer((host, int(port)), config)


def serve_in_thread(
    host: str = "127.0.0.1",
    port: int = 0,
    config: Optional[MockConfig] = None,
    **config_kwargs: Any,
) -> MockServerHandle:
    """Start the mock on a background thread and return its handle."""
    server = make_server(host=host, port=port, config=config, **config_kwargs)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    return MockServerHandle(server, thread)


# Alias kept readable for tests.
start_mock_server = serve_in_thread


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Deterministic OpenAI-compatible mock server (stdlib only).")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8099, help="0 picks a free port")
    p.add_argument("--model", default="mock-local", help="model id served (default: mock-local)")
    p.add_argument("--ctx", type=int, default=131072, help="model context length; prompts above it get HTTP 400")
    p.add_argument("--ttft-ms", type=float, default=50.0, help="artificial delay before the first content token")
    p.add_argument("--tps", type=float, default=80.0, help="token pacing for content deltas (tokens/second)")
    p.add_argument(
        "--prefill-tps",
        type=float,
        default=0.0,
        help="modeled prompt-processing rate; adds prompt_tokens/prefill_tps to TTFT (0 = constant TTFT)",
    )
    p.add_argument("--draft-n", type=int, default=0, help="llama.cpp timings.draft_n (speculative draft tokens)")
    p.add_argument("--draft-accepted", type=int, default=0, help="llama.cpp timings.draft_n_accepted")
    p.add_argument("--default-max-tokens", type=int, default=32, help="completion length when max_tokens is absent")
    p.add_argument("--max-completion-tokens", type=int, default=1024, help="hard cap on generated tokens")
    p.add_argument(
        "--no-context-length",
        action="store_true",
        help="omit context_length from /v1/models (tests the 400-based context-overflow path)",
    )
    p.add_argument("--verbose", action="store_true", help="log every request to stderr")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = MockConfig(
        model=args.model,
        ctx=args.ctx,
        ttft_ms=args.ttft_ms,
        tps=args.tps,
        prefill_tps=args.prefill_tps,
        draft_n=args.draft_n,
        draft_accepted=args.draft_accepted,
        default_max_tokens=args.default_max_tokens,
        max_completion_tokens=args.max_completion_tokens,
        quiet=not args.verbose,
        hide_context_length=args.no_context_length,
    )
    server = make_server(host=args.host, port=args.port, config=config)
    host, port = server.server_address[0], server.server_address[1]
    shown = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    print(
        "mock_openai listening on http://%s:%d/v1 (model=%s ctx=%d ttft=%.1fms tps=%.1f draft=%d/%d)"
        % (shown, port, config.model, config.ctx, config.ttft_ms, config.tps, config.draft_n, config.draft_accepted),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
