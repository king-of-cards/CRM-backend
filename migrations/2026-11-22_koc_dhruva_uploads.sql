-- CRM MySQL migration: crm_koc_dhruva_leads, the KOC -> Dhruva rows uploaded in the CRM.
-- Do not run against the read-only Spin workflow PostgreSQL database. Run order: 1 of 2
-- (then 2026-11-22_vtk_drop_vendor_phone_unique.sql), both before starting the new code.
--
-- KOC -> Dhruva lists the workflow spin_lead rows on or after SPIN_LIVE_FROM by rule; the history
-- before that date is uploaded here (POST /api/v1/koc-to-dhruva/leads/import, admins). Same columns
-- as crm_spin_leads; lead keys "kd-<id>"; the section's CRM details, hot flags and activity use the
-- existing tables with source 'koc_to_dhruva'. crm_dhruva_leads (Dhruva -> KOC) is not touched.
-- The app's create_all() would also create the table on startup; it is here for review.

-- ── 1. Preview (read only) ─────────────────────────────────────────────
SELECT COUNT(*) AS table_exists FROM information_schema.tables
WHERE table_schema = DATABASE() AND table_name = 'crm_koc_dhruva_leads';

-- ── 2. Apply ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crm_koc_dhruva_leads (
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
    INDEX ix_crm_koc_dhruva_leads_created_by (created_by),   -- no foreign key: logins can be deleted
    INDEX ix_crm_koc_dhruva_leads_branch_id (branch_id),
    INDEX ix_crm_koc_dhruva_leads_created_at (created_at),
    INDEX ix_crm_koc_dhruva_leads_is_deleted (is_deleted),
    INDEX ix_crm_koc_dhruva_leads_origin (origin),
    INDEX ix_crm_koc_dhruva_leads_phone (phone),
    INDEX ix_crm_koc_dhruva_leads_phone_e164 (phone_e164)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Check: the table exists and is empty
SELECT COUNT(*) AS uploaded_rows FROM crm_koc_dhruva_leads;

-- ── R. Rollback (only to undo, together with reverting the code) ──────
-- Uploaded rows would be lost: check first, and keep a copy if any.
--   SELECT COUNT(*) FROM crm_koc_dhruva_leads;
--   SELECT COUNT(*) FROM crm_lead_meta WHERE source = 'koc_to_dhruva' AND lead_key LIKE 'kd-%';
-- DELETE FROM crm_lead_meta WHERE source = 'koc_to_dhruva' AND lead_key LIKE 'kd-%';
-- DELETE FROM crm_hot_leads WHERE source = 'koc_to_dhruva' AND lead_key LIKE 'kd-%';
-- DROP TABLE IF EXISTS crm_koc_dhruva_leads;
