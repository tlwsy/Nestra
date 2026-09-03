# notifier/

订阅匹配与推送投递。

| 计划文件 | 职责 |
|---|---|
| `matcher.py` | 订阅匹配（any/all）、转载去重、静默时段 |
| `dispatcher.py` | 投递执行、退避重试、失效目标自动禁用 |
| `message.py` | 消息体构建、按渠道能力截断 |
| `capabilities.py` | 渠道能力表：长度上限 / 附件支持 / body_format |
| `apprise_client.py` | Apprise 封装、URL 解密、测试推送 |

去重依赖 DB 唯一约束 `(subscription_id, article_id, target_id)` +
`INSERT OR IGNORE`，不依赖应用层判重。

参考 docs/05-notifier.md
