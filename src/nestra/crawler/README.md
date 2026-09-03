# crawler/

从站点发现文章 URL 并下载页面。**不负责解析正文**（那是 extractor/）。

| 计划文件 | 职责 |
|---|---|
| `fetcher.py` | httpx 封装：限速、退避重试、条件请求（ETag/Last-Modified）、robots.txt 缓存 |
| `renderer.py` | Playwright 封装。并发上限 1，用完即关 |
| `discovery/base.py` | `Discoverer` 协议 + 模式注册表 |
| `discovery/rss.py` | feedparser；feed 含全文时可跳过单页请求 |
| `discovery/sitemap.py` | sitemap（支持 index 嵌套）+ lastmod 过滤 |
| `discovery/html_list.py` | selectolax 选择器 + 分页 |
| `discovery/json_api.py` | JSON 点号路径映射 |

新增发现模式 = 加一个文件 + 注册，不改现有代码。

参考 docs/03-crawler.md
