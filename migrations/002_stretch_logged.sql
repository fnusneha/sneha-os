-- Sneha.OS — manual Stretch toggle
--
-- Stretch becomes a manual one-tap toggle (parallel to `sauna`) instead
-- of an auto-detected Garmin field. Past rows that already have a
-- stretch_note (from the old auto-detection path) are backfilled to
-- stretch_logged = TRUE so historic Recover stars stay correct.

ALTER TABLE daily_entries
    ADD COLUMN IF NOT EXISTS stretch_logged BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE daily_entries
   SET stretch_logged = TRUE
 WHERE stretch_note IS NOT NULL
   AND stretch_note <> ''
   AND stretch_logged = FALSE;
