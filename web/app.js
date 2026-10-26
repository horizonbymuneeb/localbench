/* ==========================================================================
   LocalBench — LLM benchmark console
   Vanilla ES2022 module. No framework, no build step.

   Rendering is DOM-node based (textContent only) — user strings can never
   become markup. Missing numbers stay null and render as "n/a": the UI never
   invents a 0 for an unmeasured value.
   ========================================================================== */

/* ------------------------------- DOM helpers ------------------------------ */

const $id = (id) => document.getElementById(id);
const $$ = (sel, root = document) => Array.from((root || document).querySelectorAll(sel));
const SVG_NS = 'http://www.w3.org/2000/svg';

function clear(node) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Create an element. Children are nodes or text — never HTML strings. */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = String(value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (typeof value === 'function' && key.startsWith('on')) {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

/** Icon from the inline sprite: <svg class="icon"><use href="#i-name"></svg>. */
function icon(name, size = 16) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

/* ------------------------------- formatting ------------------------------ */

const NF = new Intl.NumberFormat('en-US');
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

const fmtInt = (v) => (isNum(v) ? NF.format(Math.round(v)) : 'n/a');

function fmtMs(v) {
  if (!isNum(v)) return 'n/a';
  const a = Math.abs(v);
  if (a >= 1000) return NF.format(Math.round(v));
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  return v.toFixed(2);
}

function fmtTps(v) {
  if (!isNum(v)) return 'n/a';
  const a = Math.abs(v);
  if (a >= 1000) return NF.format(Math.round(v));
  if (a >= 100) return v.toFixed(1);
  return v.toFixed(2);
}

function fmtSec(v) {
  if (!isNum(v)) return 'n/a';
  return Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(2);
}

function fmtMBShort(v) {
  if (!isNum(v)) return 'n/a';
  return v >= 1024 ? (v / 1024).toFixed(1) + ' GB' : Math.round(v) + ' MB';
}

function fmtW(v) {
  if (!isNum(v)) return 'n/a';
  return v >= 100 ? Math.round(v) + ' W' : v.toFixed(1) + ' W';
}

function fmtTemp(v) {
  return isNum(v) ? Math.round(v) + ' °C' : 'n/a';
}

function fmtUtil(v) {
  return isNum(v) ? Math.round(v) + '%' : 'n/a';
}

/** Compact chart-axis label: 0, 5, 50, 100, 10,000 — never 0.00 or 50.0 */
function fmtAxis(v) {
  if (!isNum(v)) return '';
  if (v === 0) return '0';
  const a = Math.abs(v);
  if (a >= 1000) return NF.format(Math.round(v));
  if (a >= 10) return String(Math.round(v));
  if (Number.isInteger(v)) return String(v);
  return v.toFixed(1);
}

/** 4096 -> 4K, 1024 -> 1K, 65536 -> 64K */
function fmtCtx(v) {
  if (!isNum(v)) return 'n/a';
  if (v >= 1024) {
    const k = v / 1024;
    return (Number.isInteger(k) ? k : k.toFixed(1)) + 'K';
  }
  return String(Math.round(v));
}

const pad2 = (n) => String(n).padStart(2, '0');

function fmtWhen(ts) {
  if (!isNum(ts) || ts <= 0) return 'n/a';
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return 'n/a';
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(
    d.getHours()
  )}:${pad2(d.getMinutes())}`;
}

function fmtClock(ts) {
  const d = isNum(ts) ? new Date(ts * 1000) : new Date();
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

function fmtAgo(ts) {
  if (!isNum(ts) || ts <= 0) return '';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function fmtDelta(diff, digits = 1) {
  const sign = diff > 0 ? '+' : '';
  if (Math.abs(diff) >= 1000) return sign + NF.format(Math.round(diff));
  return sign + diff.toFixed(digits);
}

/** Full label for tooltips, clipped label for dense cells. */
function shortLabel(run, max = 28) {
  const text = run.label || run.id;
  return text.length > max ? `${text.slice(0, max - 1)}\u2026` : text;
}

/* --------------------------- theme / chart tokens -------------------------- */

function readTheme() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name, fallback) => (cs.getPropertyValue(name) || '').trim() || fallback;
  return {
    text: v('--text', '#101828'),
    secondary: v('--secondary', '#475467'),
    muted: v('--muted', '#667085'),
    faint: v('--faint', '#98a2b3'),
    hairline: v('--hairline', '#e3e6ea'),
    hairlineStrong: v('--hairline-strong', '#cfd5dc'),
    panel: v('--panel', '#ffffff'),
    subtle: v('--subtle', '#fafbfc'),
    hover: v('--hover', '#f3f5f8'),
    bg: v('--bg', '#f6f7f9'),
    grid: v('--grid', '#eaecf0'),
    axis: v('--axis', '#667085'),
    fontMono: v('--font-mono', 'monospace'),
    metric: {
      ttft: v('--m-ttft', '#0b66d0'),
      prefill: v('--m-prefill', '#12805c'),
      decode: v('--m-decode', '#9a6700'),
      agg: v('--m-agg', '#98a2b3'),
    },
    gpu: {
      vram: v('--g-vram', '#0b66d0'),
      util: v('--g-util', '#12805c'),
      power: v('--g-power', '#9a6700'),
      temp: v('--g-temp', '#b42318'),
    },
  };
}

const THEME = readTheme();
const HAS_CHART = typeof window.Chart === 'function';

if (HAS_CHART) {
  window.Chart.defaults.animation = false;
  window.Chart.defaults.font.family = THEME.fontMono;
  window.Chart.defaults.font.size = 10;
  window.Chart.defaults.color = THEME.axis;
  window.Chart.defaults.borderColor = THEME.grid;
}

/* series colours for multi-run comparison (token-consistent, one accent) */
const RUN_COLORS = ['#0b66d0', '#12805c', '#9a6700', '#b42318', '#98a2b3', '#475467'];
const runColor = (i) => RUN_COLORS[i % RUN_COLORS.length];

/* ------------------------------- normalizers -------------------------------
   The backend contract is authoritative. Absent values stay null and render
   as "n/a" — never 0.
   -------------------------------------------------------------------------- */

function nv(v) {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v);
  return null;
}

const sv = (v) => (typeof v === 'string' ? v : '');
const obj = (v) => (v && typeof v === 'object' && !Array.isArray(v) ? v : {});
const arr = (v) => (Array.isArray(v) ? v : []);

function normalizeStat(s) {
  if (!s || typeof s !== 'object') return null;
  const out = {
    mean: nv(s.mean),
    p50: nv(s.p50),
    p95: nv(s.p95),
    min: nv(s.min),
    max: nv(s.max),
    n: nv(s.n) ?? 0,
  };
  if (out.mean === null && out.p50 === null && out.p95 === null) return null;
  // all-zero placeholder with n=0 = "not measured yet", not a 0 ms result
  if (out.n === 0 && !out.mean && !out.p50 && !out.p95) return null;
  return out;
}

function normalizeDraft(d) {
  if (!d || typeof d !== 'object') return null;
  const n = nv(d.n);
  const accepted = nv(d.accepted);
  let rate = nv(d.acceptance_rate);
  if (rate === null && n && accepted !== null && n > 0) rate = accepted / n;
  if (n === null && accepted === null && rate === null) return null;
  return { n, accepted, acceptance_rate: rate };
}

function normalizeContextPoint(p) {
  const raw = obj(p);
  const source = sv(raw.prefill_source);
  return {
    ctx_requested: nv(raw.ctx_requested),
    prompt_tokens: nv(raw.prompt_tokens),
    max_tokens: nv(raw.max_tokens),
    ttft_ms: nv(raw.ttft_ms),
    ttft_corrected_ms: nv(raw.ttft_corrected_ms),
    prompt_ms: nv(raw.prompt_ms),
    prefill_tps: nv(raw.prefill_tps),
    prefill_source:
      source === 'server' || source === 'derived' || source === 'unreliable' ? source : null,
    prefill_tps_server: nv(raw.prefill_tps_server),
    prompt_ms_server: nv(raw.prompt_ms_server),
    cache_bust: raw.cache_bust === true,
    decode_tps: nv(raw.decode_tps),
    total_s: nv(raw.total_s),
    completion_tokens: nv(raw.completion_tokens),
    ok: raw.ok !== false,
    error: typeof raw.error === 'string' && raw.error ? raw.error : null,
    draft: normalizeDraft(raw.draft),
  };
}

function normalizeConcurrencyPoint(p) {
  const raw = obj(p);
  return {
    n: nv(raw.n),
    prompt_tokens: nv(raw.prompt_tokens),
    wall_s: nv(raw.wall_s),
    agg_tps: nv(raw.agg_tps),
    per_req_tps: nv(raw.per_req_tps),
    ttft_p50: nv(raw.ttft_p50),
    ttft_p95: nv(raw.ttft_p95),
    completion_tokens: nv(raw.completion_tokens),
    ok: raw.ok !== false,
    error: typeof raw.error === 'string' && raw.error ? raw.error : null,
  };
}

const GPU_FIELDS = ['vram_mb', 'ram_mb', 'gpu_util', 'power_w', 'temp_c'];

function normalizeGpuSample(s) {
  const raw = obj(s);
  const out = {};
  let any = false;
  for (const f of GPU_FIELDS) {
    out[f] = nv(raw[f]);
    if (out[f] !== null) any = true;
  }
  return any ? out : null;
}

function normalizeGpu(g) {
  const raw = obj(g);
  const readSide = (side) => {
    const s = obj(side);
    const out = {};
    for (const f of GPU_FIELDS) out[f] = nv(s[f]);
    return out;
  };
  const hasNumbers = (side) => GPU_FIELDS.some((f) => side[f] !== null);
  const idle = readSide(raw.idle);
  const peak = readSide(raw.peak);
  const avg = readSide(raw.avg);
  const declared = typeof raw.available === 'boolean' ? raw.available : null;
  const available = declared !== null ? declared : hasNumbers(idle) || hasNumbers(peak) || hasNumbers(avg);
  return { available, idle, peak, avg, samples_n: nv(raw.samples_n) ?? 0 };
}

function normalizeProgress(p) {
  const raw = obj(p);
  const total = nv(raw.total) ?? 0;
  let pct = nv(raw.pct);
  if (pct !== null && pct > 0 && pct <= 1 && total > 1) pct *= 100;
  if (pct === null && total > 0) pct = ((nv(raw.step) ?? 0) / total) * 100;
  pct = pct === null ? 0 : Math.max(0, Math.min(100, pct));
  return {
    phase: sv(raw.phase) || 'queued',
    step: nv(raw.step) ?? 0,
    total,
    pct,
    label: sv(raw.label) || sv(raw.phase) || 'queued',
  };
}

const RUN_STATUSES = new Set(['queued', 'running', 'done', 'failed', 'cancelled']);
const normalizeStatus = (s) => (RUN_STATUSES.has(s) ? s : 'unknown');

function normalizeRun(rawRun) {
  const raw = obj(rawRun);
  const results = obj(raw.results);
  const tokens = obj(results.tokens);
  const agent = obj(raw.agent);
  return {
    id: sv(raw.id),
    label: sv(raw.label),
    model: sv(raw.model),
    endpoint_host: sv(raw.endpoint_host),
    preset_id: sv(raw.preset_id) || null,
    status: normalizeStatus(raw.status),
    created_at: nv(raw.created_at),
    started_at: nv(raw.started_at),
    finished_at: nv(raw.finished_at),
    error: typeof raw.error === 'string' && raw.error ? raw.error : null,
    config: obj(raw.config),
    progress: normalizeProgress(raw.progress),
    overhead: {
      ping_ms_p50: nv(obj(raw.overhead).ping_ms_p50),
      samples: nv(obj(raw.overhead).samples) ?? 0,
    },
    results: {
      ttft_ms: normalizeStat(results.ttft_ms),
      decode_tps: normalizeStat(results.decode_tps),
      prompt_tps: normalizeStat(results.prompt_tps),
      total_s: normalizeStat(results.total_s),
      tokens: { prompt: nv(tokens.prompt), completion: nv(tokens.completion) },
      context_points: arr(results.context_points).map(normalizeContextPoint),
      concurrency_points: arr(results.concurrency_points).map(normalizeConcurrencyPoint),
      gpu: normalizeGpu(results.gpu),
      model_load_s: nv(results.model_load_s),
      notes: arr(results.notes).filter((n) => typeof n === 'string'),
    },
    agent: {
      key: typeof agent.key === 'string' ? agent.key : null,
      connected: Boolean(agent.connected),
    },
  };
}

function headlineValue(v) {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (v && typeof v === 'object') return nv(v.p50) ?? nv(v.mean) ?? null;
  return null;
}

function normalizeSummary(rawSummary) {
  const raw = obj(rawSummary);
  const hl = obj(raw.headline || raw.headline_metrics || {});
  return {
    id: sv(raw.id),
    label: sv(raw.label),
    model: sv(raw.model),
    endpoint_host: sv(raw.endpoint_host),
    preset_id: sv(raw.preset_id) || null,
    status: normalizeStatus(raw.status),
    created_at: nv(raw.created_at),
    finished_at: nv(raw.finished_at),
    ttft_ms: headlineValue(hl.ttft_ms ?? raw.ttft_ms),
    decode_tps: headlineValue(hl.decode_tps ?? raw.decode_tps),
    prompt_tps_4k: headlineValue(hl.prompt_tps_4k ?? raw.prompt_tps_4k),
    agg_tps_8: headlineValue(hl.agg_tps_8 ?? raw.agg_tps_8),
    has_gpu: Boolean(raw.has_gpu),
  };
}

/* --------------------------------- API ----------------------------------- */

class ApiError extends Error {
  constructor(message, status, data = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
  }
}

async function api(path, { method = 'GET', body = null, timeout = 30000, headers = null } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  const merged = {};
  if (body !== null) merged['Content-Type'] = 'application/json';
  if (headers) Object.assign(merged, headers);

  let res;
  try {
    res = await fetch(path, {
      method,
      headers: Object.keys(merged).length ? merged : undefined,
      body: body !== null ? JSON.stringify(body) : undefined,
      signal: controller.signal,
      cache: 'no-store',
    });
  } catch (err) {
    clearTimeout(timer);
    if (err && err.name === 'AbortError') throw new ApiError('request timed out', 0);
    throw new ApiError('backend unreachable', 0);
  }
  clearTimeout(timer);

  const text = await res.text().catch(() => '');
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }

  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    if (data && typeof data === 'object') {
      const detail = data.detail ?? data.error ?? data.message;
      if (typeof detail === 'string' && detail) msg = detail;
      else if (detail) {
        try {
          msg = JSON.stringify(detail);
        } catch {
          /* keep status text */
        }
      }
    }
    throw new ApiError(msg, res.status, data);
  }
  if (data && typeof data === 'object' && data.ok === false) {
    throw new ApiError(sv(data.error) || sv(data.message) || 'request failed', res.status, data);
  }
  return data;
}

/* ----------------------- local run store (IndexedDB) ---------------------
   History lives in the visitor's browser. The server keeps a run in RAM for a
   short TTL only and never persists anything, so the saved copy is the source
   of truth for History/Compare/exports. If IndexedDB is unavailable (private
   mode, blocked storage) we fall back to an in-memory list for this session.
   ------------------------------------------------------------------------ */

const DB_NAME = 'localbench';
const DB_VERSION = 1;
const RUNS_STORE = 'runs';

const localStore = {
  db: null,
  broken: false,
  memory: new Map(),

  open() {
    if (this.db) return Promise.resolve(this.db);
    if (this.broken || typeof indexedDB === 'undefined') {
      this.broken = true;
      return Promise.resolve(null);
    }
    return new Promise((resolve) => {
      let req = null;
      try {
        req = indexedDB.open(DB_NAME, DB_VERSION);
      } catch {
        this.broken = true;
        resolve(null);
        return;
      }
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(RUNS_STORE)) {
          const store = db.createObjectStore(RUNS_STORE, { keyPath: 'id' });
          store.createIndex('created_at', 'created_at');
        }
      };
      req.onsuccess = () => {
        this.db = req.result;
        this.db.onversionchange = () => {
          try {
            this.db.close();
          } catch {
            /* ignore */
          }
          this.db = null;
        };
        resolve(this.db);
      };
      req.onerror = () => {
        this.broken = true;
        resolve(null);
      };
      req.onblocked = () => {
        this.broken = true;
        resolve(null);
      };
    });
  },

  request(mode, make) {
    return new Promise((resolve, reject) => {
      const db = this.db;
      let tx = null;
      try {
        tx = db.transaction(RUNS_STORE, mode);
        const req = make(tx.objectStore(RUNS_STORE));
        tx.oncomplete = () => resolve(req && 'result' in req ? req.result : undefined);
        tx.onerror = () => reject(tx.error || new Error('indexeddb error'));
        tx.onabort = () => reject(tx.error || new Error('indexeddb aborted'));
      } catch (err) {
        reject(err);
      }
    });
  },

  async put(record) {
    const db = await this.open();
    if (!db) {
      this.memory.set(record.id, record);
      return 'memory';
    }
    try {
      await this.request('readwrite', (store) => store.put(record));
      return 'indexeddb';
    } catch {
      this.broken = true;
      this.memory.set(record.id, record);
      return 'memory';
    }
  },

  async getAll() {
    const db = await this.open();
    if (!db) return [...this.memory.values()];
    try {
      return (await this.request('readonly', (store) => store.getAll())) || [];
    } catch {
      return [...this.memory.values()];
    }
  },

  async get(id) {
    const db = await this.open();
    if (!db) return this.memory.get(id) || null;
    try {
      return (await this.request('readonly', (store) => store.get(id))) || null;
    } catch {
      return this.memory.get(id) || null;
    }
  },

  async remove(id) {
    this.memory.delete(id);
    const db = await this.open();
    if (!db) return;
    try {
      await this.request('readwrite', (store) => store.delete(id));
    } catch {
      /* ignore */
    }
  },

  async clear() {
    this.memory.clear();
    const db = await this.open();
    if (!db) return;
    try {
      await this.request('readwrite', (store) => store.clear());
    } catch {
      /* ignore */
    }
  },

  async estimate() {
    try {
      if (navigator.storage && navigator.storage.estimate) return await navigator.storage.estimate();
    } catch {
      /* ignore */
    }
    return null;
  },
};

/* --------------------- per-run access token (session only) ----------------
   POST /api/runs returns a token that every later call must present. It only
   matters while the run is in server memory, so it lives in sessionStorage —
   never IndexedDB/localStorage, never in a URL we persist.
   ------------------------------------------------------------------------ */

const TOKEN_KEY = 'lb_run_tokens';

function readTokens() {
  try {
    const raw = sessionStorage.getItem(TOKEN_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function saveRunToken(id, token) {
  if (!id || !token) return;
  try {
    const map = readTokens();
    map[id] = token;
    sessionStorage.setItem(TOKEN_KEY, JSON.stringify(map));
  } catch {
    /* session storage unavailable: live calls will 404 -> friendly expiry */
  }
}

function getRunToken(id) {
  if (!id) return null;
  const token = readTokens()[id];
  return typeof token === 'string' && token ? token : null;
}

function forgetRunToken(id) {
  try {
    const map = readTokens();
    delete map[id];
    sessionStorage.setItem(TOKEN_KEY, JSON.stringify(map));
  } catch {
    /* ignore */
  }
}

function runHeaders(id) {
  const token = getRunToken(id);
  return token ? { 'X-Run-Token': token } : {};
}

/* ---------------------- client-side export helpers ----------------------- */

function downloadText(filename, text, mime = 'text/plain') {
  try {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
    return true;
  } catch {
    return false;
  }
}

function runJson(run) {
  return JSON.stringify(run, null, 2);
}

function csvCell(value) {
  if (value === null || value === undefined) return '';
  const text = String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** CSV with three sections: run summary, context ladder, concurrency. */
function runCsv(run) {
  const rows = [];
  const gpu = run.results.gpu;
  const summary = [
    ['id', run.id],
    ['label', run.label],
    ['model', run.model],
    ['endpoint_host', run.endpoint_host],
    ['preset_id', run.preset_id || ''],
    ['status', run.status],
    ['created_at', fmtWhen(run.created_at)],
    ['saved_at', isNum(run.saved_at) ? fmtWhen(run.saved_at) : ''],
    ['ttft_p50_ms', run.results.ttft_ms ? run.results.ttft_ms.p50 : ''],
    ['decode_mean_tps', run.results.decode_tps ? run.results.decode_tps.mean : ''],
    ['prefill_mean_tps', run.results.prompt_tps ? run.results.prompt_tps.mean : ''],
    ['total_p50_s', run.results.total_s ? run.results.total_s.p50 : ''],
    ['prompt_tokens', run.results.tokens.prompt],
    ['completion_tokens', run.results.tokens.completion],
    ['gpu_available', gpu.available],
    ['gpu_idle_vram_mb', gpu.idle.vram_mb],
    ['gpu_peak_vram_mb', gpu.peak.vram_mb],
    ['gpu_peak_power_w', gpu.peak.power_w],
    ['gpu_peak_temp_c', gpu.peak.temp_c],
    ['model_load_s', run.results.model_load_s],
    ['error', run.error || ''],
  ];
  rows.push(['section', 'field', 'value']);
  for (const [field, value] of summary) rows.push(['run', field, value]);
  rows.push([]);
  rows.push([
    'context',
    'ctx_requested',
    'prompt_tokens',
    'ttft_ms',
    'ttft_corrected_ms',
    'prefill_tps',
    'prefill_source',
    'decode_tps',
    'total_s',
    'completion_tokens',
    'ok',
    'error',
  ]);
  for (const p of run.results.context_points || []) {
    rows.push([
      'context',
      p.ctx_requested,
      p.prompt_tokens,
      p.ttft_ms,
      p.ttft_corrected_ms,
      p.prefill_tps,
      p.prefill_source || '',
      p.decode_tps,
      p.total_s,
      p.completion_tokens,
      p.ok,
      p.error || '',
    ]);
  }
  rows.push([]);
  rows.push([
    'concurrency',
    'n',
    'prompt_tokens',
    'wall_s',
    'agg_tps',
    'per_req_tps',
    'ttft_p50',
    'ttft_p95',
    'completion_tokens',
    'ok',
    'error',
  ]);
  for (const p of run.results.concurrency_points || []) {
    rows.push([
      'concurrency',
      p.n,
      p.prompt_tokens,
      p.wall_s,
      p.agg_tps,
      p.per_req_tps,
      p.ttft_p50,
      p.ttft_p95,
      p.completion_tokens,
      p.ok,
      p.error || '',
    ]);
  }
  return rows.map((row) => row.map(csvCell).join(',')).join('\n');
}

/* -------------------------------- state ---------------------------------- */

const state = {
  tab: 'run',
  run: null,
  runId: null,
  stream: null,
  streamFailures: 0,
  pollTimer: null,
  agentTimer: null,
  agentInfo: null,
  charts: new Map(),
  spark: null,
  sparkSamples: [],
  logCount: 0,
  history: [],
  historyError: null,
  historyLoading: false,
  selected: new Set(),
  compareIds: [],
  compareRuns: [],
  presets: [],
  presetsById: new Map(),
  selectedPresetId: '',
  config: null,
  stats: null,
  toastTimer: null,
  savedIds: new Set(),
  memoryNoticeShown: false,
  runMissing: false,
};

const isActive = (status) => status === 'queued' || status === 'running';

/* -------------------------------- charts --------------------------------- */

function chartThemeOptions(overrides = {}) {
  const extra = overrides || {};
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    parsing: false,
    normalized: true,
    interaction: { mode: 'nearest', intersect: false },
    layout: { padding: { top: 4, right: 6, bottom: 0, left: 0 } },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: THEME.panel,
        borderColor: THEME.hairline,
        borderWidth: 1,
        titleColor: THEME.text,
        bodyColor: THEME.secondary,
        titleFont: { family: THEME.fontMono, size: 10, weight: '500' },
        bodyFont: { family: THEME.fontMono, size: 10 },
        padding: 8,
        cornerRadius: 4,
        displayColors: false,
        callbacks: {
          title: extra.tooltipTitle || ((items) => (items.length ? String(items[0].parsed.x) : '')),
          label:
            extra.tooltipLabel ||
            ((item) => {
              let out = String(item.parsed.y);
              if (item.raw && item.raw.src) out += ` · ${item.raw.src}`;
              return out;
            }),
        },
      },
    },
    scales: {
      x: {
        type: 'linear',
        grid: { display: false },
        border: { display: false },
        ticks: {
          color: THEME.axis,
          font: { family: THEME.fontMono, size: 10 },
          maxTicksLimit: 5,
          maxRotation: 0,
          autoSkip: true,
          padding: 4,
          callback:
            extra.xTick || ((v) => (extra.integerX && !Number.isInteger(v) ? '' : String(v))),
        },
      },
      y: {
        beginAtZero: true,
        grid: { color: THEME.grid, drawTicks: false, tickLength: 0 },
        border: { display: false },
        ticks: {
          color: THEME.axis,
          font: { family: THEME.fontMono, size: 10 },
          count: 3,
          maxTicksLimit: 3,
          padding: 4,
          callback: extra.yTick || fmtAxis,
        },
      },
    },
  };
}

function getChart(canvasId, overrides) {
  if (!HAS_CHART) return null;
  const existing = state.charts.get(canvasId);
  if (existing) return existing;
  const canvas = $id(canvasId);
  if (!canvas) return null;
  const chart = new window.Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { datasets: [] },
    options: chartThemeOptions(overrides),
  });
  state.charts.set(canvasId, chart);
  return chart;
}

function setChartData(canvasId, datasets, { overrides = {} } = {}) {
  const chart = getChart(canvasId, overrides);
  if (!chart) return;
  chart.data.datasets = (datasets || []).map((ds, i) => {
    const raw = (ds.data || []).filter((p) => p && isNum(p.x) && isNum(p.y));
    return {
    borderColor: runColor(i),
    backgroundColor: 'transparent',
    borderWidth: 2,
    // a one-point series has no line to draw — show its marker instead
    pointRadius: raw.length === 1 ? 3 : 0,
    pointHoverRadius: 3,
    pointBorderWidth: 0,
    tension: 0,
    spanGaps: true,
    fill: false,
    ...ds,
    data: raw.slice().sort((a, b) => a.x - b.x),
    };
  });
  const xs = chart.data.datasets.flatMap((ds) => ds.data.map((p) => p.x));
  if (xs.length > 1) {
    const min = Math.min(...xs);
    const max = Math.max(...xs);
    if (min !== max) {
      chart.options.scales.x.min = min;
      chart.options.scales.x.max = max;
    } else {
      delete chart.options.scales.x.min;
      delete chart.options.scales.x.max;
    }
  } else {
    delete chart.options.scales.x.min;
    delete chart.options.scales.x.max;
  }
  chart.update('none');
}

function toggleChartEmpty(id, isEmpty) {
  const node = $id(id);
  if (node) node.hidden = !isEmpty;
}

function resizeAllCharts() {
  state.charts.forEach((chart) => {
    try {
      chart.resize();
    } catch {
      /* detached canvas */
    }
  });
}

const CTX_CHARTS = [
  { id: 'chart-ttft', empty: 'chart-ttft-empty', key: 'ttft_ms', fmt: fmtMs },
  { id: 'chart-prefill', empty: 'chart-prefill-empty', key: 'prefill_tps', fmt: fmtTps },
  { id: 'chart-decode', empty: 'chart-decode-empty', key: 'decode_tps', fmt: fmtTps },
];
const AGG_CHART = { id: 'chart-agg', empty: 'chart-agg-empty', fmt: fmtTps };

/** The ladder rung is only the target; measured prompt tokens are the truth. */
function pointX(point) {
  if (isNum(point.prompt_tokens) && point.prompt_tokens > 0) return point.prompt_tokens;
  return isNum(point.ctx_requested) ? point.ctx_requested : null;
}

function ctxSeries(run, key) {
  const out = [];
  for (const point of run.results.context_points || []) {
    if (!point.ok) continue;
    const x = pointX(point);
    const y = point[key];
    // nulls (prefill_tps when prefill_source is "unreliable", decode_tps with
    // <8 tokens) are dropped — a gap means unknown, never 0.
    if (!isNum(x) || !isNum(y) || y <= 0) continue;
    out.push({
      x,
      y,
      rung: point.ctx_requested,
      src: key === 'prefill_tps' ? point.prefill_source : null,
    });
  }
  return out;
}

function concSeries(run, key) {
  return (run.results.concurrency_points || [])
    .filter((p) => p.ok && isNum(p.n) && isNum(p[key]) && p[key] > 0)
    .map((p) => ({ x: p.n, y: p[key] }));
}

function ctxTooltipTitle(items) {
  if (!items || !items.length) return '';
  const item = items[0];
  const raw = item.raw || {};
  const x = nv(item.parsed && item.parsed.x);
  const parts = [`${fmtInt(x)} prompt tok`];
  if (isNum(raw.rung)) parts.push(`rung ${fmtCtx(raw.rung)}`);
  return parts.join(' · ');
}

function ctxTooltipLabel(formatter) {
  return (item) => {
    let text = formatter(item.parsed.y);
    if (item.raw && item.raw.src) text += ` · ${item.raw.src}`;
    return text;
  };
}

function updateLiveCharts(run) {
  for (const spec of CTX_CHARTS) {
    const pts = ctxSeries(run, spec.key);
    const metricColor = THEME.metric[spec.key === 'ttft_ms' ? 'ttft' : spec.key === 'prefill_tps' ? 'prefill' : 'decode'];
    setChartData(spec.id, [{ label: spec.key, data: pts, borderColor: metricColor }], {
      overrides: {
        tooltipTitle: ctxTooltipTitle,
        tooltipLabel: ctxTooltipLabel(spec.fmt),
        xTick: fmtCtx,
        yTick: fmtAxis,
      },
    });
    toggleChartEmpty(spec.empty, pts.length === 0);
  }
  const aggPts = concSeries(run, 'agg_tps');
  setChartData(AGG_CHART.id, [{ label: 'agg_tps', data: aggPts, borderColor: THEME.metric.agg }], {
    overrides: {
      tooltipTitle: (x) => `n = ${x}`,
      tooltipLabel: (item) => fmtTps(item.parsed.y),
      integerX: true,
      xTick: (v) => (Number.isInteger(v) ? String(v) : ''),
      yTick: fmtAxis,
    },
  });
  toggleChartEmpty(AGG_CHART.empty, aggPts.length === 0);

  const noteEl = $id('prefill-note');
  if (noteEl) {
    const sources = (run.results.context_points || [])
      .filter((p) => p.ok && p.prefill_source)
      .map((p) => p.prefill_source);
    if (!sources.length) {
      noteEl.textContent = 'tok/s';
      noteEl.title = '';
    } else {
      const counts = new Map();
      for (const s of sources) counts.set(s, (counts.get(s) || 0) + 1);
      noteEl.textContent = `tok/s · ${[...counts.entries()].map(([s, n]) => `${n} ${s}`).join(' / ')}`;
      noteEl.title =
        'server = endpoint timings · derived = client TTFT · unreliable = withheld (link noise)';
    }
  }
}

/* ------------------------------ derived stats ---------------------------- */

function quantile(sorted, q) {
  if (!sorted.length) return null;
  if (sorted.length === 1) return sorted[0];
  const idx = (sorted.length - 1) * q;
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return sorted[lo];
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
}

function statsFromValues(values) {
  const vals = values.filter((v) => isNum(v) && v > 0).sort((a, b) => a - b);
  if (!vals.length) return null;
  return {
    mean: vals.reduce((a, b) => a + b, 0) / vals.length,
    p50: quantile(vals, 0.5),
    p95: quantile(vals, 0.95),
    min: vals[0],
    max: vals[vals.length - 1],
    n: vals.length,
  };
}

/** Provisional aggregates while a run executes (final stats arrive with it). */
function derivedResults(run) {
  const ctx = (run.results.context_points || []).filter((p) => p.ok);
  const conc = (run.results.concurrency_points || []).filter((p) => p.ok);
  const tokens = { prompt: 0, completion: 0 };
  for (const point of [...ctx, ...conc]) {
    if (isNum(point.prompt_tokens)) tokens.prompt += point.prompt_tokens;
    if (isNum(point.completion_tokens)) tokens.completion += point.completion_tokens;
  }
  return {
    ttft: statsFromValues(ctx.map((p) => p.ttft_ms)),
    decode: statsFromValues(ctx.map((p) => p.decode_tps)),
    total: statsFromValues(ctx.map((p) => p.total_s)),
    tokens,
  };
}

/* -------------------------------- tiles ---------------------------------- */

function tile(spec) {
  const { label, value, unit, sub, metric, empty, title } = spec;
  return el(
    'div',
    { class: 'tile' + (empty ? ' is-empty' : ''), dataset: metric ? { metric } : {}, title: title || null },
    el('span', { class: 'tile-label', text: label }),
    el('span', { class: 'tile-value' }, value, unit ? el('span', { class: 'unit', text: unit }) : null),
    sub ? el('span', { class: 'tile-sub', text: sub }) : null
  );
}

function prefillTileInfo(point) {
  if (!point) return { value: 'n/a', unit: '', sub: 'no 4K rung', title: '', empty: true };
  const source = point.prefill_source;
  const serverValue = isNum(point.prefill_tps_server) ? point.prefill_tps_server : null;
  const title = [
    source ? `prefill source: ${source}` : '',
    serverValue !== null ? `server timings: ${fmtTps(serverValue)} tok/s` : '',
    point.cache_bust ? 'cache-busted prompt' : '',
  ]
    .filter(Boolean)
    .join(' · ');

  if (isNum(point.prefill_tps)) {
    return {
      value: fmtTps(point.prefill_tps),
      unit: 'tok/s',
      sub: `${source || 'measured'} · rung ${fmtCtx(point.ctx_requested)} · ${fmtInt(point.prompt_tokens)} tok`,
      title,
      empty: false,
    };
  }
  if (source === 'unreliable') {
    return { value: 'n/a', unit: '', sub: 'link noise — withheld', title, empty: true };
  }
  return { value: 'n/a', unit: '', sub: source ? `source: ${source}` : 'not reported', title, empty: true };
}

function renderTiles(run) {
  const root = $id('tiles');
  const r = run.results;
  clear(root);
  const derived = derivedResults(run);
  const closest = prefillClosest(run, 4096);
  const prefillPoint =
    (r.context_points || []).find((p) => p.ok && p.ctx_requested === (closest.ctx ?? 4096)) || null;
  const prefill = prefillTileInfo(prefillPoint);
  const ping = run.overhead.ping_ms_p50;
  const ttft = r.ttft_ms || derived.ttft;
  const decode = r.decode_tps || derived.decode;
  const total = r.total_s || derived.total;
  const tokens = {
    prompt: isNum(r.tokens.prompt) && r.tokens.prompt > 0 ? r.tokens.prompt : derived.tokens.prompt || null,
    completion:
      isNum(r.tokens.completion) && r.tokens.completion > 0
        ? r.tokens.completion
        : derived.tokens.completion || null,
  };

  append(root, [
    tile({
      label: 'TTFT p50',
      value: ttft && isNum(ttft.p50) ? fmtMs(ttft.p50) : 'n/a',
      unit: 'ms',
      sub: ttft ? `p95 ${fmtMs(ttft.p95)} · n=${ttft.n}` : '',
      metric: 'ttft',
      empty: !isNum(ttft && ttft.p50),
    }),
    tile({
      label: 'Decode tok/s',
      value: decode && isNum(decode.mean) ? fmtTps(decode.mean) : 'n/a',
      unit: 'tok/s',
      sub: decode ? `min ${fmtTps(decode.min)} · max ${fmtTps(decode.max)}` : '',
      metric: 'decode',
      empty: !isNum(decode && decode.mean),
    }),
    tile({
      label: 'Prefill @4K',
      value: prefill.value,
      unit: prefill.unit,
      sub: prefill.sub,
      title: prefill.title,
      metric: 'prefill',
      empty: prefill.empty,
    }),
    tile({
      label: 'Total response',
      value: total && isNum(total.p50) ? fmtSec(total.p50) : 'n/a',
      unit: 's',
      sub: total ? `p95 ${fmtSec(total.p95)}` : '',
      metric: 'total',
      empty: !isNum(total && total.p50),
    }),
    tile({
      label: 'Prompt tokens',
      value: isNum(tokens.prompt) ? fmtInt(tokens.prompt) : 'n/a',
      unit: 'tok',
      sub: isNum(tokens.prompt) ? 'sum across points' : '',
      metric: 'tokens',
      empty: !isNum(tokens.prompt),
    }),
    tile({
      label: 'Completion tokens',
      value: isNum(tokens.completion) ? fmtInt(tokens.completion) : 'n/a',
      unit: 'tok',
      sub: r.model_load_s !== null ? `load ${fmtSec(r.model_load_s)} s` : 'sum across points',
      metric: 'tokens',
      empty: !isNum(tokens.completion),
    }),
  ]);

  $id('tiles-note').textContent = ping !== null ? `TTFT corrected by ${fmtMs(ping)} ms ping` : '';
}

function renderProgress(run) {
  const p = run.progress;
  const pct = run.status === 'done' ? 100 : p.pct;
  $id('progress-fill').style.width = `${pct}%`;
  $id('progress-pct').textContent = `${Math.round(pct)}%`;
  $id('progress-label').textContent =
    run.status === 'queued'
      ? 'queued — waiting for a worker'
      : run.status === 'running'
        ? `${p.phase} · ${p.label}`
        : run.status;
  $id('progress-bar').classList.toggle('is-indeterminate', run.status === 'queued');
  $id('progress-note').textContent =
    p.total > 0
      ? `step ${p.step}/${p.total}`
      : isNum(run.finished_at) && isNum(run.started_at)
        ? `took ${fmtSec(run.finished_at - run.started_at)} s`
        : '';
}

function renderRunMeta(run) {
  const node = $id('run-meta');
  const preset = run.preset_id ? state.presetsById.get(run.preset_id) : null;
  const presetText = run.preset_id
    ? ` · preset ${preset ? preset.name : run.preset_id}${preset && preset.has_credential ? ' (server key)' : ''}`
    : '';
  node.textContent = `${run.id || 'n/a'} · ${run.endpoint_host || 'no endpoint'} · ${
    run.model || 'no model'
  } · created ${fmtWhen(run.created_at)}${presetText}${run.error ? ` · ${run.error}` : ''}`;
  node.dataset.tone = run.error ? 'danger' : '';
  node.title = node.textContent;
}

/* ------------------------------- table cells ----------------------------- */

function numCell(value, formatter) {
  return el('td', { class: 'num', text: isNum(value) ? formatter(value) : 'n/a' });
}

/** Number cell with a small muted suffix (@8K, @4) naming the measured rung/n. */
function unitNumCell(value, formatter, suffix, title) {
  const td = el('td', { class: 'num' });
  if (!isNum(value)) {
    td.textContent = 'n/a';
    td.dataset.empty = 'true';
    if (title) td.title = title;
    return td;
  }
  td.appendChild(document.createTextNode(formatter(value)));
  if (suffix) td.appendChild(el('span', { class: 'cell-unit', text: suffix }));
  if (title) td.title = title;
  return td;
}

function emptyNumCell(value, formatter, title) {
  const td = el('td', { class: 'num', text: isNum(value) ? formatter(value) : 'n/a' });
  if (!isNum(value)) {
    td.dataset.empty = 'true';
    if (title) td.title = title;
  }
  return td;
}

/** decode_tps is null when fewer than 8 tokens were generated — never a 0. */
function decodeCell(point) {
  const few = isNum(point.completion_tokens) && point.completion_tokens < 8;
  return emptyNumCell(
    point.decode_tps,
    fmtTps,
    few
      ? `decode not measurable — only ${fmtInt(point.completion_tokens)} completion token(s)`
      : 'decode rate not reported'
  );
}

function perReqCell(point) {
  return emptyNumCell(point.per_req_tps, fmtTps, 'per-request decode rate not measurable');
}

/** Prefill may come from server timings, client TTFT, or be withheld. */
function prefillCell(point) {
  const td = el('td', {
    class: 'num',
    text: isNum(point.prefill_tps) ? fmtTps(point.prefill_tps) : 'n/a',
  });
  const bits = [];
  if (point.prefill_source) bits.push(`source: ${point.prefill_source}`);
  if (isNum(point.prefill_tps_server)) bits.push(`server timings: ${fmtTps(point.prefill_tps_server)} tok/s`);
  if (isNum(point.prompt_ms_server)) bits.push(`server prompt: ${fmtMs(point.prompt_ms_server)} ms`);
  if (point.cache_bust) bits.push('cache-busted prompt');
  if (bits.length) td.title = bits.join(' · ');
  if (!isNum(point.prefill_tps)) td.dataset.empty = 'true';
  return td;
}

function okCell(point) {
  if (point.ok) return el('td', { class: 'center cell-ok', text: 'ok' });
  return el('td', {
    class: 'center cell-ok',
    dataset: { ok: 'false' },
    text: point.error || 'fail',
    title: point.error || 'point failed',
  });
}

/* ---------------------------- context / conc tables ---------------------- */

function renderContextTable(run) {
  const body = $id('ctx-body');
  const table = $id('ctx-table');
  const empty = $id('ctx-empty');
  const points = run.results.context_points || [];
  clear(body);
  const okPoints = points.filter((p) => p.ok).length;

  if (!points.length) {
    table.hidden = true;
    empty.hidden = false;
    empty.textContent = isActive(run.status) ? 'Waiting for context points…' : 'No context points recorded.';
  } else {
    table.hidden = false;
    empty.hidden = true;
    append(
      body,
      points.map((p) => {
        const tr = el('tr', p.ok ? {} : { dataset: { failed: 'true' } });
        const draft =
          p.draft && isNum(p.draft.acceptance_rate) ? `${(p.draft.acceptance_rate * 100).toFixed(1)}%` : 'n/a';
        append(tr, [
          numCell(p.ctx_requested, fmtCtx),
          numCell(p.prompt_tokens, fmtInt),
          numCell(p.ttft_ms, fmtMs),
          prefillCell(p),
          decodeCell(p),
          numCell(p.total_s, fmtSec),
          el('td', { class: 'num', text: draft, title: p.draft ? 'draft tokens accepted' : 'no draft stats' }),
          okCell(p),
        ]);
        return tr;
      })
    );
  }
  $id('ctx-note').textContent = points.length
    ? `${okPoints}/${points.length} ok${headingPing(run)}${prefillSourceNote(points)}`
    : '';
  renderContextCards(run);
}

function headingPing(run) {
  const ping = run.overhead.ping_ms_p50;
  return ping !== null ? ` · overhead ${fmtMs(ping)} ms` : '';
}

function prefillSourceNote(points) {
  const counts = new Map();
  for (const p of points) {
    if (!p.ok || !p.prefill_source) continue;
    counts.set(p.prefill_source, (counts.get(p.prefill_source) || 0) + 1);
  }
  if (!counts.size) return '';
  return ` · prefill ${[...counts.entries()].map(([s, n]) => `${n} ${s}`).join(', ')}`;
}

function renderConcurrencyTable(run) {
  const body = $id('conc-body');
  const table = $id('conc-table');
  const empty = $id('conc-empty');
  const points = run.results.concurrency_points || [];
  clear(body);

  if (!points.length) {
    table.hidden = true;
    empty.hidden = false;
    empty.textContent = isActive(run.status)
      ? 'Waiting for concurrency points…'
      : 'No concurrency points recorded.';
  } else {
    table.hidden = false;
    empty.hidden = true;
    append(
      body,
      points.map((p) => {
        const tr = el('tr', p.ok ? {} : { dataset: { failed: 'true' } });
        append(tr, [
          numCell(p.n, fmtInt),
          emptyNumCell(p.agg_tps, fmtTps, 'aggregate throughput not reported'),
          perReqCell(p),
          emptyNumCell(p.ttft_p50, fmtMs, 'TTFT p50 not reported'),
          emptyNumCell(p.ttft_p95, fmtMs, 'TTFT p95 not reported'),
          numCell(p.wall_s, fmtSec),
          numCell(p.completion_tokens, fmtInt),
          okCell(p),
        ]);
        return tr;
      })
    );
  }
  const okPoints = points.filter((p) => p.ok).length;
  $id('conc-note').textContent = points.length ? `${okPoints}/${points.length} ok` : '';
  renderConcurrencyCards(run);
}

/* ------------------------------ mobile cards ----------------------------- */

/**
 * Definition card used by the phone layout: a heading row plus label/value
 * pairs. Replaces the tables below 640px — tables are never squeezed.
 */
function dataCard({ title, sub, tag, status, rows, actions }) {
  const head = el('div', { class: 'data-card-head' });
  if (tag) head.appendChild(tag);
  if (title) head.appendChild(el('span', { class: 'card-title', text: title }));
  if (sub) head.appendChild(el('span', { class: 'card-sub', text: sub }));
  if (status) head.appendChild(status);

  const dl = el('dl', { class: 'def-list' });
  for (const row of rows) {
    const dd = el('dd', { class: row.empty ? 'is-empty' : '' });
    if (row.node) {
      dd.appendChild(row.node);
    } else {
      dd.appendChild(
        el('span', {
          class: 'dd-value',
          text: row.value === null || row.value === undefined ? 'n/a' : String(row.value),
        })
      );
      if (row.chip) dd.appendChild(row.chip);
    }
    dl.appendChild(el('div', {}, el('dt', { text: row.label }), dd));
  }

  const card = el('div', { class: 'data-card' }, head, dl);
  if (actions && actions.length) {
    const bar = el('div', { class: 'card-actions' });
    append(bar, actions);
    card.appendChild(bar);
  }
  return card;
}

function renderContextCards(run) {
  const box = $id('ctx-cards');
  clear(box);
  const points = run.results.context_points || [];
  if (!points.length) return;
  append(
    box,
    points.map((p) => {
      const draft = p.draft && isNum(p.draft.acceptance_rate) ? `${(p.draft.acceptance_rate * 100).toFixed(1)}%` : null;
      return dataCard({
        title: `ctx ${fmtCtx(p.ctx_requested)}`,
        sub: isNum(p.prompt_tokens) ? `${fmtInt(p.prompt_tokens)} prompt tok` : '',
        status: p.ok
          ? el('span', { class: 'status-pill', dataset: { status: 'done' }, text: 'ok' })
          : el('span', { class: 'status-pill', dataset: { status: 'failed' }, text: p.error || 'fail' }),
        rows: [
          { label: 'TTFT', value: p.ttft_ms, fmt: fmtMs },
          { label: 'Prefill tok/s', value: p.prefill_tps, fmt: fmtTps },
          { label: 'Decode tok/s', value: p.decode_tps, fmt: fmtTps },
          { label: 'Total s', value: p.total_s, fmt: fmtSec },
          { label: 'Draft accept', value: draft },
          { label: 'Prompt tok', value: p.prompt_tokens, fmt: fmtInt },
        ].map((row) => ({
          label: row.label,
          value: isNum(row.value) ? row.fmt(row.value) : 'n/a',
          empty: !isNum(row.value),
        })),
      });
    })
  );
}

function renderConcurrencyCards(run) {
  const box = $id('conc-cards');
  clear(box);
  const points = run.results.concurrency_points || [];
  if (!points.length) return;
  append(
    box,
    points.map((p) =>
      dataCard({
        title: `n = ${fmtInt(p.n)}`,
        sub: isNum(p.prompt_tokens) ? `${fmtInt(p.prompt_tokens)} prompt tok` : '',
        status: p.ok
          ? el('span', { class: 'status-pill', dataset: { status: 'done' }, text: 'ok' })
          : el('span', { class: 'status-pill', dataset: { status: 'failed' }, text: p.error || 'fail' }),
        rows: [
          ['Agg tok/s', p.agg_tps, fmtTps],
          ['Per-req tok/s', p.per_req_tps, fmtTps],
          ['TTFT p50', p.ttft_p50, fmtMs],
          ['TTFT p95', p.ttft_p95, fmtMs],
          ['Wall s', p.wall_s, fmtSec],
          ['Completion tok', p.completion_tokens, fmtInt],
        ].map(([label, value, fmt]) => ({
          label,
          value: isNum(value) ? fmt(value) : 'n/a',
          empty: !isNum(value),
        })),
      })
    )
  );
}

/* --------------------------------- GPU ----------------------------------- */

function aggregateSamples(samples) {
  const idle = {};
  const peak = {};
  const avg = {};
  for (const f of GPU_FIELDS) {
    const vals = samples.map((s) => nv(s[f])).filter((v) => v !== null);
    if (!vals.length) {
      idle[f] = null;
      peak[f] = null;
      avg[f] = null;
      continue;
    }
    peak[f] = Math.max(...vals);
    avg[f] = vals.reduce((a, b) => a + b, 0) / vals.length;
    const head = vals.slice(0, Math.min(5, vals.length));
    idle[f] = head.reduce((a, b) => a + b, 0) / head.length;
  }
  return { idle, peak, avg };
}

function gpuView(run) {
  const g = run.results.gpu;
  if (g.available) return { ...g, source: 'agent', samples: g.samples_n };
  if (state.sparkSamples.length) {
    const agg = aggregateSamples(state.sparkSamples);
    return { available: true, ...agg, samples_n: state.sparkSamples.length, source: 'stream' };
  }
  return { available: false, source: 'none', samples_n: 0 };
}

function gpuCell(label, value, formatter, isTemp = false) {
  return el(
    'div',
    { class: 'gpu-cell' },
    el('span', { class: 'k', text: label }),
    el('span', { class: 'v' + (isTemp ? ' is-temp' : ''), text: isNum(value) ? formatter(value) : 'n/a' })
  );
}

function renderGpu(run) {
  const view = gpuView(run);
  const emptyEl = $id('gpu-empty');
  const blockEl = $id('gpu-block');
  const tilesEl = $id('gpu-tiles');
  const noteEl = $id('gpu-note');

  $id('gpu-samples').textContent = view.available
    ? `${fmtInt(view.samples_n)} samples${view.source === 'stream' ? ' · live' : ' · agent'}`
    : '';

  if (!view.available) {
    blockEl.hidden = true;
    emptyEl.hidden = false;
    const notes = run.results.notes || [];
    const gpuNote = notes.find((n) => /gpu|telemetry|agent/i.test(n));
    const span = emptyEl.querySelector('span');
    if (span) {
      span.textContent =
        gpuNote ||
        (run.agent.connected
          ? 'Agent connected — waiting for the first telemetry sample.'
          : 'No GPU agent attached — telemetry unavailable.');
    }
    return;
  }

  blockEl.hidden = false;
  emptyEl.hidden = true;
  clear(tilesEl);

  append(
    tilesEl,
    [
      { key: 'idle', label: 'Idle' },
      { key: 'peak', label: 'Peak' },
      { key: 'avg', label: 'Average' },
    ].map(({ key, label }) => {
      const side = view[key] || {};
      return el(
        'div',
        { class: 'gpu-tile' },
        el('div', { class: 'gpu-label', text: label }),
        el(
          'div',
          { class: 'gpu-series' },
          gpuCell('VRAM', side.vram_mb, fmtMBShort),
          gpuCell('Power', side.power_w, fmtW),
          gpuCell('Temp', side.temp_c, fmtTemp, true),
          gpuCell('Util', side.gpu_util, fmtUtil)
        )
      );
    })
  );

  noteEl.textContent =
    view.source === 'stream' ? 'aggregated from the live agent stream' : 'reported by the telemetry agent';
}

const SPARK_SERIES = [
  { key: 'vram_mb', label: 'VRAM', color: () => THEME.gpu.vram, fmt: fmtMBShort },
  { key: 'power_w', label: 'Power', color: () => THEME.gpu.power, fmt: fmtW },
  { key: 'temp_c', label: 'Temp', color: () => THEME.gpu.temp, fmt: fmtTemp },
  { key: 'gpu_util', label: 'Util', color: () => THEME.gpu.util, fmt: fmtUtil },
];

function pushTelemetrySample(rawSample) {
  if (!state.run) return;
  const s = normalizeGpuSample(rawSample);
  if (!s) return;
  s.ts = nv(obj(rawSample).ts);
  state.sparkSamples.push(s);
  if (state.sparkSamples.length > 300) state.sparkSamples.splice(0, state.sparkSamples.length - 300);
  updateSpark();
  renderGpu(state.run);
}

function updateSpark() {
  const block = $id('gpu-block');
  const card = block ? block.querySelector('.spark-card') : null;
  if (!card) return;
  const samples = state.sparkSamples;
  const available = samples.length > 0;
  card.hidden = !available;
  const legend = $id('spark-legend');
  clear(legend);
  if (!available || !HAS_CHART) return;

  const maxBy = {};
  for (const series of SPARK_SERIES) {
    const vals = samples.map((s) => nv(s[series.key])).filter((v) => v !== null);
    maxBy[series.key] = vals.length ? Math.max(...vals, 1) : 1;
  }

  const datasets = SPARK_SERIES.map((series) => ({
    label: series.label,
    borderColor: series.color(),
    backgroundColor: 'transparent',
    borderWidth: 1.5,
    pointRadius: 0,
    pointHoverRadius: 3,
    tension: 0,
    spanGaps: true,
    fill: false,
    meta: { key: series.key, fmt: series.fmt },
    data: samples
      .map((s, i) => {
        const v = nv(s[series.key]);
        return v === null ? null : { x: i, y: v / maxBy[series.key] };
      })
      .filter(Boolean),
  }));

  if (!state.spark) {
    const canvas = $id('spark');
    if (!canvas) return;
    state.spark = new window.Chart(canvas.getContext('2d'), {
      type: 'line',
      data: { datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        parsing: false,
        normalized: true,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: THEME.panel,
            borderColor: THEME.hairline,
            borderWidth: 1,
            titleColor: THEME.text,
            bodyColor: THEME.secondary,
            titleFont: { family: THEME.fontMono, size: 10 },
            bodyFont: { family: THEME.fontMono, size: 10 },
            padding: 6,
            cornerRadius: 4,
            displayColors: false,
            callbacks: {
              title: (items) => {
                const i = items.length ? Math.round(items[0].parsed.x) : 0;
                const sample = state.sparkSamples[i];
                return sample && isNum(sample.ts) ? fmtClock(sample.ts) : `sample ${i + 1}`;
              },
              label: (item) => {
                const i = Math.round(item.parsed.x);
                const sample = state.sparkSamples[i];
                const key = item.dataset.meta ? item.dataset.meta.key : null;
                const fmt = item.dataset.meta ? item.dataset.meta.fmt : String;
                const v = sample && key ? sample[key] : null;
                return `${item.dataset.label}: ${isNum(v) ? fmt(v) : 'n/a'}`;
              },
            },
          },
        },
        scales: {
          x: { display: false, type: 'linear', min: 0, max: Math.max(samples.length - 1, 1) },
          y: { display: false, min: 0, max: 1 },
        },
      },
    });
  } else {
    state.spark.data.datasets = datasets;
    state.spark.options.scales.x.max = Math.max(samples.length - 1, 1);
    state.spark.update('none');
  }

  append(
    legend,
    SPARK_SERIES.map((series) => {
      const last = [...samples].reverse().find((s) => nv(s[series.key]) !== null);
      const value = last && isNum(last[series.key]) ? series.fmt(last[series.key]) : 'n/a';
      return el('span', {}, el('i', { style: `background:${series.color()}` }), `${series.label} ${value}`);
    })
  );
}

/* --------------------------------- agent --------------------------------- */

function renderAgent(run) {
  const box = $id('agent-box');
  const unavailable = $id('agent-unavailable');
  const stateEl = $id('agent-state');
  const stateText = $id('agent-state-text');
  const cmdEl = $id('agent-cmd');
  const note = $id('agent-note');
  const info = state.agentInfo;
  const enabled = state.config ? state.config.gpu_agent_enabled !== false : true;

  if (!enabled) {
    box.hidden = true;
    unavailable.hidden = false;
    note.textContent = '';
    return;
  }
  unavailable.hidden = true;
  box.hidden = false;

  const connected = Boolean((info && (info.agent_connected ?? info.connected)) || run.agent.connected);
  stateEl.dataset.connected = connected ? 'true' : 'false';
  stateText.textContent = connected ? 'connected' : 'not connected';
  cmdEl.textContent = (info && sv(info.command)) || 'agent command unavailable';
  note.textContent = connected ? 'telemetry streaming' : info && info.command ? 'run on the GPU host' : '';
}

async function loadAgentInfo(runId) {
  try {
    const data = await api(`/api/runs/${encodeURIComponent(runId)}/agent`, {
      headers: runHeaders(runId),
    });
    state.agentInfo = {
      command: sv(data.command),
      agent_connected: Boolean(data.agent_connected ?? data.connected),
      agent_key: sv(data.agent_key),
    };
    if (state.run && state.run.id === runId) renderAgent(state.run);
  } catch {
    state.agentInfo = null;
  }
}

function startAgentPolling(runId) {
  stopAgentPolling();
  if (!isActive(state.run ? state.run.status : '')) return;
  state.agentTimer = setInterval(() => {
    if (!state.run || state.run.id !== runId || !isActive(state.run.status)) {
      stopAgentPolling();
      return;
    }
    loadAgentInfo(runId);
  }, 3000);
}

function stopAgentPolling() {
  if (state.agentTimer) {
    clearInterval(state.agentTimer);
    state.agentTimer = null;
  }
}

async function copyText(text) {
  if (!text) return false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through */
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.left = '-1000px';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

/* -------------------------------- exports -------------------------------- */

function renderExports(run) {
  const box = $id('export-links');
  clear(box);
  if (!run.id) {
    $id('export-note').textContent = '';
    return;
  }

  const mkLocal = (format) => {
    const btn = el('button', { class: 'btn btn-sm btn-ghost', type: 'button' });
    append(btn, [icon('download', 13), `Export ${format.toUpperCase()}`]);
    btn.addEventListener('click', () => {
      const ok = downloadText(
        `${run.id}.${format}`,
        format === 'json' ? runJson(run) : runCsv(run),
        format === 'json' ? 'application/json' : 'text/csv'
      );
      toast(ok ? `${format.toUpperCase()} exported from your local copy` : 'Export failed', ok ? 'ok' : 'error');
    });
    return btn;
  };

  append(box, [mkLocal('json'), mkLocal('csv')]);

  // server-side export only while the run is still in the server's memory
  const live = isActive(run.status) && Boolean(getRunToken(run.id));
  if (live) {
    for (const format of ['json', 'csv']) {
      const btn = el('button', { class: 'btn btn-sm btn-ghost', type: 'button' });
      append(btn, [icon('external-link', 13), `Server ${format.toUpperCase()}`]);
      btn.title = 'download from the server while the run is still in memory (TTL)';
      btn.addEventListener('click', () => serverExport(run.id, format));
      box.appendChild(btn);
    }
  }
  $id('export-note').textContent = live
    ? 'local copy · server link available while the run is in memory'
    : 'built in this browser from the saved copy';
}

/** Fetch a server export with the run token and save it as a file. */
async function serverExport(runId, format) {
  try {
    const res = await fetch(
      `/api/runs/${encodeURIComponent(runId)}/export?format=${format}`,
      { headers: runHeaders(runId), cache: 'no-store' }
    );
    if (!res.ok) {
      throw new ApiError(res.status === 404 ? 'run expired' : `server export failed (${res.status})`, res.status);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${runId}.${format}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
    toast(`${format.toUpperCase()} exported from the server`, 'ok');
  } catch (err) {
    toast(err.status === 404 ? 'Server copy expired — use the local export' : `Export failed: ${err.message}`, 'error');
  }
}

/* ---------------------------------- log ---------------------------------- */

function appendLog(level, message) {
  const list = $id('log-list');
  if (!list) return;
  const row = el(
    'div',
    { class: 'log-line', dataset: { level: level || 'info' } },
    el('span', { class: 'log-time', text: fmtClock() }),
    el('span', { text: message })
  );
  list.appendChild(row);
  state.logCount += 1;
  while (list.childElementCount > 200) list.removeChild(list.firstChild);
  list.scrollTop = list.scrollHeight;
  $id('log-note').textContent = `${state.logCount} message${state.logCount === 1 ? '' : 's'}`;
}

/* --------------------------------- live ---------------------------------- */

function statusPill(status) {
  return el(
    'span',
    { class: 'status-pill', dataset: { status } },
    el('span', { class: 'dot' }),
    el('span', { text: status })
  );
}

function renderLive() {
  const run = state.run;
  const emptyEl = $id('live-empty');
  const bodyEl = $id('live-body');
  const statusEl = $id('run-status');
  const statusText = $id('run-status-text');
  const cancelEl = $id('cancel-btn');
  const subEl = $id('live-sub');
  const layout = document.querySelector('.run-layout');
  if (layout) layout.dataset.hasRun = run ? 'true' : 'false';

  if (!run) {
    emptyEl.hidden = false;
    bodyEl.hidden = true;
    statusEl.hidden = true;
    cancelEl.hidden = true;
    subEl.textContent = 'no run selected';
    if (!state.runMissing) resetLiveEmpty();
    renderActionBar();
    return;
  }

  emptyEl.hidden = true;
  bodyEl.hidden = false;
  statusEl.hidden = false;
  statusEl.dataset.status = run.status;
  statusText.textContent = run.status;

  const active = isActive(run.status);
  cancelEl.hidden = !active;
  cancelEl.disabled = false;

  const parts = [run.label || run.model || run.id];
  if (run.id) parts.push(run.id);
  if (!active) parts.push('complete');
  subEl.textContent = parts.join(' · ');

  renderRunMeta(run);
  renderProgress(run);
  renderTiles(run);
  updateLiveCharts(run);
  renderContextTable(run);
  renderConcurrencyTable(run);
  renderGpu(run);
  renderAgent(run);
  renderExports(run);
  renderActionBar();
}

function teardownStream() {
  if (state.stream) {
    try {
      state.stream.close();
    } catch {
      /* already closed */
    }
    state.stream = null;
  }
  state.streamFailures = 0;
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  stopAgentPolling();
}

function upsertPoint(list, keyName, keyValue, point) {
  const idx = list.findIndex((p) => p[keyName] === keyValue);
  if (idx >= 0) list[idx] = point;
  else list.push(point);
  return list;
}

function handleEvent(payload) {
  if (!payload || typeof payload !== 'object') return;
  const type = sv(payload.type);

  if (type === 'snapshot' || type === 'done') {
    const run = normalizeRun(payload.run);
    if (!run.id) return;
    state.run = run;
    state.runId = run.id;
    renderLive();
    if (type === 'done') {
      teardownStream();
      // the run's payload only exists in server memory for a short TTL:
      // keep the full local copy so History/Compare/exports survive it
      persistRun(run, { silent: false });
      if (state.tab === 'compare' && state.compareRuns.length) refreshCompare();
    }
    return;
  }

  const run = state.run;
  if (!run) return;

  if (type === 'progress') {
    run.progress = normalizeProgress(payload.progress);
    renderProgress(run);
    return;
  }

  if (type === 'point') {
    const kind = sv(payload.kind);
    if (kind === 'context') {
      const point = normalizeContextPoint(payload.point);
      if (point.ctx_requested !== null) {
        upsertPoint(run.results.context_points, 'ctx_requested', point.ctx_requested, point);
        renderTiles(run);
        renderContextTable(run);
        updateLiveCharts(run);
      }
    } else if (kind === 'concurrency') {
      const point = normalizeConcurrencyPoint(payload.point);
      if (point.n !== null) {
        upsertPoint(run.results.concurrency_points, 'n', point.n, point);
        renderTiles(run);
        renderConcurrencyTable(run);
        updateLiveCharts(run);
      }
    }
    return;
  }

  if (type === 'telemetry') {
    if (payload.sample) pushTelemetrySample(payload.sample);
    return;
  }

  if (type === 'log') appendLog(sv(payload.level) || 'info', sv(payload.message));
}

function attachStream(runId) {
  teardownStream();
  const token = getRunToken(runId);
  if (!token) {
    showRunMissing();
    return;
  }
  if (typeof window.EventSource !== 'function') {
    startPolling(runId);
    return;
  }
  let source;
  try {
    // EventSource cannot send headers — the run token goes in the query string
    source = new window.EventSource(
      `/api/runs/${encodeURIComponent(runId)}/events?token=${encodeURIComponent(token)}`
    );
  } catch {
    startPolling(runId);
    return;
  }
  state.stream = source;
  source.onopen = () => {
    state.streamFailures = 0;
  };
  const onPayload = (event) => {
    let payload = null;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }
    handleEvent(payload);
  };
  source.onmessage = onPayload;
  for (const name of ['snapshot', 'progress', 'point', 'telemetry', 'log', 'done']) {
    source.addEventListener(name, onPayload);
  }
  source.onerror = () => {
    state.streamFailures += 1;
    const active = state.run && state.run.id === runId && isActive(state.run.status);
    if (!active) {
      teardownStream();
      return;
    }
    if (state.streamFailures >= 2) {
      try {
        source.close();
      } catch {
        /* ignore */
      }
      state.stream = null;
      appendLog('warn', 'SSE connection failed — falling back to polling every 2 s.');
      startPolling(runId);
    }
  };
}

function startPolling(runId) {
  if (state.pollTimer) return;
  const tick = async () => {
    try {
      const data = await api(`/api/runs/${encodeURIComponent(runId)}`, {
        headers: runHeaders(runId),
      });
      const run = normalizeRun(data.run ?? data);
      if (!run.id) return;
      state.run = run;
      state.runId = run.id;
      renderLive();
      if (!isActive(run.status)) {
        teardownStream();
        persistRun(run, { silent: false });
        if (state.tab === 'compare' && state.compareRuns.length) refreshCompare();
      }
    } catch (err) {
      if (err && err.status === 404) {
        teardownStream();
        showRunMissing();
      }
      /* other failures: keep polling */
    }
  };
  tick();
  state.pollTimer = setInterval(tick, 2000);
}

function showRunMissing() {
  teardownStream();
  state.run = null;
  state.runMissing = true;
  $id('live-skeleton').hidden = true;
  $id('live-body').hidden = true;
  const empty = $id('live-empty');
  empty.hidden = false;
  const title = empty.querySelector('.empty-title');
  const bodyText = empty.querySelector('.empty-body');
  if (title) title.textContent = 'Run expired or not saved in this browser';
  if (bodyText) {
    bodyText.textContent =
      'Server copies live in memory for about 15 minutes and are never written to disk. This run was not saved locally, so it is gone — run a new benchmark to create a fresh one.';
  }
  $id('run-status').hidden = true;
  $id('cancel-btn').hidden = true;
  $id('live-sub').textContent = 'no run selected';
  const layout = document.querySelector('.run-layout');
  if (layout) layout.dataset.hasRun = 'false';
  renderActionBar();
}

function resetLiveEmpty() {
  const empty = $id('live-empty');
  const title = empty.querySelector('.empty-title');
  const bodyText = empty.querySelector('.empty-body');
  if (title) title.textContent = 'No run selected';
  if (bodyText) {
    bodyText.textContent =
      'Paste an OpenAI-compatible endpoint, pick a model and run the benchmark. Results stream in live; finished runs are saved in this browser.';
  }
}

async function openRun(runId, { announce = false } = {}) {
  teardownStream();
  state.run = null;
  state.runId = runId;
  state.agentInfo = null;
  state.sparkSamples = [];
  state.runMissing = false;
  resetLiveEmpty();
  if (state.spark) {
    try {
      state.spark.destroy();
    } catch {
      /* ignore */
    }
    state.spark = null;
  }
  $id('live-skeleton').hidden = false;
  $id('live-empty').hidden = true;
  $id('live-body').hidden = true;

  try {
    // 1) a saved run renders entirely from the browser copy (no network at all)
    const saved = await localStore.get(runId);
    if (saved) {
      const run = normalizeRun(saved);
      run.saved_at = nv(saved.saved_at);
      state.run = run;
      state.runId = run.id;
      if (announce) toast(`Opened ${run.label || state.runId}`, 'ok');
      return run;
    }

    // 2) not saved: a live/recent run needs the per-run token
    if (!getRunToken(runId)) {
      showRunMissing();
      return null;
    }
    const data = await api(`/api/runs/${encodeURIComponent(runId)}`, {
      headers: runHeaders(runId),
    });
    const run = normalizeRun(data.run ?? data);
    state.run = run;
    state.runId = run.id || runId;
    if (isActive(run.status)) {
      attachStream(state.runId);
      startAgentPolling(state.runId);
    } else {
      await persistRun(run, { silent: true });
    }
    loadAgentInfo(state.runId);
    if (announce) toast(`Opened ${run.label || state.runId}`, 'ok');
    return run;
  } catch (err) {
    if (err && err.status === 404) {
      showRunMissing();
      return null;
    }
    toast(`Could not load run: ${err.message}`, 'error');
    return null;
  } finally {
    $id('live-skeleton').hidden = true;
    renderLive();
  }
}

/* -------------------------------- run form -------------------------------- */

function parseList(raw, { min = 1, max = 1e9 } = {}) {
  const parts = String(raw || '')
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map(Number);
  const out = [];
  for (const n of parts) {
    if (!Number.isFinite(n) || !Number.isInteger(n) || n < min || n > max) continue;
    if (!out.includes(n)) out.push(n);
  }
  return out.sort((a, b) => a - b);
}

function intField(id, fallback) {
  const node = $id(id);
  const value = node ? Number(node.value) : NaN;
  return Number.isFinite(value) && Number.isInteger(value) ? value : fallback;
}

function buildRunBody() {
  const preset = selectedPreset();
  const label = $id('label').value.trim();
  const ladderOn = $id('cfg-ladder').checked;
  const concOn = $id('cfg-conc-enable').checked;
  const contexts = parseList($id('cfg-contexts').value, { min: 16, max: 10_000_000 });
  const concurrency = parseList($id('cfg-concurrency').value, { min: 1, max: 1024 });
  if (ladderOn && contexts.length === 0) throw new Error('Context ladder is enabled but no lengths were given.');
  if (concOn && concurrency.length === 0) throw new Error('Concurrency is enabled but no levels were given.');

  const config = {
    contexts,
    concurrency,
    concurrency_context: intField('cfg-cc', 2048),
    max_tokens: intField('cfg-max-tokens', 128),
    repeats: intField('cfg-repeats', 1),
    warmup: intField('cfg-warmup', 1),
    request_timeout_s: intField('cfg-timeout', 600),
    enable_context_ladder: ladderOn,
    enable_concurrency: concOn,
  };

  // server-credential preset: the server resolves endpoint, model and key
  if (preset && preset.has_credential) {
    return { preset_id: preset.id, label: label || preset.name, config };
  }

  const baseUrl = $id('base-url').value.trim();
  const model = $id('model').value.trim();
  if (!baseUrl) throw new Error('Base URL is required.');
  let parsedUrl = null;
  try {
    parsedUrl = new URL(baseUrl);
  } catch {
    throw new Error('Base URL is not a valid URL (include http:// or https://).');
  }
  if (!/^https?:$/.test(parsedUrl.protocol)) throw new Error('Base URL must be http or https.');
  if (!model) throw new Error('Model is required — type it or press Fetch.');

  const apiKey = $id('api-key').value.trim();
  const body = {
    base_url: baseUrl,
    model,
    label: label || `${model} @ ${parsedUrl.host}`,
    config,
    telemetry: { mode: $id('cfg-telemetry').value || 'auto' },
  };
  if (apiKey) body.api_key = apiKey;
  return body;
}

function showFormError(message, tone = 'error') {
  const box = $id('form-error');
  const text = $id('form-error-text');
  if (!message) {
    box.hidden = true;
    text.textContent = '';
    return;
  }
  box.hidden = false;
  box.dataset.tone = tone === 'warn' ? 'warn' : 'error';
  text.textContent = message;
}

function setDiscoverStatus(text, tone = '') {
  const node = $id('discover-status');
  node.textContent = text || '';
  node.dataset.tone = tone || '';
}

async function discoverModels() {
  const btn = $id('fetch-models');
  const baseUrl = $id('base-url').value.trim();
  if (!baseUrl) {
    setDiscoverStatus('enter a base URL first', 'bad');
    $id('base-url').focus();
    return;
  }
  btn.disabled = true;
  setDiscoverStatus('querying /v1/models…');
  try {
    const data = await api('/api/discover', {
      method: 'POST',
      body: { base_url: baseUrl, api_key: $id('api-key').value.trim() || '' },
    });
    const models = arr(data && data.models)
      .map((m) => (m && typeof m === 'object' ? { id: sv(m.id), context_length: nv(m.context_length) } : null))
      .filter((m) => m && m.id);
    const list = $id('model-options');
    clear(list);
    append(
      list,
      models.map((m) =>
        el('option', {
          value: m.id,
          label: m.context_length ? `${m.id} (${fmtCtx(m.context_length)})` : m.id,
        })
      )
    );
    const latency = nv(data && data.latency_ms);
    if (models.length) {
      if (!$id('model').value.trim()) $id('model').value = models[0].id;
      $id('model-hint').textContent = `${models.length} model${models.length === 1 ? '' : 's'} found${
        latency !== null ? ` · ${fmtMs(latency)} ms` : ''
      }`;
      $id('model-hint').dataset.tone = 'ok';
      setDiscoverStatus(`${models.length} models`, 'ok');
    } else {
      $id('model-hint').textContent = 'Endpoint returned no models — type the model id manually.';
      $id('model-hint').dataset.tone = 'bad';
      setDiscoverStatus('no models', 'bad');
    }
  } catch (err) {
    $id('model-hint').textContent = `Model discovery failed: ${err.message}. Type the id manually.`;
    $id('model-hint').dataset.tone = 'bad';
    setDiscoverStatus('discovery failed', 'bad');
  } finally {
    btn.disabled = false;
  }
}

function normalizePreset(rawPreset) {
  const raw = obj(rawPreset);
  const name = sv(raw.name);
  let id = sv(raw.id);
  if (!id && name) {
    id = name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '');
  }
  return {
    id,
    name: name || sv(raw.base_url),
    base_url: sv(raw.base_url),
    model: sv(raw.model),
    note: sv(raw.note),
    has_credential: Boolean(raw.has_credential),
  };
}

function selectedPreset() {
  return state.selectedPresetId ? state.presetsById.get(state.selectedPresetId) || null : null;
}

/** Keep the form honest about where the credential comes from. */
function syncPresetUi() {
  const preset = selectedPreset();
  const noteEl = $id('preset-note');
  const credRow = $id('credential-row');
  const keyField = $id('api-key-field');
  const baseInput = $id('base-url');
  const modelInput = $id('model');

  if (!preset) {
    noteEl.hidden = true;
    noteEl.textContent = '';
    credRow.hidden = true;
    keyField.hidden = false;
    baseInput.readOnly = false;
    modelInput.readOnly = false;
    baseInput.classList.remove('is-locked');
    modelInput.classList.remove('is-locked');
    return;
  }

  noteEl.hidden = !preset.note;
  noteEl.textContent = preset.note || '';
  if (preset.has_credential) {
    credRow.hidden = false;
    keyField.hidden = true;
    baseInput.readOnly = true;
    modelInput.readOnly = true;
    baseInput.classList.add('is-locked');
    modelInput.classList.add('is-locked');
  } else {
    credRow.hidden = true;
    keyField.hidden = false;
    baseInput.readOnly = false;
    modelInput.readOnly = false;
    baseInput.classList.remove('is-locked');
    modelInput.classList.remove('is-locked');
  }
}

async function loadPresets() {
  const select = $id('preset-select');
  try {
    const data = await api('/api/presets');
    const presets = arr(data && data.presets).map(normalizePreset).filter((p) => p.id && (p.name || p.base_url));
    state.presets = presets;
    state.presetsById = new Map(presets.map((p) => [p.id, p]));
    clear(select);
    select.appendChild(el('option', { value: '', text: 'Manual endpoint…' }));
    append(
      select,
      presets.map((p) => el('option', { value: p.id, text: p.name || p.base_url, title: p.note || '' }))
    );
    select.disabled = presets.length === 0;
    if (state.selectedPresetId && !state.presetsById.has(state.selectedPresetId)) {
      // preset disappeared (hot reload) — fall back to manual mode
      state.selectedPresetId = '';
    }
    select.value = state.selectedPresetId || '';
    syncPresetUi();
    if (state.history.length) renderHistoryCards();
  } catch {
    select.disabled = true;
  }
}

function applyPreset(presetId) {
  const preset = state.presetsById.get(presetId);
  if (!preset) {
    deselectPreset();
    return;
  }
  state.selectedPresetId = preset.id;
  if (preset.base_url) $id('base-url').value = preset.base_url;
  if (preset.model) $id('model').value = preset.model;
  if (!$id('label').value.trim()) $id('label').value = preset.name || '';
  $id('preset-select').value = preset.id;
  syncPresetUi();
}

function deselectPreset() {
  state.selectedPresetId = '';
  const select = $id('preset-select');
  if (select) select.value = '';
  syncPresetUi();
}

async function submitRun(event) {
  if (event) event.preventDefault();
  showFormError('');
  let body;
  try {
    body = buildRunBody();
  } catch (err) {
    showFormError(err.message);
    return;
  }
  const btn = $id('run-btn');
  btn.disabled = true;
  try {
    const data = await api('/api/runs', { method: 'POST', body });
    const runId = sv(data && (data.run_id || data.id));
    const accessToken = sv(data && data.access_token);
    if (!runId) throw new Error('server did not return a run id');
    if (!accessToken) throw new Error('server did not return a run access token');
    saveRunToken(runId, accessToken);
    toast('Run queued', 'ok');
    switchTab('run');
    await openRun(runId);
    window.scrollTo(0, 0);
  } catch (err) {
    if (err.status === 429) {
      const retry = nv(err.data && err.data.retry_after);
      const limit = state.config && state.config.limits ? state.config.limits.rate_limit : null;
      const windowText = limit ? `max ${limit.runs} runs / ${fmtWindow(limit.window_s)}` : '';
      showFormError(
        `Rate limited${retry !== null ? ` — try again in ${Math.ceil(retry)} s` : ''}${
          windowText ? ` (${windowText} per IP)` : ''
        }`
      );
    } else if (err.status === 404 && /preset/i.test(err.message || '')) {
      showFormError('That preset no longer exists on the server — reloading the preset list.', 'warn');
      deselectPreset();
      await loadPresets();
    } else {
      showFormError(err.message);
    }
  } finally {
    btn.disabled = false;
  }
}

async function cancelRun() {
  const run = state.run;
  if (!run || !isActive(run.status)) return;
  try {
    await api(`/api/runs/${encodeURIComponent(run.id)}/cancel`, {
      method: 'POST',
      headers: runHeaders(run.id),
      timeout: 15000,
    });
    toast('Cancel requested', 'ok');
  } catch (err) {
    toast(`Cancel failed: ${err.message}`, 'error');
  }
}

function setupForm() {
  $id('run-form').addEventListener('submit', (event) => {
    submitRun(event);
  });
  $id('run-form').addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      submitRun(event);
    }
  });
  $id('fetch-models').addEventListener('click', () => {
    discoverModels();
  });
  $id('preset-select').addEventListener('change', (event) => {
    const value = event.target.value;
    if (value === '') deselectPreset();
    else applyPreset(value);
  });
  // typing an endpoint by hand takes the form out of preset mode; credential
  // presets are read-only and keep the preset body shape
  for (const id of ['base-url', 'model']) {
    $id(id).addEventListener('input', () => {
      const preset = selectedPreset();
      if (preset && preset.has_credential) return;
      if (state.selectedPresetId) deselectPreset();
    });
  }
  $id('cancel-btn').addEventListener('click', () => {
    cancelRun();
  });
  $id('toggle-key').addEventListener('click', () => {
    const input = $id('api-key');
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    const btn = $id('toggle-key');
    btn.setAttribute('aria-pressed', show ? 'true' : 'false');
    btn.setAttribute('aria-label', show ? 'Hide API key' : 'Show API key');
    const use = $id('toggle-key-use');
    if (use) use.setAttribute('href', show ? '#i-eye-off' : '#i-eye');
  });
  $id('adv-toggle').addEventListener('click', () => {
    const btn = $id('adv-toggle');
    const open = btn.getAttribute('aria-expanded') === 'true';
    btn.setAttribute('aria-expanded', open ? 'false' : 'true');
    $id('advanced').hidden = open;
  });
  $id('copy-agent').addEventListener('click', async () => {
    const text = ($id('agent-cmd').textContent || '').trim();
    const ok = await copyText(text);
    toast(ok ? 'Agent command copied' : 'Copy failed — select the command manually', ok ? 'ok' : 'error');
  });
}

/* -------------------------------- history -------------------------------- */

/** Row view over a full locally-stored run. */
function historyView(run) {
  return {
    id: run.id,
    label: run.label,
    model: run.model,
    endpoint_host: run.endpoint_host,
    preset_id: run.preset_id || null,
    status: run.status,
    created_at: run.created_at,
    saved_at: run.saved_at || null,
    ttft: run.results.ttft_ms ? run.results.ttft_ms.p50 : null,
    decode: run.results.decode_tps ? run.results.decode_tps.mean : null,
    prefill: prefillClosest(run, 4096),
    agg: aggAtMax(run),
    has_gpu: run.results.gpu.available,
  };
}

/** Small chip naming the preset a saved run used (lock when server-held key). */
function presetChip(presetId) {
  if (!presetId) return null;
  const preset = state.presetsById.get(presetId);
  const chip = el(
    'span',
    {
      class: 'preset-chip',
      title: preset
        ? `${preset.name}${preset.has_credential ? ' · server-managed credential' : ''}`
        : `preset ${presetId}`,
    }
  );
  if (preset && preset.has_credential) chip.appendChild(icon('key', 10));
  chip.appendChild(el('span', { text: preset ? preset.name : presetId }));
  return chip;
}

function renderHistoryCards() {
  const box = $id('history-cards');
  clear(box);
  if (!state.history.length) return;
  append(
    box,
    state.history.map((run) => {
      const row = historyView(run);
      const check = el('input', {
        type: 'checkbox',
        class: 'row-check card-check',
        'aria-label': `Select ${row.label || row.id}`,
      });
      check.checked = state.selected.has(row.id);
      check.addEventListener('change', () => {
        if (check.checked) state.selected.add(row.id);
        else state.selected.delete(row.id);
        renderHistory();
      });
      const openBtn = el('button', { class: 'btn btn-ghost btn-sm', type: 'button' });
      append(openBtn, [icon('play', 12), 'Open']);
      openBtn.addEventListener('click', () => {
        switchTab('run');
        openRun(row.id);
      });
      const delBtn = el('button', { class: 'btn btn-danger btn-sm', type: 'button' });
      append(delBtn, [icon('trash', 12), 'Delete']);
      delBtn.addEventListener('click', () => deleteRun(row));

      const rows = [
        ['TTFT p50', row.ttft, fmtMs],
        ['Decode tok/s', row.decode, fmtTps],
        ['Prefill tok/s', row.prefill.value, fmtTps, isNum(row.prefill.ctx) ? `@${fmtCtx(row.prefill.ctx)}` : ''],
        ['Agg tok/s', row.agg.value, fmtTps, isNum(row.agg.n) ? `@${fmtInt(row.agg.n)}` : ''],
        ['GPU', row.has_gpu ? 'yes' : 'no'],
        ['Saved', isNum(row.saved_at) ? fmtWhen(row.saved_at) : 'n/a'],
      ].map(([label, value, fmt, suffix]) => ({
        label,
        value:
          isNum(value) && fmt
            ? `${fmt(value)}${suffix ? ` ${suffix}` : ''}`
            : value === null || value === undefined
              ? 'n/a'
              : value,
        empty: value === null || value === undefined,
      }));

      const chip = presetChip(row.preset_id);
      return dataCard({
        tag: el('label', { class: 'card-check-wrap', title: 'Select for comparison' }, check),
        title: shortLabel(row, 40),
        sub: [row.model, row.endpoint_host, fmtWhen(row.created_at)].filter(Boolean).join(' · '),
        status: statusPill(row.status),
        rows: chip ? [{ label: 'Preset', node: chip }, ...rows] : rows,
        actions: [openBtn, delBtn],
      });
    })
  );
}

function renderHistory() {
  const body = $id('history-body');
  const table = $id('history-table');
  const empty = $id('history-empty');
  const errorBox = $id('history-error');
  const compareBtn = $id('history-compare');
  const clearBtn = $id('history-clear-all');
  const foot = $id('history-foot');
  const rows = state.history.map(historyView);
  const hasError = Boolean(state.historyError);
  const isEmpty = rows.length === 0;

  clear(body);
  table.hidden = isEmpty;
  empty.hidden = !isEmpty || hasError;
  errorBox.hidden = !hasError;
  if (hasError) $id('history-error-text').textContent = state.historyError;
  $id('history-skeleton').hidden = true;

  $id('history-sub').textContent = isEmpty
    ? 'saved runs are stored in this browser'
    : `${rows.length} saved run${rows.length === 1 ? '' : 's'} · stored in this browser`;
  $id('history-count').hidden = isEmpty;
  $id('history-count').textContent = String(rows.length);
  if (foot) foot.hidden = isEmpty;
  if (clearBtn) clearBtn.hidden = isEmpty;

  $id('compare-count').hidden = state.compareRuns.length === 0;
  $id('compare-count').textContent = String(state.compareRuns.length);

  compareBtn.disabled = state.selected.size < 2;
  compareBtn.textContent = state.selected.size
    ? `Compare selected (${state.selected.size})`
    : 'Compare selected';

  if (!isEmpty) {
    const colorIndex = new Map(state.compareIds.map((id, i) => [id, i]));
    append(
      body,
      rows.map((row) => {
        const selected = state.selected.has(row.id);
        const tr = el('tr', selected ? { class: 'is-selected' } : {});
        const checkbox = el('input', {
          type: 'checkbox',
          class: 'row-check',
          'aria-label': `Select ${row.label || row.id}`,
        });
        checkbox.checked = selected;
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) state.selected.add(row.id);
          else state.selected.delete(row.id);
          renderHistory();
        });
        const compareIdx = colorIndex.get(row.id);
        const labelCell = el('td', { class: 'label-cell', title: `${row.label || row.id} (${row.id})` });
        append(labelCell, [
          compareIdx !== undefined
            ? el('span', {
                class: 'cell-sub',
                style: `color:${runColor(compareIdx)}; font-weight:600`,
                text: compareIdx === 0 ? 'baseline' : `series ${compareIdx + 1}`,
              })
            : null,
          document.createTextNode(row.label || row.model || row.id),
          el('span', { class: 'cell-sub', text: row.id }),
        ]);
        const chip = presetChip(row.preset_id);
        if (chip) {
          const chipWrap = el('span', { class: 'cell-chip' });
          chipWrap.appendChild(chip);
          labelCell.appendChild(chipWrap);
        }

        const openBtn = el('button', { class: 'btn btn-sm btn-ghost', type: 'button', text: 'Open' });
        openBtn.addEventListener('click', () => {
        switchTab('run');
        openRun(row.id);
      });
        const delBtn = el('button', { class: 'btn btn-sm btn-danger', type: 'button' });
        delBtn.appendChild(icon('trash', 12));
        delBtn.addEventListener('click', () => deleteRun(row));
        const actions = el('td', { class: 'num' }, el('div', { class: 'row-actions' }, openBtn, delBtn));

        append(tr, [
          el('td', { class: 'center' }, checkbox),
          labelCell,
          el('td', { text: row.model || 'n/a' }),
          el('td', { class: 'mono', text: row.endpoint_host || 'n/a' }),
          emptyNumCell(row.ttft, fmtMs, 'TTFT not reported'),
          emptyNumCell(row.decode, fmtTps, 'decode not measurable'),
          unitNumCell(row.prefill.value, fmtTps, isNum(row.prefill.ctx) ? `@${fmtCtx(row.prefill.ctx)}` : '', 'prefill not reported'),
          unitNumCell(row.agg.value, fmtTps, isNum(row.agg.n) ? `@${fmtInt(row.agg.n)}` : '', 'no concurrency data'),
          el('td', { class: 'center', text: row.has_gpu ? 'yes' : '—' }),
          el('td', { text: fmtWhen(row.created_at), title: fmtAgo(row.created_at) }),
          el('td', { class: 'center' }, statusPill(row.status)),
          actions,
        ]);
        return tr;
      })
    );
  }

  const allBox = $id('history-select-all');
  const visibleIds = rows.map((r) => r.id);
  const selectedVisible = visibleIds.filter((id) => state.selected.has(id)).length;
  allBox.checked = visibleIds.length > 0 && selectedVisible === visibleIds.length;
  allBox.indeterminate = selectedVisible > 0 && selectedVisible < visibleIds.length;

  renderHistoryCards();
  renderActionBar();
}

/** Storage usage footer: honest, from navigator.storage.estimate(). */
async function renderHistoryFooter() {
  const usageEl = $id('history-usage');
  if (!usageEl) return;
  const estimate = await localStore.estimate();
  const used = estimate && isNum(estimate.usage) ? estimate.usage : null;
  if (used === null) {
    usageEl.textContent = state.history.length
      ? `${state.history.length} run${state.history.length === 1 ? '' : 's'} stored in this browser`
      : '';
    return;
  }
  const mb = used / (1024 * 1024);
  const text = mb >= 0.1 ? `${mb.toFixed(1)} MB used` : `${Math.max(1, Math.round(used / 1024))} KB used`;
  usageEl.textContent = text;
}

/** Persist a finished (or failed) run into the local store. */
async function persistRun(run, { silent = false } = {}) {
  if (!run || !run.id) return;
  const record = { ...run, saved_at: Date.now() / 1000 };
  const mode = await localStore.put(record);
  state.savedIds.add(run.id);
  if (mode === 'memory' && !state.memoryNoticeShown) {
    state.memoryNoticeShown = true;
    toast('Private mode — saved runs last for this session only', 'warn');
  }
  await loadLocalHistory();
  if (!silent) {
    toast(
      mode === 'memory' ? 'Run kept for this session only' : 'Run saved in this browser',
      mode === 'memory' ? 'warn' : 'ok'
    );
  }
}

async function loadLocalHistory() {
  if (!state.history.length) $id('history-skeleton').hidden = false;
  try {
    const records = await localStore.getAll();
    state.history = records
      .map((raw) => {
        const run = normalizeRun(raw);
        run.saved_at = nv(raw.saved_at);
        return run;
      })
      .filter((run) => run.id)
      .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    state.historyError = null;
  } catch (err) {
    state.history = [];
    state.historyError = err && err.message ? err.message : 'browser storage unavailable';
  } finally {
    renderHistory();
    renderHistoryFooter();
  }
}

/* kept name for existing call sites */
const loadHistory = loadLocalHistory;

async function deleteRun(row) {
  const confirmed = window.confirm(`Delete the local copy of run "${row.label || row.id}"?`);
  if (!confirmed) return;
  await localStore.remove(row.id);
  state.selected.delete(row.id);
  state.compareIds = state.compareIds.filter((id) => id !== row.id);
  state.savedIds.delete(row.id);
  // best effort: evict the server's in-memory copy while the token still works
  if (getRunToken(row.id)) {
    try {
      await api(`/api/runs/${encodeURIComponent(row.id)}`, {
        method: 'DELETE',
        headers: runHeaders(row.id),
        timeout: 10000,
      });
    } catch {
      /* expired or already gone — the local copy is what matters */
    }
    forgetRunToken(row.id);
  }
  toast('Run deleted from this browser', 'ok');
  await loadLocalHistory();
  if (state.compareRuns.length) refreshCompare();
}

async function clearAllRuns() {
  if (!state.history.length) return;
  const confirmed = window.confirm(
    `Delete all ${state.history.length} saved runs from this browser? This cannot be undone.`
  );
  if (!confirmed) return;
  await localStore.clear();
  state.selected = new Set();
  state.compareIds = [];
  state.compareRuns = [];
  state.savedIds.clear();
  state.history = [];
  state.run = null;
  teardownStream();
  renderLive();
  renderCompare();
  await loadLocalHistory();
  toast('All local runs cleared', 'ok');
}

/* -------------------------------- compare -------------------------------- */

/** Prefill at the closest measured rung (4K when present, else nearest). */
function prefillClosest(run, target = 4096) {
  let best = null;
  for (const point of run.results.context_points || []) {
    if (!point.ok || !isNum(point.prefill_tps)) continue;
    const ctx = isNum(point.ctx_requested) ? point.ctx_requested : pointX(point);
    if (!isNum(ctx)) continue;
    const dist = Math.abs(ctx - target);
    if (!best || dist < best.dist || (dist === best.dist && ctx < best.ctx)) {
      best = { dist, ctx, value: point.prefill_tps };
    }
  }
  return best || { value: null, ctx: null, dist: null };
}

/** Highest measured concurrency level and its aggregate throughput. */
function aggAtMax(run) {
  let best = null;
  for (const point of run.results.concurrency_points || []) {
    if (!point.ok || !isNum(point.n) || !isNum(point.agg_tps)) continue;
    if (!best || point.n > best.n) best = { n: point.n, value: point.agg_tps };
  }
  return best || { n: null, value: null };
}

function prefillAt(run, target) {
  const point = (run.results.context_points || []).find(
    (p) => p.ok && p.ctx_requested === target && isNum(p.prefill_tps)
  );
  return point ? point.prefill_tps : null;
}

function aggAt(run, target) {
  const point = (run.results.concurrency_points || []).find(
    (p) => p.ok && p.n === target && isNum(p.agg_tps)
  );
  return point ? point.agg_tps : null;
}

function draftAcceptance(run) {
  const rates = (run.results.context_points || [])
    .filter((p) => p.ok && p.draft && isNum(p.draft.acceptance_rate))
    .map((p) => p.draft.acceptance_rate);
  if (!rates.length) return null;
  return rates.reduce((a, b) => a + b, 0) / rates.length;
}

const HEADLINE_METRICS = [
  { key: 'ttft_p50', label: 'TTFT p50', unit: 'ms', better: 'lower', digits: 1, get: (r) => r.results.ttft_ms?.p50, fmt: fmtMs },
  { key: 'decode', label: 'Decode tok/s', unit: 'tok/s', better: 'higher', digits: 2, get: (r) => r.results.decode_tps?.mean, fmt: fmtTps },
  { key: 'prefill', label: 'Prefill tok/s', unit: 'tok/s', better: 'higher', digits: 1, get: (r) => prefillClosest(r, 4096).value, detail: (r) => { const p = prefillClosest(r, 4096); return isNum(p.ctx) ? `@${fmtCtx(p.ctx)}` : ''; }, fmt: fmtTps },
  { key: 'agg', label: 'Agg tok/s', unit: 'tok/s', better: 'higher', digits: 1, get: (r) => aggAtMax(r).value, detail: (r) => { const a = aggAtMax(r); return isNum(a.n) ? `@${fmtInt(a.n)}` : ''; }, fmt: fmtTps },
  { key: 'ttft_p95', label: 'TTFT p95', unit: 'ms', better: 'lower', digits: 1, get: (r) => r.results.ttft_ms?.p95, fmt: fmtMs },
  { key: 'prefill_mean', label: 'Prefill mean', unit: 'tok/s', better: 'higher', digits: 1, get: (r) => r.results.prompt_tps?.mean, fmt: fmtTps },
  { key: 'total', label: 'Total response p50', unit: 's', better: 'lower', digits: 2, get: (r) => r.results.total_s?.p50, fmt: fmtSec },
  { key: 'draft', label: 'Draft acceptance', unit: '%', better: 'higher', digits: 1, get: (r) => draftAcceptance(r), fmt: (v) => `${(v * 100).toFixed(1)}%` },
  { key: 'prompt_tokens', label: 'Prompt tokens', unit: 'tok', better: 'none', digits: 0, get: (r) => r.results.tokens?.prompt, fmt: fmtInt },
  { key: 'completion_tokens', label: 'Completion tokens', unit: 'tok', better: 'none', digits: 0, get: (r) => r.results.tokens?.completion, fmt: fmtInt },
  { key: 'load', label: 'Model load', unit: 's', better: 'lower', digits: 2, get: (r) => r.results.model_load_s, fmt: fmtSec },
  { key: 'vram_peak', label: 'VRAM peak', unit: 'MB', better: 'lower', digits: 0, get: (r) => (r.results.gpu.available ? r.results.gpu.peak.vram_mb : null), fmt: (v) => (isNum(v) ? NF.format(Math.round(v)) : 'n/a') },
  { key: 'power_peak', label: 'Power peak', unit: 'W', better: 'lower', digits: 0, get: (r) => (r.results.gpu.available ? r.results.gpu.peak.power_w : null), fmt: (v) => (isNum(v) ? Math.round(v) + ' W' : 'n/a') },
];

function deltaVerdict(diff, better) {
  if (Math.abs(diff) < 1e-9) return 'flat';
  if (better === 'none') return 'flat';
  return (better === 'higher') === diff > 0 ? 'up' : 'down';
}

function deltaCell(current, baseline, better, digits = 1) {
  const td = el('td', { class: 'num' });
  if (!isNum(current) || !isNum(baseline)) {
    td.textContent = 'n/a';
    td.classList.add('cell-ok');
    return td;
  }
  const diff = current - baseline;
  const verdict = deltaVerdict(diff, better);
  const pct = baseline !== 0 ? (diff / baseline) * 100 : null;
  const span = el('span', { class: 'delta', dataset: { sign: verdict } });
  append(span, [
    el('span', { text: fmtDelta(diff, digits) }),
    pct !== null ? el('span', { class: 'delta-pct', text: `(${pct > 0 ? '+' : ''}${pct.toFixed(1)}%)` }) : null,
  ]);
  td.appendChild(span);
  td.title = `${verdict === 'up' ? 'better' : verdict === 'down' ? 'worse' : 'unchanged'} than baseline`;
  return td;
}

function deltaChip(current, baseline, better, digits = 1) {
  if (!isNum(current) || !isNum(baseline)) return el('span', { class: 'delta-chip', text: 'n/a' });
  const diff = current - baseline;
  const verdict = deltaVerdict(diff, better);
  const pct = baseline !== 0 ? (diff / baseline) * 100 : null;
  const chip = el('span', { class: 'delta-chip', dataset: { sign: verdict } });
  append(chip, [
    icon(verdict === 'up' ? 'arrow-up' : verdict === 'down' ? 'arrow-down' : 'minus', 10),
    el('span', { text: fmtDelta(diff, digits) }),
    pct !== null ? el('span', { text: `${pct > 0 ? '+' : ''}${pct.toFixed(1)}%` }) : null,
  ]);
  return chip;
}

function renderCompareLegend(runs) {
  const box = $id('compare-legend');
  clear(box);
  append(
    box,
    runs.map((run, i) =>
      el(
        'span',
        { class: 'legend-item' },
        el('span', { class: 'swatch', style: `background:${runColor(i)}` }),
        el('span', { class: 'legend-label', text: run.label || run.id, title: `${run.label || run.id} (${run.id})` }),
        i === 0 ? el('span', { class: 'legend-base', text: 'baseline' }) : null
      )
    )
  );
}

function renderCompareHeadline(runs) {
  const table = $id('compare-headline');
  clear(table);
  const baseline = runs[0];
  const head = el('tr', {}, el('th', { text: 'Metric' }));
  runs.forEach((run, i) => {
    head.appendChild(
      el(
        'th',
        { class: 'num', title: `${run.label || run.id} (${run.id})` },
        el('span', { style: `color:${runColor(i)}`, text: shortLabel(run, 30) })
      )
    );
    if (i > 0) {
      head.appendChild(
        el('th', {
          class: 'num',
          text: runs.length === 2 ? 'Delta' : `Δ ${shortLabel(run, 10)}`,
          title: `delta vs baseline ${baseline.label || baseline.id}`,
        })
      );
    }
  });
  const thead = el('thead', {}, head);
  const tbody = el('tbody');

  append(
    tbody,
    HEADLINE_METRICS.map((metric) => {
      const tr = el('tr', {});
      append(tr, [
        el(
          'td',
          { class: 'label-cell' },
          el('span', { text: metric.label }),
          el('span', { class: 'cell-sub', text: metric.unit })
        ),
      ]);
      runs.forEach((run, i) => {
        const value = metric.get(run);
        const cell = el('td', { class: 'num', text: isNum(value) ? metric.fmt(value) : 'n/a' });
        if (isNum(value) && metric.detail) {
          const detail = metric.detail(run);
          if (detail) cell.appendChild(el('span', { class: 'cell-unit', text: detail }));
        }
        tr.appendChild(cell);
        if (i > 0) tr.appendChild(deltaCell(value, metric.get(baseline), metric.better, metric.digits));
      });
      return tr;
    })
  );
  append(table, [thead, tbody]);
}

function renderCompareContext(runs) {
  const table = $id('compare-context');
  clear(table);
  const baseline = runs[0];
  const allCtx = new Set();
  for (const run of runs) {
    for (const p of run.results.context_points || []) {
      if (isNum(p.ctx_requested)) allCtx.add(p.ctx_requested);
    }
  }
  const ctxList = [...allCtx].sort((a, b) => a - b);

  const head = el(
    'tr',
    {},
    el('th', { class: 'num', text: 'Context' }),
    el('th', { text: 'Run' }),
    el('th', { class: 'num', text: 'TTFT ms' }),
    el('th', { class: 'num', text: 'Δ TTFT' }),
    el('th', { class: 'num', text: 'Prefill tok/s' }),
    el('th', { class: 'num', text: 'Δ Prefill' }),
    el('th', { class: 'num', text: 'Decode tok/s' }),
    el('th', { class: 'num', text: 'Δ Decode' }),
    el('th', { class: 'num', text: 'Total s' }),
    el('th', { class: 'num', text: 'Δ Total' })
  );
  const thead = el('thead', {}, head);
  const tbody = el('tbody');

  if (!ctxList.length) {
    tbody.appendChild(
      el('tr', {}, el('td', { class: 'table-empty', colspan: '10', text: 'No context points in the selected runs.' }))
    );
  }

  for (const ctx of ctxList) {
    const basePoint = (baseline.results.context_points || []).find((p) => p.ctx_requested === ctx && p.ok);
    runs.forEach((run, i) => {
      const point = (run.results.context_points || []).find((p) => p.ctx_requested === ctx && p.ok);
      const tr = el('tr', i === 0 ? { class: 'group-start' } : {});
      if (i === 0) tr.appendChild(el('td', { class: 'num', text: fmtCtx(ctx), rowspan: String(runs.length) }));
      const runCell = el('td', { class: 'label-cell', title: run.label || run.id });
      append(runCell, [el('span', { style: `color:${runColor(i)}`, text: shortLabel(run, 26) })]);
      tr.appendChild(runCell);

      const baseVal = (metric) => (basePoint && isNum(basePoint[metric]) ? basePoint[metric] : null);
      const metrics = [
        { metric: 'ttft_ms', better: 'lower', fmt: fmtMs },
        { metric: 'prefill_tps', better: 'higher', fmt: fmtTps },
        { metric: 'decode_tps', better: 'higher', fmt: fmtTps },
        { metric: 'total_s', better: 'lower', fmt: fmtSec },
      ];
      for (const m of metrics) {
        const current = point ? point[m.metric] : null;
        tr.appendChild(el('td', { class: 'num', text: isNum(current) ? m.fmt(current) : 'n/a' }));
        tr.appendChild(
          i === 0
            ? el('td', { class: 'num cell-ok', text: '—' })
            : deltaCell(current, baseVal(m.metric), m.better, 1)
        );
      }
      tbody.appendChild(tr);
    });
  }
  append(table, [thead, tbody]);
}

function renderCompareCards(runs) {
  const runBox = $id('compare-run-cards');
  const ctxBox = $id('compare-ctx-cards');
  clear(runBox);
  clear(ctxBox);
  if (runs.length < 2) return;
  const baseline = runs[0];

  // one card per run: headline values with a delta chip against the baseline
  append(
    runBox,
    runs.map((run, i) => {
      const rows = HEADLINE_METRICS.slice(0, 10).map((metric) => {
        const value = metric.get(run);
        const baseValue = metric.get(baseline);
        const detail = isNum(value) && metric.detail ? metric.detail(run) : '';
        return {
          label: metric.label,
          value: isNum(value) ? `${metric.fmt(value)}${detail ? ` ${detail}` : ''}` : 'n/a',
          empty: !isNum(value),
          chip: i === 0 ? null : deltaChip(value, baseValue, metric.better, 1),
        };
      });
      return dataCard({
        title: shortLabel(run, 28),
        sub: `${run.model || ''}${run.model ? ' · ' : ''}${run.id}`,
        tag: el(
          'span',
          { class: 'card-swatch' },
          el('span', { class: 'swatch', style: `background:${runColor(i)}` }),
          i === 0 ? el('span', { class: 'legend-base', text: 'baseline' }) : null
        ),
        status: statusPill(run.status),
        rows,
      });
    })
  );

  // per-context card per rung: every run as a row with a delta chip
  const ctxSet = new Set();
  for (const run of runs) {
    for (const p of run.results.context_points || []) if (isNum(p.ctx_requested)) ctxSet.add(p.ctx_requested);
  }
  for (const ctx of [...ctxSet].sort((a, b) => a - b)) {
    const basePoint = (baseline.results.context_points || []).find((p) => p.ctx_requested === ctx && p.ok);
    const rows = runs.map((run, index) => {
      const point = (run.results.context_points || []).find((p) => p.ctx_requested === ctx && p.ok);
      const ttft = point ? point.ttft_ms : null;
      const base = basePoint ? basePoint.ttft_ms : null;
      return {
        label: shortLabel(run, 20),
        value: isNum(ttft) ? fmtMs(ttft) : 'n/a',
        empty: !isNum(ttft),
        chip: index === 0 ? null : deltaChip(ttft, base, 'lower', 1),
      };
    });
    append(ctxBox, [dataCard({ title: `ctx ${fmtCtx(ctx)}`, sub: 'TTFT ms · delta vs baseline', rows })]);
  }
}

function buildCompareCharts(runs) {
  for (const spec of [
    { id: 'cc-ttft', key: 'ttft_ms', fmt: fmtMs },
    { id: 'cc-prefill', key: 'prefill_tps', fmt: fmtTps },
    { id: 'cc-decode', key: 'decode_tps', fmt: fmtTps },
  ]) {
    setChartData(
      spec.id,
      runs.map((run) => ({ label: run.label || run.id, data: ctxSeries(run, spec.key) })),
      {
        overrides: {
          tooltipTitle: ctxTooltipTitle,
          tooltipLabel: ctxTooltipLabel(spec.fmt),
          xTick: fmtCtx,
          yTick: fmtAxis,
        },
      }
    );
  }
  setChartData(
    'cc-agg',
    runs.map((run) => ({ label: run.label || run.id, data: concSeries(run, 'agg_tps') })),
    {
      overrides: {
        tooltipTitle: (x) => `n = ${x}`,
        tooltipLabel: (item) => fmtTps(item.parsed.y),
        integerX: true,
        xTick: (v) => (Number.isInteger(v) ? String(v) : ''),
        yTick: fmtAxis,
      },
    }
  );
}

function renderCompare() {
  const emptyEl = $id('compare-empty');
  const bodyEl = $id('compare-body');
  const runs = state.compareRuns;
  if (runs.length < 2) {
    emptyEl.hidden = false;
    bodyEl.hidden = true;
    $id('compare-count').hidden = true;
    return;
  }
  emptyEl.hidden = true;
  bodyEl.hidden = false;
  $id('compare-count').hidden = false;
  $id('compare-count').textContent = String(runs.length);
  $id('compare-sub').textContent = `${runs.length} runs · baseline: ${runs[0].label || runs[0].id}`;
  renderCompareLegend(runs);
  renderCompareHeadline(runs);
  renderCompareContext(runs);
  renderCompareCards(runs);
  buildCompareCharts(runs);
}

async function openCompare(ids) {
  const list = (ids || state.compareIds || []).filter(Boolean);
  if (list.length < 2) {
    toast('Select at least two runs to compare', 'error');
    return;
  }
  const runs = [];
  for (const id of list.slice(0, 6)) {
    try {
      const saved = await localStore.get(id);
      if (!saved) continue;
      const run = normalizeRun(saved);
      run.saved_at = nv(saved.saved_at);
      if (run.id) runs.push(run);
    } catch {
      /* skip unreadable run */
    }
  }
  if (runs.length < 2) {
    toast('Those runs are not stored in this browser', 'error');
    return;
  }
  state.compareRuns = runs;
  state.compareIds = runs.map((r) => r.id);
  state.selected = new Set(state.compareIds);
  renderHistory();
  switchTab('compare');
}

async function refreshCompare() {
  if (!state.compareIds.length) return;
  const runs = [];
  for (const id of state.compareIds) {
    try {
      const saved = await localStore.get(id);
      if (!saved) continue;
      const run = normalizeRun(saved);
      run.saved_at = nv(saved.saved_at);
      if (run.id) runs.push(run);
    } catch {
      /* keep previous snapshot */
    }
  }
  if (runs.length >= 2) {
    state.compareRuns = runs;
    if (state.tab === 'compare') renderCompare();
  }
}

function clearCompareSelection() {
  state.selected = new Set();
  state.compareIds = [];
  state.compareRuns = [];
  renderHistory();
  renderCompare();
}

function setupCompare() {
  $id('history-compare').addEventListener('click', () => openCompare([...state.selected]));
  $id('compare-clear').addEventListener('click', () => clearCompareSelection());
  $id('history-retry').addEventListener('click', () => loadHistory());
}

/* ------------------------------ action bar ------------------------------- */

/** Sticky bottom action bar (phone layout): contextual primary action. */
function renderActionBar() {
  const bar = $id('action-bar');
  const primary = $id('action-primary');
  const secondary = $id('action-secondary');
  bar.hidden = false;
  clear(primary);
  clear(secondary);

  if (state.tab === 'run') {
    secondary.hidden = true;
    if (state.run && isActive(state.run.status)) {
      primary.className = 'btn btn-danger';
      append(primary, [icon('x', 15), 'Cancel run']);
      primary.onclick = () => cancelRun();
    } else {
      primary.className = 'btn btn-primary';
      append(primary, [icon('play', 15), 'Run benchmark']);
      primary.onclick = () => $id('run-form').requestSubmit();
    }
    return;
  }

  if (state.tab === 'history') {
    primary.className = 'btn btn-primary';
    append(primary, [
      icon('columns', 15),
      state.selected.size ? `Compare ${state.selected.size}` : 'Compare selected',
    ]);
    primary.disabled = state.selected.size < 2;
    primary.onclick = () => openCompare([...state.selected]);
    secondary.hidden = false;
    append(secondary, [icon('refresh', 15), 'Refresh']);
    secondary.onclick = () => loadHistory();
    return;
  }

  primary.className = 'btn btn-ghost';
  append(primary, [icon('x', 15), 'Clear selection']);
  primary.onclick = () => clearCompareSelection();
  secondary.hidden = true;
}

/* --------------------------------- tabs ---------------------------------- */

const TAB_NAMES = ['run', 'history', 'compare'];

function switchTab(name, { focus = false } = {}) {
  if (!TAB_NAMES.includes(name)) name = 'run';
  state.tab = name;
  for (const tab of TAB_NAMES) {
    const btn = $id(`tabbtn-${tab}`);
    const panel = $id(`tab-${tab}`);
    const selected = tab === name;
    btn.setAttribute('aria-selected', selected ? 'true' : 'false');
    btn.tabIndex = selected ? 0 : -1;
    panel.hidden = !selected;
    if (selected && focus) btn.focus();
  }
  requestAnimationFrame(() => resizeAllCharts());
  if (name === 'history') loadHistory({ silent: true });
  if (name === 'compare') renderCompare();
  renderActionBar();
}

function setupTabs() {
  const buttons = TAB_NAMES.map((tab) => $id(`tabbtn-${tab}`));
  buttons.forEach((btn, index) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    btn.addEventListener('keydown', (event) => {
      let next = null;
      if (event.key === 'ArrowRight') next = (index + 1) % buttons.length;
      else if (event.key === 'ArrowLeft') next = (index - 1 + buttons.length) % buttons.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = buttons.length - 1;
      if (next === null) return;
      event.preventDefault();
      switchTab(buttons[next].dataset.tab, { focus: true });
    });
  });
}

/* -------------------------------- health --------------------------------- */

function fmtWindow(seconds) {
  if (!isNum(seconds)) return '—';
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600} h`;
  if (seconds % 60 === 0) return `${seconds / 60} min`;
  return `${Math.round(seconds)} s`;
}

/** Small print under the Run button: the instance's per-IP abuse guard. */
function updateRateLimitNote() {
  const note = $id('rate-limit-note');
  if (!note) return;
  const limit = state.config && state.config.limits ? state.config.limits.rate_limit : null;
  if (!limit || !isNum(limit.runs)) {
    note.hidden = true;
    return;
  }
  note.hidden = false;
  note.textContent = `Limit: ${limit.runs} runs / ${fmtWindow(limit.window_s)} per IP.`;
}

async function loadConfig() {
  const dot = $id('health-dot');
  try {
    const data = await api('/api/config', { timeout: 8000 });
    state.config = data && typeof data === 'object' ? data : {};
    const version = sv(state.config.version);
    $id('app-version').textContent = version ? `v${version}` : 'v—';
    dot.dataset.state = 'ok';
    $id('health-text').textContent = 'online';
    dot.title = `Backend online${version ? ' · v' + version : ''}`;
  } catch {
    state.config = null;
    dot.dataset.state = 'bad';
    $id('health-text').textContent = 'offline';
    dot.title = 'Backend unreachable';
  } finally {
    updateRateLimitNote();
  }
}

/** GET /api/stats: aggregate counters only (the run list is withdrawn). */
async function loadStats() {
  try {
    const data = await api('/api/stats', { timeout: 8000 });
    state.stats = data && typeof data === 'object' ? data : {};
    const active = nv(state.stats.active) ?? 0;
    const finished = nv(state.stats.finished) ?? 0;
    $id('meta-runs').textContent = `${active} active`;
    $id('meta-runs').title = `${active} running · ${finished} recently finished · server memory only`;
  } catch {
    $id('meta-runs').textContent = 'offline';
  }
}

/* --------------------------------- toast --------------------------------- */

function toast(message, tone = 'ok') {
  const box = $id('toasts');
  if (!box) return;
  const item = el(
    'div',
    { class: 'toast-item', dataset: { tone }, role: 'status' },
    icon(tone === 'error' ? 'alert-triangle' : tone === 'warn' ? 'info' : 'check', 14),
    el('span', { class: 'toast-text', text: message })
  );
  box.appendChild(item);
  while (box.childElementCount > 2) box.removeChild(box.firstChild);
  setTimeout(() => {
    item.classList.add('is-out');
    setTimeout(() => item.remove(), 200);
  }, 3000);
}

/* --------------------------------- init ---------------------------------- */

function setupMisc() {
  $id('history-refresh').addEventListener('click', () => loadHistory());
  const clearAllBtn = $id('history-clear-all');
  if (clearAllBtn) clearAllBtn.addEventListener('click', () => clearAllRuns());
  $id('history-select-all').addEventListener('change', (event) => {
    const checked = event.target.checked;
    for (const row of state.history) {
      if (checked) state.selected.add(row.id);
      else state.selected.delete(row.id);
    }
    renderHistory();
  });
  const syncSummary = () => {
    const contexts = parseList($id('cfg-contexts').value, { min: 16 });
    const concurrency = parseList($id('cfg-concurrency').value, { min: 1 });
    $id('adv-summary').textContent = `${contexts.length} ctx · ${concurrency.length} conc`;
  };
  for (const id of ['cfg-contexts', 'cfg-concurrency']) {
    $id(id).addEventListener('input', syncSummary);
    $id(id).addEventListener('change', syncSummary);
  }
  syncSummary();
  renderActionBar();
}

function parseQuery() {
  const params = new URLSearchParams(window.location.search);
  return { run: params.get('run') || '', tab: params.get('tab') || '' };
}

async function init() {
  try {
    setupTabs();
    setupForm();
    setupCompare();
    setupMisc();
    await Promise.allSettled([loadConfig(), loadPresets(), loadHistory({ silent: true }), loadStats()]);
    setInterval(() => {
      loadStats();
    }, 30000);
    const query = parseQuery();
    if (query.tab) switchTab(query.tab);
    if (query.run) {
      const run = await openRun(query.run);
      if (run) switchTab('run');
    }
  } catch {
    /* never let an init failure surface as an uncaught error */
  }
}

init();
