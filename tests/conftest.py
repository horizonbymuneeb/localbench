"""Shared pytest plumbing for the LocalBench suite.

Design goals
------------
* Hermetic: every test talks to ``mock/mock_openai.py`` on a loopback port; no
  GPU, no internet, no reliance on the production ``data/runs.db``.
* Backend is imported lazily and exactly once (session scope) so tests can run
  while ``backend/`` is still being written -- an ImportError surfaces as an
  obvious skip-with-reason instead of a collection crash.
* Contract-level assertions only: the suite drives the documented REST API,
  not backend internals, so it keeps working while backend/ changes shape.
"""

from __future__ import annotations

import atexit
import importlib
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOCK_DIR = ROOT / "mock"
BACKEND_DIR = ROOT / "backend"

for _p in (str(ROOT), str(MOCK_DIR), str(BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Isolate the sqlite DB *before* anything imports the backend.
# ---------------------------------------------------------------------------

_TMP_DIR = Path(tempfile.mkdtemp(prefix="localbench-tests-"))
TEST_DB = _TMP_DIR / "runs.db"
#: The in-process suite runs the *persistence* shape on purpose: CONTRACT.md says
#: LOCALBENCH_PERSIST=1 is test-only, and it keeps the sqlite store covered while
#: the tmp paths guarantee nothing touches /opt/localbench/data.
os.environ["LOCALBENCH_PERSIST"] = "1"
os.environ["LOCALBENCH_DB"] = str(TEST_DB)
os.environ["LOCALBENCH_DATA_DIR"] = str(_TMP_DIR)
os.environ["LOCALBENCH_TESTING"] = "1"
# The abuse guard is off for the suite by default (rate-limit tests turn it on
# per test with monkeypatch), so tests never burn each other's 6/600 budget.
os.environ["LOCALBENCH_RATE_LIMIT"] = "0/600"


def _cleanup_tmp() -> None:
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


atexit.register(_cleanup_tmp)


# ---------------------------------------------------------------------------
# Mock server fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_factory():
    """Create ad-hoc mock servers; all are stopped at teardown."""
    from mock_openai import start_mock_server  # mock/ is on sys.path

    handles: List[Any] = []

    def make(**kwargs: Any):
        kwargs.setdefault("ttft_ms", 8.0)
        kwargs.setdefault("tps", 600.0)
        kwargs.setdefault("ctx", 131072)
        kwargs.setdefault("model", "mock-qwen")
        handle = start_mock_server(port=0, **kwargs)
        handles.append(handle)
        return handle

    yield make

    for handle in handles:
        try:
            handle.stop()
        except Exception:
            pass


@pytest.fixture()
def mock_server(mock_factory):
    """Default fast mock.

    * ``ttft_ms=20``          -> overhead p50 ~20 ms, so corrected TTFT is real
    * ``prefill_tps=4000``    -> TTFT grows with the prompt, making the contract's
      ``prefill_tps = prompt_tokens / (prompt_ms/1000)`` field measurable
    * ``tps=600`` + draft 24/15 -> decode speed + speculative-decoding stats
    """
    return mock_factory(
        ttft_ms=20.0, prefill_tps=4000.0, tps=600.0, draft_n=24, draft_accepted=15
    )


@pytest.fixture()
def slow_mock(mock_factory):
    """Mock paced slowly enough that runs overlap / can be cancelled."""
    return mock_factory(ttft_ms=250.0, tps=120.0, draft_n=0, draft_accepted=0)


# ---------------------------------------------------------------------------
# Backend import + FastAPI TestClient
# ---------------------------------------------------------------------------

_backend_error: Optional[str] = None
_backend_module: Any = None


def import_backend():
    """Import backend.app as a package module, falling back to flat layout."""
    global _backend_error, _backend_module
    if _backend_module is not None:
        return _backend_module
    errors: List[str] = []
    module = None
    for name in ("backend.app", "app"):
        try:
            module = importlib.import_module(name)
            break
        except ImportError as exc:  # backend may still be absent / half-written
            errors.append("%s: %s" % (name, exc))
        except Exception as exc:  # import-time crash in backend code
            errors.append("%s: %r" % (name, exc))
    if module is None:
        _backend_error = "; ".join(errors) or "backend.app not importable"
        raise ImportError(_backend_error)
    _backend_module = module
    _isolate_backend_db()
    return module


def _isolate_backend_db() -> None:
    """Best-effort: point any store module paths at the tmp DB after import."""
    for mod_name in list(sys.modules):
        if not (mod_name.endswith(".store") or mod_name == "store"):
            continue
        mod = sys.modules[mod_name]
        patched = False
        for attr in ("DB_PATH", "DB_FILE", "DATABASE", "DB", "PATH"):
            if isinstance(getattr(mod, attr, None), (str, Path)):
                setattr(mod, attr, str(TEST_DB))
                patched = True
        init = getattr(mod, "init_db", None) or getattr(mod, "init", None)
        if patched and callable(init):
            try:
                init()
            except Exception:
                pass


def get_fastapi_app():
    """Return the FastAPI application object from backend.app."""
    module = import_backend()
    app = getattr(module, "app", None)
    if app is not None and hasattr(app, "router"):
        return app
    factory = getattr(module, "create_app", None)
    if callable(factory):
        return factory()
    raise ImportError("backend.app exposes neither a FastAPI `app` nor create_app()")


@pytest.fixture(scope="session")
def app():
    """The FastAPI app under test (session wide)."""
    try:
        return get_fastapi_app()
    except ImportError as exc:
        pytest.skip("backend not importable yet: %s" % exc)


@pytest.fixture(scope="session")
def api(app):
    """Starlette TestClient with lifespan started; shared by all API tests."""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Per-run access tokens (CONTRACT.md: X-Run-Token / ?token=). The registry is
#: process-wide, like a browser's sessionStorage, so helpers can attach them.
RUN_TOKENS: Dict[str, str] = {}


def remember_token(run_id: str, token: str) -> str:
    assert isinstance(run_id, str) and run_id
    assert isinstance(token, str) and token.startswith("rt_"), token
    RUN_TOKENS[run_id] = token
    return token


def token_for(run_id: str) -> str:
    token = RUN_TOKENS.get(run_id)
    assert token, "no access token registered for %s (did post_run() create it?)" % run_id
    return token


def headers_for(run_id: str) -> Dict[str, str]:
    return {"X-Run-Token": token_for(run_id)}


def events_url(run_id: str) -> str:
    return "/api/runs/%s/events?token=%s" % (run_id, token_for(run_id))


def create_run(client, base_url: str, label: str = "test run", **kwargs):
    """POST /api/runs; returns the full JSON body with the access token."""
    body = run_payload(base_url, label=label, **kwargs)
    response = client.post("/api/runs", json=body)
    assert response.status_code == 200, "POST /api/runs -> %s %s" % (
        response.status_code,
        response.text[:400],
    )
    data = response.json()
    assert data.get("ok") is True, data
    run_id = data.get("run_id")
    token = data.get("access_token")
    assert isinstance(run_id, str) and run_id, data
    assert isinstance(token, str) and token.startswith("rt_"), data
    remember_token(run_id, token)
    return data


#: Fast, deterministic run config used by most API tests.
FAST_CONFIG: Dict[str, Any] = {
    "contexts": [512, 1024],
    "concurrency": [1, 2],
    "concurrency_context": 256,
    "max_tokens": 8,
    "repeats": 1,
    "warmup": 1,
    "idle_sample_s": 1,
    "enable_context_ladder": True,
    "enable_concurrency": True,
    "request_timeout_s": 30,
}


def run_payload(
    base_url: str,
    label: str = "test run",
    model: str = "mock-qwen",
    api_key: str = "sk-test-secret-do-not-persist",
    config: Optional[Dict[str, Any]] = None,
    telemetry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "label": label,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "config": dict(FAST_CONFIG, **(config or {})),
    }
    if telemetry is not None:
        payload["telemetry"] = telemetry
    return payload


def post_run(client, base_url: str, label: str = "test run", **kwargs) -> str:
    """POST /api/runs, remember the access token, return the run_id."""
    return create_run(client, base_url, label=label, **kwargs)["run_id"]


def get_run(client, run_id: str, token: Optional[str] = None) -> Dict[str, Any]:
    """GET the run document with its access token (CONTRACT.md run auth)."""
    headers = {"X-Run-Token": token} if token is not None else headers_for(run_id)
    response = client.get("/api/runs/%s" % run_id, headers=headers)
    assert response.status_code == 200, "GET /api/runs/%s -> %s %s" % (
        run_id,
        response.status_code,
        response.text[:400],
    )
    data = response.json()
    assert data.get("ok") is True, data
    return data["run"]


def wait_terminal(client, run_id: str, timeout: float = 45.0, poll: float = 0.1) -> Dict[str, Any]:
    """Poll GET /api/runs/{id} until the run reaches a terminal status."""
    terminal = ("done", "failed", "cancelled")
    deadline = time.time() + timeout
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        last = get_run(client, run_id)
        if last.get("status") in terminal:
            return last
        time.sleep(poll)
    raise AssertionError(
        "run %s did not finish within %.1fs (last status=%r, progress=%r)"
        % (run_id, timeout, last.get("status"), last.get("progress"))
    )


def wait_for(predicate, timeout: float = 20.0, poll: float = 0.05, what: str = "condition") -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(poll)
    raise AssertionError("timed out after %.1fs waiting for %s" % (timeout, what))


def free_port() -> int:
    """Grab an ephemeral loopback port (bound then released)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def wait_http_ok(url: str, timeout: float = 20.0) -> Dict[str, Any]:
    """Poll *url* until it answers 200 with JSON; return the payload."""
    import httpx

    deadline = time.time() + timeout
    last_error: Any = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200:
                try:
                    return response.json()
                except Exception:
                    return {}
        except Exception as exc:  # not up yet
            last_error = exc
        time.sleep(0.05)
    raise AssertionError("%s never became ready (%r)" % (url, last_error))


def parse_sse_lines(lines: Iterable[str]) -> List[Dict[str, Any]]:
    """Turn an SSE line iterator into a list of JSON events (ignores comments)."""
    events: List[Dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            events.append(json.loads(data))
        except json.JSONDecodeError:
            raise AssertionError("SSE data is not JSON: %r" % line[:300])
    return events


def collect_sse(client, run_id: str, timeout: float = 60.0) -> List[Dict[str, Any]]:
    """Consume GET /api/runs/{id}/events with the TestClient, return all events."""
    events: List[Dict[str, Any]] = []
    deadline = time.time() + timeout
    with client.stream("GET", events_url(run_id), timeout=timeout) as response:
        assert response.status_code == 200, "SSE -> %s" % response.status_code
        assert "text/event-stream" in response.headers.get("content-type", ""), response.headers
        for event in parse_sse_lines(response.iter_lines()):
            events.append(event)
            if event.get("type") == "done":
                break
            if time.time() > deadline:
                raise AssertionError("SSE did not send done within %.1fs" % timeout)
    return events


def json_text(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def sqlite_dump_text(db_path: Path) -> str:
    """All text stored in a sqlite file (used to prove no secret persisted)."""
    import sqlite3

    if not Path(db_path).exists():
        return ""
    chunks: List[str] = []
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            try:
                rows = conn.execute('SELECT * FROM "%s"' % table).fetchall()
            except Exception:
                continue
            for row in rows:
                chunks.append(repr(row))
    finally:
        conn.close()
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Backend module accessors (contract-level tests may still assert pure helpers)
# ---------------------------------------------------------------------------


def _import_backend_submodule(name: str):
    errors = []
    for module_name in ("backend.%s" % name, name):
        try:
            return importlib.import_module(module_name)
        except Exception as exc:  # ImportError or half-written module
            errors.append("%s: %r" % (module_name, exc))
    raise ImportError("; ".join(errors))


@pytest.fixture(scope="session")
def engine_mod():
    try:
        return _import_backend_submodule("engine")
    except ImportError as exc:
        pytest.skip("backend engine not importable yet: %s" % exc)


@pytest.fixture(scope="session")
def store_mod():
    try:
        module = _import_backend_submodule("store")
        if hasattr(module, "set_db_path"):
            module.set_db_path(str(TEST_DB))
        return module
    except ImportError as exc:
        pytest.skip("backend store not importable yet: %s" % exc)


@pytest.fixture(scope="session")
def agent_mod():
    try:
        return _import_backend_submodule("agent")
    except ImportError as exc:
        pytest.skip("backend agent helpers not importable yet: %s" % exc)
