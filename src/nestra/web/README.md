# web/

FastAPI + Jinja2 + htmx 管理端。设计为可公网暴露。

| 计划文件 | 职责 |
|---|---|
| `app.py` | 应用装配、中间件、安全响应头、异常处理 |
| `deps.py` | 依赖注入：当前用户、admin 校验 |
| `security.py` | Argon2 哈希、会话签发校验、CSRF、限流 |
| `api/auth.py` | 登录/登出/setup/2FA |
| `api/subscriptions.py` | 订阅 CRUD |
| `api/targets.py` | 推送目标 CRUD + 测试推送 |
| `api/articles.py` | 文章列表/详情/附件下载（归属校验） |
| `api/admin_users.py` | 用户管理 |
| `api/admin_sites.py` | 站点管理 + probe |
| `api/admin_system.py` | provider 健康 / 系统状态 / 审计 |
| `templates/` | Jinja2 模板 |
| `static/` | CSS / JS |

上线前必须过 docs/06-web-auth.md §8 检查清单。
标签集在此**只读**——冻结语义不允许从界面新增标签。

参考 docs/06-web-auth.md
