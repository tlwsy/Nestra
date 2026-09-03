# 09 · 实现路线

分 7 个阶段；当前代码交付均已完成。真实 LLM、消息渠道、目标 VPS 以及由历史语料
生成的 `tags.json` 属于部署方凭据/数据，仓库以 mock 集成测试和手工验收命令覆盖，
不提交密钥、站点快照或虚构标签。

**M-1（站点探测）已完成**，结论见 [10-site-probe-ujs-jwc.md](10-site-probe-ujs-jwc.md)。
首站配置已确定：`html_list` 模式、无需 JS、1162 篇历史文章可用于标签集生成。

## M0 · 地基（已完成）

| 项 | 内容 |
|---|---|
| 交付 | `pyproject.toml`、`core/config.py`（pydantic-settings + YAML）、结构化日志、异常层次、`storage/` 迁移框架 + `001_init.sql`、`web/app.py` 骨架仅含 `/healthz` |
| 验收 | `docker compose up` 起得来，`/healthz` 返回 200，DB 文件与全部表创建成功，配置校验能对错误配置给出清晰报错 |

先把配置校验做扎实。后面每个模块都依赖它，返工成本最高。

## M1 · 抓取到提取（已完成）

| 项 | 内容 |
|---|---|
| 交付 | `crawler/fetcher.py`（限速/重试/条件请求/robots）、`discovery/html_list.py`、`crawler/url_canonical.py`、`extractor/article.py`、`extractor/sanitize.py`、`extractor/dedupe.py`、CLI `nestra crawl --site <slug> --dry-run` |
| 验收 | 对 `ujs-jwc` 跑通 `DISCOVERED → EXTRACTED`；重复运行不产生重复文章；`content.jsp` 与 `/info/` 两种 URL 归一为同一篇；`--dry-run` 只打印不写库 |

**先做 `html_list` 而非 RSS**——探测确认目标站点无 RSS、无 sitemap。
RSS 模式推到 M6（接第二个站点时大概率需要）。

本阶段必须落地的三个站点特性（否则后面返工）：

1. `url_canonical`：`content.jsp?wbtreeid=&wbnewsid=` → `/info/{treeid}/{newsid}.htm`
2. `pagination.order: desc_index`：页号越小越旧，增量只取入口页
3. 标题从 `a@title` 属性取，列表页文本被 CSS 截断

## M2 · 打标（已完成）

| 项 | 内容 |
|---|---|
| 交付 | `scripts/backfill_history.py`、`scripts/bootstrap_tagset.py`（形态 A/B 双模式）、`scripts/freeze_tagset.py`、`tagger/tagset.py`、`tagger/prompt.py`、`tagger/chain.py`、`providers/openai_compatible.py`、CLI `nestra tag --limit N` |
| 验收 | mock/小语料自动化覆盖生成、冻结、checksum、白名单与 `article_tags.backend`；部署方按命令回填真实 1162 篇并审阅 30–80 个标签后冻结 |

先只做 `openai_compatible` 一个适配器 + 单 provider。
降级链的编排逻辑要在这一步写好（哪怕只有一个 provider），
错误分类体系（Transient / Fatal / Quota / OutputInvalid）也在此定型。

标签集生成默认走**形态 B（VPS 上直接调 LLM）**：1162 篇分约 30 批归纳，
成本几毛钱，2C2G 完全跑得动，不需要本地模型。形态 A（本地 embedding 聚类）
作为可选路径，与 M3 的本地兜底共用模型文件。

## M3 · 降级链完整化（已完成）

| 项 | 内容 |
|---|---|
| 交付 | `providers/gemini.py`、`providers/anthropic.py`、熔断 + `provider_health` 持久化、`tagger/local_onnx.py`（懒加载/空闲卸载，**默认关闭**）、`scripts/backfill_centroids.py` |
| 验收 | 故意把第一个 provider 的 key 设错 → 自动切下一个且不重试；全部 provider 断网 → 落到本地兜底（若开启）或停在 `EXTRACTED`；本地模型删除 → 文章停在 `EXTRACTED` 不变 `FAILED`；空闲 15min 后内存下降可观测 |

降级路径必须**主动测**，不能假定它能工作。故障注入是这一阶段的主要工作量。

本地兜底默认 `enabled: false`，与 Playwright 同一原则：重资源组件显式开启。
未开启时降级链末级就是「保持 `EXTRACTED` 等下轮」，这本身是可自愈的正常状态。

## M4 · 推送（已完成）

| 项 | 内容 |
|---|---|
| 交付 | `notifier/matcher.py`、`dispatcher.py`、`message.py`、`capabilities.py`、`apprise_client.py`、`scheduler/` 全部任务接入 APScheduler |
| 验收 | 离线端到端覆盖抓取 → 打标 → 命中 → 全文 + 附件参数；真实 Telegram/Discord 由部署方手测；`download.jsp` 类附件、中文 `Content-Disposition`、唯一约束、静默时段与截断均有自动化覆盖 |

到这里核心需求已闭环，可以先跑起来自用。

## M5 · Web 管理端（已完成）

| 项 | 内容 |
|---|---|
| 交付 | 鉴权全套（会话/Argon2/setup token/限流/CSRF/安全响应头）、用户与订阅 CRUD、推送目标管理 + 测试、文章浏览 + 附件下载鉴权、admin 页面（站点/provider健康/系统状态/审计/标签集只读） |
| 验收 | 完成 [06-web-auth.md](06-web-auth.md) §8 的全部检查项；用户 A 无法访问用户 B 的订阅、文章、附件（需写成自动化测试） |

跨用户越权必须有自动化测试覆盖。虽然本项目面向私人自部署、
多用户功能刻意做轻（无注册、无邮件流），但**数据隔离的测试不能省**——
隔离漏洞事后补的代价远高于现在写测试。

## M6 · 部署与运维（已完成）

| 项 | 内容 |
|---|---|
| 交付 | 多阶段 `Dockerfile`（Playwright 可选层）、`docker-compose.yml`、`install.sh` / `update.sh` / `backup.sh` / `restore.sh` / `rotate_key.py`、nginx 与 Caddy 示例、其余发现模式（rss / sitemap / json_api）、`probe_site.py` |
| 验收 | 全新 VPS 上 `./install.sh` 一键起；重复执行不破坏数据；备份恢复演练成功；2C2G 实测内存符合 [07-deployment.md](07-deployment.md) §6 预算；用 `probe_site.py` 接入第二个站点全程只改 YAML |

"只改 YAML 就能接入第二个站点"是通用性目标的验收标准。做不到说明发现层抽象有问题。

## M7 · 站点接入向导（已完成）

把 M6 的「只改 YAML」推进到「不写 YAML」。放在最后是因为它依赖前面所有组件：
探测复用抓取层，预览复用提取层，拾取器复用清洗层。提前做会重复建设。

| 项 | 内容 |
|---|---|
| 交付 | `onboarding/` 全模块（SSRF 校验、有界探测、列表页发现、选择器归纳、分页证据、双形态检测、试运行预览、配置生成）、五阶段服务端 UI、选择器编辑器 + 沙箱预览、CLI 共用 probe |
| 验收 | SSRF/DNS pin/逐跳重定向自动化测试；探测候选、选择器编辑、提取预览、配置 hash 确认与直接 JSON/YAML 退路均可运行 |

两个硬要求，不达成不算完成：

- **SSRF 防护必须有自动化测试**。探测接口让服务端请求用户指定的任意 URL，
  内网地址、IP pin、重定向逐跳校验三项各自有用例
- **向导失败时必须有退路**。选择器编辑器 + `nestra site sync` YAML 两条路径都要可用，
  否则遇到 SPA 站点就彻底卡住

---

## 测试策略

| 层次 | 范围 |
|---|---|
| 单元 | URL 规范化（含 `content.jsp` → `/info/` 重写）、simhash、订阅匹配布尔逻辑、静默时段跨零点、消息截断、prompt 输出解析与白名单过滤、错误分类、`Content-Disposition` 中文文件名解析 |
| 集成 | 用 `tests/fixtures/` 里的离线 HTML 样本跑完整提取链（已可从目标站点抓取真实样本）；用 mock HTTP 跑降级链全部分支；SQLite 内存库跑仓储层 |
| 安全 | 越权访问（跨用户）、限流、CSRF、恶意 HTML 样本的清洗、签名链接过期与篡改 |

**不对真实站点和真实 LLM API 跑自动化测试**——不稳定、有成本、有礼貌问题。
全部用 fixture + mock。真实调用只在手工验收时做。

框架用 `pytest` + `pytest-asyncio` + `respx`（httpx mock）。

## 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| ~~目标站点需 JS 渲染~~ | — | **已排除**：探测确认静态 HTML，`render_js=false` |
| 站点无 RSS，依赖 CSS 选择器 | 改版即提取失败 | 选择器全部走 YAML 可配；连续失败告警 + Web 端提示 |
| 标签集质量差 | 推送不准，用户失去信任 | 自动净化（同名合并、小簇/过泛项丢弃）+ 自检报告；`require_manual_review` 可选开启；`backend` 字段支持定向重打 |
| 学校站点限流/封 IP | 抓取中断 | `max_concurrency=2` + `delay_sec=2` + 条件请求（站点支持 ETag） |
| LLM provider 全挂 | 打标停滞 | 文章停在 EXTRACTED 可自愈；本地兜底可选开启；告警推给 admin |
| 附件占满磁盘 | 服务不可用 | `total_quota_gb` 硬限 + 保留策略默认开启 + 磁盘 85% 告警 |
| 公网暴露被攻击 | 数据泄漏 | [06-web-auth.md](06-web-auth.md) §8 检查清单为上线门槛；无开放注册减小攻击面 |
