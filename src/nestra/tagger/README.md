# tagger/

文章文本 → 冻结标签集的子集。**运行期永不新增标签。**

| 计划文件 | 职责 |
|---|---|
| `base.py` | `Tagger` 协议、`TagResult`、异常层次（决定降级行为） |
| `tagset.py` | 分组标签集加载、逐组 checksum 校验、冻结集查询 |
| `prompt.py` | prompt 构建、正文截断、输出解析 + 白名单过滤 |
| `chain.py` | 降级链编排、错误分类、熔断 |
| `local_onnx.py` | ONNX embedding + 余弦相似度，懒加载 / 空闲卸载 |
| `providers/openai_compatible.py` | 覆盖 DeepSeek / OpenRouter / vLLM / Ollama 等 |
| `providers/gemini.py` | Gemini 原生 API |
| `providers/anthropic.py` | Anthropic Messages API |

降级顺序：providers 外层 × models 内层 → 全失败 → local → 仍失败则
保持文章 EXTRACTED 状态等下轮（**不写 FAILED**）。

标签集生成是离线阶段，代码在 scripts/，不在这里。

参考 docs/04-tagger.md
