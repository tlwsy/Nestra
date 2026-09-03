# 06 · Web 管理端与鉴权

**前提：本服务设计为可公网暴露。** 本文档中标注为「硬性」的项不是可选加固，
而是上线前必须满足的条件。

## 0. 产品定位：私人自部署优先

本项目面向**私人使用**，他人需要时唯一方案是**自部署**。多用户能力
因此做得刻意轻量，不往 SaaS 方向做。

具体取舍：

| 不做 | 理由 |
|---|---|
| 开放注册 / 邮箱验证 / 邀请码 | 自部署场景下用户由 admin 直接创建即可 |
| 密码重置邮件流 | 需要 SMTP 配置，产出与成本不成比例；admin 直接重置 |
| 团队 / 组织 / 角色细分 | `admin` + `user` 两级足够 |
| 用户配额 / 计费 / 使用量统计 | 无多租户计费需求 |
| 邮箱作为登录标识 | 用用户名即可，避开邮箱验证整套逻辑 |

| 仍然保留 | 理由 |
|---|---|
| `user_id` 级数据隔离 | 即使只有 1 个用户，隔离写在仓储层才不会事后补不上 |
| 完整登录鉴权 + 限流 + 2FA | 公网暴露的底线，与用户数无关 |
| 审计日志 | 单人使用时也是排障依据 |
| 多用户表结构 | 表结构保留零成本，后期想扩不用迁移 |

一句话概括：**数据模型支持多用户，产品功能不为多用户做额外投入**。
安全相关的硬性项不因「只有我自己用」而降级——单用户服务被接管的后果
和多用户一样严重。

## 1. 技术选型

| 组件 | 选择 | 理由 |
|---|---|---|
| 框架 | FastAPI | 与 asyncio 抓取链路同栈；自带 OpenAPI |
| 前端 | Jinja2 + htmx + Tailwind(CDN 或构建产物) | 无 Node 构建链，镜像小，2C2G 友好 |
| 会话 | 服务端 Session（DB 存储） | 可即时吊销，优于纯 JWT |
| 密码哈希 | Argon2id (`argon2-cffi`) | 当前推荐；bcrypt 为次选 |
| 限流 | `slowapi` 或自实现 SQLite 计数窗口 | 不引入 Redis |

选 htmx 而非 SPA：这个管理端的交互复杂度是"表单 + 列表 + 筛选"，
上 React/Vue 要额外的构建产物、镜像体积和维护成本，收益不成比例。

## 2. 角色与权限

| 角色 | 权限 |
|---|---|
| `admin` | 全部：用户管理、站点管理、provider 配置、系统状态、标签集查看 |
| `user` | 自己的订阅、推送目标、已推送文章历史、个人设置 |

隔离原则（硬性）：所有 `user` 级查询必须在 SQL 层带 `user_id = :current_user`
条件，不依赖前端隐藏或路由约定。仓储层的用户态方法一律强制接收 `user_id` 参数，
使其无法被遗漏。

**站点与 provider 配置只有 admin 可见可改**——普通用户能改抓取目标或
看到 API key 变量名都是不必要的暴露。

## 3. 路由草图

```
公开
  GET  /login                    登录页
  POST /login                    登录（限流）
  POST /logout
  GET  /healthz                  存活探针（不含任何内部信息）

用户
  GET  /                         仪表盘：最近推送、订阅概览
  GET  /subscriptions            订阅列表
  POST /subscriptions            创建/编辑
  DELETE /subscriptions/{id}
  GET  /targets                  推送目标列表（脱敏）
  POST /targets
  POST /targets/{id}/test        测试推送
  GET  /articles                 已推送给我的文章（按标签/站点/时间筛选）
  GET  /articles/{id}            文章详情（清洗后 HTML）
  GET  /attachments/{id}         附件下载（鉴权 + 归属校验）
  GET  /settings                 改密码、2FA、活跃会话
  POST /settings/sessions/revoke 登出其他设备

管理员
  GET  /admin/users              用户 CRUD、重置密码、停用
  GET  /admin/sites              站点列表、启停、立即抓取、探测
  GET  /admin/sites/new          站点接入向导（五阶段）
  POST /admin/sites/probe        启动探测任务 → task_id
  GET  /admin/sites/probe/{tid}  轮询探测进度与结果
  POST /admin/sites/dryrun       按候选配置试运行，返回预览（不入库）
  POST /admin/sites/confirm      校验 dry-run hash 后落库（默认停用）
  GET  /admin/sites/picker       可视化选择器拾取器（沙箱渲染目标页）
  GET  /admin/tagset             冻结标签集查看（只读，按组）
  GET  /admin/tagset/groups      标签集分组列表
  POST /admin/tagset/groups      新建分组
  GET  /admin/providers          provider 健康状态、可用率、熔断状态
  GET  /admin/system             队列积压、内存占用、磁盘占用、最近错误
  GET  /admin/audit              审计日志
```

站点接入向导的完整设计见 [11-site-onboarding.md](11-site-onboarding.md)。
三个安全约束在此重申，因为它们是 Web 层的责任：

- `POST /admin/sites/probe` 接受用户输入的任意 URL，**是标准 SSRF 入口**。
  必须过内网地址拒绝 + IP pin + 重定向逐跳校验，且限流 3 次/min
- `GET /admin/sites/picker` 渲染第三方 HTML，必须 `<iframe sandbox>`（不带
  `allow-scripts`）+ 严格 CSP + 剥离 `<script>`/事件属性/`<base>`，
  复用 `extractor/sanitize.py`
- 探测与试运行都是后台任务，不可同步阻塞请求

标签集在 Web 端**只读**（除可选的审核/重建确认入口）。冻结语义要求不能
从界面随手新增标签；生成与重建走 [04-tagger.md](04-tagger.md) 的专用流程，
并以新 `tagset_version` 形式显式切换。

相应的管理员路由：

```
  POST /admin/tagset/build       触发标签集生成（llm 模式，后台任务，指定 group）
  GET  /admin/tagset/report      查看自检报告（按 group）
  POST /admin/tagset/freeze      确认冻结（require_manual_review 时必需）
```

生成、报告、冻结都以**分组**为单位。界面需明确标出各组状态
（`draft` / `frozen` / 文档数是否达到 `min_docs_for_build`），
因为未冻结组的站点会「正常抓取但不推送」，不标出来用户会以为系统故障。

## 4. 鉴权实现

### 4.1 会话

- 登录成功 → 生成 32 字节随机 token → Cookie 存明文，DB 存 SHA-256
- Cookie 属性（硬性）：`HttpOnly`、`Secure`、`SameSite=Lax`、`Path=/`
- 有效期：服务端默认固定 14 天；`remember_me=false` 时 Cookie 为浏览器会话级
- 登出 → 置 `revoked_at`，不删行（保留审计线索）

`Secure` 标志要求 HTTPS。若用户在纯 HTTP 下部署，登录会静默失败——
因此配置项 `web.cookie_secure` 默认 `true`，但需在文档与启动日志里
明确提示：**未配 TLS 时必须显式改为 false，且此时不应暴露公网**。

### 4.2 密码策略

- Argon2id，参数遵循 `argon2-cffi` 当前默认（约 64MB 内存/次）。
  注意：在 2G 机器上并发登录会吃内存，需限流兜底（见 §4.4）。
  若实测有压力，降至 `memory_cost=32MB`。
- 最小长度 12，不做复杂度强制（NIST 现行建议），但对照弱密码表拒绝
- 改密码需验证旧密码，成功后吊销其他所有会话

### 4.3 首次启动（硬性）

**不存在默认账号密码。** 首次启动时：

1. 若 `users` 表为空且环境变量 `NESTRA_ADMIN_PASSWORD` 已设 → 用它创建 admin
2. 否则生成一次性 setup token 打印到**容器日志**，
   访问 `/setup?token=...` 完成管理员创建
3. setup 路由在管理员创建后永久关闭

这条是公网部署的底线。带默认凭据的服务暴露公网后被接管只是时间问题。

**无开放注册。** 后续用户一律由 admin 在 `/admin/users` 创建（生成初始密码，
首次登录强制修改）。这是自部署定位的直接推论，也顺便消除了注册端点
这个公网攻击面。

### 4.4 限流（硬性）

| 端点 | 限制 |
|---|---|
| `POST /login` | 单 IP 10 次/5min；单账号失败状态递增（1/5/15/60 分钟），但完整正确的密码+2FA 可清除状态，避免远程锁死账号 |
| `POST /targets/{id}/test` | 单用户 5 次/min（防当外发中继滥用） |
| 全局写操作 | 单用户 60 次/min |

失败状态写 `users.locked_until` 并持久化；它抑制后续失败计数，但不能阻止完整正确
凭据登录，否则攻击者只需五次请求就能持续锁死已知的 admin 用户名。
失败登录一律记 `audit_log`，含 IP。

**反向代理下取真实 IP**：必须显式配置可信代理，
不能无条件信任 `X-Forwarded-For`（否则限流可被伪造头绕过）。
配置项 `web.trusted_proxies`，默认空（即用直连 IP）。

### 4.5 2FA（可选）

TOTP（标准库实现 RFC 6238），用户可在设置页自助开启或经密码 + TOTP/恢复码关闭。
`totp_secret` 加密存储；启用时提供 8 个只显示一次的恢复码（哈希存储）。开启后登录多一步验证；恢复码也丢失时，管理员可重置该用户的 2FA 并撤销其全部会话。

### 4.6 CSRF

htmx 表单提交需 CSRF 防护：Double Submit Cookie 或服务端 token。
`SameSite=Lax` 已挡住大部分场景，但状态变更端点仍应校验 token——
纵深防御，成本很低。

## 5. 机密管理

三类机密，处理方式不同：

| 机密 | 存放 | 说明 |
|---|---|---|
| LLM API key | **环境变量**，不入库不入 YAML | YAML 只写变量名 |
| 应用主密钥 | 环境变量 `NESTRA_SECRET_KEY` | 用于字段加密与签名 |
| 用户 Apprise URL | DB，用主密钥加密 | 用户自己填的，必须能读回来发送 |

字段加密用 `cryptography` 的 AES-256-GCM（随机 96-bit nonce，密文带版本前缀），
主密钥经 HKDF-SHA256 按用途派生独立的字段加密、会话签名、附件链接与 setup
子密钥。AEAD 的 additional data 绑定用途，跨用途密文不能互相解密。

启动校验（硬性）：`NESTRA_SECRET_KEY` 缺失或长度不足时**拒绝启动**，
不生成临时密钥——临时密钥会导致重启后所有已存的推送目标无法解密。

主密钥轮换：提供 `scripts/rotate_key.py`，用旧密钥解密全部密文字段、
用新密钥重新加密。这个脚本必须在设计时就留出，事后补很痛苦。

## 6. 安全响应头

由应用统一注入（不依赖用户是否正确配置了反向代理）：

```
Content-Security-Policy: default-src 'self'; img-src 'self' data:;
                         style-src 'self'; script-src 'self'; object-src 'none';
                         base-uri 'none'; form-action 'self'; frame-ancestors 'none'
Referrer-Policy: strict-origin-when-cross-origin
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Strict-Transport-Security: max-age=31536000   # 仅在 HTTPS 下发送
```

样式与脚本均由 `/static/` 同源提供，不允许内联脚本、CDN 资源或任意远程图片。

**文章正文渲染是本项目最大的 XSS 面**：`content_html` 来自第三方站点。
必须在入库前用白名单清洗（见 [03-crawler.md](03-crawler.md) §3），
Web 端渲染时不再依赖模板转义（因为需要保留格式）。清洗是唯一防线，
因此这一步不能省、不能"以后再说"。

## 7. 已实现文件

| 文件 | 职责 |
|---|---|
| `web/app.py` | FastAPI 应用装配、中间件、异常处理 |
| `web/deps.py` | 依赖注入：当前用户、admin 校验、DB 会话 |
| `web/security.py` | 密码哈希、会话签发校验、CSRF、限流 |
| `web/api/auth.py` | 登录/登出/setup/2FA |
| `web/api/user.py` | 订阅、推送目标、文章与附件（全部按 user_id 隔离） |
| `web/api/admin.py` | 用户、站点、向导、标签集、provider、系统与审计管理 |
| `web/templates/` | Jinja2 模板（`base.html` + 各页面） |
| `web/static/` | CSS/JS 静态资源 |

## 8. 上线前安全检查清单

- [ ] `NESTRA_SECRET_KEY` 已设为强随机值且已备份
- [ ] 管理员密码非默认、非弱密码
- [ ] TLS 已配置（反向代理），`cookie_secure=true`
- [ ] `trusted_proxies` 与实际部署拓扑一致
- [ ] 应用未直接监听 `0.0.0.0`（仅容器内网 / 回环）
- [ ] 登录限流经过实测验证
- [ ] 附件下载路径已验证归属校验（用户 A 不能下载用户 B 的附件）
- [ ] 文章 HTML 清洗已用恶意样本测试
- [ ] 拾取器沙箱渲染已用含 `<script>` 的页面测试，脚本未执行
- [ ] 探测接口已用 `127.0.0.1`、`169.254.169.254`、重定向到内网的 URL 测试并全部被拒
- [ ] `/healthz` 不泄漏版本、路径、配置等内部信息
- [ ] 审计日志正常写入
