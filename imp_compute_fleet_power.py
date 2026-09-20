#!/usr/bin/env python3
"""
IMP  .  fleet power collector            (multi-node)

Reaches EVERY node in the DC and rolls readings up node -> rack -> DC, carrying
attribution confidence at each level.

The per-node reading is delegated to imp_server_power_source.read_server_power()
- the fallback-ladder reader (dcgm_exporter -> dcgm_api -> nvml -> redfish -> pdu,
and the CPU/node equivalent). This file's job is only:
    * reach the node (local, ssh, or a remote endpoint),
    * run the server source there,
    * roll the results up with coverage + confidence.

Transports:
    local  - run read_server_power() in-process (control node reads via prom/bmc)
    ssh    - ssh to the node, run the server source remotely, parse JSON back
    sim    - synthetic node, for testing roll-up with no real DC

READ-ONLY throughout. Concurrent + fault-tolerant: a dead node is marked
UNREACHABLE and never stalls the sweep.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import imp_server_power_source as sps   # the per-node ladder reader


# ---------------------------------------------------------------- inventory
@dataclass
class NodeSpec:
    host: str                         # DNS name / IP (from control-node hosts file)
    rack: str
    transport: str = "local"          # local | ssh | sim
    # endpoints handed to the server source (per node or shared):
    prom_url: Optional[str] = None    # dcgm-exporter / node-exporter
    bmc: Optional[str] = None         # Redfish node total
    ipmi: Optional[str] = None        # ipmitool dcmi
    pdu: Optional[str] = None         # PDU / DCIM rack
    gpu_count: int = 0                # sim hint only


# ------------------------------------------------------------- node result
class NodeStatus(str, Enum):
    OK = "ok"
    UNREACHABLE = "unreachable"
    PARTIAL = "partial"


@dataclass
class NodeResult:
    host: str
    rack: str
    status: NodeStatus
    measured_w: float = 0.0
    modeled_w: float = 0.0
    gpu_tier: str = "none"
    cpu_tier: str = "none"
    detail: str = ""

    @property
    def total_w(self) -> float:
        return self.measured_w + self.modeled_w

    @property
    def measured_fraction(self) -> float:
        return (self.measured_w / self.total_w) if self.total_w else 0.0


# ---------------------------------------------------------------- transports
class Transport:
    """Reach one node, return a NodeResult. Read-only. Never raises upward."""
    name = "base"

    def read(self, spec: NodeSpec, timeout: float) -> NodeResult:
        raise NotImplementedError


class LocalTransport(Transport):
    """
    Run the server source IN-PROCESS. Correct when the control node can reach a
    node's telemetry directly - Prometheus/DCGM-exporter scrape, Redfish to the
    BMC, or reading the local machine itself. No SSH needed.
    """
    name = "local"

    def read(self, spec: NodeSpec, timeout: float) -> NodeResult:
        try:
            sp = sps.read_server_power(prom_url=spec.prom_url, bmc=spec.bmc,
                                       ipmi=spec.ipmi, pdu=spec.pdu)
        except Exception as e:                    # a source must not kill the node
            return NodeResult(spec.host, spec.rack, NodeStatus.UNREACHABLE,
                              detail=str(e)[:50])
        if not sp.readings:
            return NodeResult(spec.host, spec.rack, NodeStatus.PARTIAL,
                              detail="no telemetry on any rung")
        return NodeResult(spec.host, spec.rack, NodeStatus.OK,
                          measured_w=sp.measured_w, modeled_w=sp.modeled_w,
                          gpu_tier=sp.gpu_tier, cpu_tier=sp.cpu_tier)


class SshTransport(Transport):
    """
    SSH to the node and run the server source THERE with --json, parsing the
    JSON back. Rides the customer control node's existing read-only SSH; IMP
    never holds fleet credentials. Requires imp_server_power_source.py present
    on the node (push it once). If it isn't there, the node is PARTIAL - we do
    NOT fall back to a raw command that yields unparseable output.
    """
    name = "ssh"
    REMOTE = "python3 imp_server_power_source.py --json"

    def read(self, spec: NodeSpec, timeout: float) -> NodeResult:
        try:
            out = subprocess.run(
                ["ssh", "-o", "BatchMode=yes",
                 "-o", f"ConnectTimeout={int(timeout)}", spec.host, self.REMOTE],
                capture_output=True, text=True, timeout=timeout + 3)
        except (subprocess.TimeoutExpired, OSError) as e:
            return NodeResult(spec.host, spec.rack, NodeStatus.UNREACHABLE,
                              detail=str(e)[:40])
        if out.returncode != 0:
            return NodeResult(spec.host, spec.rack, NodeStatus.UNREACHABLE,
                              detail="ssh rc!=0")
        try:
            d = json.loads(out.stdout.strip().splitlines()[-1])
            return NodeResult(spec.host, spec.rack, NodeStatus.OK,
                              measured_w=d.get("measured_w", 0.0),
                              modeled_w=d.get("modeled_w", 0.0),
                              gpu_tier=d.get("gpu_tier", "none"),
                              cpu_tier=d.get("cpu_tier", "none"))
        except (ValueError, IndexError):
            return NodeResult(spec.host, spec.rack, NodeStatus.PARTIAL,
                              detail="unparseable remote output")


class SimTransport(Transport):
    """Synthetic node so the roll-up is testable with no real DC."""
    name = "sim"

    def read(self, spec: NodeSpec, timeout: float) -> NodeResult:
        import random, hashlib
        seed = int(hashlib.md5(spec.host.encode()).hexdigest(), 16) % 1000
        rng = random.Random(seed + int(time.time()) // 5)
        if rng.random() < 0.05:
            return NodeResult(spec.host, spec.rack, NodeStatus.UNREACHABLE,
                              detail="sim timeout")
        gpus = spec.gpu_count or 8
        gpu_w = sum(rng.uniform(120, 680) for _ in range(gpus))    # measured
        floor = 250 + rng.uniform(0, 150)                           # modeled
        return NodeResult(spec.host, spec.rack, NodeStatus.OK,
                          measured_w=gpu_w, modeled_w=floor,
                          gpu_tier="sim", cpu_tier="sim")


TRANSPORTS = {"local": LocalTransport(), "ssh": SshTransport(),
              "sim": SimTransport()}


# ---------------------------------------------------------------- fleet poll
def poll_fleet(inventory: list[NodeSpec], timeout: float = 5.0,
               max_workers: int = 64) -> list[NodeResult]:
    results: list[NodeResult] = []
    valid, bad = [], []
    for s in inventory:
        (valid if s.transport in TRANSPORTS else bad).append(s)
    # unknown transport (e.g. a typo "SSH") -> that node is unreachable, not fatal
    for s in bad:
        results.append(NodeResult(s.host, s.rack, NodeStatus.UNREACHABLE,
                                  detail=f"unknown transport '{s.transport}'"))

    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(TRANSPORTS[s.transport].read, s, timeout): s
                for s in valid}
        for fut in cf.as_completed(futs):
            s = futs[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                results.append(NodeResult(s.host, s.rack,
                               NodeStatus.UNREACHABLE, detail=str(e)[:40]))
    return results


# ---------------------------------------------------------------- roll-up
@dataclass
class RollUp:
    label: str
    measured_w: float = 0.0
    modeled_w: float = 0.0
    nodes_ok: int = 0
    nodes_total: int = 0
    children: dict = field(default_factory=dict)

    @property
    def total_w(self):
        return self.measured_w + self.modeled_w

    @property
    def measured_fraction(self):
        return (self.measured_w / self.total_w) if self.total_w else 0.0

    @property
    def coverage(self):
        """Fraction of nodes we got USABLE power from (not merely reachable)."""
        return (self.nodes_ok / self.nodes_total) if self.nodes_total else 0.0


def rollup(results: list[NodeResult], dc_label="DC") -> RollUp:
    dc = RollUp(dc_label)
    for r in results:
        dc.nodes_total += 1
        rack = dc.children.setdefault(r.rack, RollUp(r.rack))
        rack.nodes_total += 1
        # Only OK nodes with actual power contribute. UNREACHABLE (no contact)
        # and PARTIAL (reached but no telemetry on any rung) are NOT power-covered.
        if r.status is not NodeStatus.OK or r.total_w <= 0:
            continue
        for lvl in (dc, rack):
            lvl.measured_w += r.measured_w
            lvl.modeled_w += r.modeled_w
            lvl.nodes_ok += 1
    return dc


# ---------------------------------------------------------------- render
def render(dc: RollUp, results: list[NodeResult]) -> str:
    L = ["\n  FLEET POWER  (multi-node, ladder-based per node)\n"]
    L.append(f"  {'RACK':<10}{'NODES':>8}{'kW':>10}{'MEASURED%':>11}")
    L.append("  " + "-" * 40)
    for name, rack in sorted(dc.children.items()):
        L.append(f"  {name:<10}{rack.nodes_ok:>3}/{rack.nodes_total:<4}"
                 f"{rack.total_w/1000:>10.1f}{rack.measured_fraction*100:>10.0f}%")
    L.append("  " + "-" * 40)
    unreachable = [r for r in results if r.status is NodeStatus.UNREACHABLE]
    tiers = sorted({r.gpu_tier for r in results if r.status is NodeStatus.OK} |
                   {r.cpu_tier for r in results if r.status is NodeStatus.OK})
    L.append(f"  DC TOTAL  : {dc.total_w/1000:.1f} kW   "
             f"(measured {dc.measured_w/1000:.1f} + modeled {dc.modeled_w/1000:.1f})")
    L.append(f"  coverage  : {dc.nodes_ok}/{dc.nodes_total} nodes "
             f"({dc.coverage*100:.0f}%)   confidence {dc.measured_fraction*100:.0f}% measured")
    L.append(f"  tiers used: {', '.join(t for t in tiers if t != 'none')}")
    if unreachable:
        L.append(f"  unreachable: {', '.join(r.host for r in unreachable[:8])}"
                 + (" ..." if len(unreachable) > 8 else ""))
    L.append("")
    return "\n".join(L)


def demo_inventory(racks=4, nodes_per_rack=8) -> list[NodeSpec]:
    return [NodeSpec(host=f"gpu-r{r}-n{n}", rack=f"rack-{r}",
                     transport="sim", gpu_count=8)
            for r in range(racks) for n in range(nodes_per_rack)]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", help="JSON file: list of node specs")
    ap.add_argument("--racks", type=int, default=4)
    ap.add_argument("--nodes", type=int, default=8, help="nodes per rack (sim)")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--local", action="store_true",
                    help="add THIS machine as one real node (reads via server source)")
    a = ap.parse_args()

    if a.inventory:
        inv = [NodeSpec(**d) for d in json.load(open(a.inventory))]
    else:
        inv = demo_inventory(a.racks, a.nodes)
    if a.local:
        inv.append(NodeSpec(host="localhost", rack="rack-local",
                            transport="local"))

    t0 = time.time()
    results = poll_fleet(inv, timeout=a.timeout)
    dc = rollup(results)
    print(render(dc, results))
    print(f"  polled {len(inv)} nodes in {time.time()-t0:.1f}s\n")
