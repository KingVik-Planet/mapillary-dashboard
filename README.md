# Mapillary Organization Dashboard

Collects imagery statistics for a Mapillary organization — images, sequences,
contributing users, and country breakdown — and publishes a static dashboard
that updates automatically every day at **02:00 UTC**, same pattern as the
Maplet Starts project.

## How it works

1. `src/collect.py` calls the Mapillary API v4 `/images` endpoint filtered by
   `organization_id`, paging through every image contributed to the org.
2. Each image's coordinates are reverse-geocoded **offline** (via
   `reverse_geocoder`) into a country — no external geocoding API, no extra
   rate limits.
3. Results are aggregated by country, by user, and by day, and written to
   `data/latest.json` (+ `data/latest_images.csv` and a dated snapshot in
   `data/history/`).
4. `docs/index.html` is a static dashboard (Chart.js, no backend) that reads
   `data/latest.json` directly — works as a GitHub Pages site.
5. A GitHub Actions workflow (`.github/workflows/collect.yml`) runs the
   collector on a schedule and commits the refreshed data back to the repo,
   so the dashboard is always current.

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
