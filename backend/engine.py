"""LocalBench measurement engine — the single source of truth for the
"Measurement definitions" section of CONTRACT.md.

Everything here is transport-level measurement plus pure math; no FastAPI, no
database, no SSE.  ``app.py`` owns scheduling, persistence and streaming.

Definitions implemented (CONTRACT.md lines 146-173):

* timing with ``time.perf_counter()``;
* ``ttft_ms`` = body sent -> first streamed chunk carrying a non-empty
  ``choices[0].delta.content`` (or an earlier reasoning/tool delta);
* ``overhead.ping_ms_p50`` = p50 of 5 sequential ``max_tokens=1`` pings;
* ``ttft_corrected_ms = max(0, ttft_ms - ping_ms_p50)``; ``prompt_ms`` is that;
* ``prefill_tps = usage.prompt_tokens / (prompt_ms / 1000)`` (fallback
  ``len(prompt)/4`` tokens);
* ``decode_tps = (completion_tokens - 1) / ((t_last - t_first) / 1000)``;
* deterministic synthetic prompt of ~``ctx*4`` chars ending with
  ``"Reply with the single word: ready."``;
* context rung skip rules (``context_exceeded`` / ``exceeds_model_context``);
* concurrency wall time, aggregate tok/s, per-request decode tok/s,
  ttft p50/p95;
* llama.cpp ``timings`` / ``timings_per_token`` -> ``draft`` stats;
* aggregates mean/p50/p95/min/max/n;
* every per-point failure becomes ``ok=false`` + ``error`` instead of killing
  the run.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import random
import time
import uuid
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

import httpx

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
MAX_CTX = 131072
MIN_CTX = 16

DEFAULT_CONFIG: dict[str, Any] = {
    "contexts": [1024, 4096, 8192, 16384, 32768, 65536],
    "concurrency": [1, 2, 4, 8],
    "concurrency_context": 2048,
    "max_tokens": 128,
    "repeats": 1,
    "warmup": 1,
    "idle_sample_s": 5,
    "enable_context_ladder": True,
    "enable_concurrency": True,
    "request_timeout_s": 600,
}

EMPTY_AGG: dict[str, Any] = {"mean": 0, "p50": 0, "p95": 0, "min": 0, "max": 0, "n": 0}

#: Every measured prompt ends with an instruction that forces a long answer:
#: a 2-token reply leaves nothing to measure (CONTRACT.md "Decode measurements
#: need room").
PROMPT_TAIL = (
    "\n\nWrite a long, continuous, plain-English paragraph of at least 250 words "
    "about computer hardware. Do not stop early. Do not use lists, headings or code."
)
PING_PROMPT = "ok"

#: Token-dense filler word cycle from CONTRACT.md ("the of and to in a is that
#: for it be").  Dense common words keep the prompt close to one token per word
#: instead of ~1 token per 4 characters of prose.
DENSE_WORDS = ("the", "of", "and", "to", "in", "a", "is", "that", "for", "it", "be")

#: Average characters per whitespace word of the dense cycle (incl. trailing
#: space).  Public so tests can predict the filler sizing without duplicating
#: the cycle.
DENSE_CHARS_PER_WORD = (len(" ".join(DENSE_WORDS)) + 1) / float(len(DENSE_WORDS))

#: Fallback characters-per-token for the dense filler.  The LocalBench mock counts
#: ``round(words / 0.75)`` tokens (DENSE_CHARS_PER_WORD * 0.75 = 2.522727... chars
#: per token for this word cycle); real tokenizers differ.  The engine calibrates
#: this per endpoint from the warmup request's ``usage.prompt_tokens``, so
#: ``prompt_tokens`` lands within ~10% of the rung on any tokenizer.
DEFAULT_CHARS_PER_TOKEN = DENSE_CHARS_PER_WORD * 0.75

#: Completion-token floor below which decode tok/s is not measurable.
MIN_DECODE_TOKENS = 8

#: CONTRACT.md: "Never emit values > 1e6 tok/s."
MAX_PREFILL_TPS = 1_000_000.0

#: Cache-busting nonce size (~30-60 random tokens).  Random letters tokenize at
#: ~2 tokens/word on real BPE, so 25 words stays inside the contract's range
#: while keeping the nonce's share of a 1K rung small.
NONCE_WORDS = 25
_NONCE_ALPHABET = "abcdefghijklmnopqrstuvwxyz"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class EngineError(Exception):
    """A single request failed.  Carries enough detail to classify it."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body or ""

    @property
    def is_context_error(self) -> bool:
        """True when the endpoint rejected the request for being too long."""
        if self.status not in (400, 413, 422, 500):
            return False
        text = (self.body or self.message or "").lower()
        markers = (
            "context",
            "too long",
            "too large",
            "exceed",
            "n_ctx",
            "maximum context",
            "context_length_exceeded",
            "kv cache",
            "prompt is too",
            "input length",
        )
        return any(marker in text for marker in markers)


class RunCancelled(Exception):
    """Raised internally when the caller asked to cancel the run."""


# --------------------------------------------------------------------------- #
# pure helpers (also imported by unit tests)
# --------------------------------------------------------------------------- #
def percentile(sorted_values: list, p: float) -> float:
    """Linear-interpolation percentile (numpy default) over a sorted list."""
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_values[0])
    rank = (n - 1) * (p / 100.0)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(sorted_values[lo])
    frac = rank - lo
    return float(sorted_values[lo]) * (1.0 - frac) + float(sorted_values[hi]) * frac


def aggregate_stats(values: Iterable[Any], ndigits: int = 2) -> dict:
    """mean/p50/p95/min/max/n over numeric values (``n`` counts valid ones)."""
    clean = []
    for value in values or []:
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(num):
            clean.append(num)
    if not clean:
        return dict(EMPTY_AGG)
    ordered = sorted(clean)
    return {
        "mean": round(sum(ordered) / len(ordered), ndigits),
        "p50": round(percentile(ordered, 50), ndigits),
        "p95": round(percentile(ordered, 95), ndigits),
        "min": round(ordered[0], ndigits),
        "max": round(ordered[-1], ndigits),
        "n": len(ordered),
    }


#: Backwards-friendly alias — some callers/tests use the shorter name.
aggregates = aggregate_stats


def prefill_tps(prompt_tokens: Any, prompt_ms: Any) -> float:
    """``prompt_tokens / (prompt_ms/1000)``; 0 when the timing is unusable."""
    try:
        tokens = float(prompt_tokens)
        ms = float(prompt_ms)
    except (TypeError, ValueError):
        return 0.0
    if ms <= 0 or tokens <= 0:
        return 0.0
    return round(tokens / (ms / 1000.0), 2)


def decode_tps(completion_tokens: Any, first_ts: Any, last_ts: Any) -> float:
    """``(completion_tokens - 1) / ((last_ts - first_ts)/1000)`` (ms input)."""
    try:
        tokens = int(completion_tokens)
        first = float(first_ts)
        last = float(last_ts)
    except (TypeError, ValueError):
        return 0.0
    span_ms = last - first
    if tokens <= 1 or span_ms <= 0:
        return 0.0
    return round((tokens - 1) / (span_ms / 1000.0), 2)


def _dense_text(chars: int) -> str:
    """~``chars`` characters of the dense word cycle (deterministic, word-safe)."""
    chars = int(chars)
    if chars <= 0:
        return ""
    cycle = " ".join(DENSE_WORDS) + " "
    text = (cycle * (chars // len(cycle) + 1))[:chars]
    cut = text.rfind(" ")
    return text[:cut] if cut > 0 else text.strip()


def make_nonce(rung: int, words: int = NONCE_WORDS) -> str:
    """Unique cache-busting prefix for one request (CONTRACT.md).

    llama.cpp keeps prompt/prefix caches, so a 64K rung issued after a 32K rung
    would otherwise read most of its prompt from cache and report fiction.
    """
    letters = " ".join(
        "".join(random.choice(_NONCE_ALPHABET) for _ in range(6))
        for _ in range(int(words))
    )
    return "Benchmark run %s rung %d. %s." % (uuid.uuid4().hex, int(rung), letters)


def build_prompt(
    ctx: int,
    nonce: str = "",
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
) -> str:
    """Deterministic token-dense prompt for a ladder rung.

    The filler is sized so ``usage.prompt_tokens`` lands on ``ctx`` for the
    endpoint's measured characters-per-token ratio (calibrated at runtime from
    the warmup request, see :meth:`Engine._calibrate_chars_per_token`).  The
    long tail instruction and the cache-busting nonce are budgeted as words so
    their (longer) words cannot skew the rung.  Always ends with
    :data:`PROMPT_TAIL`.
    """
    ratio = float(chars_per_token or DEFAULT_CHARS_PER_TOKEN)
    target_tokens = max(1, int(ctx))
    # tokens per whitespace word of the dense filler at this tokenizer ratio
    tokens_per_word = DENSE_CHARS_PER_WORD / ratio
    nonce_tokens = len(nonce.split()) * tokens_per_word
    tail_tokens = len(PROMPT_TAIL.split()) * tokens_per_word
    filler_tokens = max(0.0, target_tokens - nonce_tokens - tail_tokens)
    filler_chars = int(round(filler_tokens * ratio))
    prefix = (nonce.strip() + " ") if nonce.strip() else ""
    return prefix + _dense_text(filler_chars) + PROMPT_TAIL


# --------------------------------------------------------------------------- #
# config / URL handling
# --------------------------------------------------------------------------- #
def _as_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, out))


def _as_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, out))


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def normalize_config(raw: Optional[dict]) -> dict:
    """Merge user config over defaults and clamp every field to sane bounds."""
    config = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        for key in DEFAULT_CONFIG:
            if key in raw and raw[key] is not None:
                config[key] = raw[key]

    contexts = []
    for item in config["contexts"] if isinstance(config["contexts"], (list, tuple)) else []:
        try:
            rung = int(item)
        except (TypeError, ValueError):
            continue
        if rung <= 0:
            continue
        rung = min(rung, MAX_CTX)  # never ask for more than the server limit
        if rung < MIN_CTX:
            rung = MIN_CTX
        if rung not in contexts:
            contexts.append(rung)
    config["contexts"] = sorted(contexts) or list(DEFAULT_CONFIG["contexts"])

    concurrency = []
    for item in config["concurrency"] if isinstance(config["concurrency"], (list, tuple)) else []:
        try:
            n = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 64 and n not in concurrency:
            concurrency.append(n)
    config["concurrency"] = sorted(concurrency) or list(DEFAULT_CONFIG["concurrency"])

    config["concurrency_context"] = _as_int(
        config["concurrency_context"], 2048, MIN_CTX, MAX_CTX
    )
    config["max_tokens"] = _as_int(config["max_tokens"], 128, 1, 8192)
    config["repeats"] = _as_int(config["repeats"], 1, 1, 10)
    config["warmup"] = _as_int(config["warmup"], 1, 0, 10)
    config["idle_sample_s"] = _as_float(config["idle_sample_s"], 5.0, 0.0, 120.0)
    config["request_timeout_s"] = _as_float(config["request_timeout_s"], 600.0, 5.0, 3600.0)
    config["enable_context_ladder"] = _as_bool(config["enable_context_ladder"], True)
    config["enable_concurrency"] = _as_bool(config["enable_concurrency"], True)
    return config


_REJECTED_HOSTS = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "internal"}
)
_REJECTED_SUFFIXES = (".internal", ".localdomain", ".localhost")


def validate_base_url(raw: Any) -> str:
    """Normalise + SSRF-check a user supplied base URL.

    Rejects non-http(s) schemes, ``localhost``, ``.internal`` and the
    link-local ``169.254.0.0/16`` range plus ``::1`` (CONTRACT.md line 65).
    Returns the normalised URL (trailing ``/`` stripped, ``/v1`` appended for a
    bare ``host:port``).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("base_url is required")
    url = raw.strip()
    if "://" not in url:
        url = "http://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("base_url must be an http(s) URL")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("base_url has no host")
    if host in _REJECTED_HOSTS or host.endswith(_REJECTED_SUFFIXES):
        raise ValueError("base_url host is not allowed: %s" % host)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.version == 4 and ip.is_link_local:
            raise ValueError("base_url host is not allowed: link-local 169.254.0.0/16")
        if ip.version == 6 and ip.is_loopback:
            raise ValueError("base_url host is not allowed: ::1")
    path = (parsed.path or "").rstrip("/")
    if not path:
        path = "/v1"
    return "%s://%s%s" % (parsed.scheme, parsed.netloc, path)


def normalize_base_url(raw: Any) -> str:
    """Alias of :func:`validate_base_url` kept for test/import convenience."""
    return validate_base_url(raw)


def endpoint_host(base_url: str) -> str:
    """``host:port`` for the Run document (never the full URL)."""
    parsed = urlparse(base_url if "://" in base_url else "http://" + base_url)
    return parsed.netloc or parsed.hostname or ""


# --------------------------------------------------------------------------- #
# llama.cpp timings -> draft stats
# --------------------------------------------------------------------------- #
_DRAFT_N_KEYS = ("draft_n", "n_draft", "num_drafted", "draft_n_tokens", "spec_n")
_DRAFT_ACC_KEYS = (
    "draft_n_accepted",
    "n_draft_accepted",
    "draft_accepted",
    "num_accepted",
    "spec_n_accepted",
)


def _first_number(mapping: dict, keys: Iterable[str]) -> Optional[float]:
    for key in keys:
        if key in mapping:
            try:
                return float(mapping[key])
            except (TypeError, ValueError):
                continue
    return None


def _timing_dicts(chunk: Any) -> list:
    out = []
    if not isinstance(chunk, dict):
        return out
    for key in ("timings", "timings_per_token"):
        value = chunk.get(key)
        if isinstance(value, dict) and value:
            out.append(value)
    return out


def extract_draft(chunk: Any) -> Optional[dict]:
    """Map llama.cpp ``timings``/``timings_per_token`` draft counters."""
    candidates = []
    if isinstance(chunk, dict):
        candidates.append(chunk)
        candidates.extend(_timing_dicts(chunk))
    for mapping in candidates:
        n = _first_number(mapping, _DRAFT_N_KEYS)
        if n is None:
            continue
        accepted = _first_number(mapping, _DRAFT_ACC_KEYS) or 0.0
        n_int = int(round(n))
        acc_int = int(round(accepted))
        if n_int <= 0:
            # draft_n=0 means "no speculative decoding" -> draft: null
            continue
        rate = round(acc_int / n_int, 3) if n_int > 0 else 0.0
        return {"n": n_int, "accepted": acc_int, "acceptance_rate": rate}
    return None


def extract_timings(chunk: Any) -> Optional[dict]:
    timings = _timing_dicts(chunk)
    return timings[0] if timings else None


# --------------------------------------------------------------------------- #
# single request measurement
# --------------------------------------------------------------------------- #
def _usage_tokens(usage: Any, key: str) -> Optional[int]:
    if not isinstance(usage, dict):
        return None
    for candidate in (key, key + "_tokens", "input_" + key if key == "prompt" else ""):
        if candidate and candidate in usage:
            try:
                return int(usage[candidate])
            except (TypeError, ValueError):
                continue
    return None


async def measure_request(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: Optional[str],
    model: str,
    prompt: str,
    max_tokens: int,
    timeout_s: float = 600.0,
    stream: bool = True,
    temperature: float = 0.0,
) -> dict:
    """One chat completion; returns raw timing/token data.

    Raises :class:`EngineError` on transport/HTTP failure.  Never raises for a
    malformed SSE chunk - those are skipped.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(max_tokens),
        "temperature": temperature,
        "stream": bool(stream),
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    timeout = httpx.Timeout(timeout_s, connect=min(15.0, timeout_s))
    t_send = time.perf_counter()
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    content_parts: list = []
    content_deltas = 0
    reasoning_deltas = 0
    usage: Optional[dict] = None
    timings: Optional[dict] = None
    draft: Optional[dict] = None
    non_stream = False

    try:
        async with client.stream(
            "POST", url, json=payload, headers=headers, timeout=timeout
        ) as response:
            if response.status_code >= 400:
                raw = b""
                try:
                    raw = await response.aread()
                except Exception:  # pragma: no cover - best effort
                    pass
                body = raw.decode("utf-8", "replace")[:2000]
                raise EngineError(
                    "HTTP %s: %s" % (response.status_code, body.strip()[:300]),
                    status=response.status_code,
                    body=body,
                )
            content_type = (response.headers.get("content-type") or "").lower()
            if "event-stream" not in content_type:
                # Non-stream fallback: the endpoint ignored stream:true.
                non_stream = True
                raw = await response.aread()
                t_end = time.perf_counter()
                try:
                    body = json.loads(raw.decode("utf-8", "replace"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise EngineError("invalid JSON response: %s" % exc)
                usage = body.get("usage") if isinstance(body, dict) else None
                timings = extract_timings(body)
                draft = extract_draft(body)
                text = ""
                if isinstance(body, dict):
                    choices = body.get("choices") or []
                    if choices and isinstance(choices[0], dict):
                        message = choices[0].get("message") or {}
                        text = message.get("content") or ""
                        if not text and message.get("reasoning_content"):
                            text = message.get("reasoning_content") or ""
                if text:
                    content_parts.append(text)
                    content_deltas = 1
                first_ts = last_ts = t_end
                completion_tokens = _usage_tokens(usage, "completion")
                if completion_tokens is None:
                    completion_tokens = max(1, len(text) // 4) if text else 0
                return {
                    "ok": True,
                    "non_stream": True,
                    "ttft_ms": round((t_end - t_send) * 1000.0, 2),
                    "total_s": round(t_end - t_send, 3),
                    "completion_tokens": int(completion_tokens),
                    "prompt_tokens": _usage_tokens(usage, "prompt"),
                    "usage": usage,
                    "timings": timings,
                    "draft": draft,
                    "n_deltas": content_deltas,
                    "text": "".join(content_parts),
                    "_first_ts": t_end,
                    "_last_ts": t_end,
                }

            async for line in response.aiter_lines():
                if not line:
                    continue
                if line.startswith(":"):
                    continue  # SSE comment / keep-alive
                if not line.startswith("data:"):
                    continue  # event:/id:/retry: lines are irrelevant here
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                now = time.perf_counter()
                chunk_usage = chunk.get("usage")
                if isinstance(chunk_usage, dict) and chunk_usage:
                    usage = chunk_usage
                chunk_draft = extract_draft(chunk)
                if chunk_draft is not None:
                    draft = chunk_draft
                chunk_timings = extract_timings(chunk)
                if chunk_timings is not None:
                    timings = chunk_timings

                choices = chunk.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    continue
                choice = choices[0]
                delta = choice.get("delta") or choice.get("message") or {}
                if not isinstance(delta, dict):
                    delta = {}
                piece = delta.get("content") or ""
                reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                tool_calls = delta.get("tool_calls")
                if piece or reasoning or tool_calls:
                    if first_ts is None:
                        first_ts = now
                    last_ts = now
                if piece:
                    content_parts.append(piece)
                    content_deltas += 1
                elif reasoning:
                    reasoning_deltas += 1
    except EngineError:
        raise
    except httpx.TimeoutException as exc:
        raise EngineError("timeout after %.0fs: %s" % (timeout_s, exc.__class__.__name__))
    except httpx.HTTPError as exc:
        raise EngineError("transport error: %s: %s" % (exc.__class__.__name__, exc))
    except (ValueError, UnicodeDecodeError) as exc:
        raise EngineError("invalid response stream: %s" % exc)

    t_end = time.perf_counter()
    if first_ts is None:
        first_ts = t_end
    if last_ts is None:
        last_ts = first_ts
    completion_tokens = _usage_tokens(usage, "completion")
    if completion_tokens is None:
        completion_tokens = content_deltas or (content_deltas + reasoning_deltas)
    return {
        "ok": True,
        "non_stream": non_stream,
        "ttft_ms": round((first_ts - t_send) * 1000.0, 2),
        "total_s": round(t_end - t_send, 3),
        "completion_tokens": int(completion_tokens),
        "prompt_tokens": _usage_tokens(usage, "prompt"),
        "usage": usage,
        "timings": timings,
        "draft": draft,
        "n_deltas": content_deltas,
        "text": "".join(content_parts),
        "_first_ts": first_ts,
        "_last_ts": last_ts,
        "_decode_tps": decode_tps(
            completion_tokens, first_ts * 1000.0, last_ts * 1000.0
        ),
    }


# --------------------------------------------------------------------------- #
# point construction
# --------------------------------------------------------------------------- #
def failed_context_point(
    ctx_requested: int, max_tokens: int, error: str, cache_bust: bool = True
) -> dict:
    return {
        "ctx_requested": int(ctx_requested),
        "prompt_tokens": 0,
        "max_tokens": int(max_tokens),
        "ttft_ms": 0,
        "ttft_corrected_ms": 0,
        "prompt_ms": 0.5,
        "prefill_tps": None,
        "decode_tps": 0,
        "total_s": 0,
        "completion_tokens": 0,
        "ok": False,
        "error": error,
        "prefill_source": None,
        "prefill_tps_server": None,
        "prompt_ms_server": None,
        "cache_bust": bool(cache_bust),
        "draft": None,
    }


def failed_concurrency_point(n: int, prompt_tokens: int, error: str) -> dict:
    return {
        "n": int(n),
        "prompt_tokens": int(prompt_tokens or 0),
        "wall_s": 0,
        "agg_tps": 0,
        "per_req_tps": 0,
        "ttft_p50": 0,
        "ttft_p95": 0,
        "completion_tokens": 0,
        "ok": False,
        "error": error,
    }


def _timings_float(timings: Any, key: str) -> Optional[float]:
    """Read a numeric field out of a llama.cpp ``timings`` dict."""
    if not isinstance(timings, dict):
        return None
    value = timings.get(key)
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def build_context_point(
    ctx_requested: int,
    max_tokens: int,
    measured: dict,
    ping_ms_p50: float = 0.0,
    prompt_text: str = "",
    cache_bust: bool = True,
) -> dict:
    """Turn a :func:`measure_request` result into a context ladder point.

    Prefill rules (CONTRACT.md "Measurement definitions"):

    * ``prompt_ms = max(ttft_corrected_ms, 0.5)`` - never divide by ~0;
    * ``prefill_source == "server"`` when llama.cpp reports
      ``timings.prompt_per_second`` (that value becomes ``prefill_tps``);
    * ``"derived"`` only when the client-side correction is credible
      (``ttft_corrected_ms >= 2.0`` and ``>= 5%`` of the raw TTFT);
    * otherwise ``"unreliable"`` with ``prefill_tps = None`` - a link-noise
      artifact must never be published;
    * no value above :data:`MAX_PREFILL_TPS` (1e6 tok/s) ever leaves the engine.
    """
    ttft_ms = float(measured.get("ttft_ms") or 0.0)
    ttft_corrected = max(0.0, ttft_ms - float(ping_ms_p50 or 0.0))
    prompt_ms = max(ttft_corrected, 0.5)

    prompt_tokens = measured.get("prompt_tokens")
    if prompt_tokens is None:
        prompt_tokens = max(1, len(prompt_text) // 4)
    prompt_tokens = int(prompt_tokens)
    completion_tokens = int(measured.get("completion_tokens") or 0)
    total_s = float(measured.get("total_s") or 0.0)

    # --- decode tok/s (independent of the prefill correction) --------------
    if completion_tokens < MIN_DECODE_TOKENS:
        # Not enough generated tokens to time a decode rate: publish null, not 0.
        decode: Optional[float] = None
    elif measured.get("non_stream"):
        # No separate TTFT exists; treat the whole request as generation.
        decode = round(completion_tokens / total_s, 2) if total_s > 0 else None
    else:
        first_ts = measured.get("_first_ts")
        last_ts = measured.get("_last_ts")
        if first_ts is not None and last_ts is not None:
            decode = decode_tps(completion_tokens, first_ts * 1000.0, last_ts * 1000.0)
        else:
            raw_decode = measured.get("decode_tps")
            decode = round(float(raw_decode), 2) if raw_decode else None

    # --- prefill tok/s + provenance ---------------------------------------
    timings = measured.get("timings")
    server_tps = _timings_float(timings, "prompt_per_second")
    server_ms = _timings_float(timings, "prompt_ms")
    if server_tps is not None and (server_tps <= 0 or server_tps > MAX_PREFILL_TPS):
        server_tps = None
    if server_ms is not None and server_ms < 0:
        server_ms = None

    derived_tps = prefill_tps(prompt_tokens, prompt_ms)
    credible = (
        ttft_corrected >= 2.0
        and ttft_ms > 0
        and ttft_corrected >= 0.05 * ttft_ms
    )

    if server_tps is not None:
        prefill_source = "server"
        prefill_value: Optional[float] = round(server_tps, 2)
    elif credible and 0 < derived_tps <= MAX_PREFILL_TPS:
        prefill_source = "derived"
        prefill_value = derived_tps
    else:
        prefill_source = "unreliable"
        prefill_value = None

    return {
        "ctx_requested": int(ctx_requested),
        "prompt_tokens": prompt_tokens,
        "max_tokens": int(max_tokens),
        "ttft_ms": round(ttft_ms, 2),
        "ttft_corrected_ms": round(ttft_corrected, 2),
        "prompt_ms": round(prompt_ms, 2),
        "prefill_tps": prefill_value,
        "decode_tps": decode,
        "total_s": round(total_s, 3),
        "completion_tokens": completion_tokens,
        "ok": True,
        "error": None,
        "prefill_source": prefill_source,
        "prefill_tps_server": round(server_tps, 2) if server_tps is not None else None,
        "prompt_ms_server": round(server_ms, 2) if server_ms is not None else None,
        "cache_bust": bool(cache_bust),
        "draft": measured.get("draft"),
    }


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
class Engine:
    """Runs one benchmark suite against one endpoint.

    ``results`` is mutated in place (the same dict the API persists) and
    ``on_point(kind, point)`` / ``on_progress(progress)`` are notification
    hooks used by the SSE layer.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str],
        model: str,
        config: Optional[dict] = None,
        *,
        results: Optional[dict] = None,
        on_progress: Optional[Callable[[dict], None]] = None,
        on_point: Optional[Callable[[str, dict], None]] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
        on_overhead: Optional[Callable[[dict], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.config = normalize_config(config)
        self.results = results if results is not None else {}
        self.on_progress = on_progress
        self.on_point = on_point
        self.on_log = on_log
        self.on_overhead = on_overhead
        self.should_cancel = should_cancel
        self.notes: list = []
        self.overhead: Optional[dict] = None
        self.models_context_length: Optional[int] = None
        self.client: Optional[httpx.AsyncClient] = None
        self.chars_per_token = DEFAULT_CHARS_PER_TOKEN
        self._rescaled = False
        self._step = 0
        self._total = 0

    # -- plumbing ---------------------------------------------------------- #
    def _log(self, level: str, message: str) -> None:
        if self.on_log:
            try:
                self.on_log(level, message)
            except Exception:  # pragma: no cover - never break a run
                pass

    def _note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def _check_cancel(self) -> None:
        if self.should_cancel and self.should_cancel():
            raise RunCancelled("run cancelled")

    def _progress(self, phase: str, label: str, step: Optional[int] = None) -> None:
        if step is not None:
            self._step = step
        total = max(1, self._total)
        progress = {
            "phase": phase,
            "step": int(self._step),
            "total": int(total),
            "pct": int(round(100.0 * self._step / total)),
            "label": label,
        }
        if self.on_progress:
            try:
                self.on_progress(progress)
            except Exception:  # pragma: no cover
                pass

    def _emit_point(self, kind: str, point: dict) -> None:
        if self.on_point:
            try:
                self.on_point(kind, point)
            except Exception:  # pragma: no cover
                pass

    async def _measure(self, prompt: str, max_tokens: int) -> dict:
        self._check_cancel()
        assert self.client is not None
        measured = await measure_request(
            self.client,
            self.base_url,
            self.api_key,
            self.model,
            prompt,
            max_tokens,
            self.config["request_timeout_s"],
        )
        if measured.get("non_stream"):
            self._note("endpoint ignored stream=true; TTFT/decode are approximations")
        return measured

    # -- phases ------------------------------------------------------------ #
    async def _fetch_models_context(self) -> Optional[int]:
        """Best-effort ``GET {base}/models`` -> context_length of our model."""
        assert self.client is not None
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        try:
            response = await self.client.get(
                self.base_url + "/models", headers=headers, timeout=10.0
            )
            if response.status_code >= 400:
                return None
            data = response.json()
        except Exception:
            return None
        models = []
        if isinstance(data, dict):
            models = data.get("data") or data.get("models") or []
        elif isinstance(data, list):
            models = data
        if not isinstance(models, list):
            return None
        chosen = None
        for entry in models:
            if isinstance(entry, dict) and entry.get("id") == self.model:
                chosen = entry
                break
        if chosen is None:
            for entry in models:
                if isinstance(entry, dict):
                    chosen = entry
                    break
        if not isinstance(chosen, dict):
            return None
        return _context_length_from_model(chosen)

    async def _overhead_phase(self) -> None:
        pings: list = []
        errors: list = []
        for index in range(5):
            self._check_cancel()
            self._progress("overhead", "overhead ping %d / 5" % (index + 1), step=0)
            try:
                measured = await self._measure(PING_PROMPT, 1)
                pings.append(float(measured.get("ttft_ms") or 0.0))
            except EngineError as exc:
                errors.append(exc.message)
                self._log("warn", "overhead ping %d failed: %s" % (index + 1, exc.message))
        if not pings:
            raise EngineError(
                "endpoint unreachable: %s" % (errors[0] if errors else "no response")
            )
        self.overhead = {
            "ping_ms_p50": round(percentile(sorted(pings), 50), 2),
            "samples": len(pings),
        }
        if self.on_overhead:
            try:
                self.on_overhead(self.overhead)
            except Exception:  # pragma: no cover
                pass
        self._step = 1

    async def _calibrate_chars_per_token(self) -> None:
        """Derive this tokenizer's chars-per-token from the warmup request.

        CONTRACT.md "Tokenizer calibration": a 512-token warmup filler reports
        ``usage.prompt_tokens``; ``chars_per_token = len(prompt)/prompt_tokens``
        then sizes every ladder rung so ``prompt_tokens`` lands within ~10% of
        ``ctx_requested`` on any tokenizer.
        """
        # Pure dense filler (no nonce, no tail): the ratio must describe the
        # filler that scales with the rung, not the longer nonce/tail words.
        prompt = _dense_text(int(round(512 * self.chars_per_token)))
        try:
            measured = await self._measure(prompt, 1)
        except EngineError as exc:
            self._log("warn", "tokenizer calibration request failed: %s" % exc.message)
            return
        tokens = measured.get("prompt_tokens")
        try:
            tokens = float(tokens)
        except (TypeError, ValueError):
            return
        if tokens <= 0:
            return
        ratio = len(prompt) / tokens
        if 0.5 <= ratio <= 40.0:
            self.chars_per_token = ratio
            self._log(
                "info",
                "tokenizer calibrated: %.2f chars/token (%d-char warmup -> %d tokens)"
                % (ratio, len(prompt), int(tokens)),
            )

    async def _warmup_phase(self) -> None:
        # One cheap request also calibrates the filler for this endpoint.
        await self._calibrate_chars_per_token()
        warmup = int(self.config["warmup"] or 0)
        if warmup <= 0:
            return
        ladder = self.config["contexts"]
        warm_ctx = min(ladder[0], self.config["concurrency_context"])
        prompt = build_prompt(
            warm_ctx, nonce=make_nonce(0), chars_per_token=self.chars_per_token
        )
        for index in range(warmup):
            self._check_cancel()
            self._progress("overhead", "warmup %d / %d" % (index + 1, warmup), step=1)
            try:
                await self._measure(prompt, self.config["max_tokens"])
            except EngineError as exc:
                self._note("warmup request failed: %s" % exc.message)
                self._log("warn", "warmup failed: %s" % exc.message)
                break

    async def _context_phase(self) -> None:
        ladder = list(self.config["contexts"])
        repeats = int(self.config["repeats"])
        max_tokens = int(self.config["max_tokens"])
        models_ctx = self.models_context_length
        if models_ctx:
            self._note("/v1/models reports context_length=%d" % models_ctx)
        overflow = False
        for index, rung in enumerate(ladder):
            for rep in range(repeats):
                self._step += 1
                self._progress(
                    "context",
                    "context %d / %d" % (rung, len(ladder))
                    + (" rep %d/%d" % (rep + 1, repeats) if repeats > 1 else ""),
                )
                if models_ctx and rung > models_ctx:
                    point = failed_context_point(rung, max_tokens, "exceeds_model_context")
                    self.results.setdefault("context_points", []).append(point)
                    self._emit_point("context", point)
                    continue
                if overflow:
                    point = failed_context_point(rung, max_tokens, "context_exceeded")
                    self.results.setdefault("context_points", []).append(point)
                    self._emit_point("context", point)
                    continue
                # Cache-bust every ladder request (llama.cpp keeps prefixes).
                prompt = build_prompt(
                    rung, nonce=make_nonce(rung), chars_per_token=self.chars_per_token
                )
                try:
                    measured = await self._measure(prompt, max_tokens)
                except EngineError as exc:
                    if exc.is_context_error:
                        overflow = True
                        self._note(
                            "context %d exceeded the endpoint context window" % rung
                        )
                        error = "context_exceeded"
                    else:
                        error = exc.message[:300]
                    self._log("warn", "context %d failed: %s" % (rung, error))
                    point = failed_context_point(rung, max_tokens, error)
                    self.results.setdefault("context_points", []).append(point)
                    self._emit_point("context", point)
                    continue
                point = build_context_point(
                    rung,
                    max_tokens,
                    measured,
                    ping_ms_p50=(self.overhead or {}).get("ping_ms_p50", 0.0),
                    prompt_text=prompt,
                )
                self._maybe_rescale(point, rung)
                if point.get("completion_tokens", 0) < MIN_DECODE_TOKENS:
                    self._note(
                        "decode tok/s not measurable at rung %d "
                        "(only %d completion tokens)"
                        % (rung, int(point.get("completion_tokens") or 0))
                    )
                self.results.setdefault("context_points", []).append(point)
                self._emit_point("context", point)

    async def _concurrency_phase(self) -> None:
        ladder = list(self.config["concurrency"])
        max_tokens = int(self.config["max_tokens"])
        for index, n in enumerate(ladder):
            self._check_cancel()
            self._step += 1
            self._progress(
                "concurrency",
                "concurrency %d / %d (n=%d)" % (index + 1, len(ladder), n),
            )
            # Identical requests within a sweep, fresh nonce per sweep.
            prompt = build_prompt(
                self.config["concurrency_context"],
                nonce=make_nonce(n),
                chars_per_token=self.chars_per_token,
            )
            try:
                point = await self._concurrency_point(n, prompt, max_tokens)
            except EngineError as exc:
                point = failed_concurrency_point(n, 0, exc.message[:300])
                self._log("warn", "concurrency n=%d failed: %s" % (n, exc.message))
            except Exception as exc:  # pragma: no cover - defensive
                point = failed_concurrency_point(n, 0, "%s: %s" % (exc.__class__.__name__, exc))
                self._log("warn", "concurrency n=%d failed: %s" % (n, exc))
            if point.get("per_req_tps") is None:
                self._note(
                    "decode tok/s not measurable at concurrency n=%d "
                    "(completions under %d tokens)"
                    % (n, MIN_DECODE_TOKENS)
                )
            self.results.setdefault("concurrency_points", []).append(point)
            self._emit_point("concurrency", point)

    async def _concurrency_point(self, n: int, prompt: str, max_tokens: int) -> dict:
        t_start = time.perf_counter()
        tasks = [self._measure(prompt, max_tokens) for _ in range(n)]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        wall_s = time.perf_counter() - t_start

        ttfts: list = []
        decodes: list = []
        completion_total = 0
        prompt_tokens = None
        errors: list = []
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                if isinstance(outcome, EngineError):
                    errors.append(outcome.message[:200])
                elif isinstance(outcome, asyncio.CancelledError):
                    raise RunCancelled("run cancelled")
                else:
                    errors.append("%s: %s" % (outcome.__class__.__name__, outcome))
                continue
            ttfts.append(float(outcome.get("ttft_ms") or 0.0))
            completion_total += int(outcome.get("completion_tokens") or 0)
            if prompt_tokens is None and outcome.get("prompt_tokens") is not None:
                prompt_tokens = int(outcome.get("prompt_tokens"))
            if not outcome.get("non_stream"):
                decode = _decode_from_measured(outcome)
                if decode:
                    decodes.append(decode)

        if not ttfts and errors:
            raise EngineError("all %d requests failed: %s" % (n, errors[0]))

        ordered = sorted(ttfts)
        point = {
            "n": int(n),
            "prompt_tokens": int(prompt_tokens or 0),
            "wall_s": round(wall_s, 3),
            "agg_tps": round(completion_total / wall_s, 2) if wall_s > 0 else 0,
            "per_req_tps": round(sum(decodes) / len(decodes), 2) if decodes else None,
            "ttft_p50": round(percentile(ordered, 50), 2) if ordered else 0,
            "ttft_p95": round(percentile(ordered, 95), 2) if ordered else 0,
            "completion_tokens": completion_total,
            "ok": not errors,
            "error": errors[0] if errors else None,
        }
        return point

    def _maybe_rescale(self, point: dict, rung: int) -> None:
        """Re-check filler sizing once after the first rung (CONTRACT.md)."""
        if self._rescaled:
            return
        self._rescaled = True
        tokens = point.get("prompt_tokens") or 0
        if not tokens or rung <= 0:
            return
        error = abs(tokens - rung) / float(rung)
        if error > 0.15:
            ratio = self.chars_per_token * (float(rung) / float(tokens))
            if 0.5 <= ratio <= 40.0:
                self.chars_per_token = ratio
                self._note(
                    "prompt filler rescaled after rung %d (%d tokens, %.2f chars/token)"
                    % (rung, tokens, ratio)
                )
                self._log(
                    "info",
                    "prompt filler rescaled: rung %d measured %d tokens -> %.2f chars/token"
                    % (rung, tokens, ratio),
                )

    # -- aggregates -------------------------------------------------------- #
    def finalize(self) -> dict:
        results = self.results
        context_points = [p for p in (results.get("context_points") or []) if p.get("ok")]
        results["ttft_ms"] = aggregate_stats([p.get("ttft_ms") for p in context_points])
        results["decode_tps"] = aggregate_stats([p.get("decode_tps") for p in context_points])
        # Only real prefill values: nulls ("unreliable") and zeros are excluded
        # so the aggregate can never inherit a link-noise artifact.
        prefill_values = [
            p.get("prefill_tps")
            for p in context_points
            if p.get("prefill_tps") and p.get("prefill_tps") > 0
        ]
        results["prompt_tps"] = aggregate_stats(prefill_values)
        unreliable = [
            p.get("ctx_requested")
            for p in context_points
            if p.get("prefill_source") == "unreliable"
        ]
        if unreliable:
            self._note(
                "prefill tok/s unavailable for rung(s) %s "
                "(TTFT correction collapsed and no server timings)"
                % ", ".join(str(int(r)) for r in unreliable if r is not None)
            )
        results["total_s"] = aggregate_stats([p.get("total_s") for p in context_points])

        all_ok = [
            p
            for p in (results.get("context_points") or [])
            + (results.get("concurrency_points") or [])
            if p.get("ok")
        ]
        results["tokens"] = {
            "prompt": int(sum(int(p.get("prompt_tokens") or 0) for p in all_ok)),
            "completion": int(sum(int(p.get("completion_tokens") or 0) for p in all_ok)),
        }
        existing_notes = results.get("notes") if isinstance(results.get("notes"), list) else []
        merged = list(existing_notes)
        for note in self.notes:
            if note not in merged:
                merged.append(note)
        results["notes"] = merged
        results.setdefault(
            "gpu",
            {
                "available": False,
                "idle": {"vram_mb": 0, "ram_mb": 0, "gpu_util": 0, "power_w": 0, "temp_c": 0},
                "peak": {"vram_mb": 0, "ram_mb": 0, "gpu_util": 0, "power_w": 0, "temp_c": 0},
                "avg": {"vram_mb": 0, "ram_mb": 0, "gpu_util": 0, "power_w": 0, "temp_c": 0},
                "samples_n": 0,
            },
        )
        results.setdefault("model_load_s", None)
        return results

    # -- entry point ------------------------------------------------------- #
    async def run(self) -> dict:
        config = self.config
        self.results.setdefault("context_points", [])
        self.results.setdefault("concurrency_points", [])
        context_steps = (
            len(config["contexts"]) * int(config["repeats"])
            if config["enable_context_ladder"]
            else 0
        )
        concurrency_steps = len(config["concurrency"]) if config["enable_concurrency"] else 0
        self._total = 1 + context_steps + concurrency_steps
        self._step = 0

        max_parallel = max([1] + list(config["concurrency"]))
        limits = httpx.Limits(
            max_connections=max(16, max_parallel * 2),
            max_keepalive_connections=16,
        )
        timeout = httpx.Timeout(config["request_timeout_s"], connect=min(15.0, config["request_timeout_s"]))
        self._progress("overhead", "overhead ping", step=0)
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
            self.client = client
            try:
                self.models_context_length = await self._fetch_models_context()
                await self._overhead_phase()
                await self._warmup_phase()
                if config["enable_context_ladder"]:
                    await self._context_phase()
                if config["enable_concurrency"]:
                    await self._concurrency_phase()
            finally:
                self.client = None
        self._step = self._total
        self._progress("finish", "finish")
        return self.finalize()


def _decode_from_measured(measured: dict) -> Optional[float]:
    """Per-request decode rate, or None when it is not measurable."""
    first = measured.get("_first_ts")
    last = measured.get("_last_ts")
    tokens = int(measured.get("completion_tokens") or 0)
    if tokens < MIN_DECODE_TOKENS:
        return None
    if first is not None and last is not None:
        rate = decode_tps(tokens, float(first) * 1000.0, float(last) * 1000.0)
        return rate or None
    return None


def _context_length_from_model(entry: dict) -> Optional[int]:
    """Pull a context window out of an OpenAI-ish ``/models`` entry."""
    candidates = [
        entry.get("context_length"),
        entry.get("max_model_len"),
        entry.get("n_ctx"),
        entry.get("context_window"),
        entry.get("loaded_context_length"),
    ]
    meta = entry.get("meta")
    if isinstance(meta, dict):
        candidates.extend(
            [
                meta.get("n_ctx_train"),
                meta.get("n_ctx"),
                meta.get("context_length"),
            ]
        )
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None
