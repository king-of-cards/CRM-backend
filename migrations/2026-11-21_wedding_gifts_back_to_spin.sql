-- CRM MySQL migration: move CRM rows from source 'wedding_gifts' back to 'spin'.
-- Do not run against the read-only Spin workflow PostgreSQL database.
--
-- The separate Wedding gifts source was removed: gift leads (otherWeddingServices names
-- "Wedding Gifts & Return Gifts") are spin leads again, auto-assigned to WEDDING_GIFTS_VENDOR_NAME.
-- Any CRM row still filed under 'wedding_gifts' goes back to 'spin', with the same lead_key:
--   crm_lead_meta, crm_hot_leads, crm_lead_activity, crm_notifications
-- crm_notification_reads has no source (it follows notification ids): untouched.
-- No schema change. The ids moved are kept in crm_mig_wedding_gifts_back (a real table, so the
-- rollback still works in a later session); drop it once you no longer need the rollback.
--
-- When this file was written (2026-11-21, read-only counts) the old migration
-- (2026-11-20_wedding_gifts_source.sql, now deleted) had never been applied: 0 rows under
-- 'wedding_gifts' in every table, 0 conflicts. So today this moves nothing; it is here for any
-- database where the old migration did run.
--
-- Steps:
--   1. Preview     (read only; the conflict checks must be 0)
--   2. Apply       (one transaction)
--   R. Rollback    (only to undo, together with reverting the code)

-- ── 1. Preview (read only) ─────────────────────────────────────────────
SELECT 'crm_lead_meta' AS tbl, COUNT(*) AS rows_to_move FROM crm_lead_meta WHERE source = 'wedding_gifts';
SELECT 'crm_hot_leads' AS tbl, COUNT(*) AS rows_to_move FROM crm_hot_leads WHERE source = 'wedding_gifts';
SELECT 'crm_lead_activity' AS tbl, COUNT(*) AS rows_to_move FROM crm_lead_activity WHERE source = 'wedding_gifts';
SELECT 'crm_notifications' AS tbl, COUNT(*) AS rows_to_move FROM crm_notifications WHERE source = 'wedding_gifts';
-- The CRM details that move
SELECT id, lead_key, vendor_id, auto_assign, update_status, quoted_amount, follow_up_date
FROM crm_lead_meta WHERE source = 'wedding_gifts';
-- Conflict checks: both must be 0 (unique (source, lead_key) on these two tables)
SELECT 'crm_lead_meta conflicts' AS chk, COUNT(*) AS n
FROM crm_lead_meta g JOIN crm_lead_meta s ON s.lead_key = g.lead_key AND s.source = 'spin'
WHERE g.source = 'wedding_gifts';
SELECT 'crm_hot_leads conflicts' AS chk, COUNT(*) AS n
FROM crm_hot_leads g JOIN crm_hot_leads s ON s.lead_key = g.lead_key AND s.source = 'spin'
WHERE g.source = 'wedding_gifts';

-- ── 2. Apply ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crm_mig_wedding_gifts_back (
    tbl VARCHAR(30) NOT NULL,
    row_id INT NOT NULL,
    PRIMARY KEY (tbl, row_id)
);
START TRANSACTION;
INSERT IGNORE INTO crm_mig_wedding_gifts_back (tbl, row_id)
SELECT 'crm_lead_meta', id FROM crm_lead_meta WHERE source = 'wedding_gifts';
INSERT IGNORE INTO crm_mig_wedding_gifts_back (tbl, row_id)
SELECT 'crm_hot_leads', id FROM crm_hot_leads WHERE source = 'wedding_gifts';
INSERT IGNORE INTO crm_mig_wedding_gifts_back (tbl, row_id)
SELECT 'crm_lead_activity', id FROM crm_lead_activity WHERE source = 'wedding_gifts';
INSERT IGNORE INTO crm_mig_wedding_gifts_back (tbl, row_id)
SELECT 'crm_notifications', id FROM crm_notifications WHERE source = 'wedding_gifts';
UPDATE crm_lead_meta SET source = 'spin' WHERE source = 'wedding_gifts';
UPDATE crm_hot_leads SET source = 'spin' WHERE source = 'wedding_gifts';
UPDATE crm_lead_activity SET source = 'spin' WHERE source = 'wedding_gifts';
UPDATE crm_notifications SET source = 'spin' WHERE source = 'wedding_gifts';
COMMIT;
-- If an UPDATE fails (a conflict the preview missed), run ROLLBACK; instead of COMMIT.

-- Check after: every count 0
SELECT
  (SELECT COUNT(*) FROM crm_lead_meta WHERE source = 'wedding_gifts') AS meta_left,
  (SELECT COUNT(*) FROM crm_hot_leads WHERE source = 'wedding_gifts') AS hot_left,
  (SELECT COUNT(*) FROM crm_lead_activity WHERE source = 'wedding_gifts') AS activity_left,
  (SELECT COUNT(*) FROM crm_notifications WHERE source = 'wedding_gifts') AS notifications_left;

-- ── R. Rollback (only to undo, together with reverting the code) ──────
-- Puts exactly the rows this migration moved back under 'wedding_gifts'.
-- START TRANSACTION;
-- UPDATE crm_lead_meta t JOIN crm_mig_wedding_gifts_back b ON b.tbl = 'crm_lead_meta' AND b.row_id = t.id
--   SET t.source = 'wedding_gifts' WHERE t.source = 'spin';
-- UPDATE crm_hot_leads t JOIN crm_mig_wedding_gifts_back b ON b.tbl = 'crm_hot_leads' AND b.row_id = t.id
--   SET t.source = 'wedding_gifts' WHERE t.source = 'spin';
-- UPDATE crm_lead_activity t JOIN crm_mig_wedding_gifts_back b ON b.tbl = 'crm_lead_activity' AND b.row_id = t.id
--   SET t.source = 'wedding_gifts' WHERE t.source = 'spin';
-- UPDATE crm_notifications t JOIN crm_mig_wedding_gifts_back b ON b.tbl = 'crm_notifications' AND b.row_id = t.id
--   SET t.source = 'wedding_gifts' WHERE t.source = 'spin';
-- COMMIT;
-- When the rollback is no longer needed:
-- DROP TABLE IF EXISTS crm_mig_wedding_gifts_back;
