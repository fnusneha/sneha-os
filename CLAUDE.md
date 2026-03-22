# Fitness Automation

Personal project that syncs Oura Ring data to a weekly Google Sheets accountability spreadsheet.

## What this does

- Pulls sleep hours, step count, and cycle phase from Oura Ring API v2
- Writes to the correct weekly tab and day column in Google Sheets
- Runs automatically at 6 AM daily via macOS launchd
- Supports backfill mode (`--morning --force`) and single-date mode (`--date YYYY-MM-DD`)

## Key files

- `oura_sheets_sync.py` — the entire sync logic (single file)
- `.env` — Oura token + Google Sheet ID (never commit)
- `service_account.json` — Google Cloud service account key (never commit)
- `com.sneha.oura-sync.plist` — launchd config for scheduled runs

## Running

```bash
# Activate venv and run
source .venv/bin/activate
python3 oura_sheets_sync.py --morning --force   # backfill missed days
python3 oura_sheets_sync.py --date 2026-03-10   # specific date
python3 oura_sheets_sync.py --steps-left         # weekly steps report
```

## Notes

- This is a **personal** project, separate from work (WeatherBug)
- Sundays are skipped (no column in the sheet)
- The script never overwrites existing data if Oura returns nothing
- Cycle phase endpoint (`/v2/usercollection/daily_cycle`) may return 404 depending on firmware/subscription — handled gracefully
