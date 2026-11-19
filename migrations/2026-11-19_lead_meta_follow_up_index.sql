-- CRM MySQL migration. Never apply to the read-only Spin workflow database.
-- Check first; if this index already exists, skip the CREATE statement:
-- SELECT INDEX_NAME FROM information_schema.STATISTICS
-- WHERE TABLE_SCHEMA = DATABASE()
--   AND TABLE_NAME = 'crm_lead_meta'
--   AND INDEX_NAME = 'ix_crm_lead_meta_follow_up_date';
CREATE INDEX ix_crm_lead_meta_follow_up_date
    ON crm_lead_meta (follow_up_date);

-- Rollback:
-- DROP INDEX ix_crm_lead_meta_follow_up_date ON crm_lead_meta;
