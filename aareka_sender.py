#!/usr/bin/env python3
"""
Aareka  .  Sender agent   (customer side)

Runs inside the customer's environment. On an interval it:

    1. reads this machine's power via the collector (imp_server_power_source),
    2. reads the OWNERSHIP telemetry too - which process/workload is on which GPU
       (nvidia-smi compute-apps, live) - so the AWS engine can attribute it,
    3. packages watts + ownership into the Ingest API's batch shape,
    4. POSTs it to your Ingest API, authenticated with the customer's org key.

Read-only on the customer's systems. Outbound HTTPS only - it never opens a
port. Standard library only (no pip install).

What it sends (the data contract):
    (A) power     node_total_w, gpus[] (watts + mode + owner/shares), cpu_readings[]
    (B) topology  workloads[] (which workload, which GPU it's on)   <- live via nvidia-smi
    (C) tags      department per workload, from AAREKA_DEPT_MAP      <- optional mapping

Bare-metal / single-node works live today via nvidia-smi. Pulling rich topology
(cpu shares, tags) from Kubernetes / vCenter uses imp_topology / vcenter_connector,
whose live reads are still stubbed against mocks - that's the remaining integration.

Config (environment variables, or the matching --flags):
    AAREKA_INGEST_URL   your Ingest API base URL                                (required)
    AAREKA_ORG_KEY      the customer's secret key                               (required)
    AAREKA_INTERVAL     seconds between sends (default 60)
    AAREKA_HOST         host label (default: hostname)
    AAREKA_DEPT_MAP     JSON {workload_name: department} to tag ownership (optional)
    # optional telemetry endpoints handed to the collector:
    AAREKA_PROM_URL / AAREKA_BMC / AAREKA_IPMI / AAREKA_PDU

Run:
    python aareka_sender.py            # loop forever
    python aareka_sender.py --once     # send one batch then exit
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import imp_server_power_source as sps

VERSION = "0.2.0"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------- ownership telemetry
def _nvidia_smi(query: str) -> list[list[str]]:
    """Run a read-only nvidia-smi CSV query -> list of split rows. [] on failure."""
    try:
        out = subprocess.run(
            ["nvidia-smi", query, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return []
    if out.returncode != 0:
        return []
    return [[c.strip() for c in line.split(",")]
            for line in out.stdout.strip().splitlines() if line.strip()]


def gpu_processes_by_index() -> dict:
    """{gpu_index: [{name, mem_mb}]} - which processes run on which GPU, live."""
    # map GPU uuid -> index
    uuid_to_idx = {}
    for row in _nvidia_smi("--query-gpu=index,uuid"):
        if len(row) >= 2:
            uuid_to_idx[row[1]] = row[0]
    # per-process: pid, name, gpu uuid, used memory
    out: dict = {}
    for row in _nvidia_smi(
            "--query-compute-apps=pid,process_name,gpu_uuid,used_gpu_memory"):
        if len(row) < 4:
            continue
        _pid, name, uuid, mem = row[0], row[1], row[2], row[3]
        idx = uuid_to_idx.get(uuid, uuid)
        try:
            mem_mb = float(mem)
        except ValueError:
            mem_mb = 0.0
        # trim a full path down to the program name for a readable workload id
        short = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or name
        out.setdefault(idx, []).append({"name": short, "mem_mb": mem_mb})
    return out


# ------------------------------------------------------- batch builders
def build_full_batch(sp: "sps.ServerPower", host: str, dept_map: dict):
    """Watts + live GPU ownership -> the full contract. None if no GPU telemetry
    (caller then falls back to the flat, watts-only batch)."""
    gpu_readings = [r for r in sp.readings if r.scope in ("gpu", "gpu(detail)")]
    cpu_readings = [r for r in sp.readings if r.scope in ("cpu", "dram")]
    node_reading = next((r for r in sp.readings if r.scope in ("node", "rack")), None)
    if not gpu_readings:
        return None

    procs_by_idx = gpu_processes_by_index()
    gpus, workloads = [], {}

    for r in gpu_readings:
        idx = r.entity_id.replace("gpu", "").split("(")[0]     # "gpu0" -> "0"
        watts = round(r.watts, 2)
        procs = procs_by_idx.get(idx, [])
        # aggregate processes on this card by program name (same app = one workload)
        by_name: dict = {}
        for p in procs:
            by_name[p["name"]] = by_name.get(p["name"], 0.0) + p["mem_mb"]

        if not by_name:
            # powered GPU with no compute process -> nobody owns it (honest residual)
            gpus.append({"gpu_id": r.entity_id, "watts": watts,
                         "method": r.method.value, "mode": "passthrough", "owner": None})
            continue
        if len(by_name) == 1:                                  # sole tenant -> exact
            name = next(iter(by_name))
            gpus.append({"gpu_id": r.entity_id, "watts": watts,
                         "method": r.method.value, "mode": "passthrough", "owner": name})
        else:                                                  # shared -> split by mem
            total = sum(by_name.values()) or 1e-9
            shares = {n: round(m / total, 3) for n, m in by_name.items()}
            gpus.append({"gpu_id": r.entity_id, "watts": watts,
                         "method": r.method.value, "mode": "vgpu", "shares": shares})
        for n in by_name:
            w = workloads.setdefault(n, {"gpu_ids": set()})
            w["gpu_ids"].add(r.entity_id)

    workload_list = [{
        "id": n, "kind": "process",
        "cpu_share": 0.0,          # bare metal: no per-process CPU share without an
        "mem_mb": 0.0,             #   orchestrator, so CPU stays node overhead (honest)
        "gpu_ids": sorted(v["gpu_ids"]),
        "department": dept_map.get(n),   # None -> shown as "unmapped"
    } for n, v in workloads.items()]

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
    """Fallback: watts only (no ownership). The engine stores it but can't attribute."""
    return {
        "host": host, "captured_at": _iso_now(), "collector_version": VERSION,
        "readings": [{"entity_id": r.entity_id, "scope": r.scope,
                      "watts": round(r.watts, 2), "method": r.method.value,
                      "tier": r.tier, "error_pct": r.error_pct} for r in sp.readings],
    }


def post_batch(base_url: str, key: str, batch: dict, timeout: float = 10.0):
    """POST one batch to <base_url>/v1/ingest with the org key. Outbound only."""
    data = json.dumps(batch).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/ingest", data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Aareka-Key": key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def run_once(cfg: dict) -> bool:
    """Read this machine (watts + ownership), package, send one batch."""
    sp = sps.read_server_power(prom_url=cfg.get("prom"), bmc=cfg.get("bmc"),
                               ipmi=cfg.get("ipmi"), pdu=cfg.get("pdu"))
    if not sp.readings:
        print("  no readings from any source this cycle - nothing to send")
        return False

    batch = build_full_batch(sp, cfg["host"], cfg["dept_map"])
    shape = "full (watts + ownership)"
    if batch is None:
        batch = build_flat_batch(sp, cfg["host"])
        shape = "flat (watts only - no GPU ownership found)"

    try:
        status, body = post_batch(cfg["url"], cfg["key"], batch)
        print(f"  sent {shape} -> HTTP {status}: "
              f"readings={body.get('stored_readings', body.get('stored'))} "
              f"attributed={body.get('attributed_workloads', 0)}")
        attr = body.get("attribution")
        if attr and attr.get("by_department"):
            for d in attr["by_department"]:
                print(f"      {d['department']:<14} {d['watts']:>8} W")
        return True
    except urllib.error.HTTPError as e:
        print(f"  ingest rejected: HTTP {e.code} {e.read().decode('utf-8','ignore')[:200]}")
    except (urllib.error.URLError, OSError) as e:
        print(f"  could not reach ingest at {cfg['url']}: {e}")
    return False


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
    return {
        "url": url, "key": key,
        "host": args.host or os.environ.get("AAREKA_HOST") or socket.gethostname(),
        "interval": args.interval or int(os.environ.get("AAREKA_INTERVAL", "60")),
        "dept_map": dept_map,
        "prom": os.environ.get("AAREKA_PROM_URL"),
        "bmc": os.environ.get("AAREKA_BMC"),
        "ipmi": os.environ.get("AAREKA_IPMI"),
        "pdu": os.environ.get("AAREKA_PDU"),
    }


def main():
    ap = argparse.ArgumentParser(description="Aareka sender agent (customer side).")
    ap.add_argument("--once", action="store_true", help="send one batch then exit")
    ap.add_argument("--url", help="Ingest API base URL")
    ap.add_argument("--key", help="org key")
    ap.add_argument("--host", help="host label (default: hostname)")
    ap.add_argument("--interval", type=int, help="seconds between sends")
    args = ap.parse_args()
    cfg = load_cfg(args)

    print(f"\n  Aareka sender  ->  {cfg['url']}   host={cfg['host']}")
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
