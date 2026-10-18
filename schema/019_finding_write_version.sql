-- Version only: preserve historical decisions and all fact/evidence records.
BEGIN IMMEDIATE;
ALTER TABLE finding_triage ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1);
-- The runner commits this change and the migration registry together.
