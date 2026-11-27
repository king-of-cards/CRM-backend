-- Payout (commission) on converted leads, as a % of the conversion amount.
-- KOC -> Vendors leads keep their CRM details in crm_lead_meta (one row per lead per vendor copy);
-- Vendors -> KOC leads keep them on crm_vendor_to_koc_leads.
-- Run ONCE on the shared database (it changes the live tables for everyone).

ALTER TABLE crm_lead_meta
  ADD COLUMN payout_percent DECIMAL(5,2) NULL AFTER conversion_amount,
  ADD COLUMN payout_amount  INT          NULL AFTER payout_percent;

ALTER TABLE crm_vendor_to_koc_leads
  ADD COLUMN payout_percent DECIMAL(5,2) NULL AFTER conversion_amount,
  ADD COLUMN payout_amount  INT          NULL AFTER payout_percent;

-- Check:
--   SHOW COLUMNS FROM crm_lead_meta LIKE 'payout%';
--   SHOW COLUMNS FROM crm_vendor_to_koc_leads LIKE 'payout%';
