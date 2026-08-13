# Mapillary Organization Dashboard

Collects imagery statistics for a Mapillary organization — images, sequences,
contributing users, and country breakdown — and publishes a static dashboard
that updates automatically every day at **02:00 UTC**, same pattern as the
Maplet Starts project.

## How it works

1. `src/collect.py` calls the Mapillary API v4 `/images` endpoint, one
   calendar month at a time, and reverse-geocodes each image's coordinates
   **offline** (via `reverse_geocoder`) into a country - no external geocoding
   API, no extra rate limits.
2. Results are aggregated by country, by user, and by day, and written to
   `data/latest.json` (+ `data/latest_images.csv` and a dated snapshot in
   `data/history/`).
3. `docs/index.html` is a static dashboard (Chart.js, no backend) that reads
   `data/latest.json` directly - works as a GitHub Pages site.
4. A GitHub Actions workflow (`.github/workflows/collect.yml`) runs the
   collector daily at 02:00 UTC and commits the refreshed data back to the
   repo, so the dashboard is always current.

### Why this isn't a single simple API call

Two confirmed quirks in Mapillary's `/images` endpoint make a naive fetch
silently incomplete, so the collector works around both:

- **Pagination breaks when filtering by `organization_id` alone.** A "full"
  page of results often comes back with no `next` cursor at all - anything
  past image #500 for that query is just gone, with no error. The collector
  works around this by bisecting the time window whenever a response looks
  capped, to find every contributing user (the "discovery" pass).
- **`organization_id` and `creator_username` can't be combined** - Mapillary's
  API rejects that combination outright. But `creator_username` **alone**
  paginates reliably. So for each user discovered, the collector re-fetches
  that user's full history for the month (no org filter) and keeps only the
  images whose `organization_id` matches this org (the "authoritative" pass).
  This is what actually guarantees nothing gets missed, regardless of how
  dense a capture session was.

### Monthly chunking + closing out past months

Every calendar month from `MAPILLARY_START_MONTH` (default **2026-01**) up to
the current month is processed separately:

- **Past, fully-elapsed months** are fetched **once**, written to
  `data/monthly/YYYY-MM.jsonl` + `YYYY-MM.csv`, and marked done with a
  `YYYY-MM.done` file. Once marked done, that month is never re-fetched -
  it's just loaded from disk on every subsequent run.
- **The current, in-progress month** is re-fetched in full on every run
  (since new images keep arriving all month). Once the calendar rolls into
  the next month, it gets one final fetch, is written out, and gets its own
  `.done` marker - then a new month starts accumulating.
- `data/latest.json` / `data/latest_images.csv` (what the dashboard and
  cross-date questions use) are rebuilt every run from **all** monthly files
  combined - cached past months plus the freshly-fetched current month.

This keeps each run fast (bisection recursion only ever has to cover at most
one month, not the whole history), and avoids re-scanning settled data every
single night.

**Trade-off:** an image captured in a past month but uploaded to Mapillary
very late (after that month was already closed out) won't be picked up
automatically. To force a recheck of a specific month, just delete its
`data/monthly/YYYY-MM.done` marker - the next run will re-fetch that month
in full.

## Data files

| File | Contents |
|---|---|
| `data/monthly/YYYY-MM.jsonl` | Raw image records for that month (persisted, cached once closed). |
| `data/monthly/YYYY-MM.csv` | Flat per-image table for that month alone. |
| `data/monthly/YYYY-MM.done` | Marker: this month is closed and won't be re-fetched. Delete to force a recheck. |
| `data/latest.json` | Current cumulative summary across all months - this is what the dashboard reads. |
| `data/latest_images.csv` | Flat per-image table of the full cumulative dataset. |
| `data/history/<date>.json` | One cumulative snapshot per day, for trend tracking over time. |

## What you get in `data/latest.json`

```json
{
  "organization_id": "...",
  "generated_at": "...",
  "total_images": 12345,
  "total_sequences": 210,
  "total_users": 8,
  "total_countries": 6,
  "by_country": [{"country": "Rwanda", "images": 5000, "sequences": 80, "users": 3}, ...],
  "by_user": [{"username": "jdoe", "user_id": "...", "images": 3000, "sequences": 40, "countries": ["Rwanda"]}, ...],
  "by_day": [{"date": "2026-08-10", "images": 120, "sequences": 3}, ...]
}
```

This is enough to answer things like "how many sequences were collected
between two dates" or "which country had the most imagery" directly from the
JSON, or by filtering `data/latest_images.csv`.

## Setup

### 1. Get your Mapillary credentials
- **Access token**: Mapillary → Developer settings → create a Client
  Application → copy the Client Token (looks like `MLY|xxxx|xxxx`).
- **Organization ID**: visible in your organization's Mapillary dashboard
  URL, or via your profile settings.

### 2. Add repo secrets
In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name         | Value                          |
|----------------------|--------------------------------|
| `MAPILLARY_TOKEN`    | your Mapillary access token    |
| `MAPILLARY_ORG_ID`   | your organization ID           |

Optionally set `MAPILLARY_START_MONTH` (format `YYYY-MM`, default `2026-01`)
as a repo **variable** (not secret) if you want backfill to start from a
different month.

### 3. Enable GitHub Pages (optional, for the hosted dashboard)
**Settings → Pages → Source: Deploy from branch → `main` / `docs`**

Your dashboard will then be live at:
`https://<your-username>.github.io/<repo-name>/`

### 4. Run it
- It runs automatically every day at 02:00 UTC.
- To trigger it manually: **Actions tab → Mapillary Organization Imagery
  Collector → Run workflow**.

## Running locally

```bash
pip install -r requirements.txt
export MAPILLARY_TOKEN="MLY|your_token"
export MAPILLARY_ORG_ID="your_org_id"
export MAPILLARY_START_MONTH="2026-01"   # optional, this is the default
python src/collect.py
```

## Notes / current limitations

- Country is resolved from image GPS coordinates offline (fast, no API
  quota), so accuracy is only as good as `reverse_geocoder`'s coastal/border
  approximation — fine for country-level grouping, not for exact
  administrative boundaries.
- Mapillary's `/images` endpoint pages via cursor; large organizations
  (100k+ images) will take a few minutes to fully paginate — this runs fine
  in GitHub Actions' default job time limit but is worth knowing about.
- `data/history/<date>.json` accumulates one snapshot per day, so you get a
  running trend history for free without needing a database.
