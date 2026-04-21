# Sneha.OS

A single-user personal fitness operating system. Pulls data from Oura, Garmin, Strava, and Google Calendar; surfaces it on a mobile-friendly web UI (Quest Hub + Ride Atlas). Mac-independent, zero-cost hosting.

**Repo:** `sneha-os` · **Live URL:** `https://sneha-os.onrender.com` *(coming up)*

## Current stack (target state — migration in progress)

```
┌──────────────────────────────────────────────────────────────┐
│                     GitHub Actions (cron)                    │
│   6am · 12pm · 6pm · 10pm daily                              │
│   sync.py → Oura/Garmin/Strava/GCal → Postgres               │
└──────────────────────────┬───────────────────────────────────┘
                           │
                           ▼
                ┌──────────────────────┐
                │  Neon Postgres       │       ┌─────────────────┐
                │  daily_entries       │◄──────┤  Phone browser  │
                │  rides               │       │  (HTTPS)        │
                │  season_pass         │       └────────▲────────┘
                └──────────┬───────────┘                │
                           │                            │
                           ▼                            │
                ┌──────────────────────┐                │
                │  Render web service  │────────────────┘
                │  Flask (app.py)      │
                │  /dashboard /rides   │
                │  /api/collect        │
                │  /api/manual         │
                └──────────────────────┘
```

No laptop. No Tailscale. No Google Sheets as a data store. Entirely free tier.

## Data sources

| Data               | Source                       | Frequency |
|--------------------|------------------------------|-----------|
| Sleep              | Oura `/sleep`                | Every sync |
| Steps              | Oura `/daily_activity`       | Every sync |
| Cycle phase        | Oura + Google Calendar       | Every sync |
| Calories           | Garmin Connect (MFP mirror)  | Every sync |
| Strength / Cardio  | Garmin activities            | Every sync |
| Rides              | Strava API                   | 2x daily   |
| Notes / Trips      | Google Calendar              | Every sync |
| Habits (annual)    | Google Docs (habit tracker)  | Daily      |
| Travel pins        | Google Sheets (read-only)    | 6h cache   |
| **Sauna**          | **Manual toggle in mobile UI** | On tap   |
| **Morning/Night**  | **Manual collect in mobile UI** | On tap  |

## Repo layout

```
fitness-automation/
├── app.py                 # Flask entrypoint (Render web service)
├── sync.py                # Daily sync orchestrator (GitHub Actions cron)
├── db.py                  # psycopg3 connection + row mappers
├── migrate.py             # One-shot: Google Sheets → Postgres backfill
├── api_clients.py         # Oura + Garmin wrappers (preserved from v1)
├── strava_fetch.py        # Strava OAuth + ride fetch
├── cycle.py               # Cycle-phase calc (preserved)
├── constants.py           # Goals, thresholds, phase definitions
├── scoring.py             # Daily-star logic
├── html_report.py         # Quest Hub HTML renderer
├── rides_report.py        # Ride Atlas HTML renderer
├── tz.py                  # Pacific-time helpers (local_today, local_now)
├── travel_source.py       # Travel Master Planner reader
├── habit_source.py        # Habit tracker doc reader
├── templates/
│   ├── morning_report.html  # Quest Hub template
│   └── rides.html           # Ride Atlas template
├── render.yaml            # Render deploy blueprint
├── .github/workflows/
│   └── sync.yml           # Cron: run sync.py on schedule
├── requirements.txt
├── .gitignore
└── README.md              # You are here
```

## Branch workflow

- `main` is protected. All changes go through a PR.
- Conventional commits: `feat:`, `fix:`, `chore:`, `refactor:`, `docs:`.
- Render auto-deploys `main` on merge; preview environments on PRs.

## Local development

```bash
# First time
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set DATABASE_URL in .env (Neon connection string)
# Set OURA_TOKEN, GARMIN_EMAIL/PASSWORD, STRAVA_* tokens

# Manual sync (same data flow as the cron)
python sync.py --morning --force
python sync.py --date 2026-04-20

# Run the web app locally
python app.py  # serves http://localhost:8000/dashboard

# One-shot migration from old Sheets
python migrate.py --source-spreadsheet <id> --month 2026-04
```

## Secrets

Never committed. Stored in:
- `.env` (local dev only — gitignored)
- Render env vars (production runtime)
- GitHub Actions repo secrets (cron runtime)

Required: `OURA_TOKEN`, `GARMIN_EMAIL`, `GARMIN_PASSWORD`, `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN`, `DATABASE_URL`, `GOOGLE_CREDS_JSON`, `GARMIN_TOKENS_TARBALL_B64`.

## Why this rewrite

The v1 used Google Sheets as a write-heavy data store, ran Python scripts via macOS launchd, and exposed a Node MCP server via Tailscale funnel. It worked, but:

- The laptop had to be on + awake + MCP server healthy for the mobile URL to load.
- Google Sheets hit write rate limits, silently dropped writes into merged cells, and (bug) accidentally created 50 orphan spreadsheets in Drive root.
- ~6,700 lines of Python + ~1,000 lines of Node was mostly plumbing around Sheets quirks.

The new stack is ~1/3 the code, has zero laptop dependency, and costs $0.

## Migration plan

See `.claude/plans/playful-sniffing-tiger.md` for the full migration plan and current step.
