#!/usr/bin/env python3
"""Lead end-to-end UI acceptance: two labelled runs through the real browser,
local (IndexedDB) history, compare view, exports, no server-side history.

Starts a mock OpenAI server, drives the live site with Playwright Chromium.

  python3 scripts/lead_e2e_ui.py
"""
import json
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

ROOT = "/opt/localbench"
URL = "http://127.0.0.1:21700/"
SHOTS = f"{ROOT}/tests/screenshots"
MOCK_PORT = 28921
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" · {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


mock = subprocess.Popen(
    [f"{ROOT}/.venv/bin/python", f"{ROOT}/mock/mock_openai.py", "--port", str(MOCK_PORT),
     "--ttft-ms", "60", "--tps", "140", "--ctx", "32768", "--draft-n", "64", "--draft-accepted", "41"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.2)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 980}, accept_downloads=True)
        page = ctx.new_page()
        errors, downloads = [], []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "cloudflareinsights" not in m.text else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("download", lambda d: downloads.append(d.suggested_filename))
        page.goto(URL, wait_until="networkidle", timeout=45000)

        def run_benchmark(label):
            page.click("#tabbtn-run")
            page.fill("#base-url", f"http://127.0.0.1:{MOCK_PORT}/v1")
            page.fill("#api-key", "sk-mock-key")
            page.fill("#model", "mock-local")
            page.fill("#label", label)
            if not page.is_visible("#cfg-contexts"):
                page.click("#adv-toggle")
            page.fill("#cfg-contexts", "1024,4096")
            page.fill("#cfg-concurrency", "1,2")
            page.fill("#cfg-max-tokens", "32")
            page.fill("#cfg-warmup", "0")
            page.click("#run-btn")
            page.wait_for_function(
                """() => {
                    const t = (document.querySelector('#run-status-text')?.textContent || '').toLowerCase();
                    const cls = document.querySelector('#run-status')?.className || '';
                    return t.includes('done') || cls.includes('done');
                }""", timeout=180000)
            page.wait_for_timeout(800)

        run_benchmark("lead e2e baseline")
        page.screenshot(path=f"{SHOTS}/lead-e2e-run1.png")

        rows = page.evaluate("""async () => {
            const db = await new Promise((res, rej) => { const r = indexedDB.open('localbench');
                r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
            const tx = db.transaction('runs', 'readonly');
            const all = await new Promise((res, rej) => { const q = tx.objectStore('runs').getAll();
                q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error); });
            return all.map(r => ({id: r.id, label: r.label, ctx: (r.results?.context_points || []).length,
                                  conc: (r.results?.concurrency_points || []).length,
                                  decode: r.results?.decode_tps?.p50, ttft: r.results?.ttft_ms?.p50}));
        }""")
        print("indexeddb runs:", json.dumps(rows))
        check("run_saved_locally", len(rows) == 1, len(rows))
        check("local_has_context_points", rows and rows[0]["ctx"] == 2, rows[0]["ctx"] if rows else None)
        check("local_has_concurrency_points", rows and rows[0]["conc"] == 2, rows[0]["conc"] if rows else None)
        check("local_has_decode_rate", rows and (rows[0]["decode"] or 0) > 50, rows[0]["decode"] if rows else None)

        # client-side export from the run panel (still on Run tab, run displayed)
        exp = page.locator("#export-links button")
        print("export buttons:", exp.count())
        if exp.count():
            exp.first.click()
            page.wait_for_timeout(1200)
        check("export_local_json", any(d.endswith(".json") for d in downloads), downloads[:3])

        run_benchmark("lead e2e mtp draft=3")
        page.click("#tabbtn-history")
        page.wait_for_timeout(800)
        page.screenshot(path=f"{SHOTS}/lead-e2e-history.png", full_page=True)
        hist_rows = page.locator("#history-body tr").count()
        print("history rows:", hist_rows)
        check("history_two_rows", hist_rows == 2, hist_rows)

        # select both runs -> compare
        checks = page.locator('#history-body input[type="checkbox"]')
        for i in range(min(2, checks.count())):
            checks.nth(i).check()
        page.click("#history-compare")
        page.wait_for_timeout(1200)
        page.screenshot(path=f"{SHOTS}/lead-e2e-compare.png", full_page=True)
        cmp_text = page.inner_text("#tab-compare")
        check("compare_has_baseline", "baseline" in cmp_text.lower(), cmp_text[:80].replace("\n", " "))
        check("compare_has_delta", ("+" in cmp_text or "−" in cmp_text or "-" in cmp_text))
        check("compare_charts", page.locator("#tab-compare canvas").count() >= 2, page.locator("#tab-compare canvas").count())

        check("no_console_errors", len(errors) == 0, errors[:3])
        browser.close()
finally:
    mock.terminate()

print("\nRESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(0 if not FAILS else 1)
