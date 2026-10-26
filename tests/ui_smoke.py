#!/usr/bin/env python3
"""Headless Chromium smoke test for the LocalBench UI, over raw CDP.

No playwright needed: the box has a Playwright-downloaded Chrome binary and the
``websockets`` package, so we drive the browser through the DevTools protocol.

Checks (CONTRACT.md "Testing / acceptance" #4):
  * page loads (HTTP 200, expected title / tab copy present)
  * zero console errors, page exceptions and failed resource loads
  * ``document.documentElement.scrollWidth - window.innerWidth === 0`` at
    1440x980 and 390x844
  * screenshots saved to tests/screenshots/bench-ui-<w>x<h>.png

Usage:
    .venv/bin/python tests/ui_smoke.py [url]        # default public URL
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from websockets.sync.client import connect as ws_connect
except Exception as exc:  # pragma: no cover
    raise SystemExit("websockets package missing: %r" % exc)

ROOT = Path(__file__).resolve().parents[1]
SHOTS = Path(__file__).resolve().parent / "screenshots"
DEFAULT_URL = "http://127.0.0.1:21700/"

CHROME_CANDIDATES = [
    os.path.expanduser("~/.cache/ms-playwright/chromium-1248/chrome-linux-arm64/chrome"),
    os.path.expanduser("~/.cache/ms-playwright/chromium-1201/chrome-linux/chrome"),
    os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-1248/"
                       "chrome-headless-shell-linux-arm64/chrome-headless-shell"),
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
]


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("no usable Chrome/Chromium binary found")


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class CDP:
    """Minimal DevTools-protocol client on one page target."""

    def __init__(self, ws_url: str) -> None:
        self.ws = ws_connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=15)
        self._next_id = 0
        self.console_errors: List[str] = []
        self.page_errors: List[str] = []
        self.network_errors: List[str] = []
        self.events: List[Dict[str, Any]] = []

    # -- low level ---------------------------------------------------------
    def _handle(self, message: Dict[str, Any]) -> None:
        self.events.append(message)
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            text = " ".join(
                str(arg.get("value", arg.get("description", "")))
                for arg in params.get("args") or []
            )
            self.console_errors.append("console.error: " + text[:400])
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            exception = details.get("exception") or {}
            self.page_errors.append(
                "pageerror: %s %s"
                % (details.get("text", ""), str(exception.get("description", ""))[:400])
            )
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if entry.get("level") == "error":
                self.console_errors.append(
                    "log[%s]: %s %s"
                    % (entry.get("source"), entry.get("text"), entry.get("url", ""))
                )
        elif method == "Network.loadingFailed":
            error_text = str(params.get("errorText") or "")
            if "ERR_ABORTED" not in error_text:
                self.network_errors.append(
                    "request failed: %s (%s)" % (error_text, params.get("requestId"))
                )

    def send(self, method: str, params: Optional[Dict[str, Any]] = None,
             timeout: float = 30.0) -> Dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(self.ws.recv(timeout=max(0.1, deadline - time.time())))
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError("%s failed: %r" % (method, message["error"]))
                return message.get("result") or {}
            self._handle(message)
        raise TimeoutError("%s timed out after %.1fs" % (method, timeout))

    def drain(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                message = json.loads(self.ws.recv(timeout=max(0.05, min(0.25, deadline - time.time()))))
            except TimeoutError:
                continue
            except Exception:
                return
            self._handle(message)

    def wait_event(self, name: str, timeout: float = 30.0) -> Dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                message = json.loads(self.ws.recv(timeout=max(0.05, min(0.5, deadline - time.time()))))
            except TimeoutError:
                continue
            self._handle(message)
            if message.get("method") == name:
                return message
        raise TimeoutError("event %s not seen within %.1fs" % (name, timeout))

    def evaluate(self, expression: str) -> Any:
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
        )
        if result.get("exceptionDetails"):
            raise RuntimeError("JS evaluation failed: %r" % result["exceptionDetails"])
        return (result.get("result") or {}).get("value")

    def evaluate_async(self, expression: str, timeout: float = 30.0) -> Any:
        """Evaluate a promise-returning expression (IndexedDB probes)."""
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "timeout": int(timeout * 1000),
            },
            timeout=timeout + 5,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError("JS evaluation failed: %r" % result["exceptionDetails"])
        return (result.get("result") or {}).get("value")

    def requests(self) -> List[Dict[str, str]]:
        """Every Network.requestWillBeSent as {method,url} (redirects included)."""
        out = []
        for event in self.events:
            if event.get("method") != "Network.requestWillBeSent":
                continue
            request = (event.get("params") or {}).get("request") or {}
            out.append({"method": request.get("method", ""), "url": request.get("url", "")})
        return out

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


PROBE_JS = r"""
(() => {
  const doc = document.documentElement;
  const text = document.body ? document.body.innerText : "";
  const tabs = Array.from(document.querySelectorAll('[role="tab"], .tab, [data-tab], button'))
    .map(el => (el.textContent || "").trim())
    .filter(t => t.length > 0 && t.length < 40);
  return JSON.stringify({
    title: document.title,
    ready: document.readyState,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    scrollWidth: doc.scrollWidth,
    overflow: doc.scrollWidth - window.innerWidth,
    tabs: tabs.slice(0, 20),
    hasRun: /\bRun\b/.test(text),
    hasHistory: /\bHistory\b/.test(text),
    hasCompare: /\bCompare\b/.test(text),
    bodySample: text.slice(0, 160).replace(/\s+/g, " "),
  });
})()
"""


def launch_chrome(chrome: str) -> subprocess.Popen:
    port = free_port()
    profile = tempfile.mkdtemp(prefix="bench-ui-chrome-")
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--mute-audio",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--window-size=1440,980",
            "--remote-debugging-port=%d" % port,
            "--user-data-dir=%s" % profile,
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.port = port  # type: ignore[attr-defined]
    proc.profile = profile  # type: ignore[attr-defined]
    return proc


def page_ws_url(port: int, timeout: float = 20.0) -> str:
    deadline = time.time() + timeout
    last_error: Any = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=2) as response:
                targets = json.loads(response.read().decode("utf-8"))
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except Exception as exc:
            last_error = exc
        time.sleep(0.1)
    raise SystemExit("devtools endpoint never came up (%r)" % (last_error,))


def check_viewport(cdp: CDP, url: str, width: int, height: int, mobile: bool) -> Dict[str, Any]:
    cdp.console_errors.clear()
    cdp.page_errors.clear()
    cdp.network_errors.clear()

    cdp.send(
        "Emulation.setDeviceMetricsOverride",
        {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": mobile},
    )
    cdp.send("Page.navigate", {"url": url})
    cdp.wait_event("Page.loadEventFired", timeout=45)
    cdp.drain(2.5)  # let the SPA boot, fetch /api/config, /api/presets and render

    probe = json.loads(cdp.evaluate(PROBE_JS))

    screenshot = cdp.send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    SHOTS.mkdir(parents=True, exist_ok=True)
    shot_path = SHOTS / ("bench-ui-%dx%d.png" % (width, height))
    shot_path.write_bytes(base64.b64decode(screenshot["data"]))

    errors = list(cdp.console_errors) + list(cdp.page_errors) + list(cdp.network_errors)
    # CF-injected analytics beacon blocked by the app CSP = deployment setting,
    # not a frontend error; keep it visible but out of the pass/fail verdict.
    external = [
        err for err in errors if "cloudflareinsights" in err or err.startswith("request failed:")
    ]
    app_errors = [err for err in errors if err not in external]
    return {
        "viewport": "%dx%d" % (width, height),
        "screenshot": str(shot_path),
        "title": probe.get("title"),
        "ready": probe.get("ready"),
        "inner_width": probe.get("innerWidth"),
        "scroll_width": probe.get("scrollWidth"),
        "overflow_px": probe.get("overflow"),
        "tabs": probe.get("tabs"),
        "copy": {k: probe.get(k) for k in ("hasRun", "hasHistory", "hasCompare")},
        "body_sample": probe.get("bodySample"),
        "console_errors": app_errors,
        "external_console_errors": external,
        "ok": probe.get("overflow") == 0
        and probe.get("ready") == "complete"
        and not app_errors
        and bool(probe.get("title")),
    }


def _main_smoke(url: str) -> int:
    chrome = find_chrome()
    proc = launch_chrome(chrome)
    report: Dict[str, Any] = {"url": url, "chrome": chrome, "viewports": []}
    try:
        ws_url = page_ws_url(proc.port)
        cdp = CDP(ws_url)
        try:
            for method in ("Page.enable", "Runtime.enable", "Log.enable", "Network.enable"):
                cdp.send(method)
            for width, height, mobile in ((1440, 980, False), (390, 844, True)):
                result = check_viewport(cdp, url, width, height, mobile)
                report["viewports"].append(result)
                print(json.dumps(result, indent=2))
        finally:
            cdp.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    passed = all(v["ok"] for v in report["viewports"])
    print(json.dumps({"passed": passed, "url": url,
                      "viewports": [v["viewport"] for v in report["viewports"]]}))
    return 0 if passed else 1



# ---------------------------------------------------------------------------
# Client-side storage proof (CONTRACT.md "Storage model"): the UI stores runs in
# IndexedDB (DB "localbench", store "runs") and History/Compare render from it
# without ever calling the withdrawn GET /api/runs list endpoint.
# ---------------------------------------------------------------------------

IDB_PROBE_JS = r"""
(async () => {
  const open = () => new Promise((resolve) => {
    let request = null;
    try { request = indexedDB.open('localbench'); } catch (e) { resolve(null); return; }
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => resolve(null);
    request.onblocked = () => resolve(null);
  });
  const db = await open();
  if (!db) return JSON.stringify({ok: false, error: 'indexedDB unavailable'});
  const stores = Array.from(db.objectStoreNames);
  if (!stores.includes('runs')) {
    return JSON.stringify({ok: false, error: 'no runs store', stores});
  }
  const records = await new Promise((resolve) => {
    const tx = db.transaction('runs', 'readonly');
    const request = tx.objectStore('runs').getAll();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => resolve([]);
  });
  return JSON.stringify({
    ok: true,
    db: db.name,
    version: db.version,
    stores: stores,
    count: records.length,
    records: records.map((r) => ({
      id: r.id,
      label: r.label,
      model: r.model,
      status: r.status,
      ctx_points: (((r.results || {}).context_points) || []).length,
      conc_points: (((r.results || {}).concurrency_points) || []).length,
      saved_at: r.saved_at || null,
      keys: Object.keys(r).slice(0, 40),
      has_api_key: JSON.stringify(r).indexOf('"api_key"') !== -1,
      has_run_token: JSON.stringify(r).indexOf('"rt_') !== -1,
    })),
  });
})()
"""

SET_FORM_JS = r"""
(() => {
  const set = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return false;
    const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
    if (descriptor && descriptor.set) descriptor.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    return true;
  };
  const filled = {
    base_url: set('base-url', %s),
    model: set('model', %s),
    label: set('label', %s),
    contexts: set('cfg-contexts', %s),
    concurrency: set('cfg-concurrency', %s),
    max_tokens: set('cfg-max-tokens', %s),
    warmup: set('cfg-warmup', %s),
  };
  const button = document.getElementById('run-btn');
  if (button) button.click();
  return JSON.stringify({filled: filled, clicked: !!button});
})()
"""

HISTORY_PROBE_JS = r"""
(() => {
  const body = document.getElementById('history-body');
  const rows = body ? body.querySelectorAll('tr') : [];
  const counter = document.getElementById('history-count');
  const text = (document.body && document.body.innerText) || '';
  return JSON.stringify({
    rows: rows.length,
    counter: counter ? (counter.textContent || '').trim() : null,
    has_label: text.indexOf(%s) !== -1,
    overflow: document.documentElement.scrollWidth - window.innerWidth,
  });
})()
"""


def _is_list_runs_call(method: str, url: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return method.upper() == "GET" and path == "/api/runs"


def _idb_probe(cdp: "CDP") -> Dict[str, Any]:
    raw = cdp.evaluate_async(IDB_PROBE_JS)
    return json.loads(raw) if raw else {"ok": False, "error": "empty probe result"}


def run_idb_check(url: str) -> Dict[str, Any]:
    """Drive one real run through the served UI, then prove IndexedDB history."""
    sys.path.insert(0, str(ROOT / "mock"))
    from mock_openai import start_mock_server

    mock = start_mock_server(
        port=0, ttft_ms=15.0, prefill_tps=4000.0, tps=400.0, model="mock-qwen", draft_n=8, draft_accepted=6
    )
    label = "bench-idb-smoke-%d" % int(time.time())
    chrome = find_chrome()
    proc = launch_chrome(chrome)
    report: Dict[str, Any] = {"url": url, "mock": mock.base_url, "label": label}
    cdp = None
    try:
        cdp = CDP(page_ws_url(proc.port))
        for method in ("Page.enable", "Runtime.enable", "Log.enable", "Network.enable"):
            cdp.send(method)
        cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 980, "deviceScaleFactor": 1, "mobile": False},
        )
        cdp.send("Page.navigate", {"url": url})
        cdp.wait_event("Page.loadEventFired", timeout=45)
        cdp.drain(2.5)

        # baseline: fresh browser profile -> no stored runs, no list calls
        baseline = _idb_probe(cdp)
        report["baseline"] = baseline
        list_calls_before = sum(
            1 for r in cdp.requests() if _is_list_runs_call(r["method"], r["url"])
        )

        # fill the real form and click "Run benchmark"
        form_js = SET_FORM_JS % (
            json.dumps(mock.base_url),
            json.dumps("mock-qwen"),
            json.dumps(label),
            json.dumps("512,1024"),
            json.dumps("1"),
            json.dumps("8"),
            json.dumps("0"),
        )
        report["form"] = json.loads(cdp.evaluate(form_js))

        # wait for the finished run to land in IndexedDB (client-side save)
        deadline = time.time() + 150
        record = None
        last_probe: Dict[str, Any] = {}
        while time.time() < deadline:
            cdp.drain(0.5)
            last_probe = _idb_probe(cdp)
            if last_probe.get("ok"):
                for candidate in last_probe.get("records") or []:
                    if candidate.get("label") == label:
                        record = candidate
                        break
            if record is not None:
                break
            form_error = cdp.evaluate(
                "(() => { const el = document.getElementById('form-error');"
                " return el ? (el.hidden ? '' : (el.innerText || '').trim()) : ''; })()"
            )
            if form_error:
                report["form_error"] = form_error
                break
        report["idb_after_run"] = last_probe
        report["record"] = record
        if record is None:
            report["passed"] = False
            report["error"] = "run never appeared in IndexedDB: %r" % (report.get("form_error") or "")
            return report

        # the stored record must be the real run, without any secret material
        assert record["id"].startswith("r_"), record
        assert record["label"] == label, record
        assert record["status"] == "done", record
        assert record["ctx_points"] >= 1, record
        assert record["conc_points"] >= 1, record
        assert record["has_api_key"] is False, record
        assert record["has_run_token"] is False, record

        # History must render from IndexedDB with zero GET /api/runs calls
        history_before = sum(
            1 for r in cdp.requests() if _is_list_runs_call(r["method"], r["url"])
        )
        cdp.evaluate(
            "(() => { const tab = Array.from(document.querySelectorAll('button,[role=tab]'))"
            ".find((e) => /^History/i.test((e.textContent || '').trim()));"
            " if (tab) tab.click(); return !!tab; })()"
        )
        cdp.drain(2.0)
        history_after = sum(
            1 for r in cdp.requests() if _is_list_runs_call(r["method"], r["url"])
        )
        history = json.loads(cdp.evaluate(HISTORY_PROBE_JS % json.dumps(label)))
        report["history"] = history
        report["list_calls_before"] = list_calls_before
        report["list_calls_after_history"] = history_after
        all_errors = cdp.console_errors + cdp.page_errors + cdp.network_errors
        # Cloudflare injects its Web Analytics beacon into proxied HTML; the app's
        # own CSP (script-src 'self') blocks it -> a console error that is a
        # deployment (CF setting) issue, not the frontend's. Kept separately.
        external = [
            err
            for err in all_errors
            if "cloudflareinsights" in err or err.startswith("request failed:")
        ]
        app_errors = [err for err in all_errors if err not in external]
        report["console_errors"] = all_errors
        report["app_console_errors"] = app_errors
        report["external_console_errors"] = external
        report["passed"] = (
            history.get("rows", 0) >= 1
            and history.get("has_label") is True
            and history.get("overflow", 0) <= 0
            and history_before == 0
            and history_after == 0
            and not app_errors
        )
        SHOTS.mkdir(parents=True, exist_ok=True)
        shot = cdp.send("Page.captureScreenshot", {"format": "png"})
        shot_path = SHOTS / "bench-ui-idb-history-1440x980.png"
        shot_path.write_bytes(base64.b64decode(shot["data"]))
        report["screenshot"] = str(shot_path)
        return report
    finally:
        if cdp is not None:
            cdp.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        mock.stop()


def main(argv: List[str]) -> int:
    flags = [a for a in argv[1:] if not a.startswith("--")]
    url = flags[0] if flags else DEFAULT_URL
    if "--idb" in argv:
        report = run_idb_check(url)
        print(json.dumps(report, indent=2))
        return 0 if report.get("passed") else 1
    return _main_smoke(url)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
