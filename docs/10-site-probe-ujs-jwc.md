# 站点探测报告：江苏大学教务处

- 目标：`https://jwc.ujs.edu.cn/`
- 探测时间：2026-08（实测，非推断）
- 结论用途：作为**首个适配站点**的配置依据，同时反推抽象层设计

---

## 1. 关键结论速览

| 项 | 结论 | 对设计的影响 |
|---|---|---|
| RSS / Atom | **不存在**（`/rss.xml` `/feed` 均 404） | 必须实现 `html_list` 发现模式，不能只依赖 RSS |
| `robots.txt` | **404**（不存在） | 无声明性抓取约束，仍应主动限速自律 |
| `sitemap.xml` | **404** | `sitemap` 模式对本站不可用 |
| JS 渲染 | **不需要**，正文在初始 HTML 中 | Playwright 默认关闭即可满足需求 |
| 编码 | UTF-8（`<meta charset="UTF-8">`） | 无需 GBK 转码，但抽象层仍需保留编码兜底 |
| 缓存头 | 提供 `Last-Modified` + `ETag` | **可做条件请求**，显著降低带宽与被封风险 |
| CMS | 学校常见的「西湖/正方」系 `content.jsp` 站群系统 | 同类高校站点大概率可复用同一套选择器 |
| 附件 | 存在，但**URL 无扩展名**，需特殊处理 | 见 §4，这是本次探测最重要的发现 |

---

## 2. 内容发现

### 2.1 无 RSS，走列表页

栏目列表页结构高度规整，实测 `通知公告` / `学生选课` / `学籍` / `专业建设` 四个栏目**完全同构**，每页均为 15 条。

列表项形态：

```html
<li id="line_u8_0">
  <a href="../content.jsp?urltype=news.NewsContentUrl&wbtreeid=1291&wbnewsid=30301"
     target="_blank" title="2026年拟增设专业情况公示" class="title tt1">2026年拟增设专业情况公示</a>
  <p class="date">2026-08-24</p>
</li>
```

选择器方案：

| 字段 | 选择器 |
|---|---|
| 列表项 | `li[id^="line_"]` |
| 链接 | `a.title.tt1@href` |
| 标题 | `a.title.tt1@title`（`title` 属性是完整标题，文本节点可能被 CSS 截断，**优先取属性**） |
| 日期 | `p.date` → `%Y-%m-%d` |

> 标题取 `@title` 而非文本节点，是因为列表文本存在视觉截断风险；属性值是完整的。

### 2.2 分页：倒序，需特殊处理

实测分页结构：

```html
<span class="p_t">共1162条</span>
<span class="p_pages">
  <span class="p_no_d">1</span>
  <span class="p_no"><a href="tzgg/77.htm">2</a></span>
  ...
  <span class="p_no"><a href="tzgg/1.htm">78</a></span>
  <span class="p_last p_fun"><a href="tzgg/1.htm">尾页</a></span>
</span>
```

**页码是倒序的**，实测确认：

- `index/tzgg.htm`（第 1 页）首条 = `2026-08-24`（最新）
- `index/tzgg/77.htm`（第 2 页）首条 = `2026-07-08`
- `index/tzgg/1.htm`（第 78 页 / 尾页）首条 = `2022-08-26`（最旧）

即：**页号越小，内容越旧**；入口页 `tzgg.htm` 才是最新。

设计要求：

- **增量抓取**只需请求入口页 `index/tzgg.htm`，不翻页。日增不足 100 条，首页 15 条足以覆盖。
- **历史回溯**（标签集生成用）需从 `77` 递减到 `1` 遍历，URL 模板 `index/tzgg/{page}.htm`。
- 总量约 1162 条 / 78 页，一次性全量回溯是可行的，也正好是标签集生成的语料来源。
- 分页方向必须做成配置项 `pagination.order: desc_index`，不能硬编码。其他 CMS 多为正序，抽象层要能表达两种。

### 2.3 URL 双形态：同一篇文章两个地址

早期样本确认以下两个 URL 返回**同一篇文章**（标题一致）：

```
https://jwc.ujs.edu.cn/info/1331/30031.htm
https://jwc.ujs.edu.cn/content.jsp?urltype=news.NewsContentUrl&wbtreeid=1331&wbnewsid=30031
```

对应关系：`info/{wbtreeid}/{wbnewsid}.htm` ≡ `content.jsp?...&wbtreeid={}&wbnewsid={}`

且同一列表页内**两种形态混用**（列表用 `content.jsp`，首页部分区块用 `info/`）。
后续在线复测还发现部分新 `content.jsp` 只返回“系统提示/校内访问”短页，而对应
`info/` 地址可能 404。因此实现会先试规范 URL、404 时回退原 URL，并用
`reject_title_patterns` / `reject_content_patterns` 将访问提示标为 `SKIPPED`，
不能假设每个参数组合都可互换。

> **这会直接导致重复推送**：同一篇文章两种 URL 各算一次，URL 哈希去重失效，用户收到两条通知。

去重规范化规则（写入站点配置）：

1. 若 URL 匹配 `content.jsp`，提取 `wbtreeid` + `wbnewsid`，改写为规范形式 `info/{wbtreeid}/{wbnewsid}.htm`
2. 剥离 `urltype` 等无信息查询参数
3. 以规范化后的 URL 计算 `url_hash`

补充：正文 simhash 作为第二道防线（应对无法归一的形态）。两道都要有。

### 2.4 站外链接必须过滤

列表中混有指向学校主站 `www.ujs.edu.cn` 的条目（实测通知公告页 2 条）。这些是**不同站点的文章**，结构不同，提取会失败。

要求：`html_list` 发现模式必须支持 `url_allow_pattern`，本站配置为仅接受 `jwc.ujs.edu.cn` 域下且路径匹配 `info/` 或 `content.jsp` 的链接。跨域条目直接丢弃，不进队列。

---

## 3. 正文提取

文章页结构干净，实测 `info/1331/30031.htm`：

| 字段 | 选择器 | 实测值 |
|---|---|---|
| 标题 | `h1.title` | `2026-2027-1学期选课停开课程（总）` |
| 正文 | `div.v_news_content` | 提取到 3145 字符纯文本 |
| 发布时间 | 页面含 `发布时间：2026-07-21` | 需正则 `发布时间：\s*([\d-]+)` |

`div.v_news_content` 在整页中**仅出现 1 次**，是可靠的唯一锚点。

关于 trafilatura：本站结构规整，`v_news_content` 选择器精度高于通用算法。设计上应**优先 per-site 选择器、trafilatura 兜底**（而非反过来）——这与 docs/03 原定顺序一致，此处实测确认该顺序正确。

发布时间还可用 HTTP `Last-Modified` 头交叉校验（实测 `Tue, 21 Jul 2026 09:36:08 GMT`，与页面 `2026-07-21` 吻合）。页面解析失败时可降级取该头。

---

## 4. 附件：本次探测最重要的发现

### 4.1 URL 不含扩展名，无法靠后缀识别

初次抽样 6 篇文章，用 `href` 后缀匹配 `.doc|.docx|.xls|.pdf` **全部为 0**，一度误判为「本站无附件」。

实际形态：

```html
<a href="/system/_content/download.jsp?urltype=news.DownloadAttachUrl&owner=2025460573&wbfileid=4FF07519D4C6C7D19F824EF01E409835">
  附件1 2026-2027-1学期本科生开放实验项目申报表.docx
</a>
```

**URL 里没有任何扩展名**，文件名只出现在**锚文本**里。

> 教训：附件识别**不能依赖 URL 后缀**。这是通用抽象必须吸收的一条——扩展名匹配对相当多 CMS 无效。

识别规则（按优先级）：

1. `href` 匹配 `download.jsp` 或含 `DownloadAttachUrl` → 判定为附件（本站主路径）
2. `href` 后缀匹配办公文档扩展名 → 判定为附件（通用兜底）
3. `href` 位于正文容器内且锚文本匹配 `附件\d*` → 补充信号

配置项设计为 `attachment.link_patterns`（正则列表），而非固定后缀集合。

### 4.2 文件名与类型只能从响应头取

实测 `HEAD` 响应：

```
HTTP/2 200
content-type: application/octet-stream
content-length: 23883
content-disposition: attachment;
  filename=%E9%99%84%E4%BB%B61%20....docx;
  filename*=utf-8''%E9%99%84%E4%BB%B61%20....docx;
```

处理要点：

- `Content-Type` 是 `application/octet-stream`（无信息量），**不能用它判断类型**
- 真实文件名在 `Content-Disposition` 中，且是 **URL-encoded 的中文**，需 percent-decode
- 同时提供 `filename` 与 `filename*`（RFC 5987），**应优先 `filename*`**（明确带 utf-8 编码声明），回退到 `filename`
- 实际类型需下载后嗅探。实测下载 23883 字节，`file` 识别为 `Microsoft Word 2007+`，与文件名 `.docx` 一致
- MIME 白名单校验应基于**魔数嗅探结果**，而非服务器声明的 `Content-Type`，否则白名单形同虚设

### 4.3 无 Referer 防盗链

实测不带 `Referer` 直接请求下载链接，同样返回 `200`。**无需伪造 Referer**。

但设计上仍应保留 `send_referer` 配置项（默认开启，带上文章页 URL）——其他站点常有防盗链，且带 Referer 更接近真实浏览器行为，风险更低。

### 4.4 附件密度

抽样约 12 篇，仅 1 篇（`info/1221/30111.htm`，含 3 个 `.docx`/`.xls`）有附件。

推论：附件是**少数情况**，附件下载不应阻塞主流程。设计为文章入库后的**独立异步任务**，失败可单独重试，不影响文章本身的打标与推送。

补充：正文内 `__local/` 路径是**图片**（实测首页 banner 为 `.jpg`），不是文档附件。需与 `download.jsp` 附件区分——`__local` 默认归类为内联图片，不作为「附件」推送，避免把装饰性图片推给用户。

---

## 5. 抓取礼貌性与稳定性

- 无 `robots.txt`，但这是学校单位站点，**必须自律**：建议 `concurrency: 2`、`delay: 2s`
- 站点提供 `ETag` / `Last-Modified` → 列表页轮询**必须走条件请求**（`If-None-Match` / `If-Modified-Since`），304 直接跳过解析。日常轮询几乎零成本
- 响应头 `Server` 为空，`X-Content-Type-Options: nosniff`，无明显 WAF 特征
- 历史回溯 78 页需限速慢跑（建议串行 + 2s 间隔，约 3 分钟完成），避免被判定为异常流量

---

## 6. 反哺通用抽象的三条修正

本次探测暴露了原设计文档中三处过于乐观的假设，已同步修正到对应文档：

1. **附件识别不能依赖 URL 扩展名** → `attachment.link_patterns` 正则化（docs/03）
2. **分页可能是倒序的** → `pagination.order` 必须可配（docs/03）
3. **同一文章可能有多种 URL 形态** → 规范化规则需可配，且 simhash 兜底不可省（docs/02、docs/03）

第 1、3 条若未在探测阶段发现，会直接导致功能性缺陷（附件全部漏抓、重复推送），而非仅仅是不优雅。

---

## 7. 待确认

- 目前仅适配「通知公告」栏目。是否需要同时抓取「最新动态」等其他栏目？（栏目同构，配置上只是多一个 entry）
- 站外条目（指向 `www.ujs.edu.cn`）是否需要跟进抓取？当前设计是丢弃。若需要，应作为**独立站点**配置，而非在本站配置里特殊处理。
