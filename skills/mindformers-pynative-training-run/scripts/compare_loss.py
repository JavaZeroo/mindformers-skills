#!/usr/bin/env python3
"""Bit-identity check between two MindFormers worker logs.

After an optimization that should mathematically preserve the same forward/backward
results (allreduce stacking, kernel fusion that's exact like mint.add(alpha=), op
reordering that doesn't touch FP arithmetic order), loss should be **bit-identical**
between the before and after runs.  When that's the case this script reports OK.

When losses differ, it shows the first divergent step and the relative magnitude —
useful to distinguish:
  * ~1e-5 relative differences from FP accumulation order changes (mm -> bmm on
    Ascend, allreduce-order rearrangement) — equivalent, not a bug
  * >1e-3 differences that suggest a real semantic change

Usage:
  python3 compare_loss.py <log_a> <log_b>
  python3 compare_loss.py <log_a> <log_b> --tol 1e-5
"""
import argparse
import re
import sys


LOSS_LINE = re.compile(
    r"step:\[\s*(\d+)/\s*\d+\],\s*loss:\s*([0-9.]+),.*per_step_time:\s*(\d+)ms"
)


def parse(path):
    rows = {}
    with open(path) as f:
        for line in f:
            m = LOSS_LINE.search(line)
            if m:
                rows[int(m.group(1))] = float(m.group(2))
    return rows


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("log_a", help="first worker_*.log (baseline)")
    ap.add_argument("log_b", help="second worker_*.log (after change)")
    ap.add_argument(
        "--tol",
        type=float,
        default=0.0,
        help="treat |a-b| <= tol as identical (default 0 = strict bit-identity). "
        "Use 1e-5 if comparing across mm/bmm or FP-reordering changes.",
    )
    args = ap.parse_args()

    a = parse(args.log_a)
    b = parse(args.log_b)
    if not a or not b:
        print(
            f"empty parse: a={len(a)} samples from {args.log_a}, "
            f"b={len(b)} samples from {args.log_b}",
            file=sys.stderr,
        )
        sys.exit(1)

    common = sorted(set(a) & set(b))
    if not common:
        print(f"no overlapping step numbers (a={sorted(a)[:3]}.., b={sorted(b)[:3]}..)")
        sys.exit(1)

    mismatches = []
    max_abs_diff = 0.0
    for s in common:
        d = abs(a[s] - b[s])
        if d > args.tol:
            mismatches.append((s, a[s], b[s], d))
        if d > max_abs_diff:
            max_abs_diff = d

    print(f"a:        {args.log_a}")
    print(f"b:        {args.log_b}")
    print(f"common steps: {len(common)}  ({min(common)}..{max(common)})")
    print(f"max abs(a-b): {max_abs_diff:.6e}")
    if not mismatches:
        print(f"=> OK — bit-identical (within tol={args.tol})")
        return

    print(f"=> DIFF — {len(mismatches)} of {len(common)} steps differ")
    print()
    print("first 5 divergences:")
    for s, av, bv, d in mismatches[:5]:
        rel = d / max(abs(av), 1e-12)
        print(f"  step {s:>4d}: a={av:.6f}  b={bv:.6f}  |Δ|={d:.3e}  rel={rel:.2e}")

    largest = sorted(mismatches, key=lambda x: -x[3])[:3]
    print()
    print("largest 3 divergences:")
    for s, av, bv, d in largest:
        rel = d / max(abs(av), 1e-12)
        print(f"  step {s:>4d}: a={av:.6f}  b={bv:.6f}  |Δ|={d:.3e}  rel={rel:.2e}")

    # Heuristic: relative diff < 1e-4 across all = likely FP-order equivalence
    max_rel = max(d / max(abs(av), 1e-12) for _, av, _, d in mismatches)
    print()
    if max_rel < 1e-4:
        print(
            f"  max relative diff = {max_rel:.2e} — likely FP accumulation order "
            "(e.g., mm→bmm on Ascend, allreduce reorder). Math-equivalent."
        )
    elif max_rel < 1e-2:
        print(
            f"  max relative diff = {max_rel:.2e} — moderate. Inspect: does the "
            "change touch arithmetic order at scale (large reductions)?"
        )
    else:
        print(
            f"  max relative diff = {max_rel:.2e} — likely a real semantic "
            "change. Verify the optimization preserves the math."
        )


if __name__ == "__main__":
    main()
