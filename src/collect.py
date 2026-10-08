#!/usr/bin/env python3
"""
Mapillary Organization Imagery Collector
=========================================

Pulls images contributed to a given Mapillary organization, groups them into
sequences, resolves each image's country (offline reverse geocoding),
computes kilometers of ground covered, and writes aggregated JSON (+ CSV)
that a static dashboard / Power BI can read.

WHY THIS ISN'T A SINGLE SIMPLE API CALL
-----------------------------------------
Mapillary's /images endpoint has a confirmed, undocumented gotcha: cursor
pagination (the `paging.next` field) is NOT reliably returned when filtering
by organization_id alone. A "full" page (== page_limit records) commonly
comes back with no cursor at all - anything past record #500 for that query
is silently dropped, no error, no indication.

The workaround is organization_id-filtered time-window bisection: whenever a
window's results look capped (== page_limit, no cursor), the window is split
in half and each half is re-fetched, recursively, down to a fine time
granularity (MAPILLARY_MIN_SPLIT_MINUTES, default 1 minute). Cost is
proportional to how many genuinely dense pockets of time exist.

(An earlier version also fetched each contributor's full history by
creator_username. That was a dead end - it pulled every user's entire
cross-organization history - and has been removed.)

TWO RUN MODES
-------------
The script runs in one of two modes (--mode, or MAPILLARY_MODE):

1. daily (default) - the normal daily scan.
   * If a past month has not been closed out yet, fetch the OLDEST such
     month in full, write it to data/monthly/YYYY-MM.jsonl + .csv and mark
     it with data/monthly/YYYY-MM.done. Nothing else is fetched this run.
     A month that has a .done marker but whose .jsonl is missing (e.g. the
     Actions cache was evicted) counts as NOT closed and is re-fetched.
   * Otherwise refresh the CURRENT (in-progress) month INCREMENTALLY: only
     the window from (newest cached capture time - overlap) to now is
     fetched (overlap = MAPILLARY_CURRENT_OVERLAP_DAYS, default 7) and merged
     into the cached month. This replaces the old "re-fetch the whole month
     every day" behaviour and cuts API calls a lot as the month fills up.
     The month still gets a FULL fetch when it is closed out.

2. reconcile - the late-upload safety net (separate run, see the workflow).
   Photos are often uploaded days or weeks after they were captured, and
   Mapillary's captured_at is the CAPTURE time, so a late upload lands
   inside a month that is already closed. On the 21st, 22nd and 23rd of
   every month a reconcile run fully re-fetches one closed month:

        day 21  ->  the month 3 months back   (M-3)
        day 22  ->  the month 2 months back   (M-2)
        day 23  ->  the month 1 month back    (M-1)

   e.g. Oct 21 -> July, Oct 22 -> Aug, Oct 23 -> Sept;
        Nov 21 -> Aug,  Nov 22 -> Sept, Nov 23 -> Oct; ...
   Every closed month is therefore re-checked three times: at M+1 (23rd),
   M+2 (22nd) and M+3 (21st). A specific month can also be reconciled by
   hand with --month YYYY-MM (the workflow's manual-run form exposes this).
   Each reconcile is logged in data/reconcile_log.json.

MERGE RULE (important)
----------------------
Every fetch - closing fetch, incremental refresh, reconcile - is MERGED into
what is already cached for that month by image id. Existing records are kept
exactly as they are (including their resolved country), new ids are added,
and nothing is ever dropped. A flaky or partial API response can therefore
only ever add data, never remove it.

KILOMETERS COVERED
-------------------
Distance is estimated as straight-line (haversine great-circle) distance
between consecutive images WITHIN THE SAME SEQUENCE, ordered by
captured_at. It is NOT road-network distance - it slightly undercounts real
route length. It is computed once per run over the FULL combined record set
(all monthly files on disk), not per month, because a sequence can span a
month boundary. Each segment is attributed to the country and calendar day
of the LATER of its two points.

GPS OUTLIER FILTER: a segment whose implied speed exceeds
MAPILLARY_MAX_SPEED_KMH (default 200 km/h), or that jumps more than 100 m
between two images with identical timestamps, is treated as a GPS glitch
and contributes 0 km. The glitch point is skipped as an anchor, so the next
good image is measured from the last good one. If 3 segments in a row are
rejected the anchor is reset (the earlier point was probably the bad one).
The number of rejected segments is printed to stderr; the output structure
is unchanged.

Required environment variables:
    MAPILLARY_TOKEN   - Mapillary API access token (client token, "MLY|...")
    MAPILLARY_ORG_ID  - Organization ID to collect imagery for

Optional environment variables (empty values fall back to the default):
    MAPILLARY_MODE                  - "daily" (default) or "reconcile"
    MAPILLARY_RECONCILE_MONTH       - "YYYY-MM": reconcile this month instead of
                                      the one implied by today's date
    MAPILLARY_START_MONTH           - "YYYY-MM", first tracked month. Default 2025-01
    MAPILLARY_PAGE_LIMIT            - API page size (default 500)
    MAPILLARY_MIN_SPLIT_MINUTES     - floor for window bisection (default 1)
    MAPILLARY_CURRENT_OVERLAP_DAYS  - look-back for the incremental refresh of
                                      the current month (default 7)
    MAPILLARY_MAX_SPEED_KMH         - GPS outlier speed ceiling (default 200)
    MAPILLARY_HTTP_RETRIES          - retries for 5xx / network errors (default 6)

Output:
    data/monthly/YYYY-MM.jsonl  - raw per-month image records (cached)
    data/monthly/YYYY-MM.csv    - flat per-month image table
    data/monthly/YYYY-MM.done   - marker: this month is closed
    data/latest.json            - cumulative snapshot across all months (dashboard)
    data/latest_images.csv      - flat per-image table of everything (Power BI source)
    data/history/<date>.json    - dated cumulative snapshot, one per day
    data/reconcile_log.json     - when each closed month was re-checked, and what it added
"""

import argparse
import os
import sys
import json
import csv
import time
import calendar
from math import radians, sin, cos, sqrt, atan2
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests

try:
    import reverse_geocoder as rg
except ImportError:
    rg = None


def _env(name, default):
    """Environment lookup where an empty string counts as 'not set'
    (GitHub Actions passes unset variables through as empty strings)."""
    val = os.environ.get(name)
    return val if val not in (None, "") else default


MAPILLARY_TOKEN = _env("MAPILLARY_TOKEN", None)
ORG_ID = _env("MAPILLARY_ORG_ID", None)
PAGE_LIMIT = int(_env("MAPILLARY_PAGE_LIMIT", "500"))
START_MONTH = _env("MAPILLARY_START_MONTH", "2025-01")  # YYYY-MM
MIN_SPLIT = timedelta(minutes=float(_env("MAPILLARY_MIN_SPLIT_MINUTES", "1")))
CURRENT_OVERLAP = timedelta(days=float(_env("MAPILLARY_CURRENT_OVERLAP_DAYS", "7")))
MAX_SPEED_KMH = float(_env("MAPILLARY_MAX_SPEED_KMH", "200"))
HTTP_RETRIES = int(_env("MAPILLARY_HTTP_RETRIES", "6"))

MAX_RATE_LIMIT_WAITS = 60          # give up after this many consecutive-ish 429s
MAX_JUMP_NO_TIME_KM = 0.1          # identical timestamps but >100 m apart = glitch
REANCHOR_AFTER_REJECTS = 3         # consecutive rejected segments before re-anchoring

# day-of-month -> how many months back to reconcile
RECONCILE_DAYS = {21: 3, 22: 2, 23: 1}

API_ROOT = "https://graph.mapillary.com"
FIELDS = "id,captured_at,creator,sequence,organization_id,geometry"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
MONTHLY_DIR = os.path.join(DATA_DIR, "monthly")
HISTORY_DIR = os.path.join(DATA_DIR, "history")
RECONCILE_LOG_PATH = os.path.join(DATA_DIR, "reconcile_log.json")

EARTH_RADIUS_KM = 6371.0088  # mean earth radius

_sleep = time.sleep  # indirection so tests can skip real waiting


# ---------------------------------------------------------------------------
# Low-level HTTP / pagination
# ---------------------------------------------------------------------------

class MapillaryAPIError(RuntimeError):
    """Raised when the Mapillary API cannot be read after all retries,
    or answers with a non-retryable error (4xx other than 429)."""


def _dt_to_api(dt):
    """Format a UTC datetime as the ISO 8601 'Z' string Mapillary's API expects."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _backoff_seconds(attempt):
    """2, 4, 8, 16, 32, 60, 60 ... seconds for attempt 0, 1, 2 ..."""
    return min(60, 2 ** (attempt + 1))


def _retry_after_seconds(resp):
    try:
        wait = int(float(resp.headers.get("Retry-After", "5")))
    except (TypeError, ValueError):
        wait = 5
    return max(1, min(wait, 300))


def _fetch_page(headers, url, params, timeout=60):
    """GET a single page and return the parsed JSON payload.

    - 200            -> return the payload
    - 429            -> wait Retry-After and try again (bounded)
    - 5xx / network error / unreadable body -> retry with exponential backoff
                        (HTTP_RETRIES attempts), then raise MapillaryAPIError
    - other 4xx      -> raise immediately (retrying a bad request/token is pointless)
    """
    attempt = 0
    rate_limit_hits = 0

    while True:
        reason = None
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ContentDecodingError) as exc:
            reason = f"network error ({type(exc).__name__})"
            resp = None

        if resp is not None:
            status = resp.status_code
            if status == 200:
                try:
                    return resp.json()
                except ValueError:
                    reason = "200 with unreadable JSON body"
            elif status == 429:
                rate_limit_hits += 1
                if rate_limit_hits > MAX_RATE_LIMIT_WAITS:
                    raise MapillaryAPIError("Mapillary API kept rate-limiting (429) - giving up.")
                wait = _retry_after_seconds(resp)
                print(f"    Rate limited, waiting {wait}s...", file=sys.stderr)
                _sleep(wait)
                continue
            elif 500 <= status < 600:
                reason = f"HTTP {status}"
            else:
                raise MapillaryAPIError(
                    f"Mapillary API returned {status}: {resp.text[:500]}")

        # retryable failure
        attempt += 1
        if attempt > HTTP_RETRIES:
            raise MapillaryAPIError(
                f"Mapillary API still failing after {HTTP_RETRIES} retries ({reason}).")
        wait = _backoff_seconds(attempt - 1)
        print(f"    Transient failure ({reason}); retry {attempt}/{HTTP_RETRIES} in {wait}s...",
              file=sys.stderr)
        _sleep(wait)


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


def fetch_month(headers, org_id, year, month, page_limit, cap_end_dt=None, start_dt=None):
    """Fetch images belonging to org_id captured within the given month.

    cap_end_dt: stop here instead of at month end (in-progress current month).
    start_dt:   start here instead of at month start (incremental refresh).
    """
    month_start, month_end = month_bounds(year, month)
    start = month_start if start_dt is None else max(month_start, start_dt)
    end = month_end if cap_end_dt is None else min(month_end, cap_end_dt)
    if start >= end:
        return []

    label = f"{year:04d}-{month:02d}"
    print(f"  [{label}] Fetching {start.isoformat()} to {(end - timedelta(seconds=1)).isoformat()}...",
          file=sys.stderr)
    records = _fetch_window(headers, org_id, start, end, page_limit, MIN_SPLIT)

    by_id = {r["id"]: r for r in records}
    usernames = sorted({
        r["creator"]["username"] for r in records
        if r.get("creator", {}).get("username")
    })
    print(f"  [{label}] {len(by_id)} images from {len(usernames)} contributor(s): {usernames}",
          file=sys.stderr)
    return list(by_id.values())


def merge_records(existing, fetched):
    """Union by image id. Existing records win (so their resolved country is
    kept) and nothing is ever dropped. Returns (merged_list, number_added)."""
    by_id = {r["id"]: r for r in existing}
    added = 0
    for r in fetched:
        rid = r.get("id")
        if rid is None or rid in by_id:
            continue
        by_id[rid] = r
        added += 1
    return list(by_id.values()), added


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


def _is_closed(ym):
    """A month is closed only if BOTH its .done marker and its cached data
    exist. A marker without data (cache evicted) means it must be re-fetched."""
    p = _month_paths(*ym)
    return os.path.exists(p["done"]) and os.path.exists(p["jsonl"])


def refresh_month(headers, org_id, year, month, page_limit, *, incremental=False, cap_end_dt=None):
    """Fetch (fully, or incrementally for the current month), MERGE into the
    cached month, resolve countries for new records only, and save.
    Returns a stats dict."""
    paths = _month_paths(year, month)
    existing = load_jsonl(paths["jsonl"])

    start_dt = None
    if incremental and existing:
        newest_ms = max((r.get("captured_at") or 0) for r in existing)
        if newest_ms:
            newest = datetime.fromtimestamp(newest_ms / 1000, tz=timezone.utc)
            start_dt = newest - CURRENT_OVERLAP
            print(f"  [{paths['label']}] incremental: newest cached image {newest.isoformat()}, "
                  f"re-checking from {max(start_dt, month_bounds(year, month)[0]).isoformat()}.",
                  file=sys.stderr)

    fetched = fetch_month(headers, org_id, year, month, page_limit,
                          cap_end_dt=cap_end_dt, start_dt=start_dt)
    merged, added = merge_records(existing, fetched)
    merged = resolve_countries(merged)

    save_jsonl(paths["jsonl"], merged)
    write_csv(paths["csv"], merged)
    return {
        "label": paths["label"],
        "cached_before": len(existing),
        "fetched": len(fetched),
        "new_records": added,
        "total_after": len(merged),
    }


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
# Distance (kilometers covered)
# ---------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two lat/lon points, in kilometers."""
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * atan2(sqrt(a), sqrt(1 - a))


def _is_gps_outlier(dist_km, dt_hours):
    """True if travelling dist_km in dt_hours is physically implausible."""
    if dt_hours <= 0:
        return dist_km > MAX_JUMP_NO_TIME_KM
    return (dist_km / dt_hours) > MAX_SPEED_KMH


def compute_segment_distances(records):
    """Walk each sequence (grouped by 'sequence', ordered by captured_at) and
    compute the straight-line distance from each image to the previous GOOD
    image in that same sequence. Mutates records in place, adding:
        r["_segment_km"]  - distance (km) from the previous good image in its
                            sequence; 0.0 for a sequence's first image, for
                            images missing coords/timestamp/sequence, and for
                            segments rejected by the GPS outlier filter.

    Must be called on the FULL combined record set (all months on disk), not
    per month, since a sequence can span a month boundary.

    Returns {"rejected_segments": int, "rejected_km": float}.
    """
    by_seq = defaultdict(list)
    for r in records:
        r["_segment_km"] = 0.0
        seq = r.get("sequence")
        geom = r.get("geometry")
        if seq and geom and geom.get("coordinates") and r.get("captured_at") is not None:
            by_seq[seq].append(r)

    rejected = 0
    rejected_km = 0.0

    for seq_id, imgs in by_seq.items():
        imgs.sort(key=lambda r: r["captured_at"])
        prev = None
        consecutive_rejects = 0
        for r in imgs:
            if prev is None:
                prev = r
                continue

            lon1, lat1 = prev["geometry"]["coordinates"][0], prev["geometry"]["coordinates"][1]
            lon2, lat2 = r["geometry"]["coordinates"][0], r["geometry"]["coordinates"][1]
            dist = haversine_km(lat1, lon1, lat2, lon2)
            dt_hours = (r["captured_at"] - prev["captured_at"]) / 3_600_000.0

            if _is_gps_outlier(dist, dt_hours):
                rejected += 1
                rejected_km += dist
                consecutive_rejects += 1
                if consecutive_rejects >= REANCHOR_AFTER_REJECTS:
                    # Several in a row: the anchor itself was probably the bad
                    # point. Re-anchor here without adding distance.
                    prev = r
                    consecutive_rejects = 0
                continue  # otherwise keep the last good point as the anchor

            r["_segment_km"] = dist
            prev = r
            consecutive_rejects = 0

    return {"rejected_segments": rejected, "rejected_km": rejected_km}


# ---------------------------------------------------------------------------
# Aggregation + output
# ---------------------------------------------------------------------------

def build_aggregates(records, org_id, months_fetched, months_pending):
    dist_stats = compute_segment_distances(records)
    if dist_stats["rejected_segments"]:
        print(f"GPS outlier filter: ignored {dist_stats['rejected_segments']} segment(s) "
              f"({dist_stats['rejected_km']:.1f} km of implausible jumps; "
              f"ceiling {MAX_SPEED_KMH:g} km/h).", file=sys.stderr)

    by_country = defaultdict(lambda: {"images": 0, "sequences": set(), "users": set(), "km": 0.0})
    by_user = defaultdict(lambda: {"images": 0, "sequences": set(), "countries": set(), "km": 0.0})
    by_day = defaultdict(lambda: {"images": 0, "sequences": set(), "km": 0.0})
    by_sequence = defaultdict(lambda: {
        "images": 0, "km": 0.0, "username": None, "user_id": None,
        "country": None, "start": None, "end": None,
    })
    sequences_seen = set()
    users_seen = {}
    total_km = 0.0

    for r in records:
        cc = r.get("_country", "Unknown")
        cname = country_name(cc)
        seq_id = r.get("sequence")
        creator = r.get("creator") or {}
        user_id = creator.get("id", "unknown")
        username = creator.get("username", user_id)
        captured_at_ms = r.get("captured_at")
        seg_km = r.get("_segment_km", 0.0)
        day = None
        if captured_at_ms:
            day = datetime.fromtimestamp(captured_at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        by_country[cname]["images"] += 1
        by_country[cname]["km"] += seg_km
        if seq_id:
            by_country[cname]["sequences"].add(seq_id)
            sequences_seen.add(seq_id)
        by_country[cname]["users"].add(username)

        by_user[username]["images"] += 1
        by_user[username]["km"] += seg_km
        if seq_id:
            by_user[username]["sequences"].add(seq_id)
        by_user[username]["countries"].add(cname)
        users_seen[username] = user_id

        if day:
            by_day[day]["images"] += 1
            by_day[day]["km"] += seg_km
            if seq_id:
                by_day[day]["sequences"].add(seq_id)

        total_km += seg_km

        if seq_id:
            s = by_sequence[seq_id]
            s["images"] += 1
            s["km"] += seg_km
            s["username"] = username
            s["user_id"] = user_id
            if s["country"] is None:
                s["country"] = cname
            if captured_at_ms is not None:
                if s["start"] is None or captured_at_ms < s["start"]:
                    s["start"] = captured_at_ms
                if s["end"] is None or captured_at_ms > s["end"]:
                    s["end"] = captured_at_ms

    countries_out = [
        {
            "country": c, "images": v["images"], "sequences": len(v["sequences"]),
            "users": len(v["users"]), "km": round(v["km"], 3),
        }
        for c, v in sorted(by_country.items(), key=lambda kv: -kv[1]["images"])
    ]
    users_out = [
        {
            "username": u, "user_id": users_seen.get(u, "unknown"),
            "images": v["images"], "sequences": len(v["sequences"]),
            "countries": sorted(v["countries"]), "km": round(v["km"], 3),
        }
        for u, v in sorted(by_user.items(), key=lambda kv: -kv[1]["images"])
    ]
    daily_out = [
        {"date": d, "images": v["images"], "sequences": len(v["sequences"]), "km": round(v["km"], 3)}
        for d, v in sorted(by_day.items())
    ]
    sequences_out = [
        {
            "sequence_id": seq_id,
            "images": v["images"],
            "km": round(v["km"], 3),
            "username": v["username"],
            "user_id": v["user_id"],
            "country": v["country"],
            "start_captured_at": v["start"],
            "end_captured_at": v["end"],
        }
        for seq_id, v in sorted(by_sequence.items(), key=lambda kv: -kv[1]["km"])
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
        "total_km_covered": round(total_km, 3),
        "km_covered_note": (
            "Straight-line (haversine) distance between consecutive images in "
            "the same sequence, summed. Not road-network distance - undercounts "
            "actual route length, and accuracy depends on capture density."
        ),
        "by_country": countries_out,
        "by_user": users_out,
        "by_day": daily_out,
        "by_sequence": sequences_out,
    }


# NOTE: this column layout is a contract with the Power BI report that reads
# data/latest_images_chunks/. Do not rename, reorder, add or remove columns.
CSV_HEADER = ["image_id", "sequence_id", "user_id", "username",
              "captured_at_utc", "country", "lon", "lat", "segment_km"]


def write_csv(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
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
                round(r.get("_segment_km", 0.0), 4),
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


def append_reconcile_log(stats, now):
    """Record a reconcile run in data/reconcile_log.json (last 12 entries per month)."""
    log = {}
    if os.path.exists(RECONCILE_LOG_PATH):
        try:
            with open(RECONCILE_LOG_PATH, "r") as f:
                log = json.load(f)
        except (ValueError, OSError):
            log = {}
    entries = log.setdefault(stats["label"], [])
    entries.append({
        "run_at": now.isoformat(),
        "fetched": stats["fetched"],
        "new_records": stats["new_records"],
        "total_after": stats["total_after"],
    })
    log[stats["label"]] = entries[-12:]

    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = RECONCILE_LOG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dict(sorted(log.items())), f, indent=2)
    os.replace(tmp, RECONCILE_LOG_PATH)


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

def reconcile_target(now, forced_month=None):
    """Which closed month should a reconcile run re-check?
    forced_month ("YYYY-MM") wins; otherwise the day of month decides:
    21 -> M-3, 22 -> M-2, 23 -> M-1. Returns (year, month) or None."""
    if forced_month:
        try:
            y, m = (int(x) for x in forced_month.split("-"))
            datetime(y, m, 1)
        except ValueError:
            raise ValueError(f"Invalid reconcile month {forced_month!r}; expected YYYY-MM")
        return (y, m)

    back = RECONCILE_DAYS.get(now.day)
    if back is None:
        return None
    y, m = now.year, now.month - back
    while m <= 0:
        m += 12
        y -= 1
    return (y, m)


def run_daily(headers, now, all_months, current_ym):
    past_months = [ym for ym in all_months if ym != current_ym]
    unclosed_past = [ym for ym in past_months if not _is_closed(ym)]

    healed = [ym for ym in unclosed_past
              if os.path.exists(_month_paths(*ym)["done"]) and not os.path.exists(_month_paths(*ym)["jsonl"])]
    if healed:
        print(f"NOTE: {len(healed)} month(s) have a .done marker but no cached data "
              f"(cache evicted?) - they will be re-fetched one per run: "
              f"{[f'{y:04d}-{m:02d}' for y, m in healed]}", file=sys.stderr)

    if unclosed_past:
        target = unclosed_past[0]
        print(f"Closing one past month this run: {target[0]:04d}-{target[1]:02d} "
              f"({len(unclosed_past) - 1} more past month(s) still pending after this).",
              file=sys.stderr)
        stats = refresh_month(headers, ORG_ID, *target, PAGE_LIMIT, incremental=False)
        with open(_month_paths(*target)["done"], "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"[{stats['label']}] closed out ({stats['total_after']} images, "
              f"{stats['new_records']} new).", file=sys.stderr)
    else:
        target = current_ym
        print(f"All past months closed - refreshing current month "
              f"{target[0]:04d}-{target[1]:02d} incrementally.", file=sys.stderr)
        stats = refresh_month(headers, ORG_ID, *target, PAGE_LIMIT,
                              incremental=True, cap_end_dt=now)
        print(f"[{stats['label']}] current month: {stats['new_records']} new image(s), "
              f"{stats['total_after']} total so far.", file=sys.stderr)
    return [stats["label"]]


def run_reconcile(headers, now, all_months, current_ym, forced_month):
    target = reconcile_target(now, forced_month)
    if target is None:
        print(f"Reconcile mode, but day {now.day} is not a reconcile day (21/22/23) and no "
              f"--month was given. Nothing to do.", file=sys.stderr)
        return []
    if target >= current_ym:
        sys.exit(f"ERROR: can only reconcile closed (past) months; "
                 f"{target[0]:04d}-{target[1]:02d} is not in the past.")
    if target not in all_months:
        print(f"Reconcile target {target[0]:04d}-{target[1]:02d} is before MAPILLARY_START_MONTH "
              f"({START_MONTH}); skipping.", file=sys.stderr)
        return []

    print(f"Reconciling closed month {target[0]:04d}-{target[1]:02d} "
          f"(late-upload check, full re-fetch + merge).", file=sys.stderr)
    stats = refresh_month(headers, ORG_ID, *target, PAGE_LIMIT, incremental=False)

    # A reconciled month is by definition closed; make sure the marker exists.
    done_path = _month_paths(*target)["done"]
    if not os.path.exists(done_path):
        with open(done_path, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())

    append_reconcile_log(stats, now)
    print(f"[{stats['label']}] reconcile complete: fetched {stats['fetched']}, "
          f"{stats['new_records']} NEW late record(s) added, {stats['total_after']} total.",
          file=sys.stderr)
    return [stats["label"]]


def load_all_months(all_months):
    """Load every monthly file on disk. De-duplicates by image id across months."""
    all_records, months_present, months_pending = [], [], []
    seen = set()
    for year, month in all_months:
        paths = _month_paths(year, month)
        if os.path.exists(paths["jsonl"]):
            for r in load_jsonl(paths["jsonl"]):
                rid = r.get("id")
                if rid in seen:
                    continue
                seen.add(rid)
                all_records.append(r)
            months_present.append(paths["label"])
        else:
            months_pending.append(paths["label"])
    return all_records, months_present, months_pending


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Mapillary organization imagery collector")
    ap.add_argument("--mode", choices=["daily", "reconcile"],
                    default=_env("MAPILLARY_MODE", "daily"),
                    help="daily scan (default) or late-upload reconcile of one closed month")
    ap.add_argument("--month", default=_env("MAPILLARY_RECONCILE_MONTH", None),
                    help="reconcile mode only: YYYY-MM to re-check instead of the date-derived month")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.mode not in ("daily", "reconcile"):
        sys.exit(f"ERROR: unknown mode {args.mode!r}; use 'daily' or 'reconcile'.")

    if not MAPILLARY_TOKEN:
        sys.exit("ERROR: MAPILLARY_TOKEN is not set.")
    if not ORG_ID:
        sys.exit("ERROR: MAPILLARY_ORG_ID is not set.")

    headers = {"Authorization": f"OAuth {MAPILLARY_TOKEN}"}

    start_year, start_month = (int(x) for x in START_MONTH.split("-"))
    now = datetime.now(timezone.utc)
    current_ym = (now.year, now.month)
    all_months = list(iter_months(start_year, start_month, *current_ym))

    try:
        if args.mode == "reconcile":
            months_touched = run_reconcile(headers, now, all_months, current_ym, args.month)
            if not months_touched:
                return
        else:
            months_touched = run_daily(headers, now, all_months, current_ym)
    except MapillaryAPIError as exc:
        # Nothing for the failed month has been saved (saves happen only after a
        # complete fetch), so the next run simply starts that month again.
        sys.exit(f"ERROR: {exc}")

    # Rebuild combined output from everything currently on disk.
    all_records, months_present, months_pending = load_all_months(all_months)

    summary = build_aggregates(all_records, ORG_ID, months_present, months_pending)
    write_outputs(all_records, summary)

    print(
        f"Done ({args.mode}). Months touched this run: {months_touched}. "
        f"Cumulative across {len(months_present)} month(s) on disk "
        f"({len(months_pending)} not present): "
        f"{summary['total_images']} images, {summary['total_sequences']} sequences, "
        f"{summary['total_users']} users, {summary['total_countries']} countries, "
        f"{summary['total_km_covered']} km covered.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
