#!/usr/bin/env python3
"""Extract per_step_time stats from a MindFormers worker_*.log.

The loss callback in MindFormers logs lines like:
  { step:[   53/  250], loss:  11.687554, per_step_time:    535ms, ... }

This script parses every such line, drops the first N warmup/profiler-affected
steps, and prints median / mean / min per_step_time plus the final loss.
Designed to be the one-line answer to "how fast did this run train?"

Usage:
  python3 median_per_step.py <worker_log>
  python3 median_per_step.py <worker_log> --warmup 100
"""
import argparse
import re
import statistics
import sys

LOSS_LINE = re.compile(
    r"step:\[\s*(\d+)/\s*\d+\],\s*loss:\s*([0-9.]+),.*per_step_time:\s*(\d+)ms"
)


def parse(path):
    """Return {step: (loss, per_step_time_ms)} from the log."""
    rows = {}
    with open(path) as f:
        for line in f:
            m = LOSS_LINE.search(line)
            if m:
                rows[int(m.group(1))] = (float(m.group(2)), int(m.group(3)))
    return rows


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("log", help="path to worker_*.log")
    ap.add_argument(
        "--warmup",
        type=int,
        default=50,
        help="drop steps with step_num <= warmup (default 50; this repo only logs the last "
        "50 steps anyway so the typical filter is step >= 51)",
    )
    args = ap.parse_args()

    rows = parse(args.log)
    if not rows:
        print(f"no per_step_time lines found in {args.log}", file=sys.stderr)
        sys.exit(1)

    steady = [(s, l, t) for s, (l, t) in rows.items() if s > args.warmup]
    if not steady:
        print(
            f"no steady-state samples after warmup={args.warmup}; "
            f"steps seen: {sorted(rows)[:3]}..{sorted(rows)[-3:]}",
            file=sys.stderr,
        )
        sys.exit(1)

    times = [t for _, _, t in steady]
    steps = sorted(s for s, _, _ in steady)
    last_step = max(rows)
    last_loss, _ = rows[last_step]

    print(f"file:           {args.log}")
    print(f"steps measured: {len(times)}  (from step {steps[0]} to {steps[-1]})")
    print(f"  median per_step: {statistics.median(times):.1f} ms")
    print(f"  mean   per_step: {statistics.mean(times):.1f} ms")
    print(f"  min    per_step: {min(times)} ms")
    print(f"  max    per_step: {max(times)} ms")
    print(f"  final loss @ step {last_step}: {last_loss}")


if __name__ == "__main__":
    main()
