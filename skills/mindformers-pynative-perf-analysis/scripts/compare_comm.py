#!/usr/bin/env python3
"""Diff communication.json across two profiles (before/after an optimization).

Prints each op type with (count_a → count_b, elapse_a → elapse_b), plus the
deltas, sorted by |Δ elapse| desc.  Strong signal that a batching optimization
landed: a big count drop on one op type (e.g. allReduce 28 → 5).

Usage:
  python3 compare_comm.py <profile_dir_a> <profile_dir_b>
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import resolve_profile_output


def aggregate(profile_dir):
    out = resolve_profile_output(profile_dir)
    path = os.path.join(out, "communication.json")
    if not os.path.isfile(path):
        sys.exit(f"missing: {path}")
    with open(path) as f:
        data = json.load(f)
    if not data:
        sys.exit(f"empty communication.json: {path}")
    step = next(iter(data.values()))
    agg = defaultdict(lambda: {"count": 0, "elapse_ms": 0.0})
    for kind in ("collective", "p2p"):
        for op_name, info in step.get(kind, {}).items():
            if "Total Op Info" in op_name:
                continue
            if "hcom_" in op_name:
                op_type = op_name.split("hcom_", 1)[1].split("_")[0]
            else:
                op_type = op_name
            t = info.get("Communication Time Info", {})
            agg[(kind, op_type)]["count"] += 1
            agg[(kind, op_type)]["elapse_ms"] += t.get("Elapse Time(ms)", 0)
    return out, agg


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("profile_a", help="baseline profile dir")
    ap.add_argument("profile_b", help="after-change profile dir")
    args = ap.parse_args()

    out_a, a = aggregate(args.profile_a)
    out_b, b = aggregate(args.profile_b)

    print(f"A: {out_a}")
    print(f"B: {out_b}")
    print()

    all_keys = set(a) | set(b)
    rows = []
    for k in all_keys:
        av = a.get(k, {"count": 0, "elapse_ms": 0.0})
        bv = b.get(k, {"count": 0, "elapse_ms": 0.0})
        rows.append((k, av["count"], bv["count"], av["elapse_ms"], bv["elapse_ms"]))
    rows.sort(key=lambda r: -abs(r[4] - r[3]))

    print(
        f"  {'kind':11s} {'op':14s}  {'cnt A':>5s}/{'cnt B':<5s}  "
        f"{'Δcnt':>+6s}  {'elapse A':>9s} {'elapse B':>9s}  {'Δelapse':>+10s}"
    )
    print(f"  {'-'*11} {'-'*14}  {'-'*11}  {'-'*6}  {'-'*9} {'-'*9}  {'-'*10}")

    total_a = total_b = 0.0
    for (kind, op), ca, cb, ea, eb in rows:
        d_cnt = cb - ca
        d_elapse = eb - ea
        print(
            f"  {kind:11s} {op:14s}  {ca:5d}/{cb:<5d}  "
            f"{d_cnt:+6d}  {ea:>7.1f}ms {eb:>7.1f}ms  {d_elapse:+8.1f}ms"
        )
        total_a += ea
        total_b += eb

    print(f"  {'-'*11} {'-'*14}  {'-'*11}  {'-'*6}  {'-'*9} {'-'*9}  {'-'*10}")
    print(
        f"  {'TOTAL':11s} {'':14s}  {'':11s}  {'':6s}  "
        f"{total_a:>7.1f}ms {total_b:>7.1f}ms  {total_b - total_a:+8.1f}ms"
    )


if __name__ == "__main__":
    main()
