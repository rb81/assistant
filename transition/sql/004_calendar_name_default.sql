-- Migration 004: Ensure calendar_name has a default value
--
-- This migration hardens the calendar_managed_events table to ensure
-- calendar_name always has a safe default value, preventing null constraint
-- violations when the insert path omits this column.
--
-- Background: The router now explicitly sets calendar_name in inserts, but
-- this migration ensures older database schemas are brought up to the same
-- standard, and provides defense-in-depth for any future code paths.

-- Step 1: Backfill any null or empty calendar_name values with calendar_id
UPDATE calendar_managed_events
SET calendar_name = COALESCE(NULLIF(calendar_name, ''), calendar_id, 'unknown')
WHERE calendar_name IS NULL OR calendar_name = '';

-- Step 2: Ensure the column has a default value
ALTER TABLE calendar_managed_events
  ALTER COLUMN calendar_name SET DEFAULT '';

-- Step 3: Ensure NOT NULL constraint is in place
-- (This should already be set from the original schema, but we make it explicit)
ALTER TABLE calendar_managed_events
  ALTER COLUMN calendar_name SET NOT NULL;
