-- 001_init.sql —— 初始 schema
-- 对应 docs/02-data-model.md
-- 时间戳统一为 UTC ISO8601 文本（SQLite 无原生时间类型，文本可比可索引）

-- ── 用户与鉴权 ────────────────────────────────────────────────

CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL COLLATE NOCASE UNIQUE
                          CHECK (length(username) BETWEEN 1 AND 64
                                 AND username = lower(username)
                                 AND username NOT GLOB '*[^a-z0-9_.-]*'),
    email         TEXT    UNIQUE,
    password_hash TEXT    NOT NULL,                 -- Argon2id
    role          TEXT    NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    totp_secret   TEXT,                             -- 加密存储；NULL=未开 2FA
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until  TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE sessions (
    id           TEXT    PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash   TEXT    NOT NULL UNIQUE,           -- SHA-256；明文只在 Cookie
    expires_at   TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    created_ip   TEXT,
    user_agent   TEXT,
    revoked_at   TEXT
);

CREATE INDEX idx_sessions_user    ON sessions(user_id);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);

-- ── 标签集分组（先建，sites 要引用）───────────────────────────

CREATE TABLE tagset_groups (
    id              INTEGER PRIMARY KEY,
    slug            TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    description     TEXT,
    tagset_version  TEXT,
    build_mode      TEXT    CHECK (build_mode IN ('llm', 'embedding')),
    status          TEXT    NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'frozen')),
    frozen_at       TEXT,
    created_at      TEXT    NOT NULL
);

-- ── 站点与文章 ────────────────────────────────────────────────
-- 运行期真值在本表。YAML 的 sites[] 仅在库中无该 slug 时导入一次，
-- 之后以 DB 为准（向导写库）。见 docs/08 §修改配置后如何生效。

CREATE TABLE sites (
    id                   INTEGER PRIMARY KEY,
    slug                 TEXT    NOT NULL UNIQUE,
    name                 TEXT    NOT NULL,
    base_url             TEXT    NOT NULL,
    discovery_mode       TEXT    NOT NULL
                             CHECK (discovery_mode IN ('rss', 'sitemap', 'html_list', 'json_api')),
    tagset_group_id      INTEGER NOT NULL REFERENCES tagset_groups(id),
    config_json          TEXT    NOT NULL,          -- 该模式的完整参数
    enabled              INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    crawl_interval_sec   INTEGER NOT NULL DEFAULT 1800,
    render_js            INTEGER NOT NULL DEFAULT 0 CHECK (render_js IN (0, 1)),
    source               TEXT    NOT NULL DEFAULT 'yaml'
                             CHECK (source IN ('yaml', 'wizard')),
    last_crawled_at      TEXT,
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL
);

CREATE INDEX idx_sites_enabled ON sites(enabled, last_crawled_at);

CREATE TABLE articles (
    id              INTEGER PRIMARY KEY,
    site_id         INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    url             TEXT    NOT NULL,
    url_hash        TEXT    NOT NULL UNIQUE,        -- 规范化 URL 的 SHA-256
    title           TEXT,
    author          TEXT,
    published_at    TEXT,
    summary         TEXT,
    content_text    TEXT,
    content_html    TEXT,
    lang            TEXT,
    simhash         TEXT,
    word_count      INTEGER,
    status          TEXT    NOT NULL DEFAULT 'DISCOVERED'
                        CHECK (status IN ('DISCOVERED', 'FETCHED', 'EXTRACTED',
                                          'TAGGED', 'NOTIFIED', 'FAILED', 'SKIPPED')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT,
    discovered_at   TEXT    NOT NULL,
    fetched_at      TEXT,
    tagged_at       TEXT
);

CREATE INDEX idx_articles_status_next ON articles(status, next_attempt_at);
CREATE INDEX idx_articles_site_pub    ON articles(site_id, published_at DESC);
CREATE INDEX idx_articles_simhash     ON articles(simhash);

CREATE TABLE attachments (
    id          INTEGER PRIMARY KEY,
    article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    source_url  TEXT    NOT NULL,
    filename    TEXT,
    mime_type   TEXT,
    size_bytes  INTEGER,
    sha256      TEXT,
    local_path  TEXT,
    status      TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'downloaded', 'skipped', 'failed')),
    skip_reason TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    UNIQUE (article_id, source_url)
);

CREATE INDEX idx_attachments_article ON attachments(article_id);
CREATE INDEX idx_attachments_sha     ON attachments(sha256);
CREATE INDEX idx_attachments_status  ON attachments(status);

-- ── 标签（冻结）───────────────────────────────────────────────

CREATE TABLE tags (
    id             INTEGER PRIMARY KEY,             -- 订阅绑定此 ID，永不复用
    group_id       INTEGER NOT NULL REFERENCES tagset_groups(id),
    slug           TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    description    TEXT,
    keywords       TEXT,                            -- JSON 数组
    threshold      REAL    NOT NULL DEFAULT 0.35,
    tagset_version TEXT    NOT NULL,
    frozen_at      TEXT    NOT NULL,
    UNIQUE (group_id, slug)                         -- 不同组可同名
);

CREATE INDEX idx_tags_group ON tags(group_id);

CREATE TABLE article_tags (
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id),
    confidence REAL    NOT NULL,
    backend    TEXT    NOT NULL,                    -- llm:<provider>:<model> | local:<model>
    created_at TEXT    NOT NULL,
    PRIMARY KEY (article_id, tag_id)
);

CREATE INDEX idx_article_tags_tag ON article_tags(tag_id, confidence DESC);

-- 质心向量。sqlite-vec 可用时由 002 迁移替换为 vec0 虚拟表；
-- 此处用普通表存 BLOB，保证无扩展也能跑（暴力比对 40 个标签足够快）。
CREATE TABLE tag_vectors (
    tag_id    INTEGER PRIMARY KEY REFERENCES tags(id) ON DELETE CASCADE,
    dim       INTEGER NOT NULL,
    embedding BLOB    NOT NULL                      -- float32 little-endian
);

-- ── 订阅与投递 ────────────────────────────────────────────────

CREATE TABLE subscriptions (
    id                  INTEGER PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                TEXT    NOT NULL,
    match_mode          TEXT    NOT NULL DEFAULT 'any' CHECK (match_mode IN ('any', 'all')),
    min_confidence      REAL    NOT NULL DEFAULT 0.5,
    site_filter         TEXT,                        -- JSON 数组；NULL=全部
    include_attachments INTEGER NOT NULL DEFAULT 1 CHECK (include_attachments IN (0, 1)),
    quiet_hours         TEXT,                        -- 如 '23:00-07:00'
    enabled             INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE INDEX idx_subscriptions_user ON subscriptions(user_id, enabled);

CREATE TABLE subscription_tags (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    tag_id          INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (subscription_id, tag_id)
);

CREATE INDEX idx_subscription_tags_tag ON subscription_tags(tag_id);

CREATE TABLE notify_targets (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    apprise_url_enc BLOB    NOT NULL,                -- 加密；内含 token
    url_fingerprint TEXT,                            -- 脱敏展示
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_ok_at      TEXT,
    last_error      TEXT,
    created_at      TEXT    NOT NULL
);

CREATE INDEX idx_notify_targets_user ON notify_targets(user_id, enabled);

CREATE TABLE subscription_targets (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    target_id       INTEGER NOT NULL REFERENCES notify_targets(id) ON DELETE CASCADE,
    PRIMARY KEY (subscription_id, target_id)
);

CREATE TABLE deliveries (
    id              INTEGER PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    article_id      INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    target_id       INTEGER NOT NULL REFERENCES notify_targets(id) ON DELETE CASCADE,
    status          TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT,
    created_at      TEXT    NOT NULL,
    sent_at         TEXT,
    -- 投递去重的唯一保证：靠 DB 而非应用逻辑
    UNIQUE (subscription_id, article_id, target_id)
);

CREATE INDEX idx_deliveries_status_next ON deliveries(status, next_attempt_at);
CREATE INDEX idx_deliveries_article     ON deliveries(article_id);

-- ── 运维 ──────────────────────────────────────────────────────

CREATE TABLE provider_health (
    provider             TEXT    PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    cooldown_until       TEXT,
    last_error           TEXT,
    total_calls          INTEGER NOT NULL DEFAULT 0,
    total_failures       INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT    NOT NULL
);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- 系统操作为 NULL
    action      TEXT    NOT NULL,
    target_type TEXT,
    target_id   INTEGER,
    detail      TEXT,                                -- JSON
    ip          TEXT,
    created_at  TEXT    NOT NULL
);

CREATE INDEX idx_audit_user_time ON audit_log(user_id, created_at DESC);
CREATE INDEX idx_audit_action    ON audit_log(action, created_at DESC);
