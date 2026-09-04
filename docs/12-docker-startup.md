# 12 · Docker 启动

## 首次启动

在仓库根目录执行：

```bash
./scripts/install.sh
curl --noproxy '*' -f http://127.0.0.1:8080/healthz
```

脚本会创建未存在的 `.env`、`config/config.yaml` 与 `data/`，生成应用密钥，构建镜像并等待健康检查。首次管理员 setup token 可从日志获取：

```bash
docker compose -f deploy/docker-compose.yml logs nestra
```

打开日志中 `Initial administrator setup` 对应的 URL 创建管理员。

## 公网部署

先配置域名和 TLS 反向代理，再执行：

```bash
NESTRA_BASE_URL=https://nestra.example.com ./scripts/install.sh
```

Compose 只将服务绑定到 `127.0.0.1:8080`；不要直接暴露该端口到公网。

## 常用命令

```bash
# 服务状态与健康状态
docker compose -f deploy/docker-compose.yml ps

# 实时日志
docker compose -f deploy/docker-compose.yml logs -f nestra

# 停止服务
docker compose -f deploy/docker-compose.yml down

# 再次启动（保留数据）
docker compose -f deploy/docker-compose.yml up -d
```

## WSL 2 故障排查

若提示 `docker could not be found in this WSL 2 distro`，在 Docker Desktop 的 **Settings → Resources → WSL Integration** 中启用当前发行版，然后重新打开终端，再运行上述首次启动命令。
