-- HIPAA-EXCLUDE
-- Reason: Librarian never ingests HIPAA folders (galaxy/area/resource rows
-- are written with hipaa_isolated=FALSE by construction; the lister refuses
-- HIPAA roots). No HIPAA-bearing rows match the note_kind filter below.
--
-- One-time backfill prune (ADR 0071 §4).
--
-- Before ADR 0071 the Librarian wrote agent_outputs.notes via INSERT-append
-- keyed on (file_id, revision_id), so a file rewritten each sweep (e.g.
-- asb-people-sync's weekly galaxy dossiers) accumulated one row per revision.
-- This deletes the stale revisions, keeping the newest row per
-- (note_id, note_kind) for the Librarian-written kinds.
--
-- Deletes only OLDER revisions (the newest, possibly-streaming row is kept),
-- so it is streaming-buffer-safe (ADR 0025).
--
-- ORDER OF OPERATIONS (load-bearing):
--   1. Deploy the ADR 0071 writer fix (Librarian now MERGEs on note_id) so
--      nothing re-creates dupes mid-prune.
--   2. Run the PREVIEW query and confirm the counts.
--   3. Run the DELETE.
--
-- PREVIEW — how many rows would be deleted, and how many notes are affected:
--
--   SELECT
--     note_kind,
--     COUNT(*)                              AS rows_to_delete,
--     COUNT(DISTINCT note_id)               AS notes_affected
--   FROM `agency-brain-demo.agent_outputs.notes` t
--   WHERE note_kind IN ('galaxy','area','resource')
--     AND ingested_at < (
--       SELECT MAX(ingested_at)
--       FROM `agency-brain-demo.agent_outputs.notes` x
--       WHERE x.note_id = t.note_id AND x.note_kind = t.note_kind
--     )
--   GROUP BY note_kind;

DELETE FROM `agency-brain-demo.agent_outputs.notes` t
WHERE t.note_kind IN ('galaxy','area','resource')
  AND t.ingested_at < (
    SELECT MAX(x.ingested_at)
    FROM `agency-brain-demo.agent_outputs.notes` x
    WHERE x.note_id = t.note_id
      AND x.note_kind = t.note_kind
  );
