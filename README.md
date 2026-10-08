# Mapillary Organization Dashboard

Collects imagery statistics for a Mapillary organization - images, sequences,
contributing users, countries and kilometers covered - and publishes them in
two ways, both refreshed automatically by GitHub Actions:

| Consumer | Reads | Notes |
|---|---|---|
| **Power BI report** | `data/latest_images_chunks/latest_images_N.csv` | One row per image. Append-only - see [The Power BI data contract](#the-power-bi-data-contract). |
| **Static web dashboard** (`docs/index.html`) | `data/latest.json` | Pre-aggregated summary; works as a GitHub Pages site. |

## Schedule at a glance

| Run | When (UTC) | What it does |
|---|---|---|
| **Daily scan** | every day, 00:00 | Closes any unfinished past month, otherwise refreshes the *current* month incrementally. |
| **Reconcile** | 12:00 on the **21st, 22nd and 23rd** of each month | Fully re-fetches one *closed* month to catch images that were captured earlier but uploaded late. |

The two runs are separate workflow runs, 12 hours apart, and a `concurrency`
lock guarantees they can never overlap.

## How it works

1. `src/collect.py` calls the Mapillary API v4 `/images` endpoint one calendar
   month at a time and reverse-geocodes each image's coordinates **offline**
   (via `reverse_geocoder`) into a country - no extra geocoding API, no extra
   rate limits.
2. Results are cached per month, merged, aggregated by country / user / day /
   sequence, and written to `data/latest.json`, `data/latest_images.csv` and a
   dated snapshot in `data/history/`.
3. `src/export_chunks.py` publishes the CSV into GitHub-safe chunks (each under
   40 MB) so the data lives in the repo and Power BI can read it.
4. `.github/workflows/collect.yml` runs all of this on a schedule and commits
   the results.

### Why it isn't a single simple API call

Mapillary's `/images` endpoint has a confirmed, undocumented gotcha:
**pagination breaks when filtering by `organization_id` alone.** A "full" page
of results (exactly the page limit) often comes back with no `next` cursor at
all - anything past image #500 for that query is silently dropped, with no
error and no indication.

The collector works around this by **bisecting the time window** whenever a
response looks capped, recursively, down to `MAPILLARY_MIN_SPLIT_MINUTES`
(default 1 minute) - fine enough to separate even dense mapping campaigns.

*(An earlier version also tried re-fetching each contributor's history via
`creator_username`. That was a dead end - `organization_id` and
`creator_username` can't be combined, so it pulled every user's entire
cross-organization history and filtered client-side, which was enormously slow
and silently matched nothing. It was removed.)*

## Daily scan

Every calendar month from `MAPILLARY_START_MONTH` (default **2025-01**) to the
current month is tracked separately, and **each daily run touches only one of
them**, to keep runs fast:

- **A past month isn't closed yet** -> the oldest such month is fetched in
  full, saved to `data/monthly/YYYY-MM.jsonl` + `.csv`, and marked with
  `data/monthly/YYYY-MM.done`. Nothing else is fetched this run.
- **Every past month is closed** -> the *current* month is refreshed
  **incrementally**: only the window from *(newest image already cached - 7
  days)* up to now is fetched and merged into the cached month. Previously the
  whole month was re-downloaded every day; now the cost stays small as the
  month fills up. (`MAPILLARY_CURRENT_OVERLAP_DAYS` changes the 7-day
  look-back.) The month still gets a **full** fetch when it is closed out on
  the first run after it ends.

A month that has a `.done` marker but **no cached data** (for example because
GitHub evicted the Actions cache) is treated as *not closed* and is re-fetched
automatically, one month per run.

## Reconcile: catching late uploads

Photos are often uploaded days or weeks after they were taken. Mapillary's
`captured_at` is the **capture** time, so a late upload lands inside a month
that is already closed - a closed month would never see it. The reconcile run
fixes that by re-checking closed months on a fixed rota:

| Day of month | Month re-checked | Example (October) | Example (November) | Example (December) |
|---|---|---|---|---|
| **21st** | 3 months back (M-3) | July | August | September |
| **22nd** | 2 months back (M-2) | August | September | October |
| **23rd** | 1 month back (M-1) | September | October | November |

Result: **every closed month is re-checked exactly three times** - on the 23rd
of the month after it ends (M+1), the 22nd of the month after that (M+2), and
the 21st one month later (M+3) - on top of its normal closing fetch.

Each reconcile run fully re-fetches one month, merges the result into the
cached month, and logs what happened in `data/reconcile_log.json`:

```json
{
  "2026-07": [
    {"run_at": "2026-10-21T12:03:11+00:00", "fetched": 41872, "new_records": 63, "total_after": 41935}
  ]
}
```

`new_records` is how many late uploads that check found.

**Manual reconcile:** *Actions -> Mapillary Organization Imagery Collector ->
Run workflow*, choose `mode = reconcile` and optionally a `month` (`YYYY-MM`).
With no month, the date rule above is used.

### Merge rule

Every fetch - closing, incremental, reconcile - is **merged by image id** into
what is already cached. Existing records are kept exactly as they are
(including their resolved country), new ids are added, and **nothing is ever
dropped**. A flaky or partial API response can only add data, never remove it.

## The Power BI data contract

`data/latest_images_chunks/` is what the Power BI report reads, so its
structure is treated as fixed:

- **File names:** `latest_images_1.csv`, `latest_images_2.csv`, ...
- **Columns** (always this header, in this order):

  | Column | Meaning |
  |---|---|
  | `image_id` | Mapillary image id (unique) |
  | `sequence_id` | Mapillary sequence the image belongs to |
  | `user_id`, `username` | Contributor |
  | `captured_at_utc` | Capture time, ISO 8601, UTC |
  | `country` | Country name from offline reverse geocoding |
  | `lon`, `lat` | Image coordinates |
  | `segment_km` | Straight-line km from the previous *good* image in the same sequence (0 for the first image of a sequence and for GPS outliers) |

- **Append-only.** Rows already in a chunk **stay in that file at the same
  position**. New rows - including late-uploaded images belonging to older
  months - are appended to the **newest chunk** until it reaches 40 MB; then
  the next numbered file is started (`latest_images_11.csv`, ...). Nothing is
  shuffled between files.
- **Rows are never removed**, even if they are missing from a rebuilt source.
  If the Actions cache is ever lost, the published data does not shrink.
- **Structure guard.** If the header of the freshly built data ever differs
  from the existing chunks, the export **aborts before changing anything**.
- **The only edit made to an existing row** is its `segment_km` value, and only
  when the recomputed value differs (a late image can land in the middle of an
  already-stored sequence, or the GPS outlier filter zeroes a bogus jump).
  Only the chunk files that actually contain such a row are rewritten.
  `--no-patch` turns this off.
- Rows are therefore **not guaranteed to be in date order** across the files.
  Sort or filter on `captured_at_utc` inside Power BI.

> **Power BI note:** make sure the report loads *every* file in
> `data/latest_images_chunks/` (for example via a folder-style query) rather than
> a hard-coded list of file names, so a new `latest_images_11.csv` is picked up
> when it appears.

## Data accuracy

**Kilometers covered** is the straight-line (haversine) distance between
consecutive images in the same sequence, ordered by capture time. It is not
road-network distance - it cuts corners, so it slightly undercounts real route
length - and it depends on capture density. It is computed over the *full*
dataset, because a sequence can span a month boundary. Each segment is
attributed to the country and day of the later of its two images.

**GPS outlier filter:** a segment whose implied speed exceeds
`MAPILLARY_MAX_SPEED_KMH` (default 200 km/h), or that jumps more than 100 m
between two images with the same timestamp, is treated as a GPS glitch and
adds 0 km. The glitch point is skipped, so the next good image is measured from
the last good one; after 3 rejected segments in a row the anchor is reset. The
number of rejected segments is printed in the workflow log.

**Country** is resolved offline from the nearest populated place, so it is
accurate for country-level grouping but can be wrong for points very close to a
border or coast. It is resolved once per image and then cached.

**API reliability:** `5xx` responses, timeouts, dropped connections and
unreadable responses are retried with exponential backoff (2, 4, 8, 16, 32,
60 s; `MAPILLARY_HTTP_RETRIES`, default 6). `429` rate limits honour
`Retry-After`. Other `4xx` errors (bad token, bad request) fail immediately.
If a month still cannot be fetched, the run exits with an error **without
saving anything for that month**, so the next run simply starts it again.

## Data files

| File | Committed? | Contents |
|---|---|---|
| `data/latest_images_chunks/latest_images_N.csv` | yes | **Power BI source.** Append-only per-image table, <=40 MB per file. |
| `data/latest.json` | yes | Cumulative summary across all months - read by `docs/index.html`. |
| `data/history/<date>.json` | yes | One cumulative snapshot per day, for trend tracking. |
| `data/reconcile_log.json` | yes | When each closed month was re-checked and how many late records it added. |
| `data/monthly/YYYY-MM.done` | yes | Marker: this month is closed. Delete it to force a full re-fetch of that month. |
| `data/monthly_chunks/*.csv` | yes (optional) | Legacy per-month CSV chunks. Duplicates the rows in `latest_images_chunks`; Power BI doesn't use them. Safe to remove - see workflow comments. Their `segment_km` column is always `0` (per-month files are written before distances are computed); use `latest_images_chunks` for km. |
| `data/monthly/YYYY-MM.jsonl` / `.csv` | no (Actions cache) | Raw cached records per month. |
| `data/latest_images.csv` | no (Actions cache + Release asset) | Full per-image table, the source the chunks are built from. |

## What you get in `data/latest.json`

```json
{
  "organization_id": "...",
  "generated_at": "...",
  "months_fetched": ["2025-01", "..."],
  "months_pending_backfill": [],
  "total_images": 12345,
  "total_sequences": 210,
  "total_users": 8,
  "total_countries": 6,
  "total_km_covered": 4321.5,
  "by_country": [{"country": "Rwanda", "images": 5000, "sequences": 80, "users": 3, "km": 1200.3}],
  "by_user": [{"username": "jdoe", "user_id": "...", "images": 3000, "sequences": 40, "countries": ["Rwanda"], "km": 800.1}],
  "by_day": [{"date": "2026-08-10", "images": 120, "sequences": 3, "km": 15.2}],
  "by_sequence": [{"sequence_id": "...", "images": 300, "km": 12.4, "username": "jdoe", "user_id": "...", "country": "Rwanda", "start_captured_at": 0, "end_captured_at": 0}]
}
```

## Setup

### 1. Get your Mapillary credentials
- **Access token**: Mapillary -> Developer settings -> create a Client
  Application -> copy the Client Token (looks like `MLY|xxxx|xxxx`).
- **Organization ID**: visible in your organization's Mapillary dashboard URL,
  or via your profile settings.

### 2. Add repo secrets
**Settings -> Secrets and variables -> Actions -> New repository secret**

| Secret name | Value |
|---|---|
| `MAPILLARY_TOKEN` | your Mapillary access token |
| `MAPILLARY_ORG_ID` | your organization ID |

### 3. Optional repo variables
**Settings -> Secrets and variables -> Actions -> Variables.** Leave unset to
use the defaults.

| Variable | Default | Purpose |
|---|---|---|
| `MAPILLARY_START_MONTH` | `2025-01` | First tracked month (`YYYY-MM`). |
| `MAPILLARY_MIN_SPLIT_MINUTES` | `1` | Finest time-window bisection, for very high-volume orgs. |
| `MAPILLARY_CURRENT_OVERLAP_DAYS` | `7` | Look-back of the incremental current-month refresh. |
| `MAPILLARY_MAX_SPEED_KMH` | `200` | GPS outlier speed ceiling. |

Also available as environment variables when running locally:
`MAPILLARY_PAGE_LIMIT` (default 500), `MAPILLARY_HTTP_RETRIES` (default 6).

### 4. Enable GitHub Pages (optional, for the hosted dashboard)
**Settings -> Pages -> Source: Deploy from branch -> `master` / `docs`**

The dashboard is then live at `https://<your-username>.github.io/<repo-name>/`.

## Running locally

```bash
pip install -r requirements.txt
export MAPILLARY_TOKEN="MLY|your_token"
export MAPILLARY_ORG_ID="your_org_id"

python src/collect.py                                  # daily scan
python src/collect.py --mode reconcile                 # only does something on the 21st-23rd
python src/collect.py --mode reconcile --month 2026-07 # re-check one specific closed month

# rebuild the Power BI chunks from data/latest_images.csv (append-only)
python src/export_chunks.py --src data/latest_images.csv \
    --out data/latest_images_chunks --max-mb 40 --append
```

Because the raw monthly data normally lives only in the Actions cache, a local
run starts without it and will re-fetch history. Don't commit a locally
generated `data/latest_images_chunks/` over the ones produced by the workflow.

## Troubleshooting

- **A month looks short / a late upload is missing.** Run the workflow
  manually with `mode = reconcile` and that `month`, or delete its
  `data/monthly/YYYY-MM.done` marker to force a full re-fetch on the next daily
  run.
- **The run failed with an API error.** Nothing was saved for the month being
  fetched; the next run retries it from scratch. Persistent `4xx` errors
  usually mean an expired or wrong `MAPILLARY_TOKEN`.
- **The Actions cache was evicted.** The collector detects months with a
  `.done` marker but no data and re-fetches them one per run; the published CSV
  chunks keep all their rows meanwhile. The dashboard banner lists the months
  still pending.
- **Export aborted with "header mismatch".** The column layout of
  `latest_images.csv` no longer matches the existing chunks. That protects the
  Power BI report - don't change the CSV columns without migrating the chunks.

## Notes / current limitations

- Distances are straight-line estimates, not road distance (see above).
- Country is nearest-place based, not a true boundary lookup (see above).
- Images deleted on Mapillary after they were collected stay in the data - rows
  are never removed.
- A late upload is picked up on the next reconcile run for its month, so there
  can be a delay of up to a few weeks before it appears; each month is
  re-checked three times over about three months.
- `data/history/<date>.json` accumulates one snapshot per day, giving a
  running trend history without a database.
