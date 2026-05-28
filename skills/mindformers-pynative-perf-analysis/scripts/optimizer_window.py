#!/usr/bin/env python3
"""Find a function's time window in trace_view.json, print its duration and
the device-kernel breakdown that ran inside it.

Default pattern matches MindFormers Muon optimizer: ``muon.py(...):construct``.
For other functions pass ``--pattern foo.py(...):bar``.

Use this to answer:
  * "how long was the optimizer in this step?"
  * "inside that window, what was the device doing — compute, comm, or wait?"

Note: trace_view.json can be 100MB+ — parsing takes ~10-30 seconds.

Usage:
  python3 optimizer_window.py <profile_dir>
  python3 optimizer_window.py <profile_dir> --pattern muon.py --func construct
"""
import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import resolve_profile_output


PYTHON_FN_PID = 1     # MindSpore profiler emits Python function events under pid=1


def discover_device_pids(events):
    """Find the pids that hold Ascend hardware kernel + communication events.

    The MindSpore profiler emits ``process_name`` metadata events (``ph=='M'``)
    naming each pid.  We pick the ones named like "Ascend Hardware" or
    "Communication"; their numeric pids differ across profile sessions.
    Falls back to a permissive default of "any pid > 1" if we can't find names.
    """
    interesting = set()
    have_metadata = False
    for e in events:
        if e.get("ph") != "M":
            continue
        if e.get("name") != "process_name":
            continue
        have_metadata = True
        nm = e.get("args", {}).get("name", "")
        if "Ascend Hardware" in nm or "Communication" in nm:
            interesting.add(e.get("pid"))
    if interesting:
        return interesting
    if have_metadata:
        # Metadata exists but no Ascend/Communication processes — empty profile?
        return set()
    # No metadata at all — fall back to "anything that isn't a Python pid".
    return {e.get("pid") for e in events
            if e.get("pid") not in (None, PYTHON_FN_PID)}


def classify_kernel(name):
    """Map a kernel event name to a coarse bucket."""
    if "allGather" in name:
        return "allGather"
    if "allReduce" in name:
        return "allReduce"
    if "reduceScatter" in name:
        return "reduceScatter"
    if "Send" in name or "send" in name.lower():
        return "P2P_send"
    if "Receive" in name or "recv" in name.lower() or "eceive" in name:
        return "P2P_recv"
    if "Notify" in name and "Wait" in name:
        return "Notify_Wait"
    if "EVENT_WAIT" in name:
        return "EVENT_WAIT"
    if "Memcpy" in name or "SDMA" in name:
        return "Memcpy/SDMA"
    return "compute"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("profile_dir")
    ap.add_argument(
        "--pattern",
        default="muon.py",
        help="substring that must appear in the event name (default 'muon.py')",
    )
    ap.add_argument(
        "--func",
        default="construct",
        help="the function name suffix after the colon (default 'construct')",
    )
    args = ap.parse_args()

    out = resolve_profile_output(args.profile_dir)
    path = os.path.join(out, "trace_view.json")
    if not os.path.isfile(path):
        sys.exit(f"missing: {path}")

    print(f"profile: {out}")
    print(f"loading {path} ({os.path.getsize(path) / 1024**2:.0f} MB)...", file=sys.stderr)

    with open(path) as f:
        data = json.load(f)
    events = data if isinstance(data, list) else data.get("traceEvents", [])
    print(f"  {len(events)} events", file=sys.stderr)

    # Find the function window.  If multiple events match (e.g. two optimizer
    # invocations per step), take the longest — that's the most informative.
    matches = []
    needle = f":{args.func}"
    for e in events:
        nm = e.get("name", "")
        if not isinstance(nm, str):
            continue
        if e.get("pid") != PYTHON_FN_PID:
            continue
        if args.pattern not in nm:
            continue
        if needle not in nm:
            continue
        try:
            dur = float(e.get("dur", 0))
        except (TypeError, ValueError):
            continue
        matches.append((dur, e))

    if not matches:
        sys.exit(
            f"no event matched pattern={args.pattern!r} func={args.func!r} on pid={PYTHON_FN_PID}.\n"
            "Try running with --pattern '' --func construct to see all :construct events first."
        )

    matches.sort(key=lambda x: -x[0])
    dur, opt = matches[0]
    t0 = float(opt["ts"])
    t1 = t0 + dur

    print(f"matched: {opt['name']}")
    if len(matches) > 1:
        print(f"  ({len(matches)} matches; using longest, {dur/1000:.1f} ms)")
    print(f"window: {dur/1000:.1f} ms  (ts {t0/1000:.0f}us → {t1/1000:.0f}us)")
    print()

    # Aggregate device kernels in window
    device_pids = discover_device_pids(events)
    if not device_pids:
        sys.exit("could not find Ascend Hardware / Communication process pids in the trace")

    kernels = defaultdict(lambda: {"count": 0, "dur_us": 0.0})
    for e in events:
        if e.get("pid") not in device_pids:
            continue
        if e.get("ph") != "X":  # complete events only
            continue
        try:
            ts = float(e.get("ts"))
            d = float(e.get("dur", 0))
        except (TypeError, ValueError):
            continue
        if ts < t0 or ts + d > t1 + 1:
            continue
        bucket = classify_kernel(e.get("name", ""))
        kernels[bucket]["count"] += 1
        kernels[bucket]["dur_us"] += d

    if not kernels:
        print("no device kernels found in window — try a different rank's profile")
        return

    print(f"  {'bucket':14s}  {'count':>5s}  {'total':>10s}")
    print(f"  {'-'*14}  {'-'*5}  {'-'*10}")
    for bucket, v in sorted(kernels.items(), key=lambda kv: -kv[1]["dur_us"]):
        print(f"  {bucket:14s}  {v['count']:5d}  {v['dur_us']/1000:>8.1f}ms")
    print(f"  {'-'*14}  {'-'*5}  {'-'*10}")
    print(f"  {'window dur':14s}  {'':5s}  {dur/1000:>8.1f}ms")
    print()
    print(
        "  Note: bucket totals are summed across compute+comm streams which run in "
        "parallel, so they can exceed the window duration (e.g. Notify_Wait on the "
        "comm stream during compute). What matters is the RELATIVE size — big "
        "Notify_Wait / EVENT_WAIT = device is idle waiting; big allGather/allReduce "
        "= the comm itself is expensive; big 'compute' = real on-device work."
    )


if __name__ == "__main__":
    main()
