# onboarding/

输入一个 URL，输出一份可用的站点配置。**只做检测与建议，不写配置文件**——
落库由 `web/api/onboarding.py` 在用户确认后执行。

| 计划文件 | 职责 |
|---|---|
| `guard.py` | SSRF 防护：内网地址拒绝、DNS 解析后 IP pin、重定向逐跳校验 |
| `probe.py` | 阶段一探测编排；预算控制（页数/时长/字节）；返回 `ProbeReport` |
| `detect/feed.py` | `<link rel=alternate>` + 常见路径探测 RSS/Atom/sitemap |
| `detect/encoding.py` | HTTP header → meta charset → chardet 三级判定 |
| `detect/render.py` | 静态正文长度 vs `<script>` 占比启发式判断是否需 JS |
| `detect/listpage.py` | 从入口页发现候选列表页并按链接密度/日期密度排序 |
| `detect/induce.py` | 重复结构归纳 `item_selector` / `link_selector` / `date_selector` |
| `detect/pagination.py` | 抓两页比对日期中位数，判定 `order: asc \| desc_index` |
| `detect/dualform.py` | 检测同文多 URL 形态，生成 `url_canonical` 规则 |
| `detect/attachment.py` | 扫描候选链接 + HEAD 验证 `Content-Disposition`，归纳 `link_patterns` |
| `dryrun.py` | 按候选配置试抓 N 篇，产出预览，**不入库** |
| `emit.py` | `ProbeReport` + 用户选择 → 站点配置字典 / YAML 片段 |

## 硬约束

- **所有网络请求必须走 `crawler/fetcher.py`**，不新开 httpx 客户端。
  否则限速、退避、robots 处理会出现两套实现
- **每个 detect 模块返回带置信度的候选列表，不返回单一答案**。
  自动检测会错，UI 需要展示备选让用户切换
- `dryrun.py` 复用 `extractor/`，不自己解析正文
- 检测失败返回空候选 + 原因，不抛异常中断整个探测

参考 docs/11-site-onboarding.md、docs/03-crawler.md
