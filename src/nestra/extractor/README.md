# extractor/

HTML → 结构化字段 + 附件。

| 计划文件 | 职责 |
|---|---|
| `article.py` | trafilatura 通用提取 + 站点级选择器覆盖 |
| `sanitize.py` | HTML 白名单清洗。**XSS 唯一防线，不可省** |
| `attachment.py` | 附件发现、流式下载（边下边校验大小）、sha256 去重 |
| `dedupe.py` | URL 规范化、simhash 计算与汉明距离 |

注意：`content_html` 来自第三方站点且会在 Web 端渲染，
入库前必须过 `sanitize.py`。

参考 docs/03-crawler.md §3 §5
