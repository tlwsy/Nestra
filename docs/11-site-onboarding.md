# 11 · 站点接入向导：自动探测 + 可视化补全

目标：**输入一个 URL，走完向导，站点开始抓取。** 能自动测的自动测，
测不出的用可视化界面点选，业务语义的问用户。

## 0. 设计原则

**不追求全自动，优先做到「常见站点零手写 YAML」。** 这两者差别很大：

- 全自动 = 机器猜完就用。在未知站点上必然有猜错的项，错了用户还不知道
- 常见路径 = 机器出候选，人一眼确认；候选失败时仍保留带说明的选择器字段和 YAML 兜底

关键设计是**试运行预览**：所有自动检测结果先跑一次真实抓取，把 10 篇文章的
标题/日期/正文摘要/附件列表摆出来给用户看。人眼判断「对不对」比任何自动校验
都可靠，且不需要用户理解配置项的含义。

诚实的能力边界：下述算法在**标准 CMS 站群**（高校/政府/企业官网、WordPress、
Discourse 等，中文站点的大多数）上成功率高；在 SPA、瀑布流、需登录的站点上
会失败。因此手动兜底路径（§5）是必需组件，不是锦上添花。

## 1. 配置项自动化分级

按能否自动确定分三级。这个表是向导 UI 的设计依据。

### A 级 · 自动探测（仍在预览中确认）

| 配置项 | 当前探测方法 |
|---|---|
| URL/重定向安全 | 每跳解析并 pin 公网 IP，校验实际连接地址 |
| RSS/Atom | HTML 声明与少量同源常见路径候选，抓取后识别 feed |
| sitemap | `robots.txt` 的 `Sitemap:` 与同源常见路径候选；嵌套展开由正式 crawler 完成 |
| 字符编码 | BOM → HTTP/meta charset → UTF-8，GB2312/GBK 统一按 GB18030 解码 |
| `render_js` | 只根据静态 HTML 的正文/链接结构给出低成本启发式建议，必须由 dry-run 确认 |
| `attachments.link_patterns` | 从有界文章样本的链接 URL/锚文本归纳候选，不在 probe 阶段下载附件 |
| `pagination.order` | 能归纳页码模板时抓相邻页，比较日期中位数；证据不足就留给用户编辑 |

探测本身不启动 Playwright，也不测试条件请求；正式 crawler 分别负责可选渲染、
条件请求和附件 MIME 魔数校验。这样 probe 保持有界且不会为了猜配置下载大量资源。

### B 级 · 生成候选，用户确认（机器排序，人点选）

| 配置项 | 候选来源 |
|---|---|
| `list_urls` | 从首页 BFS 两层，按「列表页特征」评分排序（§4.1） |
| `item_selector` | 重复结构归纳（§4.2），输出 top 3 候选 + 每个候选抓到的条目数与样例 |
| `fields.title` | 随 `item_selector` 一起归纳，含 `@title` 属性检测 |
| `fields.published_at` | 条目块内含日期文本的节点 |
| `extract.selectors.content` | picker 提供可编辑字段；未可靠推断时由正式 extractor fallback + dry-run 验证 |
| `url_allow_pattern` | 高级候选 JSON/YAML 中人工补充 |
| `url_canonical.rules` | 对携带相同数字身份且样本正文一致的双形态 URL 给出证据，规则仍人工确认 |
| `max_page` | 仅使用页面中可见页码证据；取不到时保留有界默认值供编辑 |

B 级项的 UI 呈现是**带样例的单选列表**，不是空白输入框。用户看到的是
「候选 1：抓到 15 条，样例：关于2026-2027学年…」，而不是 `li[id^="line_"] a.title.tt1`。
选择器字符串折叠在「高级」里可编辑。

当前不把第三方页面发送给 LLM。确定性候选不足时直接使用选择器编辑器或 YAML；
只有实测证明这两条路径仍不足时再增加 LLM 辅助，避免额外成本和数据外发。

### C 级 · 必须人工（业务语义，机器无从知晓）

| 配置项 | 为什么不能自动 |
|---|---|
| 抓哪些栏目 | 站点有「通知公告」「最新动态」「教学研究」多个列表页，要哪些只有你知道 |
| `tagset_group` | 新站点与已有站点是否同一主题，是业务判断。见 [04-tagger.md](04-tagger.md) §1.6 |
| `crawl_interval_sec` | 取决于你对时效的要求，默认 1800 |
| 跨域条目的处置 | 列表页混入外站链接时，是丢弃还是作为独立站点接入 |
| `attachments.enabled` | 是否需要附件推送 |
| `politeness` 收紧 | 机器只能给保守默认值（`delay_sec: 2` / `max_concurrency: 2`） |

## 2. 探测的安全边界

**用户输入任意 URL 让服务端去请求，这是标准 SSRF 入口。** 必须在探测前拦：

- 域名解析后校验 IP，拒绝 `127.0.0.0/8`、`10/8`、`172.16/12`、`192.168/16`、
  `169.254/16`、`::1`、`fc00::/7`、`0.0.0.0`
- **解析结果与实际连接必须是同一个 IP**（pin 已解析的 IP 再发起连接），
  否则 DNS rebinding 可绕过校验
- 跟随重定向时**每一跳都重新校验**，不能只校验首个 URL
- 只允许 `http` / `https` scheme
- 探测入口限 admin，且限流（`POST /admin/sites/probe`：单用户 3 次/min）

资源上限（2C2G 环境的硬约束）：

```yaml
onboarding:
  probe:
    max_pages: 40             # 全程最多请求页数
    max_duration_sec: 120     # 超时即中止，返回已得部分结果
    max_bytes_per_page: 3145728
    sample_articles: 6        # 用于提取/附件探测的样本数
    delay_sec: 1              # 探测自身也要限速
```

探测是后台任务，Web 端轮询进度。不能同步阻塞请求——一个大站点的探测会跑一两分钟。

## 3. 向导五阶段

```
① 输入 URL
   ↓  自动探测（后台任务，进度条）
② 确认发现方式        ← 有 RSS 就推荐 RSS，A 级结论直接展示
   ↓
③ 确认列表页与条目    ← B 级候选点选 + 选择器编辑器兜底
   ↓
④ 试运行预览          ← 抓 10 篇真实数据，人眼验收
   ↓
⑤ 归组并确认保存      ← C 级业务语义，选 tagset_group
   ↓
   写入 sites 表（保持 disabled）；管理员复核后再显式启用
```

**阶段二**：若探测到 RSS 且 feed 含全文，直接推荐 `discovery_mode: rss` 并跳过
阶段三——RSS 路径不需要选择器，这是最省事也最省资源的情况。有 sitemap 则次之。
两者皆无才进入 `html_list` 的选择器流程。

**阶段四是整个向导的质量闸门。** 展示表格：

| # | 标题 | 发布时间 | 正文长度 | 正文摘要 | 附件 |
|---|---|---|---|---|---|
| 1 | 关于2026-2027学年… | 2026-08-24 | 1247 | 各学院、部门… | 1 个（申报表.docx, 23KB） |

用户看到日期全是 `null`、或标题被截断成「关于2026-2027学年...」、或正文长度
只有 30，立刻就知道哪一项配错了，可以退回上一阶段改。这比让用户读 YAML 判断
配置对不对现实得多。

预览用**独立的临时抓取**，结果不入库。用户点「确认」才落库。

**阶段五**：选 `tagset_group`，也可先在标签集页面新建组。组尚未冻结时，
该站点文章会先存档但不推送；达到该组 `min_docs_for_build` 后再构建标签集。

## 4. 关键算法

### 4.1 列表页候选发现

从 `base_url` BFS 两层，对每个页面评分：

| 信号 | 权重 |
|---|---|
| 重复结构块数 ≥ 5（相同 DOM 路径签名的兄弟节点） | 高 |
| 块内含日期文本的比例 > 0.6 | 高 |
| 块内链接同域且路径深度大于当前页 | 中 |
| 页面标题/URL 含 `通知\|公告\|新闻\|列表\|blog\|posts\|news\|archive` | 中 |
| 无日期的高链接密度页 | 负（纯导航页） |

输出 top 8 候选，每个附「抓到 N 条，首条标题」。用户多选。

### 4.2 `item_selector` 归纳

经典 wrapper induction，步骤：

1. 对页面所有 `<a>` 计算 DOM 路径签名：`tag.class` 序列，剥掉 `nth-child`
2. 按签名分组，保留组内数量 ≥ 5 的组
3. 每组打分：href 同域比例、锚文本长度中位数落在 8–80 字、是否有兄弟日期节点
4. 为胜出组生成**最短唯一选择器**

实现会优先生成稳定的标签/class 选择器，不依赖 `nth-child`。对于动态 ID 前缀等
无法可靠归纳的结构，由 picker 中的选择器编辑器补全。

**`@title` 属性检测可以自动化**：若锚文本以 `...`/`…` 结尾，
或锚文本长度显著小于 `title` 属性长度，则 `fields.title` 自动改用 `@title`。
不做这一步会拿到一批截断标题，而且从预览里不容易看出来（看起来像是正常标题）。

### 4.3 分页方向自动判定

当页面暴露可归纳的页码 URL 时：

1. 从入口页页码链接归纳 `url_template`
2. 有请求预算时抓相邻页，比较条目日期中位数
3. 入口页更新则建议 `desc_index`，另一页更新则建议 `asc`
4. `max_page` 只取当前 HTML 中明确可见的最大页码

证据不足时不猜二分边界或方向，候选保留保守页数并交给用户编辑。首站 UJS 的
反向分页值来自已验证的站点配置，而不是宣称所有 CMS 都能自动推断。

### 4.4 URL 双形态检测

若列表页出现两种 URL 路径形态：各取一个抓下来，比对 title 与正文 simhash。
相同则从两个 URL 提取公共参数，生成候选 `url_canonical` 规则给用户确认。

映射关系（`info/{wbtreeid}/{wbnewsid}.htm` ≡ `content.jsp?wbtreeid=&wbnewsid=`）
需要参数名对齐，机器能给候选但**不能自动采纳**——搞错会把不同文章归并成一篇，
比重复推送更严重。

## 5. 确认与兜底路径

自动归纳后，picker 展示每个候选的命中数、置信度和文章文本样本；admin 可选择
候选，也可在带标签的选择器表单中修正条目、链接、标题、日期和正文选择器，再直接
发起正式提取器 dry-run。页面只渲染服务端生成且转义的摘要，放在
无 `allow-scripts` 的 `<iframe sandbox>` 中，不加载目标站资源。

选择器不足时可直接编辑候选 JSON；复杂 SPA/认证站点则保留 `nestra site sync` 的
YAML 路径。LLM 辅助不是必需链路：确定性 DOM 归纳更便宜、可复现，也不把第三方
页面发送给模型。若未来实测大量站点只能靠 LLM 再添加，输出仍必须经过命中数和
dry-run 校验。

## 6. 探测报告落盘

Web 任务在 15 分钟内提供结果；CLI 可用 `--output` 显式保存 JSON/YAML 报告。
格式对齐 [10-site-probe-ujs-jwc.md](10-site-probe-ujs-jwc.md) 的结论清单——
那份手工报告就是本流程的输出规格。

`scripts/probe_site.py` 与 Web 向导**共用同一套探测实现**（`onboarding/probe.py`），
CLI 只是另一个前端。这样 CLI 探测出的配置可以直接喂给向导的阶段四预览。

## 7. 已实现文件

| 文件 | 职责 |
|---|---|
| `onboarding/probe.py` | 有界探测编排：feed/sitemap/list/分页/附件与双形态候选 |
| `onboarding/ssrf.py` | URL 校验、IP pin、重定向逐跳校验 |
| `onboarding/analysis.py` / `detect/` | 列表、选择器、分页、双形态、feed 与附件候选 |
| `onboarding/dryrun.py` | 复用正式提取器的试运行预览，结果不入库 |
| `onboarding/emit.py` | 探测报告 → 配置候选 |
| `web/api/admin.py` | admin 向导 API、任务所有权与配置 hash 确认 |
| `web/templates/admin_sites.html` / `picker.html` | 五阶段表单、候选文本样本与沙箱预览 |
| `scripts/probe_site.py` | CLI 前端，复用 `onboarding/probe.py` |

新增路由（admin only）：

```
POST /admin/sites/probe            启动探测任务 → task_id
GET  /admin/sites/probe/{task_id}  轮询进度与结果
POST /admin/sites/dryrun           按候选配置试运行，返回 10 篇预览
POST /admin/sites/confirm          校验 dry-run hash 后落库（默认停用，需 admin 启用）
GET  /admin/sites/picker           候选样本文本拾取 + 沙箱 dry-run 预览
GET  /admin/tagset/groups          分组列表（阶段五用）
POST /admin/tagset/groups          新建分组
```

## 8. 验收标准

- UJS 离线样本能识别 HTML 列表、`@title` 与 `download.jsp` 候选；反向分页等
  站点特例由选择器/候选编辑器或已验证 YAML 补全
- RSS 站点会优先生成 RSS 候选，不要求填写 HTML 选择器
- SSRF 测试覆盖回环、链路本地/元数据地址、重定向到内网和 DNS/实际连接不一致
- picker 可选择候选并编辑条目、链接、标题、日期、正文选择器，随后运行不入库预览
- 确认必须匹配用户自有 dry-run 的配置 hash；创建后默认 disabled，需管理员显式启用
- 确定性候选不足时可编辑候选 JSON，复杂站点可用 `nestra site sync` YAML 路径
- 上述能力不新增第三方依赖（对照 §9）

## 9. 依赖清单：零新增

向导复用已有依赖，不增加安装量：

| 能力 | 用到的包 | 来源 |
|---|---|---|
| 探测请求 | `httpx` | 爬虫已用 |
| DOM 解析、选择器归纳 | `selectolax` | 爬虫已用 |
| 正文预览/清洗 | `trafilatura` / `nh3` | 提取与 Web 已用 |
| RSS 发现与解析 | `feedparser` | 爬虫已用 |
| 向导 UI | FastAPI + Jinja2（服务端表单） | Web 已用 |
| 后台探测任务 | `asyncio` task | 标准库 |

新增的只有 `src/nestra/onboarding/` 下的自写模块、一个 prompt 模板和几个模板页。

归纳算法（§4.2）不引 `scikit-learn`——重复结构检测用 DOM 路径计数就够，
为此装一个几十 MB 的科学计算栈不划算。拾取器在前端用原生 JS 生成选择器，
不引前端构建链。

一个例外需说明：若站点确实需要 JS 渲染，向导只能给出建议，不能自动安装
Playwright；管理员需按 [07-deployment.md](07-deployment.md) 选择 `runtime-render`
或 `runtime-full` 镜像 target，再显式启用该站点的 `render_js`。
