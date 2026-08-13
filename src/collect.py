#!/usr/bin/env python3
"""
Mapillary Organization Imagery Collector
=========================================

Pulls images contributed to a given Mapillary organization, groups them into
sequences, resolves each image's country (offline reverse geocoding), and
writes aggregated JSON (+ CSV) that a static dashboard can render.

WHY THIS IS MORE COMPLEX THAN "JUST CALL THE API"
---------------------------------------------------
Two real, confirmed problems with Mapillary's /images endpoint make a naive
fetch silently incomplete:

1. Cursor pagination (the `paging.next` field) is NOT reliably returned when
   filtering by organization_id alone. A "full" page (== page_limit records)
   commonly comes back with no cursor at all - anything past record #500 for
   that query is just gone, with no error, no indication.

2. Combining organization_id with creator_username in the same request is
   REJECTED outright by the API ("Incompatible filters: creator_username,
   organization_id", confirmed by direct testing) - so we can't ask for "just
   this org, just this user" in one call, which would otherwise have been the
   easy fix for (1).

THE APPROACH
------------
For each calendar month:
  Phase 1 (discovery): query organization_id alone, bisecting the time
    window whenever a response looks capped (== page_limit, no cursor).
    This finds every user who contributed *something* that month, even
    though exact per-window counts here can't be trusted if truly dense.
  Phase 2 (authoritative): for each user discovered, query creator_username
    ALONE (no organization_id - that combination is rejected) across the
    full month. creator_username-only queries paginate reliably via cursor,
    so this is complete regardless of density. Results are then filtered
    client-side to just this organization, using the organization_id field
    Mapillary returns on each image record.
Phase 2's output supersedes phase 1 (merged by image id), so phase 1 only
ever under-counts, never over-counts or misses a contributor entirely.

MONTHLY CHUNKING
-----------------
Processing one month at a time (rather than the whole history every run)
keeps each run's bisection recursion shallow and fast, and lets us treat
past months as immutable once finished:

- Every month strictly before the current month is fetched ONCE, written to
  data/monthly/YYYY-MM.jsonl + YYYY-MM.csv, and marked complete with a
  YYYY-MM.done marker. Once marked done, it is never re-fetched.
- The current (in-progress) month is re-fetched in full on every run, since
  new images keep arriving all month. Once the calendar rolls into the next
  month, this month gets one final fetch, is written out, and marked done.
- data/latest.json / data/latest_images.csv are rebuilt every run from ALL
  monthly files combined (cached past months + freshly fetched current
  month), so the dashboard always reflects the full history since
  MAPILLARY_START_MONTH.

Trade-off: an image captured in a past month but uploaded to Mapillary very
late (after that month was already closed out) will not be picked up unless
that month's .done marker is deleted to force a one-time re-fetch. This
matches the explicit "close out each month once we move on" request; delete
data/monthly/<month>.done any time you want to force a recheck.

Required environment variables:
    MAPILLARY_TOKEN   - Mapillary API access token (client token, "MLY|...")
    MAPILLARY_ORG_ID  - Organization ID to collect imagery for

Optional environment variables:
    MAPILLARY_START_MONTH  - "YYYY-MM", first month to backfill. Default: 2026-01
    MAPILLARY_PAGE_LIMIT    - page size for API pagination (default 500)
    MAPILLARY_DISCOVERY_MIN_SPLIT_HOURS - floor for phase-1 discovery time
                                bisection, in hours (default 3 - coarser is
                                fine here since phase 1 only needs to find
                                who contributed, not exact counts)
    MAPILLARY_MIN_SPLIT_HOURS - floor for phase-2 per-user bisection fallback,
                                in hours (default 1 - phase 2 relies on real
                                cursor pagination and should rarely need this)

Output:
    data/monthly/YYYY-MM.jsonl  - raw per-month image records (persisted, cached)
    data/monthly/YYYY-MM.csv    - flat per-month image table
    data/monthly/YYYY-MM.done   - marker: this month is closed, never re-fetched
    data/latest.json            - current full cumulative snapshot (used by dashboard)
    data/latest_images.csv      - flat per-image table of the full cumulative dataset
    data/history/<date>.json    - dated cumulative snapshot, one per day, for trend history
"""

import os
import sys
import json
import csv
import time
import calendar
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
START_MONTH = os.environ.get("MAPILLARY_START_MONTH", "2026-01")  # YYYY-MM
DISCOVERY_MIN_SPLIT = timedelta(hours=float(os.environ.get("MAPILLARY_DISCOVERY_MIN_SPLIT_HOURS", "3")))
MIN_SPLIT = timedelta(hours=float(os.environ.get("MAPILLARY_MIN_SPLIT_HOURS", "1")))

API_ROOT = "https://graph.mapillary.com"
FIELDS = "id,captured_at,creator,sequence,organization_id,geometry"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
MONTHLY_DIR = os.path.join(DATA_DIR, "monthly")
HISTORY_DIR = os.path.join(DATA_DIR, "history")


# ---------------------------------------------------------------------------
# Low-level HTTP / pagination
# ---------------------------------------------------------------------------

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


def _fetch_window(headers, start_dt, end_dt, page_limit, query_params, min_split, depth=0):
    """Fetch all images captured in [start_dt, end_dt) matching query_params
    (e.g. {"organization_id": ...} OR {"creator_username": ...} - never both,
    the API rejects that combination). Recursively bisects the window if it
    looks like results were capped rather than pagination-complete."""
    params = {
        "fields": FIELDS,
        "limit": page_limit,
        "start_captured_at": _dt_to_api(start_dt),
        "end_captured_at": _dt_to_api(end_dt - timedelta(seconds=1)),
    }
    params.update(query_params)

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
        left = _fetch_window(headers, start_dt, mid, page_limit, query_params, min_split, depth + 1)
        right = _fetch_window(headers, mid, end_dt, page_limit, query_params, min_split, depth + 1)
        return left + right

    if hit_cap_without_cursor:
        print(f"    WARNING: window {start_dt}..{end_dt} (query={query_params}) still at page "
              f"cap at minimum granularity ({min_split}) - results may be incomplete here.",
              file=sys.stderr)

    return records


# ---------------------------------------------------------------------------
# Month-at-a-time fetch: discovery sweep + authoritative per-user fetch
# ---------------------------------------------------------------------------

def month_bounds(year, month):
    """Return (start_dt, end_dt) for a calendar month, end exclusive, both UTC."""
    start_dt = datetime(year, month, 1, tzinfo=timezone.utc)
    last_day = calendar.monthrange(year, month)[1]
    end_dt = datetime(year, month, last_day, tzinfo=timezone.utc) + timedelta(days=1)
    return start_dt, end_dt


def fetch_month(headers, org_id, year, month, page_limit, cap_end_dt=None):
    """Fetch every image belonging to org_id captured within the given month
    (capped at cap_end_dt if provided, for the in-progress current month)."""
    start_dt, end_dt = month_bounds(year, month)
    if cap_end_dt is not None:
        end_dt = min(end_dt, cap_end_dt)
    if start_dt >= end_dt:
        return []

    label = f"{year:04d}-{month:02d}"
    print(f"  [{label}] Phase 1/2: org-wide discovery sweep "
          f"({start_dt.date()} to {(end_dt - timedelta(seconds=1)).date()})...", file=sys.stderr)
    discovery_records = _fetch_window(
        headers, start_dt, end_dt, page_limit,
        query_params={"organization_id": org_id}, min_split=DISCOVERY_MIN_SPLIT,
    )

    by_id = {r["id"]: r for r in discovery_records}
    usernames = sorted({
        r["creator"]["username"] for r in discovery_records
        if r.get("creator", {}).get("username")
    })
    print(f"  [{label}] Phase 1 complete: {len(discovery_records)} images seen, "
          f"{len(usernames)} contributor(s): {usernames}", file=sys.stderr)

    print(f"  [{label}] Phase 2/2: authoritative per-user fetch...", file=sys.stderr)
    org_id_str = str(org_id)
    for username in usernames:
        # NOTE: organization_id and creator_username cannot be combined in one
        # query (the API rejects it) - so we fetch this user's FULL history for
        # the month (no org filter) and then keep only records whose returned
        # organization_id matches. creator_username-only queries paginate
        # reliably, which is the whole point of this second pass.
        user_records = _fetch_window(
            headers, start_dt, end_dt, page_limit,
            query_params={"creator_username": username}, min_split=MIN_SPLIT,
        )
        org_matched = [r for r in user_records if str(r.get("organization_id")) == org_id_str]
        new_count = sum(1 for r in org_matched if r["id"] not in by_id)
        for r in org_matched:
            by_id[r["id"]] = r
        print(f"    '{username}': {len(user_records)} total images, "
              f"{len(org_matched)} for this org ({new_count} not seen in discovery phase)",
              file=sys.stderr)

    all_records = list(by_id.values())
    print(f"  [{label}] Month total: {len(all_records)} images", file=sys.stderr)
    return all_records


# ---------------------------------------------------------------------------
# Monthly file cache (closed months are fetched once and never touched again)
# ---------------------------------------------------------------------------

def _month_paths(year, month):
    label = f"{year:04d}-{month:02d}"
    return {
        "jsonl": os.path.join(MONTHLY_DIR, f"{label}.jsonl"),
        "csv": os.path.join(MONTHLY_DIR, f"{label}.csv"),
        "done": os.path.join(MONTHLY_DIR, f"{label}.done"),
        "label": label,
    }


def load_jsonl(path):
    records = []
    if os.path.exists(path):
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def save_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp_path, path)


def iter_months(start_year, start_month, end_year, end_month):
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


# ---------------------------------------------------------------------------
# Country resolution (offline reverse geocoding)
# ---------------------------------------------------------------------------

def resolve_countries(records):
    """Attach a 'country' field to each record using offline reverse geocoding.
    Skips records that already have _country (e.g. loaded from a cached
    closed-month file), so this only does work on freshly fetched records."""
    todo = [r for r in records if "_country" not in r]
    if not todo:
        return records

    if rg is None:
        print("WARNING: reverse_geocoder not installed; country will be 'Unknown' for all records.",
              file=sys.stderr)
        for r in todo:
            r["_country"] = "Unknown"
        return records

    coords = []
    idx_map = []
    for i, r in enumerate(todo):
        geom = r.get("geometry")
        if geom and geom.get("coordinates"):
            lon, lat = geom["coordinates"][0], geom["coordinates"][1]
            coords.append((lat, lon))
            idx_map.append(i)

    if coords:
        results = rg.search(coords)  # offline, batched, fast
        for pos, i in enumerate(idx_map):
            todo[i]["_country"] = results[pos].get("cc", "Unknown")

    for r in todo:
        r.setdefault("_country", "Unknown")

    return records


ISO2_TO_NAME = {}
try:
    import pycountry
    for c in pycountry.countries:
        ISO2_TO_NAME[c.alpha_2] = c.name
except ImportError:
    pycountry = None


def country_name(cc):
    if cc == "Unknown" or not cc:
        return "Unknown"
    return ISO2_TO_NAME.get(cc, cc)


# ---------------------------------------------------------------------------
# Aggregation + output
# ---------------------------------------------------------------------------

def build_aggregates(records, org_id):
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
        {"country": c, "images": v["images"], "sequences": len(v["sequences"]), "users": len(v["users"])}
        for c, v in sorted(by_country.items(), key=lambda kv: -kv[1]["images"])
    ]
    users_out = [
        {
            "username": u, "user_id": users_seen.get(u, "unknown"),
            "images": v["images"], "sequences": len(v["sequences"]),
            "countries": sorted(v["countries"]),
        }
        for u, v in sorted(by_user.items(), key=lambda kv: -kv[1]["images"])
    ]
    daily_out = [
        {"date": d, "images": v["images"], "sequences": len(v["sequences"])}
        for d, v in sorted(by_day.items())
    ]

    return {
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


def write_csv(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["image_id", "sequence_id", "user_id", "username",
                          "captured_at_utc", "country", "lon", "lat"])
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
                r.get("id"), r.get("sequence"), creator.get("id"), creator.get("username"),
                captured_iso, country_name(r.get("_country", "Unknown")), coords[0], coords[1],
            ])


def write_outputs(all_records, summary):
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
    write_csv(csv_path, all_records)
    print(f"Wrote {csv_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not MAPILLARY_TOKEN:
        sys.exit("ERROR: MAPILLARY_TOKEN is not set.")
    if not ORG_ID:
        sys.exit("ERROR: MAPILLARY_ORG_ID is not set.")

    headers = {"Authorization": f"OAuth {MAPILLARY_TOKEN}"}

    start_year, start_month = (int(x) for x in START_MONTH.split("-"))
    now = datetime.now(timezone.utc)
    current_year, current_month = now.year, now.month

    print(f"Collecting images for organization {ORG_ID}, "
          f"months {start_year:04d}-{start_month:02d} through {current_year:04d}-{current_month:02d}",
          file=sys.stderr)

    all_records = []
    for year, month in iter_months(start_year, start_month, current_year, current_month):
        paths = _month_paths(year, month)
        is_current = (year, month) == (current_year, current_month)

        if not is_current and os.path.exists(paths["done"]):
            print(f"[{paths['label']}] closed month, using cached file.", file=sys.stderr)
            records = load_jsonl(paths["jsonl"])
        else:
            cap_end_dt = now if is_current else None
            records = fetch_month(headers, ORG_ID, year, month, PAGE_LIMIT, cap_end_dt=cap_end_dt)
            records = resolve_countries(records)
            save_jsonl(paths["jsonl"], records)
            write_csv(paths["csv"], records)
            if not is_current:
                # This month is now in the past and fully fetched - close it out
                # so it's never re-fetched again.
                with open(paths["done"], "w") as f:
                    f.write(datetime.now(timezone.utc).isoformat())
                print(f"[{paths['label']}] closed out ({len(records)} images).", file=sys.stderr)
            else:
                print(f"[{paths['label']}] current month, will re-fetch fully next run.",
                      file=sys.stderr)

        all_records.extend(records)

    summary = build_aggregates(all_records, ORG_ID)
    write_outputs(all_records, summary)

    print(
        f"Done. Cumulative since {START_MONTH}: "
        f"{summary['total_images']} images, {summary['total_sequences']} sequences, "
        f"{summary['total_users']} users, {summary['total_countries']} countries.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
