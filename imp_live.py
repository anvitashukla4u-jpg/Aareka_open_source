#!/usr/bin/env python3
"""
IMPS  .  live monitor  (THIS machine, real data)

No simulation. This reads YOUR machine's actual power every second using the
same source ladder the product uses - your GPU via nvidia-smi (measured), your
CPU via the model (or RAPL on Linux). Stress the machine and watch it move.

    py imp_live.py                 # refresh every 1s, Ctrl+C to stop
    py imp_live.py --interval 2

Everything here is a real reading off your hardware. The only estimated part is
the CPU (no RAPL on Windows) and the unmeasured tail - both flagged, honestly,
exactly as they would be in a datacenter.
"""

import argparse
import os
import shutil
import time
from collections import deque

import imp_server_power_source as sps


def bar(value, peak, width=40):
    """A simple text gauge scaled to the running peak."""
    if peak <= 0:
        peak = 1.0
    filled = int(width * min(value / peak, 1.0))
    return "#" * filled + "-" * (width - filled)


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()

    history = deque(maxlen=60)     # last 60 samples of total watts
    peak = 1.0

    try:
        while True:
            sp = sps.read_server_power()      # REAL read of this machine
            total = sp.total_w
            history.append(total)
            peak = max(peak, total)
            lo, hi = sp.est_total_range

            clear()
            print("=" * 62)
            print("  IMPS LIVE  -  this machine, real power  (Ctrl+C to stop)")
            print("=" * 62)
            print(f"  gpu tier: {sp.gpu_tier:<8}  cpu tier: {sp.cpu_tier}")
            print()

            # per-source lines (real readings)
            for r in sorted(sp.readings, key=lambda x: x.watts, reverse=True):
                tag = r.method.value
                print(f"  {r.entity_id:<20}{r.watts:>7.1f} W  {tag:<9} [{r.tier}]")
            print()

            # live gauge on total
            print(f"  TOTAL  {total:6.1f} W   peak {peak:5.1f} W")
            print(f"  [{bar(total, peak)}]")
            print()

            # honesty line
            print(f"  measured: {sp.measured_fraction*100:3.0f}%   "
                  f"(GPU real; CPU {'modeled' if sp.cpu_tier=='model' else sp.cpu_tier})")
            if sp.low_confidence:
                print(f"  est. true total: {lo:.0f} - {hi:.0f} W  "
                      f"(no BMC on a laptop -> fan/SSD tail estimated)")

            # tiny sparkline of recent totals
            if len(history) > 1:
                mn, mx = min(history), max(history)
                spark = "".join(
                    " ▁▂▃▄▅▆▇█"[int(8 * (v - mn) / (mx - mn))] if mx > mn else "▁"
                    for v in history)
                print(f"\n  last {len(history)}s: {spark}")

            print("\n  tip: open apps / run something GPU-heavy and watch it climb.")
            time.sleep(a.interval)

    except KeyboardInterrupt:
        print("\n  stopped.\n")


if __name__ == "__main__":
    main()
