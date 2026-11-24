-- CRM MySQL migration: logins can be deleted while history keeps their user id.
-- Do not run against the read-only Spin workflow PostgreSQL database.
-- Run after 2026-11-22_koc_dhruva_uploads.sql and 2026-11-22_vtk_drop_vendor_phone_unique.sql,
-- before starting the code with DELETE /api/v1/users/{id}.
--
-- DELETE /users/{id} removes the login with its sessions (crm_refresh_tokens, ON DELETE CASCADE),
-- vendor link (crm_vendor_users), notification read marks (crm_notification_reads) and own inbox
-- (crm_notifications.recipient_user_id): those foreign keys stay. These history columns only store
-- a user id, shown as "Deleted user" once the login is gone, so their foreign keys to crm_users are
-- dropped (the columns, their values and their indexes stay):
--   crm_spin_leads.created_by              crm_koc_dhruva_leads.created_by
--   crm_dhruva_leads.created_by            crm_dhruva_leads.status_updated_by
--   crm_hot_leads.hot_marked_by            crm_lead_activity.actor_user_id
--   crm_lead_meta.updated_by               crm_notifications.actor_user_id
--   crm_vendor_to_koc_leads.created_by     crm_vendor_to_koc_leads.status_updated_by
--   crm_vendor_to_koc_leads.hot_marked_by
-- MySQL named these foreign keys itself (…_ibfk_N), so step 2 finds them by table and column.

-- ── 1. Preview (read only): the foreign keys that will be dropped ──────
SELECT TABLE_NAME, COLUMN_NAME, CONSTRAINT_NAME
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = 'crm_users'
  AND (TABLE_NAME, COLUMN_NAME) IN (
      ('crm_spin_leads', 'created_by'), ('crm_koc_dhruva_leads', 'created_by'),
      ('crm_dhruva_leads', 'created_by'), ('crm_dhruva_leads', 'status_updated_by'),
      ('crm_hot_leads', 'hot_marked_by'), ('crm_lead_activity', 'actor_user_id'),
      ('crm_lead_meta', 'updated_by'), ('crm_notifications', 'actor_user_id'),
      ('crm_vendor_to_koc_leads', 'created_by'), ('crm_vendor_to_koc_leads', 'status_updated_by'),
      ('crm_vendor_to_koc_leads', 'hot_marked_by'))
ORDER BY TABLE_NAME, COLUMN_NAME;

-- ── 2. Apply: drop each of them (a one-off procedure, removed at the end) ──
DROP PROCEDURE IF EXISTS tmp_drop_user_history_fks;
DELIMITER //
CREATE PROCEDURE tmp_drop_user_history_fks()
BEGIN
    DECLARE done INT DEFAULT 0;
    DECLARE t VARCHAR(64);
    DECLARE c VARCHAR(64);
    DECLARE cur CURSOR FOR
        SELECT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = 'crm_users'
          AND (TABLE_NAME, COLUMN_NAME) IN (
              ('crm_spin_leads', 'created_by'), ('crm_koc_dhruva_leads', 'created_by'),
              ('crm_dhruva_leads', 'created_by'), ('crm_dhruva_leads', 'status_updated_by'),
              ('crm_hot_leads', 'hot_marked_by'), ('crm_lead_activity', 'actor_user_id'),
              ('crm_lead_meta', 'updated_by'), ('crm_notifications', 'actor_user_id'),
              ('crm_vendor_to_koc_leads', 'created_by'), ('crm_vendor_to_koc_leads', 'status_updated_by'),
              ('crm_vendor_to_koc_leads', 'hot_marked_by'));
    DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;
    OPEN cur;
    drop_loop: LOOP
        FETCH cur INTO t, c;
        IF done THEN LEAVE drop_loop; END IF;
        SET @stmt = CONCAT('ALTER TABLE `', t, '` DROP FOREIGN KEY `', c, '`');
        PREPARE s FROM @stmt;
        EXECUTE s;
        DEALLOCATE PREPARE s;
    END LOOP;
    CLOSE cur;
END //
DELIMITER ;
CALL tmp_drop_user_history_fks();
DROP PROCEDURE IF EXISTS tmp_drop_user_history_fks;

-- Check: run step 1 again; it must return no rows. The remaining foreign keys to crm_users
-- (crm_refresh_tokens, crm_vendor_users, crm_notification_reads, crm_notifications.recipient_user_id):
SELECT TABLE_NAME, COLUMN_NAME, CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = 'crm_users' ORDER BY TABLE_NAME, COLUMN_NAME;

-- ── R. Rollback (only to undo, together with reverting the code) ──────
-- A foreign key can only come back while every stored id still exists in crm_users. Each count
-- must be 0 first (logins deleted in the meantime break it; set those ids to an existing user or
-- NULL where the column allows it):
--   SELECT COUNT(*) FROM crm_lead_meta m LEFT JOIN crm_users u ON u.id = m.updated_by
--   WHERE m.updated_by IS NOT NULL AND u.id IS NULL;
--   (the same for every table / column above)
-- ALTER TABLE crm_spin_leads ADD FOREIGN KEY (created_by) REFERENCES crm_users (id);
-- ALTER TABLE crm_koc_dhruva_leads ADD FOREIGN KEY (created_by) REFERENCES crm_users (id);
-- ALTER TABLE crm_dhruva_leads ADD FOREIGN KEY (created_by) REFERENCES crm_users (id),
--                              ADD FOREIGN KEY (status_updated_by) REFERENCES crm_users (id);
-- ALTER TABLE crm_hot_leads ADD FOREIGN KEY (hot_marked_by) REFERENCES crm_users (id);
-- ALTER TABLE crm_lead_activity ADD FOREIGN KEY (actor_user_id) REFERENCES crm_users (id);
-- ALTER TABLE crm_lead_meta ADD FOREIGN KEY (updated_by) REFERENCES crm_users (id);
-- ALTER TABLE crm_notifications ADD FOREIGN KEY (actor_user_id) REFERENCES crm_users (id);
-- ALTER TABLE crm_vendor_to_koc_leads ADD FOREIGN KEY (created_by) REFERENCES crm_users (id),
--                                     ADD FOREIGN KEY (status_updated_by) REFERENCES crm_users (id),
--                                     ADD FOREIGN KEY (hot_marked_by) REFERENCES crm_users (id);
