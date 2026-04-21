# Sneha.OS — How to use it

Your personal fitness operating system is **fully cloud-hosted**. It runs whether your laptop is on, off, asleep, or at the bottom of a lake.

## The URL

**📱 https://sneha-os.onrender.com/dashboard**

Bookmark it. Add it to your phone's home screen (iOS: Share → "Add to Home Screen"). That's your only URL.

## How your day works starting tomorrow

```
6 AM   → GitHub Actions runs sync.py automatically (behind the scenes).
         Oura uploads last night's sleep + yesterday's final step count.
         Garmin uploads calories/workouts.
         Neon database gets updated.

9 AM   → You open the dashboard on your phone.
         First load after idle: 30–60 sec cold start (free tier nap).
         Subsequent loads: instant.
         You see fresh sleep + steps + cycle phase + yesterday's totals.

         Tap ⭐ "Morning Ritual" collect → saves to Neon immediately.

12 PM  → Actions runs again. Fresh mid-day step count appears.

3 PM   → You check dashboard from Starbucks. Cold-start for ~40 sec
         (Render slept since morning). Then live. Tap sauna toggle
         if you did a sauna session.

6 PM   → Actions runs. Strava rides fetched too. Dinner calories appear
         after Garmin/MFP syncs (usually within 30 min of logging).

10 PM  → Actions runs one last time. Tap 🌙 "Night Ritual" collect.

Sleep → Oura records overnight → ready for tomorrow.
```

You never manually sync anything. You never touch a terminal. The only interactions are **tap to collect stars**, **tap the sauna toggle**, and **refresh the dashboard to see new numbers**.

## What's a "workflow"?

A GitHub Actions **workflow** is a YAML file (`.github/workflows/sync.yml`) that runs on schedule or manually. Our workflow:

- **Name:** `sync`
- **Schedule:** 4 times a day (6am / 12pm / 6pm / 10pm Pacific)
- **Job:** Spins up a fresh Ubuntu VM, installs Python, runs `python sync.py --morning --force`, which pulls from Oura/Garmin/Strava/GCal and writes to Neon.

**You can watch it run live:**
- Go to https://github.com/fnusneha/sneha-os/actions
- Click the most recent `sync` run to see logs — each step is expandable.
- Green check = success. Red X = fail (I'll help debug).

**You can trigger it manually anytime:**
- Actions tab → click `sync` in left sidebar → **Run workflow** button top right → Run workflow.
- Useful if you just came home from a ride and want fresh data without waiting for the next scheduled slot.

## How to see the raw data in the database

Three ways, easy to hard:

### 1. Dashboard (what you'll use 99% of the time)
https://sneha-os.onrender.com/dashboard — the whole point. No DB knowledge needed.

### 2. Neon web console (spreadsheet-style view of raw rows)
1. Go to https://console.neon.tech
2. Log in → select the `sneha-os` project
3. Left sidebar → **SQL Editor**
4. Run queries like:
   ```sql
   -- See the last 10 days of everything
   SELECT date, sleep_hours, steps, calories, cycle_phase, sauna, morning_star
   FROM daily_entries
   ORDER BY date DESC LIMIT 10;

   -- How many stars have I collected in April?
   SELECT COUNT(*) FILTER (WHERE morning_star) AS mornings,
          COUNT(*) FILTER (WHERE night_star)   AS nights,
          COUNT(*) FILTER (WHERE sauna)        AS saunas
   FROM daily_entries WHERE date LIKE '2026-04-%';

   -- Total mileage this year
   SELECT COUNT(*), SUM(distance_mi) FROM rides WHERE year = 2026;
   ```
5. It's a real SQL shell. Read-only queries are safe. UPDATE/DELETE will actually mutate the real data, so be careful.

### 3. Local CLI (requires dev setup)
From your Mac:
```bash
cd /Users/sneha.rana/fitness-automation
source .venv/bin/activate
python db.py health              # row counts
python db.py get 2026-04-20      # full row for a date
```

## What to do when something goes wrong

### Dashboard won't load / stuck spinning
- Cold start is **up to 60 seconds**. Wait it out once.
- If >2 min with no response, check Render status: https://dashboard.render.com → `sneha-os` → is service "Live" (green)? If "Failed" or "Crashed," click **Logs**.

### Data looks stale
- Check last-sync: https://sneha-os.onrender.com/api/health — look at `last_sync_date`. If >24h old, Actions cron may have failed.
- Go to https://github.com/fnusneha/sneha-os/actions and check last `sync` run.
- Force a refresh: trigger the workflow manually (see "workflow" section above).

### Oura / Garmin / Strava token expired
- Oura personal access tokens don't expire but can be revoked.
- Strava refresh tokens auto-refresh as long as the sync runs at least every 60 days.
- Garmin can force MFA if Garmin flags unusual activity — you'd see Actions failing with a login error. Fix is to log into Garmin normally once from Mac, then copy new token files into GitHub/Render secrets (`GARMIN_OAUTH1_TOKEN`, `GARMIN_OAUTH2_TOKEN`). Ping me if this happens.

### Star tap didn't save
- Bottom of screen usually shows a ⚠️ error toast on failure.
- If it did save but doesn't show: refresh the dashboard (pull-to-refresh).

## Secrets / credentials (for reference)

You have **two places** that need secrets: Render and GitHub Actions. Both currently have 8 matching secrets:

| Name | What it's for |
|---|---|
| `DATABASE_URL` | Neon connection string |
| `OURA_TOKEN` | Oura Ring API |
| `GARMIN_EMAIL`, `GARMIN_PASSWORD` | Garmin login |
| `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN` | Strava OAuth |
| `GOOGLE_TOKEN_JSON` | Google Calendar + Docs reads |

Optional (if Garmin starts demanding MFA in CI):
- `GARMIN_OAUTH1_TOKEN` — paste contents of `~/fitness-automation/.garmin_tokens/oauth1_token.json`
- `GARMIN_OAUTH2_TOKEN` — paste contents of `oauth2_token.json`

**Update in Render:** Dashboard → `sneha-os` → Environment → edit a value → Save → Manual Deploy.
**Update in Actions:** https://github.com/fnusneha/sneha-os/settings/secrets/actions → edit secret.

## Timezone notes

Everything runs in **Pacific time** (`APP_TIMEZONE` env var, defaults to `America/Los_Angeles`). If you travel, the dashboard stays on PT — so a Tuesday evening in New York still shows as Tuesday, not Wednesday.

If you ever move timezones permanently, change `APP_TIMEZONE` in Render + Actions env.

## Rollback

Mac still has every v1 file (Google Sheets are untouched). If Render/Neon ever fails catastrophically:
```bash
# Re-enable old Mac jobs (they were unloaded, not deleted... until we deleted the plists)
# Rollback path: git checkout the pre-migration commit in a new dir and run the old way.
```
But honestly the new setup is simpler and won't break. Just keep the `sneha-os` repo in GitHub.

## What to do NOW (before tomorrow)

1. **Bookmark** https://sneha-os.onrender.com/dashboard on your phone.
2. **Add to Home Screen** (iOS Share menu) so it launches like an app.
3. Open the dashboard tonight. If cold start, wait 60s. Verify it shows today's 4,505 steps + sauna ✓.
4. Tomorrow 9 AM: open it again. Should show overnight sleep + fresh morning steps.

That's the whole workflow.
