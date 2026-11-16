-- One-time migration for the CRM MySQL database (never run this on the spin PostgreSQL DB).
--
-- 1. Creates crm_dhruva_leads ("Dhruva leads"). The app's create_all() would also create it on
--    startup; it is here so the change can be reviewed and applied on its own.
--      name, venue, event_type                as written (line breaks in event type become ", ")
--      event_date_text                        the date as typed ("Dec 4th", "Feb 24&25th 2027", "Nil")
--      event_date                             parsed when clear, else NULL (ranges, "or", month only)
--      phone / alt_phone                      first / second number of the cell, as written
--      phone_e164 / alt_phone_e164            the same in E.164 (+919845012345); NULL = no number
--      source                                 manual | bulk_upload
--      update_status ... status_updated_at    admin-only fields (update_status NULL = Pending)
-- 2. Adds an index on crm_vendor_to_koc_leads.phone (cross-source duplicate lookups by phone;
--    the unique key (vendor_id, phone) can't be used for a phone-only lookup).
--
-- Dhruva logins use crm_users.role = 'dhruva' (VARCHAR(20) already): no change to crm_users.
-- Run it before starting the new code. Both statements are safe on a live database (new table;
-- the index is built online on a small table).

CREATE TABLE IF NOT EXISTS crm_dhruva_leads (
    id INTEGER NOT NULL AUTO_INCREMENT,
    name VARCHAR(200),
    venue VARCHAR(300),
    event_date_text VARCHAR(200),
    event_date DATE,
    phone VARCHAR(32),
    alt_phone VARCHAR(32),
    phone_e164 VARCHAR(16),
    alt_phone_e164 VARCHAR(16),
    event_type VARCHAR(200),
    source VARCHAR(20) NOT NULL,
    update_status VARCHAR(30),
    additional_comments TEXT,
    quoted_amount INTEGER,
    conversion_amount INTEGER,
    follow_up_date DATE,
    status_updated_by INTEGER,
    status_updated_at DATETIME,
    created_by INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    is_deleted BOOL NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    FOREIGN KEY (created_by) REFERENCES crm_users (id),
    FOREIGN KEY (status_updated_by) REFERENCES crm_users (id),
    INDEX ix_crm_dhruva_leads_phone_e164 (phone_e164),
    INDEX ix_crm_dhruva_leads_alt_phone_e164 (alt_phone_e164),
    INDEX ix_crm_dhruva_leads_event_date (event_date),
    INDEX ix_crm_dhruva_leads_created_at (created_at),
    INDEX ix_crm_dhruva_leads_update_status (update_status),
    INDEX ix_crm_dhruva_leads_is_deleted (is_deleted)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE crm_vendor_to_koc_leads ADD INDEX ix_crm_vendor_to_koc_leads_phone (phone);

-- Check 1: the table exists and is empty (expect 0)
-- SELECT COUNT(*) FROM crm_dhruva_leads;
-- Check 2: the new index exists (expect one row)
-- SHOW INDEX FROM crm_vendor_to_koc_leads WHERE Key_name = 'ix_crm_vendor_to_koc_leads_phone';
-- Check 3 (after creating Dhruva logins): SELECT id, name, email FROM crm_users WHERE role = 'dhruva';
--
-- Rollback (deletes every Dhruva lead; deploy the previous code first; Dhruva logins then can't
-- log in to anything useful: change or delete them first):
-- DROP TABLE crm_dhruva_leads;
-- ALTER TABLE crm_vendor_to_koc_leads DROP INDEX ix_crm_vendor_to_koc_leads_phone;
