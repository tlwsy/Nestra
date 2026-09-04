# Nestra

自动抓取多站点文章 → 按冻结标签集自动打标 → 命中用户订阅时推送全文与附件。

面向低配 VPS（2C2G）设计，单容器部署，Docker / `install.sh` 一键起。

## 当前状态

**M0–M7 实现完成。** 已包含多模式抓取与附件下载、冻结标签集生成、原生
OpenAI-compatible/Gemini/Anthropic 降级链、可选 ONNX 兜底、订阅匹配与 Apprise
投递、单进程调度、安全多用户 Web、站点探测/预览向导以及部署运维脚本。

冻结标签集是部署数据，不提交虚构的默认标签。首次部署需先回填本组文章并执行
bootstrap；在标签集冻结前，抓取正常运行，文章会安全停在 `EXTRACTED`。
验收边界和手工外部服务检查见 [docs/09-roadmap.md](docs/09-roadmap.md)。

## 快速开始

一键 Docker 部署：

```bash
./scripts/install.sh  # 默认生成可在本机使用的 http://127.0.0.1:8080 配置
curl http://127.0.0.1:8080/healthz
# 公网首次安装：NESTRA_BASE_URL=https://nestra.example.com ./scripts/install.sh
# 未预设 NESTRA_ADMIN_PASSWORD 时，从 docker compose logs nestra 取得一次性 setup token
```

本地开发：

```bash
cp config/config.example.yaml config/config.yaml
install -m 0600 config/env.example .env
export NESTRA_SECRET_KEY="$(openssl rand -base64 32)"
export NESTRA__WEB__BASE_URL=http://127.0.0.1:8080
export NESTRA__WEB__COOKIE_SECURE=false  # 仅本地；公网必须 TLS + true
export DEEPSEEK_API_KEY="..."       # 也可稍后配置；缺失时只暂停打标
uv sync --extra crawl --extra web --extra notify
uv run nestra config check
uv run nestra db migrate
uv run nestra crawl --site ujs-jwc --dry-run
uv run nestra serve
```

首次标签集引导（本地开发示例）：

```bash
uv run python scripts/backfill_history.py --site ujs-jwc --pages 78
uv run python scripts/bootstrap_tagset.py --group campus --mode llm --config config/config.yaml
# require_manual_review=true 时，审阅 draft/report 后再运行 scripts/freeze_tagset.py
uv run nestra run-once
```

Docker 部署把前缀换为 `docker compose -f deploy/docker-compose.yml exec nestra python`
（CLI 命令则用同一前缀后接 `-m nestra.cli`）；也可直接在 Web 的 Tagsets 页面构建、
审阅并冻结。

质量检查：`uv run ruff check src tests scripts && uv run pytest -q`。

## 核心设计取向

| 决策 | 选择 | 原因 |
|---|---|---|
| 语言 | Python 3.12 | Apprise / trafilatura / onnxruntime 生态；打标链路无替代品 |
| 站点扩展 | 声明式 YAML 适配器 | 加站点 = 加配置，不改代码 |
| 标签集 | 一次性生成后**冻结** | 运行期只做分类，永不新增标签 |
| 打标后端 | LLM 多provider链 → 本地 ONNX 兜底 | 常态零内存占用，断网仍可用 |
| 存储 | SQLite (WAL)；sqlite-vec 可选 | 30–80 个质心可先用 BLOB 暴力比对，不强制扩展 |
| 进程模型 | 单进程 uvicorn + APScheduler | 不引入 Redis/Celery |
| 推送 | Apprise | 复用固定云服务插件与附件能力，不开放任意主机目标 |

## 文档索引

| 文档 | 内容 |
|---|---|
| [01-architecture.md](docs/01-architecture.md) | 总体架构、流水线状态机、模块边界 |
| [02-data-model.md](docs/02-data-model.md) | 全部表结构、索引、状态流转 |
| [03-crawler.md](docs/03-crawler.md) | 多站点发现与抓取、正文/附件提取 |
| [04-tagger.md](docs/04-tagger.md) | 标签集生成与冻结、provider 降级链、本地兜底 |
| [05-notifier.md](docs/05-notifier.md) | 订阅匹配、投递去重、Apprise 集成 |
| [06-web-auth.md](docs/06-web-auth.md) | Web 管理端、鉴权、多用户、公网暴露加固 |
| [07-deployment.md](docs/07-deployment.md) | 镜像构建、compose、一键脚本、内存预算 |
| [08-config-reference.md](docs/08-config-reference.md) | 配置项逐项说明 |
| [09-roadmap.md](docs/09-roadmap.md) | 分阶段实现计划与验收标准 |
| [10-site-probe-ujs-jwc.md](docs/10-site-probe-ujs-jwc.md) | 首个目标站点的实测探测报告 |
| [11-site-onboarding.md](docs/11-site-onboarding.md) | 站点接入向导：自动探测 + 可视化确认 |
| [12-docker-startup.md](docs/12-docker-startup.md) | Docker 首次启动、验证与 WSL 2 排障 |

## 目录结构

```
Nestra/
├── config/              # 配置文件（config.example.yaml 为模板）
├── data/                # 运行时数据（不入库）
│   ├── db/              #   SQLite 库文件
│   ├── attachments/      #   下载的附件，按 sha256 分片存放
│   └── models/          #   本地 ONNX 模型与 tagsets/{group}/tags.json
├── deploy/              # Dockerfile / compose / nginx 示例
├── docs/                # 设计与实现文档
├── scripts/             # 一键部署、标签集生成、运维脚本
├── src/nestra/         # 应用包
│   ├── core/            #   配置、日志、异常、领域模型
│   ├── crawler/         #   站点发现与抓取
│   ├── extractor/       #   正文与附件提取
│   ├── tagger/          #   打标（含 providers/ 降级链）
│   ├── onboarding/      #   站点接入探测（含 detect/ 各项检测）
│   ├── notifier/        #   订阅匹配与推送
│   ├── scheduler/       #   定时任务编排
│   ├── storage/         #   仓储层与迁移
│   └── web/             #   FastAPI 应用（api/ templates/ static/）
└── tests/               # unit / integration / fixtures
```

## 安全前提

Web 端设计为可公网暴露，因此以下为**硬性要求**，不是可选项：

- 首次启动强制设置管理员密码，不存在默认凭据
- 密码 Argon2id 哈希，会话 Cookie 走 `Secure` + `HttpOnly` + `SameSite=Lax`
- 登录接口限流，失败递增退避
- 用户的 Apprise 推送 URL（内含 token）在库中加密存储
- TLS 由反向代理终结，应用本身只监听回环或容器内网

细节见 [06-web-auth.md](docs/06-web-auth.md)。
