-- 条件请求校验器；列表页没有 article 行，需单独持久化。
CREATE TABLE fetch_cache (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    updated_at    TEXT NOT NULL
);
