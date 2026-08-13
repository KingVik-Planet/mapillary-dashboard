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


def _dt_to_api(dt):
    """Format a UTC datetime as the ISO 8601 'Z' string Mapillary's API expects."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_page(headers, url, params, timeout=60):
    """GET a single page, handling 429 backoff. Returns the parsed JSON payload."""
    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "5"))
            print(f"  Rate limited, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            sys.exit(f"ERROR: Mapillary API returned {resp.status_code}: {resp.text[:500]}")
        return resp.json()


# Mapillary's cursor-based pagination on /images is only reliably provided when the
# query is combined with a creator_username filter (per their docs). Filtered by
# Mapillary's cursor-based pagination on /images is only reliably provided when the
# query is combined with a creator_username filter (per their docs). Filtered by
# organization_id alone, a "full" page (== page_limit records) commonly comes back
# with NO `next` cursor at all - meaning anything past record #page_limit is silently
# dropped. Our earlier fix (bisecting the time range whenever this happens) helps but
# still has a floor: if a MIN_SPLIT-sized window alone contains more than page_limit
# images (very plausible during an active mapping campaign - a car capturing every
# couple of seconds can produce 500+ images in well under an hour), it gets silently
# truncated with no further recourse.
#
# So this now runs in two phases:
#   Phase 1 (discovery): the org-wide, time-bisected sweep as before. This is enough
#     to discover every user who has contributed *something*, even if their exact
#     counts in dense windows are undercounted here.
#   Phase 2 (authoritative per-user fetch): for each user discovered, re-fetch their
#     full history filtered by organization_id + creator_username together, which
#     Mapillary's docs confirm reliably paginates via cursor - no bisection guessing
#     needed, and no density ceiling. This is what actually guarantees completeness.
# Phase 2 output supersedes phase 1 for any image ID appearing in both (they merge on
# id, so nothing gets double-counted).
MIN_SPLIT = timedelta(hours=1)
DISCOVERY_MIN_SPLIT = timedelta(hours=1)


def _fetch_window(headers, org_id, start_dt, end_dt, page_limit, extra_params=None,
                   min_split=MIN_SPLIT, depth=0):
    """Fetch all images captured in [start_dt, end_dt), optionally filtered further by
    extra_params (e.g. {"creator_username": "..."}). Recursively bisects the window if
    it looks like results were capped rather than pagination-complete."""
    params = {
        "organization_id": org_id,
        "fields": FIELDS,
        "limit": page_limit,
        "start_captured_at": _dt_to_api(start_dt),
        "end_captured_at": _dt_to_api(end_dt - timedelta(seconds=1)),
    }
    if extra_params:
        params.update(extra_params)

    records = []
    url = f"{API_ROOT}/images"
    first = True
    hit_cap_without_cursor = False

    while url:
        payload = _fetch_page(headers, url, params if first else None)
        first = False
        data = payload.get("data", [])
        records.extend(data)

        next_url = payload.get("paging", {}).get("next")
        if next_url:
            url = next_url
            params = None
            continue

        url = None
        if len(data) >= page_limit:
            hit_cap_without_cursor = True

    if hit_cap_without_cursor and (end_dt - start_dt) > min_split:
        mid = start_dt + (end_dt - start_dt) / 2
        indent = "  " * (depth + 1)
        print(f"{indent}Window {start_dt.date()}..{end_dt.date()} hit the page cap - splitting "
              f"at {mid.isoformat()}", file=sys.stderr)
        left = _fetch_window(headers, org_id, start_dt, mid, page_limit, extra_params, min_split, depth + 1)
        right = _fetch_window(headers, org_id, mid, end_dt, page_limit, extra_params, min_split, depth + 1)
        return left + right

    if hit_cap_without_cursor:
        print(f"  WARNING: window {start_dt}..{end_dt} still at page cap at minimum "
              f"granularity ({min_split}) even with extra_params={extra_params} - "
              f"results may be incomplete here.", file=sys.stderr)

    indent = "  " * (depth + 1) if depth else "  "
    print(f"{indent}{start_dt.date()}..{end_dt.date()}"
          f"{' user='+extra_params['creator_username'] if extra_params and extra_params.get('creator_username') else ''}"
          f": {len(records)} images", file=sys.stderr)
    return records


def fetch_all_images(token, org_id, start_date=None, end_date=None, page_limit=500):
    """Fetch every image for the organization captured within [start_date, end_date]
    (inclusive, YYYY-MM-DD strings), using a two-phase strategy (see comment above)
    to guarantee completeness even for dense/bursty capture windows."""
    if not token:
        sys.exit("ERROR: MAPILLARY_TOKEN is not set.")
    if not org_id:
        sys.exit("ERROR: MAPILLARY_ORG_ID is not set.")

    headers = {"Authorization": f"OAuth {token}"}

    start_dt = (
        datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if start_date else datetime(2010, 1, 1, tzinfo=timezone.utc)
    )
    if end_date:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    else:
        end_dt = datetime.now(timezone.utc) + timedelta(days=1)

    print(f"Fetching images from {start_dt.date()} to {(end_dt - timedelta(days=1)).date()}...",
          file=sys.stderr)

    print("Phase 1/2: org-wide discovery sweep (finds every contributing user)...", file=sys.stderr)
    discovery_records = _fetch_window(headers, org_id, start_dt, end_dt, page_limit,
                                       extra_params=None, min_split=DISCOVERY_MIN_SPLIT)

    by_id = {r["id"]: r for r in discovery_records}

    usernames = sorted({
        r["creator"]["username"] for r in discovery_records
        if r.get("creator", {}).get("username")
    })
    print(f"Phase 1 complete: {len(discovery_records)} images seen, "
          f"{len(usernames)} unique contributors found: {usernames}", file=sys.stderr)

    print("Phase 2/2: authoritative per-user fetch (guaranteed-complete pagination)...",
          file=sys.stderr)
    for username in usernames:
        user_records = _fetch_window(
            headers, org_id, start_dt, end_dt, page_limit,
            extra_params={"creator_username": username}, min_split=MIN_SPLIT,
        )
        new_count = sum(1 for r in user_records if r["id"] not in by_id)
        for r in user_records:
            by_id[r["id"]] = r
        print(f"  '{username}': {len(user_records)} images via per-user pagination "
              f"({new_count} not seen in discovery phase)", file=sys.stderr)

    all_records = list(by_id.values())
    print(f"Combined total after both phases: {len(all_records)} images", file=sys.stderr)
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