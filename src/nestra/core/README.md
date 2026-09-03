# core/

基础设施层。**不做任何 I/O**（除读配置文件与写日志）。

| 计划文件 | 职责 |
|---|---|
| `config.py` | pydantic-settings 模型、YAML 加载、交叉校验 |
| `logging.py` | structlog 配置，JSON / console 两种输出 |
| `errors.py` | 异常层次根：`NestraError` 及各域基类 |
| `models.py` | 领域数据类（`Site` / `ArticleText` / `TagAssignment` …） |
| `crypto.py` | AES-256-GCM 字段加密、HKDF 子密钥派生、Argon2id、链接签名 |
| `time.py` | UTC 存储 / 本地时区展示、静默时段计算（含跨零点） |

参考 docs/08-config-reference.md、docs/06-web-auth.md §5
