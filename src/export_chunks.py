#!/usr/bin/env python3
"""
Split large CSV files into GitHub-safe chunks.
================================================

This does NOT touch collection/fetch logic at all - it's a pure post-processing
step. It takes CSV files that may be too large for a single git push (GitHub
warns above 50MB and hard-blocks above 100MB per file) and splits each one into
numbered parts small enough to commit safely, e.g.:

    data/latest_images.csv   (400MB+, source - stays out of git)
      -> data/latest_images_chunks/latest_images_1.csv   (~40MB)
      -> data/latest_images_chunks/latest_images_2.csv   (~40MB)
      -> ...

Every chunk keeps the original header row, so each part file is independently
a valid, readable CSV.

TWO WAYS TO RUN IT
------------------

1. REGENERATE (default) - wipes and rebuilds all chunks of a source file from
   scratch on every run. Used for data/monthly -> data/monthly_chunks:

    python src/export_chunks.py --src data/monthly --out data/monthly_chunks --max-mb 40

   --src can be a single .csv file OR a directory (all *.csv files in it are
   chunked).

2. APPEND (--append) - used for data/latest_images_chunks, the files a Power BI
   report reads. Existing chunk files are treated as a permanent ledger:

    python src/export_chunks.py --src data/latest_images.csv \\
        --out data/latest_images_chunks --max-mb 40 --append

   - Rows already in a chunk STAY in that same chunk, at the same position.
     Nothing is reshuffled between files, so a late-uploaded image never
     shifts rows from one file to the next.
   - Rows in the source that are not in any chunk yet (matched by image_id)
     are APPENDED to the newest chunk until it reaches the size cap, then a
     new numbered chunk is started (latest_images_11.csv, ...).
   - Rows that are in a chunk but missing from the source are KEPT. (If the
     source is ever rebuilt partially, e.g. after a cache loss, the published
     data does not shrink.)
   - The only edit made to an existing row is its segment_km value, and only
     when the freshly computed value differs (a late image can land in the
     middle of an already-stored sequence, or the GPS outlier filter can zero
     out a bogus jump). Only chunk files that actually contain such a row are
     rewritten. Use --no-patch to disable this and never touch existing rows.
   - STRUCTURE GUARD: if the header of the source and of any existing chunk
     are not identical, the script aborts BEFORE changing anything, so the
     column layout the Power BI report depends on can never silently change.
   - If no chunks exist yet, it falls back to a normal full split.
"""

import argparse
import csv
import glob
import io
import os
import re
import sys

ID_COL = "image_id"
KM_COL = "segment_km"
KM_TOLERANCE = 0.00015  # km values are written with 4 decimals; ignore rounding noise


# ---------------------------------------------------------------------------
# Regenerate mode (unchanged behaviour)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Append mode
# ---------------------------------------------------------------------------

def list_chunks(out_dir, base_name):
    """Existing chunk files for base_name, sorted NUMERICALLY (…_9 before …_10)."""
    pat = re.compile(rf"^{re.escape(base_name)}_(\d+)\.csv$")
    found = []
    if os.path.isdir(out_dir):
        for fn in os.listdir(out_dir):
            m = pat.match(fn)
            if m:
                found.append((int(m.group(1)), os.path.join(out_dir, fn)))
    found.sort()
    return found  # list of (number, path)


def _read_header(path):
    with open(path, "r", newline="") as f:
        return next(csv.reader(f), None)


class _RowSerializer:
    """Serialise a row exactly the way csv.writer would write it to a file."""

    def __init__(self):
        self._buf = io.StringIO(newline="")
        self._writer = csv.writer(self._buf)

    def __call__(self, row):
        self._buf.seek(0)
        self._buf.truncate(0)
        self._writer.writerow(row)
        return self._buf.getvalue()


def _km_differs(old, new):
    try:
        return abs(float(old) - float(new)) > KM_TOLERANCE
    except (TypeError, ValueError):
        return old != new


def append_to_chunks(src_path, out_dir, max_bytes, patch=True):
    base_name = os.path.splitext(os.path.basename(src_path))[0]
    chunks = list_chunks(out_dir, base_name)

    if not chunks:
        print(f"  No existing chunks for '{base_name}' in {out_dir} - doing a full split.",
              file=sys.stderr)
        return split_one_csv(src_path, out_dir, max_bytes)

    # ---- structure guard (nothing is modified until all headers match) -----
    with open(src_path, "r", newline="") as f:
        src_header = next(csv.reader(f), None)
    if not src_header:
        sys.exit(f"ERROR: {src_path} is empty - refusing to touch existing chunks.")
    if ID_COL not in src_header or KM_COL not in src_header:
        sys.exit(f"ERROR: {src_path} header lacks '{ID_COL}' / '{KM_COL}': {src_header}")
    for _, path in chunks:
        header = _read_header(path)
        if header != src_header:
            sys.exit(
                "ERROR: header mismatch - refusing to modify any chunk so the Power BI "
                f"structure cannot change.\n  source : {src_header}\n  {os.path.basename(path)}: {header}")
    id_idx = src_header.index(ID_COL)
    km_idx = src_header.index(KM_COL)

    # ---- pass 1: source id -> segment_km ------------------------------------
    src_km = {}
    with open(src_path, "r", newline="") as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if len(row) > km_idx:
                src_km[row[id_idx]] = row[km_idx]

    # ---- pass 2: walk existing chunks, collect ids, patch segment_km --------
    existing_ids = set()
    patched_rows = 0
    rewritten = []
    for _, path in chunks:
        with open(path, "r", newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            rows = list(reader)

        changed = False
        for row in rows:
            rid = row[id_idx]
            existing_ids.add(rid)
            if patch:
                new_km = src_km.get(rid)
                if new_km is not None and new_km != row[km_idx] and _km_differs(row[km_idx], new_km):
                    row[km_idx] = new_km
                    patched_rows += 1
                    changed = True

        if changed:
            tmp = path + ".tmp"
            with open(tmp, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(rows)
            os.replace(tmp, path)
            rewritten.append(os.path.basename(path))
        del rows

    kept_only_in_chunks = sum(1 for rid in existing_ids if rid not in src_km)

    # ---- pass 3: append rows that are in the source but in no chunk yet -----
    serialize = _RowSerializer()
    last_n, last_path = chunks[-1]

    # make sure the newest chunk ends with a line break before appending
    with open(last_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() > 0:
            f.seek(-1, os.SEEK_END)
            needs_newline = f.read(1) not in (b"\n", b"\r")
        else:
            needs_newline = False

    out_fh = open(last_path, "a", newline="")
    if needs_newline:
        out_fh.write("\r\n")
    current_size = os.path.getsize(last_path) + (2 if needs_newline else 0)
    appended = 0
    new_parts = []

    with open(src_path, "r", newline="") as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            rid = row[id_idx]
            if rid in existing_ids:
                continue
            existing_ids.add(rid)  # also guards against duplicates inside the source

            line = serialize(row)
            size = len(line.encode("utf-8"))
            if current_size + size > max_bytes and current_size > 0:
                out_fh.close()
                last_n += 1
                last_path = os.path.join(out_dir, f"{base_name}_{last_n}.csv")
                out_fh = open(last_path, "w", newline="")
                header_line = serialize(src_header)
                out_fh.write(header_line)
                current_size = len(header_line.encode("utf-8"))
                new_parts.append(os.path.basename(last_path))
            out_fh.write(line)
            current_size += size
            appended += 1
    out_fh.close()

    total_rows = len(existing_ids)
    print(f"  {os.path.basename(src_path)} -> {out_dir} (append mode)", file=sys.stderr)
    print(f"    rows already in chunks (left in place): {total_rows - appended:,}", file=sys.stderr)
    print(f"    new rows appended                      : {appended:,}", file=sys.stderr)
    print(f"    segment_km values corrected in place   : {patched_rows:,}"
          f"{' (' + ', '.join(rewritten) + ')' if rewritten else ''}", file=sys.stderr)
    print(f"    rows kept that are not in the source   : {kept_only_in_chunks:,}", file=sys.stderr)
    if new_parts:
        print(f"    NEW chunk file(s) started              : {', '.join(new_parts)}", file=sys.stderr)
    return [p for _, p in list_chunks(out_dir, base_name)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="A .csv file, or a directory containing .csv files")
    ap.add_argument("--out", required=True, help="Directory to write numbered chunk files into")
    ap.add_argument("--max-mb", type=float, default=40.0,
                     help="Max size per chunk in MB (default 40, safely under GitHub's 50MB warning / 100MB hard limit)")
    ap.add_argument("--append", action="store_true",
                     help="Keep existing chunk files as a ledger: append only new rows to the newest chunk "
                          "instead of regenerating everything (requires --src to be a single .csv file)")
    ap.add_argument("--no-patch", action="store_true",
                     help="With --append: never edit existing rows (skip segment_km corrections)")
    args = ap.parse_args()

    max_bytes = int(args.max_mb * 1024 * 1024)

    if args.append:
        if not os.path.isfile(args.src):
            sys.exit("ERROR: --append needs --src to be a single .csv file")
        print(f"Appending new rows from {args.src} into {args.out} (max {args.max_mb}MB/part)...",
              file=sys.stderr)
        append_to_chunks(args.src, args.out, max_bytes, patch=not args.no_patch)
        return

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
