"""Contract-level unit tests for backend/engine.py pure helpers.

CONTRACT.md "Measurement definitions" is the authority; these tests pin the
exact formulas so the API-level tests can assert identities on real JSON.
"""

from __future__ import annotations

import math

import pytest


# ---------------------------------------------------------------------------
# percentile / aggregate_stats
# ---------------------------------------------------------------------------


def test_percentile_linear_interpolation(engine_mod):
    values = [10, 20, 30, 40, 50]
    assert engine_mod.percentile(values, 0) == 10
    assert engine_mod.percentile(values, 50) == 30
    assert engine_mod.percentile(values, 100) == 50
    assert engine_mod.percentile(values, 25) == 20
    assert engine_mod.percentile(values, 95) == pytest.approx(48.0)
    assert engine_mod.percentile([], 50) == 0.0
    assert engine_mod.percentile([7], 95) == 7


def test_aggregate_stats_contract_shape(engine_mod):
    stats = engine_mod.aggregate_stats([10, 20, 30, 40, 50])
    assert set(stats) == {"mean", "p50", "p95", "min", "max", "n"}
    assert stats["n"] == 5
    assert stats["mean"] == pytest.approx(30.0)
    assert stats["p50"] == pytest.approx(30.0)
    assert stats["p95"] == pytest.approx(48.0)
    assert stats["min"] == 10 and stats["max"] == 50


def test_aggregate_stats_ignores_junk_and_empty(engine_mod):
    stats = engine_mod.aggregate_stats([1, None, "x", 3, float("nan"), 5])
    assert stats["n"] == 3
    assert stats["mean"] == pytest.approx(3.0)
    assert stats["max"] == 5
    empty = engine_mod.aggregate_stats([])
    assert empty == {"mean": 0, "p50": 0, "p95": 0, "min": 0, "max": 0, "n": 0}


# ---------------------------------------------------------------------------
# prefill / decode formulas (CONTRACT.md lines 154-156)
# ---------------------------------------------------------------------------


def test_prefill_tps_formula(engine_mod):
    # 8192 tokens in 794 ms -> 10317.38 tok/s
    assert engine_mod.prefill_tps(8192, 794.0) == pytest.approx(8192 / 0.794, rel=1e-6)
    assert engine_mod.prefill_tps(1000, 100.0) == pytest.approx(10000.0)
    # unusable timings collapse to 0 instead of raising / dividing by zero
    assert engine_mod.prefill_tps(1000, 0) == 0.0
    assert engine_mod.prefill_tps(1000, -5) == 0.0
    assert engine_mod.prefill_tps(0, 100.0) == 0.0
    assert engine_mod.prefill_tps(None, 100.0) == 0.0


def test_decode_tps_formula(engine_mod):
    # 128 completion tokens between t=1000ms and t=4000ms -> 127 tokens / 3 s
    # (the helper rounds to 2 decimals, like the API value)
    assert engine_mod.decode_tps(128, 1000.0, 4000.0) == pytest.approx(round(127 / 3.0, 2))
    assert engine_mod.decode_tps(2, 0.0, 1000.0) == pytest.approx(1.0)
    # single token / zero span / junk
    assert engine_mod.decode_tps(1, 0.0, 1000.0) == 0.0
    assert engine_mod.decode_tps(8, 1000.0, 1000.0) == 0.0
    assert engine_mod.decode_tps(None, 0, 1) == 0.0


# ---------------------------------------------------------------------------
# synthetic prompt (CONTRACT.md line 163)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ctx", [512, 4096, 32768])
def test_build_prompt_lands_near_requested_tokens(engine_mod, ctx):
    """Synthetic prompt must land on ~ctx tokens and end with the long-answer tail.

    CONTRACT.md "Decode measurements need room": every measured prompt ends with
    the long-generation instruction (never "reply with one word"), so there is
    something to time. The dense filler plus the calibrated chars/token ratio
    must land ``usage.prompt_tokens`` within 10% of the rung on the mock's
    words/0.75 tokenizer.
    """
    from mock_openai import count_tokens

    prompt = engine_mod.build_prompt(ctx)
    assert prompt.endswith(engine_mod.PROMPT_TAIL)
    assert "at least 250 words" in prompt
    assert "computer hardware" in prompt
    assert "Reply with the single word: ready." not in prompt  # obsolete tail
    assert engine_mod.build_prompt(ctx) == prompt  # deterministic
    assert abs(count_tokens(prompt) - ctx) <= max(8, int(ctx * 0.10)), (
        count_tokens(prompt),
        ctx,
    )


def test_build_prompt_scales_with_context(engine_mod):
    from mock_openai import count_tokens

    small = engine_mod.build_prompt(1024)
    large = engine_mod.build_prompt(8192)
    assert count_tokens(large) >= 7.5 * count_tokens(small)
    assert len(large) > 7 * len(small)
    assert small.endswith(engine_mod.PROMPT_TAIL)
    assert large.endswith(engine_mod.PROMPT_TAIL)


def test_build_prompt_nonce_is_unique_and_included(engine_mod):
    """Cache-busting nonce (CONTRACT.md keeps llama.cpp prefix cache honest)."""
    from mock_openai import count_tokens

    nonce = engine_mod.make_nonce(4096)
    prompt = engine_mod.build_prompt(4096, nonce=nonce)
    assert nonce.strip() in prompt
    assert len(nonce.split()) >= 3
    assert prompt.endswith(engine_mod.PROMPT_TAIL)
    assert "4096" in nonce
    assert engine_mod.make_nonce(4096) != engine_mod.make_nonce(4096)
    # the nonce is budgeted inside the rung, it must not inflate the token count
    assert abs(count_tokens(prompt) - 4096) <= max(8, int(4096 * 0.10)), count_tokens(prompt)


def test_build_prompt_chars_per_token_controls_filler(engine_mod):
    """Calibration hook: a larger chars/token ratio yields a longer filler."""
    from mock_openai import count_tokens

    default = engine_mod.build_prompt(1024)
    doubled = engine_mod.build_prompt(
        1024, chars_per_token=engine_mod.DEFAULT_CHARS_PER_TOKEN * 2.0
    )
    assert count_tokens(doubled) > 1.7 * count_tokens(default)
    assert doubled.endswith(engine_mod.PROMPT_TAIL)


# ---------------------------------------------------------------------------
# SSRF / base_url validation (CONTRACT.md line 65)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://localhost:8080/v1",
        "http://LOCALHOST/v1",
        "localhost:8080",
        "http://169.254.169.254/latest/meta-data",
        "169.254.1.1:8080",
        "http://[::1]:8080/v1",
        "http://metadata.google.internal/v1",
        "ftp://example.com/v1",
        "file:///etc/passwd",
        "gopher://example.com",
        "://missing-scheme",
        "",
        "   ",
    ],
)
def test_validate_base_url_rejects_ssrf_targets(engine_mod, bad_url):
    with pytest.raises(ValueError):
        engine_mod.validate_base_url(bad_url)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080/v1"),
        ("http://127.0.0.1:8080/v1/", "http://127.0.0.1:8080/v1"),
        ("127.0.0.1:8080", "http://127.0.0.1:8080/v1"),
        ("https://api.example.com/v1", "https://api.example.com/v1"),
        ("http://192.168.1.50:8080/v1/chat", "http://192.168.1.50:8080/v1/chat"),
        ("https://api.example.com", "https://api.example.com/v1"),
    ],
)
def test_validate_base_url_normalizes(engine_mod, raw, expected):
    assert engine_mod.validate_base_url(raw) == expected


def test_endpoint_host_never_includes_scheme_or_key(engine_mod):
    assert engine_mod.endpoint_host("http://1.2.3.4:8080/v1") == "1.2.3.4:8080"
    assert engine_mod.endpoint_host("https://api.example.com/v1") == "api.example.com"
    assert "sk-" not in engine_mod.endpoint_host("http://10.0.0.5:8000/v1")


# ---------------------------------------------------------------------------
# llama.cpp timings -> draft stats (CONTRACT.md lines 170-172)
# ---------------------------------------------------------------------------


def test_extract_draft_maps_llama_cpp_timings(engine_mod):
    chunk = {"timings": {"draft_n": 96, "draft_n_accepted": 61}}
    draft = engine_mod.extract_draft(chunk)
    assert draft == {"n": 96, "accepted": 61, "acceptance_rate": pytest.approx(0.635)}


def test_extract_draft_supports_timings_per_token_and_flat(engine_mod):
    nested = {"timings_per_token": {"draft_n": 10, "draft_n_accepted": 7}}
    assert engine_mod.extract_draft(nested)["accepted"] == 7
    flat = {"draft_n": 4, "draft_n_accepted": 2}
    assert engine_mod.extract_draft(flat) == {"n": 4, "accepted": 2, "acceptance_rate": 0.5}


def test_extract_draft_absent_returns_none(engine_mod):
    assert engine_mod.extract_draft({}) is None
    assert engine_mod.extract_draft({"timings": {"prompt_n": 5}}) is None
    assert engine_mod.extract_draft(None) is None


# ---------------------------------------------------------------------------
# point construction
# ---------------------------------------------------------------------------


def _measured(ttft_ms=812.0, **overrides):
    measured = {
        "ttft_ms": ttft_ms,
        "total_s": 4.9,
        "completion_tokens": 128,
        "prompt_tokens": 8211,
        "_first_ts": 1.0,
        "_last_ts": 13.0,  # perf_counter seconds -> 12 s span -> 127/12 tok/s
        "draft": {"n": 96, "accepted": 61, "acceptance_rate": 0.635},
    }
    measured.update(overrides)
    return measured


def test_build_context_point_derived_prefill_math(engine_mod):
    """No llama.cpp timings -> prefill_tps derived from the corrected TTFT."""
    point = engine_mod.build_context_point(8192, 128, _measured(), ping_ms_p50=18.0)

    assert point["ctx_requested"] == 8192
    assert point["prompt_tokens"] == 8211
    assert point["max_tokens"] == 128
    assert point["ttft_ms"] == pytest.approx(812.0)
    # ttft_corrected_ms = ttft_ms - ping_p50; prompt_ms tracks it (floored at 0.5)
    assert point["ttft_corrected_ms"] == pytest.approx(794.0)
    assert point["prompt_ms"] == pytest.approx(max(794.0, 0.5))
    assert point["prefill_source"] == "derived"
    # prefill_tps = prompt_tokens / (prompt_ms / 1000)
    assert point["prefill_tps"] == pytest.approx(round(8211 / 0.794, 2))
    # decode_tps = (completion_tokens-1) / span
    assert point["decode_tps"] == pytest.approx(round(127 / 12.0, 2))
    assert point["ok"] is True and point["error"] is None
    assert point["draft"]["n"] == 96 and point["draft"]["accepted"] == 61
    assert point["cache_bust"] is True


def test_build_context_point_prefers_server_prefill_timings(engine_mod):
    """llama.cpp ``timings.prompt_per_second`` wins over the derived number."""
    timings = {"prompt_n": 8211, "prompt_ms": 100.0, "prompt_per_second": 5000.0}
    point = engine_mod.build_context_point(
        8192, 128, _measured(timings=timings), ping_ms_p50=18.0
    )
    assert point["prefill_source"] == "server"
    assert point["prefill_tps"] == pytest.approx(5000.0)
    assert point["prefill_tps_server"] == pytest.approx(5000.0)
    assert point["prompt_ms_server"] == pytest.approx(100.0)

    # an absurd server value is not published
    bogus = {"prompt_per_second": 5e7, "prompt_ms": 0.1}
    point = engine_mod.build_context_point(
        8192, 128, _measured(timings=bogus), ping_ms_p50=18.0
    )
    assert point["prefill_source"] != "server"
    assert point["prefill_tps"] is None or point["prefill_tps"] < 1e6


def test_build_context_point_marks_unreliable_prefill(engine_mod):
    """A TTFT indistinguishable from link noise must not publish a prefill rate."""
    measured = _measured(ttft_ms=5.0, total_s=1.0, completion_tokens=8, prompt_tokens=100,
                         _first_ts=0.0, _last_ts=100.0, draft=None)
    point = engine_mod.build_context_point(512, 8, measured, ping_ms_p50=20.0)
    assert point["ttft_corrected_ms"] == 0.0
    assert point["prompt_ms"] == pytest.approx(0.5)  # never divide by zero
    assert point["prefill_source"] == "unreliable"
    assert point["prefill_tps"] is None
    assert point["ok"] is True


def test_build_context_point_decode_is_null_below_min_tokens(engine_mod):
    """CONTRACT.md: completion_tokens < 8 -> decode_tps is None, never 0.0."""
    assert engine_mod.MIN_DECODE_TOKENS == 8

    short = engine_mod.build_context_point(
        512, 4, _measured(completion_tokens=4, draft=None, timings=None), ping_ms_p50=18.0
    )
    assert short["completion_tokens"] == 4
    assert short["decode_tps"] is None, short["decode_tps"]
    assert short["ok"] is True  # the point still counts, only decode is unknown

    measured = engine_mod.build_context_point(
        512,
        engine_mod.MIN_DECODE_TOKENS,
        _measured(completion_tokens=engine_mod.MIN_DECODE_TOKENS, draft=None, timings=None),
        ping_ms_p50=18.0,
    )
    assert measured["decode_tps"] is not None


def test_build_context_point_non_stream_decode(engine_mod):
    measured = {
        "ttft_ms": 500.0,
        "total_s": 2.0,
        "completion_tokens": 100,
        "prompt_tokens": 1000,
        "non_stream": True,
        "draft": None,
    }
    point = engine_mod.build_context_point(1024, 100, measured, ping_ms_p50=10.0)
    assert point["decode_tps"] == pytest.approx(50.0)  # 100 tokens / 2 s


def test_failed_points_match_contract_shape(engine_mod):
    context_point = engine_mod.failed_context_point(32768, 128, "context_exceeded")
    assert context_point["ctx_requested"] == 32768
    assert context_point["ok"] is False
    assert context_point["error"] == "context_exceeded"
    assert context_point["draft"] is None
    assert context_point["prefill_tps"] is None

    concurrency_point = engine_mod.failed_concurrency_point(8, 2048, "timeout")
    assert concurrency_point["n"] == 8
    assert concurrency_point["ok"] is False
    assert concurrency_point["error"] == "timeout"
    assert concurrency_point["wall_s"] == 0


# ---------------------------------------------------------------------------
# config normalisation
# ---------------------------------------------------------------------------


def test_normalize_config_defaults_match_contract(engine_mod):
    config = engine_mod.normalize_config(None)
    assert config["contexts"] == [1024, 4096, 8192, 16384, 32768, 65536]
    assert config["concurrency"] == [1, 2, 4, 8]
    assert config["concurrency_context"] == 2048
    assert config["max_tokens"] == 128
    assert config["request_timeout_s"] == 600
    assert config["enable_context_ladder"] is True


def test_normalize_config_clamps_and_sorts(engine_mod):
    config = engine_mod.normalize_config(
        {
            "contexts": [8192, 512, 512, 999999, -4],
            "concurrency": [0, 8, 2, 999, 2],
            "max_tokens": 999999,
            "repeats": 99,
            "warmup": -3,
            "request_timeout_s": 1,
        }
    )
    assert config["contexts"] == [512, 8192, engine_mod.MAX_CTX]
    assert config["concurrency"] == [2, 8]
    assert config["max_tokens"] == 8192
    assert config["repeats"] == 10
    assert config["warmup"] == 0
    assert config["request_timeout_s"] == 5.0


def test_normalize_config_falls_back_when_ladder_is_empty(engine_mod):
    config = engine_mod.normalize_config({"contexts": [], "concurrency": []})
    assert config["contexts"] == engine_mod.DEFAULT_CONFIG["contexts"]
    assert config["concurrency"] == engine_mod.DEFAULT_CONFIG["concurrency"]


def test_dense_filler_ratio_matches_mock_tokenizer(engine_mod):
    """Artefact note, not a backend bug.

    mock_openai counts ``usage.prompt_tokens = words/0.75`` (2.5227 chars/token
    for the engine's dense word cycle). The engine's default
    ``DEFAULT_CHARS_PER_TOKEN`` is that mock-tokenizer equivalent, and the real
    ratio is calibrated per endpoint from the warmup request. A >10% deviation
    here would mean the mock's heuristic diverged from the engine's filler
    sizing, not that the engine mis-measured.
    """
    from mock_openai import TOKENS_PER_WORD, count_tokens

    assert TOKENS_PER_WORD == 0.75
    assert engine_mod.DENSE_CHARS_PER_WORD == pytest.approx(3.3636, rel=1e-3)
    # default chars/token == dense chars per word / (tokens per word)
    assert engine_mod.DEFAULT_CHARS_PER_TOKEN == pytest.approx(
        engine_mod.DENSE_CHARS_PER_WORD / (1.0 / TOKENS_PER_WORD), rel=1e-9
    )
    assert engine_mod.DEFAULT_CHARS_PER_TOKEN == pytest.approx(2.5227, rel=1e-3)
    assert count_tokens(" ".join(["alpha"] * 40)) == round(40 / 0.75)
    for rung in (512, 1024, 4096, 32768):
        prompt = engine_mod.build_prompt(rung)
        assert abs(count_tokens(prompt) - rung) <= max(8, rung * 0.10), (
            count_tokens(prompt),
            rung,
        )
