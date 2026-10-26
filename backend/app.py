"""LocalBench backend — FastAPI application.

Serves every ``/api`` route from CONTRACT.md, the SSE event stream, the
telemetry agent ingest endpoint, exports, and the static frontend at ``/``
with an SPA fallback to ``index.html``.

Runs are server-side asyncio background tasks: closing the browser tab never
kills a run.  At most ``LOCALBENCH_MAX_RUNS`` (default 2) execute at once; the
rest wait in a FIFO queue with ``status="queued"``.

Secrets: the user's ``api_key`` lives in :class:`RunState` (memory) for the
duration of the run only.  It is never written to sqlite, never returned by an
HTTP response and never placed on the SSE wire.
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

try:  # imported as ``backend.app``
    from . import agent as agent_helpers
    from . import engine as engine_mod
    from . import store
except ImportError:  # pragma: no cover - imported with backend/ on sys.path
    import agent as agent_helpers  # type: ignore
    import engine as engine_mod  # type: ignore
    import store  # type: ignore

# --------------------------------------------------------------------------- #
# constants / paths
# --------------------------------------------------------------------------- #
VERSION = "1.0.0"
ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"
AGENT_DIR = ROOT_DIR / "agent"
DATA_DIR = ROOT_DIR / "data"

MAX_CTX = engine_mod.MAX_CTX
logger = logging.getLogger("localbench")
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
ZERO_GPU_BLOCK = {
    "available": False,
    "idle": {"vram_mb": 0, "ram_mb": 0, "gpu_util": 0, "power_w": 0, "temp_c": 0},
    "peak": {"vram_mb": 0, "ram_mb": 0, "gpu_util": 0, "power_w": 0, "temp_c": 0},
    "avg": {"vram_mb": 0, "ram_mb": 0, "gpu_util": 0, "power_w": 0, "temp_c": 0},
    "samples_n": 0,
}
SSE_PING_SECONDS = 10.0
TELEMETRY_BROADCAST_SECONDS = 0.25  # ~4/s max, per CONTRACT.md
STARTED_AT = time.time()


def _max_runs() -> int:
    try:
        value = int(os.environ.get("LOCALBENCH_MAX_RUNS", "2"))
    except (TypeError, ValueError):
        value = 2
    return max(1, min(8, value))


MAX_CONCURRENT_RUNS = _max_runs()


# --------------------------------------------------------------------------- #
# pydantic request models
# --------------------------------------------------------------------------- #
class DiscoverBody(BaseModel):
    base_url: str
    api_key: Optional[str] = None


class RunConfigModel(BaseModel):
    contexts: List[int] = Field(default_factory=lambda: list(engine_mod.DEFAULT_CONFIG["contexts"]))
    concurrency: List[int] = Field(
        default_factory=lambda: list(engine_mod.DEFAULT_CONFIG["concurrency"])
    )
    concurrency_context: int = 2048
    max_tokens: int = 128
    repeats: int = 1
    warmup: int = 1
    idle_sample_s: float = 5
    enable_context_ladder: bool = True
    enable_concurrency: bool = True
    request_timeout_s: float = 600

    model_config = {"extra": "ignore"}


class CreateRunBody(BaseModel):
    # Either base_url+model (+api_key) or preset_id alone (server-side credential
    # preset from data/presets.json).
    base_url: Optional[str] = None
    model: Optional[str] = None
    label: Optional[str] = None
    api_key: Optional[str] = None
    preset_id: Optional[str] = None
    config: Optional[RunConfigModel] = None
    telemetry: Optional[Dict[str, Any]] = None

    model_config = {"extra": "ignore"}


class TelemetryBody(BaseModel):
    samples: Optional[List[Dict[str, Any]]] = None
    model_load_s: Optional[float] = None

    model_config = {"extra": "ignore"}


# --------------------------------------------------------------------------- #
# server-credential presets (data/presets.json)
# --------------------------------------------------------------------------- #
def data_dir() -> Path:
    """``data/`` dir; ``LOCALBENCH_DATA_DIR`` keeps test runs hermetic."""
    override = os.environ.get("LOCALBENCH_DATA_DIR")
    return Path(override) if override else DATA_DIR


def presets_path() -> Path:
    override = os.environ.get("LOCALBENCH_PRESETS")
    if override:
        return Path(override)
    return data_dir() / "presets.json"


def slugify(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return text or "preset"


def load_presets(include_secrets: bool = False) -> List[dict]:
    """Read ``data/presets.json`` fresh on every call (edits need no restart).

    With ``include_secrets=False`` the private ``api_key`` never leaves this
    function; callers only learn ``has_credential``.
    """
    path = presets_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("presets") or []
    if not isinstance(data, list):
        return []
    presets: List[dict] = []
    seen = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("id") or "preset").strip() or "preset"
        preset_id = str(entry.get("id") or slugify(name)).strip() or slugify(name)
        base_id = preset_id
        suffix = 2
        while preset_id in seen:
            preset_id = "%s-%d" % (base_id, suffix)
            suffix += 1
        seen.add(preset_id)
        raw_key = entry.get("api_key")
        api_key = raw_key.strip() if isinstance(raw_key, str) else ""
        preset = {
            "id": preset_id,
            "name": name,
            "base_url": str(entry.get("base_url") or "").strip(),
            "model": str(entry.get("model") or "").strip(),
            "note": str(entry.get("note") or ""),
            "has_credential": bool(api_key),
        }
        if include_secrets and api_key:
            preset["api_key"] = api_key
        presets.append(preset)
    return presets


def check_presets_security() -> None:
    """Warn (never crash) when the private preset file is too permissive."""
    path = presets_path()
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return
    secrets = [p for p in load_presets(include_secrets=True) if p.get("api_key")]
    if not secrets:
        return
    if mode & 0o044:
        logger.warning(
            "presets file %s (mode %o) holds %d server-side api_key(s) but is "
            "group/world readable - chmod 600 it",
            path,
            mode,
            len(secrets),
        )
    else:
        logger.info(
            "loaded %d server-credential preset(s) from %s (mode %o)",
            len(secrets),
            path,
            mode,
        )


# --------------------------------------------------------------------------- #
# per-IP rate limit on run creation
# --------------------------------------------------------------------------- #
def _parse_rate_limit(raw: Optional[str]):
    """``"6/600"`` -> (6, 600).  A zero run count disables the limiter."""
    default = (6, 600)
    if not raw:
        return default
    text = str(raw).strip()
    try:
        if "/" in text:
            runs_text, window_text = text.split("/", 1)
            runs, window = int(float(runs_text)), int(float(window_text))
        else:
            runs, window = int(float(text)), 600
    except (TypeError, ValueError):
        return default
    if runs < 0 or window <= 0:
        return default
    return runs, window


class RateLimiter:
    """Sliding-window per-IP limiter for ``POST /api/runs`` (in memory).

    ``LOCALBENCH_RATE_LIMIT`` is re-read per request, so ops/tests can change or
    disable it ("0/600") without a restart.
    """

    def __init__(self, spec: Optional[str] = None):
        self._spec = spec if spec is not None else os.environ.get("LOCALBENCH_RATE_LIMIT")
        self.limit, self.window = _parse_rate_limit(self._spec)
        self._hits: Dict[str, deque] = {}
        #: cumulative count of rejected requests (exposed in /api/stats)
        self.limited_total = 0

    def _sync(self) -> None:
        spec = os.environ.get("LOCALBENCH_RATE_LIMIT")
        if spec != self._spec:
            self._spec = spec
            self.limit, self.window = _parse_rate_limit(spec)
            self._hits.clear()

    def retry_after(self, ip: str) -> Optional[int]:
        self._sync()
        if self.limit <= 0:
            return None
        now = time.time()
        hits = self._hits.get(ip)
        if not hits:
            return None
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if not hits:
            self._hits.pop(ip, None)
            return None
        if len(hits) >= self.limit:
            return max(1, int(math.ceil(hits[0] + self.window - now)))
        return None

    def record(self, ip: str) -> None:
        self._sync()
        if self.limit <= 0:
            return
        self._hits.setdefault(ip, deque()).append(time.time())

    def note_limited(self) -> None:
        """Count one rejected request."""
        self.limited_total += 1

    def snapshot(self) -> dict:
        """Live spec for ``GET /api/config`` (the counter lives in /api/stats)."""
        self._sync()
        return {"runs": self.limit, "window_s": self.window}


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(str(value))
        return True
    except ValueError:
        return False


def is_loopback(value: str) -> bool:
    """True for 127.0.0.0/8, ::1 and IPv4-mapped loopback (operator access)."""
    text = str(value or "").strip()
    if not text:
        return False
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return text.lower() in ("localhost", "localhost.localdomain")
    if addr.is_loopback:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def client_ip(request: Request) -> str:
    """Client IP, honouring nginx: ``X-Forwarded-For`` first hop."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    real = request.headers.get("x-real-ip")
    if real and real.strip():
        return real.strip()
    return request.client.host if request.client else "unknown"


rate_limiter = RateLimiter()


# --------------------------------------------------------------------------- #
# run state
# --------------------------------------------------------------------------- #
class RunState:
    """Everything about one run that must stay in memory."""

    def __init__(
        self,
        run: dict,
        api_key: str = "",
        agent_key: str = "",
        base_url: str = "",
        access_token: str = "",
    ):
        self.run = run
        self.api_key = api_key or ""
        self.agent_key = agent_key or ""
        self.base_url = base_url or ""
        #: per-run secret the browser must present on every run-scoped call.
        self.access_token = access_token or ""
        self.last_activity = time.time()
        self.samples: list = []
        self.subscribers: set = set()
        self.cancel_event = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        self.agent_connected = False
        self.model_load_s: Optional[float] = None
        self.phase_hint = "idle"
        self.idle_until = 0.0
        self.deleted = False
        self.last_telemetry_broadcast = 0.0

    # -- helpers ----------------------------------------------------------- #
    @property
    def status(self) -> str:
        return self.run.get("status") or "queued"

    def public_run(self) -> dict:
        """The Run document as sent to clients (agent key never included)."""
        doc = copy.deepcopy(self.run)
        agent = doc.get("agent") if isinstance(doc.get("agent"), dict) else {}
        agent["key"] = None
        doc["agent"] = {"key": None, "connected": bool(self.agent_connected)}
        return doc


def _empty_results() -> dict:
    return {
        "ttft_ms": dict(engine_mod.EMPTY_AGG),
        "decode_tps": dict(engine_mod.EMPTY_AGG),
        "prompt_tps": dict(engine_mod.EMPTY_AGG),
        "total_s": dict(engine_mod.EMPTY_AGG),
        "tokens": {"prompt": 0, "completion": 0},
        "context_points": [],
        "concurrency_points": [],
        "gpu": copy.deepcopy(ZERO_GPU_BLOCK),
        "model_load_s": None,
        "notes": [],
    }


def _initial_progress() -> dict:
    return {"phase": "overhead", "step": 0, "total": 0, "pct": 0, "label": "queued"}


# --------------------------------------------------------------------------- #
# run manager (FIFO queue + workers + SSE fan-out)
# --------------------------------------------------------------------------- #
class RunManager:
    def __init__(self, concurrency: int = MAX_CONCURRENT_RUNS):
        self.worker_count = concurrency
        self.states: Dict[str, RunState] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.workers: List[asyncio.Task] = []
        self._started = False

    # -- lifecycle --------------------------------------------------------- #
    def ensure_started(self) -> None:
        """Start worker tasks; safe to call from a route handler."""
        if self._started:
            return
        self._started = True
        for index in range(self.worker_count):
            self.workers.append(
                asyncio.create_task(self._worker(index), name="localbench-worker-%d" % index)
            )

    async def start(self) -> None:
        self.ensure_started()

    async def stop(self) -> None:
        for worker in self.workers:
            worker.cancel()
        if self.workers:
            await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers = []
        self._started = False

    # -- state access ------------------------------------------------------ #
    def get_state(self, run_id: str, create: bool = False) -> Optional[RunState]:
        state = self.states.get(run_id)
        if state is not None:
            if self._ttl_expired(state):
                self.drop(run_id)
                return None
            return state
        if not create:
            return None
        run = store.get_run(run_id)
        if run is None:
            return None
        # Runs reloaded from a persistence backend have no access token any
        # more (tokens are memory-only), so they can never be authorised.
        state = RunState(run, api_key="", agent_key=store.get_agent_key(run_id) or "")
        state.agent_connected = bool((run.get("agent") or {}).get("connected"))
        self.states[run_id] = state
        return state

    @staticmethod
    def _ttl_expired(state: "RunState", now: Optional[float] = None) -> bool:
        ttl = store.ttl_seconds()
        if ttl <= 0 or state.status not in TERMINAL_STATUSES:
            return False
        stamp = state.run.get("finished_at") or state.run.get("created_at") or 0
        if not stamp:
            return False
        return (now if now is not None else time.time()) - float(stamp) > ttl

    def drop(self, run_id: str) -> None:
        """Forget a run: state, telemetry samples and stored copy.

        Attached SSE streams are closed with a final ``done`` frame first.
        """
        state = self.states.pop(run_id, None)
        if state is not None:
            for queue in list(state.subscribers):
                try:
                    queue.put_nowait({"type": "done", "run": state.public_run()})
                except asyncio.QueueFull:  # pragma: no cover
                    pass
            state.subscribers.clear()
        try:
            store.evict_run(run_id)
        except Exception:  # pragma: no cover - eviction must never break a request
            pass

    def sweep(self, now: Optional[float] = None) -> int:
        """TTL housekeeping: finished runs age out, abandoned queued runs die.

        Running runs are never evicted mid-flight.
        """
        now = now or time.time()
        ttl = store.ttl_seconds()
        if ttl <= 0:
            return 0
        evicted = 0
        for run_id, state in list(self.states.items()):
            if self._ttl_expired(state, now):
                self.drop(run_id)
                evicted += 1
                continue
            if state.status == "queued" and (now - (state.last_activity or now)) > ttl:
                # queued but nobody ever attached a client: abandon it
                state.cancel_event.set()
                state.run["status"] = "cancelled"
                _add_note(state.run, "queued run abandoned by client")
                _finish_run(state)
                self.drop(run_id)
                evicted += 1
        for run_id in store.evict_expired():
            self.states.pop(run_id, None)
        if evicted:
            logger.info("ttl: evicted %d run(s)", evicted)
        return evicted

    def create(
        self,
        run: dict,
        api_key: str = "",
        agent_key: str = "",
        base_url: str = "",
        access_token: str = "",
    ) -> RunState:
        state = RunState(
            run,
            api_key=api_key,
            agent_key=agent_key,
            base_url=base_url,
            access_token=access_token,
        )
        self.states[run["id"]] = state
        return state

    def enqueue(self, run_id: str) -> None:
        self.ensure_started()
        self.queue.put_nowait(run_id)

    # -- SSE fan-out ------------------------------------------------------- #
    def broadcast(self, state: RunState, event: dict) -> None:
        state.last_activity = time.time()
        for queue in list(state.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def event_stream(self, state: RunState):
        """Async generator of SSE frames: snapshot first, done last."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
        state.subscribers.add(queue)
        state.last_activity = time.time()
        try:
            yield _sse_frame({"type": "snapshot", "run": state.public_run()})
            if state.status in TERMINAL_STATUSES:
                yield _sse_frame({"type": "done", "run": state.public_run()})
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_PING_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse_frame(event)
                if event.get("type") == "done":
                    return
                if state.status in TERMINAL_STATUSES:
                    # Terminal but the done frame was missed (e.g. deleted run).
                    yield _sse_frame({"type": "done", "run": state.public_run()})
                    return
        finally:
            state.subscribers.discard(queue)

    # -- worker loop ------------------------------------------------------- #
    async def _worker(self, index: int) -> None:
        while True:
            run_id = await self.queue.get()
            try:
                state = self.states.get(run_id)
                if state is None or state.status != "queued":
                    continue
                state.task = asyncio.create_task(self._execute(state))
                try:
                    await state.task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # pragma: no cover - defensive
                    _finalize_failure(state, exc)
            finally:
                self.queue.task_done()

    # -- execution --------------------------------------------------------- #
    async def _execute(self, state: RunState) -> None:
        run = state.run
        run["status"] = "running"
        run["started_at"] = time.time()
        run["progress"] = {
            "phase": "overhead",
            "step": 0,
            "total": run["progress"].get("total") or 0,
            "pct": 0,
            "label": "starting",
        }
        store.save_run(run, agent_key=state.agent_key)
        self.broadcast(state, {"type": "progress", "progress": run["progress"]})

        try:
            await self._idle_baseline(state)
            engine = engine_mod.Engine(
                state.base_url,
                state.api_key,
                run["model"],
                run["config"],
                results=run["results"],
                on_progress=_progress_hook(self, state),
                on_overhead=lambda overhead: state.run.__setitem__("overhead", overhead),
                on_point=_point_hook(self, state),
                on_log=_log_hook(self, state),
                should_cancel=lambda: state.cancel_event.is_set(),
            )
            await engine.run()
            run["overhead"] = engine.overhead
            attempted = (run["results"].get("context_points") or []) + (
                run["results"].get("concurrency_points") or []
            )
            if attempted and not any(p.get("ok") for p in attempted):
                raise engine_mod.EngineError("all measurement points failed")
            run["status"] = "done"
        except engine_mod.RunCancelled:
            run["status"] = "cancelled"
            _add_note(run, "run cancelled by user")
        except asyncio.CancelledError:
            run["status"] = "cancelled"
            _add_note(run, "run cancelled by user")
        except engine_mod.EngineError as exc:
            run["status"] = "failed"
            run["error"] = exc.message
        except Exception as exc:  # pragma: no cover - defensive
            run["status"] = "failed"
            run["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        finally:
            _finish_run(state)
            if not state.deleted:
                try:
                    store.save_run(state.run, agent_key=state.agent_key)
                except Exception:  # pragma: no cover - shutdown races
                    pass
            self.broadcast(state, {"type": "done", "run": state.public_run()})

    async def _idle_baseline(self, state: RunState) -> None:
        seconds = float(state.run["config"].get("idle_sample_s") or 0)
        if seconds <= 0 or not state.agent_connected:
            return
        state.phase_hint = "idle"
        state.idle_until = time.time() + seconds
        progress = {
            "phase": "overhead",
            "step": 0,
            "total": state.run["progress"].get("total") or 0,
            "pct": 0,
            "label": "idle baseline %ds" % int(round(seconds)),
        }
        state.run["progress"] = progress
        self.broadcast(state, {"type": "progress", "progress": progress})
        await asyncio.sleep(seconds)
        state.idle_until = 0.0


def _sse_frame(event: dict) -> str:
    return "data: %s\n\n" % json.dumps(event, separators=(",", ":"), ensure_ascii=False)


def _add_note(run: dict, note: str) -> None:
    results = run.setdefault("results", {})
    notes = results.get("notes")
    if not isinstance(notes, list):
        notes = []
        results["notes"] = notes
    if note not in notes:
        notes.append(note)


GPU_NOTE = "GPU telemetry: no agent connected"


def _sync_gpu_notes(run: dict, gpu: dict) -> None:
    """Keep results.notes truthful when telemetry arrives late."""
    results = run.setdefault("results", {})
    notes = results.get("notes")
    if not isinstance(notes, list):
        notes = []
        results["notes"] = notes
    if gpu.get("available"):
        while GPU_NOTE in notes:
            notes.remove(GPU_NOTE)
    elif GPU_NOTE not in notes:
        notes.append(GPU_NOTE)


def _finish_run(state: RunState) -> None:
    """Finalise GPU aggregates / status / timestamps (always runs)."""
    run = state.run
    results = run.setdefault("results", {})
    gpu = agent_helpers.aggregate_gpu(state.samples)
    results["gpu"] = gpu
    _sync_gpu_notes(run, gpu)
    if gpu.get("available"):
        idle_sampled = any(
            str(s.get("phase") or "").startswith("idle") for s in state.samples
        )
        if not idle_sampled:
            _add_note(run, "idle baseline not sampled (agent attached after run start)")
    if state.model_load_s is not None:
        results["model_load_s"] = round(float(state.model_load_s), 3)
    else:
        results.setdefault("model_load_s", None)
    run["agent"] = {"key": None, "connected": bool(state.agent_connected)}
    if run.get("status") not in TERMINAL_STATUSES:
        run["status"] = "failed"
        run["error"] = run.get("error") or "run ended unexpectedly"
    # The api_key only lives in memory for the duration of the run.
    state.api_key = ""
    run["finished_at"] = time.time()
    total = (run.get("progress") or {}).get("total") or 0
    run["progress"] = {
        "phase": "finish",
        "step": total,
        "total": total,
        "pct": 100,
        "label": run["status"],
    }
    state.phase_hint = "finish"


def _finalize_failure(state: RunState, exc: BaseException) -> None:
    state.run["status"] = "failed"
    state.run["error"] = "%s: %s" % (exc.__class__.__name__, exc)
    _finish_run(state)
    try:
        store.save_run(state.run, agent_key=state.agent_key)
    except Exception:  # pragma: no cover
        pass
    try:
        manager.broadcast(state, {"type": "done", "run": state.public_run()})
    except Exception:  # pragma: no cover
        pass


def _progress_hook(manager_ref: "RunManager", state: RunState):
    def hook(progress: dict) -> None:
        state.run["progress"] = progress
        phase = progress.get("phase") or ""
        label = progress.get("label") or ""
        hint = phase
        if phase == "context":
            match = re.search(r"context (\d+)", label)
            hint = "context:%s" % match.group(1) if match else "context"
        elif phase == "concurrency":
            match = re.search(r"n=(\d+)", label)
            hint = "concurrency:%s" % match.group(1) if match else "concurrency"
        state.phase_hint = hint
        manager_ref.broadcast(state, {"type": "progress", "progress": progress})

    return hook


def _point_hook(manager_ref: "RunManager", state: RunState):
    def hook(kind: str, point: dict) -> None:
        if not state.deleted:
            try:
                store.save_run(state.run, agent_key=state.agent_key)
            except Exception:  # pragma: no cover - persistence must not kill a run
                pass
        manager_ref.broadcast(state, {"type": "point", "kind": kind, "point": point})

    return hook


def _log_hook(manager_ref: "RunManager", state: RunState):
    def hook(level: str, message: str) -> None:
        manager_ref.broadcast(
            state, {"type": "log", "level": level or "info", "message": str(message)}
        )

    return hook


manager = RunManager()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def _not_found(run_id: str = "") -> JSONResponse:
    """404 for unknown runs AND for missing/wrong run tokens (same body)."""
    return JSONResponse({"ok": False, "error": "run not found"}, status_code=404)


def _presented_token(request: Request) -> str:
    """Run token from ``X-Run-Token`` or, for EventSource, ``?token=``."""
    header = request.headers.get("x-run-token")
    if header:
        return header.strip()
    return (request.query_params.get("token") or "").strip()


def _authorized_state(run_id: str, request: Request) -> Optional["RunState"]:
    """The run state if the caller presents its token, else ``None`` (404)."""
    state = manager.get_state(run_id, create=True)
    if state is None:
        return None
    token = _presented_token(request)
    if not token or not state.access_token:
        return None
    try:
        if not secrets.compare_digest(token, state.access_token):
            return None
    except TypeError:  # pragma: no cover - non-ascii token
        return None
    return state


def _server_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return "%s://%s" % (proto.split(",")[0].strip(), host.split(",")[0].strip())


def _normalize_models(payload: Any) -> List[dict]:
    entries = []
    if isinstance(payload, dict):
        entries = payload.get("data") or payload.get("models") or []
    elif isinstance(payload, list):
        entries = payload
    if not isinstance(entries, list):
        return []
    models = []
    for entry in entries:
        if isinstance(entry, str):
            models.append({"id": entry, "context_length": None, "owned_by": "local"})
            continue
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id") or entry.get("name") or entry.get("model")
        if not model_id:
            continue
        models.append(
            {
                "id": str(model_id),
                "context_length": engine_mod._context_length_from_model(entry),
                "owned_by": str(entry.get("owned_by") or entry.get("ownedBy") or "local"),
            }
        )
    return models


# --------------------------------------------------------------------------- #
# app / lifespan
# --------------------------------------------------------------------------- #
def _setup_logging() -> None:
    """Give our own logger a handler so INFO/WARNING lines reach the unit log."""
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     [localbench] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _install_log_redaction() -> None:
    """Never let an access token reach journald/stdout via access logs.

    EventSource cannot send headers, so ``/events`` carries ``?token=…`` in the
    query string; uvicorn's access log would otherwise print it.
    """
    if getattr(_install_log_redaction, "_done", False):
        return
    _install_log_redaction._done = True  # type: ignore[attr-defined]

    class _RedactQuery(logging.Filter):
        _pattern = re.compile(r"(token|key|api_key)=[^&\s\"']+", re.IGNORECASE)

        def filter(self, record: logging.LogRecord) -> bool:
            try:
                if isinstance(record.args, tuple) and record.args:
                    record.args = tuple(
                        self._pattern.sub(r"\1=REDACTED", arg)
                        if isinstance(arg, str) and self._pattern.search(arg)
                        else arg
                        for arg in record.args
                    )
                elif isinstance(record.msg, str) and self._pattern.search(record.msg):
                    record.msg = self._pattern.sub(r"\1=REDACTED", record.msg)
            except Exception:  # pragma: no cover - logging must never raise
                pass
            return True

    redactor = _RedactQuery()
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        logging.getLogger(name).addFilter(redactor)
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(redactor)
    for handler in logger.handlers:
        handler.addFilter(redactor)


async def _ttl_sweeper() -> None:
    """Periodic TTL eviction (see CONTRACT.md "Storage model")."""
    interval = store.sweep_seconds()
    while True:
        try:
            await asyncio.sleep(interval)
            manager.sweep()
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:  # pragma: no cover - housekeeping must never crash
            logger.warning("ttl sweep failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    _install_log_redaction()
    store.init_db()
    logger.info(store.storage_summary())
    if store.persist_enabled():
        _recover_interrupted_runs()
    check_presets_security()
    await manager.start()
    sweeper = asyncio.create_task(_ttl_sweeper(), name="localbench-ttl-sweeper")
    try:
        yield
    finally:
        sweeper.cancel()
        try:
            await sweeper
        except (asyncio.CancelledError, Exception):
            pass
        await manager.stop()


def _recover_interrupted_runs() -> None:
    """Mark runs orphaned by a server restart as failed (honest status)."""
    try:
        for summary in store.list_runs(limit=500):
            if summary.get("status") in ("queued", "running"):
                run = store.get_run(summary["id"])
                if not run:
                    continue
                run["status"] = "failed"
                run["error"] = "interrupted by server restart"
                run["finished_at"] = run.get("finished_at") or time.time()
                _add_note(run, "run interrupted by server restart")
                store.save_run(run, agent_key=store.get_agent_key(run["id"]))
    except Exception:  # pragma: no cover - startup must not fail on a bad db
        pass


app = FastAPI(title="LocalBench", version=VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    """Keep the ``{ok:false,error}`` envelope the frontend expects."""
    try:
        detail = json.loads(json.dumps(exc.errors(), default=str))
    except Exception:  # pragma: no cover
        detail = str(exc.errors())
    message = "; ".join(
        "%s: %s" % (".".join(str(p) for p in err.get("loc", [])), err.get("msg", ""))
        for err in exc.errors()
    )
    return JSONResponse(
        {"ok": False, "error": message or "invalid request body", "detail": detail},
        status_code=422,
    )


# --------------------------------------------------------------------------- #
# health / config / presets
# --------------------------------------------------------------------------- #
@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "runs": store.count_runs(),
        "uptime_s": round(time.time() - STARTED_AT, 1),
    }


@app.get("/api/config")
async def get_config():
    return {
        "ok": True,
        "version": VERSION,
        "gpu_agent_enabled": True,
        "limits": {
            "max_concurrent_runs": manager.worker_count,
            "max_ctx": MAX_CTX,
            "rate_limit": rate_limiter.snapshot(),
        },
    }


@app.get("/api/presets")
async def get_presets():
    """Public preset list: never includes the server-side api_key."""
    return {"ok": True, "presets": load_presets(include_secrets=False)}


# --------------------------------------------------------------------------- #
# discover
# --------------------------------------------------------------------------- #
@app.post("/api/discover")
async def discover(body: DiscoverBody):
    try:
        base_url = engine_mod.validate_base_url(body.base_url)
    except ValueError as exc:
        return _error(str(exc), 400)
    headers = {"Accept": "application/json"}
    if body.api_key:
        headers["Authorization"] = "Bearer " + body.api_key
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(base_url + "/models", headers=headers)
    except httpx.HTTPError as exc:
        return _error("upstream request failed: %s: %s" % (exc.__class__.__name__, exc), 502)
    latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
    if response.status_code >= 400:
        return _error(
            "upstream returned HTTP %s for %s/models" % (response.status_code, base_url), 502
        )
    try:
        payload = response.json()
    except ValueError:
        return _error("upstream returned invalid JSON from /models", 502)
    return {"ok": True, "models": _normalize_models(payload), "latency_ms": latency_ms}


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #
@app.post("/api/runs")
async def create_run(body: CreateRunBody, request: Request):
    ip = client_ip(request)
    # Per-IP limiting needs an IP, and never applies to the operator on loopback
    # (tunnel/local MTP sessions). An in-process ASGI client (tests) has no IP.
    limited = is_ip(ip) and not is_loopback(ip)
    if limited:
        retry_after = rate_limiter.retry_after(ip)
        if retry_after is not None:
            rate_limiter.note_limited()
            logger.info(
                "run rejected: rate_limited ip=%s retry_after=%d", ip, retry_after
            )
            return JSONResponse(
                {"ok": False, "error": "rate_limited", "retry_after": retry_after},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

    # --- resolve endpoint: explicit fields win, else a server-credential preset
    preset = None
    if body.preset_id:
        preset = next(
            (p for p in load_presets(include_secrets=True) if p["id"] == body.preset_id),
            None,
        )
        if preset is None:
            return _error("unknown preset_id: %s" % body.preset_id, 404)

    base_url_raw = (body.base_url or (preset or {}).get("base_url") or "").strip()
    model = (body.model or (preset or {}).get("model") or "").strip()
    # The resolved key stays in memory only; it is never echoed or persisted.
    api_key = body.api_key or (preset or {}).get("api_key") or ""

    if not base_url_raw:
        return _error("base_url is required (or use preset_id)", 400)
    try:
        base_url = engine_mod.validate_base_url(base_url_raw)
    except ValueError as exc:
        return _error(str(exc), 400)
    if not model:
        return _error("model is required (or use preset_id)", 400)

    config = engine_mod.normalize_config(
        body.config.model_dump() if body.config is not None else None
    )
    run_id = agent_helpers.new_run_id()
    while store.get_run(run_id) is not None:  # pragma: no cover - collision guard
        run_id = agent_helpers.new_run_id()
    agent_key = agent_helpers.new_agent_key()
    #: Per-run secret; memory only, never logged, never persisted.
    access_token = "rt_" + secrets.token_urlsafe(18)
    label = (body.label or model).strip()[:200] or model
    now = time.time()

    run = {
        "id": run_id,
        "label": label,
        "model": model,
        "endpoint_host": engine_mod.endpoint_host(base_url),
        "preset_id": body.preset_id or None,
        "status": "queued",
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "config": config,
        "progress": _initial_progress(),
        "overhead": None,
        "results": _empty_results(),
        "agent": {"key": None, "connected": False},
    }
    manager.create(
        run,
        api_key=api_key,
        agent_key=agent_key,
        base_url=base_url,
        access_token=access_token,
    )
    store.create_run(run, agent_key=agent_key)
    manager.enqueue(run_id)
    if limited:
        rate_limiter.record(ip)
    logger.info("run %s queued", run_id)
    return {"ok": True, "run_id": run_id, "access_token": access_token}


@app.get("/api/stats")
async def get_stats():
    """Aggregate counters only — no ids, labels or endpoints (privacy)."""
    counters = store.stats()
    return {
        "ok": True,
        "active": int(counters.get("active") or 0),
        "finished": int(counters.get("finished") or 0),
        "storage": store.backend_name(),
        "ttl_s": int(store.ttl_seconds()),
        # additive: abuse-guard counter (no ids, labels or endpoints)
        "rate_limited_total": int(rate_limiter.limited_total),
    }


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    state = _authorized_state(run_id, request)
    if state is None:
        return _not_found(run_id)
    return {"ok": True, "run": state.public_run()}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request):
    # Token via header or ?token= (EventSource cannot set headers). A finished
    # run that is still in memory replays snapshot -> done and the stream closes.
    state = _authorized_state(run_id, request)
    if state is None:
        return _not_found(run_id)
    return StreamingResponse(
        manager.event_stream(state),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request):
    state = _authorized_state(run_id, request)
    if state is None:
        return _not_found(run_id)
    if state.status in TERMINAL_STATUSES:
        return {"ok": True, "status": state.status}
    state.cancel_event.set()
    if state.task is not None and not state.task.done():
        state.task.cancel()
    elif state.status == "queued":
        # Still in the queue: settle it now, the worker will skip it.
        state.run["status"] = "cancelled"
        _add_note(state.run, "run cancelled by user")
        _finish_run(state)
        if not state.deleted:
            store.save_run(state.run, agent_key=state.agent_key)
        manager.broadcast(state, {"type": "done", "run": state.public_run()})
        return {"ok": True, "status": "cancelled"}
    # Running: the task unwinds asynchronously and emits its own done event.
    return {"ok": True}


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str, request: Request):
    state = _authorized_state(run_id, request)
    if state is None:
        return _not_found(run_id)
    state.deleted = True
    state.cancel_event.set()
    if state.task is not None and not state.task.done():
        state.task.cancel()
    manager.drop(run_id)
    logger.info("run %s deleted by its owner", run_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# telemetry ingest (agent)
# --------------------------------------------------------------------------- #
@app.post("/api/runs/{run_id}/telemetry")
async def ingest_telemetry(
    run_id: str,
    body: TelemetryBody,
    x_agent_key: Optional[str] = Header(default=None, alias="X-Agent-Key"),
):
    state = manager.get_state(run_id, create=True)
    if state is None:
        return _not_found(run_id)
    if not x_agent_key or x_agent_key != state.agent_key:
        return _error("invalid agent key", 403)

    now = time.time()
    received = 0
    if body.samples:
        for raw in body.samples[:512]:
            sample = agent_helpers.normalize_sample(raw, phase_default=state.phase_hint or "idle")
            if sample is None:
                continue
            state.samples.append(sample)
            received += 1
        if len(state.samples) > store.MAX_SAMPLES:
            state.samples = agent_helpers.downsample(state.samples, store.MAX_SAMPLES)
        if not state.agent_connected:
            state.agent_connected = True
            state.run.setdefault("agent", {})["connected"] = True
            store.set_agent_connected(run_id, True)
            manager.broadcast(
                state,
                {"type": "log", "level": "info", "message": "GPU telemetry agent connected"},
            )
        # Thinned broadcast: ~4 events/s max.
        if state.samples and now - state.last_telemetry_broadcast >= TELEMETRY_BROADCAST_SECONDS:
            state.last_telemetry_broadcast = now
            manager.broadcast(state, {"type": "telemetry", "sample": state.samples[-1]})

    if body.model_load_s is not None:
        state.model_load_s = float(body.model_load_s)
        state.run.setdefault("results", {})["model_load_s"] = round(
            float(body.model_load_s), 3
        )

    # Late flush after the run already finished: refresh GPU aggregates.
    if state.status in TERMINAL_STATUSES and not state.deleted:
        gpu = agent_helpers.aggregate_gpu(state.samples)
        state.run["results"]["gpu"] = gpu
        _sync_gpu_notes(state.run, gpu)
        if state.model_load_s is not None:
            state.run["results"]["model_load_s"] = round(float(state.model_load_s), 3)
        try:
            store.save_run(state.run, agent_key=state.agent_key)
        except Exception:  # pragma: no cover
            pass

    return {
        "ok": True,
        "status": state.status,
        "phase": state.phase_hint,
        "received": received,
        "samples": len(state.samples),
    }


@app.get("/api/runs/{run_id}/agent")
async def get_agent(run_id: str, request: Request):
    state = _authorized_state(run_id, request)
    if state is None:
        return _not_found(run_id)
    server = _server_url(request)
    return {
        "ok": True,
        "agent_key": state.agent_key,
        "command": agent_helpers.agent_command(server, run_id, state.agent_key),
        "agent_connected": bool(state.agent_connected),
        # additive: lets the UI hide the attach box for a finished run
        "status": state.status,
        "run_finished": state.status in TERMINAL_STATUSES,
    }


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
@app.get("/api/runs/{run_id}/export")
async def export_run(run_id: str, request: Request, format: str = Query("json")):
    state = _authorized_state(run_id, request)
    if state is None:
        return _not_found(run_id)
    run = state.public_run()
    fmt = (format or "json").lower()
    if fmt not in ("json", "csv"):
        return _error("format must be json or csv", 400)
    if fmt == "csv":
        content = store.export_csv(run)
        media_type = "text/csv; charset=utf-8"
        filename = "%s.csv" % run_id
    else:
        content = store.export_json(run)
        media_type = "application/json"
        filename = "%s.json" % run_id
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": 'attachment; filename="%s"' % filename,
            "Cache-Control": "no-store",
        },
    )


# --------------------------------------------------------------------------- #
# static: agent script + frontend SPA
# --------------------------------------------------------------------------- #
@app.get("/bench_agent.py", include_in_schema=False)
async def serve_agent_script():
    path = AGENT_DIR / "bench_agent.py"
    if not path.is_file():
        return JSONResponse({"ok": False, "error": "agent script missing"}, status_code=404)
    return FileResponse(path, media_type="text/x-python", filename="bench_agent.py")


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    """Static files from ``web/`` with SPA fallback to ``index.html``."""
    if full_path.startswith("api/") or full_path == "healthz":
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if full_path:
        candidate = (WEB_DIR / full_path).resolve()
        try:
            inside = candidate == WEB_DIR or WEB_DIR in candidate.parents
        except Exception:  # pragma: no cover
            inside = False
        if inside and candidate.is_file():
            return FileResponse(candidate)
    index = WEB_DIR / "index.html"
    if index.is_file():
        return FileResponse(index, media_type="text/html; charset=utf-8")
    return JSONResponse(
        {"ok": False, "error": "frontend not built yet"}, status_code=404
    )


# --------------------------------------------------------------------------- #
# __main__ convenience: ``python -m backend.app``
# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "backend.app:app",
        host="127.0.0.1",
        port=int(os.environ.get("LOCALBENCH_PORT", "21700")),
        log_level="info",
    )
