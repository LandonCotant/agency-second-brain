CREATE OR REPLACE VIEW airtable_replica.active_clients_v AS
SELECT id, name
FROM airtable_replica.clients
WHERE active = TRUE;
