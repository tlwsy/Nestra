# deploy/

| 文件 | 状态与说明 |
|---|---|
| `Dockerfile` | **M0 已实现**：多阶段、锁定依赖、`python:3.12-slim`、非 root 运行 |
| `docker-compose.yml` | **M0 已实现**：单服务、回环端口、1400M 上限、健康检查、日志轮转 |
| `entrypoint.sh` | **M0 已实现**：严格配置校验 → DB 迁移 → 启动 |
| `nginx.example.conf` | M6 计划：反代示例 |
| `Caddyfile.example` | M6 计划：自动 TLS 反代示例 |

Playwright 与本地 ONNX 均保持默认不安装；可选镜像层在 M3/M6 接入。

应用不直面公网：容器只绑回环，TLS 由宿主反代终结。

参考 docs/07-deployment.md
