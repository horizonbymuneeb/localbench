#!/usr/bin/env python3
"""LocalBench GPU telemetry agent (stdlib only, Python 3.8+).

Samples the local NVIDIA GPU with ``nvidia-smi`` plus host RAM with ``free -m``
and POSTs small batches to a LocalBench backend while a benchmark run is live.
Optional ``--restart-cmd`` / ``--ready-url`` measure model load time.

Typical use (the one-liner the web UI hands out)::

    curl -sO http://127.0.0.1:21700/bench_agent.py
    python3 bench_agent.py --server http://127.0.0.1:21700 --run r_ab12cd34 --key ak_...

Exit codes
----------
0  success (run finished, watch window elapsed, or Ctrl-C)
1  unexpected error
2  nvidia-smi missing / no NVIDIA GPU visible  (message on stderr)
3  backend rejected the agent key or run id
4  backend unreachable (gave up after 5 consecutive failures)
5  bad command line
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

VERSION = "1.0.0"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_GPU = 2
EXIT_AUTH = 3
EXIT_NETWORK = 4
EXIT_USAGE = 5

POST_EVERY_S = 1.0
BATCH_MAX = 8
MAX_POST_FAILURES = 5
TERMINAL_STATUSES = ("done", "failed", "cancelled")

NVIDIA_SMI_ARGS = [
    "nvidia-smi",
    "--query-gpu=memory.used,memory.total,utilization.gpu,power.draw,temperature.gpu",
    "--format=csv,noheader,nounits",
]


class NetworkError(Exception):
    """The backend could not be reached."""


# --------------------------------------------------------------------------- #
# local sampling
# --------------------------------------------------------------------------- #
def _run(cmd, timeout=15.0, shell=False):
    """Run a command, never raise.  Returns (rc, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            shell=shell,
        )
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )
    except FileNotFoundError:
        return 127, "", "%s: not found" % (cmd if isinstance(cmd, str) else cmd[0])
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %.0fs" % timeout
    except Exception as exc:  # noqa: BLE001 - agent must never crash here
        return 1, "", "%s: %s" % (exc.__class__.__name__, exc)


def _num(text, default=0.0):
    """Parse the first number out of an nvidia-smi cell ('[N/A]' -> 0.0)."""
    if text is None:
        return default
    match = re.search(r"-?\d+(?:\.\d+)?", str(text))
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def read_gpu():
    """Return (sample_dict|None, error_message|None) from nvidia-smi."""
    rc, out, err = _run(NVIDIA_SMI_ARGS)
    if rc == 127:
        return None, "nvidia-smi not found on PATH"
    if rc != 0:
        return None, (err.strip() or "nvidia-smi exited %s" % rc)
    line = ""
    for candidate in out.strip().splitlines():
        if candidate.strip():
            line = candidate.strip()
            break
    if not line:
        return None, "nvidia-smi returned no output (no GPU visible?)"
    parts = [part.strip() for part in line.split(",")]
    while len(parts) < 5:
        parts.append("")
    sample = {
        "vram_mb": _num(parts[0]),
        "vram_total_mb": _num(parts[1]),
        "gpu_util": _num(parts[2]),
        "power_w": _num(parts[3]),
        "temp_c": _num(parts[4]),
    }
    return sample, None


def read_ram_mb():
    """Used host RAM in MB (``free -m`` 'used' column)."""
    rc, out, _err = _run(["free", "-m"])
    if rc != 0:
        return 0.0
    for line in out.splitlines():
        if line.strip().lower().startswith("mem:"):
            parts = line.split()
            if len(parts) >= 3:
                return _num(parts[2])
    return 0.0


def sample_once(phase):
    gpu, err = read_gpu()
    if gpu is None:
        return None, err
    sample = {
        "ts": round(time.time(), 3),
        "vram_mb": gpu["vram_mb"],
        "ram_mb": read_ram_mb(),
        "gpu_util": gpu["gpu_util"],
        "power_w": gpu["power_w"],
        "temp_c": gpu["temp_c"],
        "phase": phase or "",
    }
    return sample, None


# --------------------------------------------------------------------------- #
# HTTP helpers (urllib, stdlib only)
# --------------------------------------------------------------------------- #
def _ssl_context(insecure):
    if not insecure:
        return None
    try:
        return ssl._create_unverified_context()  # noqa: SLF001 - deliberate --insecure
    except Exception:  # pragma: no cover
        return None


def post_json(url, payload, headers=None, insecure=False, timeout=20.0):
    """POST JSON; returns (status_code, parsed_body).  Raises NetworkError."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "localbench-agent/%s" % VERSION)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_ssl_context(insecure)
        ) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, _parse(raw)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover
            raw = ""
        return exc.code, _parse(raw)
    except urllib.error.URLError as exc:
        raise NetworkError(str(getattr(exc, "reason", exc)))
    except OSError as exc:
        raise NetworkError(str(exc))


def _parse(raw):
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"raw": raw[:500]}
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def wait_ready(url, timeout_s, insecure=False, interval=0.5):
    """Poll ``url`` until it answers HTTP 200.  Returns the timestamp or None."""
    deadline = time.time() + timeout_s
    context = _ssl_context(insecure)
    while time.time() < deadline:
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=5.0, context=context) as response:
                if 200 <= response.status < 300:
                    return time.time()
        except urllib.error.HTTPError as exc:
            if 200 <= exc.code < 300:
                return time.time()
        except Exception:  # noqa: BLE001 - not ready yet
            pass
        time.sleep(interval)
    return None


# --------------------------------------------------------------------------- #
# model load timing
# --------------------------------------------------------------------------- #
def measure_model_load(args):
    """Run ``--restart-cmd`` then time readiness.  Returns seconds or None."""
    print("[agent] restart-cmd: %s" % args.restart_cmd)
    started = time.time()
    rc, out, err = _run(args.restart_cmd, timeout=args.restart_timeout, shell=True)
    exited = time.time()
    print(
        "[agent] restart-cmd exited rc=%s after %.2fs"
        % (rc, exited - started)
    )
    if out.strip():
        print("[agent] stdout: %s" % out.strip()[-500:])
    if err.strip():
        print("[agent] stderr: %s" % err.strip()[-500:], file=sys.stderr)
    if args.ready_url:
        ready_at = wait_ready(args.ready_url, args.ready_timeout, insecure=args.insecure)
        if ready_at is None:
            print(
                "[agent] readiness URL %s never answered 200 within %.0fs"
                % (args.ready_url, args.ready_timeout),
                file=sys.stderr,
            )
            return None
        seconds = round(ready_at - exited, 3)
        print(
            "[agent] model load time: %.2fs (command exit -> %s ready)"
            % (seconds, args.ready_url)
        )
        return seconds
    seconds = round(exited - started, 3)
    print("[agent] model load time: %.2fs (no --ready-url; command duration)" % seconds)
    return seconds


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        prog="bench_agent.py",
        description="LocalBench GPU telemetry agent (nvidia-smi + free -m).",
        epilog=(
            "exit codes: 0 ok, 1 error, 2 no nvidia-smi/GPU, "
            "3 bad agent key or run id, 4 backend unreachable, 5 bad arguments"
        ),
    )
    parser.add_argument(
        "--server",
        default=os.environ.get("LOCALBENCH_SERVER", ""),
        help="LocalBench base URL, e.g. http://127.0.0.1:21700",
    )
    parser.add_argument("--run", default="", help="run id, e.g. r_ab12cd34")
    parser.add_argument("--key", default="", help="agent key from the run's Agent panel")
    parser.add_argument(
        "--interval",
        type=float,
        default=0.25,
        help="seconds between nvidia-smi samples (default 0.25)",
    )
    parser.add_argument(
        "--restart-cmd",
        default="",
        help="shell command that reloads the model (e.g. systemctl --user restart llama-server)",
    )
    parser.add_argument(
        "--ready-url",
        default="",
        help="URL that answers 200 once the model is loaded (e.g. http://127.0.0.1:8080/v1/models)",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=600.0,
        help="max seconds to wait for --ready-url (default 600)",
    )
    parser.add_argument(
        "--restart-timeout",
        type=float,
        default=600.0,
        help="max seconds for --restart-cmd (default 600)",
    )
    parser.add_argument(
        "--watch",
        type=float,
        default=0,
        help="stop after this many seconds; 0 = until the run reaches a terminal status",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="sample once, print the JSON body that would be posted, exit 0",
    )
    parser.add_argument(
        "--insecure", action="store_true", help="skip TLS certificate verification"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="print every POST result"
    )
    parser.add_argument("--version", action="version", version="bench_agent.py %s" % VERSION)
    return parser


def _telemetry_url(server, run_id):
    return server.rstrip("/") + "/api/runs/" + run_id + "/telemetry"


def run_dry(args):
    """Print exactly what a live agent would POST.  Never needs a GPU."""
    sample, gpu_err = sample_once("idle")
    if sample is None:
        print("[agent] dry-run: nvidia-smi unavailable (%s)" % gpu_err)
        print(
            "[agent] live mode would print that message and exit %d "
            "(no GPU telemetry is invented)" % EXIT_NO_GPU
        )
        sample = {
            "ts": round(time.time(), 3),
            "vram_mb": 0.0,
            "ram_mb": read_ram_mb(),
            "gpu_util": 0.0,
            "power_w": 0.0,
            "temp_c": 0.0,
            "phase": "idle",
        }
    body = {"samples": [sample]}
    url = _telemetry_url(args.server or "http://127.0.0.1:21700", args.run or "r_dryrun00")
    print("[agent] would POST to %s" % url)
    print("[agent] header X-Agent-Key: <redacted>")
    print(json.dumps(body))
    if args.restart_cmd:
        print("[agent] would run --restart-cmd: %s" % args.restart_cmd)
        if args.ready_url:
            print("[agent] then poll %s for HTTP 200 and POST model_load_s" % args.ready_url)
    return EXIT_OK


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.dry_run:
        return run_dry(args)

    missing = [name for name in ("server", "run", "key") if not getattr(args, name)]
    if missing:
        parser.error("missing required arguments: --" + ", --".join(missing))
    if args.interval <= 0:
        parser.error("--interval must be > 0")

    url = _telemetry_url(args.server, args.run)
    headers = {"X-Agent-Key": args.key}

    # Fail fast and loudly when there is no GPU (CONTRACT.md).
    probe, gpu_err = read_gpu()
    if probe is None:
        print("[agent] ERROR: cannot read GPU: %s" % gpu_err, file=sys.stderr)
        print(
            "[agent] this agent needs an NVIDIA GPU visible to nvidia-smi; "
            "install the driver or skip the agent (the run works without it)",
            file=sys.stderr,
        )
        return EXIT_NO_GPU

    print("[agent] LocalBench telemetry agent %s" % VERSION)
    print("[agent] server=%s run=%s interval=%.2fs" % (args.server, args.run, args.interval))

    # Optional model-load timing happens before the sample loop.
    if args.restart_cmd:
        model_load_s = measure_model_load(args)
        if model_load_s is not None:
            try:
                code, body = post_json(
                    url, {"model_load_s": model_load_s}, headers, insecure=args.insecure
                )
                if code >= 400:
                    print(
                        "[agent] backend rejected model_load_s (HTTP %s): %s"
                        % (code, json.dumps(body)[:200]),
                        file=sys.stderr,
                    )
                    if code in (401, 403, 404):
                        return EXIT_AUTH
            except NetworkError as exc:
                print("[agent] cannot reach backend: %s" % exc, file=sys.stderr)
                return EXIT_NETWORK

    deadline = time.time() + args.watch if args.watch and args.watch > 0 else None
    phase = "idle"
    batch = []
    next_post = time.time() + POST_EVERY_S
    failures = 0
    posted = 0
    exit_code = EXIT_OK

    try:
        while True:
            if deadline is not None and time.time() >= deadline:
                print("[agent] watch window (%.0fs) elapsed" % args.watch)
                break
            sample, err = sample_once(phase)
            if sample is None:
                print("[agent] sampling failed: %s" % err, file=sys.stderr)
                exit_code = EXIT_NO_GPU
                break
            batch.append(sample)

            now = time.time()
            if not batch or (now < next_post and len(batch) < BATCH_MAX):
                time.sleep(args.interval)
                continue

            try:
                code, body = post_json(
                    url, {"samples": batch}, headers, insecure=args.insecure
                )
            except NetworkError as exc:
                failures += 1
                print(
                    "[agent] POST failed (%d/%d): %s"
                    % (failures, MAX_POST_FAILURES, exc),
                    file=sys.stderr,
                )
                batch = []
                next_post = time.time() + POST_EVERY_S
                if failures >= MAX_POST_FAILURES:
                    print("[agent] giving up: backend unreachable", file=sys.stderr)
                    return EXIT_NETWORK
                continue

            if code in (401, 403, 404):
                print(
                    "[agent] backend rejected this agent key / run id (HTTP %s): %s"
                    % (code, json.dumps(body)[:200]),
                    file=sys.stderr,
                )
                return EXIT_AUTH
            if code >= 400:
                failures += 1
                print(
                    "[agent] backend error HTTP %s: %s"
                    % (code, json.dumps(body)[:200]),
                    file=sys.stderr,
                )
                batch = []
                next_post = time.time() + POST_EVERY_S
                continue

            failures = 0
            batch = []
            next_post = time.time() + POST_EVERY_S
            posted += 1
            status = body.get("status") or "running"
            phase = body.get("phase") or phase
            if posted == 1:
                print(
                    "[agent] streaming telemetry to %s (run status: %s)"
                    % (url, status)
                )
            if args.verbose:
                print(
                    "[agent] post #%d ok, samples=%s, phase=%s, status=%s"
                    % (posted, body.get("samples"), phase, status)
                )
            if status in TERMINAL_STATUSES:
                print("[agent] run reached terminal status: %s" % status)
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("[agent] interrupted by user")
    finally:
        if batch:
            try:
                post_json(url, {"samples": batch}, headers, insecure=args.insecure)
            except NetworkError:
                pass
    print("[agent] done: %d batches posted" % posted)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
