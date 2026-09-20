#!/usr/bin/env python3
"""
Aareka  .  Sender agent   (customer side)

Runs inside the customer's environment. On an interval it:

    1. reads this machine's power via the collector (imp_server_power_source),
    2. packages the readings into the shape the Ingest API expects,
    3. POSTs them to your Ingest API, authenticated with the customer's org key.

Read-only on the customer's systems. Outbound HTTPS only - it never opens a
port. Standard library only (no pip install), so it drops onto any box that has
Python and the collector.

Config (environment variables, or the matching --flags):
    AAREKA_INGEST_URL   your Ingest API base URL, e.g. https://ingest.aareka.io  (required)
    AAREKA_ORG_KEY      the customer's secret key                                (required)
    AAREKA_INTERVAL     seconds between sends (default 60)
    AAREKA_HOST         host label (default: this machine's hostname)
    # optional telemetry endpoints handed to the collector:
    AAREKA_PROM_URL · AAREKA_BMC · AAREKA_IPMI · AAREKA_PDU

Run:
    python aareka_sender.py            # loop forever, sending every interval
    python aareka_sender.py --once     # read + send one batch, then exit
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import imp_server_power_source as sps

VERSION = "0.1.0"


def build_batch(sp: "sps.ServerPower", host: str) -> dict:
    """Turn a ServerPower reading into the Ingest API's request body."""
    return {
        "host": host,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "collector_version": VERSION,
        "readings": [
            {
                "entity_id": r.entity_id,
                "scope": r.scope,
                "watts": round(r.watts, 2),
                "method": r.method.value,     # "measured" | "modeled"
                "tier": r.tier,
                "error_pct": r.error_pct,
            }
            for r in sp.readings
        ],
    }


def post_batch(base_url: str, key: str, batch: dict, timeout: float = 10.0):
    """POST one batch to <base_url>/v1/ingest with the org key. Outbound only."""
    data = json.dumps(batch).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/ingest",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "X-Aareka-Key": key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def run_once(cfg: dict) -> bool:
    """Read this machine, package, and send one batch. Never raises upward."""
    sp = sps.read_server_power(prom_url=cfg.get("prom"), bmc=cfg.get("bmc"),
                               ipmi=cfg.get("ipmi"), pdu=cfg.get("pdu"))
    batch = build_batch(sp, cfg["host"])
    if not batch["readings"]:
        print("  no readings from any source this cycle - nothing to send")
        return False
    try:
        status, body = post_batch(cfg["url"], cfg["key"], batch)
        print(f"  sent {len(batch['readings'])} reading(s) -> {status} {body}")
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        print(f"  ingest rejected: HTTP {e.code} {detail}")
    except (urllib.error.URLError, OSError) as e:
        print(f"  could not reach ingest at {cfg['url']}: {e}")
    return False


def load_cfg(args) -> dict:
    url = args.url or os.environ.get("AAREKA_INGEST_URL")
    key = args.key or os.environ.get("AAREKA_ORG_KEY")
    if not url or not key:
        sys.exit("error: set AAREKA_INGEST_URL and AAREKA_ORG_KEY "
                 "(or pass --url and --key)")
    return {
        "url": url,
        "key": key,
        "host": args.host or os.environ.get("AAREKA_HOST") or socket.gethostname(),
        "interval": args.interval or int(os.environ.get("AAREKA_INTERVAL", "60")),
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
