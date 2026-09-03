# storage/

仓储层。**业务规则不放这里**，只负责数据访问与事务。

| 计划文件 | 职责 |
|---|---|
| `db.py` | 连接管理、pragma 设置、事务上下文 |
| `migrations/` | 编号 SQL 文件（`001_init.sql` …），启动时自动应用 |
| `repositories/articles.py` | 文章状态流转查询（按 status + next_attempt_at 扫描） |
| `repositories/sites.py` | 站点同步（YAML → DB） |
| `repositories/tags.py` | 冻结标签集 + tag_vectors（sqlite-vec） |
| `repositories/users.py` | 用户、会话 |
| `repositories/subscriptions.py` | 订阅、推送目标 |
| `repositories/deliveries.py` | 投递记录，INSERT OR IGNORE 去重 |
| `repositories/ops.py` | provider_health、audit_log |

硬性约束：所有面向普通用户的查询方法**必须**接收 `user_id` 参数并写入
SQL 条件，使越权在类型层面就难以发生。

参考 docs/02-data-model.md
