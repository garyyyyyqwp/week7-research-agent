# Week 6 技术选型说明文档

> 第 7-8 周产品开发的前置输入 — 五个核心模块技术预研结论

## 文档概览

本文档汇总了 Week 6 五个技术模块的验证结论、技术选型决策和踩坑记录，作为 Week 7 产品开发的参考。

---

## 模块 1：真实联网数据接入

### 选型结论

| 组件 | 选择 | 备选 | 理由 |
|------|------|------|------|
| 通用搜索 | **Tavily API** | Jina Reader `s.jina.ai` | 专为 AI Agent 设计，返回结构化结果，支持 `search_depth=advanced`，中文支持优秀 |
| 正文提取 | **Jina Reader** `r.jina.ai` | httpx + BeautifulSoup | 返回干净 Markdown，零配置，对 Wikipedia/arXiv/大多数新闻站点支持极好 |
| 定向站点（有 API） | **直接调 API**（PubMed Entrez、arXiv API、Semantic Scholar） | — | 结果完全结构化，可靠性最高 |
| 定向站点（无 API） | **Jina Reader** 提取搜索页 | — | 省力，无需逐个站点适配 HTML 解析 |

### 关键发现

1. **Tavily 对 3 类查询（中文行业、英文学术、时事）均返回高质量结果**（5 条/查询，~4-5s 响应时间）。Mock 搜索完全无法替代。
2. **Wikipedia 用 httpx+bs4 直接请求会返回 403**（反爬），Jina Reader 却能稳定返回内容。bs4 作为兜底的意义有限，建议直接 Jina。
3. **PubMed Entrez API 是数据质量最高的方案**，PubMed 搜索返回了 10 条高质量 Long COVID 论文摘要 + PMID + 日期。
4. **WHO 站点用 Jina 提取**可行但带宽消耗大（9s 完整页面拉取）。后续可对 WHO 等高频站点编写专用轻量解析器。

### 成本估算

| 服务 | 免费额度 | 月用量预估 | 是否够用 |
|------|----------|-----------|---------|
| Tavily | 1000 次/月 | ~500 次/月（每次研报 5-10 次搜索） | ✅ |
| Jina Reader | 免费，无限制 | ~200 次/月 | ✅ |
| PubMed API | 免费（需遵守 rate limit 0.34s） | ~100 次/月 | ✅ |
| arXiv API | 免费 | ~50 次/月 | ✅ |

### Week 7 建议

- 封装统一的 `SearchProvider` 接口，支持 Tavily/Jina/PubMed 后端切换
- 为高频站点（WHO、CDC）实现专用 API 调用，避免 Jina 带宽浪费
- 考虑加搜索结果缓存（同一查询 1 小时内复用）

---

## 模块 2：并行工具调用

### 选型结论

| 决策 | 选择 | 理由 |
|------|------|------|
| 并行化机制 | **asyncio.gather** | Python 标准库，无需额外依赖，与现有的 async/await 代码无缝集成 |
| 后续替代 | concurrent.futures | 如果需要跨线程，可平滑切换 |

### 性能数据

```
3 路并发搜索（模拟 1.0s/1.2s/0.8s 延迟）:
  串行: 3.03s
  并行: 1.21s
  加速比: 2.5x
  节省: 1.82s
```

### 关键发现

1. **并行化效果取决于 LLM 是否一次返回多个 tool_calls**。glm-4.6v 在一次测试中只返回 1 个 tool_call，需要通过 system prompt 明确引导「同时发起多个搜索」。
2. **需要 URL 去重机制**：glm-4.6v 在 3 步测试中连续 3 次 fetch 同一个 PubMed URL，需要 agent 引擎层做 fetched_urls set 去重。
3. `asyncio.gather(return_exceptions=True)` 确保单个工具失败不影响其他工具的返回。

### Week 7 建议

- 配置 `tool_choice` 参数，引导 LLM 在适当场景下进行多路并发
- 对搜索类工具做结果去重（相似度阈值 0.9），避免重复数据浪费 token

---

## 模块 3：引用追踪 CitationManager

### 选型结论

| 决策 | 选择 | 理由 |
|------|------|------|
| 引用管理 | **自研 CitationManager** | 几个核心类，不需要第三方库。按索引/按 URL 双维度查找，支持 Markdown/JSON 输出 |
| 引用注入 | **LLM 自然生成** | 在 tool result 中告知 LLM 每条结果的引用编号，LLM 在回答中自行插入 [n] |

### 设计要点

```python
class CitationManager:
    def add(url, title, snippet, source_type, site_name) -> int  # 返回编号
    def format_references(style="markdown") -> str                 # 参考文献列表
    def format_inline_refs() -> str                                # inline 引用（供 LLM prompt）
    def to_dict() -> dict                                          # JSON 序列化
```

核心特性：
- **URL 去重**：同一 URL 重复 add 返回已有编号
- **来源分类**：`web` / `academic` / `official` / `code`
- **多格式输出**：Markdown 参考文献列表 + JSON 结构化数据

### Week 7 建议

- 考虑用句子级 NER（spaCy/Jieba）自动识别 LLM 回答中提及了哪些来源，自动插入引用标记
- 参考文献信息应持久化到数据库，支持跨会话查询

---

## 模块 4：研报 Schema + 分章节 SSE 流式生成

### 选型结论

| 决策 | 选择 | 理由 |
|------|------|------|
| 文档模型 | **Pydantic v2** ResearchReport | 类型安全 + 序列化 + Markdown 导出内聚 |
| SSE 事件流 | **sse-starlette** | 与 FastAPI 无缝集成，支持事件类型区分 |
| 前后端渲染 | **marked.js**（前端）| 轻量，即时渲染 Streaming Markdown |
| 大纲生成 | LLM 生成 JSON Schema | 灵活，支持自定义章节数 |

### SSE 事件流设计

```
status → outline → section_start → section_chunk (×N) → section_end
  → (下一节...) → abstract → references → report_complete → done
```

### 实测数据

- 大纲生成：3-6s（需等待完整 JSON 响应）
- 每节生成：15-60s（流式推送，用户实时可见）
- 主题「AI in healthcare」2 个章节：~120s 完整生成

### 关键发现

1. **生成质量不稳定** — 章节生成出了完整的英文段落，但需要 better system prompt 来控制内容质量
2. **上下文累积** — 将之前章节的摘要注入后续章节的 prompt 中，可以提升报告结构的一致性
3. **中文生成速度慢** — glm-4.6v 生成中文内容比英文慢约 2x

### Week 7 建议

- 在 system prompt 中加入**具体的数据/案例要求**，减少生成空洞内容
- 考虑节间内容 dedup（相似度阈值 0.7），防止不同章节间内容重复
- Markdown 关键词渲染（表格、代码块、列表）需要 prompt 中明确引导

---

## 模块 5：划词优化 + 文档导出

### 选型结论

| 组件 | 选择 | 理由 |
|------|------|------|
| 前端选区获取 | **浏览器 Selection API** | 标准 Web API，无需依赖，跨浏览器支持 |
| 文本替换 | **DOM 直接操作** | 支持精确替换，保留上下文结构 |
| Markdown 导出 | **前端 Blob download** | 零服务端依赖，极简实现 |
| PDF 导出 | **window.print()** + 打印样式表 | 零依赖，用户可用浏览器的「另存为 PDF」 |

### 划词优化流程

```
用户选中文字 → Selection API 获取选区+位置
  → 弹出优化面板（选择优化风格）
  → POST /api/v1/report/refine（发送 {selected_text, context_before, context_after, instruction}）
  → LLM 局部改写
  → 前端 DOM 操作替换原文
```

### 关键发现

1. **LLM 对简短选区的优化效果很好**：测试「This is AI.」→「This constitutes Artificial Intelligence.」
2. **上下文信息量不够**时，LLM 可能改变原意或产生风格不匹配。建议至少传递选区前后 200 字的上下文
3. **window.print() PDF 导出**体验一般但够用：用户可以「另存为 PDF」，中文字体依赖系统字体。weasyprint 需要额外配置中文字体
4. **weasyprint 在 Windows 上不需要配置**即可工作（使用系统字体），但测试中未充分验证

### Week 7 建议

- 划词优化加「撤销」功能（保留修改历史）
- 考虑升级到 weasyprint 服务端 PDF 导出，支持中文字体的 docker 化部署
- 增加更多优化模板（学术化、通俗化、简洁化、英文润色等）

---

## 📊 跨模块技术决策总结

| 维度 | 决策 | 替代方案 | 迁移成本 |
|------|------|---------|---------|
| 搜索后端 | Tavily | Jina Reader | 低（接口封装） |
| 正文提取 | Jina Reader | BS4 | 低（strategy 参数切换） |
| Agent 引擎 | ReAct + asyncio.gather | LangChain | 中（需重构） |
| LLM 模型 | glm-4.6v | GPT-4o/Claude | 低（OpenAI 兼容协议） |
| 流式协议 | SSE | WebSocket | 中（路由重构） |
| 引用追踪 | 自研 CitationManager | — | 已实现 |
| 前端渲染 | marked.js | markdown-it | 低 |
| PDF 导出 | window.print() | weasyprint | 低 |

---

## ⚠️ 坑点记录

1. **Wikipedia 反爬**：httpx 默认 User-Agent 会被 403 拒绝，需要伪造浏览器 UA 或用 Jina Reader 绕过
2. **glm-4.6v 惰性 tool_calls**：默认只发 1 个 tool_call，需 system prompt 引导才能同时发起多个
3. **Agent 循环 risk**：LLM 可能重复抓取同一 URL，需要 agent 引擎层加 fetched_urls set
4. **SSE 连接超时**：长时间流式生成（>30s）需要在 uvicorn 设置 `timeout_keep_alive`
5. **Windows GBK 编码**：`PYTHONIOENCODING=utf-8` 必须设置，否则 Emoji 报错
6. **.claude/launch.json 路径**：必须在项目根目录或 parent 目录创建，否则 preview_start 不识别

---

## 📋 Week 7 产品开发路线图建议

基于本周技术预研结论，建议 Week 7 产品开发按以下优先级推进：

### 高优先级（必须）
1. **统一 SearchProvider 接口**：封装 Tavily + PubMed + arXiv + Jina 为统一接口
2. **Research Agent 核心循环**：ReAct Agent + CitationManager + 并行搜索
3. **分章节 SSE 流式生成**：大纲 + 逐节生成 + 参考文献
4. **基本前端交互**：主题输入 → 大纲预览 → 章节流式渲染 → 导出

### 中优先级（时间允许）
5. **划词优化**：完整的选区交互 + 优化历史
6. **PDF 导出优化**：weasyprint + Docker + 中文字体
7. **搜索结果缓存**：减少 API 调用，提升响应速度

### 低优先级（看情况）
8. 更多定向站点（FDA、ClinicalTrials.gov 等）
9. 引用的 NLP 自动插入
10. 报告模板系统（不同类型报告的 prompt 模板）

---

## 🔧 Week 7 建议的 Agent 完整流程设计

基于 Week 6 五个模块的预研结论，以下是 Week 7 产品开发的推荐架构：

### 核心流水线

```
用户输入研究主题
  │
  ▼
┌──────────────────────────────────────────────┐
│  Phase 1: Research (Agent Loop)              │
│                                              │
│  1. LLM 分析主题 → 拆解为 N 个子问题          │
│  2. 对每个子问题，LLM 发出多个 tool_calls      │
│     (search_web + search_site + fetch_url)    │
│  3. asyncio.gather 并行执行                   │
│  4. 结果 → CitationManager 注册              │
│  5. LLM 判断信息是否充足 → 不足则继续搜索     │
│  6. 信息收集完毕 → 进入 Phase 2              │
│                                              │
│  关键配置: tool_choice="auto", 引导并行       │
│  防重复: fetched_urls set + 结果相似度去重    │
│  超时保护: 每步 max 30s, 总步数 max 10       │
└──────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────┐
│  Phase 2: Outline Generation                 │
│                                              │
│  1. LLM 基于收集的研究资料生成章节大纲         │
│  2. 输出 JSON: [{title, description, sources}]│
│  3. SSE event: outline                       │
│                                              │
│  每个章节绑定引用来源:                         │
│  { title: "Long COVID的流行病学",             │
│    description: "...",                        │
│    source_indices: [1, 3, 5] }               │
└──────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────┐
│  Phase 3: Section-by-Section Generation       │
│                                              │
│  for each section in outline:                │
│    1. SSE: section_start                     │
│    2. LLM 流式生成该节内容                     │
│       - Prompt 注入该节专属的引用来源全文       │
│       - Prompt 注入已生成章节的摘要（防重复）  │
│    3. SSE: section_chunk (token by token)    │
│    4. SSE: section_end (完整内容 + 引用)      │
│                                              │
│  前端: marked.js 实时渲染，逐节展示            │
└──────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────┐
│  Phase 4: Post-processing                    │
│                                              │
│  1. LLM 生成摘要                              │
│  2. CitationManager.format_references()      │
│  3. 组装 ResearchReport Pydantic 模型         │
│  4. SSE: report_complete                     │
│  5. 持久化到 JSON 文件 / 数据库               │
└──────────────────────────────────────────────┘
  │
  ▼
  用户交互: 划词优化 | 导出 MD/PDF | 分享
```

### Week 7 第一天的开工任务（按优先级）

1. **统一 SearchProvider 接口** — 把 `web_search.py` + `site_registry.py` + `content_fetcher.py` 抽象为统一的 `SearchProvider` 基类，`TavilyProvider`、`PubMedProvider`、`JinaProvider` 分别实现
2. **ResearchAgent 类** — 把 `agent.py` 的 `run_agent_stream()` 函数重构为 `ResearchAgent` 类，封装 Phase 1 的完整研究循环
3. **端到端流水线** — 把 Phase 1-4 串联成一个 `POST /api/v1/report/generate` 调用，替换当前模块 4 的「纯 LLM 大纲→生成」简化版
4. **前端升级** — 在现有 Demo 页基础上加研究进度展示（搜索中/已找到 N 个来源/正在生成第 X 节）

### 技术债务（Week 6 遗留，Week 7 应偿还）

| 问题 | 当前状态 | Week 7 改进 |
|------|---------|------------|
| 模块 4 的 `report_generator.py` 生成大纲时不使用真实搜索 | 纯 LLM 知识生成 | 替换为 Phase 1 Agent 研究 + Phase 2 大纲 |
| `agent_tools.py` 和 `report_generator.py` 中的 CitationManager 是两套 | 各自创建实例 | 统一为一个贯穿全程的实例 |
| 前端 SSE 解析只处理了基本事件 | 无错误恢复 | 加断线重连 + 进度条 |
| 搜索结果无缓存 | 每次调用 API | 加 TTL 缓存（同 query 1h 复用） |

---

## 🔧 Week 6 最终审查修复记录（2026-07-12）

提交: `3496188` | 审查维度: 安全性 · 正确性 · 鲁棒性 · 架构 | 共计 10 个问题全部修复

### 严重问题 (已修复)

| # | 问题 | 位置 | 修复 |
|---|------|------|------|
| 1 | 报告导出链路断裂 — `_reports` 字典从未被写入，导出永远返回 404 | `app/routers/report.py:23` | SSE `report_complete` 事件拦截 → 存入 `_reports[report_id]`，注入 `report_id` 给前端，FIFO 上限 50 条 |
| 2 | CitationManager 全局 `_citation_manager` 并发竞态 — 多用户同时请求时引用数据互相覆盖 | `app/services/agent_tools.py:19` | 全局变量 → 显式参数传递。`execute_tool(name, args, citation_manager=cm)` |
| 3 | `time.sleep()` 阻塞 asyncio 事件循环 — 所有并发请求在 rate limiting 期间全部暂停 | `app/services/site_registry.py:114` | → `await asyncio.sleep()` |
| 4 | Render 部署 `buildCommand` 中 `apt-get install` 包名无效（`python3-cffi`、`python3-brotli` 不存在于 Debian apt） → exit 100 | `render.yaml` | 恢复为简单 `pip install -r requirements.txt` |

### 高风险问题 (已修复)

| # | 问题 | 位置 | 修复 |
|---|------|------|------|
| 5 | 全项目日志静默丢失 — 所有模块调用 `logging.getLogger()` 但无 `basicConfig` | 全局 | `main.py` 添加 `logging.basicConfig` + `X-Request-ID` 中间件 |
| 6 | SSRF 漏洞 — `fetch_url` 可访问内网地址 | `app/routers/search.py:24` | `_validate_public_url()`: 阻止内网 IP（10.x/192.168.x/169.254.x/127.0.0.1）、非 HTTP 协议，site_id 加 Pydantic `field_validator` |
| 7 | 异常信息泄露给客户端 — 错误响应的 `detail` 中包含 `str(e)` 全文 | `app/routers/report.py:120,179` | 改为通用错误消息，完整堆栈仅记录到 `logger.error(..., exc_info=True)` |

### 中等问题 (已修复)

| # | 问题 | 位置 | 修复 |
|---|------|------|------|
| 8 | PDF 导出 HTML `<title>` 未转义 → XSS 向量 | `app/routers/report.py:208` | `html.escape(title)` 处理 title 字段 |
| 9 | 前端 SSE parser `catch(e){}` 静默丢弃 JSON 解析错误 | `static/index.html:213` | → `console.warn('SSE parse error:', e)` |
| 10 | `ReportRefineRequest` Pydantic 类被 `ReportGenerateResponse` 注释覆盖 | `app/schemas/report.py:112-119` | 分离为两个独立类 |

### 审查增强功能

| 功能 | 说明 |
|------|------|
| `GET /health/deep` | 深度健康检查 — LLM API DNS 连通性验证 |
| `X-Request-ID` 中间件 | 每个请求自动生成追踪 ID，记录到日志 |
| `/healthCheckPath` | Render 自动健康检查路径 |
| `_MAX_REPORTS = 50` | 内存中报告存储上限，FIFO 淘汰 |

### 验证脚本矩阵

| 脚本 | 覆盖模块 | 不需要服务器 | 需要服务器 |
|------|---------|-------------|-----------|
| `verify_module1.py` | 搜索 + 正文提取 + 站点抓取 | ✅ | — |
| `verify_module2_3.py` | 并行调用 + 引用追踪（单元测试） | ✅ | — |
| `demo_agent.py` | Agent 完整循环 + 串行vs并行 + CitationManager | ✅ | — |
| `verify_module4.py` | Schema + 大纲生成 | ✅ | `--server` |
| `verify_module5.py` | 划词优化 + 文档导出 | ✅ (Test 5.3) | `--server` |

### 实测性能数据

```
串行 vs 并行 (3路真实API搜索):
  串行:   11.09s
  并行:    4.59s
  加速比:   2.4x
  节省:    6.50s

大纲生成 (LLM, 3章):  21.9s
PubMed API 搜索:       2.6s
Tavily 搜索:           3-5s
```
