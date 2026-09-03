# 02 · 数据模型

SQLite，WAL 模式。所有时间戳存 UTC ISO8601 文本（SQLite 无原生时间类型，
文本格式可比较可索引，且便于人工排查）。

启动时 pragma：

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;   -- WAL 下足够安全，减少 fsync
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA cache_size   = -32000;   -- 32MB 上限，控制内存
```

## 1. 用户与鉴权

### users
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| username | TEXT UNIQUE NOT NULL | 1–64 位 ASCII `[a-z0-9_.-]`，入库存小写；NOCASE 唯一作纵深防御 |
| email | TEXT UNIQUE | 可空；预留联系信息，当前无邮件找回/投递流程 |
| password_hash | TEXT NOT NULL | Argon2id |
| role | TEXT NOT NULL | `admin` / `user` |
| totp_secret | TEXT | 加密存储；空表示未开 2FA |
| is_active | INTEGER NOT NULL DEFAULT 1 | 停用而非删除 |
| failed_logins | INTEGER NOT NULL DEFAULT 0 | 限流用 |
| locked_until | TEXT | 锁定截止时间 |
| must_change_password | INTEGER NOT NULL DEFAULT 0 | admin 创建/重置的临时密码必须在首次登录后更换 |
| created_at / updated_at | TEXT NOT NULL | |

### sessions
| 列 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | 随机 ID（非密钥本体） |
| user_id | INTEGER FK→users ON DELETE CASCADE | |
| token_hash | TEXT NOT NULL UNIQUE | 会话令牌的 SHA-256；**明文只存在 Cookie 里** |
| expires_at | TEXT NOT NULL | |
| created_ip / user_agent | TEXT | 便于用户查看活跃会话 |
| revoked_at | TEXT | 支持"登出所有设备" |

索引：`idx_sessions_user (user_id)`、`idx_sessions_expires (expires_at)`

## 2. 站点与文章

### sites
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| slug | TEXT UNIQUE NOT NULL | 配置文件里的键名 |
| name | TEXT NOT NULL | 展示名 |
| base_url | TEXT NOT NULL | |
| discovery_mode | TEXT NOT NULL | `rss` / `sitemap` / `html_list` / `json_api` |
| tagset_group_id | INTEGER FK→tagset_groups NOT NULL | 该站点的文章用哪组标签打标 |
| config_json | TEXT NOT NULL | 该模式的完整参数（选择器、分页规则等） |
| enabled | INTEGER NOT NULL DEFAULT 1 | |
| crawl_interval_sec | INTEGER NOT NULL DEFAULT 1800 | |
| render_js | INTEGER NOT NULL DEFAULT 0 | 是否走 Playwright |
| last_crawled_at | TEXT | |
| last_error | TEXT | |
| consecutive_failures | INTEGER NOT NULL DEFAULT 0 | |

站点运行期配置以 **DB 为唯一事实来源**。启动时，YAML `sites[]` 仅在库中
不存在对应 slug 时导入一次；已存在站点不被 YAML 覆盖。Web 接入向导直接写 DB。
这样既保留初始声明式部署，也避免 Web 与 YAML 互相覆盖。若要重新导入，必须通过
显式管理操作而非重启时隐式同步。

### articles
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| site_id | INTEGER FK→sites | |
| url | TEXT NOT NULL | 原始 URL |
| url_hash | TEXT NOT NULL UNIQUE | 规范化 URL 的 SHA-256，去重主键 |
| title | TEXT | |
| author | TEXT | |
| published_at | TEXT | 站点声明的发布时间 |
| summary | TEXT | 提取或生成的摘要 |
| content_text | TEXT | 纯文本正文，打标与推送用 |
| content_html | TEXT | 清洗后的 HTML，Web 端阅读用 |
| lang | TEXT | 检测到的语言 |
| simhash | TEXT | 正文 simhash，跨站转载去重 |
| word_count | INTEGER | |
| status | TEXT NOT NULL | 见 [01-architecture.md](01-architecture.md) §3 |
| attempts | INTEGER NOT NULL DEFAULT 0 | 当前阶段重试次数 |
| next_attempt_at | TEXT | 退避后的下次尝试时间 |
| last_error | TEXT | |
| discovered_at / fetched_at / tagged_at | TEXT | |

索引：
```
idx_articles_status_next   (status, next_attempt_at)   -- 调度扫描主索引
idx_articles_site_pub      (site_id, published_at DESC)
idx_articles_simhash       (simhash)
```

去重两层：`url_hash` 精确去重；`simhash` 汉明距离 ≤ 3 视为转载，
仅记录关联不重复推送。

### attachments
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| article_id | INTEGER FK→articles ON DELETE CASCADE | |
| source_url | TEXT NOT NULL | |
| filename | TEXT | 清洗后的安全文件名 |
| mime_type | TEXT | |
| size_bytes | INTEGER | |
| sha256 | TEXT | 内容哈希，跨文章去重 |
| local_path | TEXT | 当前写入规范化绝对路径；读取层兼容旧版根目录内相对路径 |
| status | TEXT NOT NULL | `pending` / `downloaded` / `skipped` / `failed` |
| skip_reason | TEXT | 超限/类型不允许等 |

索引：`idx_attachments_article (article_id)`、`idx_attachments_sha (sha256)`

同一 `sha256` 只存一份物理文件，多条记录共享 `local_path`（引用计数式清理）。

## 3. 标签（冻结）

### tagset_groups

标签集分组。**每组独立生成、独立冻结、互不影响**——这是多站点异构主题的解。

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| slug | TEXT UNIQUE NOT NULL | 配置中 `tagset_group` 引用此值 |
| name | TEXT NOT NULL | 展示名，如「校园教务」 |
| description | TEXT | 该组的主题范围说明 |
| tagset_version | TEXT | 本组当前生效版本 |
| build_mode | TEXT | `llm` / `embedding`，决定是否有质心 |
| status | TEXT NOT NULL | `draft` / `frozen` |
| frozen_at | TEXT | |
| created_at | TEXT NOT NULL | |

至少存在一个默认组。只有一个组时，系统行为与无分组完全一致——
分组是向下兼容的，加组成本为零。

### tags
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 订阅关系绑定此 ID，永不复用 |
| group_id | INTEGER FK→tagset_groups NOT NULL | 所属分组 |
| slug | TEXT NOT NULL | 稳定标识 |
| name | TEXT NOT NULL | 人类可读名 |
| description | TEXT | 供 LLM prompt 使用的判定说明 |
| keywords | TEXT | JSON 数组，辅助召回与解释 |
| threshold | REAL NOT NULL DEFAULT 0.35 | 本地兜底的余弦相似度阈值 |
| tagset_version | TEXT NOT NULL | 与该组 `{group}/tags.json` 的 checksum 对应 |
| frozen_at | TEXT NOT NULL | 冻结时间 |

**唯一约束改为 `(group_id, slug)`**，不再是全局 `slug` 唯一。
不同组可以有同名标签（如两个组都有「通知公告」），语义由组决定。

索引：`idx_tags_group (group_id)`

### tag_vectors
存每个标签的质心向量，供本地兜底做相似度检索与 LLM 路径的候选预筛。
按 `tag_id` 关联，分组信息从 `tags` 表带出，不重复存储。

M0 使用可移植的普通表，embedding 以 little-endian float32 BLOB 存储：

```sql
CREATE TABLE tag_vectors (
  tag_id INTEGER PRIMARY KEY REFERENCES tags(id) ON DELETE CASCADE,
  dim INTEGER NOT NULL,
  embedding BLOB NOT NULL
);
```

默认标签量仅 30–80 个，普通内存余弦比对足够快，也避免在本地 ONNX 默认关闭时
强制安装 SQLite 扩展。M3 启用本地兜底后可增加迁移，将其替换为 sqlite-vec `vec0`
虚拟表；上层仓储接口保持不变。

### article_tags
| 列 | 类型 | 说明 |
|---|---|---|
| article_id | INTEGER FK→articles ON DELETE CASCADE | |
| tag_id | INTEGER FK→tags | |
| confidence | REAL NOT NULL | 0–1 |
| backend | TEXT NOT NULL | `llm:<provider>:<model>` 或 `local:<model>` |
| created_at | TEXT NOT NULL | |

主键 `(article_id, tag_id)`。索引 `idx_article_tags_tag (tag_id, confidence DESC)`。

记录 `backend` 的意义：将来发现某个 provider 打标质量差，可以定向重跑，
而不必全量重打。

## 4. 订阅与投递

### subscriptions
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| user_id | INTEGER FK→users ON DELETE CASCADE | |
| name | TEXT NOT NULL | 用户自定义名称 |
| match_mode | TEXT NOT NULL DEFAULT 'any' | `any` / `all` |
| min_confidence | REAL NOT NULL DEFAULT 0.5 | |
| site_filter | TEXT | JSON 数组；空=全部站点 |
| include_attachments | INTEGER NOT NULL DEFAULT 1 | |
| quiet_hours | TEXT | 如 `23:00-07:00`，静默期缓冲到期后发 |
| enabled | INTEGER NOT NULL DEFAULT 1 | |

### subscription_tags
`(subscription_id, tag_id)` 复合主键。

订阅可跨组选标签（一个订阅同时关注「校园教务/考试安排」与「技术/开源」）。
但匹配时需注意：`match_mode: all` 跨组时永不可能命中（一篇文章只属于一个
站点、只用一组标签打标）。Web 端在用户选了跨组标签 + `all` 时应直接告警。

### notify_targets
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| user_id | INTEGER FK→users ON DELETE CASCADE | |
| name | TEXT NOT NULL | |
| apprise_url_enc | BLOB NOT NULL | **加密存储**，内含 token |
| url_fingerprint | TEXT | 脱敏展示用（如 `tgram://***1234`） |
| enabled | INTEGER NOT NULL DEFAULT 1 | |
| last_ok_at / last_error | TEXT | |

订阅与推送目标是多对多（一个订阅可发多个渠道），
用 `subscription_targets (subscription_id, target_id)` 关联；
不配则默认发到该用户所有启用的 target。

### deliveries
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| subscription_id | INTEGER FK→subscriptions ON DELETE CASCADE | |
| article_id | INTEGER FK→articles ON DELETE CASCADE | |
| target_id | INTEGER FK→notify_targets ON DELETE CASCADE | |
| status | TEXT NOT NULL | `pending` / `sent` / `failed` / `skipped` |
| attempts | INTEGER NOT NULL DEFAULT 0 | |
| next_attempt_at | TEXT | |
| last_error | TEXT | |
| claim_token / claim_until | TEXT | 跨进程外发租约，避免 CLI 与调度器重复发送 |
| sent_at | TEXT | |

**唯一约束 `(subscription_id, article_id, target_id)`** —— 这是投递去重的
唯一保证，靠数据库而不是应用逻辑来保证"同一篇文章不重复推给同一个人"。
插入用 `INSERT OR IGNORE`，天然幂等。

索引：`idx_deliveries_status_next (status, next_attempt_at)`

## 5. 运维表

### provider_health
| 列 | 类型 | 说明 |
|---|---|---|
| provider | TEXT PK | 配置中的 provider 名 |
| consecutive_failures | INTEGER NOT NULL DEFAULT 0 | |
| cooldown_until | TEXT | 熔断冷却截止 |
| last_error | TEXT | |
| total_calls / total_failures | INTEGER | 用于 Web 端展示可用率 |
| updated_at | TEXT | |

### audit_log
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| user_id | INTEGER FK→users ON DELETE SET NULL | 系统操作为 NULL |
| action | TEXT NOT NULL | `login` / `login_failed` / `sub_create` / `site_update` … |
| target_type / target_id | TEXT / INTEGER | |
| detail | TEXT | JSON |
| ip | TEXT | |
| created_at | TEXT NOT NULL | |

公网暴露的系统需要审计。索引 `idx_audit_user_time (user_id, created_at DESC)`。

### schema_migrations
| 列 | 类型 |
|---|---|
| version | INTEGER PK |
| applied_at | TEXT NOT NULL |

迁移用朴素的编号 SQL 文件（`storage/migrations/001_init.sql` …），
不引入 Alembic——单库单写者场景下它的复杂度不划算。

## 6. 数据保留

`housekeeping` 任务按配置清理：

- `articles`：超过 `retention.article_days`（默认 180）且已 `NOTIFIED` 的，
  清空 `content_html` 但保留元数据与标签（供统计与去重）
- `attachments`：超过 `retention.attachment_days`（默认 30）且无 pending 投递引用的，
  删物理文件并置 `local_path = NULL`
- `sessions`：过期即删
- `audit_log`：超过 `retention.audit_days`（默认 90）
- 之后执行 `PRAGMA incremental_vacuum`

附件是磁盘占用主因，小 VPS 上这条保留策略必须默认开启。
