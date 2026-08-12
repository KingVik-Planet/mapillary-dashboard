#!/usr/bin/env python3
"""
Mapillary Organization Imagery Collector
=========================================

Pulls images contributed to a given Mapillary organization, groups them into
sequences, resolves each image's country (offline reverse geocoding), and
writes aggregated JSON (+ CSV) that a static dashboard can render.

INCREMENTAL / APPEND BEHAVIOUR
-------------------------------
All images ever collected are kept in a persistent master store
(data/images_master.jsonl), keyed by image id.

- First run ever (master store doesn't exist yet): backfills from
  MAPILLARY_START_DATE, or 2026-01-01 by default, up through today.
- Every later run: only fetches images captured since the newest image
  already in the master store (minus a small overlap window to catch
  late-arriving uploads), then merges them into the master store by id
  (so re-fetched/duplicate images just overwrite in place, nothing doubles
  up). Aggregates are then rebuilt from the FULL accumulated master store,
  so data/latest.json always reflects everything collected since 2026-01-01,
  not just the latest run's slice.

Required environment variables:
    MAPILLARY_TOKEN   - Mapillary API access token (client token, "MLY|...")
    MAPILLARY_ORG_ID  - Organization ID to collect imagery for

Optional environment variables:
    MAPILLARY_START_DATE  - ISO date (YYYY-MM-DD), backfill start, used only
                             on the very first run. Default: 2026-01-01
    MAPILLARY_END_DATE    - ISO date (YYYY-MM-DD), only images captured on/
                             before this date. Default: unset (up to now)
    MAPILLARY_OVERLAP_DAYS - On incremental runs, re-check this many days
                              before the last-seen image to catch late
                              uploads. Default: 2
    MAPILLARY_PAGE_LIMIT   - page size for API pagination (default 500)

Output:
    data/images_master.jsonl  - full accumulated raw dataset, one image per line (persisted)
    data/latest.json          - current full cumulative snapshot (used by dashboard)
    data/latest_images.csv    - flat per-image table of the full cumulative dataset
    data/history/<date>.json  - dated cumulative snapshot, one per day, for trend history
"""

import os
import sys
import json
import csv
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests

try:
    import reverse_geocoder as rg
except ImportError:
    rg = None

MAPILLARY_TOKEN = os.environ.get("MAPILLARY_TOKEN")
ORG_ID = os.environ.get("MAPILLARY_ORG_ID")
PAGE_LIMIT = int(os.environ.get("MAPILLARY_PAGE_LIMIT", "500"))
START_DATE = os.environ.get("MAPILLARY_START_DATE")  # YYYY-MM-DD, first-run backfill only
END_DATE = os.environ.get("MAPILLARY_END_DATE")      # YYYY-MM-DD
OVERLAP_DAYS = int(os.environ.get("MAPILLARY_OVERLAP_DAYS", "2"))

DEFAULT_BACKFILL_START_DATE = "2026-01-01"

API_ROOT = "https://graph.mapillary.com"
FIELDS = "id,captured_at,creator,sequence,organization_id,geometry"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
HISTORY_DIR = os.path.join(DATA_DIR, "history")
MASTER_PATH = os.path.join(DATA_DIR, "images_master.jsonl")


def _iso_to_api_datetime(date_str, end_of_day=False):
    """Convert a YYYY-MM-DD string to the ISO 8601 'Z' format the Mapillary
    API expects for start_captured_at/end_captured_at, e.g. '2022-08-16T16:42:46Z'."""
    if not date_str:
        return None
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_all_images(token, org_id, start_date=None, end_date=None, page_limit=500):
    """Page through /images filtered by organization_id, returning raw records."""
    if not token:
        sys.exit("ERROR: MAPILLARY_TOKEN is not set.")
    if not org_id:
        sys.exit("ERROR: MAPILLARY_ORG_ID is not set.")

    headers = {"Authorization": f"OAuth {token}"}
    params = {
        "organization_id": org_id,
        "fields": FIELDS,
        "limit": page_limit,
    }
    start_iso = _iso_to_api_datetime(start_date)
    end_iso = _iso_to_api_datetime(end_date, end_of_day=True)
    if start_iso:
        params["start_captured_at"] = start_iso
    if end_iso:
        params["end_captured_at"] = end_iso

    url = f"{API_ROOT}/images"
    all_records = []
    page = 0

    while url:
        page += 1
        resp = requests.get(url, headers=headers, params=params if page == 1 else None, timeout=60)
        if resp.status_code == 429:
            # Rate limited - back off and retry
            wait = int(resp.headers.get("Retry-After", "5"))
            print(f"  Rate limited, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            sys.exit(f"ERROR: Mapillary API returned {resp.status_code}: {resp.text[:500]}")

        payload = resp.json()
        data = payload.get("data", [])
        all_records.extend(data)
        print(f"  Page {page}: fetched {len(data)} images (total so far: {len(all_records)})", file=sys.stderr)

        # Cursor-based pagination
        next_url = payload.get("paging", {}).get("next")
        url = next_url
        params = None  # next_url already has all query params embedded

        if not data:
            break

    return all_records


def load_master():
    """Load the persistent master store (all images ever collected), keyed by id."""
    master = {}
    if os.path.exists(MASTER_PATH):
        with open(MASTER_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                master[rec["id"]] = rec
    return master


def save_master(master):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = MASTER_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        for rec in master.values():
            f.write(json.dumps(rec) + "\n")
    os.replace(tmp_path, MASTER_PATH)
    print(f"Master store saved: {len(master)} total images", file=sys.stderr)


def determine_fetch_window(master):
    """
    Decide the [start_date, end_date] to query the API for on this run.

    - Empty master -> full backfill window (MAPILLARY_START_DATE or 2026-01-01) to END_DATE/today.
    - Non-empty master -> from (newest captured_at in master - OVERLAP_DAYS) to END_DATE/today,
      so we only pull what's new (plus a small safety overlap for late uploads).
    """
    if not master:
        start = START_DATE or DEFAULT_BACKFILL_START_DATE
        print(f"No existing data found - backfilling from {start}.", file=sys.stderr)
        return start, END_DATE

    newest_ms = max(r["captured_at"] for r in master.values() if r.get("captured_at"))
    newest_dt = datetime.fromtimestamp(newest_ms / 1000, tz=timezone.utc)
    since_dt = newest_dt - timedelta(days=OVERLAP_DAYS)
    start = since_dt.strftime("%Y-%m-%d")
    print(
        f"Existing data found ({len(master)} images, newest captured {newest_dt.date()}). "
        f"Fetching incrementally from {start} (overlap={OVERLAP_DAYS}d).",
        file=sys.stderr,
    )
    return start, END_DATE


def resolve_countries(records):
    """Attach a 'country' field to each record using offline reverse geocoding."""
    if rg is None:
        print("WARNING: reverse_geocoder not installed; country will be 'Unknown' for all records.",
              file=sys.stderr)
        for r in records:
            r["_country"] = "Unknown"
        return records

    coords = []
    idx_map = []
    for i, r in enumerate(records):
        geom = r.get("geometry")
        if geom and geom.get("coordinates"):
            lon, lat = geom["coordinates"][0], geom["coordinates"][1]
            coords.append((lat, lon))
            idx_map.append(i)

    if coords:
        results = rg.search(coords)  # offline, batched, fast
        for pos, i in enumerate(idx_map):
            records[i]["_country"] = results[pos].get("cc", "Unknown")  # ISO2 country code

    for r in records:
        r.setdefault("_country", "Unknown")

    return records


ISO2_TO_NAME = {}  # populated lazily below if pycountry available
try:
    import pycountry
    for c in pycountry.countries:
        ISO2_TO_NAME[c.alpha_2] = c.name
except ImportError:
    pycountry = None


def country_name(cc):
    if cc == "Unknown":
        return "Unknown"
    return ISO2_TO_NAME.get(cc, cc)


def build_aggregates(records, org_id):
    """Produce the summary structures the dashboard consumes."""
    by_country = defaultdict(lambda: {"images": 0, "sequences": set(), "users": set()})
    by_user = defaultdict(lambda: {"images": 0, "sequences": set(), "countries": set()})
    by_day = defaultdict(lambda: {"images": 0, "sequences": set()})
    sequences_seen = set()
    users_seen = {}

    for r in records:
        cc = r.get("_country", "Unknown")
        cname = country_name(cc)
        seq_id = r.get("sequence")
        creator = r.get("creator") or {}
        user_id = creator.get("id", "unknown")
        username = creator.get("username", user_id)
        captured_at_ms = r.get("captured_at")
        day = None
        if captured_at_ms:
            day = datetime.fromtimestamp(captured_at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        by_country[cname]["images"] += 1
        if seq_id:
            by_country[cname]["sequences"].add(seq_id)
            sequences_seen.add(seq_id)
        by_country[cname]["users"].add(username)

        by_user[username]["images"] += 1
        if seq_id:
            by_user[username]["sequences"].add(seq_id)
        by_user[username]["countries"].add(cname)
        users_seen[username] = user_id

        if day:
            by_day[day]["images"] += 1
            if seq_id:
                by_day[day]["sequences"].add(seq_id)

    countries_out = [
        {
            "country": c,
            "images": v["images"],
            "sequences": len(v["sequences"]),
            "users": len(v["users"]),
        }
        for c, v in sorted(by_country.items(), key=lambda kv: -kv[1]["images"])
    ]

    users_out = [
        {
            "username": u,
            "user_id": users_seen.get(u, "unknown"),
            "images": v["images"],
            "sequences": len(v["sequences"]),
            "countries": sorted(v["countries"]),
        }
        for u, v in sorted(by_user.items(), key=lambda kv: -kv[1]["images"])
    ]

    daily_out = [
        {"date": d, "images": v["images"], "sequences": len(v["sequences"])}
        for d, v in sorted(by_day.items())
    ]

    summary = {
        "organization_id": org_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_images": len(records),
        "total_sequences": len(sequences_seen),
        "total_users": len(users_seen),
        "total_countries": len([c for c in by_country if c != "Unknown"]),
        "by_country": countries_out,
        "by_user": users_out,
        "by_day": daily_out,
    }
    return summary


def write_outputs(records, summary):
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)

    latest_path = os.path.join(DATA_DIR, "latest.json")
    with open(latest_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Wrote {latest_path}", file=sys.stderr)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    history_path = os.path.join(HISTORY_DIR, f"{today}.json")
    with open(history_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Wrote {history_path}", file=sys.stderr)

    csv_path = os.path.join(DATA_DIR, "latest_images.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["image_id", "sequence_id", "user_id", "username", "captured_at_utc", "country", "lon", "lat"])
        for r in records:
            geom = r.get("geometry") or {}
            coords = geom.get("coordinates", [None, None])
            creator = r.get("creator") or {}
            captured_at_ms = r.get("captured_at")
            captured_iso = (
                datetime.fromtimestamp(captured_at_ms / 1000, tz=timezone.utc).isoformat()
                if captured_at_ms else ""
            )
            writer.writerow([
                r.get("id"),
                r.get("sequence"),
                creator.get("id"),
                creator.get("username"),
                captured_iso,
                country_name(r.get("_country", "Unknown")),
                coords[0],
                coords[1],
            ])
    print(f"Wrote {csv_path}", file=sys.stderr)


def main():
    print(f"Collecting images for organization {ORG_ID}...", file=sys.stderr)

    master = load_master()
    fetch_start, fetch_end = determine_fetch_window(master)

    new_records = fetch_all_images(MAPILLARY_TOKEN, ORG_ID, fetch_start, fetch_end, PAGE_LIMIT)
    print(f"Fetched {len(new_records)} images in this run's window. Resolving countries...", file=sys.stderr)
    new_records = resolve_countries(new_records)

    # Merge into master store by id - re-fetched images (the overlap window) just
    # overwrite their existing entry in place, so nothing gets double-counted.
    for r in new_records:
        master[r["id"]] = r
    save_master(master)

    all_records = list(master.values())
    summary = build_aggregates(all_records, ORG_ID)
    write_outputs(all_records, summary)

    print(
        f"Done. Cumulative since {START_DATE or DEFAULT_BACKFILL_START_DATE}: "
        f"{summary['total_images']} images, {summary['total_sequences']} sequences, "
        f"{summary['total_users']} users, {summary['total_countries']} countries "
        f"({len(new_records)} images added/updated this run).",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
