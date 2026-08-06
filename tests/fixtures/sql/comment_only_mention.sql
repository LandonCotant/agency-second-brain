-- This file used to query the clients and projects tables but now hits a non-PII view.
SELECT public_summary
FROM marketing_replica.public_metrics
WHERE active = TRUE;
