# Fitness Automation — Oura Ring to Google Sheets

Pulls daily sleep, steps, and cycle phase data from the Oura Ring API
and writes it to your weekly accountability Google Sheet. Runs automatically
at 6 AM every day via macOS launchd.

## What it does

| Data          | Source                  | Sheet row           | Columns  |
|---------------|-------------------------|---------------------|----------|
| Sleep (hrs)   | Oura `/sleep`           | Row 12 (Oura Sleep) | C-H (Mon-Sat) |
| Steps         | Oura `/daily_activity`  | Row 8 (Steps Goal)  | C-H (Mon-Sat) |
| Cycle Phase   | Oura `/daily_cycle`     | Row 13 (Cycle Phase)| C-H (Mon-Sat) |

- Skips Sundays (no column in the sheet)
- Never overwrites existing data if Oura returns nothing
- Logs every run to `logs/sync.log`

## Project structure

```
fitness-automation/
  oura_sheets_sync.py       # main script
  .env                      # secrets (Oura token, sheet ID)
  service_account.json      # Google service account key (gitignored)
  requirements.txt          # Python dependencies
  com.sneha.oura-sync.plist # launchd config for 6am daily runs
  logs/                     # daily run logs
  .gitignore
```

## Setup (already done)

These steps were completed during initial setup. Documented here for
reference if you need to recreate on a new machine.

### 1. Google Cloud service account

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project (e.g., `fitness-automation`)
3. Enable the **Google Sheets API** (APIs & Services > Library > Google Sheets API)
4. Create a service account (IAM & Admin > Service Accounts > Create)
5. On the service account, go to **Keys** tab > Add Key > Create new key > JSON
6. Save the downloaded JSON as `service_account.json` in this folder
7. Share your Google Sheet with the service account email as **Editor**:
   ```
   oura-sheets-sync@fast-academy-490421-i6.iam.gserviceaccount.com
   ```

### 2. Oura token

1. Go to https://cloud.ouraring.com/v2/docs
2. Create a Personal Access Token
3. Add it to `.env`:
   ```
   OURA_TOKEN=your_token_here
   ```

### 3. Python environment

```bash
cd ~/fitness-automation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. launchd (daily 6 AM trigger)

```bash
# Copy plist to LaunchAgents
cp com.sneha.oura-sync.plist ~/Library/LaunchAgents/

# Load the job
launchctl load ~/Library/LaunchAgents/com.sneha.oura-sync.plist

# Verify it's loaded
launchctl list | grep oura
```

## Manual run

```bash
# Run for yesterday (default)
.venv/bin/python3 oura_sheets_sync.py

# Run for a specific date
.venv/bin/python3 oura_sheets_sync.py --date 2026-03-10
```

## Manage the launchd job

```bash
# Stop the scheduled job
launchctl unload ~/Library/LaunchAgents/com.sneha.oura-sync.plist

# Restart after editing the plist
launchctl unload ~/Library/LaunchAgents/com.sneha.oura-sync.plist
cp com.sneha.oura-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.sneha.oura-sync.plist
```

## Check logs

```bash
# Application log
cat logs/sync.log

# launchd stdout/stderr
cat logs/launchd_stdout.log
cat logs/launchd_stderr.log
```

## Cycle phase note

The Oura `/v2/usercollection/daily_cycle` endpoint currently returns 404.
This may mean your Oura firmware or subscription doesn't expose this data
via the API yet. The script handles this gracefully — when cycle data
becomes available, it will automatically start writing phase info
(e.g., "Follicular (Day 7)") to row 13. No code changes needed.

The phase calculation uses the PMS Quick Guide ranges from your sheet:

| Days  | Phase              |
|-------|--------------------|
| 1-3   | Menstrual          |
| 4-13  | Follicular         |
| 14-16 | Ovulation          |
| 17-23 | Luteal (Early-Mid) |
| 24-28 | Luteal (PMS)       |
