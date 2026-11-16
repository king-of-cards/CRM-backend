-- One-time migration for the CRM MySQL database (never run this on the spin PostgreSQL DB).
--
-- 1. crm_lead_meta (the 4 sources: spin, whatsapp, wedding, wedding_planner)
--    update_status      replaces "Interest" + "Converted": the same 7 values as Vendor -> KOC
--                       (converted, not_converted, purchased_elsewhere, purchased_with_us,
--                       will_call_back, interested, not_interested); NULL = Pending.
--    conversion_amount  rupees, nullable. Existing rows stay empty.
--    interest, converted and converted_at are NOT changed or dropped: the new code stops reading
--    and writing them, and they keep the old values for the rollback.
-- 2. crm_vendor_to_koc_leads: quoted_amount and conversion_amount (rupees, nullable).
--
-- Run it BEFORE starting the new code: the lead lists, the edit endpoints and the dashboards read
-- these columns and fail with "Unknown column" until they exist.

ALTER TABLE crm_lead_meta
    ADD COLUMN update_status VARCHAR(30) NULL AFTER auto_assign,
    ADD COLUMN conversion_amount INT NULL AFTER quoted_amount,
    ADD INDEX ix_crm_lead_meta_update_status (update_status);

ALTER TABLE crm_vendor_to_koc_leads
    ADD COLUMN quoted_amount INT NULL AFTER follow_up_date,
    ADD COLUMN conversion_amount INT NULL AFTER quoted_amount;

-- Convert the old values (interest / converted are only read here):
--   converted = yes, interest empty or interested   -> converted
--   interested                                      -> interested
--   not_interested                                  -> not_interested
--   follow_up                                       -> will_call_back
--   other (or empty, not converted)                 -> empty (Pending)
UPDATE crm_lead_meta SET update_status = CASE
    WHEN converted = 1 AND (interest IS NULL OR interest = 'interested') THEN 'converted'
    WHEN interest = 'interested'     THEN 'interested'
    WHEN interest = 'not_interested' THEN 'not_interested'
    WHEN interest = 'follow_up'      THEN 'will_call_back'
    ELSE NULL
END;

-- Check 1: the new values. On 16 Nov 2026 (5 rows, all spin) expect: converted 4, interested 1.
-- SELECT COALESCE(update_status, '(pending)') AS u, COUNT(*) FROM crm_lead_meta GROUP BY u ORDER BY u;
--
-- Check 2: old -> new, row by row (every combination and what it became)
-- SELECT COALESCE(interest, '(empty)') AS interest, converted, COALESCE(update_status, '(pending)') AS u, COUNT(*)
--   FROM crm_lead_meta GROUP BY interest, converted, update_status ORDER BY interest, converted;
--
-- Check 3: the new columns exist (expect 2 rows each)
-- SHOW COLUMNS FROM crm_lead_meta WHERE Field IN ('update_status', 'conversion_amount');
-- SHOW COLUMNS FROM crm_vendor_to_koc_leads WHERE Field IN ('quoted_amount', 'conversion_amount');
--
-- Rollback (lossless for the old fields: interest / converted / converted_at were never changed;
-- Update values and amounts entered after the migration are lost). Deploy the previous code first.
-- ALTER TABLE crm_lead_meta DROP INDEX ix_crm_lead_meta_update_status,
--     DROP COLUMN update_status, DROP COLUMN conversion_amount;
-- ALTER TABLE crm_vendor_to_koc_leads DROP COLUMN quoted_amount, DROP COLUMN conversion_amount;
