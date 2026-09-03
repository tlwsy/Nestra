-- M5 Web authentication additions.

CREATE TABLE recovery_codes (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash  TEXT    NOT NULL UNIQUE,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (user_id, code_hash)
);

CREATE INDEX idx_deliveries_user_lookup
    ON deliveries(subscription_id, status, article_id);
