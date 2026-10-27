-- Migration 003: Calendar provider plugin architecture
--
-- BREAKING CHANGE: Replaces the local CalDAV/vdirsyncer calendar sync system
-- with a direct API provider plugin architecture (Google, Outlook, Zoho).
--
-- Changes to calendar_managed_events:
--   ADD:  provider_name    — which provider owns this event (e.g. 'google')
--   ADD:  provider_event_id — the provider's native event ID
--   ADD:  calendar_id      — the provider-specific calendar collection ID
--   DROP: uid              — ICS UID (file-system specific, no longer used)
--   DROP: relative_path    — local .ics file path (no longer used)
--   DROP: file_hash        — SHA-256 of .ics content (no longer used)
--
-- Existing rows (created by the old CalDAV gateway) are preserved with
-- status='deleted' and provider_name='legacy' so audit history is retained.
-- They cannot be updated or deleted through the new router.
--
-- This file is safe to run against both the pre-rebuild table (with uid)
-- and a database already created from the current schema.sql (no uid).

-- Step 1: Mark all existing active events as 'legacy_deleted' so they don't
-- interfere with the new provider-based lookup, while retaining audit trail.
UPDATE calendar_managed_events
SET status = 'deleted',
    deleted_at = now(),
    updated_at = now()
WHERE status = 'active';

-- Step 2: Add new provider columns (nullable initially for migration safety).
ALTER TABLE calendar_managed_events
  ADD COLUMN IF NOT EXISTS provider_name    text,
  ADD COLUMN IF NOT EXISTS provider_event_id text,
  ADD COLUMN IF NOT EXISTS calendar_id      text;

-- Step 3: Backfill provider info for legacy rows.
-- uid is referenced only via dynamic SQL so this still works after the
-- column has already been dropped (or never existed on a fresh install).
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'calendar_managed_events'
      AND column_name = 'uid'
  ) THEN
    EXECUTE $sql$
      UPDATE calendar_managed_events
      SET provider_name     = 'legacy',
          provider_event_id = COALESCE(uid, assistant_id),
          calendar_id       = COALESCE(NULLIF(calendar_name, ''), 'default')
      WHERE provider_name IS NULL
    $sql$;
  ELSE
    EXECUTE $sql$
      UPDATE calendar_managed_events
      SET provider_name     = COALESCE(provider_name, 'legacy'),
          provider_event_id = COALESCE(provider_event_id, assistant_id),
          calendar_id       = COALESCE(calendar_id, NULLIF(calendar_name, ''), 'default')
      WHERE provider_name IS NULL
         OR provider_event_id IS NULL
         OR calendar_id IS NULL
    $sql$;
  END IF;
END $$;

-- Step 4: Set NOT NULL constraints now that all rows are populated.
ALTER TABLE calendar_managed_events
  ALTER COLUMN provider_name     SET NOT NULL,
  ALTER COLUMN provider_event_id SET NOT NULL,
  ALTER COLUMN calendar_id       SET NOT NULL;

-- Step 5: Add index for provider-based lookups.
CREATE INDEX IF NOT EXISTS calendar_managed_events_provider_idx
  ON calendar_managed_events(provider_name, provider_event_id);

-- Step 6: Drop old file-system-specific columns.
-- calendar_name is kept temporarily (used in backfill above; harmless to retain).
ALTER TABLE calendar_managed_events
  DROP COLUMN IF EXISTS uid,
  DROP COLUMN IF EXISTS relative_path,
  DROP COLUMN IF EXISTS file_hash;
