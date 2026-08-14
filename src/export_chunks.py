#!/usr/bin/env python3
"""
Split large CSV files into GitHub-safe chunks.
================================================

This does NOT touch collection/fetch logic at all - it's a pure post-processing
step. It takes CSV files that may be too large for a single git push (GitHub
warns above 50MB and hard-blocks above 100MB per file) and splits each one into
numbered parts small enough to commit safely, e.g.:

    data/monthly/2026-05.csv           (87MB, source - stays out of git)
      -> data/monthly_chunks/2026-05_1.csv   (~40MB)
      -> data/monthly_chunks/2026-05_2.csv   (~40MB)
      -> data/monthly_chunks/2026-05_3.csv   (~7MB)

Every chunk keeps the original header row, so each part file is independently
a valid, readable CSV.

Re-run safe: on every run, this wipes and regenerates all chunks for a given
source file from scratch, so it always reflects the current state of the
source (handles the source file growing, e.g. the current in-progress month)
without ever leaving stale/orphaned old chunks behind.

Usage:
    python src/export_chunks.py --src data/monthly --out data/monthly_chunks --max-mb 40
    python src/export_chunks.py --src data/latest_images.csv --out data/latest_images_chunks --max-mb 40

    --src can be a single .csv file OR a directory (all *.csv files in it are chunked).
"""

import argparse
import csv
import glob
import os
import sys


def split_one_csv(src_path, out_dir, max_bytes):
    base_name = os.path.splitext(os.path.basename(src_path))[0]

    # Wipe any previous chunks for this source file so we never leave stale
    # leftovers behind (e.g. if this run produces fewer/more parts than last time).
    for stale in glob.glob(os.path.join(out_dir, f"{base_name}_*.csv")):
        os.remove(stale)

    os.makedirs(out_dir, exist_ok=True)

    with open(src_path, "r", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            print(f"  {src_path}: empty, skipping", file=sys.stderr)
            return []

        part_num = 1
        chunk_paths = []

        def open_part(n):
            path = os.path.join(out_dir, f"{base_name}_{n}.csv")
            fh = open(path, "w", newline="")
            w = csv.writer(fh)
            w.writerow(header)
            return path, fh, w

        path, fh, writer = open_part(part_num)
        chunk_paths.append(path)
        current_size = fh.tell()

        for row in reader:
            # Estimate this row's serialized size before writing, so we can
            # start a new part *before* going over the limit rather than after.
            row_str = ",".join(f'"{c}"' if "," in c or '"' in c else c for c in row) + "\r\n"
            row_size = len(row_str.encode("utf-8"))

            if current_size + row_size > max_bytes and current_size > 0:
                fh.close()
                part_num += 1
                path, fh, writer = open_part(part_num)
                chunk_paths.append(path)
                current_size = fh.tell()

            writer.writerow(row)
            current_size += row_size

        fh.close()

    total_size = sum(os.path.getsize(p) for p in chunk_paths)
    print(f"  {src_path} ({total_size / 1e6:.1f}MB total) -> {len(chunk_paths)} part(s): "
          f"{', '.join(os.path.basename(p) for p in chunk_paths)}", file=sys.stderr)
    return chunk_paths


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="A .csv file, or a directory containing .csv files")
    ap.add_argument("--out", required=True, help="Directory to write numbered chunk files into")
    ap.add_argument("--max-mb", type=float, default=40.0,
                     help="Max size per chunk in MB (default 40, safely under GitHub's 50MB warning / 100MB hard limit)")
    args = ap.parse_args()

    max_bytes = int(args.max_mb * 1024 * 1024)

    if os.path.isdir(args.src):
        sources = sorted(glob.glob(os.path.join(args.src, "*.csv")))
        if not sources:
            print(f"No .csv files found in {args.src}", file=sys.stderr)
            return
    elif os.path.isfile(args.src):
        sources = [args.src]
    else:
        sys.exit(f"ERROR: {args.src} does not exist")

    print(f"Chunking {len(sources)} CSV file(s) into {args.out} (max {args.max_mb}MB/part)...", file=sys.stderr)
    for src in sources:
        split_one_csv(src, args.out, max_bytes)


if __name__ == "__main__":
    main()
