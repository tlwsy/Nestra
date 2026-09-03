# 05 · 订阅匹配与推送

## 1. 匹配逻辑

`dispatch_notifications` 任务扫描 `status = TAGGED` 的文章，对每篇：

```
对每个 enabled 的 subscription：
  1. 站点过滤：site_filter 非空且不含该文章 site_id → 跳过
  2. 取该文章的 article_tags 中 confidence >= sub.min_confidence 的标签集 T
  3. 取订阅关注的标签集 S = subscription_tags
  4. match_mode == 'any'  → T ∩ S ≠ ∅ 则命中
     match_mode == 'all'  → S ⊆ T      则命中
  5. 命中 → 对该订阅关联的每个 enabled target：
        INSERT OR IGNORE INTO deliveries (sub_id, article_id, target_id, 'pending')
处理完所有订阅后 → 文章置 NOTIFIED
```

关键点：

- 文章置 `NOTIFIED` 与投递记录写入在**同一事务**内，避免数据库内重复创建投递
- `INSERT OR IGNORE` + 唯一约束 `(subscription_id, article_id, target_id)`
  是去重的唯一依据。不依赖应用层判重逻辑。
- 无人订阅也置 `NOTIFIED`——这是正常终态，不是失败
- 转载去重：若该文章 simhash 与近 `dedupe_window_days`（默认 7）内某篇已为同一
  订阅创建 `pending` 或 `sent` 投递的文章汉明距离 ≤ 3，则记 `status='skipped'`，
  `last_error='duplicate_of:<article_id>'`；因此同一批匹配也不会先创建两组外部发送

## 2. 静默时段

订阅可配 `quiet_hours`（如 `23:00-07:00`，按全局 `app.timezone`）。
命中静默期时，投递记录仍然创建，但 `next_attempt_at` 设为静默结束时刻。
`retry_deliveries` 任务自然会在到期后发出——不需要单独的缓冲队列。

跨零点的区间（`23:00-07:00`）需正确处理，这是常见 off-by-one 来源。

## 3. 投递执行

```
retry_deliveries / dispatch 后置阶段：
  取 status='pending' 且 next_attempt_at <= now 的记录，按 target 分组
  对每组：
    解密 target 的 apprise_url
    构建消息体（见 §4）
    调用 apprise.notify(body=..., title=..., attach=[...])
    成功 → status='sent', sent_at=now
    失败 → attempts += 1
           attempts < max_attempts → 退避后重试
           否则 status='failed'
```

同一 target 的多条投递可以合并成一次调用吗？**不合并。**
每篇文章独立推送，用户体验更清晰，且失败重试粒度更细。
100 篇/天的量级不存在推送风暴问题。

退避：`backoff_base_sec * 2^attempts`，默认 base 30s，max_attempts 5
（30s / 1m / 2m / 4m / 8m）。待发送行先用数据库 `claim_token/claim_until`
原子领取，防止并发 worker 同时发送。外部渠道没有统一幂等键，因此语义是
**at-least-once**：极端情况下渠道已接收但进程在写回 `sent` 前崩溃，租约到期后
可能重发；这是避免静默漏发的取舍。

附件下载不阻塞这条投递：已下载项可直发/签名链接，仍 pending 或 failed 的项显示
原站 URL。后续下载成功不会再次发送整篇文章。

## 4. 消息体

Apprise 的 `notify()` 支持 `body_format`（text / markdown / html）。
不同渠道对格式支持不一，因此按 target 的渠道类型选择：

```yaml
notify:
  body_format: markdown        # 全局默认
  include_full_content: true   # 需求要求推送完整内容
  max_body_chars: 8000         # 超长时截断并附原文链接
  attachment_mode: apprise     # apprise | link | both
```

消息结构：

```
Title: [站点名] 文章标题

Body:
  **标签**：大模型基础设施 (0.87) · 推理优化 (0.72)
  **来源**：站点名 · 2025-01-15 10:23
  **原文**：https://...

  ---
  <完整正文>

  ---
  附件（3）：
  - paper.pdf (2.1 MB)
  - fig1.png (340 KB)
```

`include_full_content: true` 满足"推送完整内容"的需求。但需注意：

- Telegram 单条消息 4096 字符、Discord 2000 字符，超出会被截断或拒绝
- `max_body_chars` 是全局上限，发送时再与内置渠道上限取较小值
- 渠道能力表（长度上限、是否支持直传附件、支持的 body_format）维护在
  `notifier/capabilities.py`；未知能力的受支持渠道使用正文中的签名附件链接

超长处理：截断到上限并在末尾加"…（全文见原文链接）"。
不做自动分片——多条消息乱序到达的体验比截断更差。

## 5. 附件推送

`attachment_mode` 三种：

| 模式 | 行为 | 适用 |
|---|---|---|
| `apprise` | 通过 Apprise `attach=` 直接发送文件 | Telegram / Discord 等支持附件的允许渠道 |
| `link` | 只发本站下载链接（需登录鉴权） | 不支持附件的渠道，或附件过大 |
| `both` | 小附件直发，超过阈值的转链接 | 推荐默认 |

约束：

- Apprise 各渠道附件大小限制不同（例如 Telegram 50MB），
  同样进渠道能力表
- 超过渠道限制的自动降级为 `link`
- `link` 模式的下载 URL 必须**鉴权**——附件可能是私有内容。
  实现为带签名的时效性 URL（HMAC + 过期时间），
  或要求登录会话。不能是公开可猜的路径。

## 6. 推送目标管理

用户在 Web 端添加受支持的固定云服务 Apprise URL（如 `tgram://bottoken/chatid`）。
允许项限定为插件自身固定目标主机的云服务 scheme（Telegram、Discord、Slack、
企业微信、钉钉、飞书、Teams 等）。自定义主机 webhook/SMTP/ntfy、文件、桌面总线
及未知 scheme 一律拒绝；仅在创建时解析公网地址仍挡不住投递时 DNS rebinding/重定向。

- 存库前用应用主密钥加密（见 [06-web-auth.md](06-web-auth.md) §5）
- 列表展示脱敏指纹（`tgram://***chatid尾4位`），不回显完整 URL
- 提供"测试推送"按钮，立即发一条测试消息，把结果写回 `last_ok_at` / `last_error`
- 连续失败 N 次自动置 `enabled=0` 并在 Web 端提示，避免对着失效渠道
  无限重试（例如用户把 bot 踢出群）

## 7. 已实现文件

| 文件 | 职责 |
|---|---|
| `notifier/matcher.py` | 订阅匹配、转载去重、静默时段计算 |
| `notifier/dispatcher.py` | 投递执行、退避重试、失败降级 |
| `notifier/message.py` | 消息体构建、按渠道截断 |
| `notifier/capabilities.py` | 渠道能力表（长度/附件/格式） |
| `notifier/apprise_client.py` | Apprise 封装、URL 解密、测试推送 |

接口契约（供实现参考）：

```
@dataclass
class MatchedDelivery:
    subscription_id: int
    article_id: int
    target_id: int
    scheduled_at: datetime        # 静默期时为静默结束时刻

class Matcher(Protocol):
    def match(self, article_id: int) -> list[MatchedDelivery]: ...

class Dispatcher(Protocol):
    async def send(self, delivery_id: int) -> DeliveryOutcome: ...
```
