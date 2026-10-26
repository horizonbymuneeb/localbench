# LocalBench — build contract (authoritative)

Public URL: **http://127.0.0.1:21700** · backend `127.0.0.1:21700` · root `/opt/localbench` · python `/opt/localbench/.venv/bin/python`

Product: a public web tool where anyone drops in an OpenAI-compatible endpoint
(`base_url` + `api_key` + `model`) and gets one complete local-LLM benchmark:
TTFT, decode tok/s, prefill tok/s per context length, prompt processing time,
total response time, VRAM/RAM/GPU util/power/temp (via optional agent), model
load time, throughput at different batch (concurrency) sizes and context
lengths, plus saved labelled runs so MTP / speculative-decoding configs can be
compared side by side.

Hard rules:
- **No benchmark data is retained on the server.** Runs live in server RAM only
  while they execute and for a short TTL afterwards (`LOCALBENCH_TTL_S`, default
  900 s) so the client can finish reading them; then they are gone. No sqlite, no
  log lines with endpoints/labels/results, no data files. History and Compare are
  stored **client-side in the browser (IndexedDB)** — the visitor owns their data,
  the operator's server stores nothing.
- **The browser never calls the LLM endpoint directly** (CORS + key leaking).
  All model traffic goes through the backend, in memory only. Keys are never
  written to disk, never logged, never returned.
- No secrets in logs, no secrets in the DB, no secrets in SSE payloads.
- Runs are server-side background jobs: closing the tab must not kill a run.
- Language of UI copy: **English**. Chat replies: Roman Urdu (Lead only).

## Layout / ownership (do not write outside your scope)

| Path | Owner | Contents |
|---|---|---|
| `backend/` | backend | `app.py` (FastAPI routes+SSE), `engine.py` (measurement), `store.py` (sqlite), `agent.py` helpers, `requirements.txt` |
| `agent/bench_agent.py` | backend | standalone nvidia-smi telemetry agent (stdlib only, py3.8+) |
| `web/` | frontend | `index.html`, `app.js`, `styles.css`, `vendor/chart.umd.min.js` (already vendored) |
| `mock/mock_openai.py` | tester | deterministic fake OpenAI-compatible server |
| `tests/` | tester | pytest suite + e2e script |
| `deploy/` | Lead | nginx conf, systemd unit, deploy.sh |
| `data/` | Lead | `runs.db`, `presets.json` |

## REST API (JSON, `/api/...`)

### `POST /api/discover` → `{"ok":true,"models":[{"id":"...","context_length":131072|null,"owned_by":"..."}],"latency_ms":42}`
Body: `{"base_url":"http://host:8080/v1","api_key":"..."}`.
Server GETs `{base_url}/models` with `Authorization: Bearer <key>`; normalize
base_url (strip trailing `/`, append `/v1` if the user pasted a bare host:port).
Never echo the key back.

### `POST /api/runs` → `{"ok":true,"run_id":"r_ab12cd34","access_token":"rt_9f2c…"}`
`access_token` is a per-run secret (16-32 url-safe chars) that must accompany
every later call about that run — `X-Run-Token` header, or `?token=` for
`/events` (EventSource cannot set headers). Without it, run-scoped endpoints
behave as 404 (no existence oracle). It is the only way another visitor on the
same public instance could ever see a run's label/endpoint, so the UI keeps it
in `sessionStorage` and never persists it.
Body:
```json
{
  "label": "llama.cpp Qwen3-27B MTP draft=3",
  "base_url": "http://1.2.3.4:8080/v1",
  "api_key": "sk-...",
  "model": "qwen3-27b",
  "config": {
    "contexts": [1024, 4096, 8192, 16384, 32768, 65536],
    "concurrency": [1, 2, 4, 8],
    "concurrency_context": 2048,
    "max_tokens": 128,
    "repeats": 1,
    "warmup": 1,
    "idle_sample_s": 5,
    "enable_context_ladder": true,
    "enable_concurrency": true,
    "request_timeout_s": 600
  },
  "telemetry": {"mode": "auto"},
  "preset_id": "shared-llamacpp-server"   // optional: server resolves base_url/model/key from data/presets.json
}
```
Either supply `base_url`+`api_key`+`model`, or `preset_id` alone (server-side
credential presets are what make the public instance usable without a key).
When `preset_id` is used, the request may omit `api_key`/`model`; the server
fills them from the preset and never echoes the key.
`config` is optional; server fills defaults above. Validation: `base_url`
must be http(s); reject `169.254.0.0/16`, `::1`, `localhost`, `.internal`, and
non-http schemes (SSRF guard). Run is queued immediately; response returns at
once (no model call in the request handler). A finished run's payload is handed
to the client over SSE/GET and then dropped after the TTL; **nothing is
persisted server-side**.

### `GET /api/stats` → `{"ok":true,"active":0,"finished":0,"storage":"memory","ttl_s":900}`
Aggregate counters only (no labels, endpoints or ids) — replaces the old
`GET /api/runs` list, which is withdrawn for privacy: one visitor must not see
another visitor's runs. History is IndexedDB, client-side.
RunSummary = id, label, model, endpoint_host (host only), status, created_at,
finished_at, headline metrics `{ttft_ms, decode_tps, prompt_tps_4k, agg_tps_8}`|null,
`has_gpu`.

### `GET /api/runs/{id}` → `{"ok":true,"run":Run}`  (full Run, see schema)
Requires the run's `X-Run-Token`. Memory-only; 404 after the TTL or without a
valid token.
### `GET /api/runs/{id}/events?token=…` → **SSE** (`text/event-stream`)
Events (each `data: <json>\n\n`, plus a `: ping` comment every 10 s so a
Cloudflare-proxied connection does not time out):
```
{"type":"snapshot","run":Run}            // first event
{"type":"progress","progress":{...}}     // phase/step/pct/label
{"type":"point","kind":"context|concurrency","point":{...}}   // incremental
{"type":"telemetry","sample":{...}}      // thinned, ~4/s max
{"type":"log","level":"info|warn|error","message":"..."}
{"type":"done","run":Run}                // terminal
```
Server closes the stream after `done`. Events must be replayable: a client that
reconnects gets a fresh `snapshot` with everything so far.
### `POST /api/runs/{id}/cancel` (X-Run-Token) → `{"ok":true}`
### `DELETE /api/runs/{id}` (X-Run-Token) → `{"ok":true}` (also evicts from memory)
### `POST /api/runs/{id}/telemetry` — agent ingest, header `X-Agent-Key: <key>`
(the agent key is minted for that run only and printed solely in the agent command)
Body: `{"samples":[{"ts":1699999999.1,"vram_mb":18122,"ram_mb":4210,"gpu_util":97,"power_w":248.5,"temp_c":71,"phase":"context:8192"}]}`
Server keeps ≤4000 samples/run (downsample by bucket-max for peaks), computes
idle/peak/avg, and broadcasts thinned `telemetry` events. `GET
/api/runs/{id}/agent` → `{"ok":true,"agent_key":"...","command":"curl -sO .../bench_agent.py && python3 bench_agent.py --server http://127.0.0.1:21700 --run r_.. --key ...","agent_connected":false}`.
### `GET /api/runs/{id}/export?format=json|csv` (X-Run-Token) → attachment download
Works while the run is still in memory (live or within the TTL); 404 afterwards.
The UI also builds JSON/CSV client-side from its IndexedDB copy so exports keep
working for old runs.
### `GET /api/presets` → `{"ok":true,"presets":[{"id","name","base_url","model","note","has_credential"}]}` from `data/presets.json`
`id` is the entry's `name` slugified. `has_credential:true` means the preset
carries a **server-side** `api_key` in `data/presets.json` (file mode 600, never
returned by any endpoint); such a preset lets any visitor benchmark the
operator's shared endpoint without ever seeing or pasting the key.
### `GET /api/config` → `{"ok":true,"version":"...","gpu_agent_enabled":true,"limits":{"max_concurrent_runs":2,"max_ctx":131072}}`
### `GET /healthz` → `{"ok":true,"runs":N,"uptime_s":N}` (no auth, used by nginx/uptime)

Global: max 2 runs executing at once, rest queued (`status:"queued"`).
Abuse guard on `POST /api/runs`: max 6 runs per 10 min per client IP
(`X-Forwarded-For` first hop), 429 `{ok:false,error:"rate_limited","retry_after":n}`
otherwise; limits configurable via env `LOCALBENCH_RATE_LIMIT` (`6/600`).

## Run / result schema (exact field names — frontend codes against this)

```jsonc
{
  "id": "r_ab12cd34",
  "label": "…", "model": "qwen3-27b", "endpoint_host": "1.2.3.4:8080",
  "preset_id": null,                      // set when the run used a server-credential preset
  "status": "queued|running|done|failed|cancelled",
  "created_at": 1699999999.0, "started_at": null, "finished_at": null,
  "error": null,
  "config": { …echoed config… },
  "progress": {"phase":"overhead|context|concurrency|finish","step":3,"total":12,"pct":25,"label":"context 8192 / 6"},
  "overhead": {"ping_ms_p50":18.0,"samples":5},
  "results": {
    "ttft_ms":   {"mean":0,"p50":0,"p95":0,"min":0,"max":0,"n":0},
    "decode_tps":{"mean":0,"p50":0,"p95":0,"min":0,"max":0,"n":0},
    "prompt_tps":{"mean":0,"p50":0,"p95":0,"min":0,"max":0,"n":0},
    "total_s":   {"mean":0,"p50":0,"p95":0,"min":0,"max":0,"n":0},
    "tokens": {"prompt":0,"completion":0},
    "context_points": [{
      "ctx_requested": 8192, "prompt_tokens": 8211, "max_tokens": 128,
      "ttft_ms": 812.0, "ttft_corrected_ms": 794.0, "prompt_ms": 794.0,
      "prefill_tps": 10340.0, "decode_tps": 41.2, "total_s": 4.9,
      "completion_tokens": 128, "ok": true, "error": null,
      "prefill_source": "server|derived|unreliable",
      "prefill_tps_server": 10880.0,           // llama.cpp timings.prompt_per_second, else null
      "prompt_ms_server": 755.0,               // llama.cpp timings.prompt_ms, else null
      "cache_bust": true,
      "draft": {"n": 96, "accepted": 61, "acceptance_rate": 0.635} | null
    }],
    "concurrency_points": [{
      "n": 4, "prompt_tokens": 2048, "wall_s": 6.1, "agg_tps": 168.0,
      "per_req_tps": 41.0, "ttft_p50": 900.0, "ttft_p95": 1400.0,
      "completion_tokens": 512, "ok": true, "error": null
    }],
    "gpu": {
      "available": true,
      "idle": {"vram_mb":0,"ram_mb":0,"gpu_util":0,"power_w":0,"temp_c":0},
      "peak": {"vram_mb":0,"ram_mb":0,"gpu_util":0,"power_w":0,"temp_c":0},
      "avg":  {"vram_mb":0,"ram_mb":0,"gpu_util":0,"power_w":0,"temp_c":0},
      "samples_n": 0
    },
    "model_load_s": null,
    "notes": ["GPU telemetry: no agent connected"]
  },
  "agent": {"key": null, "connected": false}   // key only in /agent response
}
```

### Storage model (authoritative)

| Where | What | Lifetime |
|---|---|---|
| Server RAM | active + recently finished runs, telemetry batches, agent keys | TTL 900 s after terminal, then freed (`LOCALBENCH_TTL_S`); access-guarded by the per-run token |
| Browser IndexedDB | every run the visitor wants to keep (full payload incl. context/concurrency points, gpu tiles, config, label) | until the visitor deletes it / clears site data |
| Server disk | **nothing** — no `runs.db`, no exports, no endpoint/label logs | — |

`LOCALBENCH_PERSIST=1` exists only for the test suite; production runs with
default `0` and `data/runs.db` must not exist.

## Measurement definitions (single source of truth — engine + tests must match)

- Timing uses `time.perf_counter()`; `ttft_ms` = request body sent → first
  streamed chunk carrying non-empty `choices[0].delta.content` (or first
  tool/reasoning delta if that arrives earlier — count it as first token).
- `overhead.ms` = p50 of 5 sequential tiny requests (`max_tokens=1`, 1-word
  prompt) sent before the suite; `ttft_corrected_ms = ttft_ms - ping_ms_p50`,
  floored at 0.
- `prompt_ms = max(ttft_corrected_ms, 0.5)` (never divide by ~0);
  `prefill_tps = prompt_tokens / (prompt_ms/1000)` using `usage.prompt_tokens`
  (fallback: `len(prompt)/4`). **`prefill_source`** must say where the number
  comes from and must never publish a link-noise artifact:
  - `"server"` — llama.cpp `timings.prompt_per_second` present → use it as
    `prefill_tps`, also store `prefill_tps_server`/`prompt_ms_server`;
  - `"derived"` — corrected client TTFT is credible
    (`ttft_corrected_ms >= 2.0` and `>= 5%` of raw `ttft_ms`) → formula above;
  - `"unreliable"` — correction collapsed the interval (fast mock/server, slow
    link): `prefill_tps = null`, `prefill_tps_server` may still be set. Charts
    drop nulls; the UI shows `n/a`. Never emit values > 1e6 tok/s.
- `decode_tps = (completion_tokens - 1) / ((t_last_token - t_first_token)/1000)`,
  `completion_tokens` from `usage.completion_tokens`, else count of content
  deltas.
- **Decode measurements need room.** Every context/concurrency prompt ends with
  a tail instruction that forces long generation (`"Write a long, continuous,
  plain-English paragraph of at least 250 words about computer hardware. Do not
  stop early. Do not use lists, headings or code."`), never with
  "reply with one word". If `completion_tokens < 8` the decode rate is not
  measurable → `decode_tps: null` (plus note), never `0.0`.
- **Tokenizer calibration.** Before the ladder, the warmup request (512-token
  filler) reports `usage.prompt_tokens`. Derive `chars_per_token =
  len(warmup_prompt) / warmup_prompt_tokens` for this model's tokenizer and scale
  every rung prompt so actual `prompt_tokens` lands within ±10% of the rung
  (`ctx_requested`); re-check and rescale once more after the first rung if it is
  off by >15%. `prompt_tokens` is always the reality the UI plots.
- Synthetic prompt for a target context: deterministic filler of **token-dense
  words** (~1 token per 4.2 chars, e.g. `the of and to in a is that for it be`)
  scaled to the rung, ending with the long-generation tail instruction above,
  and every point uses `max_tokens` from config (128 default). Every prompt starts
  with a unique cache-busting nonce (`"Benchmark run <uuid> rung <n>. "
  <n random letters>.` — ~30–60 random tokens, different on every request) so a
  server-side prompt/prefix cache cannot fake the prefill time; record
  `cache_bust: true`. Actual token count comes from `usage.prompt_tokens`;
  `ctx_requested` is the ladder rung and `prompt_tokens` is the truth the UI
  plots.
- Context rung handling: if the endpoint 400s with a context-length error,
  mark `ok:false, error:"context_exceeded"` and skip higher rungs; if
  `/v1/models` exposes `context_length`, skip rungs above it with
  `error:"exceeds_model_context"`.
- Concurrency: `n` identical requests in parallel at `concurrency_context`
  tokens; `wall_s` = start of slowest → all finished; `agg_tps = total
  completion tokens / wall_s`; per-request decode tps averaged.
- llama.cpp `timings`/`timings_per_token` in the JSON body (also present in the
  final SSE chunk of recent builds) → map to `draft` stats: `draft_n`,
  `draft_n_accepted`, `acceptance_rate`. Absent → `draft: null`.
- Warmup: `warmup` requests before the ladder are not recorded.

## Frontend requirements

Single page, three tabs: **Run · History · Compare**. English copy, dark
instrument-panel aesthetic (design tokens in `web/styles.css`; no purple
gradients, no emoji, tabular numerics, left-aligned labels, right-aligned
numbers). Must work at 390 px and 1440 px with zero horizontal overflow.

- Run tab: endpoint form (base URL, API key with show/hide, model dropdown
  populated by `POST /api/discover` with manual override, label, advanced
  accordion for contexts/concurrency/max_tokens/repeats), preset chips from
  `/api/presets`, big **Run benchmark** button, then a live panel: progress bar
  + per-point rows appearing as they finish (chart: TTFT vs context, prefill
  tok/s vs context, decode tok/s), GPU telemetry sparkline + idle/peak/avg
  tiles when agent connected, agent attach box with copy-able one-liner,
  cancel button, result tiles (TTFT p50, decode tok/s, prefill @4K, total time,
  tokens), context table, concurrency table, export JSON/CSV, "Save to
  history" is automatic.
- History tab: table of runs **from IndexedDB in the visitor's browser** (label,
  model, endpoint host, TTFT p50, decode tok/s, created), open/delete, clear-all,
  multi-select → Compare, storage-size indicator, plus a short line: "Runs are
  stored in this browser only — nothing is kept on the server."
- Compare tab: side-by-side table across selected runs (all from IndexedDB), overlaid charts
  (TTFT vs context, prefill vs context, decode, agg tok/s vs concurrency),
  delta column vs baseline (first selected). This is the MTP-combination
  workflow: run the same endpoint with different survivor/draft settings,
  label each run, compare.
- Polling fallback: if `EventSource` fails, poll `GET /api/runs/{id}` every 2 s.
- Accessibility: labels tied to inputs, focus states, `prefers-reduced-motion`.

## Agent contract (`agent/bench_agent.py`, stdlib only)

```
python3 bench_agent.py --server http://127.0.0.1:21700 --run r_xxx --key KEY
    [--interval 0.25] [--restart-cmd "systemctl --user restart llama-server"]
    [--ready-url http://127.0.0.1:8080/v1/models] [--watch 0]
```
Samples `nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu,power.draw,temperature.gpu --format=csv,noheader,nounits`
plus `free -m` for RAM (used MB of the host), POSTs every ~1 s in batches,
exits when the run reaches a terminal status (or `--watch` seconds elapse).
`--restart-cmd` + `--ready-url`: measure seconds from command exit to the
readiness URL answering 200 → report `model_load_s` via `POST
/api/runs/{id}/telemetry` with `{"model_load_s": 12.4}`. Never crash on a
missing nvidia-smi (print a clear message, exit 2).

## Testing / acceptance (tester owns proof)

1. `mock/mock_openai.py` — deterministic fake server: `/v1/models`, streaming
   and non-streaming `/v1/chat/completions`, honours a `--ttft-ms`,
   `--tps`, `--ctx` and returns llama.cpp-style `timings` incl. `draft_n`.
2. pytest: engine math (prefill = prompt/ttft, decode formula), context ladder
   skip-on-error, concurrency aggregation, SSRF guard, run lifecycle via
   FastAPI TestClient against the mock, agent ingest + aggregates, export CSV.
3. e2e: start uvicorn + mock, POST run, follow SSE, assert all points present
   and `status=done`, run twice with different labels → compare endpoint data.
4. Browser check (Lead + tester): headless Chromium, 1440×980 and 390×844,
   assert `document.documentElement.scrollWidth - window.innerWidth === 0`,
   zero console errors, screenshots saved to `tests/screenshots/`.
