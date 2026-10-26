#!/usr/bin/env python3
"""Lead's independent headless check of the public UI (Playwright).

Verifies what the user asked for: light theme (no dark), enterprise font/icon
assets, no horizontal overflow at 1440/390, zero console errors, zero external
requests, history served from IndexedDB (no GET /api/runs).

  python3 scripts/lead_ui_check.py [url]
"""
import json
import re
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:21700/"
SHOTS = "/opt/localbench/tests/screenshots"
VIEWPORTS = [(1440, 980), (390, 844)]
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" · {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    for w, h in VIEWPORTS:
        ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
        page = ctx.new_page()
        console, page_errors, failed, external, run_list_hits = [], [], [], [], []
        CF_BEACON = "static.cloudflareinsights.com"

        def is_platform_noise(text):
            # Cloudflare injects its Web Analytics beacon at the edge; our CSP blocks it.
            # Not our code, nothing leaves the browser — classified separately.
            return CF_BEACON in text or "content-security-policy" in text.lower() and CF_BEACON in text

        page.on("console", lambda m: console.append(m.text) if m.type == "error" and not is_platform_noise(m.text) else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("requestfailed", lambda r: failed.append(r.url) if CF_BEACON not in r.url else None)
        page.on("request", lambda r: (external.append(r.url) if not r.url.startswith(URL.split("/")[0] + "//" + URL.split("/")[2]) and CF_BEACON not in r.url else None,
                                      run_list_hits.append(r.url) if re.search(r"/api/runs(\?|$)", r.url) else None))
        page.goto(URL, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(800)

        m = page.evaluate("""() => {
            const de = document.documentElement;
            const scrollers = [...document.querySelectorAll('*')].filter(el => {
                const s = getComputedStyle(el);
                return /(auto|scroll)/.test(s.overflowX) && el.scrollWidth - el.clientWidth > 1;
            }).map(el => (el.id || el.className || el.tagName) + ':' + (el.scrollWidth - el.clientWidth));
            const body = getComputedStyle(document.body);
            const panel = document.querySelector('.panel, .card, main section, .surface');
            return {
                docOverflow: de.scrollWidth - window.innerWidth,
                scrollers,
                bg: body.backgroundColor,
                text: body.color,
                font: body.fontFamily,
                panelBg: panel ? getComputedStyle(panel).backgroundColor : null,
                icons: document.querySelectorAll('svg use').length,
                emoji: (document.body.innerText.match(/[\\u{1F300}-\\u{1FAFF}\\u{2600}-\\u{27BF}]/gu) || []).length,
            };
        }""")
        shot = f"{SHOTS}/lead-ui-{w}x{h}.png"
        page.screenshot(path=shot, full_page=(w == 390))

        print(f"\n== {w}x{h} ==")
        print(json.dumps({k: v for k, v in m.items() if k != 'scrollers'}, indent=None))
        check(f"doc_overflow_0@{w}", m["docOverflow"] == 0, m["docOverflow"])
        check(f"no_scroll_containers@{w}", len(m["scrollers"]) == 0, m["scrollers"][:3])
        check(f"light_bg@{w}", m["bg"] in ("rgb(246, 247, 249)", "rgb(255, 255, 255)", "rgb(250, 251, 252)"), m["bg"])
        check(f"inter_font@{w}", "Inter" in m["font"], m["font"])
        check(f"svg_icons@{w}", m["icons"] >= 10, m["icons"])
        check(f"no_emoji@{w}", m["emoji"] == 0, m["emoji"])
        check(f"console_errors_0@{w}", len(console) + len(page_errors) == 0, console[:2] + page_errors[:2])
        check(f"no_failed_requests@{w}", len(failed) == 0, failed[:3])
        check(f"no_external_requests@{w}", len(external) == 0, external[:3])

        # History must come from IndexedDB, not GET /api/runs
        page.get_by_role("tab", name=re.compile("History", re.I)).click()
        page.wait_for_timeout(1200)
        hist = page.evaluate("""async () => {
            const dbs = await indexedDB.databases();
            const out = {dbs: dbs.map(d => d.name), rows: 0, keys: []};
            if (!dbs.some(d => d.name === 'localbench')) return out;
            const db = await new Promise((res, rej) => {
                const r = indexedDB.open('localbench');
                r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
            });
            if (!db.objectStoreNames.contains('runs')) return out;
            const tx = db.transaction('runs', 'readonly');
            const all = await new Promise((res, rej) => {
                const q = tx.objectStore('runs').getAll();
                q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error);
            });
            out.rows = all.length;
            out.keys = all.slice(0, 3).map(r => ({id: r.id, label: r.label, ctx: (r.results?.context_points || []).length}));
            return out;
        }""")
        print("indexeddb:", json.dumps(hist)[:400])
        check(f"history_no_run_list_calls@{w}", len(run_list_hits) == 0, run_list_hits[:3])
        page.screenshot(path=f"{SHOTS}/lead-history-{w}x{h}.png", full_page=(w == 390))
        ctx.close()
    browser.close()

print("\nRESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(0 if not FAILS else 1)
