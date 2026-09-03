# tests/

`pytest` + `pytest-asyncio` + `respx`（httpx mock）。

| 目录 | 内容 |
|---|---|
| `unit/` | URL 规范化、simhash、订阅匹配布尔逻辑、静默时段跨零点、消息截断、prompt 解析与白名单过滤、错误分类 |
| `integration/` | 离线 fixture 跑完整提取链；mock HTTP 跑降级链全分支；内存 SQLite 跑仓储层 |
| `fixtures/` | 离线 HTML / RSS / API 响应样本、恶意 HTML 样本 |

原则：**不对真实站点和真实 LLM API 跑自动化测试**——
不稳定、有成本、有礼貌问题。全部 fixture + mock。

必须覆盖的安全测试：跨用户越权（订阅/文章/附件）、限流、CSRF、
HTML 清洗、签名链接过期与篡改。

参考 docs/09-roadmap.md 测试策略
