# 03 · 抓取与提取

目标：**加一个新站点只需写一段 YAML，不改代码。**

## 1. 两阶段分离

```
发现（Discovery）：站点 → 文章 URL 列表
提取（Extraction）：文章 URL → 结构化字段 + 附件清单
```

分离的理由：发现方式因站点差异极大（RSS / sitemap / 列表页 / API），
而提取在多数站点上可以用同一套通用算法（trafilatura）解决。
把差异集中在发现层，提取层保持通用 + 少量覆盖。

## 2. 发现层：四种模式

按优先级尝试，配置里显式指定 `discovery_mode`。

### 2.1 `rss` —— 首选
```yaml
discovery_mode: rss
config:
  feed_url: https://example.com/feed.xml
  # RSS 常含全文，命中则跳过单页抓取
  content_from_feed: auto    # auto | always | never
```
用 `feedparser`。若 feed 的 `content:encoded` 长度足够（可配阈值，
默认 500 字），直接当正文用，**省掉一次页面请求**。
这是最省资源的路径，接入新站点时应先探测有无 RSS。

### 2.2 `sitemap`
```yaml
discovery_mode: sitemap
config:
  sitemap_url: https://example.com/sitemap.xml
  url_pattern: '^https://example\.com/posts/\d+'
  lastmod_within_days: 7      # 只看近期更新，避免全量遍历
```
适合无 RSS 但有规范 sitemap 的站点。支持 sitemap index 嵌套。

### 2.3 `html_list` —— 通用兜底
```yaml
discovery_mode: html_list
config:
  list_urls:
    - https://example.com/blog
  item_selector: 'article.post h2 > a'    # CSS，取 href
  # 只接受匹配的 URL，跨域/异构条目直接丢弃
  url_allow_pattern: '^https://example\.com/(posts|info)/'
  # 可选分页
  pagination:
    mode: query_param        # query_param | next_link | url_template
    param: page
    order: asc               # asc | desc_index —— 见下方说明
    max_pages: 3
  # 可选：列表页即可拿到的字段，减少详情页依赖
  fields:
    title: 'h2 > a@title'
    published_at: 'time@datetime'
```
用 `selectolax`（比 BeautifulSoup 快一个数量级，内存也低）。
选择器语法支持 `selector@attr` 取属性，不写 `@attr` 则取文本。

**`url_allow_pattern` 不是可选的锦上添花。** 实测江苏大学教务处列表页混有
指向学校主站 `www.ujs.edu.cn` 的条目——那是结构完全不同的另一个站点，
抓下来必然提取失败并污染 `FAILED` 队列。没有这个过滤，多站点抽象在
第一个真实站点上就会破功。跨域条目若确实需要，应作为独立站点配置。

**`pagination.order` 用于表达倒序分页。** 部分 CMS（实测江苏大学教务处）
页号越小内容越旧，入口页才是最新：

| 页面 | 首条日期 |
|---|---|
| `index/tzgg.htm`（入口） | 2026-08-24（最新） |
| `index/tzgg/77.htm` | 2026-07-08 |
| `index/tzgg/1.htm`（尾页） | 2022-08-26（最旧） |

`desc_index` 语义：入口页为最新，页号从 `max_page` 递减到 1 为由新到旧。
增量抓取只请求入口页即可；历史回溯才需递减遍历。若把分页方向硬编码为
正序，增量抓取会一直在抓 2022 年的旧文章，且永远发现不了新文章。

### 2.4 `json_api`
```yaml
discovery_mode: json_api
config:
  endpoint: https://example.com/api/posts?page={page}
  items_path: 'data.items'        # 点号路径
  field_map:
    url: 'permalink'
    title: 'title'
    published_at: 'created_at'
    content: 'body_html'          # 若 API 直接返回全文
  max_pages: 3
```

## 3. 提取层

对每个 `FETCHED` 的页面，按顺序：

1. **站点级选择器覆盖**（若配置了 `extract.selectors`）——精确但需维护
2. **trafilatura**——通用，负责去导航/广告/推荐位，同时抽 title/author/date
3. 若 trafilatura 返回正文长度 < `min_content_length`（默认 200 字），
   标记 `FAILED`、`attempts += 1`，作为确定性选择器问题停止自动重试；Web 端提示修正配置。
   保存站点配置或执行 `nestra site sync` 会把该站点失败文章重置为 `DISCOVERED`

这个顺序（选择器优先、trafilatura 兜底）在实测中得到确认：江苏大学教务处的
`div.v_news_content` 全页仅出现一次，是可靠唯一锚点，精度高于通用算法。
通用算法的价值在于**接入未知站点时零配置可用**，而非在已知站点上更准。

发布时间可用 HTTP `Last-Modified` 头交叉校验或降级兜底（实测该站点响应头
`Tue, 21 Jul 2026 09:36:08 GMT` 与页面标注 `2026-07-21` 吻合）。

```yaml
extract:
  min_content_length: 200
  selectors:                 # 可选，覆盖 trafilatura
    content: 'div.article-body'
    title: 'h1.title'
    author: 'span.author'
    published_at: 'time@datetime'
  strip_selectors:           # 正文内需剔除的元素
    - 'div.related-posts'
    - '.ad-slot'
```

输出字段写入 `articles`：`title` / `author` / `published_at` /
`content_text` / `content_html` / `lang` / `word_count` / `simhash`。

`content_html` 需过一遍 HTML 清洗（`nh3` 或 `bleach`，允许标签白名单），
因为它会在 Web 端渲染，**未清洗的第三方 HTML 直接渲染等于 XSS 入口**。

## 4. JS 渲染

默认关闭。按站点开启：

```yaml
render_js: true
render:
  wait_until: networkidle       # load | domcontentloaded | networkidle
  wait_selector: 'article'      # 更可靠：等特定元素出现
  timeout_ms: 15000
```

在 2C2G 上的硬性要求：

- 全局 Playwright 并发 **上限 1**
- 浏览器实例用完即关，不常驻（牺牲启动耗时换内存）
- 镜像里 Playwright 作为可选 build arg，默认不装（见 [07-deployment.md](07-deployment.md)）
- 若同时启用本地 ONNX 兜底模型，需实测内存，可能需要降到 1.5G 以下才安全

**接入新站点时的判定方法**：先用 `render_js: false` 抓一次，
如果 trafilatura 抽出的正文长度异常小，再开 JS 渲染。
`scripts/probe_site.py` 已把这个探测过程自动化，
输出清单见 [10-site-probe-ujs-jwc.md](10-site-probe-ujs-jwc.md)——
那份报告是手工执行本探测流程的产物，可直接作为脚本的输出规格：
有无 RSS/sitemap/robots、编码、是否需要 JS、缓存头支持、
列表选择器候选、分页方向、URL 双形态、附件链接形态。

实测首个站点**不需要 JS 渲染**，正文在初始 HTML 中。结合你的要求，
Playwright 与本地 ONNX 模型均为**默认关闭、需手动配置**方可启用。

## 5. 附件处理

### 5.1 识别：不能依赖 URL 扩展名

实测教训。江苏大学教务处的附件链接形态：

```html
<a href="/system/_content/download.jsp?urltype=news.DownloadAttachUrl&owner=2025460573&wbfileid=4FF075...">
  附件1 2026-2027-1学期本科生开放实验项目申报表.docx
</a>
```

**URL 里没有任何扩展名，文件名只在锚文本里。** 用后缀白名单抽样 6 篇文章，
匹配结果全部为 0，足以误判成「本站无附件」——而实际上附件是该站点
通知类文章的核心价值，漏抓等于功能失效。

因此配置为正则列表而非固定后缀集合：

```yaml
attachments:
  enabled: true
  link_patterns:                    # 按序匹配，命中即判定为附件
    - 'download\.jsp|DownloadAttachUrl'   # 站群系统下载入口
    - '\.(pdf|docx?|xlsx?|pptx?|zip|rar)(\?|$)'   # 通用后缀兜底
  anchor_text_patterns:             # 补充信号（正文容器内）
    - '^\s*附件\d*'
  inline_image_patterns:            # 匹配则归类为内联图片，不作为附件推送
    - '/__local/'
  allow_mime:
    - application/pdf
    - application/vnd.openxmlformats-officedocument.*
    - application/zip
  max_size_mb: 20
  max_per_article: 10
  total_quota_gb: 5           # 超过则拒绝新下载并告警
  send_referer: true          # 默认带文章页 URL，规避防盗链
```

`inline_image_patterns` 的作用是区分装饰性图片与真实附件。实测该站点
`/__local/` 路径下是页面 banner 图，若当附件推送，用户会收到一堆无意义图片。

### 5.2 文件名与类型只能从响应头取

实测下载响应：

```
content-type: application/octet-stream
content-length: 23883
content-disposition: attachment;
  filename=%E9%99%84%E4%BB%B61%20....docx;
  filename*=utf-8''%E9%99%84%E4%BB%B61%20....docx;
```

- `Content-Type` 为 `application/octet-stream`，**零信息量**，不可用于类型判断
- 真实文件名在 `Content-Disposition`，且是 **percent-encoded 中文**，需解码
- 同时存在 `filename` 与 `filename*`（RFC 5987），**优先 `filename*`**（带明确编码声明），回退 `filename`，再回退锚文本
- 实际类型必须**下载后魔数嗅探**。实测 23883 字节嗅探为 `Microsoft Word 2007+`，与文件名一致
- **MIME 白名单校验须基于嗅探结果**，不能基于服务器声明的 `Content-Type`——否则 `octet-stream` 会让整个白名单形同虚设

实测无 Referer 亦可下载（返回 200），但 `send_referer` 默认开启更接近真实
浏览器行为，对其他站点更安全。

### 5.3 下载规则

- HTTP 响应按块读取并逐块计数，超限立即中断；为保持实现简单，单个附件在写盘前会缓存在内存中
- 默认单文件上限 20 MiB，配置硬上限 100 MiB；先看 `Content-Length`，实际读取仍计数
- 文件名清洗：去路径分隔符、控制字符、限长；最终落盘名用
  `sha256[:2]/sha256[2:4]/sha256` 分片，原始名只存库
- 下载并计算 `sha256` 后，如内容文件已存在则复用落盘文件

拒绝下载的情况记 `status='skipped'` + `skip_reason`，不算失败。

### 5.4 附件下载不阻塞主流程

抽样约 12 篇仅 1 篇有附件——附件是少数情况。设计为文章入库后的**独立异步
任务**：失败可单独重试，不影响文章的打标与推送。已下载项使用文件或签名链接；
未完成/失败项退回原站附件 URL，不生成无效的本地签名链接，也不阻塞正文。

## 6. 礼貌抓取与限流

```yaml
politeness:
  respect_robots: true
  user_agent: 'Nestra/1.0 (+https://your-host/)'
  delay_sec: 2                # 同站点请求间隔
  max_concurrency: 4          # 同站点并发
  timeout_sec: 20
  retry:
    max_attempts: 3
    backoff_base_sec: 5       # 5s, 25s, 125s
    retry_on: [429, 500, 502, 503, 504, timeout, connreset]
```

- `robots.txt` 缓存 24h
- **`robots.txt` 不存在（404）时按「无限制」处理，但不放松自律限速**。
  实测江苏大学教务处无 `robots.txt`；这类单位站点更应保守，建议
  `max_concurrency: 2` / `delay_sec: 2`
- 429 响应优先遵循 `Retry-After`
- 条件请求：保存 `ETag` / `Last-Modified`，下次带 `If-None-Match` /
  `If-Modified-Since`，304 直接跳过——对列表页尤其省流量。
  实测目标站点同时提供 `ETag` 与 `Last-Modified`，日常轮询几乎零成本，
  这是首站点应当默认开启条件请求的实证依据
- 只重试瞬时错误；4xx（除 429）不重试
- 历史回溯（如 78 页全量）应串行 + 间隔慢跑，避免触发异常流量判定

## 7. 已实现文件

| 文件 | 职责 |
|---|---|
| `crawler/fetcher.py` | httpx 客户端封装：限速、重试、条件请求、robots |
| `crawler/discovery/rss.py` | feedparser 适配 |
| `crawler/discovery/sitemap.py` | sitemap（含 index 嵌套） |
| `crawler/discovery/html_list.py` | selectolax 选择器 + 分页 |
| `crawler/discovery/json_api.py` | JSON 路径映射 |
| `crawler/discovery/base.py` | `Discoverer` 协议 + 注册表 |
| `crawler/renderer.py` | Playwright 封装（并发 1、用完即关） |
| `extractor/article.py` | trafilatura + 选择器覆盖 |
| `extractor/sanitize.py` | HTML 白名单清洗 |
| `crawler/attachments.py` | 附件发现、限额下载、内容寻址去重 |
| `extractor/dedupe.py` | URL 规范化、simhash |
| `crawler/url_canonical.py` | 站点级 URL 规范化规则（双形态归一） |

接口契约（供实现参考，非最终代码）：

```
class Discoverer(Protocol):
    async def discover(self, site: Site) -> list[DiscoveredItem]: ...

@dataclass
class DiscoveredItem:
    url: str
    title: str | None = None
    published_at: datetime | None = None
    content_html: str | None = None      # feed/API 直接给全文时填充
    attachments: list[str] = field(default_factory=list)
```

## 8. URL 规范化：同一文章多形态

江苏大学教务处的早期样本中，同一篇文章有两个可访问地址（标题一致）：

```
https://jwc.ujs.edu.cn/info/1331/30031.htm
https://jwc.ujs.edu.cn/content.jsp?urltype=news.NewsContentUrl&wbtreeid=1331&wbnewsid=30031
```

对应关系 `info/{wbtreeid}/{wbnewsid}.htm` ≡ `content.jsp?...`，且**同一批列表页
内两种形态混用**。部分新记录的 `content.jsp` 也可能只是访问提示页，所以规范 URL
404 时需回退原 URL，并由拒绝规则跳过提示页。若仅按原始 URL 哈希去重，文章仍可能入库两次，
**用户收到两条重复推送**——这是功能性缺陷，不是体验问题。

站点配置支持规范化规则：

```yaml
url_canonical:
  rules:
    - match: 'content\.jsp'
      extract_params: [wbtreeid, wbnewsid]
      rewrite: '/info/{wbtreeid}/{wbnewsid}.htm'
  strip_params: [urltype]      # 无信息量的查询参数
```

流程：原始 URL → 应用站点规则 → 通用规范化（去 fragment、排序查询参数、
统一末尾斜杠）→ 计算 `url_hash`。

**正文 simhash 是第二道防线，不可省略。** 规范化规则只能处理已知形态；
未知形态、跨栏目重发、同文不同链都要靠内容指纹兜住。两道都要有。
