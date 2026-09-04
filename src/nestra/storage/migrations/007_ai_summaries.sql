ALTER TABLE articles ADD COLUMN summary_backend TEXT;
ALTER TABLE articles ADD COLUMN summarized_at TEXT;

CREATE TABLE ai_summary_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    provider TEXT,
    model TEXT,
    updated_at TEXT,
    CHECK (
        enabled = 0 OR (
            length(trim(COALESCE(provider, ''))) > 0
            AND length(trim(COALESCE(model, ''))) > 0
        )
    )
);

INSERT INTO ai_summary_settings (id, enabled) VALUES (1, 0);
