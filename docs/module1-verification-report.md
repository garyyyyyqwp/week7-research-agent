# 模块 1 验证报告：真实联网数据接入

## 验证日期
2026-07-08

## 1. Tavily 搜索 vs Mock 对比

### 测试查询

| 序号 | 类型 | 查询 |
|------|------|------|
| 1 | 中文行业 | 新能源汽车2025年发展趋势 |
| 2 | 英文学术 | transformer architecture attention mechanism latest research |
| 3 | 时事 | 2026世界杯出线形势 |

### 对比结果

| 维度 | Tavily 真实搜索 | Week 5 Mock |
|------|----------------|-------------|
| 结果数量 | 5 条/查询 | 1 条/查询 |
| 结果时效性 | 2024-2026 年 | 固定数据 |
| URL 真实性 | 真实可访问 | example.com |
| 中文支持 | 良好 | 关键词匹配 |
| 响应时间 | ~4-5s | <1ms |
| 结构化程度 | title/url/content/score | title/url/snippet |

### 关键发现

1. Tavily 对所有 3 类查询都返回了高质量结果。中文学术 PDF、彭博行业报告、百度百科、维基百科等来源覆盖全面。
2. Mock 只能匹配预设关键词，一旦查询不在 mock_db 中就返回泛化占位文本。
3. 响应时间 ~4-5 秒（含网络往返），对于研报场景可接受——后续通过并行搜索可进一步降低总耗时。

## 2. 正文提取：Jina Reader vs BeautifulSoup

### 测试 URL

| URL | Jina | bs4 |
|-----|------|-----|
| 中文维基（深度学习） | ✅ 57k chars Markdown | ❌ 403 Forbidden |
| arXiv 1706.03762 | ✅ 2.5k chars Markdown | ✅ 4.5k chars text |
| 英文维基（AI） | ✅ 191k chars Markdown | ❌ 403 Forbidden |

### 关键发现

1. **Jina Reader 是首选方案**：返回干净 Markdown，速度快（1.6-2.7s），零配置。
2. **bs4 受反爬限制**：Wikipedia 返回 403，需要配置 User-Agent / Cookie / 代理才能绕过，不适合作为通用方案。
3. **推荐架构**：Jina 优先 → bs4 兜底（对有反爬的站点作用有限）→ 对于已知有 API 的站点直接走 API。

## 3. 定向站点抓取 vs 通用搜索

### 课题：COVID-19 long term effects

| 来源 | 策略 | 耗时 | 结果质量 |
|------|------|------|----------|
| PubMed | Entrez API | 2.6s | ⭐⭐⭐⭐⭐ 结构化论文摘要，PMID+日期完整 |
| WHO | Jina 提取 | 9.1s | ⭐⭐⭐ 搜索页 Markdown，需后处理解析 |
| Tavily 通用 | Tavily API | 3.4s | ⭐⭐⭐⭐ 来源广泛（PMC、Mayo Clinic、医院），但需人工筛选权威性 |

### 关键发现

1. **PubMed Entrez API 是数据质量最高的方案**，直接返回论文标题+摘要+PMID+日期，完全结构化，适合学术研报。
2. **WHO 站点用 Jina 提取可行但体验不如 API**：需要二次解析 Markdown，建议后续为 WHO 等站点编写专用解析器。
3. **通用搜索作为补充发现渠道**：覆盖范围广但不能区分权威性。

### 技术决策建议

| 场景 | 推荐策略 |
|------|----------|
| 发表学术研报 | 优先 PubMed / Semantic Scholar，API 直连 |
| 官方指南/政策报告 | 定向站点（WHO/CDC）+ Jina 提取 |
| 行业新闻/趋势分析 | Tavily 通用搜索 |
| 代码/技术文档 | GitHub 定向 + Jina 提取 |
