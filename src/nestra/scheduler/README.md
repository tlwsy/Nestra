# scheduler/

APScheduler 任务编排。业务逻辑在各自模块，这里只负责调度与退避。

| 计划文件 | 职责 |
|---|---|
| `jobs.py` | 五个任务：crawl_sites / tag_articles / dispatch_notifications / retry_deliveries / housekeeping |
| `runner.py` | 调度器生命周期、优雅关闭 |
| `backoff.py` | 指数退避与 next_attempt_at 计算 |

全部任务 `max_instances=1` + `coalesce=True`，防止堆积打满内存。

参考 docs/01-architecture.md §4
