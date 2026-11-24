-- CRM MySQL migration: the new Dhruva -> KOC lead fields (crm_dhruva_leads) and the event dates table.
-- Do not run against the read-only Spin workflow PostgreSQL database. Run BEFORE starting the new
-- code (it reads these columns), and before scripts/clear_dhruva_to_koc.py.
--
-- New on crm_dhruva_leads: sent_at (set when the lead is added / uploaded), whatsapp_status,
-- response, message_sent, follow_up_notes, needs_intervention. "Comments" keeps using
-- additional_comments; event_date keeps the earliest event date (for sorting). event_date_text is
-- no longer used by the app; it is kept (not dropped) so the removal script's undo file can restore
-- every column; drop it later if you like:  ALTER TABLE crm_dhruva_leads DROP COLUMN event_date_text;
-- New table crm_dhruva_event_dates: one row per event date of a lead (a lead has one or more).
-- The app's create_all() would also create the new table on startup (not the new columns).

-- ── 1. Preview (read only) ─────────────────────────────────────────────
SELECT column_name FROM information_schema.columns
WHERE table_schema = DATABASE() AND table_name = 'crm_dhruva_leads'
  AND column_name IN ('sent_at', 'whatsapp_status', 'response', 'message_sent', 'follow_up_notes',
                      'needs_intervention');
SELECT COUNT(*) AS event_dates_table_exists FROM information_schema.tables
WHERE table_schema = DATABASE() AND table_name = 'crm_dhruva_event_dates';
SELECT COUNT(*) AS leads FROM crm_dhruva_leads;

-- ── 2. Apply ───────────────────────────────────────────────────────────
ALTER TABLE crm_dhruva_leads
    ADD COLUMN sent_at DATETIME NULL AFTER source,
    ADD COLUMN whatsapp_status VARCHAR(100) NULL AFTER sent_at,
    ADD COLUMN response TEXT NULL AFTER whatsapp_status,
    ADD COLUMN message_sent VARCHAR(500) NULL AFTER response,
    ADD COLUMN follow_up_notes TEXT NULL AFTER message_sent,
    ADD COLUMN needs_intervention BOOL NOT NULL DEFAULT 0 AFTER additional_comments,
    ADD INDEX ix_crm_dhruva_leads_sent_at (sent_at),
    ADD INDEX ix_crm_dhruva_leads_needs_intervention (needs_intervention);

UPDATE crm_dhruva_leads SET sent_at = created_at WHERE sent_at IS NULL;   -- existing rows: sent when added

CREATE TABLE IF NOT EXISTS crm_dhruva_event_dates (
    id INTEGER NOT NULL AUTO_INCREMENT,
    lead_id INTEGER NOT NULL,
    event_date DATE NOT NULL,
    PRIMARY KEY (id),
    INDEX ix_crm_dhruva_event_dates_lead_id (lead_id),
    INDEX ix_crm_dhruva_event_dates_event_date (event_date),
    CONSTRAINT fk_crm_dhruva_event_dates_lead FOREIGN KEY (lead_id) REFERENCES crm_dhruva_leads (id)
);

-- Existing rows with a known date: that date as their one event date
INSERT INTO crm_dhruva_event_dates (lead_id, event_date)
SELECT id, event_date FROM crm_dhruva_leads WHERE event_date IS NOT NULL;

-- ── 3. Undo (only if needed, before any new lead is saved) ─────────────
-- DROP TABLE crm_dhruva_event_dates;
-- ALTER TABLE crm_dhruva_leads DROP INDEX ix_crm_dhruva_leads_sent_at,
--     DROP INDEX ix_crm_dhruva_leads_needs_intervention, DROP COLUMN sent_at, DROP COLUMN whatsapp_status,
--     DROP COLUMN response, DROP COLUMN message_sent, DROP COLUMN follow_up_notes, DROP COLUMN needs_intervention;
