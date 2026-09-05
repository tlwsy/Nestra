<div align="center">

# 🦅 Nestra

**轻量级多站点信息聚合 · AI 智能分类 · 精准全渠道全文与附件推送**

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/framework-FastAPI-009688.svg)](https://fastapi.tiangolo.com/)
[![Docker Image](https://img.shields.io/badge/docker%20image-tlwsy%2Fnestra-2496ED.svg?logo=docker)](https://hub.docker.com/r/tlwsy/nestra)
[![SQLite WAL](https://img.shields.io/badge/database-SQLite_WAL-003B57.svg)](https://www.sqlite.org/)
[![Tests](https://img.shields.io/badge/tests-117%20passed-brightgreen.svg)]()
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

*专为低配 VPS（2C2G）量身设计，无 Redis/Celery 负担，单容器秒级极速部署*

[快速开始](#-普通用户--自建站长极速上手) • [核心特性](#-核心特性) • [开发者指南](#-开发者指南) • [架构设计](#-核心架构与设计哲学) • [常见问题](#-常见问题-faq)

---

</div>

## 📖 什么是 Nestra？

在日常学习与工作中，我们往往需要关注多个官方网站的信息——例如**高校教务处、学院官网、政府部门招考、科研基金申报、行业技术博客**等。但许多传统网站：
- ❌ **没有 RSS 订阅源**，或者排版混乱，必须每天手动打开网页刷新；
- ❌ 公告内大量重要细节沉睡在 **PDF / Word / Excel 附件** 中，传统抓取工具只截取纯文本；
- ❌ 站点通知繁杂琐碎，**信息严重过载**，而你真正关心的可能只是某几个方向（如“研究生选课”、“奖学金”、“学科竞赛”）。

**Nestra 就是为了解决这些痛点而诞生的自托管信息聚合中枢。**

它能自动监测目标站点更新，无损提取**清洗后正文与附件**，利用大模型（LLM）按预先沉淀的“**冻结标签集**”进行精准归类；当命中你的个性化订阅规则时，第一时间将**格式优美的全文、摘要与原样附件**推送到你常用的即时通讯工具或邮箱中。

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  多站点抓取  │ ──► │  正文与附件  │ ──► │  AI 智能打标 │ ──► │  用户精准订阅│ ──► 全渠道全文推送
│ (HTML/RSS/JS)│     │  智能清洗提取│     │(冻结集/零幻觉)│     │ (交集/并集/静默)│   (Telegram/Bark/
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘    钉钉/飞书/邮件/...)
```

---

## 🌟 核心特性

### 👨‍💻 面向普通用户 / 自建站长
- 🚀 **极速开箱即用**：提供 Docker 一键安装脚本，全自动配置依赖与安全凭据，无需复杂运维。
- 🖥️ **直观的 Web 控制台**：
  - **可视化站点接入向导**：只需输入目标网站网址，系统全自动探测页面结构、抓取模式，甚至提供**交互式 CSS 选择器拾取器**。
  - **标签与订阅管理**：勾选感兴趣的标签，支持「命中任意标签」或「全部包含」，支持按站点过滤与置信度过滤。
  - **夜间免打扰（静默时段）**：自由设置勿扰时间（如 `23:00-07:00`），期间产生的推送自动顺延至次日清晨发送。
- 📲 **全渠道富文本推送**：底层基于强大的 [Apprise](https://github.com/caronc/apprise) 引擎，原生支持 80+ 种主流推送渠道：
  - **移动端推送**：Telegram Bot、Bark (iOS)、Pushover、Pushbullet 等；
  - **办公与群机器人**：钉钉、飞书 / Lark、企业微信、Discord、Slack；
  - **传统与通用协议**：邮件 (SMTP)、自定义 Webhook 等。
- 📎 **完整附件直达**：公告随附的 PDF、Word 文档、表格、压缩包自动下载保存，支持在通知中直接携带附件，或生成时效性安全下载链接。
- 🛡️ **银行级安全防护**：原生面向公网暴露设计，严格多用户隔离、强制初始密码初始化、Argon2id 密码哈希、TOTP 双因素认证（2FA）、敏感渠道 URL 加密存储、防暴力破解与防 SSRF 保护。

### ⚙️ 面向开发者与折腾党
- 🪶 **极端轻量（2C2G 友好）**：摒弃 Redis、Celery、PostgreSQL 等厚重中间件，采用 **FastAPI + 内置单进程 APScheduler + SQLite (WAL 模式)**，常态待机内存仅约 **100MB**。
- 🧩 **声明式站点适配器**：加站点 = 加几行 YAML 配置，支持 HTML 列表、RSS、Sitemap 等多种发现模式，内置 URL 规范化与正文清洗规则，无需编写爬虫代码。
- 🧠 **零幻觉（Zero-Hallucination）打标体系**：
  - **生成与打标解耦**：运行期标签集严格**只读冻结**，AI 仅执行多标签分类，绝不会无限产生随机新标签。
  - **多层降级容灾**：OpenAI 兼容接口 / DeepSeek / Gemini / Claude 串联降级；当全部外网 API 异常时，可平滑回退至本地轻量 ONNX 向量语义模型 (`bge-small-zh-v1.5`) 离线兜底。
  - **网络欠费/故障安全**：打标全链不可用时，文章安全停留在 `EXTRACTED` 状态，API 恢复后自动续跑，不污染数据、不丢失任务。
- 🔄 **强一致状态机流水线**：文章按照 `DISCOVERED → FETCHED → EXTRACTED → TAGGED → NOTIFIED` 严格推进，任务崩溃重启自动重试与断点续跑，外部投递保障 At-least-once。

---

## 🚀 普通用户 / 自建站长极速上手

### 方式一：Docker Compose 极速部署（最推荐，免源码编译）

无需下载任何仓库源码，直接拉取 Docker Hub 预构建镜像（传输仅 72MB，5 秒完成拉取）：

1. **在服务器上创建目录并新建 `docker-compose.yml`**：
```yaml
services:
  nestra:
    image: tlwsy/nestra:latest  # 或使用全功能版 tlwsy/nestra:full
    container_name: nestra
    restart: unless-stopped
    ports:
      - "127.0.0.1:8080:8080"
    environment:
      # 生成方式: openssl rand -base64 32
      - NESTRA_SECRET_KEY=请替换为你生成的32位以上随机加密密钥
      # 可选：如果已配置域名并启用 HTTPS 反向代理，请取消注释并填写：
      # - NESTRA__WEB__BASE_URL=https://nestra.example.com
      # - NESTRA__WEB__COOKIE_SECURE=true
    volumes:
      - ./data:/app/data
      - ./config.yaml:/app/config/config.yaml:ro
```

2. **启动服务**：
```bash
# 准备数据目录并后台拉起容器
mkdir -p data
docker compose up -d
```

3. **获取首次管理员注册链接**：
为了确保公网暴露绝对安全，系统**不存在任何默认密码**。在容器启动后查看日志：
```bash
docker compose logs nestra
```
日志中会输出一行一次性初始化链接，格式如：
`Initial administrator setup URL: http://127.0.0.1:8080/setup?token=xxxxxxxxxxxxxxxx`

在浏览器打开该链接，即可完成管理员账号注册与初始登录。

---

### 方式二：一键脚本自动化部署（适合已 Clone 仓库用户）

如果你已经将代码仓库 Clone 到了本地或 VPS：

```bash
# 1. 运行一键安装脚本（默认绑定本机 127.0.0.1:8080，全自动生成随机密钥与目录）
./scripts/install.sh

# 若准备绑定独立域名并暴露公网（推荐配合反向代理配置 HTTPS）：
NESTRA_BASE_URL=https://nestra.example.com ./scripts/install.sh
```

脚本将自动检测 Docker 环境、创建持久化目录、生成强随机密钥并拉起容器，直接在控制台输出管理员注册链接。

---

### 📦 镜像版本说明

| 镜像 Tag | 适用场景 | 传输体积 | 解压占用 | 包含特性 |
|---|---|:---:|:---:|---|
| **`tlwsy/nestra:latest`** | **绝大多数用户推荐** | **~72 MB** | **~300 MB** | 核心抓取、正文/附件提取、LLM 降级链、Apprise 推送、完整 Web 界面 |
| **`tlwsy/nestra:full`** | 需无头渲染 / 本地离线 AI | ~400 MB | ~1.2 GB | 包含 `latest` 全部功能 + Playwright Chromium 动态网页渲染 + ONNX 离线向量模型 |
| **`ghcr.io/tlwsy/nestra:latest`** | 备用源 (GitHub Packages) | ~72 MB | ~300 MB | 与 Docker Hub 的 `latest` 保持完全同步 |


---

### 反向代理配置（Nginx / Caddy 示例）

为保障 Cookie 安全传输及附件下载防篡改，强烈建议使用带有 SSL 证书的反向代理。

#### Caddy（最简单，自动配置 SSL）：
```caddyfile
nestra.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8080
    header {
        -Server
        Strict-Transport-Security "max-age=31536000"
    }
}
```

#### Nginx：
```nginx
server {
    listen 443 ssl http2;
    server_name nestra.example.com;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

### 3 分钟使用指引

```
 步骤 1：添加推送渠道 ──► 步骤 2：配置大模型/站点 ──► 步骤 3：订阅标签与免打扰
 (设置 Telegram/Bark 等)   (输入 URL 自动探测向导)     (勾选标签，坐等推送)
```

1. **配置大模型 Provider（管理员）**：
   - 登录后台进入 **系统管理 → 模型管理 (`/admin/providers`)**；
   - 添加你的大模型 API Key（支持 DeepSeek、OpenAI、Gemini、Claude、通义千问等）；凭证在数据库中通过主密钥加密保存。
2. **添加目标站点与冻结标签（管理员）**：
   - 进入 **系统管理 → 站点接入 (`/admin/sites/new`)**，输入任何你想监控的网页列表链接，向导会自动分析网页特征、给出推荐抓取配置；
   - 首次接入站点后，在 **标签集管理 (`/admin/tagsets`)** 基于历史抓取的文章自动提炼主题标签并执行“冻结”。
3. **添加推送目标（普通用户/管理员）**：
   - 进入 **推送目标 (`/targets`)**，添加一个新渠道。
   - 例如 Telegram 填写：`tgram://<bot_token>/<chat_id>`；
   - 例如 Bark (iOS) 填写：`bark://<device_key>`；
   - 点击「测试推送」确保通知能够顺利到达你的手机。
4. **添加订阅 (`/subscriptions`)**：
   - 选择感兴趣的站点与标签组合（如选中：`选课通知`、`考试安排`）；
   - 关联上一步创建的推送目标；
   - 设定静默时段（如开启夜间勿扰）；
   - 保存后，只要站点产生对应的新通知，你就能在手机上第一时间收到完整图文与附件！

---

### 实用运维命令

| 操作 | 命令 | 说明 |
|---|---|---|
| **查看状态** | `docker compose ps` | 查看容器健康状态与运行端口 |
| **实时日志** | `docker compose logs -f nestra` | 查看调度抓取、打标、投递日志 |
| **预构建镜像升级** | `docker compose pull && docker compose up -d` | 使用 Docker Hub 预构建镜像时一键无缝升级 |
| **源码平滑升级** | `./scripts/update.sh` | 使用 Git 仓库部署时：自动备份、拉取代码并重新编译 |
| **安全热备份** | `./scripts/backup.sh` | 使用 SQLite 在线备份 API 热备份数据库及附件 |
| **数据恢复** | `./scripts/restore.sh <备份归档.tar.gz>` | 自动停服校验并还原数据，失败自动回滚 |

---

## 💻 开发者指南

### 本地开发环境配置

Nestra 采用现代 Python 工具链 [uv](https://github.com/astral-sh/uv) 进行依赖管理，要求 Python 3.12+。

```bash
# 1. 克隆代码仓库
git clone https://github.com/your-repo/nestra.git
cd Nestra

# 2. 复制配置文件模板
cp config/config.example.yaml config/config.yaml
install -m 0600 config/env.example .env

# 3. 生成主加密密钥并填入 .env
export NESTRA_SECRET_KEY="$(openssl rand -base64 32)"
sed -i "s|^NESTRA_SECRET_KEY=.*|NESTRA_SECRET_KEY=${NESTRA_SECRET_KEY}|" .env

# 4. 同步安装所有开发与运行依赖
uv sync --extra crawl --extra web --extra notify --extra dev

# 5. 校验配置并执行数据库迁移
uv run nestra config check
uv run nestra db migrate

# 6. 启动本地开发服务 (支持自动重载)
uv run nestra serve
```

访问 `http://127.0.0.1:8080` 即可开始调试。

---

### CLI 常用命令行工具

Nestra 提供了完善的内置 CLI 子命令（`nestra <command>`）：

```bash
# 配置与数据库
uv run nestra config check               # 校验 config.yaml 语法与 LLM 配置有效性
uv run nestra db migrate                 # 执行 SQLite 数据表增量迁移
uv run nestra db stats                   # 查看数据表行数与数据库健康状态

# 抓取与提取测试
uv run nestra crawl --site ujs-jwc --dry-run  # 试运行抓取特定站点，仅输出预览不写库
uv run nestra crawl --site ujs-jwc            # 正式执行一次站点抓取入库
uv run nestra site sync --site ujs-jwc        # 显式将 config.yaml 中的站点规则同步到 DB

# 站点探测与向导分析
uv run nestra probe https://example.com/news  # 探测站点渲染类型/正文规则并生成候选配置

# 打标与全流水线运行
uv run nestra tag --limit 20                  # 对已提取 (EXTRACTED) 的前 20 篇文章执行打标
uv run nestra run-once                        # 依次单次执行完整的「抓取 → 打标 → 投递」流程
```

---

### 自动化测试与代码质量

项目中包含完整的单元测试与端到端集成测试：

```bash
# 运行全部测试集（已包含 110+ 测试用例）
uv run pytest -q

# 执行静态语法与代码规范检查
uv run ruff check src tests scripts

# 自动修复代码格式
uv run ruff check --fix src tests scripts
```

---

## 🏗️ 核心架构与设计哲学

### 1. 流水线解耦设计（Pipeline State Machine）

系统内部的各个执行器（抓取器、正文解析器、打标器、通知器）**互不直接调用**，而是完全由数据库中的文章状态流转进行解耦驱动：

```
       [ 站点列表页 / RSS ]
                │
                ▼
        ┌──────────────┐
        │  DISCOVERED  │  发现新文章 URL（去重后入库）
        └──────┬───────┘
               │ crawler 异步抓取 HTML
        ┌──────▼───────┐
  ┌─────┤   FETCHED    │  HTML 获取完毕
  │     └──────┬───────┘
  │            │ extractor 提取正文、清洗格式、排队附件
  │     ┌──────▼───────┐
  │     │  EXTRACTED   │  正文清洗完毕，附件清单就绪
  │     └──────┬───────┘
  │            │ tagger LLM 降级链 / 本地 ONNX 兜底
  │     ┌──────▼───────┐
  │     │    TAGGED    │  标签打标完成并记录置信度
  │     └──────┬───────┘
  │            │ notifier 匹配订阅规则生成 deliveries
  │     ┌──────▼───────┐
  │     │   NOTIFIED   │  终态（包含“无人订阅”或“已完成投递”）
  │     └──────────────┘
  │
  │     ┌──────────────┐
  └────►│    FAILED    │  记录错误原因；瞬时网络错误支持退避重试
        └──────────────┘
```

> **状态安全原则**：如果外部大模型 API 遭遇欠费或临时断网，文章将安全保存在 `EXTRACTED` 状态，调度器不会将其标记为失败，待服务恢复后自动无缝续打。

---

### 2. 为什么坚持“冻结标签集”？

传统的 AI 自动打标通常直接要求大模型“生成 3-5 个关键词”，这在持续运行的订阅系统中会导致灾难性的后果：
- 同一概念用词飘移（例如今天打标成 `选课`，明天打标成 `选修课`，后天变成 `选课指南`）；
- 用户订阅条件极其难以配置和维持稳定。

**Nestra 的解法**：
1. **引导阶段（Bootstrap）**：先回填某站点数十至上百篇历史文章，通过 LLM 归纳提炼（或使用 `sentence-transformers` 聚类）出 30~80 个高质量主题标签；
2. **冻结阶段（Freeze）**：人工确认后将标签集写入系统，并固定 Hash 校验；
3. **运行阶段（Runtime）**：大模型仅需在已知且固定的标签池中做**多标签多项选择题**，并给出各自分数。这彻底消除了 AI 幻觉，保证了推送订阅的绝对精准与长期稳定。

---

### 3. 架构决策与选型取舍

| 核心决策 | 选型方案 | 为什么这么选？ |
|---|---|---|
| **开发语言** | Python 3.12+ | Python 独占优秀的 Apprise 全渠道生态、trafilatura 正文提取及 onnxruntime 深度支持 |
| **Web 与 API 栈** | FastAPI + Jinja2 + htmx | 零前端打包构建步骤（无 Node.js/npm 链），内存占用低，页面响应敏捷 |
| **存储底座** | SQLite 3 (WAL 模式) | 单文件数据库，极致省心，零网络开销与进程占用，支持高并发安全只读 |
| **进程模型** | 单进程 Uvicorn + APScheduler | 彻底告别 Redis/Celery 依赖，降低 2C2G 机器资源占用；进程重启状态零丢失 |
| **站点扩展方式** | 声明式 YAML 配置 | 接入新站点只需编写/生成规则，无需修改 Python 源码或发版 |
| **敏感信息保护** | Fernet 对称加密 (AES-128-CBC + HMAC) | 用户的 Webhook 凭证、第三方 API Key 在数据库内密文落盘，防内鬼与拖库 |

---

## 📁 目录结构

```
Nestra/
├── config/              # 配置文件模板（config.example.yaml / env.example）
├── data/                # 运行时持久化数据（已加入 .gitignore，挂载至容器）
│   ├── db/              #   SQLite 数据库文件 (nestra.db)
│   ├── attachments/      #   下载的附件原件，按 sha256 分片存储
│   └── models/          #   标签集元数据与可选本地 ONNX 模型
├── deploy/              # Dockerfile / docker-compose / Nginx / Caddy 部署文件
├── docs/                # 详细系统设计与架构规范文档（共 12 篇）
├── scripts/             # 一键部署、备份、恢复、更新、密钥轮换工具脚本
├── src/nestra/         # 核心源码包
│   ├── core/            #   配置加载、日志记录、加密系统、领域模型
│   ├── crawler/         #   站点抓取器（HTTPX / 可选 Playwright）
│   ├── extractor/       #   正文提取、HTML 清洗与附件发现
│   ├── tagger/          #   智能打标链路（LLM Providers 降级链与 ONNX 兜底）
│   ├── onboarding/      #   站点自动探测引擎与可视化选择器拾取
│   ├── notifier/        #   用户订阅匹配算法与 Apprise 全渠道投递
│   ├── scheduler/       #   APScheduler 定时调度与生命周期编排
│   ├── storage/         #   SQLite 仓储层、数据迁移与原子状态更新
│   └── web/             #   FastAPI Web 路由、鉴权、i18n 多语言与模板
└── tests/               # 完整单元测试与集成测试套件
```

---

## ⚙️ 核心配置速查

系统配置分为 **环境密钥 (`.env`)** 与 **应用业务配置 (`config/config.yaml`)**：

### 1. `.env` 关键密钥清单
- `NESTRA_SECRET_KEY`: **必填**。用于加解密推送凭证的主密钥，通过 `openssl rand -base64 32` 生成。
- `NESTRA_ADMIN_PASSWORD`: 可选。留空则首次启动打印动态 Setup Token 到日志中。
- `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY`: 可选。配置外部大模型密钥。

### 2. `config.yaml` 核心业务项
```yaml
app:
  timezone: Asia/Shanghai       # 时区（影响静默时段与定时抓取计算）

runtime:
  crawl_concurrency: 4          # 抓取并发限制（低配机器推荐 2~4）
  sqlite_cache_mb: 32           # SQLite 缓存大小

schedule:
  crawl_default_interval_sec: 1800  # 站点抓取默认间隔（30 分钟）
  tag_interval_sec: 300             # 打标调度间隔（5 分钟）
  dispatch_interval_sec: 120        # 订阅匹配推送间隔（2 分钟）

attachments:
  enabled: true                 # 是否下载附件
  max_size_mb: 20               # 单个附件最大体积限制
  total_quota_gb: 5             # 附件总磁盘配额

notify:
  body_format: markdown         # 推送格式（markdown / text）
  include_full_content: true    # 是否推送文章完整清洗正文
  max_body_chars: 8000          # 超过渠道最大长度自动截断并附带原文链接
```

---

## 📚 详细设计文档索引

想要深入研究系统底层实现的开发者，请参阅 `docs/` 目录下的专题文档：

| 编号 | 文档标题 | 涵盖核心内容 |
|:---:|---|---|
| 01 | [总体架构](docs/01-architecture.md) | 架构分层、状态机流转、低配内存预算分配 |
| 02 | [数据模型](docs/02-data-model.md) | SQLite 全部数据表结构、索引策略与外键约束 |
| 03 | [爬虫与抓取](docs/03-crawler.md) | 站点发现模式、URL 规范化、去重算法与礼貌抓取 |
| 04 | [标签与分类器](docs/04-tagger.md) | 冻结标签集原理、LLM 降级链与本地 ONNX 兜底机制 |
| 05 | [通知与推送](docs/05-notifier.md) | 订阅匹配逻辑、汉明距离排重、静默时段与投递重试 |
| 06 | [Web 与安全](docs/06-web-auth.md) | 用户认证体系、Session 会话设计、CSRF 与防 SSRF 规范 |
| 07 | [部署与运维](docs/07-deployment.md) | Docker 镜像分层、备份恢复方案与平滑更新机制 |
| 08 | [配置全参考](docs/08-config-reference.md) | 配置文件中每个参数的详细含义与默认值说明 |
| 09 | [路线图与验收](docs/09-roadmap.md) | 项目阶段里程碑规划与功能验收标准 |
| 10 | [实战站点探测](docs/10-site-probe-ujs-jwc.md) | 目标高校教务网实测抓取与适配分析报告 |
| 11 | [站点接入向导](docs/11-site-onboarding.md) | 自动化探测算法、安全沙箱预览与选择器拾取设计 |
| 12 | [Docker 启动指引](docs/12-docker-startup.md) | 首次启动验证步骤及 WSL 2 常见故障排查 |

---

## ❓ 常见问题 (FAQ)

### Q1: 如果我没有或不想使用商业大模型 API Key，系统能跑起来吗？
**可以。**
1. 系统支持本地离线轻量向量模型（安装 `--extra local` 后启用 ONNX 嵌入模型），完全不消耗任何 API 费用；
2. 如果未配置打标后端，抓取仍将持续进行，文章将安全停留在 `EXTRACTED` 状态，你在 Web 端阅读或日后配置 Key 都会自动续接。

### Q2: 为什么添加了站点后，没有立刻收到推送？
请按以下顺序检查：
1. **文章状态**：刚发现的文章需要依次经历「抓取 → 正文提取 → 标签打标 → 订阅匹配」；
2. **标签集是否已冻结**：新站点所属的「标签集分组」必须已冻结标签，否则文章会停在 `EXTRACTED` 待打标；
3. **订阅规则**：检查你的订阅条件（站点过滤、标签匹配模式是 `any` 还是 `all`、置信度阈值是否过高）；
4. **静默时段**：如果当前时间落在设定的勿扰时段内，通知将被顺延至次日清晨发送。

### Q3: 为什么不开放任意用户注册？
Nestra 的产品定位是**面向个人或小圈子的自建自用中枢**。由于系统支持抓取外部网络、调用大模型并推送富文本，开放注册会带来被滥用作为爬虫肉鸡或邮件轰炸源的风险。因此系统采用受控的多用户机制：由管理员在后台安全邀请或直接创建新账户。

---

## 📄 开源许可

本项目采用 [MIT License](LICENSE) 授权开源。欢迎提交 Issue 或 Pull Request！
