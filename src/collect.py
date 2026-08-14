#!/usr/bin/env python3
"""
Mapillary Organization Imagery Collector
=========================================

Pulls images contributed to a given Mapillary organization, groups them into
sequences, resolves each image's country (offline reverse geocoding), and
writes aggregated JSON (+ CSV) that a static dashboard can render.

WHY THIS ISN'T A SINGLE SIMPLE API CALL
-----------------------------------------
Mapillary's /images endpoint has a confirmed, undocumented gotcha: cursor
pagination (the `paging.next` field) is NOT reliably returned when filtering
by organization_id alone. A "full" page (== page_limit records) commonly
comes back with no cursor at all - anything past record #500 for that query
is silently dropped, no error, no indication.

An earlier version of this script tried to work around that by ALSO
fetching each contributing user's history filtered by creator_username
(which does paginate reliably) and merging the results in. That approach
turned out to be a dead end for two reasons, discovered by actually running
it against this organization's real data:
  1. It fetches each user's ENTIRE cross-organization image history for the
     time window, not just their contributions to this org - some
     contributors here have 50,000-95,000+ images, making that pass
     enormously slow (this is what caused a run to take 4+ hours and
     eventually fail).
  2. The client-side filter meant to keep only that user's images belonging
     to THIS org never matched anything, so that expensive pass was
     silently contributing zero records anyway.

So this version drops that approach entirely and relies solely on
organization_id-filtered time-window bisection: whenever a window's results
look capped (== page_limit, no cursor), the window is split in half and
each half is re-fetched, recursively, down to a fine time granularity
(MAPILLARY_MIN_SPLIT_MINUTES, default 5 minutes). This is bounded in cost
(proportional to how many genuinely-dense pockets of time exist), unlike
the per-user approach which was proportional to each user's total lifetime
Mapillary output.

MONTHLY CHUNKING + ONE-MONTH-PER-RUN PACING
----------------------------------------------
To keep any single run fast and avoid the 4-hour-run problem entirely, this
script processes at most ONE not-yet-finished period per run:

- If there are past (fully-elapsed) months that haven't been closed out yet,
  it fetches the OLDEST one of those, writes it to
  data/monthly/YYYY-MM.jsonl + .csv, and marks it done with a
  data/monthly/YYYY-MM.done file. Nothing else is fetched this run.
- Once every past month is closed out, each run instead re-fetches the
  CURRENT (in-progress) month in full, since it's still accumulating new
  images. This happens on every run until the calendar rolls into the next
  month, at which point that month gets one final fetch, is closed out, and
  a new "current month" begins.

In practice: today's run backfills January and stops. Tomorrow's run
backfills February and stops. ... Once it catches up to the current month,
every day's run just refreshes that one month, until the month ends and a
new one starts piling up the same way.

data/latest.json / data/latest_images.csv (what the dashboard reads) are
rebuilt every run from ALL monthly files currently on disk - closed months
plus whatever's cached for the current month - so the dashboard fills in
progressively as the backfill catches up, rather than being empty until
everything is done.

Required environment variables:
    MAPILLARY_TOKEN   - Mapillary API access token (client token, "MLY|...")
    MAPILLARY_ORG_ID  - Organization ID to collect imagery for

Optional environment variables:
    MAPILLARY_START_MONTH  - "YYYY-MM", first month to backfill. Default: 2026-01
    MAPILLARY_PAGE_LIMIT    - page size for API pagination (default 500)
    MAPILLARY_MIN_SPLIT_MINUTES - floor for time-window bisection, in minutes
                                (default 5 - fine enough to handle dense
                                mapping campaigns without exploding API
                                call counts)

Output:
    data/monthly/YYYY-MM.jsonl  - raw per-month image records (persisted, cached)
    data/monthly/YYYY-MM.csv    - flat per-month image table
    data/monthly/YYYY-MM.done   - marker: this month is closed, won't be re-fetched
    data/latest.json            - current cumulative snapshot across all months fetched so far
    data/latest_images.csv      - flat per-image table of everything fetched so far
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
MIN_SPLIT = timedelta(minutes=float(os.environ.get("MAPILLARY_MIN_SPLIT_MINUTES", "5")))

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
            print(f"    Rate limited, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            sys.exit(f"ERROR: Mapillary API returned {resp.status_code}: {resp.text[:500]}")
        return resp.json()


def _fetch_window(headers, org_id, start_dt, end_dt, page_limit, min_split, depth=0):
    """Fetch all images belonging to org_id captured in [start_dt, end_dt).
    Recursively bisects the window whenever results look capped (== page_limit,
    no pagination cursor) rather than trusting that's the true, complete count."""
    params = {
        "organization_id": org_id,
        "fields": FIELDS,
        "limit": page_limit,
        "start_captured_at": _dt_to_api(start_dt),
        "end_captured_at": _dt_to_api(end_dt - timedelta(seconds=1)),
    }

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
        left = _fetch_window(headers, org_id, start_dt, mid, page_limit, min_split, depth + 1)
        right = _fetch_window(headers, org_id, mid, end_dt, page_limit, min_split, depth + 1)
        return left + right

    if hit_cap_without_cursor:
        print(f"    WARNING: window {start_dt}..{end_dt} still at page cap at minimum "
              f"granularity ({min_split}) - results may be incomplete here. Consider "
              f"lowering MAPILLARY_MIN_SPLIT_MINUTES if this org is extremely high-volume.",
              file=sys.stderr)

    return records


# ---------------------------------------------------------------------------
# Month-at-a-time fetch
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
    print(f"  [{label}] Fetching {start_dt.date()} to {(end_dt - timedelta(seconds=1)).date()}...",
          file=sys.stderr)
    records = _fetch_window(headers, org_id, start_dt, end_dt, page_limit, MIN_SPLIT)

    by_id = {r["id"]: r for r in records}
    usernames = sorted({
        r["creator"]["username"] for r in records
        if r.get("creator", {}).get("username")
    })
    print(f"  [{label}] {len(by_id)} images from {len(usernames)} contributor(s): {usernames}",
          file=sys.stderr)
    return list(by_id.values())


# ---------------------------------------------------------------------------
# Monthly file cache
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
    todo = [r for r in records if "_country" not in r]
    if not todo:
        return records

    if rg is None:
        print("WARNING: reverse_geocoder not installed; country will be 'Unknown' for all records.",
              file=sys.stderr)
        for r in todo:
            r["_country"] = "Unknown"
        return records

    coords, idx_map = [], []
    for i, r in enumerate(todo):
        geom = r.get("geometry")
        if geom and geom.get("coordinates"):
            lon, lat = geom["coordinates"][0], geom["coordinates"][1]
            coords.append((lat, lon))
            idx_map.append(i)

    if coords:
        results = rg.search(coords)
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

def build_aggregates(records, org_id, months_fetched, months_pending):
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
        "months_fetched": months_fetched,
        "months_pending_backfill": months_pending,
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

    all_months = list(iter_months(start_year, start_month, current_year, current_month))
    past_months = [ym for ym in all_months if ym != (current_year, current_month)]
    unclosed_past = [ym for ym in past_months if not os.path.exists(_month_paths(*ym)["done"])]

    if unclosed_past:
        target = unclosed_past[0]
        print(f"Backfilling one month this run: {target[0]:04d}-{target[1]:02d} "
              f"({len(unclosed_past) - 1} more past month(s) still pending after this).",
              file=sys.stderr)
    else:
        target = (current_year, current_month)
        print(f"All past months caught up - refreshing current month "
              f"{target[0]:04d}-{target[1]:02d}.", file=sys.stderr)

    months_fetched_this_run = []
    for year, month in all_months:
        paths = _month_paths(year, month)
        is_current = (year, month) == (current_year, current_month)

        if (year, month) == target:
            cap_end_dt = now if is_current else None
            records = fetch_month(headers, ORG_ID, year, month, PAGE_LIMIT, cap_end_dt=cap_end_dt)
            records = resolve_countries(records)
            save_jsonl(paths["jsonl"], records)
            write_csv(paths["csv"], records)
            if not is_current:
                with open(paths["done"], "w") as f:
                    f.write(datetime.now(timezone.utc).isoformat())
                print(f"[{paths['label']}] closed out ({len(records)} images).", file=sys.stderr)
            else:
                print(f"[{paths['label']}] current month, will re-fetch fully next run.",
                      file=sys.stderr)
            months_fetched_this_run.append(paths["label"])
        elif os.path.exists(paths["jsonl"]):
            # Already fetched in a previous run (closed, or a stale current-month cache) - reuse.
            pass
        else:
            # Not reached yet in the backfill pacing - no data for this month this run.
            print(f"[{paths['label']}] not yet backfilled, skipping for now.", file=sys.stderr)

    # Rebuild combined output from everything currently on disk.
    all_records = []
    months_present = []
    months_pending = []
    for year, month in all_months:
        paths = _month_paths(year, month)
        if os.path.exists(paths["jsonl"]):
            all_records.extend(load_jsonl(paths["jsonl"]))
            months_present.append(paths["label"])
        else:
            months_pending.append(paths["label"])

    summary = build_aggregates(all_records, ORG_ID, months_present, months_pending)
    write_outputs(all_records, summary)

    print(
        f"Done. Months fetched this run: {months_fetched_this_run}. "
        f"Cumulative across {len(months_present)} month(s) on disk "
        f"({len(months_pending)} still pending backfill): "
        f"{summary['total_images']} images, {summary['total_sequences']} sequences, "
        f"{summary['total_users']} users, {summary['total_countries']} countries.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
