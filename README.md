# 🦅 Nestra (Edge Edition)

> 🚧 **当前分支正在积极开发中 (Under Active Development)**

本分支（`edge`）致力于探索将 Nestra 的核心聚合与推送能力迁移至 **Cloudflare 原生无服务器边缘生态（Serverless / Edge）**，实现真正的**零服务器成本（0 成本）、免运维长期运行**。

---

## 🎯 目标架构规划

- **Runtime & Framework**：Cloudflare Workers / Pages Functions (TypeScript + Hono)
- **Database**：Cloudflare D1 (分布式 Serverless SQLite)
- **Attachment Storage**：Cloudflare R2 (S3 兼容对象存储)
- **Task Scheduler**：Cloudflare Cron Triggers (替代常驻单进程调度)
- **AI Inference**：Cloudflare Workers AI (向量嵌入与小模型打标)
- **Notification**：轻量原生 Webhook 驱动 (Telegram, 飞书, 钉钉, 企业微信, Bark, 邮件等)

---

## 📌 稳定版本与生产使用

如果你需要使用当前功能完整、开箱即用的生产版本（Python 3.12 + FastAPI + Docker 单容器）：

👉 **请切换至 [`master`](../../tree/master) 分支查看并部署。**
