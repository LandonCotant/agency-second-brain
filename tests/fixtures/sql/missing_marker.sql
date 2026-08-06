SELECT id, name
FROM airtable_replica.clients
WHERE COALESCE(hipaa_excluded, FALSE) = FALSE;
