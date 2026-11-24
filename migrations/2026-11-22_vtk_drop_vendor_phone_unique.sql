-- CRM MySQL migration: crm_vendor_to_koc_leads may hold the same phone twice for a vendor.
-- Do not run against the read-only Spin workflow PostgreSQL database. Run order: 2 of 2
-- (after 2026-11-22_koc_dhruva_uploads.sql), right before starting the new code.
--
-- A duplicate is now the same phone AND name, date and city (any case and spacing), so the unique
-- index uq_vtk_leads_vendor_phone (vendor_id, phone) is replaced by a normal index on the same
-- columns (the lookups stay fast; vendor_id keeps an index for its foreign key). No data changes.
-- When this file was written (2026-11-21): 362 rows, 0 (vendor_id, phone) pairs used twice.

-- ── 1. Preview (read only) ─────────────────────────────────────────────
SHOW INDEX FROM crm_vendor_to_koc_leads WHERE Key_name IN ('uq_vtk_leads_vendor_phone', 'ix_vtk_leads_vendor_phone');

-- ── 2. Apply (one statement: the new index exists before the unique one goes) ──
ALTER TABLE crm_vendor_to_koc_leads
    ADD INDEX ix_vtk_leads_vendor_phone (vendor_id, phone),
    DROP INDEX uq_vtk_leads_vendor_phone;

-- Check: only ix_vtk_leads_vendor_phone is listed, with Non_unique = 1
SHOW INDEX FROM crm_vendor_to_koc_leads WHERE Key_name IN ('uq_vtk_leads_vendor_phone', 'ix_vtk_leads_vendor_phone');

-- ── R. Rollback (only to undo, together with reverting the code) ──────
-- The unique index can only come back while no vendor has the same phone twice. This must be 0:
--   SELECT COUNT(*) FROM (SELECT vendor_id, phone FROM crm_vendor_to_koc_leads
--                         GROUP BY vendor_id, phone HAVING COUNT(*) > 1) AS x;
-- If it isn't, list them and decide which rows to keep first:
--   SELECT vendor_id, phone, COUNT(*) AS n, GROUP_CONCAT(id ORDER BY id) AS ids FROM crm_vendor_to_koc_leads
--   GROUP BY vendor_id, phone HAVING COUNT(*) > 1;
-- ALTER TABLE crm_vendor_to_koc_leads
--     ADD UNIQUE INDEX uq_vtk_leads_vendor_phone (vendor_id, phone),
--     DROP INDEX ix_vtk_leads_vendor_phone;
