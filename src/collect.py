#!/usr/bin/env python3
"""
Mapillary Organization Imagery Collector
=========================================

Pulls all images contributed to a given Mapillary organization, groups them
into sequences, resolves each image's country (offline reverse geocoding),
and writes aggregated JSON (+ CSV) that a static dashboard can render.

Required environment variables:
    MAPILLARY_TOKEN   - Mapillary API access token (client token, "MLY|...")
    MAPILLARY_ORG_ID  - Organization ID to collect imagery for

Optional environment variables:
    MAPILLARY_START_DATE  - ISO date (YYYY-MM-DD), only images captured on/after this date
    MAPILLARY_END_DATE    - ISO date (YYYY-MM-DD), only images captured on/before this date
    MAPILLARY_PAGE_LIMIT  - page size for API pagination (default 500, max Mapillary allows)

Output:
    data/latest.json          - current full snapshot (used by dashboard)
    data/latest_images.csv     - flat per-image table
    data/history/<date>.json  - dated snapshot appended each run, for trend history
"""

import os
import sys
import json
import csv
import time
from datetime import datetime, timezone
from collections import defaultdict

import requests

try:
    import reverse_geocoder as rg
except ImportError:
    rg = None

MAPILLARY_TOKEN = os.environ.get("MAPILLARY_TOKEN")
ORG_ID = os.environ.get("MAPILLARY_ORG_ID")
PAGE_LIMIT = int(os.environ.get("MAPILLARY_PAGE_LIMIT", "500"))
START_DATE = os.environ.get("MAPILLARY_START_DATE")  # YYYY-MM-DD
END_DATE = os.environ.get("MAPILLARY_END_DATE")      # YYYY-MM-DD

API_ROOT = "https://graph.mapillary.com"
FIELDS = "id,captured_at,creator,sequence,organization_id,geometry"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
HISTORY_DIR = os.path.join(DATA_DIR, "history")


def _iso_to_ms(date_str, end_of_day=False):
    """Convert a YYYY-MM-DD string to epoch milliseconds for the API filters."""
    if not date_str:
        return None
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(dt.timestamp() * 1000)


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
    start_ms = _iso_to_ms(start_date)
    end_ms = _iso_to_ms(end_date, end_of_day=True)
    if start_ms:
        params["start_captured_at"] = start_ms
    if end_ms:
        params["end_captured_at"] = end_ms

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
    records = fetch_all_images(MAPILLARY_TOKEN, ORG_ID, START_DATE, END_DATE, PAGE_LIMIT)
    print(f"Fetched {len(records)} images total. Resolving countries...", file=sys.stderr)
    records = resolve_countries(records)
    summary = build_aggregates(records, ORG_ID)
    write_outputs(records, summary)
    print(
        f"Done. {summary['total_images']} images, {summary['total_sequences']} sequences, "
        f"{summary['total_users']} users, {summary['total_countries']} countries.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
