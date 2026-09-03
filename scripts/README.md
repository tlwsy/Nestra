# scripts/

| 脚本 | 用途 | 运行位置 |
|---|---|---|
| `install.sh` | 首次安装，幂等 | VPS |
| `update.sh` | 先备份再升级；健康失败时恢复旧源码、镜像和数据库 | VPS |
| `backup.sh` | SQLite 在线 Backup API（**不能直接 cp 运行中的 DB**）+ 配置、附件、标签集/模型 | VPS |
| `restore.sh` | 校验归档、停服恢复，失败自动回滚 | VPS |
| `rotate_key.py` | 主密钥轮换，重加密全部密文字段 | VPS |
| `probe_site.py` | 探测站点：有无 RSS、是否需 JS、推荐 discovery_mode | 任意 |
| `bootstrap_tagset.py` | 标签集生成：直接 LLM，或 embedding → HDBSCAN → LLM 命名（embedding 模式先安装 `bootstrap` extra） | **本地开发机** |
| `freeze_tagset.py` | 审阅/编辑 `tags.draft.json` 后计算 checksum、写入 tags/tag_vectors | 本地或 VPS |

备份默认包含附件；仅在明确接受附件不可恢复时设置 `INCLUDE_ATTACHMENTS=0`。

embedding 模式先执行 `uv sync --extra bootstrap`。标签集生成放本地机器跑：
内存峰值可能 1GB+；产物拷到 VPS 后再用 `freeze_tagset.py` 安装到运行库，生产环境不承担生成成本。

参考 docs/07-deployment.md §3、docs/04-tagger.md 阶段一
