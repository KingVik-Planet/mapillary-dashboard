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

Mapillary's `/images` endpoint has a confirmed, undocumented gotcha:
**pagination breaks when filtering by `organization_id` alone.** A "full"
page of results (exactly the page limit) often comes back with no `next`
cursor at all - anything past image #500 for that query is silently
dropped, no error, no indication.

The collector works around this by bisecting the time window whenever a
response looks capped, recursively, down to a fine granularity
(`MAPILLARY_MIN_SPLIT_MINUTES`, default 5 minutes) - fine enough to fully
separate out even dense mapping campaigns.

*(An earlier version also tried re-fetching each contributor's history via
`creator_username`, since that filter paginates reliably on its own. That
turned out to be a dead end in practice: `organization_id` and
`creator_username` can't be combined in one query - Mapillary's API rejects
it - so that pass had to fetch each user's ENTIRE cross-organization
history and filter client-side, which for prolific contributors meant
tens of thousands of irrelevant images per user per month. It was also
where a real bug hid: it looked like it added completeness but was
silently matching zero records. Dropped entirely in favor of the simpler,
bounded-cost approach above.)*

### Monthly chunking + one-month-per-run pacing

Every calendar month from `MAPILLARY_START_MONTH` (default **2026-01**) up to
the current month is tracked separately, and **each run only fetches ONE of
them**, to keep every run fast and avoid ever repeating a many-hour run:

- If there's a past, fully-elapsed month that hasn't been closed out yet,
  the run fetches the OLDEST one of those, writes it to
  `data/monthly/YYYY-MM.jsonl` + `YYYY-MM.csv`, and marks it done with a
  `YYYY-MM.done` file. Nothing else is fetched this run.
- Once every past month is closed out, each run instead re-fetches the
  CURRENT (in-progress) month in full - since it's still accumulating new
  images, it needs a fresh full fetch every time, not a cached load. This
  keeps happening daily until the calendar rolls into the next month, at
  which point that month gets one final fetch, is closed out, and a new
  "current month" starts.

In practice: today's run backfills January and stops. Tomorrow's run
backfills February and stops. ... Once it catches up to the current month,
every day's run just refreshes that one month, until the month ends and a
new one starts piling up the same way - exactly the "get January, stop;
get February, stop; ... once we reach the current month, keep refreshing it
daily" pattern.

`data/latest.json` / `data/latest_images.csv` (what the dashboard reads)
are rebuilt every run from **whatever monthly files exist on disk** -
closed months plus the current month if it's been reached yet - so the
dashboard fills in progressively as the backfill catches up rather than
showing nothing until every month is done. `months_pending_backfill` in
`data/latest.json` tells you exactly which months haven't been reached yet.

**Trade-off:** an image captured in a past month but uploaded to Mapillary
very late (after that month was already closed out) won't be picked up
automatically. To force a recheck of a specific month, just delete its
`data/monthly/YYYY-MM.done` marker - the next run will re-fetch that month
in full (pausing the pacing on whatever month it would otherwise have
picked up).

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
different month. You can also set `MAPILLARY_MIN_SPLIT_MINUTES` (default
`5`) to control how fine the time-window bisection goes for very
high-volume organizations.

### 3. Enable GitHub Pages (optional, for the hosted dashboard)
**Settings → Pages → Source: Deploy from branch → `main` / `docs`**

Your dashboard will then be live at:
`https://<your-username>.github.io/<repo-name>/`

### 4. Run it
- It runs automatically every day at 02:00 UTC, backfilling **one month per
  run** until it catches up to the current month (see pacing explanation
  above) - so if you're starting from January and it's August now, expect
  it to take about a week of daily runs to fully catch up, after which it
  settles into refreshing just the current month each day.
- To speed up the initial backfill, or to catch up sooner: **Actions tab →
  Mapillary Organization Imagery Collector → Run workflow**, and trigger it
  manually as many times as you like (each manual run also only advances
  one month, same pacing logic).

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
- Backfill is paced at one month per run (see above) - this is intentional,
  to keep every run fast and reliable rather than one very long run that
  risks timing out or hitting transient API errors.
- `data/history/<date>.json` accumulates one snapshot per day, so you get a
  running trend history for free without needing a database.
- If you want to speed through the initial backfill faster than one month
  per day, manually trigger the workflow from the Actions tab repeatedly -
  each click advances one more month.
