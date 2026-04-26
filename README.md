# Sneha.OS

> A personal fitness operating system. One cloud backend, two ways to
> use it: **a web URL anyone can open** and **a native Android app**
> that adds home-screen widgets and a nightly ritual reminder.

**Live web:** <https://sneha-os.onrender.com/dashboard>
**Install Android APK:** <https://github.com/fnusneha/sneha-os/releases/download/android-latest/sneha-os.apk>
(auto-built from `main` by GitHub Actions — always the newest commit)

---

## What is this? (for non-technical friends)

I was filling in a fitness spreadsheet every morning. Then I
automated that. Then I rebuilt it as a proper product.

Every morning the dashboard knows:

- How I slept last night (from my Oura ring)
- How many steps I've walked today (from Garmin)
- How many calories I've eaten (Garmin / MyFitnessPal)
- What's happening in my menstrual cycle (period-tracking calendar)
- Which rides I did this weekend (Strava)
- What's on my calendar this week (birthdays, trips, appointments)

It shows me **⭐ stars for the 3 rituals I'm trying to keep** (morning
routine, core missions, night wind-down), nudges me when I'm behind,
and lights up a California map with every place I've ever cycled.

**Two surfaces, one product:**

| Surface | What it is | Best for |
| --- | --- | --- |
| **Web URL** | <https://sneha-os.onrender.com/dashboard> | iPhones, browsers, friends, anywhere |
| **Android app** | APK installed on my Pixel | Home-screen widget + nightly 10 PM push |

Both show **exactly the same UI** because the app is a thin shell
around the web URL. Whatever I change on the server instantly appears
on both. No duplicate code to maintain.

---

## Architecture (for developers)

```
                        ┌──────────────────────────────────────┐
                        │      GitHub Actions (cron)           │
                        │  6am · 12pm · 6pm · 10pm Pacific     │
                        │  sync.py → Oura / Garmin / Strava /  │
                        │           Google Calendar / Docs     │
                        └──────────────┬───────────────────────┘
                                       │ writes
                                       ▼
                      ┌───────────────────────────────────────┐
                      │  Neon Postgres (source of truth)      │
                      │   daily_entries · rides · season_pass │
                      │   0.5 GB free tier                    │
                      └──────────────┬────────────────────────┘
                                     │ reads
                                     ▼
                      ┌───────────────────────────────────────┐
                      │  Render web service (Flask)           │
                      │   /dashboard · /rides                 │
                      │   /api/collect · /api/manual          │
                      │   /api/today  ←── for Android widget  │
                      └──────────────┬────────────────────────┘
                                     │ HTTPS
              ┌──────────────────────┼───────────────────────┐
              ▼                      ▼                       ▼
        ┌──────────┐          ┌────────────────┐    ┌───────────────────┐
        │ Browser  │          │ Android app    │    │  Home-screen      │
        │ (iPhone, │          │ (Pixel 6)      │    │  widget           │
        │  Mac,    │          │                │    │  + 10 PM push     │
        │  etc.)   │          │  WebView over  │    │  (consume         │
        │          │          │  the same URL  │    │   /api/today)     │
        └──────────┘          └────────────────┘    └───────────────────┘
```

### Key design choices

- **`sync.py` is the only writer.** Every `/dashboard` response is a
  stateless snapshot of DB rows. If the UI is wrong, check the DB; if
  the DB is wrong, check the last sync log. That bright-line boundary
  made all cascade bugs go away.
- **One codebase for UI.** The Android app is ~500 lines of Kotlin —
  WebView, widget, notification. **Zero** UI or business logic
  duplicated. Web and app can never drift.
- **Native where it matters.** The widget and 10 PM reminder **can't**
  exist in a web app, so those are the only pieces of real Android
  code. Everything else is the web.
- **Idempotent sync.** Re-running for the same day never clobbers a
  collected ⭐, a manually ticked sauna, or a manually ticked stretch.
- **Timezone-pinned to Pacific.** Cron runs in UTC; `tz.local_today()`
  translates so "today" always matches the wall clock in California.
- **Graceful degradation.** If Oura / Garmin / Strava / Google flakes,
  the dashboard still renders — stale fields stay stale instead of
  blank.
- **Two layers of cold-start defence.** Render free tier naps after
  15 min of no traffic. A GitHub Actions keep-warm job pings
  `/healthz` every 10 min between 7am–11pm Pacific so the container
  stays awake during waking hours, and the Android WebView auto-retries
  failed loads (10/20/30/30 s backoff, ~90 s budget) so a cold start
  never lands the user on a permanent error page.

### Tech stack

| Layer          | Tool                                               |
| -------------- | -------------------------------------------------- |
| Web backend    | Flask · Gunicorn · Python 3.12                     |
| Data           | Postgres (Neon) via `psycopg[binary]` 3.x          |
| Cron           | GitHub Actions (4 slots/day + manual dispatch)     |
| Hosting        | Render free tier                                   |
| Integrations   | Oura v2 · Garmin Connect · Strava v3 · Google APIs |
| Frontend       | Vanilla HTML/CSS/JS — no framework, no build step  |
| Android        | Kotlin 2.0.21 · AGP 8.7.3 · Glance · WorkManager   |
| Observability  | Render logs · `/healthz` · `scripts/smoke.sh`      |

The web front-end is deliberately framework-free: one `<style>` block,
one `<script>` block, zero npm. ~100 KB per page, rendered server-side
from Postgres rows.

---

## What's on each page

### Quest Hub (`/dashboard`)

- **Weekly Pulse** — live ⭐ tally, 3-circle today slots
  (morning / core / night), day bubbles with a per-day detail modal
- **Week Agenda** — notable calendar events for Mon–Sun pulled from
  Google Calendar (big appointments, travel, expos — the stuff you'd
  want to glance at when planning workouts around life)
- **Daily Quest** — 4-item morning ritual, 7-item core missions with
  live progress ("need 2 more to earn ⭐"), 4-item night ritual
- **Manual toggles** — stretch and sauna / steam, tap to save
- **Season Pass** — monthly habit checklist (DB-backed)
- **Pillar Health** — annual anchors pulled from a Google Docs habit
  tracker
- **Cycle-phase coaching** — Follicular / Ovulation / Luteal etc.

### Ride Atlas (`/rides`)

- Monthly pulse with Bronze / Silver / Gold tiers, weekly breakdown
- Year-at-a-glance with compact past-months layout
- Year-over-year sparkline across up to 5 years
- California coverage map — every ridden location geocoded + clustered
- Upcoming rides / trips from a Google Sheets Travel Planner
- Region cards linking out to Strava

---

## Repo layout

```
.
├── app.py                 # Flask entrypoint (routes + health)
├── sync.py                # Cron: external APIs → Postgres
├── db.py                  # Postgres access layer (only SQL lives here)
├── data_gather.py         # Postgres rows → report_data dict
├── api_clients.py         # Oura + Garmin + Google Calendar
├── strava_fetch.py        # Strava OAuth + ride fetch
├── google_auth.py         # Google OAuth helper (Sheets/Drive/Calendar)
├── travel_source.py       # Travel Master Planner sheet reader
├── habit_source.py        # Habit tracker Google Doc reader
├── cycle.py               # Menstrual cycle phase calc
├── scoring.py             # Daily star logic
├── constants.py           # Goals, thresholds, scopes
├── tz.py                  # Pacific-time helpers
├── html_report.py         # Quest Hub HTML renderer
├── rides_report.py        # Ride Atlas HTML renderer
├── templates/
│   ├── morning_report.html
│   └── rides.html
├── migrations/
│   └── 001_initial_schema.sql
├── android/               # Kotlin app (WebView + widget + notification)
│   ├── app/src/main/kotlin/os/sneha/
│   │   ├── MainActivity.kt
│   │   ├── data/SnehaApi.kt            # OkHttp + Moshi for /api/today
│   │   ├── widget/TodayWidget.kt       # Glance home-screen widget
│   │   └── notification/NightReminderWorker.kt
│   └── …
├── .github/workflows/
│   ├── sync.yml                        # 4×/day Oura/Garmin/Strava pull
│   ├── keepwarm.yml                    # ping /healthz during active hours
│   └── android.yml                     # auto-build APK on push to main
├── scripts/smoke.sh                    # End-to-end smoke
├── render.yaml · Procfile · requirements.txt
```

---

## Local development

```bash
# Python 3.12
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# .env expected keys:
#   DATABASE_URL        postgres://… (Neon or local docker)
#   OURA_TOKEN
#   GARMIN_EMAIL / GARMIN_PASSWORD
#   STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET / STRAVA_REFRESH_TOKEN
#   GOOGLE_TOKEN_JSON   OAuth user token (single-line JSON)

# First-time DB setup (run all migrations in order)
psql "$DATABASE_URL" -f migrations/001_initial_schema.sql
psql "$DATABASE_URL" -f migrations/002_stretch_logged.sql

# Pull today's data
python sync.py --morning --force

# Run the web app locally
python app.py          # http://localhost:8000/dashboard
```

### Installing the Android app

**For normal use — bookmark this URL on your Pixel:**

<https://github.com/fnusneha/sneha-os/releases/download/android-latest/sneha-os.apk>

Every push to `main` that touches `android/` triggers
`.github/workflows/android.yml`, which builds a debug APK and
overwrites the file at the URL above (~3 min end-to-end). Tap the
bookmark → download → install. First install Android asks to allow
"install from unknown sources" for your browser; every update after
that is one tap.

The running app reports its build SHA in two places:

- `adb logcat | grep SnehaOS` on the first line after launch
- The footer of the offline fallback page (when the backend is down)

So any bug report can be pinned to the exact commit that produced the
APK. No Play Store needed — this is a single-user personal app.

**For local development:**

```bash
cd android
export JAVA_HOME=…/corretto-17.0.12/Contents/Home
./gradlew :app:assembleDebug                     # prod backend
./gradlew :app:assembleDebug -PbaseUrl=http://10.0.2.2:8000   # local backend
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

---

## Deployment

One push to `main` refreshes **both** surfaces from the same commit:

```
git push origin main
         │
         ├─► Render auto-deploys web           (~90 s)
         │      https://sneha-os.onrender.com/dashboard
         │
         └─► GitHub Actions builds APK         (~3 min)
                https://github.com/fnusneha/sneha-os/
                    releases/download/android-latest/sneha-os.apk
```

Both paths are fully hands-off. Bookmark the APK URL on your phone and
opening it always pulls the newest build — you can never be "stuck on
an old version" while filing bugs against a newer web UI.

| Piece         | Where                                            | Trigger                      |
| ------------- | ------------------------------------------------ | ---------------------------- |
| Web app       | Render (free tier) via `render.yaml`             | push to `main`               |
| Data sync     | GitHub Actions `.github/workflows/sync.yml`      | 4×/day + manual              |
| Keep-warm     | GitHub Actions `.github/workflows/keepwarm.yml`  | every 10 min, 7am–11pm PT    |
| Android APK   | GitHub Actions `.github/workflows/android.yml`   | push to `main` (android/\*\*) |
| APK hosting   | GitHub Release tagged `android-latest`           | overwritten per build        |
| Database      | Neon free Postgres (`us-west-2`)                 | always-on                    |

All secrets live in Render env + GitHub Actions secrets. **Nothing**
sensitive is in the repo — `.env`, `token.json`, `credentials.json`,
`.garmin_tokens/` are all gitignored.

After a deploy: `scripts/smoke.sh https://sneha-os.onrender.com`
exercises every route end-to-end and exits non-zero on failure.

---

## License

Personal project. Public for portfolio; not licensed for reuse.
