"""Run storage for LocalBench.

Default (**``LOCALBENCH_PERSIST=0``**): *memory only* — no benchmark data ever
touches the server disk.  Runs, telemetry batches and agent keys live in RAM and
are freed ``LOCALBENCH_TTL_S`` seconds (default 900) after the run reaches a
terminal status.  This is the privacy model in CONTRACT.md: history/compare live
in the visitor's browser (IndexedDB), the operator's server stores nothing.

``LOCALBENCH_PERSIST=1`` (test suite / debugging only) keeps the legacy sqlite
path at ``LOCALBENCH_DB``.  Setting ``LOCALBENCH_DB`` explicitly also opts into
sqlite so a hermetic test run cannot silently become memory-only.

Secret hygiene: every write runs :func:`strip_secrets`, so an ``api_key`` can
never be stored even if a caller passes one by accident.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

#: Default sqlite location (only used when persistence is enabled).
DEFAULT_DB_PATH = "/opt/localbench/data/runs.db"
DB_PATH = os.environ.get("LOCALBENCH_DB") or DEFAULT_DB_PATH

#: Default run lifetime in memory after a terminal status.
DEFAULT_TTL_S = 900.0

#: Telemetry samples kept per run (CONTRACT.md).
MAX_SAMPLES = 4000

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "authorization",
        "x-agent-key",
        "x-run-token",
        "access_token",
        "secret",
        "token",
        "password",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id               TEXT PRIMARY KEY,
    created_at       REAL NOT NULL,
    started_at       REAL,
    finished_at      REAL,
    label            TEXT,
    model            TEXT,
    endpoint_host    TEXT,
    status           TEXT NOT NULL,
    config_json      TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    agent_key        TEXT,
    agent_connected  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
"""


# --------------------------------------------------------------------------- #
# mode / TTL configuration (read lazily so tests and ops can flip it)
# --------------------------------------------------------------------------- #
def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def persist_enabled() -> bool:
    """True when sqlite persistence is on (tests / debugging only)."""
    raw = os.environ.get("LOCALBENCH_PERSIST")
    if raw is not None and str(raw).strip() != "":
        return _truthy(raw)
    # An explicit DB path means the caller wants sqlite (hermetic test suite).
    return bool(os.environ.get("LOCALBENCH_DB"))


def ttl_seconds() -> float:
    """Seconds a finished run stays in memory (``<= 0`` disables eviction)."""
    raw = os.environ.get("LOCALBENCH_TTL_S")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_TTL_S
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_TTL_S


def sweep_seconds() -> float:
    raw = os.environ.get("LOCALBENCH_SWEEP_S")
    try:
        value = float(raw) if raw not in (None, "") else 30.0
    except (TypeError, ValueError):
        value = 30.0
    return max(1.0, value)


def db_path() -> str:
    return DB_PATH


def set_db_path(path: str) -> None:
    """Point sqlite storage at another file (tests)."""
    global DB_PATH, _BACKEND
    DB_PATH = path
    with _BACKEND_LOCK:
        _BACKEND = None


def reset_backend() -> None:
    """Drop the active backend (tests / mode switches)."""
    global _BACKEND
    with _BACKEND_LOCK:
        _BACKEND = None


# --------------------------------------------------------------------------- #
# secret hygiene / shared helpers
# --------------------------------------------------------------------------- #
def strip_secrets(obj: Any) -> Any:
    """Recursively drop secret-bearing keys from a JSON-able structure."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if str(key).lower() in _SECRET_KEYS:
                continue
            out[key] = strip_secrets(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [strip_secrets(v) for v in obj]
    return obj


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def _loads(text: Optional[str], fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return fallback


def _closest_point(points: list, key: str, target: float) -> Optional[dict]:
    best = None
    best_delta = None
    for point in points or []:
        if not isinstance(point, dict) or not point.get("ok"):
            continue
        try:
            value = float(point.get(key))
        except (TypeError, ValueError):
            continue
        delta = abs(value - target)
        if best_delta is None or delta < best_delta:
            best, best_delta = point, delta
    return best


def headline_metrics(results: Optional[dict]) -> Optional[dict]:
    """Headline numbers for a run summary (``None`` until results exist)."""
    if not isinstance(results, dict):
        return None
    context_points = [p for p in (results.get("context_points") or []) if p.get("ok")]
    concurrency_points = [
        p for p in (results.get("concurrency_points") or []) if p.get("ok")
    ]
    ttft = (results.get("ttft_ms") or {}).get("p50")
    decode = (results.get("decode_tps") or {}).get("p50")
    p4k = _closest_point(context_points, "ctx_requested", 4096)
    p8 = _closest_point(concurrency_points, "n", 8)
    headline = {
        "ttft_ms": ttft if ttft else None,
        "decode_tps": decode if decode else None,
        "prompt_tps_4k": (p4k or {}).get("prefill_tps") or None,
        "agg_tps_8": (p8 or {}).get("agg_tps") or None,
    }
    if not any(v is not None for v in headline.values()):
        return None
    return headline


def _summary(run: dict) -> dict:
    results = run.get("results") or {}
    gpu = results.get("gpu") or {}
    return {
        "id": run.get("id"),
        "label": run.get("label"),
        "model": run.get("model"),
        "endpoint_host": run.get("endpoint_host"),
        "preset_id": run.get("preset_id"),
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "finished_at": run.get("finished_at"),
        "headline": headline_metrics(results),
        "has_gpu": bool(gpu.get("available")),
    }


# --------------------------------------------------------------------------- #
# memory backend (default)
# --------------------------------------------------------------------------- #
class MemoryStore:
    """Runs in RAM only; nothing is written to disk."""

    name = "memory"

    def __init__(self) -> None:
        self._runs: dict = {}
        self._agent_keys: dict = {}
        self._lock = threading.RLock()

    # -- helpers ----------------------------------------------------------- #
    def _expired_locked(self, run_id: str, now: Optional[float] = None) -> bool:
        """Evict a terminal run whose TTL elapsed; returns True if dropped."""
        doc = self._runs.get(run_id)
        if not doc:
            return False
        ttl = ttl_seconds()
        if ttl <= 0:
            return False
        if doc.get("status") not in TERMINAL_STATUSES:
            return False
        stamp = doc.get("finished_at") or doc.get("created_at") or 0
        if not stamp:
            return False
        if (now if now is not None else time.time()) - float(stamp) > ttl:
            self._runs.pop(run_id, None)
            self._agent_keys.pop(run_id, None)
            return True
        return False

    def _prune_locked(self, now: Optional[float] = None) -> list:
        dropped = []
        for run_id in list(self._runs):
            if self._expired_locked(run_id, now):
                dropped.append(run_id)
        return dropped

    # -- store API --------------------------------------------------------- #
    def init(self) -> None:
        return None

    def save_run(self, run: dict, agent_key: Optional[str] = None) -> None:
        doc = strip_secrets(run)
        run_id = doc.get("id")
        if not run_id:
            raise ValueError("run document needs an id")
        with self._lock:
            self._runs[run_id] = doc
            if agent_key:
                self._agent_keys[run_id] = agent_key

    def create_run(self, run: dict, agent_key: Optional[str] = None) -> None:
        self.save_run(run, agent_key=agent_key)

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._lock:
            if self._expired_locked(run_id):
                return None
            doc = self._runs.get(run_id)
            return copy.deepcopy(doc) if doc is not None else None

    def get_agent_key(self, run_id: str) -> Optional[str]:
        with self._lock:
            if self._expired_locked(run_id):
                return None
            return self._agent_keys.get(run_id)

    def set_agent_connected(self, run_id: str, connected: bool = True) -> None:
        with self._lock:
            doc = self._runs.get(run_id)
            if doc is None:
                return
            agent = doc.get("agent")
            if not isinstance(agent, dict):
                agent = {"key": None, "connected": False}
                doc["agent"] = agent
            agent["connected"] = bool(connected)

    def delete_run(self, run_id: str) -> bool:
        with self._lock:
            existed = self._runs.pop(run_id, None) is not None
            self._agent_keys.pop(run_id, None)
            return existed

    def evict_run(self, run_id: str) -> bool:
        return self.delete_run(run_id)

    def evict_expired(self) -> list:
        with self._lock:
            return self._prune_locked()

    def count_runs(self) -> int:
        with self._lock:
            self._prune_locked()
            return len(self._runs)

    def count_runs_by_status(self, status: str) -> int:
        with self._lock:
            self._prune_locked()
            return sum(1 for doc in self._runs.values() if doc.get("status") == status)

    def list_runs(self, limit: int = 50) -> list:
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit = 50
        with self._lock:
            self._prune_locked()
            docs = sorted(
                self._runs.values(), key=lambda d: d.get("created_at") or 0, reverse=True
            )
            return [_summary(doc) for doc in docs[:limit]]

    def stats(self) -> dict:
        with self._lock:
            self._prune_locked()
            active = 0
            finished = 0
            for doc in self._runs.values():
                if doc.get("status") in TERMINAL_STATUSES:
                    finished += 1
                else:
                    active += 1
            return {"runs": len(self._runs), "active": active, "finished": finished}

    def run_ids(self) -> list:
        with self._lock:
            return list(self._runs)


# --------------------------------------------------------------------------- #
# sqlite backend (LOCALBENCH_PERSIST=1 — tests / debugging only)
# --------------------------------------------------------------------------- #
class SqliteStore:
    """Legacy sqlite persistence; never used on the public instance."""

    name = "sqlite"

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def init(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def _row_to_run(self, row: sqlite3.Row) -> dict:
        run = _loads(row["result_json"], {})
        if not isinstance(run, dict):
            run = {}
        run["id"] = row["id"]
        run.setdefault("label", row["label"])
        run.setdefault("model", row["model"])
        run.setdefault("endpoint_host", row["endpoint_host"])
        run["status"] = row["status"]
        run["created_at"] = row["created_at"]
        run.setdefault("started_at", row["started_at"])
        run.setdefault("finished_at", row["finished_at"])
        if not isinstance(run.get("config"), dict):
            run["config"] = _loads(row["config_json"], {})
        if not isinstance(run.get("agent"), dict):
            run["agent"] = {"key": None, "connected": bool(row["agent_connected"])}
        return run

    def save_run(self, run: dict, agent_key: Optional[str] = None) -> None:
        doc = strip_secrets(run)
        run_id = doc.get("id")
        if not run_id:
            raise ValueError("run document needs an id")
        config = doc.get("config") or {}
        agent = doc.get("agent") or {}
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO runs (id, created_at, started_at, finished_at, label, model,
                                      endpoint_host, status, config_json, result_json,
                                      agent_key, agent_connected)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        started_at      = excluded.started_at,
                        finished_at     = excluded.finished_at,
                        label           = excluded.label,
                        model           = excluded.model,
                        endpoint_host   = excluded.endpoint_host,
                        status          = excluded.status,
                        config_json     = excluded.config_json,
                        result_json     = excluded.result_json,
                        agent_key       = COALESCE(excluded.agent_key, runs.agent_key),
                        agent_connected = excluded.agent_connected
                    """,
                    (
                        run_id,
                        float(doc.get("created_at") or time.time()),
                        doc.get("started_at"),
                        doc.get("finished_at"),
                        doc.get("label"),
                        doc.get("model"),
                        doc.get("endpoint_host"),
                        doc.get("status") or "queued",
                        _dumps(config),
                        _dumps(doc),
                        agent_key,
                        1 if agent.get("connected") else 0,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def create_run(self, run: dict, agent_key: Optional[str] = None) -> None:
        self.save_run(run, agent_key=agent_key)

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            finally:
                conn.close()
        return self._row_to_run(row) if row is not None else None

    def get_agent_key(self, run_id: str) -> Optional[str]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT agent_key FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
            finally:
                conn.close()
        return row["agent_key"] if row is not None else None

    def set_agent_connected(self, run_id: str, connected: bool = True) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE runs SET agent_connected = ? WHERE id = ?",
                    (1 if connected else 0, run_id),
                )
                conn.commit()
            finally:
                conn.close()

    def delete_run(self, run_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def evict_run(self, run_id: str) -> bool:
        """Persistence mode keeps the row for debugging; nothing to drop here."""
        return False

    def evict_expired(self) -> list:
        return []

    def count_runs(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
            finally:
                conn.close()
        return int(row["n"]) if row is not None else 0

    def count_runs_by_status(self, status: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM runs WHERE status = ?", (status,)
                ).fetchone()
            finally:
                conn.close()
        return int(row["n"]) if row is not None else 0

    def list_runs(self, limit: int = 50) -> list:
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit = 50
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            finally:
                conn.close()
        return [_summary(self._row_to_run(row)) for row in rows]

    def stats(self) -> dict:
        runs = self.count_runs()
        finished = 0
        for status in TERMINAL_STATUSES:
            finished += self.count_runs_by_status(status)
        return {"runs": runs, "active": max(0, runs - finished), "finished": finished}

    def run_ids(self) -> list:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT id FROM runs").fetchall()
            finally:
                conn.close()
        return [row["id"] for row in rows]


# --------------------------------------------------------------------------- #
# active backend + module-level API used by the app
# --------------------------------------------------------------------------- #
_BACKEND: Any = None
_BACKEND_LOCK = threading.RLock()


def _backend():
    global _BACKEND
    if _BACKEND is None:
        with _BACKEND_LOCK:
            if _BACKEND is None:
                _BACKEND = SqliteStore(DB_PATH) if persist_enabled() else MemoryStore()
                _BACKEND.init()
    return _BACKEND


def init_db() -> None:
    """Initialise the active backend (idempotent; safe at every startup)."""
    _backend().init()


def backend_name() -> str:
    return _backend().name


def storage_summary() -> str:
    """One log line describing the storage model (no paths in memory mode)."""
    ttl = int(ttl_seconds())
    if _backend().name == "memory":
        return "storage: memory-only ttl=%ds" % ttl
    return "storage: sqlite ttl=%ds" % ttl


def save_run(run: dict, agent_key: Optional[str] = None) -> None:
    _backend().save_run(run, agent_key=agent_key)


def create_run(run: dict, agent_key: Optional[str] = None) -> None:
    _backend().create_run(run, agent_key=agent_key)


def get_run(run_id: str) -> Optional[dict]:
    return _backend().get_run(run_id)


def get_agent_key(run_id: str) -> Optional[str]:
    return _backend().get_agent_key(run_id)


def set_agent_connected(run_id: str, connected: bool = True) -> None:
    _backend().set_agent_connected(run_id, connected)


def delete_run(run_id: str) -> bool:
    return _backend().delete_run(run_id)


def evict_run(run_id: str) -> bool:
    return _backend().evict_run(run_id)


def evict_expired() -> list:
    return _backend().evict_expired()


def count_runs() -> int:
    return _backend().count_runs()


def count_runs_by_status(status: str) -> int:
    return _backend().count_runs_by_status(status)


def list_runs(limit: int = 50) -> list:
    return _backend().list_runs(limit=limit)


def stats() -> dict:
    return _backend().stats()


def run_ids() -> list:
    return _backend().run_ids()


# --------------------------------------------------------------------------- #
# export (pure: no files, no disk writes)
# --------------------------------------------------------------------------- #
def export_json(run: dict) -> str:
    return json.dumps(strip_secrets(run), indent=2, ensure_ascii=False, default=str)


_CSV_COLUMNS = [
    "section",
    "ctx_requested",
    "n",
    "prompt_tokens",
    "max_tokens",
    "ttft_ms",
    "ttft_corrected_ms",
    "prompt_ms",
    "prefill_tps",
    "decode_tps",
    "total_s",
    "completion_tokens",
    "wall_s",
    "agg_tps",
    "per_req_tps",
    "ttft_p50",
    "ttft_p95",
    "prefill_source",
    "prefill_tps_server",
    "prompt_ms_server",
    "cache_bust",
    "ok",
    "error",
    "draft_n",
    "draft_accepted",
    "acceptance_rate",
]


def _blank(value):
    """CSV cell: None -> "" (so prefill_tps:null reads as empty)."""
    return "" if value is None else value


def _row(cells: dict) -> list:
    """Exactly one cell per header column, in header order."""
    return [_blank(cells.get(column, "")) for column in _CSV_COLUMNS]


def export_csv(run: dict) -> str:
    """Tidy CSV: one row per context point and per concurrency point.

    Rows are built from ``_CSV_COLUMNS`` so column order can never drift; a
    column that does not apply to a row is left empty.
    """
    results = run.get("results") or {}
    config = run.get("config") or {}
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)

    for point in results.get("context_points") or []:
        draft = point.get("draft") or {}
        writer.writerow(
            _row(
                {
                    "section": "context",
                    "ctx_requested": point.get("ctx_requested"),
                    "prompt_tokens": point.get("prompt_tokens"),
                    "max_tokens": point.get("max_tokens"),
                    "ttft_ms": point.get("ttft_ms"),
                    "ttft_corrected_ms": point.get("ttft_corrected_ms"),
                    "prompt_ms": point.get("prompt_ms"),
                    "prefill_tps": point.get("prefill_tps"),
                    "decode_tps": point.get("decode_tps"),
                    "total_s": point.get("total_s"),
                    "completion_tokens": point.get("completion_tokens"),
                    "prefill_source": point.get("prefill_source"),
                    "prefill_tps_server": point.get("prefill_tps_server"),
                    "prompt_ms_server": point.get("prompt_ms_server"),
                    "cache_bust": point.get("cache_bust"),
                    "ok": point.get("ok"),
                    "error": point.get("error"),
                    "draft_n": draft.get("n"),
                    "draft_accepted": draft.get("accepted"),
                    "acceptance_rate": draft.get("acceptance_rate"),
                }
            )
        )

    for point in results.get("concurrency_points") or []:
        writer.writerow(
            _row(
                {
                    "section": "concurrency",
                    "ctx_requested": config.get("concurrency_context"),
                    "n": point.get("n"),
                    "prompt_tokens": point.get("prompt_tokens"),
                    "max_tokens": config.get("max_tokens"),
                    "completion_tokens": point.get("completion_tokens"),
                    "wall_s": point.get("wall_s"),
                    "agg_tps": point.get("agg_tps"),
                    "per_req_tps": point.get("per_req_tps"),
                    "ttft_p50": point.get("ttft_p50"),
                    "ttft_p95": point.get("ttft_p95"),
                    "ok": point.get("ok"),
                    "error": point.get("error"),
                }
            )
        )
    return buf.getvalue()
