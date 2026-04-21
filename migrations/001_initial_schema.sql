-- Sneha.OS v2 — initial schema
--
-- Single-user personal fitness dashboard. Replaces the weekly-tab Google
-- Sheet from v1. One row per day in `daily_entries`, one row per Strava
-- ride in `rides`, monthly-keyed season pass, and a KV `sync_state` for
-- last-sync timestamps etc.
--
-- Runs idempotently: safe to re-run (IF NOT EXISTS everywhere).

CREATE TABLE IF NOT EXISTS daily_entries (
    date            DATE PRIMARY KEY,
    -- From Oura
    sleep_hours     NUMERIC(3,1),
    steps           INTEGER,
    -- From Garmin
    calories        INTEGER,
    calorie_goal    INTEGER,
    strength_note   TEXT,
    cardio_note     TEXT,
    stretch_note    TEXT,
    -- From Oura + Google Calendar
    cycle_phase     TEXT,
    cycle_day       INTEGER,
    -- From Google Calendar
    notes           TEXT,
    -- Manual toggles (from mobile UI)
    sauna           BOOLEAN NOT NULL DEFAULT FALSE,
    morning_star    BOOLEAN NOT NULL DEFAULT FALSE,
    night_star      BOOLEAN NOT NULL DEFAULT FALSE,
    -- Client-side checklist state (JSON for forward-compat)
    morning_checks  JSONB NOT NULL DEFAULT '{}'::jsonb,
    night_checks    JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS season_pass (
    month           TEXT PRIMARY KEY,  -- "2026-04"
    done_indices    INTEGER[] NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rides (
    strava_id       BIGINT PRIMARY KEY,
    date            DATE NOT NULL,
    year            INTEGER NOT NULL,
    distance_mi     NUMERIC(8,2),
    elevation_ft    INTEGER,
    -- Full payload preserves whatever rides_report.py needs without
    -- forcing schema changes for each new field.
    payload         JSONB NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS rides_year_idx ON rides (year);
CREATE INDEX IF NOT EXISTS rides_date_idx ON rides (date DESC);

CREATE TABLE IF NOT EXISTS sync_state (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Auto-update updated_at on row change (nice to have for debugging stale rows)
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'daily_entries_touch') THEN
        CREATE TRIGGER daily_entries_touch BEFORE UPDATE ON daily_entries
            FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'season_pass_touch') THEN
        CREATE TRIGGER season_pass_touch BEFORE UPDATE ON season_pass
            FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'rides_touch') THEN
        CREATE TRIGGER rides_touch BEFORE UPDATE ON rides
            FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'sync_state_touch') THEN
        CREATE TRIGGER sync_state_touch BEFORE UPDATE ON sync_state
            FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
    END IF;
END $$;
