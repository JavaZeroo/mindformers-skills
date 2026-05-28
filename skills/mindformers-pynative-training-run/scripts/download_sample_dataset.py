#!/usr/bin/env python3
"""Download a prepared Megatron-format sample dataset (idx + bin pair).

This is the fallback for when the user's yaml has a placeholder `data_path`
(e.g. `/path/to/...`) and they don't have their own data.  Sources are the
PAI-Megatron-Patch example datasets hosted on Aliyun OSS — already tokenized
and packaged as `mmap_<model>_datasets_text_document.{idx,bin}`.

After download, the prefix you put in the yaml's `data_path` is the file path
*without* the `.bin` / `.idx` suffix.  This script prints that prefix on
success so you can paste it straight into the yaml.

Usage:
  python3 download_sample_dataset.py --model deepseek_v3 --dest <dir>
  python3 download_sample_dataset.py --model qwen3       --dest <dir>

  # Resume an interrupted download — curl -C - is used internally, but
  # passing --force re-downloads from scratch.
  python3 download_sample_dataset.py --model deepseek_v3 --dest <dir> --force
"""
import argparse
import os
import shutil
import subprocess
import sys

DATASETS = {
    "deepseek_v3": {
        "bin": "https://atp-modelzoo-wlcb-pai.oss-cn-wulanchabu.aliyuncs.com/release/models/pai-megatron-patch/deepseek-datasets/mmap_deepseekv3_datasets_text_document.bin",
        "idx": "https://atp-modelzoo-wlcb-pai.oss-cn-wulanchabu.aliyuncs.com/release/models/pai-megatron-patch/deepseek-datasets/mmap_deepseekv3_datasets_text_document.idx",
        "prefix_name": "mmap_deepseekv3_datasets_text_document",
        "approx_size": "4.3 GB",
        "tokenizer": "DeepSeekV2Tokenizer",
    },
    "qwen3": {
        "bin": "https://atp-modelzoo-wlcb-pai.oss-cn-wulanchabu.aliyuncs.com/release/models/pai-megatron-patch/qwen-datasets/mmap_qwen3_datasets_text_document.bin",
        "idx": "https://atp-modelzoo-wlcb-pai.oss-cn-wulanchabu.aliyuncs.com/release/models/pai-megatron-patch/qwen-datasets/mmap_qwen3_datasets_text_document.idx",
        "prefix_name": "mmap_qwen3_datasets_text_document",
        "approx_size": "197 MB",
        "tokenizer": "Qwen tokenizer (see PAI-Megatron-Patch examples/qwen3/README)",
    },
}


def http_size(url):
    """Best-effort Content-Length via curl -sI; 0 if unavailable."""
    try:
        r = subprocess.run(
            ["curl", "-sIL", url],
            check=False, capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    for line in r.stdout.splitlines():
        if line.lower().startswith("content-length:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return 0


def download(url, dest_path, force=False):
    """Download url -> dest_path with curl, resuming if a partial exists."""
    if os.path.isfile(dest_path) and not force:
        local = os.path.getsize(dest_path)
        remote = http_size(url)
        if remote and local == remote:
            print(f"  ✓ {os.path.basename(dest_path)} already complete ({local:,} bytes)")
            return
        if remote and local < remote:
            print(f"  → resuming {os.path.basename(dest_path)} from {local:,}/{remote:,} bytes")
        # else: size unknown or mismatched, fall through to curl -C - which is safe
    else:
        print(f"  → downloading {os.path.basename(dest_path)}")

    cmd = [
        "curl", "-L", "--fail",
        "-C", "-",                 # resume; harmless on fresh files
        "-o", dest_path,
        url,
    ]
    if force and os.path.isfile(dest_path):
        os.remove(dest_path)
    rc = subprocess.call(cmd)
    if rc != 0:
        sys.exit(f"curl failed ({rc}) for {url}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--model",
        required=True,
        choices=sorted(DATASETS),
        help="which sample dataset to fetch (deepseek_v3: 4.3GB, qwen3: 197MB)",
    )
    ap.add_argument(
        "--dest",
        required=True,
        help="directory the .bin/.idx pair will be written to (created if missing)",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="re-download from scratch even if the destination files exist",
    )
    args = ap.parse_args()

    if shutil.which("curl") is None:
        sys.exit("curl not found in PATH — install it with `apt-get install -y curl`")

    spec = DATASETS[args.model]
    dest = os.path.abspath(args.dest)
    os.makedirs(dest, exist_ok=True)

    bin_path = os.path.join(dest, spec["prefix_name"] + ".bin")
    idx_path = os.path.join(dest, spec["prefix_name"] + ".idx")

    print(f"model:     {args.model}  (~{spec['approx_size']}, tokenizer: {spec['tokenizer']})")
    print(f"dest dir:  {dest}")
    print()

    # Smaller file first — fails faster if URL/permissions are broken
    download(spec["idx"], idx_path, force=args.force)
    download(spec["bin"], bin_path, force=args.force)

    prefix = os.path.join(dest, spec["prefix_name"])
    print()
    print("Done.  Paste this prefix (no extension) into the yaml's data_path:")
    print()
    print(f"  data_path:")
    print(f"    - '1'")
    print(f"    - \"{prefix}\"")
    print()
    print("(The '1' is the megatron blending weight; keep it.)")


if __name__ == "__main__":
    main()
