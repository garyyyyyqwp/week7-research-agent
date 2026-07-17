# 研报生成 SSE 事件协议参考

> 对应端点：`POST /api/v1/report/generate`
> 服务端实现：`app/services/research_pipeline.py`（编排）+ `app/services/research_engine.py`（Phase 1 进度事件）+ `app/routers/report.py`（路由与兜底 error）
> 前端消费：`static/index.html` 中的 `handleSSEEvent()`

---

## 1. 概述

研报生成是一个长耗时（数十秒至数分钟）的四阶段管道（研究 → 大纲 → 章节撰写 → 后处理），服务端通过 **Server-Sent Events (SSE)** 将进度与内容实时推送给前端。

### 1.1 传输层

- **端点**：`POST /api/v1/report/generate`，请求体为 JSON：

  ```json
  {
    "topic": "研究主题（1-500 字符，必填）",
    "num_sections": 5,
    "language": "zh-CN",
    "enabled_sites": ["pubmed", "who"],
    "include_references": true
  }
  ```

  `num_sections` 范围 2-8；`enabled_sites` 为空数组时仅用 Tavily 通用搜索。端点带限流（60s 内 3 次）与全局并发上限（超限直接 429，不排队），被拒时返回普通 HTTP 错误而非 SSE 流。

- **为什么用 fetch 而不是 EventSource**：浏览器原生 `EventSource` 只支持 GET，无法携带 JSON 请求体。前端因此用 `fetch()` 发 POST，拿到 `resp.body.getReader()`（ReadableStream）后自行按行解析 `event:` / `data:` 字段（见 `static/index.html` 约 679-711 行）。这也意味着**没有 `Last-Event-ID` 自动重连语义**——断线后的"重试"是重新发起一次完整生成（新的 report_id），不是续传。

- **保活**：服务端使用 `sse-starlette` 的 `EventSourceResponse`（默认配置），每 **15 秒**发送一条 ping 注释行（形如 `: ping - <时间>`）。注释行不携带 `event:`/`data:`，前端解析器会自然忽略它，但这些字节会刷新前端的断线看门狗计时（见 §5.3）。

- **数据编码**：每个事件的 `data` 为一行 JSON，`ensure_ascii=False`（中文直出，UTF-8）。前端对无法 `JSON.parse` 的 data 行静默跳过。

### 1.2 线格式示例

```
event: research_start
data: {"topic": "Long COVID 的神经系统影响", "sites": ["pubmed", "who"]}

: ping - 2026-07-18 08:00:15+00:00

event: research_progress
data: {"ts": "2026-07-18T08:00:16.123456+00:00", "icon": "🔍", "message": "搜索网页: Long COVID neurological effects"}
```

> 注意：前端解析对未知事件名、未知 data 字段均**静默忽略**（前向兼容）。服务端新增字段不会破坏旧前端。

---

## 2. 事件时序（文字版）

```
research_start                          ── Phase 1 开始
  → research_progress (×N)             ── 左栏进度日志（思考/工具调用/步骤小结）
research_done                           ── Phase 1 结束（唯一权威统计事件）
  → [warning code=few_sources]         ── 可选：来源 < MIN_SOURCES
outline                                 ── Phase 2：大纲 + 来源绑定
  → 每节循环（共 count 节）：
     section_start
       → [warning code=degraded_retrieval]   ── 可选，整个流最多 1 次（见 §4.2）
       → section_chunk (×M)            ── 右栏增量渲染
     section_end                        ── 含该节全文 + citations
abstract                                ── Phase 4：摘要
references                              ── 参考文献（markdown + 结构化 JSON）
report_complete                         ── 完整 report + markdown + report_id
done                                    ── 流正常终止
  ── 或任意阶段 ──► error               ── 终止事件，携带 message + phase
```

- **终止事件**共三种：`report_complete`、`done`、`error`。前端以收到其中任意一个为"流完整"的判据（`sawTerminalEvent`）；干净 EOF 但没收到终止事件 = 服务端/代理中途断流，前端会提示"报告不完整"并给出重试链接。
- `error` 之后服务端不再发送任何事件（管道 `finally` 中清理 ResearchContext 后关闭流）。
- 计划文档（PROJECT_PLAN.md §7.2）中的 `research_source_found` 事件在当前代码中**并未实现**（仅存在于注释/计划），本协议不包含它；来源计数通过 `research_progress` 的步骤小结与 `research_done.sources` 体现。

---

## 3. 事件详解

### 3.1 `research_start`

**触发时机**：管道入口，Phase 1（研究阶段）开始前。

| 字段 | 类型 | 说明 |
|---|---|---|
| `topic` | string | 研究主题（原样回显请求参数） |
| `sites` | string[] | 启用的定向站点 ID 列表；空数组 = 仅 Tavily 通用搜索 |

```json
{"topic": "Long COVID 的神经系统影响", "sites": ["pubmed", "who"]}
```

### 3.2 `research_progress`

**触发时机**：Phase 1 ReAct 循环中多次发出（由 `ResearchEngine.research()` 产生），驱动前端左栏进度日志。

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts` | string | 事件产生时间，UTC ISO 8601（前端用它渲染时间戳） |
| `icon` | string | 单个 emoji，标识事件类别（见下表） |
| `message` | string | 人类可读的进度描述（中文） |

`icon` 取值与含义（来自 `research_engine.py`）：

| icon | 场景 |
|---|---|
| 💭 | LLM 思考内容（截断至 300 字符） |
| 🔄 | 本轮并行执行 N 个工具调用 |
| 🔍 | `search_web` 网页搜索 |
| 📚 | `search_site` 站内搜索 |
| 📄 | `fetch_url` 抓取网页正文 |
| 🔧 | 其他未知工具 |
| ⏱️ | 单个工具执行超时（超过 `PER_SEARCH_TIMEOUT`，已跳过该来源） |
| ⚠️ | 站点搜索降级为 Tavily / 达到最大步数 |
| ❌ | LLM 调用失败 / 站点搜索彻底失败 / 抓取失败 / 工具执行异常 |
| ✅ | 步骤小结："步骤 N 完成 (Xs)，已收集 M 个来源" |

```json
{"ts": "2026-07-18T08:00:16.123456+00:00", "icon": "📚", "message": "搜索 pubmed: long covid neurological sequelae"}
```

> 降级/超时/失败**必然**以进度日志形式对用户可见（不只写进 LLM 消息历史）；重复 URL 的抓取会带 `(跳过-重复)` 后缀。

### 3.3 `research_done`

**触发时机**：Phase 1 结束后由管道（而非引擎）发出，是研究阶段**唯一权威**的统计事件。

| 字段 | 类型 | 说明 |
|---|---|---|
| `sources` | int | CitationManager 中注册的来源总数 |
| `elapsed_s` | float | 研究阶段耗时（秒，保留 1 位小数） |
| `chunks_stored` | int | 已切分入库（ResearchContext/向量库）的全文分块总数 |
| `hit_max_steps` | boolean | 是否因达到最大步数（`AGENT_MAX_STEPS`）而结束（见 §4.3） |

```json
{"sources": 7, "elapsed_s": 42.3, "chunks_stored": 58, "hit_max_steps": false}
```

### 3.4 `warning`

**触发时机**：两处（同一事件名、不同 `code`，详见 §4）：

1. `research_done` 之后、`outline` 之前 —— `few_sources`；
2. Phase 3 某节 `section_start` 之后 —— `degraded_retrieval`（整个流最多发一次）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | string | `"few_sources"` 或 `"degraded_retrieval"` |
| `count` | int | 仅 `few_sources` 携带：实际来源数 |
| `message` | string | 人类可读警告文案 |

```json
{"code": "few_sources", "count": 2, "message": "数据来源较少（2 个），内容可能不够全面"}
```

```json
{"code": "degraded_retrieval", "message": "检索服务异常，部分章节可能未基于已收集的资料撰写"}
```

### 3.5 `outline`

**触发时机**：Phase 2 大纲生成完成后（含来源绑定；LLM 失败时回退为基础大纲，`source_indices` 为空数组）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `topic` | string | 研究主题 |
| `sections` | object[] | 章节列表，每项 `{title, description, source_indices}` |
| `sections[].title` | string | 章节标题 |
| `sections[].description` | string | 本节要写什么的简述 |
| `sections[].source_indices` | int[] | 该节绑定的引用编号（1 起始，对应 CitationManager） |
| `count` | int | 章节数（= `sections.length`） |

```json
{"topic": "Long COVID 的神经系统影响", "sections": [{"title": "流行病学概况", "description": "发病率与人群分布", "source_indices": [1, 3]}], "count": 5}
```

前端据此渲染报告标题与目录（TOC），并以 `count` 作为进度条分母。

### 3.6 `section_start`

**触发时机**：每一节开始撰写前（Phase 3 循环内）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `index` | int | 章节序号，**0 起始** |
| `title` | string | 章节标题 |
| `total` | int | 总章节数 |

```json
{"index": 0, "title": "流行病学概况", "total": 5}
```

### 3.7 `section_chunk`

**触发时机**：该节 LLM 流式输出的每个文本增量（一节内 ×M 次）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `index` | int | 所属章节序号（0 起始） |
| `chunk` | string | Markdown 文本增量（追加式，非全量） |

```json
{"index": 0, "chunk": "根据 WHO 2024 年的统计"}
```

前端将同一 `index` 的 chunk 累加后用 marked 增量渲染，并把 `[n]` 引用标记包装为可悬浮的引用链接；若 `section_chunk` 先于 `section_start` 到达（理论上不会，防御性处理），前端会先缓冲。

### 3.8 `section_end`

**触发时机**：该节流式输出结束后。若该节 LLM 生成中途抛异常，`content` 末尾会附加 `*(本节生成时遇到错误: ...)*` 而**不会**中断整个管道。

| 字段 | 类型 | 说明 |
|---|---|---|
| `index` | int | 章节序号（0 起始） |
| `title` | string | 章节标题 |
| `content` | string | 该节**完整** Markdown 正文（权威版本，前端用它整节重渲染，覆盖增量拼接结果） |
| `citations` | int[] | 正文中实际出现的引用编号（已按 `1..cm.count` 过滤，越界/幻觉编号与年份区间如 `[2021-2025]` 不会混入） |
| `retrieved_chunks` | int | 本节实际检索到并注入 prompt 的资料块数（运行日志用；前端忽略未知字段） |

```json
{"index": 0, "title": "流行病学概况", "content": "根据 WHO 2024 年的统计…[1]", "citations": [1, 3], "retrieved_chunks": 6}
```

### 3.9 `abstract`

**触发时机**：Phase 4 开始，全部章节完成后基于大纲生成摘要。

| 字段 | 类型 | 说明 |
|---|---|---|
| `abstract` | string | 摘要纯文本 |

```json
{"abstract": "本报告系统梳理了 Long COVID 神经系统影响的流行病学、机制与干预进展…"}
```

### 3.10 `references`

**触发时机**：摘要之后。

| 字段 | 类型 | 说明 |
|---|---|---|
| `references` | string | Markdown 参考文献列表（以 `## 📚 参考文献` 开头，条目形如 `[n] *站点* — **标题** \`[类型]\`` + 换行 URL） |
| `citations_json` | object | 结构化引用：`{count, citations: [...]}` |
| `citations_json.count` | int | 引用总数 |
| `citations_json.citations[]` | object | `{index, url, title, snippet, source_type, site_name, fetched_at}`；`source_type` 取值 `web`/`academic`/`official`/`code` |

```json
{"references": "## 📚 参考文献\n\n[1] *PubMed* — **Long COVID study** `[academic]`  \nhttps://pubmed.ncbi.nlm.nih.gov/12345/", "citations_json": {"count": 1, "citations": [{"index": 1, "url": "https://pubmed.ncbi.nlm.nih.gov/12345/", "title": "Long COVID study", "snippet": "Background: …", "source_type": "academic", "site_name": "PubMed", "fetched_at": "2026-07-18T08:01:00+00:00"}]}}
```

前端用 `citations_json.citations` 填充正文 `[n]` 标记的悬浮提示（标题/摘录/站点/URL）。

### 3.11 `report_complete`

**触发时机**：报告组装完成。路由层会在此拦截：把 data 存入内存 `_reports`（供 `GET /api/v1/report/{id}/export` 导出），并确保 `report_id` 字段存在。

| 字段 | 类型 | 说明 |
|---|---|---|
| `report` | object | `ResearchReport.model_dump()`：`{title, abstract, sections: [{title, content, citations}], references: [Citation…], meta, generated_at}`；`meta` 为 `{topic, num_sources, sites, language, generated_at, model}` |
| `markdown` | string | 完整 Markdown 全文（含 §7.3 规范的 YAML 元数据头） |
| `report_id` | string | 12 位十六进制报告 ID，用于导出端点 |

```json
{"report": {"title": "Long COVID 的神经系统影响", "abstract": "…", "sections": [{"title": "流行病学概况", "content": "…", "citations": [1, 3]}], "references": [], "meta": {"topic": "…", "num_sources": 7, "sites": ["PubMed", "WHO"], "language": "zh-CN", "generated_at": "2026-07-18T08:05:00+00:00", "model": "glm-4.6v"}, "generated_at": "2026-07-18T08:05:00+00:00"}, "markdown": "---\ntopic: …\n---\n\n# …", "report_id": "a1b2c3d4e5f6"}
```

前端收到后启用导出/复制按钮，进度条置 100%。

### 3.12 `done`

**触发时机**：流的最后一个事件（正常路径）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `report_id` | string | 报告 ID |
| `sources` | int | 来源总数 |
| `sections` | int | 实际写完的章节数 |
| `elapsed_s` | float | 整个管道总耗时（秒） |

```json
{"report_id": "a1b2c3d4e5f6", "sources": 7, "sections": 5, "elapsed_s": 187.4}
```

### 3.13 `error`

**触发时机**：任意阶段的未捕获异常。是终止事件——之后流关闭（`finally` 中总会执行 `rc.cleanup()` 释放向量库资源）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `message` | string | 用户可读错误文案（如"研报生成失败，请重试"） |
| `phase` | string | 出错层级：管道内部异常为 `"pipeline"`；路由层兜底为 `"generate"`。（前端另外预留了 `research`/`outline`/`section` 的展示映射，当前服务端不发这些值） |
| `detail` | string | 可选，仅 `phase="pipeline"` 携带：异常字符串截断至 200 字符 |

```json
{"message": "研报生成失败，请重试", "phase": "pipeline", "detail": "APITimeoutError: Request timed out."}
```

---

## 4. 错误与降级语义

### 4.1 `warning: few_sources`

- **条件**：Phase 1 结束时 `cm.count < MIN_SOURCES`（配置项，`app/utils/config.py`）。
- **位置**：`research_done` 之后、`outline` 之前，最多 1 次。
- **语义**：非致命。管道继续，但内容覆盖面可能不足。
- **前端行为**：顶部黄色警示横幅（仅此 code 触发横幅）+ 进度日志一条 warn。

### 4.2 `warning: degraded_retrieval`

- **条件**：某节 `rc.retrieve()` 返回空 **且** `chunks_stored > 0`（资料已入库却检索不到 = 检索链路故障，如嵌入 API 中途失效），整个流**最多发送 1 次**（首次触发的那一节）。
- **位置**：Phase 3 循环内，紧跟该节 `section_start` 之后。
- **语义**：非致命但重要——该节（及之后可能的节）不是基于检索到的真实资料写的，事实性下降。
- **前端行为**：仅进度日志 warn（不触发横幅）。

### 4.3 `hit_max_steps`

- **条件**：ReAct 循环达到 `AGENT_MAX_STEPS` 仍未自然收敛。
- **表现**：引擎先发一条 `research_progress`（icon ⚠️，"研究已达最大步数 (N)，进入大纲生成阶段"），随后 `research_done.hit_max_steps = true`。管道不再重复发送提示。
- **语义**：非致命。研究被截断，来源可能偏少（常与 `few_sources` 同时出现）。

### 4.4 节级生成失败

单节 LLM 流式异常**不产生** `error` 事件：错误文案以斜体附加到该节 `content` 末尾，`section_end` 照常发出，管道继续写下一节。只有管道级异常才走 `error` 终止。

### 4.5 前端断线看门狗（60s）

- 前端每次从 ReadableStream 读到**任何字节**（含 ping 注释行）就刷新 `lastEventAt`；一个 10s 间隔的定时器检查 `now - lastEventAt > 60000`，超时则置 `watchdogFired` 并 `abortController.abort()`。
- 由于服务端每 15s 必发 ping，60s 无字节几乎必然是连接假死（Wi-Fi 切换 / NAT 静默断链 / 代理缓冲吞流），而不是"生成太慢"。
- 中断后前端提示"连接超时（60s 无数据），已中断"，并在进度日志中插入**重试链接**（`retryGenerate()`：用上次参数重新发起完整 POST，`reconnectAttempts` 计数递增）。同样地，干净 EOF 但未见终止事件（`report_complete`/`done`/`error` 都没收到）也会提示"报告不完整"并给出重试链接。
- 用户手动点"停止"同样走 abort，但 `watchdogFired=false`，前端仅记录"用户停止生成"，不给重试链接。

---

## 5. 版本说明

- 本文档对应 **2026-07-18** 的代码状态（`app/services/research_pipeline.py`、`app/services/research_engine.py`、`app/routers/report.py`、`static/index.html`）。
- 与 PROJECT_PLAN.md §7.2 底稿的差异（以代码为准）：
  - `research_source_found` 未实现，不在协议中；
  - `research_done` 新增 `chunks_stored`、`hit_max_steps` 字段；
  - `warning` 除 `few_sources` 外新增 `degraded_retrieval`；
  - `section_end` 新增 `retrieved_chunks` 字段；
  - `done` 新增 `sections`、`elapsed_s` 字段；
  - `error` 新增可选 `detail` 字段。
- 兼容性约定：前端对未知事件与未知字段静默忽略；服务端**新增**字段/事件属向后兼容变更，**删除或改名**现有字段属破坏性变更，需同步更新本文档与 `handleSSEEvent()`。
