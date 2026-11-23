# Migrations

`database/schema.sql` is migration `0001` (the complete initial schema). Later
changes are added here as forward-only, numbered files
(`0002_<description>.sql`, …) and are never edited after release.

Rules:

* Never alter or drop `raw_events`, `raw_delivery_log`, `*_history` or
  `audit_runs` rows — they are append-only by trigger.
* Add a new partition of `execution_events` before each month begins
  (`CREATE TABLE execution_events_YYYY_MM PARTITION OF execution_events ...`);
  the `DEFAULT` partition only catches stragglers and is monitored.
* Retention: archive partitions older than the retention period to cold
  storage with their hash-chain head recorded in `audit_runs` before detaching.
* Verify every migration with `database/tests/constraint_checks.sql`
  (`tests/database/test_schema_postgres.py` runs it when `VIA_TEST_PG_DSN` is set).
