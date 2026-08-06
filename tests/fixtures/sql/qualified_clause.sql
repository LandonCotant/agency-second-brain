-- HIPAA-EXCLUDE
-- Regression for 2026-05-28 audit: backfill SQL with a JOIN across two
-- replica tables that both have a hipaa_excluded column had to qualify
-- the clause column with the table alias to disambiguate. Static checker
-- must still accept this shape.
SELECT t.id
FROM agent_outputs.triaged_items t
JOIN airtable_replica.tasks task ON t.airtable_task_record_id = task._airtable_record_id
JOIN airtable_replica.projects p ON task.project[SAFE_OFFSET(0)] = p._airtable_record_id
WHERE COALESCE(p.hipaa_excluded, FALSE) = FALSE;
