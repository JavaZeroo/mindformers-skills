#!/usr/bin/env python3
"""Print step_trace_time.csv broken down into Computing / Comm / Free buckets.

This is always the FIRST thing to look at on a new profile — it classifies the
dominant cost (compute-bound vs comm-bound vs host-overhead-bound) and suggests
the next optimization direction.

Invariants this script checks:
  profiled_step ≈ Computing + Comm-NotOverlapped + Free + Preparing
  Communication = Comm-NotOverlapped + Overlapped

Usage:
  python3 step_trace_summary.py <profile_dir>

<profile_dir> can be any of:
  - the ASCEND_PROFILER_OUTPUT dir directly
  - the rank dir (192-168-9-112_<PID>_<TS>_ascend_ms/)
  - the parent profile/ dir (uses the first rank found)
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import resolve_profile_output


def fmt_ms(us):
    return f"{us/1000:7.1f} ms"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("profile_dir", help="path to profile/ or rank dir or ASCEND_PROFILER_OUTPUT/")
    args = ap.parse_args()

    out = resolve_profile_output(args.profile_dir)
    path = os.path.join(out, "step_trace_time.csv")
    if not os.path.isfile(path):
        sys.exit(f"missing: {path}")

    with open(path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        sys.exit(f"empty step_trace: {path}")

    print(f"profile: {out}")
    print()
    for row in rows:
        step = row.get("Step", "?")
        computing = float(row.get("Computing", 0))
        comm_not_overlapped = float(row.get("Communication(Not Overlapped)", 0))
        overlapped = float(row.get("Overlapped", 0))
        comm = float(row.get("Communication", 0))
        free = float(row.get("Free", 0))
        preparing = float(row.get("Preparing", 0))

        approx_step = computing + comm_not_overlapped + free + preparing
        print(f"=== Step {step} ===")
        print(f"  Computing            : {fmt_ms(computing)}")
        print(f"  Comm-NotOverlapped   : {fmt_ms(comm_not_overlapped)}")
        print(f"  Overlapped           : {fmt_ms(overlapped)}")
        print(f"  Communication (total): {fmt_ms(comm)}")
        print(f"  Free                 : {fmt_ms(free)}")
        print(f"  Preparing            : {fmt_ms(preparing)}")
        print(f"  ----")
        print(f"  ~step (sum)          : {fmt_ms(approx_step)}")
        print()

        # Invariant check (rough — comm/overlapped can have small accounting slop)
        comm_check = comm_not_overlapped + overlapped
        if abs(comm - comm_check) > 1.0:  # > 1us slop
            print(
                f"  ⚠ Invariant: Comm({fmt_ms(comm)}) != NotOverlapped + Overlapped "
                f"({fmt_ms(comm_check)}) — diff {fmt_ms(abs(comm - comm_check))}",
            )

        # Bottleneck classification
        buckets = {
            "Computing": computing,
            "Comm-NotOverlapped": comm_not_overlapped,
            "Free": free,
        }
        total = sum(buckets.values()) or 1.0
        ratios = {k: v / total for k, v in buckets.items()}
        dominant = max(ratios, key=ratios.get)

        print(f"  dominant bucket: {dominant} ({ratios[dominant]*100:.0f}% of step)")

        suggestion = {
            "Computing":
                "Compute-bound. Look for op fusion opportunities "
                "(`mint.add(alpha=)`, bmm-batched ops), fewer kernel launches.",
            "Comm-NotOverlapped":
                "Comm-bound. Run `comm_breakdown.py` next to see which op type. "
                "If allReduce count ≈ num_layers: batched allreduce. "
                "If P2P count is large: batched P2P. "
                "If AG/RS dominate: only fixable via fwd/bwd overlap (T3.1-class work).",
            "Free":
                "Host-overhead-bound (device idle waiting on Python / pyboost). "
                "Look for `asnumpy()` per-layer sync, repeated DTensor introspection, "
                "small kernel launch storm — batch / cache / fuse.",
        }
        print(f"  → {suggestion[dominant]}")

        overlap_ratio = overlapped / max(comm, 1.0)
        if overlap_ratio < 0.15 and comm > 0:
            print(
                f"  ⚠ Overlapped/Comm = {overlap_ratio*100:.0f}% — pipeline is sparse; "
                "issue async comms earlier, defer waits"
            )


if __name__ == "__main__":
    main()
