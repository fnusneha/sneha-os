# Sneha.OS — contributor notes

Personal fitness dashboard. Flask web app + GitHub Actions cron, backed
by Postgres. See [README.md](README.md) for the full architecture.

## Quick orientation

- **`app.py`** — Flask entrypoint. Routes: `/dashboard`, `/rides`,
  `/api/*`, `/healthz`. Runs on Gunicorn in production.
- **`sync.py`** — cron entrypoint. Pulls external APIs → writes
  Postgres rows. Runs 4×/day via `.github/workflows/sync.yml`.
- **`db.py`** — the only module that knows SQL. Everything else
  gets typed dicts back from it.
- **`data_gather.py`** — shapes DB rows into the dict the HTML
  templates expect.
- **`html_report.py` + `rides_report.py`** — HTML renderers; string
  templates in `templates/*.html`.
- **`tz.py`** — `local_today()`, `local_now()`. Always use these
  instead of `date.today()`; cron runs in UTC but the app is
  pinned to America/Los_Angeles.

## Running locally

```bash
source .venv/bin/activate
python sync.py --morning --force   # pull today's data
python app.py                      # http://localhost:8000/dashboard
```

## Testing a deploy

```bash
scripts/smoke.sh https://sneha-os.onrender.com   # 12-check smoke
```

## Conventions

- Never commit secrets — `.env` is gitignored. Production secrets
  live in Render env + GitHub Actions secrets.
- Idempotent writes: `sync.py` can re-run the same day without
  clobbering a collected star or a manual sauna tick.
- Graceful degradation: if Garmin or Oura flakes, the dashboard
  still renders — stale fields stay stale rather than blank.
