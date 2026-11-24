-- Performance indexes (MySQL). Safe to run more than once: each index is only created when
-- information_schema says it is missing. Indexes only: no data changes. Run with the app up or down.
--
-- 1. crm_vendor_to_koc_leads (phone)        also_in on every list page looks phones up across vendors
--                                           (WHERE phone IN …); the (vendor_id, phone) index can't serve
--                                           that. Created by 2026-11-17_dhruva.sql where that was run.
-- 2. crm_hot_leads (source, vendor_id)      a vendor's hot leads of a source (hot filter, vendor dashboards)
-- 3. crm_notifications (created_at)         notifications.cleanup_expired deletes by created_at alone after
--                                           every notification (every bulk upload sends one)

-- ── Preview: which of them exist already ────────────────────────────
SELECT table_name, index_name, GROUP_CONCAT(column_name ORDER BY seq_in_index) AS columns
FROM information_schema.statistics
WHERE table_schema = DATABASE()
  AND index_name IN ('ix_crm_vendor_to_koc_leads_phone', 'ix_crm_hot_leads_source_vendor',
                     'ix_crm_notifications_created_at')
GROUP BY table_name, index_name;

-- ── Apply ────────────────────────────────────────────────────────────
SET @sql = (SELECT IF(COUNT(*) = 0,
    'ALTER TABLE crm_vendor_to_koc_leads ADD INDEX ix_crm_vendor_to_koc_leads_phone (phone)',
    'SELECT ''ix_crm_vendor_to_koc_leads_phone exists'' AS skipped')
  FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'crm_vendor_to_koc_leads'
    AND index_name = 'ix_crm_vendor_to_koc_leads_phone');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (SELECT IF(COUNT(*) = 0,
    'ALTER TABLE crm_hot_leads ADD INDEX ix_crm_hot_leads_source_vendor (source, vendor_id)',
    'SELECT ''ix_crm_hot_leads_source_vendor exists'' AS skipped')
  FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'crm_hot_leads'
    AND index_name = 'ix_crm_hot_leads_source_vendor');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (SELECT IF(COUNT(*) = 0,
    'ALTER TABLE crm_notifications ADD INDEX ix_crm_notifications_created_at (created_at)',
    'SELECT ''ix_crm_notifications_created_at exists'' AS skipped')
  FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'crm_notifications'
    AND index_name = 'ix_crm_notifications_created_at');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ── Check: the preview query again (three rows) ──────────────────────

-- ── Undo ─────────────────────────────────────────────────────────────
-- (Leave ix_crm_vendor_to_koc_leads_phone if 2026-11-17_dhruva.sql created it.)
-- ALTER TABLE crm_hot_leads DROP INDEX ix_crm_hot_leads_source_vendor;
-- ALTER TABLE crm_notifications DROP INDEX ix_crm_notifications_created_at;
