# Mapillary Organization Dashboard

Collects imagery statistics for a Mapillary organization — images, sequences,
contributing users, and country breakdown — and publishes a static dashboard
that updates automatically every day at **02:00 UTC**, same pattern as the
Maplet Starts project.

## How it works

1. `src/collect.py` calls the Mapillary API v4 `/images` endpoint filtered by
   `organization_id`, paging through images contributed to the org.
2. Each image's coordinates are reverse-geocoded **offline** (via
   `reverse_geocoder`) into a country — no external geocoding API, no extra
   rate limits.
3. Results are aggregated by country, by user, and by day, and written to
   `data/latest.json` (+ `data/latest_images.csv` and a dated snapshot in
   `data/history/`).
4. `docs/index.html` is a static dashboard (Chart.js, no backend) that reads
   `data/latest.json` directly — works as a GitHub Pages site.
5. A GitHub Actions workflow (`.github/workflows/collect.yml`) runs the
   collector daily at 02:00 UTC and commits the refreshed data back to the
   repo, so the dashboard is always current.

### Backfill + daily append (how the data accumulates)

Every image ever collected lives in a persistent master store,
`data/images_master.jsonl` (one JSON record per image, committed to the
repo alongside `data/latest.json`).

- **First run ever** (no master store in the repo yet): backfills everything
  from **2026-01-01** through today. This can take a while for a
  large organization — that's expected, it only happens once.
- **Every run after that**: the script looks at the newest `captured_at`
  timestamp already in the master store, and only asks Mapillary for images
  captured from **2 days before that point** onward (the 2-day overlap is a
  safety margin in case images get uploaded a bit late). New/updated images
  are merged into the master store by image ID — nothing gets duplicated or
  double-counted — and `data/latest.json` is rebuilt from the **full**
  accumulated dataset, not just that day's slice.

So in practice: tonight's 2 AM UTC run backfills 2026-01-01 → today.
Tomorrow's run only fetches what's new since then and appends it. The
dashboard always shows totals since 2026-01-01, growing a little each day.

If you ever want to force a full re-backfill (e.g. you suspect the master
store got corrupted), just delete `data/images_master.jsonl` from the repo
and the next run will start over from `MAPILLARY_START_DATE` (or the
2026-01-01 default).

## Data files

| File | Contents |
|---|---|
| `data/images_master.jsonl` | Every image ever collected (raw, one per line), the persistent source of truth. Grows by append each day. |
| `data/latest.json` | Current cumulative summary — this is what the dashboard reads. |
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
python src/collect.py
```

Optional date filtering (e.g. to backfill or limit scope):

```bash
export MAPILLARY_START_DATE=2026-01-01
export MAPILLARY_END_DATE=2026-08-01
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
