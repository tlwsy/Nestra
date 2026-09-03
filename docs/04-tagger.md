# 04 · 打标：标签集冻结 + 多级降级链

本文档是整个项目最核心的部分。分两个彼此独立的阶段：

- **阶段一（一次性）**：从历史文章生成标签集，冻结
- **阶段二（运行期）**：把新文章分类到冻结标签集的子集上

两阶段必须分离。运行期**永远不新增标签**——这既是需求，也避免订阅语义漂移。

**标签集按组划分。** 下文所有「标签集」均指**某一组**的标签集。生成、冻结、
重建都以组为单位，组之间完全隔离。只有一个组时，行为与无分组一致。
详见 §1.6。

---

## 阶段一 · 标签集生成与冻结

### 1.0 两种执行形态（并行支持）

同一套流程逻辑，两种运行方式，共用 `tagger/bootstrap/` 下的实现：

| 形态 | 入口 | 适用 | 资源需求 |
|---|---|---|---|
| **A · 独立离线程序** | `scripts/bootstrap_tagset.py` | 开发机 / 一次性重建 | 可用本地 embedding + HDBSCAN，峰值 1GB+ |
| **B · VPS 内 LLM 生成** | Web 端「生成标签集」或 `scripts/bootstrap_tagset.py --mode=llm` | 2C2G 生产环境 | 纯 API 调用，内存近乎为零 |

形态 B 的关键差异：**不做本地 embedding，也不做 HDBSCAN**。改为分批把文章
标题+摘要送进 LLM 做「主题归纳 → 候选标签 → 跨批次合并」。这样 2C2G 也能
独立完成标签集构建，不依赖开发机。

形态 B 的取舍要说清楚：

- 优点：零本地模型、内存友好、可在 Web 端一键触发
- 缺点：**没有质心向量**。而质心是本地 ONNX 兜底（阶段二最后一级）的前提。
  因此形态 B 产出的标签集，`centroid` 字段为空，本地兜底不可用——降级链会
  在 LLM 全挂时停在 `EXTRACTED` 等待重试，而非落到本地模型
- 若后续想补齐质心：启用本地模型后跑 `scripts/backfill_centroids.py`，
  对每个标签的代表文档做 embedding 求质心，写回 `tags.json`

配置里显式选择：

```yaml
tagset:
  build_mode: llm          # llm | embedding
  # embedding 模式需 local_model.enabled: true
```

默认 `llm`，与「本地模型默认不开启」保持一致。

### 1.1 流程

```
历史文章（建议 300–2000 篇）
        │
        ▼  ① 抽取正文（复用 extractor）
   文本语料
        │
        ├──────────── 形态 A（embedding 模式）────────────┐
        │  ② embedding（bge-small-zh-v1.5）              │
        │  ③ HDBSCAN 聚类（自动定簇数，允许噪声点）        │
        │  ④ 每簇取代表文档 → LLM 命名 + 写判定说明        │
        └────────────────────────────────────────────────┘
        │
        ├──────────── 形态 B（llm 模式）─────────────────┐
        │  ②' 分批（每批 30–50 篇标题+摘要）送 LLM 归纳   │
        │  ③' 跨批次候选标签合并去重（LLM 二次归并）       │
        │  ④' LLM 补写 description / keywords            │
        └────────────────────────────────────────────────┘
        │
        ▼  ⑤ 自动净化（同 slug/同规范名去重、无意义簇过滤）—— 见 1.1.1
   候选标签集
        │
        ▼  ⑥ 可选人工审核（默认跳过，可配置要求）
   最终标签集
        │
        ▼  ⑦ 计算 checksum（+ 质心，仅形态 A）→ 冻结
   data/models/tagsets/{group}/tags.json  (只读)
```

形态 A 的 `③` 选 HDBSCAN 而非 K-Means：不需要预设簇数，且允许"噪声"——
不是每篇文章都必须归入某个主题簇，强行分配会产出垃圾标签。

`④` / `④'` 的 LLM 调用是一次性的，成本可忽略，可以用较强的模型。

### 1.1.1 自动净化：把人工审核降到最低

你的要求是**尽可能不人工介入，但保留人工介入的可能**。原设计把人工审核列为
不可省略，这里改为自动净化 + 可选审核。

自动净化规则（`⑤`，全自动执行）：

| 问题 | 自动处理 |
|---|---|
| 同 slug 或规范化名称重复 | 合并覆盖文章、关键词与代表标题，不猜测语义近似关系 |
| 过小标签 | 覆盖文档数 < `min_cluster_docs`（默认 5）→ 丢弃 |
| 过泛标签 | 语料不少于 10 篇且覆盖率 > 40% → 丢弃 |
| 标签数超上限 | 按覆盖文档数排序截断到 `max_tags`（默认 40） |

净化后跑一次**自检报告**，输出到 `data/models/tagset_report.md`：
标签列表、每个标签的覆盖文档数、代表标题 3 条、被合并/丢弃的项及原因。

```yaml
tagger:
  tagset:
    auto_curate:
      min_cluster_docs: 5
      max_tags: 40

tagset_groups:
  - slug: campus
    name: 校园
    require_manual_review: false  # true 则冻结前必须确认
```

`require_manual_review: false`（默认）时全自动冻结，只产出报告供事后查看。
置 `true` 时标签集停在 `draft` 状态，需在 Web 端逐条确认后才 `frozen`。

诚实说明：全自动的标签质量**低于**人工审核过的版本，近义标签和边界模糊
大概率仍有残留。发现不满意时可显式重建并确认新版本；系统不会擅自重打或重推
历史文章。

### 1.2 `tags.json` 结构

```json
{
  "group": "campus",
  "tagset_version": "2025-01-15-a3f9c1",
  "frozen_at": "2025-01-15T10:23:00Z",
  "embedding_model": "bge-small-zh-v1.5",
  "embedding_dim": 384,
  "checksum": "sha256:...",
  "tags": [
    {
      "id": 1,
      "slug": "llm-infra",
      "name": "大模型基础设施",
      "description": "推理框架、显存优化、模型部署与服务化相关内容。不含单纯的模型能力评测。",
      "keywords": ["vLLM", "推理", "量化", "KV cache"],
      "threshold": 0.38,
      "centroid": [0.0123, -0.0456, "..."]
    }
  ]
}
```

要点：

- `description` 是给 LLM 看的**判定边界**，包含正例与反例描述。
  这比标签名本身更能决定分类质量。
- `checksum` 覆盖除自身外的全部内容。启动时校验，不匹配则拒绝启动并告警——
  防止标签集被意外改动导致既有订阅语义变化。
- `id` 与 `tags` 表主键一一对应，永不复用。
- `centroid` 供本地兜底与候选预筛使用。**形态 B（llm 模式）下为 `null`**，
  此时本地兜底不可用，需靠 `backfill_centroids.py` 补齐。
- `build_mode` 记录产出方式，Web 端据此提示本地兜底是否可用。

### 1.3 规模建议

标签数控制在 **30–80** 个。原因：

- LLM prompt 要带全部标签的 name + description，80 个约 2–4k tokens，
  是每次调用的成本下限
- 超过这个规模时启用**候选预筛**：先用本地 embedding 算 top-K（默认 20）
  候选标签，只把候选送进 prompt。既降 token 成本又提升准确率
- 但预筛需要常驻本地模型，与"零内存"目标冲突。因此：
  标签数 ≤ 80 时不预筛，> 80 时再权衡

### 1.4 已实现脚本

| 文件 | 职责 |
|---|---|
| `scripts/bootstrap_tagset.py` | 阶段一全流程 CLI，`--mode=llm\|embedding --group=<slug>` |
| Web tagset report/draft | 浏览器查看明文报告并明确确认冻结 |
| `scripts/freeze_tagset.py` | 校验已审阅 JSON，计算 checksum 并写入 tags/tag_vectors |
| `scripts/backfill_centroids.py` | 为 llm 模式产出的标签集补质心，启用本地兜底 |
| `tagger/bootstrap/pipeline.py` | 两形态共用的流程编排 |
| `tagger/bootstrap/cluster.py` | embedding + HDBSCAN（形态 A） |
| `tagger/bootstrap/llm_induct.py` | 分批归纳 + 跨批合并（形态 B） |
| `tagger/bootstrap/curate.py` | 自动净化规则 + 自检报告 |

bootstrap 默认最多读取最近 2000 篇有效历史文章（可用 `--max-documents` 下调），
LLM 批次候选采用分层合并，避免一次把全语料/全部候选装入内存或 prompt。
形态 A 先用 `uv sync --extra bootstrap` 安装 CPU embedding/HDBSCAN 依赖，建议在
本地开发机跑（内存峰值可能 1GB+）；把产物拷到 VPS 后需运行
`scripts/freeze_tagset.py` 安装到运行库。形态 B 可直接在 VPS 上跑或从 Web 端触发，
是 2C2G 环境的默认路径。

### 1.5 标签集重建

「一经生成即固定」表示运行期分类器永不创建或修改标签。管理员仍可显式构建
新版本并确认冻结；未确认的结果只写 draft/report，不影响当前版本。

重新冻结时，同 slug 标签保留原数据库 ID，因此现有订阅不漂移；若新版本试图删除
仍被订阅的标签，冻结会直接拒绝，管理员必须先调整相关订阅。新增标签只影响后续分类，
系统不擅自重跑或重推历史文章。这样不需要 `needs_attention` 状态或隐式迁移，也不会
静默破坏订阅。冻结先写同目录 pending artifact，再提交 SQLite，最后原子替换
`tags.json`；若提交后中断，下一次启动按数据库版本恢复 pending 文件。

### 1.6 标签集分组

**问题**：标签集从某个站点的历史文章生成，主题必然带该站点的烙印。
用教务处 1162 篇文章生成的标签集去打一个技术博客的文章，结果是全部低置信度
或胡乱命中——「一经生成即固定」与「多站点异构」天然冲突。

**解法**：标签集按组划分，站点声明归属。

```yaml
tagset_groups:
  - slug: campus
    name: 校园教务
    description: 高校教务处、学院通知公告类内容
  - slug: tech
    name: 技术
    description: 技术博客、开源项目动态

sites:
  - slug: ujs-jwc
    tagset_group: campus
  - slug: some-tech-blog
    tagset_group: tech
```

语义规则：

- 每组**独立跑阶段一**，独立冻结，有各自的 `tagset_version` 与 checksum
- 文章打标时，**只在其站点所属组的标签集内选择**。prompt 里只带该组标签，
  这顺带压低了 token 成本
- 组内「一经生成即固定」照旧成立
- **新增一个组不触碰任何已有组**：不重跑、不重打标、不影响任何现有订阅。
  这是分组相对「全局标签集追加」的核心优势
- 重建（§1.5）也以组为单位，受影响面限定在该组

文件布局：

```
data/models/tagsets/
  campus/tags.json
  campus/tagset_report.md
  tech/tags.json
  tech/tagset_report.md
```

启动时逐组校验 checksum，任一组不匹配则拒绝启动并指明是哪一组。

新站点接入时组的归属**不自动判定**——这是业务语义，只有你知道新站点
和已有站点是不是同一类。向导会让你选「归入已有组」或「新建组」，
见 [11-site-onboarding.md](11-site-onboarding.md) §3 阶段五。

选新建组时，该站点会先以**无标签**状态入库抓取，攒够 `min_docs_for_build`
（默认 300 篇）后才能跑阶段一。此前文章正常存档但不推送——
这一点必须在 Web 端明确提示，否则用户会以为系统坏了。

---

## 阶段二 · 运行期打标降级链

### 2.1 链式结构

```
                    ┌─────────────────────────┐
   文章文本  ───────►│  TaggerChain            │
                    └───────────┬─────────────┘
                                │  按配置顺序
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
  provider[0]             provider[1]             provider[N]
  ├─ model[0]             ├─ model[0]             ├─ model[0]
  ├─ model[1]             └─ model[1]             └─ ...
  └─ model[2]
        │                       │                       │
        └───────────────────────┴───────────────────────┘
                                │  全部失败
                                ▼
                    ┌─────────────────────────┐
                    │  LocalTagger (ONNX)     │  兜底
                    └───────────┬─────────────┘
                                │  也失败/模型缺失
                                ▼
                     保持 EXTRACTED，下轮重试
                     （不写 FAILED）
```

遍历顺序：**provider 外层，model 内层**。即 provider[0] 的所有 model
都失败后，才切到 provider[1]。这符合"同一家的备用模型优先于换家"的直觉，
也便于把便宜模型放前面。

### 2.2 配置形态

```yaml
tagger:
  strategy: llm_chain_with_local_fallback

  llm:
    request_timeout_sec: 30
    max_retries_per_model: 2         # 仅瞬时错误
    backoff_base_sec: 2

    providers:
      - name: deepseek
        type: openai_compatible
        base_url: https://api.deepseek.com/v1
        api_key_env: DEEPSEEK_API_KEY     # 只从环境变量读，不写在 YAML
        models: [deepseek-chat]
        max_input_chars: 8000

      - name: gemini
        type: gemini
        api_key_env: GEMINI_API_KEY
        models: [gemini-2.0-flash, gemini-2.0-flash-lite]

      - name: openrouter
        type: openai_compatible
        base_url: https://openrouter.ai/api/v1
        api_key_env: OPENROUTER_API_KEY
        models: [meta-llama/llama-3.3-70b-instruct]

  local:
    enabled: false                   # 默认关闭，需手动开启
    model_path: data/models/bge-small-zh-v1.5-int8.onnx
    idle_unload_after_sec: 900       # 空闲 15min 卸载，释放内存
    top_k: 5                         # 最多打几个标签
```

设计要点：

- **API key 一律走环境变量**，YAML 里只写变量名。配置文件可以放心提交、
  可以在 Web 端展示，不含机密。
- `type` 决定适配器：`openai_compatible` 一个实现覆盖绝大多数服务
  （DeepSeek / OpenRouter / 硅基流动 / 本地 vLLM / Ollama），
  只有 Gemini 和 Anthropic 需要单独适配器。
- 顺序即优先级，用户可自由编排。
- **`local.enabled` 默认 `false`**。未开启时降级链末级直接是「保持
  `EXTRACTED` 等下轮」，不尝试加载模型。与 Playwright 一致：
  **重资源组件均需显式开启**。

### 2.3 失败分类与熔断

**必须区分错误类型**，否则降级逻辑会误判：

| 类型 | 例子 | 处理 |
|---|---|---|
| 瞬时（retryable） | 超时、连接重置、5xx、429 | 同 model 内退避重试，用满 `max_retries_per_model` 后切下一个 model |
| 永久（fatal） | 401/403（key 错）、404（model 不存在） | **不重试**，直接切下一个 model，并在 Web 端标红提示配置错误 |
| 配额 | 429 且带长 `Retry-After`、余额不足 | 切下一个 provider，该 provider 进冷却 |
| 输出不合规 | 返回非 JSON、标签不在冻结集内 | 重试 1 次（带纠正提示），仍失败则切换 |

熔断（写入 `provider_health` 表）：

```yaml
circuit_breaker:
  failure_threshold: 5          # 连续失败 5 次
  cooldown_sec: 600             # 冷却 10 分钟，期间直接跳过该 provider
  half_open_probe: true         # 冷却结束后先试 1 次
```

熔断状态持久化在 DB，重启不丢——避免重启后立刻又对着挂掉的 provider 打满重试。

### 2.4 LLM 打标的输出约束

这是**质量保证的关键环节**。三层防护：

1. **结构化输出**：优先用 provider 的 JSON Schema / `response_format`
   强约束；不支持的服务退化为 prompt 约束 + 容错解析
2. **白名单校验**：返回的每个 slug 必须存在于冻结标签集中，
   否则丢弃该项并记 warning。杜绝幻觉标签污染数据
3. **数量与置信度约束**：最多 `top_k` 个标签，置信度 < 阈值的丢弃；
   允许返回空数组（"这篇文章不属于任何已有标签"是合法且重要的结果）

Prompt 骨架（伪）：

```
System: 你是文章分类器。只能从给定标签列表中选择，不得创造新标签。
        输出 JSON: {"tags": [{"slug": "...", "confidence": 0.0-1.0}]}
        若文章不属于任何标签，返回 {"tags": []}。

User:   可用标签：
        - slug: llm-infra
          名称: 大模型基础设施
          判定: 推理框架、显存优化、模型部署与服务化。不含单纯模型评测。
        - ...

        文章标题：{title}
        文章正文（截断至 {max_input_chars} 字）：
        {content}
```

正文截断策略：取开头 60% + 结尾 40%（结论常在末尾），
而不是简单取前 N 字。

### 2.5 本地兜底（默认关闭）

仅在 `local.enabled: true` 且 `tags.json` 含质心时可用。两个前提缺一不可：

- 未开启 → 链末直接保持 `EXTRACTED`，不加载模型
- 开启但标签集为 `build_mode: llm`（无质心）→ Web 端提示需先跑
  `backfill_centroids.py`，否则兜底仍不可用

开启后的行为：

- 加载 int8 量化的 `bge-small-zh-v1.5` ONNX，`onnxruntime` CPU provider
- 文章向量与 `tag_vectors` 做余弦相似度，超过各标签 `threshold` 的取 top_k
- `intra_op_num_threads=1`，避免在 2 核机器上和 web 进程抢 CPU
- **懒加载 + 空闲卸载**：首次兜底时才 load，`idle_unload_after_sec` 后释放
- 模型文件缺失时不报致命错误，降级为"保持 EXTRACTED 等下轮"，
  并在 Web 端提示"本地兜底不可用"

置信度语义差异需注意：LLM 给的是主观置信度，本地给的是余弦相似度，
两者不同分布。`article_tags.backend` 记录来源，Web 端展示时区分标注，
用户设 `min_confidence` 时也应看到这个提示。

### 2.6 已实现文件

| 文件 | 职责 |
|---|---|
| `tagger/chain.py` | 降级链编排、熔断、错误分类 |
| `tagger/base.py` | `Tagger` 协议、`TagResult`、异常层次 |
| `tagger/tagset.py` | 分组标签集加载、逐组 checksum 校验、冻结集查询 |
| `tagger/prompt.py` | Prompt 构建、正文截断、输出解析与白名单校验 |
| `tagger/providers/openai_compatible.py` | 覆盖多数服务 |
| `tagger/providers/gemini.py` | Gemini 原生 API |
| `tagger/providers/anthropic.py` | Anthropic Messages API |
| `tagger/local_onnx.py` | ONNX embedding + 相似度 + 懒加载/卸载（可选依赖） |

接口契约（供实现参考）：

```
@dataclass
class TagAssignment:
    tag_id: int
    slug: str
    confidence: float

@dataclass
class TagResult:
    assignments: list[TagAssignment]
    backend: str                    # "llm:deepseek:deepseek-chat" | "local:bge-small-zh"

class Tagger(Protocol):
    async def tag(self, article: ArticleText) -> TagResult: ...

# 异常层次决定降级行为
class TaggerError(Exception): ...
class TransientError(TaggerError): ...     # 重试
class FatalConfigError(TaggerError): ...   # 不重试，切下一个
class QuotaError(TaggerError): ...         # 切 provider + 冷却
class OutputInvalidError(TaggerError): ... # 纠正后重试一次
```

## 3. 成本估算

100 篇/天，每次调用约 2–4k input tokens（80 个标签的 description + 正文截断）
+ 少量 output。用主流廉价模型（DeepSeek / Gemini Flash 一类），
量级在**每天几分钱**，可忽略。

标签集生成（形态 B）是一次性成本：实测目标站点共 1162 篇历史文章，
分批归纳约 30 批，量级在几毛钱到一元。这是形态 B 在 2C2G 上可行的前提。

这也是默认走 LLM 而非本地模型的根本原因：在这个量级上，
API 成本远低于为常驻模型多买 1G 内存的 VPS 差价。
