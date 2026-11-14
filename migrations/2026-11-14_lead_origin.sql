-- One-time migration for the CRM MySQL database (never run this on the spin PostgreSQL DB).
--
-- 1. Adds to the three sheet lead tables:
--      origin      sheet | added | imported. Existing rows get 'sheet' from the DEFAULT, so the
--                  sync keeps managing them and never touches added/imported rows.
--      phone_e164  the phone in E.164 form (+919845012345), used for duplicate checks.
--                  Filled for existing rows on the next app start (and by every sync); rows
--                  whose number can't be read get '' so they aren't retried.
-- 2. Creates crm_spin_leads (added/imported spin leads). The app's create_all() would also
--    create it on startup; it is here so the whole change can be applied and reviewed at once.
--
-- Run it BEFORE starting the new code: the new code reads these columns, so the lead lists and
-- the sheet sync fail with "Unknown column" until step 1 has run.
-- Each ALTER rebuilds its table online (InnoDB, ALGORITHM=INPLACE); the tables are small.

ALTER TABLE crm_whatsapp_leads
    ADD COLUMN origin VARCHAR(10) NOT NULL DEFAULT 'sheet' AFTER updated_at,
    ADD COLUMN phone_e164 VARCHAR(16) NULL AFTER origin,
    ADD INDEX ix_crm_whatsapp_leads_origin (origin),
    ADD INDEX ix_crm_whatsapp_leads_phone_e164 (phone_e164);

ALTER TABLE crm_wedding_leads
    ADD COLUMN origin VARCHAR(10) NOT NULL DEFAULT 'sheet' AFTER updated_at,
    ADD COLUMN phone_e164 VARCHAR(16) NULL AFTER origin,
    ADD INDEX ix_crm_wedding_leads_origin (origin),
    ADD INDEX ix_crm_wedding_leads_phone_e164 (phone_e164);

ALTER TABLE crm_wedding_plans
    ADD COLUMN origin VARCHAR(10) NOT NULL DEFAULT 'sheet' AFTER updated_at,
    ADD COLUMN phone_e164 VARCHAR(16) NULL AFTER origin,
    ADD INDEX ix_crm_wedding_plans_origin (origin),
    ADD INDEX ix_crm_wedding_plans_phone_e164 (phone_e164);

CREATE TABLE IF NOT EXISTS crm_spin_leads (
    id INTEGER NOT NULL AUTO_INCREMENT,
    origin VARCHAR(10) NOT NULL,
    name VARCHAR(150),
    email VARCHAR(150),
    phone VARCHAR(20) NOT NULL,
    phone_e164 VARCHAR(16) NOT NULL,
    occasion VARCHAR(100),
    source VARCHAR(50),
    campaign_id BIGINT,
    customer_id BIGINT,
    branch_id BIGINT,
    consent_at DATETIME,
    created_at DATETIME NOT NULL,
    additional_details JSON,
    created_by INTEGER,
    updated_at DATETIME NOT NULL,
    is_deleted BOOL NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY (created_by) REFERENCES crm_users (id),
    INDEX ix_crm_spin_leads_branch_id (branch_id),
    INDEX ix_crm_spin_leads_created_at (created_at),
    INDEX ix_crm_spin_leads_is_deleted (is_deleted),
    INDEX ix_crm_spin_leads_origin (origin),
    INDEX ix_crm_spin_leads_phone (phone),
    INDEX ix_crm_spin_leads_phone_e164 (phone_e164)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Check (expect only 'sheet' rows in each table right after the migration):
-- SELECT 'whatsapp', origin, COUNT(*) FROM crm_whatsapp_leads GROUP BY origin
-- UNION ALL SELECT 'wedding', origin, COUNT(*) FROM crm_wedding_leads GROUP BY origin
-- UNION ALL SELECT 'planner', origin, COUNT(*) FROM crm_wedding_plans GROUP BY origin;
-- After the first app start, phone_e164 should be filled everywhere:
-- SELECT COUNT(*) FROM crm_whatsapp_leads WHERE phone_e164 IS NULL;   -- expect 0
--
-- Rollback (only while no added/imported leads exist):
-- ALTER TABLE crm_whatsapp_leads DROP INDEX ix_crm_whatsapp_leads_origin, DROP INDEX ix_crm_whatsapp_leads_phone_e164,
--     DROP COLUMN origin, DROP COLUMN phone_e164;
-- ALTER TABLE crm_wedding_leads  DROP INDEX ix_crm_wedding_leads_origin,  DROP INDEX ix_crm_wedding_leads_phone_e164,
--     DROP COLUMN origin, DROP COLUMN phone_e164;
-- ALTER TABLE crm_wedding_plans  DROP INDEX ix_crm_wedding_plans_origin,  DROP INDEX ix_crm_wedding_plans_phone_e164,
--     DROP COLUMN origin, DROP COLUMN phone_e164;
-- DROP TABLE crm_spin_leads;
