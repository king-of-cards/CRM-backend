-- CRM MySQL migration for notifications and hot lead flags.
-- Do not run against the read-only Spin workflow PostgreSQL database.
-- The app's SQLModel.metadata.create_all() creates the three new tables at startup,
-- but it does not add columns to an existing crm_vendor_to_koc_leads table.

ALTER TABLE crm_vendor_to_koc_leads
    ADD COLUMN is_hot BOOL NOT NULL DEFAULT 0,
    ADD COLUMN hot_marked_by INTEGER NULL,
    ADD COLUMN hot_marked_at DATETIME NULL,
    ADD CONSTRAINT fk_crm_vendor_to_koc_leads_hot_marked_by
        FOREIGN KEY (hot_marked_by) REFERENCES crm_users (id);

CREATE TABLE IF NOT EXISTS crm_hot_leads (
    id INTEGER NOT NULL AUTO_INCREMENT,
    source VARCHAR(30) NOT NULL,
    lead_key VARCHAR(100) NOT NULL,
    hot_marked_by INTEGER NOT NULL,
    hot_marked_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_crm_hot_leads_source_key UNIQUE (source, lead_key),
    FOREIGN KEY (hot_marked_by) REFERENCES crm_users (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS crm_notifications (
    id INTEGER NOT NULL AUTO_INCREMENT,
    recipient_user_id INTEGER NULL,
    recipient_vendor_id INTEGER NULL,
    type VARCHAR(40) NOT NULL,
    title VARCHAR(200) NOT NULL,
    body VARCHAR(1000) NOT NULL,
    source VARCHAR(30) NULL,
    lead_key VARCHAR(100) NULL,
    lead_id INTEGER NULL,
    actor_user_id INTEGER NOT NULL,
    actor_name VARCHAR(100) NOT NULL,
    vendor_id INTEGER NULL,
    created_at DATETIME NOT NULL,
    read_at DATETIME NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_crm_notifications_one_recipient CHECK (
        (recipient_user_id IS NOT NULL AND recipient_vendor_id IS NULL) OR
        (recipient_user_id IS NULL AND recipient_vendor_id IS NOT NULL)
    ),
    FOREIGN KEY (recipient_user_id) REFERENCES crm_users (id),
    FOREIGN KEY (recipient_vendor_id) REFERENCES crm_vendors (id),
    FOREIGN KEY (actor_user_id) REFERENCES crm_users (id),
    FOREIGN KEY (vendor_id) REFERENCES crm_vendors (id),
    INDEX ix_crm_notifications_type (type),
    INDEX ix_crm_notifications_vendor_id (vendor_id),
    INDEX ix_crm_notifications_user_created (recipient_user_id, created_at),
    INDEX ix_crm_notifications_vendor_created (recipient_vendor_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS crm_notification_reads (
    id INTEGER NOT NULL AUTO_INCREMENT,
    notification_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    read_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_crm_notification_read_user UNIQUE (notification_id, user_id),
    FOREIGN KEY (notification_id) REFERENCES crm_notifications (id),
    FOREIGN KEY (user_id) REFERENCES crm_users (id),
    INDEX ix_crm_notification_reads_notification_id (notification_id),
    INDEX ix_crm_notification_reads_user (user_id, read_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Notifications older than 90 days are pruned on notification reads/creation.
