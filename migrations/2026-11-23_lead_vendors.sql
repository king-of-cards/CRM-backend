-- CRM MySQL migration: several vendors per KOC -> Vendors lead, each with its own copy of the CRM details.
-- Do not run against the read-only Spin workflow PostgreSQL database.
-- Order: 1) snapshot the database, 2) this file, 3) scripts/migrate_lead_vendors.py (preview, then
-- --apply: moves the existing rows into the vendors' copies and adds Kraftyz from its sheet),
-- 4) restart the app. The app must not run with the new code before step 2.
--
--   crm_lead_meta.copy_vendor_id   whose copy the row is (0 = the vendor-less row; koc_to_dhruva only uses 0)
--   crm_hot_leads.vendor_id        one hot star per copy (0 = the vendor-less row)
--   crm_lead_activity.vendor_id    the copy an entry concerns (NULL = the lead)
--   crm_lead_assignments           manual choices: "added" vendors, "removed" vendors (removing sticks)

-- ── 1. Preview (read only) ─────────────────────────────────────────────
SELECT source, COUNT(*) AS meta_rows, SUM(vendor_id IS NOT NULL) AS manual_vendor FROM crm_lead_meta GROUP BY source;
SELECT source, COUNT(*) AS hot_rows FROM crm_hot_leads GROUP BY source;
SHOW INDEX FROM crm_lead_meta WHERE Key_name = 'uq_lead_meta_source_key';
SHOW INDEX FROM crm_hot_leads WHERE Key_name = 'uq_crm_hot_leads_source_key';

-- ── 2. Apply ───────────────────────────────────────────────────────────
ALTER TABLE crm_lead_meta
    ADD COLUMN copy_vendor_id INT NOT NULL DEFAULT 0 AFTER lead_key,
    ADD INDEX ix_crm_lead_meta_copy_vendor_id (copy_vendor_id),
    ADD UNIQUE INDEX uq_lead_meta_source_key_copy (source, lead_key, copy_vendor_id),
    DROP INDEX uq_lead_meta_source_key;

ALTER TABLE crm_hot_leads
    ADD COLUMN vendor_id INT NOT NULL DEFAULT 0 AFTER lead_key,
    ADD UNIQUE INDEX uq_crm_hot_leads_source_key_vendor (source, lead_key, vendor_id),
    DROP INDEX uq_crm_hot_leads_source_key;

ALTER TABLE crm_lead_activity
    ADD COLUMN vendor_id INT NULL,
    ADD INDEX ix_crm_lead_activity_vendor_id (vendor_id);

CREATE TABLE IF NOT EXISTS crm_lead_assignments (
    id INTEGER NOT NULL AUTO_INCREMENT,
    source VARCHAR(30) NOT NULL,
    lead_key VARCHAR(100) NOT NULL,
    vendor_id INTEGER NOT NULL,
    state VARCHAR(10) NOT NULL,            -- added | removed
    updated_by INTEGER,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE INDEX uq_crm_lead_assignments (source, lead_key, vendor_id),
    INDEX ix_crm_lead_assignments_source (source),
    INDEX ix_crm_lead_assignments_lead_key (lead_key),
    INDEX ix_crm_lead_assignments_vendor_id (vendor_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Check: every existing row is still the vendor-less row (the script moves them), no assignments yet
SELECT COUNT(*) AS rows_not_zero FROM crm_lead_meta WHERE copy_vendor_id <> 0;
SELECT COUNT(*) AS assignments FROM crm_lead_assignments;

-- ── R. Rollback (only to undo, together with reverting the code) ──────
-- First run the script's undo file (if the script was applied). The old unique indexes need one row
-- per lead again; each count must be 0:
--   SELECT COUNT(*) FROM (SELECT source, lead_key FROM crm_lead_meta GROUP BY source, lead_key HAVING COUNT(*) > 1) x;
--   SELECT COUNT(*) FROM (SELECT source, lead_key FROM crm_hot_leads GROUP BY source, lead_key HAVING COUNT(*) > 1) x;
-- ALTER TABLE crm_lead_meta ADD UNIQUE INDEX uq_lead_meta_source_key (source, lead_key),
--     DROP INDEX uq_lead_meta_source_key_copy, DROP INDEX ix_crm_lead_meta_copy_vendor_id, DROP COLUMN copy_vendor_id;
-- ALTER TABLE crm_hot_leads ADD UNIQUE INDEX uq_crm_hot_leads_source_key (source, lead_key),
--     DROP INDEX uq_crm_hot_leads_source_key_vendor, DROP COLUMN vendor_id;
-- ALTER TABLE crm_lead_activity DROP INDEX ix_crm_lead_activity_vendor_id, DROP COLUMN vendor_id;
-- DROP TABLE IF EXISTS crm_lead_assignments;
