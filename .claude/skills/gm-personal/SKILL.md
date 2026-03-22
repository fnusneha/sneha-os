---
name: gm-personal
description: Sync Oura Ring fitness data (sleep, steps, cycle phase) to Google Sheets. Use when the user says "good morning", "gm", "morning sync", "run the sync", or wants to check their fitness data. Also triggers on /gm-personal.
---

# Good Morning — Personal Fitness Sync

Syncs Oura Ring data to the weekly accountability Google Sheet.

## When to trigger

Any of: "good morning", "gm", "morning sync", "run the sync", "sync my data", "fitness sync", or `/gm-personal`.

## Steps

### 1. Run the sync

```bash
cd /Users/sneha.rana/fitness-automation && source .venv/bin/activate && python3 oura_sheets_sync.py --morning --force
```

### 2. Show the report

After running the command, immediately show the user the formatted report from the output (everything after the log lines — starting with the morning greeting). Copy-paste the report section into your response text. The user needs to see their fitness data right away. Do not skip or summarize it.

### 3. Handle errors

If the sync fails:
- Show the error clearly
- Suggest `--date YYYY-MM-DD` as a fallback for a specific date
- Check if `.venv` exists and dependencies are installed if it's a Python error
