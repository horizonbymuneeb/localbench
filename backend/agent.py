"""Telemetry helpers for the optional GPU agent (``agent/bench_agent.py``).

Pure functions only: key generation, sample normalisation, downsampling and
the idle/peak/avg aggregates required by CONTRACT.md.  Keeping this out of
``engine.py`` keeps the measurement core free of ingest concerns.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Iterable, Optional

#: CONTRACT.md: "Server keeps <=4000 samples/run".
MAX_SAMPLES = 4000

#: The five numeric fields of a telemetry sample.
FIELDS = ("vram_mb", "ram_mb", "gpu_util", "power_w", "temp_c")

ZERO_SAMPLE = {field: 0 for field in FIELDS}


def new_agent_key() -> str:
    return "ak_" + secrets.token_hex(8)


def new_run_id() -> str:
    return "r_" + secrets.token_hex(4)


def agent_command(server: str, run_id: str, key: str) -> str:
    """The copy-able one-liner returned by ``GET /api/runs/{id}/agent``."""
    server = (server or "").rstrip("/")
    return (
        "curl -sO {server}/bench_agent.py && "
        "python3 bench_agent.py --server {server} --run {run_id} --key {key}"
    ).format(server=server, run_id=run_id, key=key)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:  # NaN
        return default
    return out


def normalize_sample(raw: Any, phase_default: str = "") -> Optional[dict]:
    """Coerce one agent sample into the contract shape; ``None`` if unusable."""
    if not isinstance(raw, dict):
        return None
    sample = {
        "ts": _num(raw.get("ts"), time.time()),
        "vram_mb": _num(raw.get("vram_mb")),
        "ram_mb": _num(raw.get("ram_mb")),
        "gpu_util": max(0.0, min(100.0, _num(raw.get("gpu_util")))),
        "power_w": _num(raw.get("power_w")),
        "temp_c": _num(raw.get("temp_c")),
        "phase": str(raw.get("phase") or phase_default or ""),
    }
    # Keep vram_total_mb if the agent sent it - useful for VRAM % in the UI.
    if "vram_total_mb" in raw:
        sample["vram_total_mb"] = _num(raw.get("vram_total_mb"))
    return sample


def downsample(samples: Iterable[dict], cap: int = MAX_SAMPLES) -> list:
    """Bucket-max downsample so peaks survive the cap (CONTRACT.md)."""
    samples = list(samples)
    if len(samples) <= cap:
        return samples
    out = []
    n = len(samples)
    size = n / float(cap)
    for i in range(cap):
        lo = int(i * size)
        hi = int((i + 1) * size)
        if hi <= lo:
            hi = lo + 1
        bucket = samples[lo:hi]
        if not bucket:
            continue
        peak = dict(bucket[0])
        for field in FIELDS:
            try:
                peak[field] = max(float(s.get(field) or 0) for s in bucket)
            except (TypeError, ValueError):
                peak[field] = bucket[0].get(field, 0)
        # Timestamp/phase of the hottest sample in the bucket.
        hottest = max(bucket, key=lambda s: _num(s.get("gpu_util")))
        peak["ts"] = hottest.get("ts")
        peak["phase"] = hottest.get("phase", peak.get("phase", ""))
        out.append(peak)
    return out


def _mean(values: list) -> float:
    return round(sum(values) / len(values), 1) if values else 0


def aggregate_gpu(samples: Iterable[dict]) -> dict:
    """Build the ``results.gpu`` block from raw samples.

    ``idle`` comes from samples taken during the idle baseline phase (the
    server marks them ``phase="idle"``); ``peak`` is per-field max and ``avg``
    the mean over every sample.
    """
    samples = [s for s in (samples or []) if isinstance(s, dict)]
    if not samples:
        return {
            "available": False,
            "idle": dict(ZERO_SAMPLE),
            "peak": dict(ZERO_SAMPLE),
            "avg": dict(ZERO_SAMPLE),
            "samples_n": 0,
        }
    idle = [
        s
        for s in samples
        if str(s.get("phase") or "").startswith("idle")
    ]
    idle_block = (
        {field: _mean([_num(s.get(field)) for s in idle]) for field in FIELDS}
        if idle
        else dict(ZERO_SAMPLE)
    )
    peak_block = {}
    for field in FIELDS:
        values = [_num(s.get(field)) for s in samples]
        peak_block[field] = round(max(values), 1) if values else 0
    avg_block = {field: _mean([_num(s.get(field)) for s in samples]) for field in FIELDS}
    return {
        "available": True,
        "idle": idle_block,
        "peak": peak_block,
        "avg": avg_block,
        "samples_n": len(samples),
    }
