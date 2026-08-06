-- HIPAA-EXCLUDE
-- Reason: backfill only resolves account_id via the Task → Project → Account
-- chain. HIPAA-flagged accounts have hipaa_excluded=TRUE on the Account row
-- which cascades to Projects and Tasks — no HIPAA records will match the JOIN.
--
-- One-time backfill: populate account_id on existing triaged_items rows
-- by joining through the Airtable Task → Project → Account chain.
--
-- Only rows that drafted an Airtable Task (airtable_task_record_id IS NOT NULL)
-- can be backfilled this way. Rows without a Task link stay NULL — acceptable
-- since those are typically non-actionable or do_now items.
--
-- Run AFTER the terraform apply that adds the account_id column.
-- DML UPDATE — BQ streaming-buffer rows (< ~90 min old) will be skipped.
--
-- Preview first:
--   Replace UPDATE...SET with SELECT t.item_id, t.triaged_at, p.account[SAFE_OFFSET(0)]

UPDATE `agency-brain-demo.agent_outputs.triaged_items` t
SET t.account_id = p.account[SAFE_OFFSET(0)]
FROM `agency-brain-demo.airtable_replica.tasks` task
JOIN `agency-brain-demo.airtable_replica.projects` p
  ON task.project[SAFE_OFFSET(0)] = p._airtable_record_id
WHERE t.account_id IS NULL
  AND t.airtable_task_record_id IS NOT NULL
  AND t.airtable_task_record_id = task._airtable_record_id
  AND p.account[SAFE_OFFSET(0)] IS NOT NULL
  AND COALESCE(p.hipaa_excluded, FALSE) = FALSE;
