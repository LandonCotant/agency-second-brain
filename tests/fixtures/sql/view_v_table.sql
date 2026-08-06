-- HIPAA-EXCLUDE
SELECT owner_email, client_id
FROM airtable_replica.client_owners_v
WHERE COALESCE(hipaa_excluded, FALSE) = FALSE;
