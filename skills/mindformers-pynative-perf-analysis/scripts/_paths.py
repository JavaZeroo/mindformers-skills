"""Shared profile-path resolution used by the other scripts in this directory.

Users may pass any of these forms to the scripts; we normalize to the
``ASCEND_PROFILER_OUTPUT/`` directory:

  ./profile/                                                       (parent — pick first rank dir)
  ./profile/192-168-9-112_<PID>_<TS>_ascend_ms/                   (rank dir)
  ./profile/192-168-9-112_<PID>_<TS>_ascend_ms/ASCEND_PROFILER_OUTPUT/  (already correct)
"""
import glob
import os
import sys


def resolve_profile_output(path: str) -> str:
    """Return the path to an ASCEND_PROFILER_OUTPUT directory.

    Tries (in order):
      1. ``path`` itself if it already points at an ASCEND_PROFILER_OUTPUT dir.
      2. ``path/ASCEND_PROFILER_OUTPUT`` if ``path`` is a rank dir.
      3. Glob ``path/*_ascend_ms/ASCEND_PROFILER_OUTPUT`` and pick the first
         (warn to stderr if more than one — caller should disambiguate).

    Exits with a clear error if nothing matches.
    """
    if not os.path.exists(path):
        sys.exit(f"profile path does not exist: {path}")

    # Case 1: already the output dir
    if os.path.basename(os.path.normpath(path)) == "ASCEND_PROFILER_OUTPUT":
        return os.path.normpath(path)
    if os.path.isfile(os.path.join(path, "step_trace_time.csv")):
        return os.path.normpath(path)

    # Case 2: rank dir
    direct = os.path.join(path, "ASCEND_PROFILER_OUTPUT")
    if os.path.isdir(direct):
        return direct

    # Case 3: parent dir — glob rank subdirs
    candidates = sorted(glob.glob(os.path.join(path, "*_ascend_ms", "ASCEND_PROFILER_OUTPUT")))
    if candidates:
        if len(candidates) > 1:
            print(
                f"warning: {len(candidates)} rank-profile dirs under {path}; "
                f"using rank 0 ({candidates[0]}). Pass a specific dir to override.",
                file=sys.stderr,
            )
        return candidates[0]

    sys.exit(
        f"could not find ASCEND_PROFILER_OUTPUT under {path}.\n"
        f"  Expected one of:\n"
        f"    {path}/ASCEND_PROFILER_OUTPUT/\n"
        f"    {path}/192-168-9-112_*_ascend_ms/ASCEND_PROFILER_OUTPUT/"
    )
