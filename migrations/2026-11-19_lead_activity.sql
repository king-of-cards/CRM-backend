-- CRM MySQL migration. Never apply to the read-only Spin workflow database.
-- Run the ALTER TABLE statement only once; it adds the CRM follow-up date column.
ALTER TABLE crm_lead_meta ADD COLUMN follow_up_date DATE NULL;

CREATE TABLE IF NOT EXISTS crm_lead_activity (
    id INTEGER NOT NULL AUTO_INCREMENT,
    source VARCHAR(30) NOT NULL,
    lead_key VARCHAR(100) NOT NULL,
    action VARCHAR(30) NOT NULL,
    field VARCHAR(100) NULL,
    old_value TEXT NULL,
    new_value TEXT NULL,
    actor_user_id INTEGER NULL,
    actor_name VARCHAR(100) NOT NULL,
    actor_role VARCHAR(30) NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY (actor_user_id) REFERENCES crm_users (id),
    INDEX ix_crm_lead_activity_source_key_created (source, lead_key, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
