#!/usr/bin/env python3
"""
IMP  .  SERVER POWER SOURCE

Reads a server's power from whatever telemetry the site exposes. Two source
types, each a FALLBACK LADDER (try most-granular first, stop at first answer,
report the tier):

  GPU power
    1 dcgm_exporter  Prometheus scrape of dcgm-exporter    per-GPU  MEASURED
    2 dcgm_api       dcgmi / nv-hostengine                 per-GPU  MEASURED
    3 nvml           nvidia-smi / NVML on the node         per-GPU  MEASURED
    4 redfish        BMC node total (no GPU granularity)    node     MEASURED
    5 pdu            PDU / DCIM rack power                   rack     MEASURED*

  CPU / node power (also the non-GPU part of a GPU box, and CPU-only servers)
    1 node_exporter  Prometheus scrape of node-exporter RAPL MEASURED
    2 rapl           /sys/class/powercap on the node (Linux) MEASURED
    3 redfish        BMC node total (GPU + CPU boxes alike)  MEASURED
    4 ipmi           ipmitool dcmi power reading             MEASURED
    5 pdu            PDU / DCIM rack power                    MEASURED*
    6 model          psutil CPU-util estimate                MODELED (last resort)

Redfish is the shared backbone: one BMC call gives node-total wattage for any
server. read_server_power() runs both ladders and resolves double-counting.
READ-ONLY throughout - no rung ever writes.
"""

from __future__ import annotations

import glob
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ========================================================== shared types
class Method(str, Enum):
    MEASURED = "measured"
    MODELED = "modeled"


@dataclass
class PowerReading:
    entity_id: str            # gpu0 / cpu-pkg0 / node / rack
    scope: str                # "gpu" | "cpu" | "dram" | "node" | "rack" | "gpu(detail)"
    watts: float
    method: Method
    error_pct: float
    tier: str                 # which rung answered
    limit_watts: Optional[float] = None   # cap, where the source exposes one

    @property
    def coverage(self) -> str:
        """
        What this reading COVERS, for dedup. Keyed on scope, not ladder name:
          gpu_component / cpu_component  -> a SUBSET of the box (summable together)
          node_total                     -> whole box, includes everything
          rack_total                     -> whole RACK, NOT a per-server meter
          detail                         -> informational only, never summed
        """
        s = self.scope
        if s.startswith("gpu(detail"):
            return "detail"
        if s == "gpu":
            return "gpu_component"
        if s in ("cpu", "dram"):
            return "cpu_component"
        if s == "node":
            return "node_total"
        if s == "rack":
            return "rack_total"
        return "detail"


class Rung:
    tier = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def read(self) -> list[PowerReading]:
        raise NotImplementedError


def _num(x: str) -> Optional[float]:
    try:
        return float(x)
    except ValueError:
        return None            # "[N/A]", "[Not Supported]"


def _prom_query(prom_url: str, promql: str, timeout: float = 4.0):
    """Instant Prometheus query. Returns the result list, or None on any
    failure. READ-ONLY HTTP GET (stdlib only)."""
    import json as _json
    import urllib.parse
    import urllib.request
    url = prom_url.rstrip("/") + "/api/v1/query?" + \
        urllib.parse.urlencode({"query": promql})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            d = _json.load(resp)
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("status") != "success":
        return None
    return d.get("data", {}).get("result", [])


# ========================================================== GPU ladder
class GpuDcgmExporterRung(Rung):
    """Prometheus scrape of dcgm-exporter. Metric DCGM_FI_DEV_POWER_USAGE."""
    tier = "dcgm_exporter"

    def __init__(self, prom_url: Optional[str] = None):
        self.prom_url = prom_url

    def available(self) -> bool:
        return self.prom_url is not None

    def read(self) -> list[PowerReading]:
        res = _prom_query(self.prom_url, "DCGM_FI_DEV_POWER_USAGE")
        if not res:
            return []
        readings = []
        for series in res:
            m = series.get("metric", {})
            gpu = m.get("gpu", m.get("device", m.get("UUID", "?")))
            val = series.get("value", [None, None])[1]
            w = _num(val) if val is not None else None
            if w is not None:
                readings.append(PowerReading(f"gpu{gpu}", "gpu", w,
                                Method.MEASURED, 5.0, self.tier))
        return readings


class GpuDcgmApiRung(Rung):
    """dcgmi directly (DCGM engine present, no Prometheus shim)."""
    tier = "dcgm_api"

    def available(self) -> bool:
        return shutil.which("dcgmi") is not None

    def read(self) -> list[PowerReading]:
        try:
            out = subprocess.run(["dcgmi", "dmon", "-e", "155", "-c", "1"],
                                 capture_output=True, text=True, timeout=5)
        except (subprocess.SubprocessError, OSError):
            return []
        readings = []
        for line in out.stdout.splitlines():
            p = line.split()
            if len(p) >= 3 and p[0].upper() == "GPU":
                w = _num(p[2])
                if w is not None:
                    readings.append(PowerReading(f"gpu{p[1]}", "gpu", w,
                                    Method.MEASURED, 5.0, self.tier))
        return readings


class GpuNvmlRung(Rung):
    """nvidia-smi / NVML on the node. Universal floor for any NVIDIA box.
    Handles boards that hide power.draw by modeling from utilization x cap."""
    tier = "nvml"

    def available(self) -> bool:
        return shutil.which("nvidia-smi") is not None

    def read(self) -> list[PowerReading]:
        try:
            out = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=index,power.draw,power.limit,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
        except (subprocess.SubprocessError, OSError):
            return []
        if out.returncode != 0:
            return []
        readings = []
        for line in out.stdout.strip().splitlines():
            f = [c.strip() for c in line.split(",")]
            if len(f) < 4:
                continue
            idx, draw, lim, util = f
            w, limit, u = _num(draw), _num(lim), _num(util)
            if w is not None:
                readings.append(PowerReading(f"gpu{idx}", "gpu", w,
                                Method.MEASURED, 5.0, self.tier,
                                limit_watts=limit))
            elif limit is not None and u is not None:      # board hides draw
                readings.append(PowerReading(f"gpu{idx}", "gpu",
                                limit * (u / 100.0), Method.MODELED, 25.0,
                                self.tier, limit_watts=limit))
        return readings


class GpuRedfishRung(Rung):
    """BMC node total via Redfish - no GPU granularity. Stub (needs BMC creds)."""
    tier = "redfish"

    def __init__(self, bmc: Optional[str] = None):
        self.bmc = bmc

    def available(self) -> bool:
        return self.bmc is not None

    def read(self) -> list[PowerReading]:
        # real: GET https://{bmc}/redfish/v1/Chassis/{id}/Power -> PowerConsumedWatts
        return []


class GpuPduRung(Rung):
    """Rack power from PDU/DCIM. Coarse. Stub."""
    tier = "pdu"

    def __init__(self, pdu: Optional[str] = None):
        self.pdu = pdu

    def available(self) -> bool:
        return self.pdu is not None

    def read(self) -> list[PowerReading]:
        return []


def _try_rung(rung):
    """Run one rung defensively: any exception -> treat as no answer, fall through."""
    try:
        if not rung.available():
            return None
        rs = rung.read()
        return rs if rs else None
    except Exception:
        return None


def read_gpu_power(prom_url=None, bmc=None, pdu=None
                   ) -> tuple[list[PowerReading], str]:
    for rung in (GpuDcgmExporterRung(prom_url), GpuDcgmApiRung(),
                 GpuNvmlRung(), GpuRedfishRung(bmc), GpuPduRung(pdu)):
        rs = _try_rung(rung)
        if rs:
            return rs, rung.tier
    return [], "none"


# ========================================================== CPU/node ladder
class CpuNodeExporterRung(Rung):
    """Prometheus scrape of node-exporter RAPL."""
    tier = "node_exporter"

    def __init__(self, prom_url: Optional[str] = None):
        self.prom_url = prom_url

    def available(self) -> bool:
        return self.prom_url is not None

    def read(self) -> list[PowerReading]:
        # RAPL energy is a counter -> rate() gives watts over the last minute.
        res = _prom_query(self.prom_url,
                          "rate(node_rapl_package_joules_total[1m])")
        if not res:
            return []
        readings = []
        for series in res:
            m = series.get("metric", {})
            pkg = m.get("index", m.get("path", m.get("instance", "pkg")))
            scope = "dram" if "dram" in str(m).lower() else "cpu"
            val = series.get("value", [None, None])[1]
            w = _num(val) if val is not None else None
            if w is not None:
                readings.append(PowerReading(f"rapl-{pkg}", scope, w,
                                Method.MEASURED, 5.0, self.tier))
        return readings


class CpuRaplRung(Rung):
    """Read RAPL energy counters directly on a Linux node."""
    tier = "rapl"
    BASE = "/sys/class/powercap"

    def _domains(self):
        out = []
        for p in glob.glob(f"{self.BASE}/intel-rapl:*"):
            try:
                nm = open(os.path.join(p, "name")).read().strip()
            except OSError:
                continue
            out.append((p, "dram" if "dram" in nm else "cpu", nm))
        return out

    def available(self) -> bool:
        if platform.system() != "Linux":
            return False
        return any(os.access(f"{p}/energy_uj", os.R_OK)
                   for p, _, _ in self._domains())

    def read(self, interval: float = 0.4) -> list[PowerReading]:
        doms = self._domains()

        def e(p):
            # returns energy in uJ, or None if unreadable/malformed
            try:
                return int(open(f"{p}/energy_uj").read())
            except (OSError, ValueError):
                return None

        def wrap_max(p):
            try:
                return int(open(f"{p}/max_energy_range_uj").read())
            except (OSError, ValueError):
                return None

        first = {p: e(p) for p, _, _ in doms}
        time.sleep(interval)
        out = []
        for p, scope, nm in doms:
            a, b = first.get(p), e(p)
            if a is None or b is None:
                continue
            delta = b - a
            if delta < 0:
                # counter wrapped or was reset. If we know the range, correct it;
                # otherwise we can't trust this interval -> skip (don't report 0).
                mx = wrap_max(p)
                if mx and mx > 0:
                    delta += mx
                else:
                    continue
            watts = (delta / 1e6) / interval
            out.append(PowerReading(nm, scope, watts, Method.MEASURED, 5.0,
                                    self.tier))
        return out


class CpuRedfishRung(Rung):
    """BMC node total via Redfish. Works for GPU and CPU servers. Stub."""
    tier = "redfish"

    def __init__(self, bmc: Optional[str] = None):
        self.bmc = bmc

    def available(self) -> bool:
        return self.bmc is not None

    def read(self) -> list[PowerReading]:
        return []


class CpuIpmiRung(Rung):
    """ipmitool dcmi power reading (node total)."""
    tier = "ipmi"

    def __init__(self, host: Optional[str] = None):
        self.host = host

    def available(self) -> bool:
        return self.host is not None and shutil.which("ipmitool") is not None

    def read(self) -> list[PowerReading]:
        try:
            out = subprocess.run(
                ["ipmitool", "-H", self.host, "dcmi", "power", "reading"],
                capture_output=True, text=True, timeout=8)
            m = re.search(r"Instantaneous power reading:\s*([\d.]+)", out.stdout)
            if m:
                return [PowerReading(f"node@{self.host}", "node",
                        float(m.group(1)), Method.MEASURED, 3.0, self.tier)]
        except (subprocess.SubprocessError, OSError):
            pass
        return []


class CpuPduRung(Rung):
    """PDU/DCIM rack power. Coarse. Stub."""
    tier = "pdu"

    def __init__(self, pdu: Optional[str] = None):
        self.pdu = pdu

    def available(self) -> bool:
        return self.pdu is not None

    def read(self) -> list[PowerReading]:
        return []


class CpuModelRung(Rung):
    """Last resort: psutil CPU-util model. MODELED, wide error band."""
    tier = "model"

    # Per-SKU idle + TDP feeds the linear model: idle + (tdp-idle)*util.
    # Defaults are LAPTOP-scale and must NOT be presented as server telemetry -
    # a caller should pass sku_idle_w / sku_tdp_w from inventory for a server.
    def __init__(self, idle_w: float = 8.0, envelope_w: float = 25.0,
                 sku_idle_w: Optional[float] = None, sku_tdp_w: Optional[float] = None):
        if sku_idle_w is not None and sku_tdp_w is not None:
            self.idle_w = sku_idle_w
            self.envelope_w = max(sku_tdp_w - sku_idle_w, 0.0)
            self.from_sku = True
        else:
            self.idle_w, self.envelope_w = idle_w, envelope_w
            self.from_sku = False

    def available(self) -> bool:
        try:
            import psutil  # noqa
            return True
        except ImportError:
            return False

    def read(self) -> list[PowerReading]:
        import psutil
        util = psutil.cpu_percent(interval=0.4) / 100.0
        watts = self.idle_w + self.envelope_w * util
        # wider error band when we're guessing with laptop defaults on a server
        err = 30.0 if self.from_sku else 50.0
        eid = "cpu-model" if self.from_sku else "cpu-model(default!)"
        return [PowerReading(eid, "cpu", watts, Method.MODELED, err, self.tier)]


def read_cpu_node_power(prom_url=None, bmc=None, ipmi=None, pdu=None
                        ) -> tuple[list[PowerReading], str]:
    for rung in (CpuNodeExporterRung(prom_url), CpuRaplRung(),
                 CpuRedfishRung(bmc), CpuIpmiRung(ipmi), CpuPduRung(pdu),
                 CpuModelRung()):
        rs = _try_rung(rung)
        if rs:
            return rs, rung.tier
    return [], "none"


# ========================================================== unified reader
@dataclass
class ServerPower:
    readings: list[PowerReading]
    gpu_tier: str
    cpu_tier: str
    note: str = ""

    @property
    def _by_coverage(self):
        buckets = {"gpu_component": [], "cpu_component": [],
                   "node_total": [], "rack_total": [], "detail": []}
        for r in self.readings:
            buckets[r.coverage].append(r)
        return buckets

    @property
    def rack_readings(self):
        """Rack-level PDU readings. NEVER summed into a server total - a rack
        meter covers many servers. Held separately for rack-level allocation."""
        return self._by_coverage["rack_total"]

    @property
    def _summable(self):
        """
        Choose exactly ONE basis for the server total, by coverage:
          1. a node_total (BMC/IPMI) is the whole box -> use it alone,
             components become non-summable detail. If several node_totals
             exist (e.g. GpuRedfish + CpuRedfish return the same box), keep ONE.
          2. else sum the components (gpu_component + cpu_component).
        rack_total and detail are never summed.
        """
        b = self._by_coverage
        if b["node_total"]:
            # one whole-box reading wins; prefer the lowest error_pct
            best = min(b["node_total"], key=lambda r: r.error_pct)
            return [best]
        return b["gpu_component"] + b["cpu_component"]

    @property
    def measured_w(self):
        return sum(r.watts for r in self._summable if r.method == Method.MEASURED)

    @property
    def modeled_w(self):
        return sum(r.watts for r in self._summable if r.method == Method.MODELED)

    @property
    def total_w(self):
        return self.measured_w + self.modeled_w

    @property
    def measured_fraction(self):
        t = self.total_w
        return (self.measured_w / t) if t else 0.0

    # --- unmeasured-tail estimation -------------------------------------
    # When there is NO whole-box (node) meter, component sensors miss the tail:
    # fans, SSD, NIC, VRM, PSU loss. Typical air-cooled overhead is 15-25%
    # beyond CPU+GPU+RAM. We state a RANGE, never a fake fan number.
    # NOTE: a RACK PDU is NOT a whole-server meter and does NOT clear this.
    TAIL_LOW = 0.15          # liquid-cooled / efficient boxes
    TAIL_HIGH = 0.25         # air-cooled / fans ramping under load
    CONFIDENCE_GATE = 0.85

    @property
    def has_node_total(self):
        """True only for a per-SERVER whole-box meter (BMC/IPMI). Rack excluded."""
        return bool(self._by_coverage["node_total"])

    @property
    def has_rack_total(self):
        return bool(self._by_coverage["rack_total"])

    @property
    def est_total_range(self):
        base = self.total_w
        if self.has_node_total or base == 0:
            return (base, base)
        low = base / (1 - self.TAIL_LOW)
        high = base / (1 - self.TAIL_HIGH)
        return (low, high)

    @property
    def low_confidence(self):
        # Tail is unmeasured whenever there is no per-server whole-box meter.
        # Completeness gap, independent of measured-fraction. Rack does NOT clear it.
        return (not self.has_node_total) and self.total_w > 0

    @property
    def modeled_values(self):
        """Separate flag: some captured values are estimates (quality gap)."""
        return self.measured_fraction < self.CONFIDENCE_GATE

    @property
    def warning(self):
        if not self.low_confidence:
            return ""
        lo, hi = self.est_total_range
        add_lo = lo - self.total_w
        add_hi = hi - self.total_w
        return (f"no whole-box (BMC/IPMI) meter - tail is ESTIMATED, not read. "
                f"true total likely {lo:.0f}-{hi:.0f} W "
                f"(+{add_lo:.0f} to +{add_hi:.0f} W unmeasured: fans/SSD/NIC/PSU loss). "
                f"grant BMC/Redfish access to make this exact.")


def read_server_power(prom_url=None, bmc=None, ipmi=None, pdu=None
                      ) -> ServerPower:
    """
    Run both ladders; ServerPower resolves double-counting by COVERAGE class,
    not by ladder name. Rules (in ServerPower._summable):
      * a node_total (BMC/IPMI) is the whole box -> it alone is summed;
        gpu/cpu components are kept as detail.
      * else the components (gpu + cpu) are summed.
      * a rack_total (PDU) is NEVER summed into a server total.
    """
    gpu, gtier = read_gpu_power(prom_url, bmc, pdu)
    cpu, ctier = read_cpu_node_power(prom_url, bmc, ipmi, pdu)

    readings = list(gpu) + list(cpu)

    # If a per-server node_total is present, relabel gpu_component readings as
    # detail so the display makes the subset-relationship obvious. (Summation
    # already ignores them via coverage; this is purely for readability.)
    has_node = any(r.coverage == "node_total" for r in readings)
    note = ""
    if has_node:
        relabelled = []
        for r in readings:
            if r.coverage == "gpu_component":
                relabelled.append(PowerReading(r.entity_id, "gpu(detail)", r.watts,
                                               r.method, r.error_pct, r.tier,
                                               r.limit_watts))
            else:
                relabelled.append(r)
        readings = relabelled
        note = "node-total is the truth; component lines shown as detail, not summed"

    return ServerPower(readings, gtier, ctier, note)


# ========================================================== cli
if __name__ == "__main__":
    import argparse
    import json as _json
    ap = argparse.ArgumentParser()
    ap.add_argument("--prom", help="Prometheus URL (dcgm-exporter / node-exporter)")
    ap.add_argument("--bmc", help="BMC host (Redfish node total)")
    ap.add_argument("--ipmi", help="IPMI host (dcmi power reading)")
    ap.add_argument("--pdu", help="PDU/DCIM host (rack power)")
    ap.add_argument("--json", action="store_true",
                    help="emit one JSON line (consumed by the fleet SSH transport)")
    a = ap.parse_args()

    sp = read_server_power(a.prom, a.bmc, a.ipmi, a.pdu)

    if a.json:
        # single machine-readable line; the fleet reader parses the last line
        print(_json.dumps({
            "measured_w": round(sp.measured_w, 2),
            "modeled_w": round(sp.modeled_w, 2),
            "total_w": round(sp.total_w, 2),
            "measured_fraction": round(sp.measured_fraction, 4),
            "gpu_tier": sp.gpu_tier,
            "cpu_tier": sp.cpu_tier,
            "has_node_total": sp.has_node_total,
            "low_confidence": sp.low_confidence,
        }))
        raise SystemExit(0)

    print(f"\n  SERVER POWER   gpu tier: {sp.gpu_tier}   cpu/node tier: {sp.cpu_tier}\n")
    if not sp.readings:
        print("  no telemetry on any rung.\n")
    else:
        print(f"  {'ENTITY':<20}{'SCOPE':<12}{'WATTS':>8}{'METHOD':>10}{'ERR':>6}{'TIER':>14}")
        print("  " + "-" * 72)
        for r in sorted(sp.readings, key=lambda x: x.watts, reverse=True):
            print(f"  {r.entity_id:<20}{r.scope:<12}{r.watts:>8.1f}"
                  f"{r.method.value:>10}{r.error_pct:>5.0f}%{r.tier:>14}")
        print("  " + "-" * 72)
        print(f"  server total : {sp.total_w:.1f} W  "
              f"(measured {sp.measured_w:.1f} + modeled {sp.modeled_w:.1f})")
        print(f"  confidence   : {sp.measured_fraction*100:.0f}% measured")
        if sp.has_rack_total:
            rack_w = sum(r.watts for r in sp.rack_readings)
            print(f"  rack meter   : {rack_w:.0f} W (rack-level, NOT summed into server)")
        if sp.low_confidence:
            lo, hi = sp.est_total_range
            print(f"  est. true    : {lo:.0f} - {hi:.0f} W  "
                  f"(measured is a FLOOR; tail estimated)")
            print(f"  !! WARNING   : {sp.warning}")
        if sp.note:
            print(f"  note         : {sp.note}")
    print()
