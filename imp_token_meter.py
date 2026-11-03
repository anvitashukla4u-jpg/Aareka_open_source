#!/usr/bin/env python3
"""
Aareka  .  Token meter   (customer side, read-only)

Measures the two raw quantities behind "tokens per watt" for inference servers
the customer already runs. It does NOT run models and does NOT compute any
efficiency figure - it only measures and hands raw numbers to the sender:

    * GPU energy   - nvidia-smi power.draw streamed every ~0.5 s and integrated
                     into joules per GPU (trapezoid rule)
    * CPU energy   - Linux RAPL package counters, only when readable without root
    * tokens       - the inference server's own Prometheus counters
                     (vLLM, SGLang, TGI, llama.cpp with --metrics), as deltas
                     per model over the window

A background thread polls the token counters every few seconds so it can also
report how long each server was actually generating ("busy") and the GPU
energy used during that busy time.

Endpoint spec (AAREKA_METRICS_URL), entries separated by ";":
    <metrics url>[|model=<label>][|gpus=<i,j>]
    e.g.  http://127.0.0.1:8080/metrics|model=llama3.2-3b|gpus=0
  model= labels servers whose counters carry no model name (llama.cpp);
  gpus=  binds a server to the GPU indices it runs on (default: all GPUs).

Standard library only.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# GPU energy (nvidia-smi)
# --------------------------------------------------------------------------


class GpuEnergyMeter(object):
    """Streams power.draw from nvidia-smi and integrates it into joules per GPU."""

    def __init__(self, gpus=None, interval_ms=500):
        self.gpus = gpus  # list of int indices, or None for all
        self.interval_ms = interval_ms
        self.lock = threading.Lock()
        self.samples = []  # (t, {gpu: watts})
        self.proc = None
        self.thread = None
        self.error = None

    def start(self):
        exe = shutil.which("nvidia-smi")
        if not exe:
            raise RuntimeError("nvidia-smi was not found on this machine")
        cmd = [exe, "--query-gpu=index,power.draw", "--format=csv,noheader,nounits",
               "-lms", str(self.interval_ms)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     universal_newlines=True, bufsize=1)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            with self.lock:
                if self.samples:
                    return
            time.sleep(0.1)
        raise RuntimeError("nvidia-smi did not return any power readings")

    def _read(self):
        pending = {}
        expected = None
        try:
            for line in self.proc.stdout:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 2:
                    continue
                try:
                    idx = int(parts[0])
                    watts = float(parts[1])
                except ValueError:
                    continue  # "[N/A]" or similar
                if self.gpus is not None and idx not in self.gpus:
                    continue
                if idx in pending:
                    # a new round started: store the previous one
                    self._store(pending)
                    expected = len(pending)
                    pending = {}
                pending[idx] = watts
                if expected is not None and len(pending) == expected:
                    self._store(pending)
                    pending = {}
        except Exception as e:  # pragma: no cover
            self.error = str(e)

    def _store(self, reading):
        with self.lock:
            self.samples.append((time.time(), dict(reading)))
            # keep about an hour of samples; windows are far shorter
            if len(self.samples) > 8000:
                del self.samples[:1000]

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                pass

    def gpu_list(self):
        with self.lock:
            if not self.samples:
                return []
            return sorted(self.samples[-1][1].keys())

    def energy(self, t0, t1, gpus=None):
        """{gpu: joules} between t0 and t1 for the given GPU indices (default all)."""
        with self.lock:
            samples = list(self.samples)
        if not samples:
            return {}
        keys = gpus if gpus is not None else sorted(samples[-1][1].keys())
        out = {}
        for g in keys:
            pts = [(t, r[g]) for t, r in samples if g in r]
            out[g] = _integrate(pts, t0, t1)
        return out


def _integrate(pts, t0, t1):
    """Joules from (t, watts) points between t0 and t1 (trapezoid rule). Holds the
    first/last value flat where the window runs past the samples."""
    if not pts or t1 <= t0:
        return 0.0
    total = 0.0
    first_t, first_w = pts[0]
    if t0 < first_t:
        total += first_w * (min(first_t, t1) - t0)
    for (pt, pw), (t, w) in zip(pts, pts[1:]):
        a, b = max(pt, t0), min(t, t1)
        if b > a:
            # linear interpolation of the segment, clipped to [t0, t1]
            span = t - pt
            wa = pw + (w - pw) * (a - pt) / span
            wb = pw + (w - pw) * (b - pt) / span
            total += (wa + wb) / 2.0 * (b - a)
    last_t, last_w = pts[-1]
    if t1 > last_t:
        total += last_w * (t1 - max(last_t, t0))
    return total


# --------------------------------------------------------------------------
# CPU package energy (Linux RAPL, only if readable without root)
# --------------------------------------------------------------------------


class Rapl(object):
    def __init__(self):
        self.zones = []
        for d in sorted(glob.glob("/sys/class/powercap/intel-rapl:[0-9]*")):
            if d.count(":") != 1:
                continue  # sub-zones (core, uncore, dram) are inside the package
            try:
                with open(os.path.join(d, "name")) as f:
                    name = f.read().strip()
                if not name.startswith("package"):
                    continue
                with open(os.path.join(d, "energy_uj")) as f:
                    int(f.read())
                rng = 2 ** 32
                try:
                    with open(os.path.join(d, "max_energy_range_uj")) as f:
                        rng = int(f.read())
                except Exception:
                    pass
                self.zones.append((d, rng))
            except Exception:
                continue  # unreadable without root on most recent kernels
        self.available = bool(self.zones)
        self._last = None
        self.joules = 0.0
        self.lock = threading.Lock()

    def _read(self):
        vals = []
        for d, _ in self.zones:
            with open(os.path.join(d, "energy_uj")) as f:
                vals.append(int(f.read()))
        return vals

    def start(self):
        if self.available:
            self._last = self._read()

    def tick(self):
        if not self.available:
            return
        try:
            cur = self._read()
        except Exception:
            return
        add = 0.0
        for (d, rng), a, b in zip(self.zones, self._last, cur):
            delta = b - a if b >= a else b + rng - a  # counter wrap
            add += delta / 1e6
        with self.lock:
            self.joules += add
        self._last = cur

    def take(self):
        """Joules since the last take()."""
        self.tick()
        with self.lock:
            j, self.joules = self.joules, 0.0
        return j


# --------------------------------------------------------------------------
# Token counters (Prometheus /metrics of the inference server)
# --------------------------------------------------------------------------

# (server, prompt-token counter, output-token counter, requests-running gauge).
# Some servers (llama.cpp) only bump the token counters when a request finishes,
# so the running gauge is what tells us the GPU was generating mid-request.
METRIC_SETS = [
    ("vLLM", "vllm:prompt_tokens_total", "vllm:generation_tokens_total",
     "vllm:num_requests_running"),
    ("SGLang", "sglang:prompt_tokens_total", "sglang:generation_tokens_total",
     "sglang:num_running_reqs"),
    ("llama.cpp", "llamacpp:prompt_tokens_total", "llamacpp:tokens_predicted_total",
     "llamacpp:requests_processing"),
    ("TGI", "tgi_request_input_length_sum", "tgi_request_generated_tokens_sum",
     "tgi_batch_current_size"),
]

_MODEL_LABEL = re.compile(r'(?:model_name|model)="([^"]*)"')


def parse_prometheus(text):
    """Return {metric_name: [(labels_str, value)]}."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "{" in line:
            name, rest = line.split("{", 1)
            labels, _, val = rest.rpartition("}")
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            name, labels, val = parts[0], "", parts[1]
        try:
            v = float(val.split()[0])
        except (ValueError, IndexError):
            continue
        out.setdefault(name.strip(), []).append((labels, v))
    return out


def token_counters(text, default_model="default"):
    """(server, {model: (prompt_total, output_total)}, requests_running) from a
    /metrics page."""
    m = parse_prometheus(text)
    for server, pm, om, rg in METRIC_SETS:
        if om not in m:
            continue
        totals = {}
        for metric, slot in ((pm, 0), (om, 1)):
            for labels, v in m.get(metric, []):
                hit = _MODEL_LABEL.search(labels)
                model = hit.group(1) if hit else default_model
                cur = totals.setdefault(model, [0.0, 0.0])
                cur[slot] += v
        running = sum(v for _, v in m.get(rg, []))
        return server, {k: (v[0], v[1]) for k, v in totals.items()}, running
    raise RuntimeError("no token counters found (supported: %s; llama.cpp needs --metrics)"
                       % ", ".join(s[0] for s in METRIC_SETS))


def parse_endpoints(spec):
    """AAREKA_METRICS_URL -> [{"url", "model", "gpus"}]."""
    eps = []
    for entry in (spec or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        ep = {"url": parts[0].strip(), "model": None, "gpus": None}
        for p in parts[1:]:
            k, _, v = p.partition("=")
            k, v = k.strip().lower(), v.strip()
            if k == "model" and v:
                ep["model"] = v
            elif k == "gpus" and v:
                ep["gpus"] = [int(x) for x in v.split(",") if x.strip() != ""]
        eps.append(ep)
    return eps


def _fetch(url, timeout=10):
    req = urllib.request.Request(url, headers={"Accept": "text/plain"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Inference meter: energy + token deltas per send window
# --------------------------------------------------------------------------


class InferenceMeter(object):
    """Runs between sends. snapshot() returns the raw measurements for the window
    since the previous snapshot and starts a new window."""

    def __init__(self, endpoints, poll_s=5.0, interval_ms=500, fetch=_fetch, gpu_meter=None):
        self.endpoints = [dict(ep) for ep in endpoints]
        self.poll_s = poll_s
        self.fetch = fetch
        self.gpu = gpu_meter or GpuEnergyMeter(interval_ms=interval_ms)
        self.rapl = Rapl()
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.window_start = None

    def start(self):
        self.gpu.start()
        self.rapl.start()
        now = time.time()
        for ep in self.endpoints:
            ep.update(server=None, last=None, last_t=now, running=0.0, error=None)
            self._new_window(ep)
            try:
                ep["server"], ep["last"], ep["running"] = token_counters(
                    self.fetch(ep["url"]), ep["model"] or "default")
            except Exception as e:
                ep["error"] = str(e)
        self.window_start = now
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.gpu.stop()

    @staticmethod
    def _new_window(ep):
        ep["tokens"] = {}
        ep["busy_s"] = 0.0
        ep["busy_j"] = 0.0
        ep["reset"] = False

    def _loop(self):
        while self.running:
            time.sleep(self.poll_s)
            with self.lock:
                self._poll()

    def _poll(self):
        now = time.time()
        for ep in self.endpoints:
            try:
                server, cur, running = token_counters(self.fetch(ep["url"]),
                                                      ep["model"] or "default")
            except Exception as e:
                ep["error"] = str(e)
                continue
            ep["server"], ep["error"] = server, None
            if ep["last"] is None:
                # first successful read: this is the baseline, nothing to count yet
                ep["last"], ep["last_t"], ep["running"] = cur, now, running
                continue
            last = ep["last"]
            if any(m in last and (c[0] < last[m][0] or c[1] < last[m][1])
                   for m, c in cur.items()):
                # server restarted and counters reset: count from zero again
                ep["reset"] = True
                last = {}
            produced = 0.0
            for m, (p, o) in cur.items():
                lp, lo = last.get(m, (0.0, 0.0))
                t = ep["tokens"].setdefault(m, [0.0, 0.0])
                t[0] += p - lp
                t[1] += o - lo
                produced += o - lo
            # busy if tokens came out, or a request was running at either end
            if produced > 0 or running > 0 or ep["running"] > 0:
                ep["busy_s"] += now - ep["last_t"]
                ep["busy_j"] += sum(self.gpu.energy(ep["last_t"], now, ep["gpus"]).values())
            ep["last"], ep["last_t"], ep["running"] = cur, now, running

    def snapshot(self, dept_map=None):
        """Raw measurements for the window since the last snapshot."""
        dept_map = dept_map or {}
        with self.lock:
            self._poll()
            t0, t1 = self.window_start, time.time()
            gpu_j = self.gpu.energy(t0, t1)
            cpu_j = self.rapl.take() if self.rapl.available else None
            eps = []
            for ep in self.endpoints:
                gpus = ep["gpus"] if ep["gpus"] is not None else sorted(gpu_j)
                eps.append({
                    "url": ep["url"],
                    "server": ep["server"],
                    "gpu_ids": ["gpu%d" % g for g in gpus],
                    "busy_s": round(ep["busy_s"], 2),
                    "busy_gpu_energy_j": round(ep["busy_j"], 2),
                    "counter_reset": ep["reset"],
                    "error": ep["error"],
                    "models": [{"model": m, "prompt_tokens": int(round(p)),
                                "output_tokens": int(round(o)),
                                "department": dept_map.get(m)}
                               for m, (p, o) in sorted(ep["tokens"].items())],
                })
                self._new_window(ep)
            self.window_start = t1
        return {
            "window_start": _iso(t0), "window_end": _iso(t1),
            "window_s": round(t1 - t0, 2),
            "sample_interval_ms": self.gpu.interval_ms,
            "gpu_energy_j": {"gpu%d" % g: round(j, 2) for g, j in sorted(gpu_j.items())},
            "cpu_energy_j": round(cpu_j, 2) if cpu_j is not None else None,
            "not_measured": ["memory", "fans", "power supply losses", "cooling"]
                            + ([] if cpu_j is not None else ["CPU"]),
            "endpoints": eps,
        }
