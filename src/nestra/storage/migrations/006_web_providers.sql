CREATE TABLE llm_providers (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    type            TEXT NOT NULL CHECK (type IN ('openai_compatible', 'gemini', 'anthropic')),
    base_url        TEXT,
    models_json     TEXT NOT NULL,
    max_input_chars INTEGER NOT NULL DEFAULT 8000 CHECK (max_input_chars >= 500),
    api_key_enc     BLOB NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
