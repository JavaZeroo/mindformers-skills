#!/usr/bin/env python3
"""Aggregate communication.json by op type and print per-op stats.

For a single profiled step, prints every collective + P2P op type with its count,
total elapsed time, and total bytes transferred. Sorted by total elapse desc, so
the top row is always the biggest comm cost.

Use this AFTER step_trace_summary.py says "Comm-NotOverlapped dominates" — this
tells you WHICH op type is the cost.

Heuristic op-count signals:
  * allReduce count ≈ num_layers     → per-layer allreduce that could be stacked
  * P2P count >> num_layers          → per-weight P2P, batchable by (owner, group)
  * allGather count high + huge size → forward/backward FSDP, not optimizer

Usage:
  python3 comm_breakdown.py <profile_dir>
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import resolve_profile_output


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("profile_dir")
    args = ap.parse_args()

    out = resolve_profile_output(args.profile_dir)
    path = os.path.join(out, "communication.json")
    if not os.path.isfile(path):
        sys.exit(f"missing: {path}")

    with open(path) as f:
        data = json.load(f)

    if not data:
        sys.exit(f"empty communication.json: {path}")

    step_key = next(iter(data))
    step = data[step_key]

    agg = defaultdict(lambda: {"count": 0, "elapse_ms": 0.0, "transit_ms": 0.0,
                               "wait_ms": 0.0, "size_mb": 0.0})

    for kind in ("collective", "p2p"):
        for op_name, info in step.get(kind, {}).items():
            if "Total Op Info" in op_name:
                continue
            # Extract op type from "hcom_allGather__416_0_1@..." → "allGather"
            if "hcom_" in op_name:
                op_type = op_name.split("hcom_", 1)[1].split("_")[0]
            else:
                op_type = op_name

            t = info.get("Communication Time Info", {})
            bw = info.get("Communication Bandwidth Info", {})
            size_mb = sum(
                bw.get(link, {}).get("Transit Size(MB)", 0)
                for link in ("HCCS", "SDMA", "RDMA", "PCIE")
            )

            key = (kind, op_type)
            agg[key]["count"] += 1
            agg[key]["elapse_ms"] += t.get("Elapse Time(ms)", 0)
            agg[key]["transit_ms"] += t.get("Transit Time(ms)", 0)
            agg[key]["wait_ms"] += t.get("Wait Time(ms)", 0)
            agg[key]["size_mb"] += size_mb

    print(f"profile: {out}  step: {step_key}")
    print()
    print(f"  {'kind':11s} {'op':14s}  {'count':>5s}  {'elapse':>10s}  "
          f"{'transit':>10s}  {'wait':>10s}  {'size':>10s}")
    print(f"  {'-'*11} {'-'*14}  {'-'*5}  {'-'*10}  {'-'*10}  {'-'*10}  {'-'*10}")

    total_elapse = 0.0
    for (kind, op), v in sorted(agg.items(), key=lambda kv: -kv[1]["elapse_ms"]):
        print(
            f"  {kind:11s} {op:14s}  {v['count']:5d}  "
            f"{v['elapse_ms']:>8.1f}ms  {v['transit_ms']:>8.1f}ms  "
            f"{v['wait_ms']:>8.1f}ms  {v['size_mb']:>8.1f}MB"
        )
        total_elapse += v["elapse_ms"]

    print(f"  {'-'*11} {'-'*14}  {'-'*5}  {'-'*10}")
    print(f"  {'TOTAL':11s} {'':14s}  {'':5s}  {total_elapse:>8.1f}ms")


if __name__ == "__main__":
    main()
