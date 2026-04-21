# Sneha.OS

A personal fitness operating system. Pulls health + training data from
Oura, Garmin, Strava, and Google Calendar; surfaces it on a mobile-first
web dashboard with two tabs — **Quest Hub** (daily rituals, weekly
progress, habit pillars) and **Ride Atlas** (every cycling ride I've
ever logged, mapped + scored against past years).

Single-user. Zero-cost hosting. Runs independently of any laptop.

**Live:** https://sneha-os.onrender.com/dashboard

---

## Screenshots

| Quest Hub | Ride Atlas |
| :--: | :--: |
| Weekly star pulse, morning/core/night rituals, manual sauna log, season-pass habits, cycle-aware coaching. | Monthly & yearly medal tiers, year-over-year sparkline, CA coverage map, upcoming-trip pins. |

---

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│                   GitHub Actions (cron)                    │
│   6am · 12pm · 6pm · 10pm Pacific — 4 slots/day            │
│   sync.py → Oura / Garmin / Strava / GCal → Postgres       │
└──────────────────────────┬─────────────────────────────────┘
                           │ writes
                           ▼
              ┌─────────────────────────┐
              │  Neon Postgres          │      ┌──────────────┐
              │  daily_entries · rides  │ ◄────┤  Phone / web │
              │  season_pass · state    │      │  (HTTPS)     │
              └──────────┬──────────────┘      └──────▲───────┘
                         │ reads                      │
                         ▼                            │
              ┌─────────────────────────┐             │
              │  Render (Flask app)     │ ────────────┘
              │  / · /dashboard · /rides│
              │  /api/collect · manual  │
              └─────────────────────────┘
```

## Tech stack

| Layer        | Tool                                                |
|--------------|-----------------------------------------------------|
| Web          | Flask · Gunicorn · Jinja-free string-template HTML  |
| Data         | Postgres (Neon) via `psycopg[binary]` 3.x           |
| Cron         | GitHub Actions (4 schedules/day + manual dispatch)  |
| Hosting      | Render (free tier)                                  |
| Integrations | Oura v2 · Garmin Connect · Strava v3 · Google APIs  |
| Frontend     | Vanilla HTML/CSS/JS — no framework, no build step   |
| Observability| Render logs + `/healthz` + `/api/health`            |

The dashboard is deliberately **framework-free**: one `<style>` block,
one `<script>` block, zero npm. The whole site is ~100 KB of HTML/CSS/JS
per page, rendered server-side from Postgres rows.

## Features

**Quest Hub**
- Weekly pulse: live star tally, 3-circle today slots (morning/core/night), day bubbles with per-day detail modal
- Daily Quest: 4-item morning ritual, 7-item core missions with live progress ("need 2 more to earn ⭐"), 4-item night ritual, explicit threshold hints
- Manual toggles (sauna / steam) — tap to save
- Season Pass: monthly habit checklist backed by DB
- Pillar Health accordion: annual anchors from a Google Docs habit tracker
- Cycle-phase aware coaching line (Follicular / Ovulation / Luteal)

**Ride Atlas**
- Monthly pulse with Bronze / Silver / Gold tiers, weekly breakdown
- Year-at-a-glance with compact past-months-only layout
- Year-over-year sparkline across up to 5 years
- California coverage map — every ridden location clustered + geocoded
- Upcoming rides from a Google-Sheets Travel Planner
- Region cards grouped by area, linking to Strava

**Operational**
- Stars / sauna / rituals saved **optimistically** — UI flips immediately, server confirms in the background
- Sync is idempotent: safe to re-run the same day without overwriting collected stars
- Timezone-pinned (America/Los_Angeles) so "today" always matches the user's local wall clock, not whatever the runner's timezone happens to be
- Graceful degradation if Oura / Garmin / Strava / Google temporarily fails — the dashboard still renders, stale fields just stay stale

## Repo layout

```
.
├── app.py                 # Flask entrypoint
├── sync.py                # Daily sync orchestrator (GitHub Actions cron)
├── db.py                  # Postgres access layer
├── data_gather.py         # Builds the dict the templates render
├── api_clients.py         # Oura + Garmin wrappers
├── strava_fetch.py        # Strava OAuth + ride fetch
├── cycle.py               # Menstrual-cycle phase calc
├── scoring.py             # Daily star logic
├── constants.py           # Goals, thresholds, row layouts
├── sheets.py              # Google Sheets + Drive + Calendar + Docs
├── travel_source.py       # Travel Master Planner reader
├── habit_source.py        # Habit tracker doc reader
├── html_report.py         # Quest Hub HTML renderer
├── rides_report.py        # Ride Atlas HTML renderer
├── tz.py                  # Pacific-time helpers
├── templates/
│   ├── morning_report.html  # Quest Hub template
│   └── rides.html           # Ride Atlas template
├── migrations/
│   └── 001_initial_schema.sql
├── scripts/
│   └── smoke.sh           # End-to-end smoke test
├── .github/workflows/
│   └── sync.yml           # Cron workflow
├── render.yaml            # Render blueprint
├── Procfile
└── requirements.txt
```

## Local development

```bash
# Python 3.12
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# .env expected keys:
#   DATABASE_URL     - postgres://... (local docker or Neon)
#   OURA_TOKEN
#   GARMIN_EMAIL / GARMIN_PASSWORD
#   STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET / STRAVA_REFRESH_TOKEN
#   GOOGLE_TOKEN_JSON  - OAuth token JSON (single line)

# Apply schema
psql "$DATABASE_URL" -f migrations/001_initial_schema.sql

# Manual sync (same code as the GitHub Actions cron runs)
python sync.py --morning --force

# Run the web app locally
python app.py          # http://localhost:8000/dashboard
```

## Deployment

- **Web app** deploys to Render from `main` via `render.yaml`. Secrets
  (DATABASE_URL, OURA_TOKEN, etc.) are set in the Render dashboard and
  never live in the repo.
- **Cron** runs in GitHub Actions via `.github/workflows/sync.yml`;
  the same secrets live in repo Actions secrets. Manual dispatch is
  always available from the Actions tab.
- **Database** is a single free-tier Neon Postgres project in
  `us-west-2` (matches Render's Oregon region so p50 query latency
  is a couple ms).

`scripts/smoke.sh <base-url>` exercises every route end-to-end and
exits non-zero on failure; run it after a deploy.

## Design notes

- **`sync` is the source of truth.** External APIs are pulled into
  Postgres on a schedule; every rendered page is a snapshot of
  `daily_entries` + `rides`. That clean boundary makes failures easy to
  reason about: if the dashboard is wrong, check the DB; if the DB is
  wrong, check the last sync log.
- **Optimistic UI everywhere.** Tapping a star flips the UI
  immediately and fires an `/api/collect` POST in the background; if
  the POST fails the UI reverts and a red toast explains why.
- **No stale HTML.** Every `/dashboard` hit re-renders from fresh
  rows. Neon is fast enough (<100 ms typical) that a cache isn't
  worth the complexity.
- **One timezone.** `tz.local_today()` pins everything to Pacific so
  a cron run at 02:00 UTC on Tuesday still writes to Monday's row
  because it's still Monday evening in Pacific.

## License

Personal project. No license — code is public for portfolio
purposes but not licensed for reuse.
