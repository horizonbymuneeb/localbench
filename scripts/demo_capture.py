#!/usr/bin/env python3
"""Generate demo runs through the real pipeline (mock endpoint + fake nvidia-smi
agent), seed them into the browser's IndexedDB and capture README screenshots.

  python3 scripts/demo_capture.py
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = str(Path(__file__).resolve().parent.parent)
API = "http://127.0.0.1:21700"
OUT = f"{ROOT}/docs/screenshots"
TMP = "/tmp/lb_demo"
FAKEBIN = f"{TMP}/fakebin"
CONTEXTS = [1024, 4096, 8192, 16384, 32768]
CONCURRENCY = [1, 2, 4, 8]

os.makedirs(OUT, exist_ok=True)
os.makedirs(FAKEBIN, exist_ok=True)
shutil.copy(f"{ROOT}/mock/mock_openai.py", f"{TMP}/mock_openai.py")

FAKE_SMI = """#!/usr/bin/env bash
R=$((RANDOM % 9))
echo "$((21380 + R * 7)), 32760, $((96 + R % 4)), $((438 + R * 6)).3, $((63 + R % 7))"
"""
with open(f"{FAKEBIN}/nvidia-smi", "w") as f:
    f.write(FAKE_SMI)
os.chmod(f"{FAKEBIN}/nvidia-smi", 0o755)

SPECS = [
    dict(label="Qwen3-8B Q4_K_M · MTP off", model="qwen3-8b-q4-k-m",
         tps=540, prefill=9800, draft=(0, 0), port=28941),
    dict(label="Qwen3-8B Q4_K_M · MTP draft=3", model="qwen3-8b-q4-k-m",
         tps=1040, prefill=10600, draft=(96, 68), port=28942),
    dict(label="Kimi-K3 FP8 · vLLM", model="kimi-k3-fp8",
         tps=620, prefill=8100, draft=(0, 0), port=28943),
]


def post_run(spec):
    body = {
        "label": spec["label"], "base_url": f"http://127.0.0.1:{spec['port']}/v1",
        "api_key": "sk-demo", "model": spec["model"],
        "config": {"contexts": CONTEXTS, "concurrency": CONCURRENCY,
                   "concurrency_context": 2048, "max_tokens": 256, "warmup": 1,
                   "repeats": 1, "idle_sample_s": 4, "request_timeout_s": 180},
    }
    out = subprocess.run(["curl", "-s", "-X", "POST", f"{API}/api/runs",
                          "-H", "Content-Type: application/json", "-d", json.dumps(body)],
                         capture_output=True, text=True).stdout
    return json.loads(out)


runs = []
for spec in SPECS:
    mock = subprocess.Popen(
        [f"{ROOT}/.venv/bin/python", f"{TMP}/mock_openai.py", "--port", str(spec["port"]),
         "--ttft-ms", "26", "--tps", str(spec["tps"]), "--prefill-tps", str(spec["prefill"]),
         "--ctx", "131072", "--draft-n", str(spec["draft"][0]),
         "--draft-accepted", str(spec["draft"][1]),
         "--model", spec["model"]],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    try:
        created = post_run(spec)
        run_id, token = created["run_id"], created["access_token"]
        print(f"[{spec['label']}] run {run_id}")
        agent = subprocess.Popen(
            [f"{ROOT}/.venv/bin/python", f"{ROOT}/agent/bench_agent.py",
             "--server", API, "--run", run_id, "--key", "ak_" + token[-12:],
             "--interval", "0.25", "--watch", "240"],
            env={**os.environ, "PATH": FAKEBIN + ":/usr/bin:/bin"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # the agent needs the real key: fetch it with the run token
        agent.terminate()
        ag = subprocess.run(["curl", "-s", "-H", f"X-Run-Token: {token}",
                             f"{API}/api/runs/{run_id}/agent"], capture_output=True, text=True).stdout
        agent_key = json.loads(ag)["agent_key"]
        agent = subprocess.Popen(
            [f"{ROOT}/.venv/bin/python", f"{ROOT}/agent/bench_agent.py",
             "--server", API, "--run", run_id, "--key", agent_key,
             "--interval", "0.25", "--watch", "240",
             "--restart-cmd", "sleep 9", "--ready-url", f"http://127.0.0.1:{spec['port']}/v1/models"],
            env={**os.environ, "PATH": FAKEBIN + ":/usr/bin:/bin"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(120):
            time.sleep(1)
            st = subprocess.run(["curl", "-s", "-H", f"X-Run-Token: {token}",
                                 f"{API}/api/runs/{run_id}"], capture_output=True, text=True).stdout
            try:
                status = json.loads(st)["run"]["status"]
            except Exception:
                continue
            if status in ("done", "failed"):
                break
        run = json.loads(subprocess.run(["curl", "-s", "-H", f"X-Run-Token: {token}",
                                         f"{API}/api/runs/{run_id}"],
                                        capture_output=True, text=True).stdout)["run"]
        print("   ->", run["status"], "decode", run["results"]["decode_tps"].get("p50"),
              "ctx", len(run["results"]["context_points"]), "gpu", run["results"]["gpu"]["available"])
        run["saved_at"] = time.time()
        runs.append(run)
        agent.terminate()
    finally:
        mock.terminate()

with open(f"{TMP}/runs.json", "w") as f:
    json.dump(runs, f)

# ---------------------------------------------------------------- screenshots
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:21700/"


def seed(page, runs):
    page.evaluate("""async (runs) => {
        await new Promise((res) => {
            const req = indexedDB.deleteDatabase('localbench');
            req.onsuccess = req.onerror = req.onblocked = () => res();
        });
        const db = await new Promise((res, rej) => {
            const r = indexedDB.open('localbench', 1);
            r.onupgradeneeded = () => {
                const d = r.result;
                if (!d.objectStoreNames.contains('runs')) {
                    const s = d.createObjectStore('runs', {keyPath: 'id'});
                    s.createIndex('created_at', 'created_at');
                }
            };
            r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
        });
        const tx = db.transaction('runs', 'readwrite');
        for (const run of runs) tx.objectStore('runs').put(run);
        await new Promise((res) => { tx.oncomplete = res; tx.onerror = res; });
        return runs.length;
    }""", runs)


with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    for w, h, tag in [(1440, 980, "desktop"), (390, 844, "mobile")]:
        ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2)
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(600)
        seed(page, runs)
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(800)

        # history
        page.click("#tabbtn-history")
        page.wait_for_timeout(900)
        page.screenshot(path=f"{OUT}/history-{tag}.png", full_page=(tag == "mobile"))

        # open the MTP run -> run view
        page.locator("#history-body tr").filter(has_text="MTP draft=3").first.get_by_role("button", name="Open").click()
        page.wait_for_timeout(1200)
        page.screenshot(path=f"{OUT}/run-{tag}.png", full_page=(tag == "mobile"))

        # compare baseline vs MTP
        page.click("#tabbtn-history")
        page.wait_for_timeout(500)
        boxes = page.locator('#history-body input[type="checkbox"]')
        picked = 0
        for i in range(boxes.count()):
            row = page.locator("#history-body tr").nth(i)
            if "Qwen3-8B" in row.inner_text():
                boxes.nth(i).check()
                picked += 1
                if picked == 2:
                    break
        page.click("#history-compare")
        page.wait_for_timeout(1400)
        page.screenshot(path=f"{OUT}/compare-{tag}.png", full_page=(tag == "mobile"))
        ctx.close()
    browser.close()

print("screenshots ->", sorted(os.listdir(OUT)))
