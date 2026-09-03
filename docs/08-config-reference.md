# 08 · 配置参考

两个来源，职责严格分离：

| 文件 | 内容 | 可否提交/分享 |
|---|---|---|
| `config/config.yaml` | 全部行为配置 | **可以**（不含机密） |
| `.env` | 全部机密 | **不可以**（`.gitignore` 必须包含） |

这个分离让配置文件能在 Web 端展示、能贴到 issue 里求助、能进版本控制，
而不用担心泄漏 key。

模板：`config/config.example.yaml`、`config/env.example`（后者复制为根目录 `.env`）。

## 加载与校验

`core/config.py` 用 `pydantic-settings`：

1. 读 `config/config.yaml`
2. 环境变量覆盖（前缀 `NESTRA__`，双下划线表示层级，
   如 `NESTRA__WEB__PORT=9000`）
3. pydantic 模型校验类型与取值范围
4. 交叉校验（见下）
5. 校验失败 → **拒绝启动**并打印具体路径与原因，不使用默认值兜底

交叉校验规则（这些是运行期最容易踩的坑，必须在启动时就拦住）：

- 每个 provider 的 `api_key_env` 指向的环境变量必须存在且非空，
  否则该 provider 标记为不可用并 warning（不阻止启动——
  用户可能故意只配一个）
- 默认降级策略下全部 provider 不可用且 local 关闭 → warning；抓取继续，文章停在
  `EXTRACTED`。显式 `llm_only` / `local_only` 所需后端不可用时才拒绝启动
- `local.enabled=true` 但 `model_path` 文件不存在 → warning，
  运行期视为兜底不可用
- `local.enabled=true` 但 `tags.json` 的 `build_mode` 为 `llm`（无质心）
  → warning，提示先跑 `scripts/backfill_centroids.py`
- `tagset_groups[].build_mode=embedding` 但 `tagger.local.enabled=false` → warning；
  bootstrap extra 仍可独立生成标签，只有运行期本地兜底保持关闭
- `web.cookie_secure=true` 且 `base_url` 是 `http://` → warning
  （会导致 Cookie 不下发，登录静默失败）
- `base_url` 是 `https://` 但 `cookie_secure=false` → 拒绝启动
- `sites[].base_url` 必须是无凭据、路径、查询参数或片段的 HTTP(S) 源站
- `sites[].discovery_mode` 与 `config` 的字段必须匹配（各模式有独立子模型）
- `sites[].tagset_group` 必须存在于 `tagset_groups[]` → 否则拒绝启动
- 某个 `tagset_group` 尚未冻结（`status: draft`）→ warning，该组站点的文章
  正常存档但不打标、不推送
- Web 订阅跨标签集分组选择标签/站点 → 拒绝保存
- `render_js=true` 但镜像未装 Playwright → **拒绝启动**，避免运行期静默提取空正文
- `attachments.max_size_mb` > `notify.attachment_inline_max_mb` 时提示
  超限附件会走链接模式

## 配置项说明

### app
| 键 | 默认 | 说明 |
|---|---|---|
| `timezone` | `Asia/Shanghai` | 影响静默时段、cron、展示时间。存储始终 UTC |
| `log_level` | `INFO` | |
| `log_format` | `json` | `json` 便于机器处理，`console` 便于本地开发 |

### runtime
| 键 | 默认 | 说明 |
|---|---|---|
| `web_workers` | 1 | 2 核机器不建议 > 1 |
| `crawl_concurrency` | 4 | 同站点并发请求 |
| `playwright_concurrency` | 1 | **不要调高**，每实例 300–500MB |
| `sqlite_cache_mb` | 32 | 直接影响常驻内存 |

### web
| 键 | 默认 | 说明 |
|---|---|---|
| `host` / `port` | `127.0.0.1` / 8080 | 本地默认仅回环；Compose 在容器内覆盖 host 为 `0.0.0.0`，宿主仍仅回环 |
| `base_url` | — | 生成推送里的绝对链接、签名附件链接 |
| `cookie_secure` | `true` | 无 TLS 时须改 false，且此时不应公网暴露 |
| `session_days` | 14 | 固定服务端有效期；未勾选 remember 时 Cookie 为浏览器会话级 |
| `trusted_proxies` | 官方模板含回环与 Docker 私网 | 必须按实际反代来源收窄；配错会影响真实客户端 IP 与限流 |

无 `allow_registration` 项——本项目不提供开放注册，用户由 admin 创建。
理由见 [06-web-auth.md](06-web-auth.md) §0。

### tagset_groups[]
| 键 | 说明 |
|---|---|
| `slug` | 唯一标识，`sites[].tagset_group` 引用此值 |
| `name` | 展示名 |
| `description` | 主题范围说明，供生成阶段的 LLM 参考 |
| `min_docs_for_build` | 默认 200。未达到时拒绝跑阶段一 |

至少定义一个组。只有一个组时行为与无分组完全一致，详见
[04-tagger.md](04-tagger.md) §1.6。

### sites[]
| 键 | 说明 |
|---|---|
| `slug` | 唯一标识，与 DB `sites.slug` 对应，改动等于换站点 |
| `tagset_group` | 必填。该站文章用哪组标签打标 |
| `discovery_mode` | `rss` / `sitemap` / `html_list` / `json_api`，各模式的 `config` 结构见 [03-crawler.md](03-crawler.md) §2 |
| `render_js` | 默认 false。先按 false 试，正文异常短再开 |
| `crawl_interval_sec` | 默认 1800。100 篇/天的站点没必要更频繁 |
| `extract.selectors` | 可选，覆盖 trafilatura。站点改版后的修复手段 |
| `url_allow_pattern` | 列表页链接白名单。不配会把跨域条目抓进来 |
| `url_canonical.rules` | 同一文章多 URL 形态的归一规则，不配会重复推送 |
| `pagination.order` | `asc` / `desc_index`。倒序分页站点必配 |
| `attachments.link_patterns` | 附件链接识别正则。**不能只靠扩展名** |

选择器语法：`selector` 取文本，`selector@attr` 取属性。

#### 首站完整配置示例

基于实测结果（详见 [10-site-probe-ujs-jwc.md](10-site-probe-ujs-jwc.md)），
可直接作为 `config.example.yaml` 的内置站点：

```yaml
tagset_groups:
  - slug: campus
    name: 校园教务
    description: 高校教务处、学院通知公告类内容

sites:
  - slug: ujs-jwc
    name: 江苏大学教务处
    tagset_group: campus
    base_url: https://jwc.ujs.edu.cn  # 仅源站；不含凭据、路径、查询参数或片段
    enabled: true
    render_js: false                 # 实测不需要
    crawl_interval_sec: 1800

    discovery_mode: html_list         # 无 RSS / 无 sitemap
    config:
      list_urls:
        - https://jwc.ujs.edu.cn/index/tzgg.htm      # 通知公告
      item_selector: 'li[id^="line_"]'
      url_allow_pattern: '^https://jwc\.ujs\.edu\.cn/(info/\d+/\d+\.htm|content\.jsp)'
      fields:
        url: 'a.title.tt1@href'
        title: 'a.title.tt1@title'    # 属性才是完整标题
        published_at: 'p.date'
      date_format: '%Y-%m-%d'
      pagination:
        mode: url_template
        template: 'https://jwc.ujs.edu.cn/index/tzgg/{page}.htm'
        order: desc_index             # 页号越小越旧
        max_page: 78
        max_pages: 1                  # 增量只看入口页

    url_canonical:
      rules:
        - match: 'content\.jsp'
          extract_params: [wbtreeid, wbnewsid]
          rewrite: '/info/{wbtreeid}/{wbnewsid}.htm'
      strip_params: [urltype]

    extract:
      selectors:
        title: 'h1.title'
        content: 'div.v_news_content'
        published_at_regex: '发布时间：\s*([\d-]+)'
      min_content_length: 100

    attachments:
      enabled: true
      link_patterns:
        - 'download\.jsp|DownloadAttachUrl'
        - '\.(pdf|docx?|xlsx?|pptx?|zip|rar)(\?|$)'
      inline_image_patterns:
        - '/__local/'
      send_referer: true

    politeness:
      max_concurrency: 2              # 学校站点，保守
      delay_sec: 2
      conditional_requests: true      # 站点提供 ETag + Last-Modified
```

### tagger
关键项：

| 键 | 说明 |
|---|---|
| `tagset_dir` | 分组标签集根目录，每组一份 `{group}/tags.json`。任一组 checksum 不匹配则拒绝启动 |
| `tagset_groups[].build_mode` | `llm`（默认，无需本地模型）/ `embedding`（需本地模型，产出质心） |
| `tagger.tagset.auto_curate.*` | 自动净化：同名/同 slug 合并、小簇/过泛项丢弃、标签数上限 |
| `tagset_groups[].require_manual_review` | 默认 `false` 全自动冻结；`true` 则需 Web 端确认 |
| `max_tags_per_article` | 单篇最多打几个标签 |
| `min_confidence_to_store` | 低于此值不入库。与订阅的 `min_confidence` 是两级过滤 |
| `llm.providers[].type` | `openai_compatible` 覆盖多数服务；`gemini` / `anthropic` 需专用适配器 |
| `llm.providers[].api_key_env` | **变量名，不是值** |
| `llm.providers[].models` | 顺序即优先级，内层遍历 |
| `llm.max_retries_per_model` | 仅对瞬时错误生效 |
| `circuit_breaker.*` | 熔断参数，状态持久化到 DB |
| `local.enabled` | **默认 `false`**。需手动开启，且依赖标签集含质心 |
| `local.idle_unload_after_sec` | 空闲卸载模型，2C2G 上不要设为 0 |
| `local.intra_op_num_threads` | 保持 1，避免和 web 抢 CPU |

遍历顺序：`providers[0].models[0]` → `providers[0].models[1]` → …
→ `providers[1].models[0]` → … → 全失败 → `local`（若开启）
→ 保持 `EXTRACTED` 等下轮。

### onboarding
站点接入向导的探测预算，详见 [11-site-onboarding.md](11-site-onboarding.md)。

| 键 | 默认 | 说明 |
|---|---|---|
| `probe.max_pages` | 40 | 单次探测最多请求页数 |
| `probe.max_duration_sec` | 120 | 超时中止，返回已得部分结果 |
| `probe.max_bytes_per_page` | 3145728 | |
| `probe.sample_articles` | 6 | 提取/附件探测的样本数 |
| `probe.delay_sec` | 1 | 探测自身也遵守限速 |
| `dryrun.sample_size` | 10 | 试运行预览抓取篇数，不入库 |
| `picker.load_external_assets` | `false` | 不加载外链 CSS/图片（防 IP 泄露） |

探测入口只限 admin，且必须过 SSRF 校验（内网地址拒绝 + IP pin +
重定向逐跳校验）。这不是可配项，不提供关闭开关。

### notify
| 键 | 默认 | 说明 |
|---|---|---|
| `include_full_content` | `true` | 需求要求推全文 |
| `max_body_chars` | 8000 | 会被渠道能力表下调（如 Telegram 4096） |
| `attachment_mode` | `both` | 小附件直发，大的转签名链接 |
| `signed_link_ttl_hours` | 72 | 签名链接有效期 |
| `dedupe_window_days` | 7 | 转载去重窗口 |
| `target_auto_disable_after_failures` | 10 | 防对失效渠道无限重试 |

所有用户（含 admin）只能使用插件自身固定目标主机的云服务 scheme；自定义主机型
webhook/SMTP/ntfy、本地文件、桌面通知及未知 scheme 均拒绝，避免投递时 DNS
rebinding 或重定向绕过创建阶段校验。

### alerts

| 键 | 默认 | 行为 |
|---|---|---|
| `enabled` | `true` | 复用启用中的 admin 推送目标发送关键告警 |
| `on_all_providers_down` | `true` | 全 provider 处于冷却时告警 |
| `on_disk_usage_pct` | 85 | 数据盘使用率达到阈值时告警 |
| `on_site_consecutive_failures` | 5 | 站点连续失败达到阈值后告警；每类一小时去重 |

### retention
小 VPS 上这组默认值应保持开启，附件是磁盘占用主因。housekeeping 会执行
`incremental_vacuum`；升级旧库时 `db migrate` 会一次性转换 auto-vacuum 模式，
因此生产升级应通过会先备份的 `scripts/update.sh`。

| 键 | 默认 | 行为 |
|---|---|---|
| `article_days` | 180 | 清 `content_html`，保留元数据与标签 |
| `attachment_days` | 30 | 删物理文件，置 `local_path=NULL` |
| `audit_days` | 90 | 删审计记录 |

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `NESTRA_SECRET_KEY` | **是** | `openssl rand -base64 32`。丢失后已存的推送目标无法解密 |
| `<PROVIDER>_API_KEY` | 否 | 名称需与 `api_key_env` 一致；全部缺失时抓取继续，文章停在 `EXTRACTED` |
| `NESTRA_ADMIN_PASSWORD` | 否 | 留空则走 setup token 流程 |
| `NESTRA__APP__LOG_LEVEL` | 否 | 覆盖 `app.log_level` |
| `NESTRA_CONFIG` | 否 | 配置路径；容器默认 `/app/config/config.yaml` |
| `NESTRA_UID` / `NESTRA_GID` | 否 | Docker build 时对齐宿主 `data/` 所有者，默认 1000 |
| `NESTRA__WEB__HOST` / `NESTRA__WEB__PORT` | 否 | 非容器时可覆盖 YAML；Compose 固定为 `0.0.0.0:8080` |
| `TZ` | 否 | 容器时区 |

`NESTRA_SECRET_KEY` 缺失时**拒绝启动**，不自动生成——
自动生成会导致每次重启都换密钥，已加密的推送目标全部失效。

## 修改配置后如何生效

| 改动 | 生效方式 |
|---|---|
| `sites[]` 新 slug | 重启后仅导入 DB 中尚不存在的 slug |
| 已存在站点 | 运行期以 DB 为准；通过 Web/管理命令修改，YAML 不隐式覆盖 |
| `tagger.llm.providers` | 重启 |
| `notify.*` / `schedule.*` / `retention.*` | 重启 |
| 用户/订阅/推送目标 | Web 端即时生效（数据在 DB） |
| `{group}/tags.json` | **不应手改**。变更需重新生成为新 `tagset_version` + 显式冻结切换，影响面限于该组 |

统一"改配置就重启容器"，不做热重载。
单容器、秒级重启的系统上，热重载带来的状态一致性问题不值得。
