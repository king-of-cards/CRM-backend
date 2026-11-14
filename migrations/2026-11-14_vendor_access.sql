-- One-time migration for the CRM MySQL database (never run this on the spin PostgreSQL DB).
--
-- Adds crm_lead_meta.auto_assign:
--   1 (default) the lead is assigned automatically when no vendor is picked (sheet "leads
--               shared", branch, area rules), as today. Every existing row gets 1.
--   0           never assigned automatically: the lead stays unassigned (admins only) until an
--               admin picks a vendor. Set for uploaded rows whose "Assigned to" is empty.
--
-- crm_vendor_users (user_id -> vendor_id) already exists and is only read; nothing changes there.
--
-- Run it BEFORE starting the new code: every lead list reads crm_lead_meta.auto_assign and
-- fails with "Unknown column" until it exists.

ALTER TABLE crm_lead_meta
    ADD COLUMN auto_assign TINYINT(1) NOT NULL DEFAULT 1 AFTER vendor_id;

-- Check 1: the column exists and every existing row is automatic (expect one row: 1, <count>)
-- SELECT auto_assign, COUNT(*) FROM crm_lead_meta GROUP BY auto_assign;
--
-- Check 2: vendor logins without a vendor (they get 403 on every lead endpoint until linked)
-- SELECT u.id, u.name, u.email FROM crm_users u
--   LEFT JOIN crm_vendor_users vu ON vu.user_id = u.id
--  WHERE u.role = 'vendor' AND vu.user_id IS NULL;
-- Link one with:  INSERT INTO crm_vendor_users (user_id, vendor_id) VALUES (<user id>, <vendor id>);
--
-- Rollback. Leads uploaded as unassigned (auto_assign = 0) would then be assigned automatically
-- again; list them first if that matters:
-- SELECT source, lead_key FROM crm_lead_meta WHERE auto_assign = 0;
-- ALTER TABLE crm_lead_meta DROP COLUMN auto_assign;
