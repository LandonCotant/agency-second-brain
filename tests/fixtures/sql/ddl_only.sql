CREATE TABLE IF NOT EXISTS airtable_replica.clients (
  id STRING NOT NULL,
  name STRING,
  hipaa_excluded BOOL DEFAULT FALSE
);

DROP INDEX IF EXISTS idx_clients_legacy;
