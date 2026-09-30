#!/usr/bin/env python3
"""
Aareka  .  Sender agent   (customer side)

Runs inside the customer's environment. On an interval it:

    1. reads this machine's power via the collector (imp_server_power_source),
    2. reads OWNERSHIP telemetry - which workload/VM is on which GPU, including
       VIRTUAL PARTITIONS (vGPU / MIG) via `nvidia-smi vgpu -q`, plus per-process
       ownership via `nvidia-smi --query-compute-apps`,
    3. packages watts + ownership into the Ingest API's batch shape,
    4. POSTs it to your Ingest API, authenticated with the customer's org key.

Read-only. Outbound HTTPS only. (local mode: stdlib only; vCenter/Kubernetes
modes add pyvmomi / kubernetes.)

Sources (AAREKA_SOURCE):
    * local (default) - THIS machine: nvidia-smi power + per-process / vGPU-MIG
                        ownership. One batch (this host).
    * vcenter         - one read-only vCenter connection: host-total power + VM
                        topology + department tags. One batch per ESXi host.
    * kubernetes      - one read-only kubeconfig: node->pod topology + labels,
                        power from Prometheus. One batch per node.
    * fleet           - one control node sweeps many servers (SSH/Prometheus) for
                        power; the IT-team server->BU map (AAREKA_HOST_DEPT_MAP)
                        attributes each whole server to its BU. One batch per server.

Ownership sources (local, in priority order per GPU):
    * vGPU / MIG slices  -> `nvidia-smi vgpu -q` (a vGPU/MIG-configured host).
                           mode = vgpu (or mig via AAREKA_GPU_MODE); split by FB.
    * per-process        -> `nvidia-smi --query-compute-apps` (bare metal).
                           1 process = passthrough (exact); 2+ = shared (estimated).

Config (environment variables, or the matching --flags):
    AAREKA_INGEST_URL   your Ingest API base URL                     (required)
    AAREKA_ORG_KEY      the customer's secret key                    (required)
    AAREKA_SOURCE       local (default) | vcenter | kubernetes
    AAREKA_INTERVAL     seconds between sends (default 60)
    AAREKA_HOST         host label (default: hostname)               (local mode)
    AAREKA_DEPT_MAP     JSON {workload_or_vm_name: department}        (optional tags)
    AAREKA_GPU_MODE     "vgpu" (default) or "mig" - label for read partitions
    AAREKA_VGPU_QFILE   path to a saved `nvidia-smi vgpu -q` dump (testing/injection)
    AAREKA_PROM_URL / AAREKA_BMC / AAREKA_IPMI / AAREKA_PDU          (telemetry endpoints)
  vCenter mode (AAREKA_SOURCE=vcenter):
    AAREKA_VCENTER            vCenter host/IP                         (required)
    AAREKA_VCENTER_USER       read-only user                         (required)
    AAREKA_VCENTER_PASSWORD   password                               (required)
    AAREKA_VCENTER_DEPT_ATTR  custom-attribute name holding department (optional)
    AAREKA_VCENTER_INSECURE   "1" to skip TLS verify (self-signed labs)
  Kubernetes mode (AAREKA_SOURCE=kubernetes):
    AAREKA_KUBECONFIG         path to kubeconfig (default: in-cluster / ~/.kube)
    AAREKA_PROM_URL           Prometheus base URL for node/GPU power (recommended)
  Fleet mode (AAREKA_SOURCE=fleet):
    AAREKA_INVENTORY          path to JSON list of node specs (host, rack, transport,
                              prom_url/bmc/ipmi/pdu per node)
    AAREKA_HOST_DEPT_MAP      JSON {server_hostname: application/BU}  (from the IT team)

Run:
    python aareka_sender.py            # loop forever
    python aareka_sender.py --once     # send one batch then exit
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import imp_server_power_source as sps

VERSION = "0.4.0"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------- nvidia-smi helpers
def _nvidia_smi(query: str) -> list[list[str]]:
    """Read-only nvidia-smi CSV query -> list of split rows. [] on failure."""
    try:
        out = subprocess.run(["nvidia-smi", query, "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return []
    if out.returncode != 0:
        return []
    return [[c.strip() for c in line.split(",")]
            for line in out.stdout.strip().splitlines() if line.strip()]


def _run_raw(args: list[str]) -> "str | None":
    """Run a read-only nvidia-smi command, return raw stdout (or None)."""
    try:
        out = subprocess.run(["nvidia-smi", *args], capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return None
    return out.stdout if out.returncode == 0 else None


def gpu_processes_by_index() -> dict:
    """{gpu_index: [{name, mem_mb}]} - processes per GPU (bare-metal ownership)."""
    uuid_to_idx = {}
    for row in _nvidia_smi("--query-gpu=index,uuid"):
        if len(row) >= 2:
            uuid_to_idx[row[1]] = row[0]
    out: dict = {}
    for row in _nvidia_smi(
            "--query-compute-apps=pid,process_name,gpu_uuid,used_gpu_memory"):
        if len(row) < 4:
            continue
        name, uuid, mem = row[1], row[2], row[3]
        try:
            mem_mb = float(mem)
        except ValueError:
            mem_mb = 0.0
        short = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or name
        out.setdefault(uuid_to_idx.get(uuid, uuid), []).append({"name": short, "mem_mb": mem_mb})
    return out


# ------------------------------------------------------- virtual partitions (vGPU / MIG)
# Ported from gpu_vm_collector.parse_vgpu_q so the sender reads the SAME partition
# telemetry the engine expects. Reads `nvidia-smi vgpu -q` (a vGPU/MIG host).
def _parse_vgpu_q(text: str, bus_to_index: "dict | None" = None) -> list[dict]:
    bus_to_index = bus_to_index or {}
    recs: list[dict] = []
    gpu_bus = None
    cur: "dict | None" = None
    in_util = in_fb = False
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("GPU ") and ":" in s:
            gpu_bus = s.split(None, 1)[1].strip()
            continue
        if s.startswith("vGPU ID"):
            if cur:
                recs.append(cur)
            cur = {"gpu": bus_to_index.get(gpu_bus, gpu_bus), "vm": None,
                   "util": 0.0, "fb_mb": 0.0}
            in_util = in_fb = False
            continue
        if cur is None:
            continue
        if s.startswith("VM Name"):
            cur["vm"] = s.split(":", 1)[1].strip()
        elif s.startswith("FB Memory Usage"):
            in_fb, in_util = True, False
        elif s.startswith("Utilization"):
            in_util, in_fb = True, False
        elif in_fb and s.startswith("Total"):
            m = re.search(r"(\d+)", s)
            if m:
                cur["fb_mb"] = float(m.group(1))
            in_fb = False
        elif in_util and s.startswith("Gpu"):
            m = re.search(r"(\d+)", s)
            if m:
                cur["util"] = float(m.group(1))
            in_util = False
    if cur:
        recs.append(cur)
    return [r for r in recs if r.get("vm")]


def _bus_to_index() -> dict:
    """Map PCI bus id -> GPU index, so vGPU slices line up with power readings."""
    m = {}
    for row in _nvidia_smi("--query-gpu=index,pci.bus_id"):
        if len(row) >= 2:
            m[row[1]] = row[0]
    return m


def read_vgpu_instances() -> list[dict]:
    """vGPU/MIG slices this host exposes: [{gpu, vm, util, fb_mb}]. Reads
    `nvidia-smi vgpu -q`, or a saved dump named by AAREKA_VGPU_QFILE (testing).
    Empty on a host without vGPU/MIG."""
    b2i = _bus_to_index()
    qfile = os.environ.get("AAREKA_VGPU_QFILE")
    if qfile and os.path.exists(qfile):
        with open(qfile) as f:
            return _parse_vgpu_q(f.read(), b2i)
    out = _run_raw(["vgpu", "-q"])
    if out:
        return _parse_vgpu_q(out, b2i)
    return []


# ------------------------------------------------------- batch builders
def build_full_batch(sp: "sps.ServerPower", host: str, dept_map: dict):
    """Watts + ownership (virtual partitions first, then per-process) -> full
    contract. None if there's no GPU telemetry (caller falls back to flat)."""
    gpu_readings = [r for r in sp.readings if r.scope in ("gpu", "gpu(detail)")]
    cpu_readings = [r for r in sp.readings if r.scope in ("cpu", "dram")]
    node_reading = next((r for r in sp.readings if r.scope in ("node", "rack")), None)
    if not gpu_readings:
        return None

    procs_by_idx = gpu_processes_by_index()
    vgpu_by_idx: dict = {}
    for inst in read_vgpu_instances():
        vgpu_by_idx.setdefault(str(inst["gpu"]), []).append(inst)
    part_mode = os.environ.get("AAREKA_GPU_MODE", "vgpu").lower()
    if part_mode not in ("vgpu", "mig"):
        part_mode = "vgpu"

    gpus, workloads = [], {}
    for r in gpu_readings:
        idx = r.entity_id.replace("gpu", "").split("(")[0]
        watts = round(r.watts, 2)
        slices = vgpu_by_idx.get(idx, [])

        if slices:
            # VIRTUAL PARTITIONS (vGPU / MIG): send each VM's allocation (frame-buffer
            # share) AND utilisation, so the engine's baseline(alloc)+dynamic(util)
            # split uses both. Attribution = estimated (per the engine).
            total_fb = sum(s.get("fb_mb", 0.0) for s in slices) or 1e-9
            vgpu_vms: dict = {}
            for s in slices:
                vm = s["vm"]
                e = vgpu_vms.setdefault(vm, {"alloc": 0.0, "util": 0.0})
                e["alloc"] = round(e["alloc"] + s.get("fb_mb", 0.0) / total_fb, 3)
                e["util"] = max(e["util"], s.get("util", 0.0))
                w = workloads.setdefault(vm, {"gpu_ids": set(), "kind": "vm"})
                w["gpu_ids"].add(r.entity_id)
            gpus.append({"gpu_id": r.entity_id, "watts": watts, "method": r.method.value,
                         "mode": part_mode, "vgpu_vms": vgpu_vms})
            continue

        procs = procs_by_idx.get(idx, [])
        by_name: dict = {}
        for p in procs:
            by_name[p["name"]] = by_name.get(p["name"], 0.0) + p["mem_mb"]
        if not by_name:
            gpus.append({"gpu_id": r.entity_id, "watts": watts, "method": r.method.value,
                         "mode": "passthrough", "owner": None})
            continue
        if len(by_name) == 1:
            name = next(iter(by_name))
            gpus.append({"gpu_id": r.entity_id, "watts": watts, "method": r.method.value,
                         "mode": "passthrough", "owner": name})
        else:
            total = sum(by_name.values()) or 1e-9
            gpus.append({"gpu_id": r.entity_id, "watts": watts, "method": r.method.value,
                         "mode": "vgpu",
                         "shares": {n: round(m / total, 3) for n, m in by_name.items()}})
        for n in by_name:
            w = workloads.setdefault(n, {"gpu_ids": set(), "kind": "process"})
            w["gpu_ids"].add(r.entity_id)

    workload_list = [{"id": n, "kind": v.get("kind", "process"), "cpu_share": 0.0,
                      "mem_mb": 0.0, "gpu_ids": sorted(v["gpu_ids"]),
                      "department": dept_map.get(n)} for n, v in workloads.items()]

    batch = {
        "host": host, "captured_at": _iso_now(), "collector_version": VERSION,
        "gpus": gpus,
        "cpu_readings": [{"entity_id": r.entity_id, "scope": r.scope,
                          "watts": round(r.watts, 2), "method": r.method.value,
                          "tier": r.tier} for r in cpu_readings],
        "workloads": workload_list,
    }
    if node_reading is not None and sp.has_node_total:
        batch["node_total_w"] = round(node_reading.watts, 2)
        batch["node_total_method"] = node_reading.method.value
    return batch


def build_flat_batch(sp: "sps.ServerPower", host: str) -> dict:
    """Fallback: watts only (no ownership). Stored but not attributed."""
    return {
        "host": host, "captured_at": _iso_now(), "collector_version": VERSION,
        "readings": [{"entity_id": r.entity_id, "scope": r.scope,
                      "watts": round(r.watts, 2), "method": r.method.value,
                      "tier": r.tier, "error_pct": r.error_pct} for r in sp.readings],
    }


# ------------------------------------------------------- vCenter (VMware) source
def _make_vcenter(cfg: dict):
    from vcenter_connector import VCenterConnector
    if not (cfg.get("vcenter") and cfg.get("vc_user") and cfg.get("vc_pwd")):
        raise RuntimeError("vcenter mode needs AAREKA_VCENTER, AAREKA_VCENTER_USER "
                           "and AAREKA_VCENTER_PASSWORD")
    return VCenterConnector(vcenter=cfg["vcenter"], user=cfg["vc_user"],
                            pwd=cfg["vc_pwd"],
                            department_attribute=cfg.get("vc_dept_attr"))


def build_vcenter_batches(connector, dept_map: dict) -> list:
    """One batch per ESXi host. vCenter gives, in a single read: the measured
    whole-host power (a node_total) and each VM's vCPU/memory + department tag.
    The engine splits that measured total across the VMs by vCPU + memory
    allocation -> per-VM, per-department attribution. Per-GPU watts are not
    exposed by vCenter, so this is an allocation split of a MEASURED total
    (estimated per VM), which the engine labels accordingly. A department from
    the VM's vCenter attribute wins; AAREKA_DEPT_MAP is a fallback override."""
    batches = []
    for host in connector.hosts():
        if not host.vms:
            continue
        total_vcpu = sum(v.vcpu for v in host.vms) or 1e-9
        workloads = []
        for v in host.vms:
            workloads.append({
                "id": v.name, "kind": "vm",
                "cpu_share": round(v.vcpu / total_vcpu, 4),
                "mem_mb": float(v.mem_mb),
                "gpu_ids": list(v.gpu_ids),
                "department": v.department or dept_map.get(v.name),
            })
        batch = {"host": host.name, "captured_at": _iso_now(),
                 "collector_version": VERSION, "gpus": [], "cpu_readings": [],
                 "workloads": workloads}
        if host.power_w is not None:
            batch["node_total_w"] = round(float(host.power_w), 2)
            batch["node_total_method"] = "measured"
        batches.append(batch)
    return batches


# ------------------------------------------------------- Kubernetes source
def _k8s_node_power(prom_url: str, node_label: str = "Hostname") -> dict:
    """Per-node power from Prometheus: GPU from dcgm-exporter, CPU from
    node-exporter RAPL, grouped by node. Returns {node_label_value: {gpu_w, cpu_w}}.
    This is the fragile, site-specific seam (metric/label names vary by install) -
    isolated here and validated on first connect; the join + attribution below is
    fixture-tested independently."""
    out: dict = {}

    def add(promql: str, key: str):
        for s in (sps._prom_query(prom_url, promql) or []):
            m = s.get("metric", {})
            node = m.get(node_label) or m.get("instance") or m.get("node")
            val = (s.get("value") or [None, None])[1]
            if node is None or val is None:
                continue
            try:
                w = float(val)
            except (TypeError, ValueError):
                continue
            out.setdefault(node, {"gpu_w": 0.0, "cpu_w": 0.0})[key] += w

    add(f"sum by ({node_label}) (DCGM_FI_DEV_POWER_USAGE)", "gpu_w")
    add(f"sum by ({node_label}) (rate(node_rapl_package_joules_total[1m]))", "cpu_w")
    return out


def _match_node_power(node: str, node_power: dict) -> "dict | None":
    """Best-effort join between a K8s node name and a Prometheus label value
    (FQDN / :port differences). Exact first, then short hostname."""
    if node in node_power:
        return node_power[node]
    short = node.split(".")[0]
    for k, v in node_power.items():
        if k.split(".")[0].split(":")[0] == short:
            return v
    return None


def build_k8s_batches(cfg: dict, reader=None, node_power=None) -> list:
    """One batch per Kubernetes node. Topology (node->pods, CPU request, memory,
    GPU count, labels) comes from the K8s API; per-node power from Prometheus.
    The engine splits each node's measured power across its pods by CPU-request +
    memory allocation, tagged to a department (AAREKA_DEPT_MAP by app label or
    namespace; namespace is the default). `reader` / `node_power` are injectable
    for testing."""
    from imp_topology import K8sReader
    if reader is None:
        reader = K8sReader(kubeconfig=cfg.get("kubeconfig"))
    tree = reader.tree()                                   # {node: [Child(pod), ...]}
    if node_power is None:
        prom = cfg.get("prom")
        if not prom:
            raise RuntimeError("kubernetes mode needs AAREKA_PROM_URL (per-node power "
                               "from dcgm-exporter / node-exporter)")
        node_power = _k8s_node_power(prom, cfg.get("prom_node_label", "Hostname"))

    dept_map = cfg.get("dept_map", {})
    batches = []
    for node, pods in tree.items():
        pw = _match_node_power(node, node_power)
        if pw is None:
            print(f"  k8s: no Prometheus power for node {node} - skipping")
            continue
        total = pw.get("gpu_w", 0.0) + pw.get("cpu_w", 0.0)
        if total <= 0:
            continue
        workloads = []
        for pod in pods:
            ns = pod.cid.split("/", 1)[0]
            app = getattr(pod, "app", None)
            workloads.append({
                "id": pod.cid, "kind": "pod",
                "cpu_share": round(pod.cpu_share, 4),
                "mem_mb": float(pod.mem_mb),
                "gpu_ids": list(pod.gpu_ids),
                "department": dept_map.get(app) or dept_map.get(ns) or ns,
            })
        batches.append({"host": node, "captured_at": _iso_now(),
                        "collector_version": VERSION, "gpus": [], "cpu_readings": [],
                        "workloads": workloads,
                        "node_total_w": round(total, 2), "node_total_method": "measured"})
    return batches


# ------------------------------------------------------- fleet source (bare-metal, one control node)
def build_fleet_batches(cfg: dict, results=None) -> list:
    """One control node sweeps many servers (SSH / Prometheus / local) and rolls
    each server's MEASURED total power up to its owning application/BU using the
    IT-team-supplied server->BU map (Layer 2 ownership). Coarser than per-workload
    attribution, but real: whole-server power -> BU. `results` is injectable for
    testing (else poll the inventory)."""
    import imp_compute_fleet_power as fleet
    if results is None:
        inv_path = cfg.get("inventory")
        if not inv_path:
            raise RuntimeError("fleet mode needs AAREKA_INVENTORY (JSON list of node "
                               "specs: host, rack, transport, endpoints)")
        specs = [fleet.NodeSpec(**d) for d in json.load(open(inv_path))]
        results = fleet.poll_fleet(specs)
    host_dept = cfg.get("host_dept_map", {})               # server -> application/BU (from IT)
    batches = []
    for r in results:
        if r.status is not fleet.NodeStatus.OK or r.total_w <= 0:
            continue
        method = "measured" if r.measured_w >= r.modeled_w else "modeled"
        batches.append({
            "host": r.host, "captured_at": _iso_now(), "collector_version": VERSION,
            "gpus": [], "cpu_readings": [],
            "workloads": [{"id": r.host, "kind": "server", "cpu_share": 1.0,
                           "mem_mb": 0.0, "gpu_ids": [],
                           "department": host_dept.get(r.host)}],
            "node_total_w": round(r.total_w, 2), "node_total_method": method})
    return batches


def post_batch(base_url: str, key: str, batch: dict, timeout: float = 10.0):
    data = json.dumps(batch).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/ingest", data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Aareka-Key": key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def collect_batches(cfg: dict) -> list:
    """Build the batch(es) to send this cycle, per AAREKA_SOURCE. local -> one
    batch (this host); vcenter/kubernetes -> one per host/node."""
    src = cfg.get("source", "local")
    if src == "vcenter":
        return build_vcenter_batches(_make_vcenter(cfg), cfg["dept_map"])
    if src == "kubernetes":
        return build_k8s_batches(cfg)
    if src == "fleet":
        return build_fleet_batches(cfg)
    # local (default)
    sp = sps.read_server_power(prom_url=cfg.get("prom"), bmc=cfg.get("bmc"),
                               ipmi=cfg.get("ipmi"), pdu=cfg.get("pdu"))
    if not sp.readings:
        return []
    batch = build_full_batch(sp, cfg["host"], cfg["dept_map"]) \
        or build_flat_batch(sp, cfg["host"])
    return [batch]


def run_once(cfg: dict) -> bool:
    try:
        batches = collect_batches(cfg)
    except Exception as e:
        print(f"  collection failed ({cfg.get('source','local')}): {e}")
        return False
    if not batches:
        print("  nothing to send this cycle")
        return False
    ok_any = False
    for batch in batches:
        host = batch.get("host", "?")
        try:
            status, body = post_batch(cfg["url"], cfg["key"], batch)
            print(f"  {host}: HTTP {status}  "
                  f"readings={body.get('stored_readings', body.get('stored'))}  "
                  f"attributed={body.get('attributed_workloads', 0)}")
            attr = body.get("attribution")
            if attr and attr.get("by_department"):
                for d in attr["by_department"]:
                    print(f"      {d['department']:<14} {d['watts']:>8} W")
            ok_any = True
        except urllib.error.HTTPError as e:
            print(f"  {host}: ingest rejected HTTP {e.code} "
                  f"{e.read().decode('utf-8', 'ignore')[:200]}")
        except (urllib.error.URLError, OSError) as e:
            print(f"  {host}: could not reach ingest at {cfg['url']}: {e}")
    return ok_any


def load_cfg(args) -> dict:
    url = args.url or os.environ.get("AAREKA_INGEST_URL")
    key = args.key or os.environ.get("AAREKA_ORG_KEY")
    if not url or not key:
        sys.exit("error: set AAREKA_INGEST_URL and AAREKA_ORG_KEY "
                 "(or pass --url and --key)")
    dept_map = {}
    raw = os.environ.get("AAREKA_DEPT_MAP")
    if raw:
        try:
            dept_map = dict(json.loads(raw))
        except (ValueError, TypeError):
            print("  warning: AAREKA_DEPT_MAP is not valid JSON - ignoring")
    source = (args.source or os.environ.get("AAREKA_SOURCE") or "local").lower()
    if source not in ("local", "vcenter", "kubernetes", "fleet"):
        sys.exit(f"error: AAREKA_SOURCE must be local|vcenter|kubernetes|fleet "
                 f"(got '{source}')")
    host_dept_map = {}
    raw_hd = os.environ.get("AAREKA_HOST_DEPT_MAP")
    if raw_hd:
        try:
            host_dept_map = dict(json.loads(raw_hd))
        except (ValueError, TypeError):
            print("  warning: AAREKA_HOST_DEPT_MAP is not valid JSON - ignoring")
    return {
        "url": url, "key": key, "source": source,
        "host": args.host or os.environ.get("AAREKA_HOST") or socket.gethostname(),
        "interval": args.interval or int(os.environ.get("AAREKA_INTERVAL", "60")),
        "dept_map": dept_map,
        "prom": os.environ.get("AAREKA_PROM_URL"),
        "bmc": os.environ.get("AAREKA_BMC"),
        "ipmi": os.environ.get("AAREKA_IPMI"),
        "pdu": os.environ.get("AAREKA_PDU"),
        # vCenter mode
        "vcenter": os.environ.get("AAREKA_VCENTER"),
        "vc_user": os.environ.get("AAREKA_VCENTER_USER"),
        "vc_pwd": os.environ.get("AAREKA_VCENTER_PASSWORD"),
        "vc_dept_attr": os.environ.get("AAREKA_VCENTER_DEPT_ATTR"),
        # Kubernetes mode
        "kubeconfig": os.environ.get("AAREKA_KUBECONFIG"),
        "prom_node_label": os.environ.get("AAREKA_PROM_NODE_LABEL", "Hostname"),
        # fleet mode
        "inventory": os.environ.get("AAREKA_INVENTORY"),
        "host_dept_map": host_dept_map,
    }


def main():
    ap = argparse.ArgumentParser(description="Aareka sender agent (customer side).")
    ap.add_argument("--once", action="store_true", help="send one batch then exit")
    ap.add_argument("--url", help="Ingest API base URL")
    ap.add_argument("--key", help="org key")
    ap.add_argument("--host", help="host label (default: hostname)")
    ap.add_argument("--source", help="local | vcenter | kubernetes")
    ap.add_argument("--interval", type=int, help="seconds between sends")
    args = ap.parse_args()
    cfg = load_cfg(args)
    where = cfg["host"] if cfg["source"] == "local" else cfg["source"]
    print(f"\n  Aareka sender  ->  {cfg['url']}   source={cfg['source']} ({where})")
    if args.once:
        run_once(cfg)
        return
    print(f"  sending every {cfg['interval']}s. Ctrl+C to stop.\n")
    try:
        while True:
            run_once(cfg)
            time.sleep(cfg["interval"])
    except KeyboardInterrupt:
        print("\n  stopped.\n")


if __name__ == "__main__":
    main()
