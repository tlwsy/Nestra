# 07 · 部署

目标：2C2G VPS 上单容器运行，`./install.sh` 一键起。

## 1. 镜像设计

多阶段构建，基础镜像 `python:3.12-slim`。

```
Stage 1 (builder)
  安装 build-essential（编译 argon2-cffi 等）
  uv / pip 安装依赖到 venv
Stage 2 (runtime)
  仅复制 venv + 应用代码
  非 root 用户运行
  不含编译工具链
```

**Playwright 作为可选层**：默认 Compose 使用轻量 `runtime` target；需要 JS 渲染时：

```bash
NESTRA_IMAGE_TARGET=runtime-render docker compose -f deploy/docker-compose.yml build
NESTRA_IMAGE_TARGET=runtime-render docker compose -f deploy/docker-compose.yml up -d
```

同时只为确认需要浏览器的站点设置 `render_js: true`。Chromium + 依赖约增加
500MB+ 镜像体积，因此普通静态站点不承担该成本。另有 `runtime-local` 和
`runtime-full` target 分别启用本地 ONNX 或同时启用渲染与本地模型。

镜像体积预期：基础约 200–300MB；带 Playwright 约 900MB–1.1GB。

## 2. compose

单服务。不引入 Postgres / Redis——见 [01-architecture.md](01-architecture.md) §7。

```yaml
# deploy/docker-compose.yml 结构要点
services:
  nestra:
    build: { context: .., dockerfile: deploy/Dockerfile }
    restart: unless-stopped
    env_file: ../.env
    ports:
      - "127.0.0.1:8080:8080"      # 只绑回环，由宿主反代暴露
    volumes:
      - ../data:/app/data
      - ../config:/app/config:ro   # 配置只读挂载
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8080/healthz')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    deploy:
      resources:
        limits: { memory: 1400M }   # 留余量给系统与突发
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }
```

要点：

- **端口绑 `127.0.0.1`**，不是 `0.0.0.0`。公网暴露交给宿主上的
  Nginx/Caddy 做 TLS 终结，应用自身永不直面公网。
- 内存 limit 设 1400M 而非 2G：给系统、反代、突发留空间。
  超限时容器 OOM 重启，比拖垮整机好。
- 日志轮转必须配，否则小 VPS 磁盘会被日志吃满。
- Compose 丢弃全部 Linux capabilities、限制 256 个进程并启用 init 回收子进程。
- `config` 只读挂载：配置变更走宿主文件 + 重启，避免容器内被改动后丢失。
- Compose 固定容器内监听 `0.0.0.0:8080`，使端口映射与 healthcheck 始终一致；
  `web.host/port` 仍可用于非容器启动，但会在 Compose 中被环境变量覆盖。
- `data` 是 bind mount，镜像非 root 用户必须与宿主目录所有者 UID/GID 对齐。
  Compose 通过 `NESTRA_UID` / `NESTRA_GID` build args 传入（默认 1000）。
  **两者都不能为 0**；若以 root 检出仓库，先执行
  `chown -R 1000:1000 data`，不要把容器改成 root。M6 的 `install.sh` 会创建/修正
  非 root 数据目录。入口发现不可写时立即退出并给出提示。

## 3. 一键脚本

`scripts/install.sh` 应完成：

```
1. 前置检查：docker / docker compose 是否可用，磁盘剩余空间
2. 若 .env 不存在 → 从 config/env.example 复制
3. 生成 NESTRA_SECRET_KEY（openssl rand -base64 32）写入 .env（若尚未设置）
4. 若 config/config.yaml 不存在 → 从 config.example.yaml 复制
5. 创建 data/ 子目录并设为 0700（数据库、`.env` 与备份为 0600）
6. 提示可稍后填写 LLM API key；缺失时抓取继续，文章停在 `EXTRACTED`
7. docker compose build && up -d
8. 等待 healthcheck 通过
9. 从容器日志提取 setup token，打印访问地址与后续步骤
```

脚本要求：

- POSIX `sh` 使用 `set -eu`（不依赖 Bash 的 `pipefail`）
- **幂等**：重复执行不覆盖既有 `.env` / `config.yaml` / 数据
- 所有变量引用加引号（防路径含空格）
- 不静默生成管理员密码，走 setup token 流程（见 [06-web-auth.md](06-web-auth.md) §4.3）
- 明确打印安全提醒：需自行配置 TLS 反代

配套脚本：

| 脚本 | 用途 |
|---|---|
| `scripts/install.sh` | 首次安装 |
| `scripts/update.sh` | 拉新代码、重建、迁移、重启 |
| `scripts/backup.sh` | SQLite 在线 Backup API + 配置、附件、冻结标签集与本地模型打包 |
| `scripts/restore.sh` | 停止服务并校验归档后恢复；任一步失败自动回滚 |
| `scripts/rotate_key.py` | 主密钥轮换 |
| `scripts/probe_site.py` | 探测站点：RSS？需 JS？推荐 discovery_mode |
| `scripts/bootstrap_tagset.py` | 标签集生成（见 [04-tagger.md](04-tagger.md)） |
| `scripts/backfill_history.py` | 回填历史文章（首站实测 1162 篇） |
| `scripts/backfill_centroids.py` | 为已冻结标签集补算质心（启用本地兜底的前置） |

备份必须用 SQLite Backup API 或 `VACUUM INTO`——WAL 模式下直接 `cp` 数据库
文件会得到不一致的副本。现有脚本使用 Python 标准库的 SQLite Backup API，并默认包含
附件和 `data/models`；仅在明确接受附件不可恢复时才设置 `INCLUDE_ATTACHMENTS=0`。恢复会
先完整校验归档，再强制停止服务，健康检查通过前任何失败都会恢复原文件。

## 4. 环境变量

环境模板 `config/env.example`（复制为仓库根 `.env`）：

```
# 必填
NESTRA_SECRET_KEY=              # openssl rand -base64 32

# LLM providers（可稍后配置；变量名需与 config.yaml 中 api_key_env 对应）
DEEPSEEK_API_KEY=
GEMINI_API_KEY=
OPENROUTER_API_KEY=

# 可选
NESTRA_ADMIN_PASSWORD=          # 留空则走 setup token 流程
NESTRA__APP__LOG_LEVEL=INFO
NESTRA_UID=1000               # Docker bind mount 所有者；install.sh 自动探测
NESTRA_GID=1000
TZ=Asia/Shanghai
```

`config.yaml` 不含任何机密，可安全提交与分享。
机密只在 `.env`（`.gitignore` 必须包含它）。

## 5. 反向代理

`deploy/nginx.example.conf` 与 `deploy/Caddyfile.example` 各提供一份。
Caddy 自动签发证书，对个人 VPS 更省事，建议作为文档里的首选示例。

代理需正确传递：`X-Forwarded-For`、`X-Forwarded-Proto`、`Host`。
同时应用侧 `web.trusted_proxies` 要配上代理的 IP，
否则限流会因为所有请求看起来同源而失效（见 [06-web-auth.md](06-web-auth.md) §4.4）。

附件下载会走大响应体，代理侧需调整 `client_max_body_size` /
`proxy_read_timeout`。

## 6. 资源预算与调优

2C2G 上的推荐配置：

```yaml
runtime:
  web_workers: 1                 # uvicorn 单 worker；2 核不值得多开
  crawl_concurrency: 4
  playwright_concurrency: 1      # 若启用
  sqlite_cache_mb: 32
```

内存估算（需实测校准）：

| 场景 | 预估常驻 | 评价 |
|---|---|---|
| Web + 调度 + LLM API 打标（**默认形态**） | 150–250 MB | 宽裕 |
| \+ 本地 ONNX 兜底加载中（需手动开启） | 400–500 MB | 安全 |
| \+ Playwright 单实例（需手动开启） | 700–1000 MB | 需实测，建议不与本地模型同时峰值 |

首站 `ujs-jwc` 实测不需 JS 渲染、不需本地模型，因此**默认部署落在第一行**，
2C2G 宽裕。后两行只在手动开启对应组件时才适用。

若三者叠加接近 limit，优先级建议：
放弃 Playwright（改用有 RSS 的源或配 CSS 选择器）> 放弃本地兜底 > 加内存。

CPU：2 核下 `onnxruntime` 的 `intra_op_num_threads=1`，
避免打标时把 web 请求饿死。

## 7. 可观测性

小系统不上 Prometheus。做法：

- 结构化 JSON 日志到 stdout，由 docker json-file 轮转收集
- `/admin/system` 页面直接查 DB 展示：各状态文章数、投递积压、
  provider 可用率、磁盘与附件占用、最近 50 条错误
- 可选：关键异常（全部 provider 熔断、磁盘超 85%、连续抓取失败）
  通过 Apprise 推给 admin ——复用已有推送能力，零额外依赖

## 8. 升级与迁移

- 迁移脚本编号递增，启动时自动执行未应用的版本
- 生产升级必须运行 `scripts/update.sh`：脚本先做 SQLite 在线备份；新版本未通过健康检查时，
  同时恢复旧源码、旧镜像和迁移前数据库。不要用裸 `docker compose up --build` 绕过升级备份
- 迁移只做前向兼容的变更；破坏性变更拆成两步发布
- 各组 `tags.json` 的 checksum 校验发生在迁移之后、服务就绪之前，
  不匹配则拒绝启动（见 [04-tagger.md](04-tagger.md) §1.2）
