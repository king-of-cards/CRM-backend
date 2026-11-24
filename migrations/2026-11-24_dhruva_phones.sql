-- CRM MySQL migration: Dhruva -> KOC phone numbers as a list (crm_dhruva_phones, one row per number).
-- Do not run against the read-only Spin workflow PostgreSQL database. Run AFTER
-- 2026-11-23_dhruva_to_koc_fields.sql and BEFORE starting the new code.
--
-- A lead now has any number of phone numbers (10 digits each) in crm_dhruva_phones. crm_dhruva_leads
-- keeps phone / phone_e164 as the FIRST number (reminders use it); alt_phone / alt_phone_e164 are
-- moved into the new table and dropped. Only Indian numbers (+91 and 10 digits) are moved; the
-- preview lists any other number (none expected: the table is empty now).

-- ── 1. Preview (read only) ─────────────────────────────────────────────
SELECT COUNT(*) AS leads,
       SUM(phone_e164 IS NOT NULL) AS with_phone,
       SUM(alt_phone_e164 IS NOT NULL) AS with_alt_phone
FROM crm_dhruva_leads;
SELECT id, phone, phone_e164, alt_phone, alt_phone_e164 FROM crm_dhruva_leads    -- not moved (not +91 / 10 digits)
WHERE (phone_e164 IS NOT NULL AND NOT (phone_e164 LIKE '+91%' AND CHAR_LENGTH(phone_e164) = 13))
   OR (alt_phone_e164 IS NOT NULL AND NOT (alt_phone_e164 LIKE '+91%' AND CHAR_LENGTH(alt_phone_e164) = 13));

-- ── 2. Apply ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crm_dhruva_phones (
    id INTEGER NOT NULL AUTO_INCREMENT,
    lead_id INTEGER NOT NULL,
    phone VARCHAR(10) NOT NULL,
    phone_e164 VARCHAR(16) NOT NULL,
    PRIMARY KEY (id),
    INDEX ix_crm_dhruva_phones_lead_id (lead_id),
    INDEX ix_crm_dhruva_phones_phone (phone),
    INDEX ix_crm_dhruva_phones_phone_e164 (phone_e164),
    CONSTRAINT fk_crm_dhruva_phones_lead FOREIGN KEY (lead_id) REFERENCES crm_dhruva_leads (id)
);

INSERT INTO crm_dhruva_phones (lead_id, phone, phone_e164)          -- the phone first, then the alternate
SELECT id, RIGHT(phone_e164, 10), phone_e164 FROM crm_dhruva_leads
WHERE phone_e164 LIKE '+91%' AND CHAR_LENGTH(phone_e164) = 13
ORDER BY id;
INSERT INTO crm_dhruva_phones (lead_id, phone, phone_e164)
SELECT id, RIGHT(alt_phone_e164, 10), alt_phone_e164 FROM crm_dhruva_leads
WHERE alt_phone_e164 LIKE '+91%' AND CHAR_LENGTH(alt_phone_e164) = 13
  AND (phone_e164 IS NULL OR alt_phone_e164 <> phone_e164)
ORDER BY id;

UPDATE crm_dhruva_leads SET phone = RIGHT(phone_e164, 10)            -- phone = the first number's 10 digits
WHERE phone_e164 LIKE '+91%' AND CHAR_LENGTH(phone_e164) = 13;

ALTER TABLE crm_dhruva_leads
    DROP INDEX ix_crm_dhruva_leads_alt_phone_e164,
    DROP COLUMN alt_phone,
    DROP COLUMN alt_phone_e164;
