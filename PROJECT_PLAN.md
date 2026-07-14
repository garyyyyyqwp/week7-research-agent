# 研究报告智能体 — Week 7 项目执行计划（架构师版）

> 版本 v1.0 ｜ 制定日期 2026-07-13 ｜ 适用范围 Week 7（核心引擎 + 产品原型）
> 本文档是可执行的工程蓝图，目标是让任意实现者（人或模型）据此独立完成搭建、联调与迭代。
> 基线代码位于 `../week6task`，Week 7 工作目录为 `./`（week7task）。

---

## 0. 如何使用本文档

- **实现者**：按第 9 章「里程碑」逐日推进，每个任务的接口与数据结构在第 5、6、7 章有精确定义，直接照写即可。
- **审查者**：用第 12 章「质量保障」与第 13 章「测试计划」作为验收清单（DoD）。
- **决策记录**：所有技术选型的理由在第 3 章，若要替换某组件，先读该组件的「理由」与「迁移成本」。
- 术语：Phase 指研究管道的四个阶段；SSE 指 Server-Sent Events；RC 指 ResearchContext；CM 指 CitationManager。

## 1. 项目目标与范围定义

### 1.1 一句话定义
用户在左栏对话式输入研究课题与参数，AI 自主联网调研（Tavily + 定向站点），把真实文献按章节路由后由 LLM 撰写**带引用标注**的研报，右栏实时逐节渲染，支持划词优化与下载。

### 1.2 Week 7 成功标准（可量化验收）
1. **真实数据**：研报每一节至少能追溯到 1 个真实联网来源；全篇来源数 ≥ 3（不足时前端明确降级提示）。抽查任一 `[n]` 标注，其 URL 可打开且内容相关。
2. **全链路打通**：一次 `POST /report/generate` 请求内完成 Phase 1→4，SSE 事件顺序正确、无中断即可产出完整 `ResearchReport`。
3. **Manus 双栏 UI**：左栏进度日志实时滚动，右栏章节按序流式出现，顶部进度条显示「已完成 X / 共 N 节」。
4. **划词优化**：选中文字→浮层→选风格→替换→可撤销一次，全流程可演示。
5. **下载**：Markdown 含元数据头 + 目录 + 正文 + 参考文献；PDF 至少有 `window.print()` 降级可用。
6. **鲁棒性**：定向站点超时自动降级 Tavily；Agent 步数超限安全进入 Outline；单来源超时跳过不崩溃。

### 1.3 范围边界（In / Out of Scope）

| 纳入 Week 7 | 不纳入（Week 8 或以后） |
|---|---|
| ResearchContext 临时向量索引（ChromaDB） | 本地知识库长期存储 / 多文档上传 |
| 四阶段研究管道 + 单一 CM 贯穿 | 多用户账号体系、鉴权、配额 |
| Manus 双栏 UI + SSE 实时集成 | UI 精修（动效、主题、移动端完善） |
| 划词优化 + 撤销一次 | 完整修改历史 / 多级 undo-redo |
| Markdown 下载（含元数据）+ PDF print 降级 | weasyprint 中文字体 Docker 化（Week 8 打磨） |
| 搜索失败降级 / 步数保护 / 超时跳过 | SSE 断线**续传**（列为高风险，best-effort，见 §11） |
| 搜索结果 TTL 缓存（可选增强） | 引用 NLP 自动插入、报告模板系统 |

### 1.4 非目标与明确不做
- 不追求生成速度极致优化（Week 7 以「跑通 + 正确」为先，性能在 Week 8）。
- 不做付费墙全文抓取（PubMed 只取摘要，够用）。
- 不引入前端构建工具链（保持单 HTML + CDN，零构建，降低部署复杂度）。

---

## 2. 现状盘点：Week 6 交付了什么，欠了什么

### 2.1 可直接复用（资产）
| 模块 | 文件 | 状态 |
|---|---|---|
| LLM 客户端（OpenAI 兼容，GLM） | `app/services/llm.py` | ✅ 直接用 |
| 通用搜索（Tavily + Jina 兜底） | `app/services/web_search.py` | ✅ 直接用 |
| 正文提取（Jina + bs4 兜底） | `app/services/content_fetcher.py` | ✅ 直接用 |
| 定向站点（PubMed/arXiv/S2 API + Jina） | `app/services/site_registry.py` | ✅ 直接用，Week 7 加超时/降级封装 |
| 引用管理 CitationManager | `app/services/citation_manager.py` | ✅ 直接用，作为贯穿全程的单实例 |
| 研报 Schema（Pydantic） | `app/schemas/report.py` | ✅ 扩展元数据字段 |
| 划词优化后端 `/report/refine` | `app/routers/report.py` | ✅ 直接用，前端补撤销 |
| 导出端点 `/{id}/export` | `app/routers/report.py` | ✅ 补元数据头 |
| SSRF 校验、日志、健康检查 | `app/routers/search.py`、`main.py` | ✅ 保留 |
| Agent 并行工具调用循环 | `app/services/agent.py` | ⚠️ 需重构为可复用的研究阶段 |

### 2.2 技术债 / 阻塞级缺口（Week 7 必须解决）
> 这四条来自 Week 6 技术选型文档的「技术债务」表，是本周核心工作。

1. **【阻塞】研报不使用真实数据**：`report_generator.py:170` 每次 `cm = CitationManager()` 新建空实例，`references_text = ""`，章节 prompt 里没有任何搜索结果——**研报完全靠 LLM 知识编造**。这是 Week 7 要消灭的头号问题。
2. **【阻塞】Token 超限风险**：Agent 抓取的全文若全量塞进章节 prompt 会超上下文窗口。需要 **ResearchContext** 做分块 + 按节检索（RAG）。
3. **【重要】两套 CitationManager**：`agent.py` 一套、`report_generator.py` 一套，引用编号不一致。需统一为**一个实例贯穿 Phase 1-4**。
4. **【重要】Agent 是函数不是可编排组件**：`run_agent_stream()` 把研究与「最终回答/citations/perf/done」耦合在一起，且只保留 500 字 observation、丢弃全文。Phase 1 需要保留全文写入 RC。

### 2.3 关键架构约束（实现者必读）
- **EventSource 断线续传不可行于当前设计**：浏览器 `EventSource` 只支持 `GET`，而生成是 `POST`（带 body）。Week 7 生成流用 `fetch` + `ReadableStream` 手动解析 SSE（Week 6 已如此）。真正的「续传」需要「先 POST 建 job → GET SSE 订阅」两段式架构，列为高风险 best-effort（§11），默认实现「断线后一键重连重生成」。
- **Render 部署对 ChromaDB 的影响**：ChromaDB 默认自带 `onnxruntime` 本地 embedding 模型（体积大、冷启动慢）。选型见 §3.3——用**外部 embedding API**（GLM/OpenAI 兼容）+ ChromaDB 仅作向量存储，避免拖垮 Render 免费实例。

## 3. 核心技术栈选型及理由

### 3.1 后端（延续 Week 6，稳定不动摇）
| 组件 | 选择 | 理由 | 迁移成本 |
|---|---|---|---|
| Web 框架 | **FastAPI** | 原生 async，与 asyncio.gather 并行搜索契合；Pydantic 集成 | — |
| ASGI Server | **uvicorn** | 生产标配；需设 `--timeout-keep-alive 120` 支撑长 SSE | — |
| SSE | **sse-starlette** `EventSourceResponse` | 与 FastAPI 无缝，事件类型区分 | 中（换 WebSocket 需重构路由） |
| LLM | **GLM（glm-4.6v，OpenAI 兼容协议）** | Week 6 已验证；协议兼容意味着可无痛切 GPT/Claude | 低 |
| 通用搜索 | **Tavily** | 为 AI Agent 设计，结构化结果，中文优秀，免费 1000 次/月 | 低 |
| 正文提取 | **Jina Reader** `r.jina.ai` | 零配置返回干净 Markdown，绕过反爬 | 低 |
| 定向站点 | **官方 API**（PubMed Entrez / arXiv / Semantic Scholar）+ Jina 兜底 | 结构化、可靠、免费 | — |

### 3.2 新增：向量检索（ResearchContext 的核心）
| 组件 | 选择 | 理由 | 备选 |
|---|---|---|---|
| 向量库 | **ChromaDB（in-memory / ephemeral client）** | 作业明确要求；API 简单；临时 collection 用完即删，天然契合「以 report_id 为命名空间」 | FAISS（无内置元数据）、内存 numpy（无持久与过滤） |
| Embedding | **外部 API（GLM `embedding-2` 或 OpenAI 兼容 `text-embedding`）** | **关键决策**：避免 ChromaDB 默认 onnxruntime 本地模型拖垮 Render 冷启动与内存；与现有 LLM 客户端同源，配置统一 | ChromaDB 默认 all-MiniLM（本地，Week 8 可选离线方案） |
| 分块策略 | **按 ~300 token 定长滑窗，overlap 15%** | 作业指定 300 token/块；overlap 防止句子被切断丢语义 | 语义分块（Week 8 优化） |

> **选型理由展开**：ChromaDB 若用默认 embedding，首次运行会下载 ~80MB 的 ONNX 模型并加载 onnxruntime，Render 免费实例（512MB RAM）易 OOM 或冷启动超时。改用 embedding API：ChromaDB 只存我们算好的向量（`collection.add(embeddings=[...])`），职责收敛为「向量存储 + 相似度检索 + 元数据过滤」，部署轻量。代价是每块文本一次 embedding API 调用——用批量接口 + 缓存缓解。

### 3.3 ResearchContext 后端形态决策
- **client 类型**：`chromadb.EphemeralClient()`（纯内存，进程级）。理由：研报是一次性任务，report 完成即 `cleanup()`；无需落盘，避免 Render 无持久磁盘的问题。
- **命名空间**：`collection_name = f"research_{report_id}"`，天然隔离并发的多份研报。
- **降级**：若 ChromaDB 初始化失败（依赖缺失/内存不足），RC 退化为「内存列表 + 关键词 BM25/子串匹配检索」，保证管道不崩（见 §5.1 降级设计）。

### 3.4 前端（保持零构建）
| 组件 | 选择 | 理由 |
|---|---|---|
| 结构 | **单 `index.html` + 原生 JS** | 零构建、零依赖安装，Render 静态托管；符合 Week 6 既有风格 |
| Markdown 渲染 | **marked.js（CDN）** | 轻量，支持增量 `marked.parse()` 流式渲染 |
| 布局 | **CSS Grid 双栏** | 作业指定；右栏 max-width 800px |
| 选区交互 | **浏览器 Selection API** | 标准 API，无依赖 |
| 流式读取 | **fetch + ReadableStream** 手动解析 SSE | 支持 POST + body（EventSource 不支持 POST） |
| 代码高亮/表格 | marked 内置 GFM | 够用 |

### 3.5 依赖清单增量（`requirements.txt` 追加）
```
chromadb>=0.5.0          # 向量存储（EphemeralClient）
tiktoken>=0.7.0          # 精确 token 计数用于 300-token 分块
tenacity>=8.2.0          # embedding/搜索 API 重试（可选，也可手写）
```
> 若 GLM 无 embedding 接口，用 `OPENAI_EMBEDDING_*` 指向任一 OpenAI 兼容 embedding 服务；配置见 §8。

## 4. 系统架构设计

### 4.1 整体架构图
```
┌──────────────────────────────────────────────────────────────────────┐
│                          浏览器 (static/index.html)                     │
│  ┌───────────────────────────┐      ┌────────────────────────────────┐ │
│  │  左栏：对话 + 控制           │      │  右栏：研报文档                   │ │
│  │  · 课题输入 / 参数           │      │  · 标题 + 目录(可跳转)            │ │
│  │  · 定向站点复选              │      │  · 章节流式渲染(marked.js)        │ │
│  │  · 研究进度流(实时日志)       │      │  · 参考文献(悬停显示来源)         │ │
│  │  · 发送 / 停止               │      │  · 工具栏：MD / PDF / 复制        │ │
│  └───────────────────────────┘      │  · 划词优化浮层(Selection API)    │ │
│         │  fetch + ReadableStream    └────────────────────────────────┘ │
└─────────┼──────────────────────────────────────────▲───────────────────┘
          │ POST /report/generate (SSE)               │ POST /report/refine
          ▼                                           │ GET /{id}/export
┌──────────────────────────────────────────────────────────────────────┐
│                      FastAPI (main.py + routers/)                       │
│   routers/report.py  ── 编排入口，拦截 report_complete 存 _reports       │
└─────────┬──────────────────────────────────────────────────────────────┘
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│              research_pipeline.py  ★核心编排★ (新增)                      │
│   一个 async generator，产出所有 SSE 事件，串起 Phase 1-4                 │
│   持有：唯一 CitationManager 实例 + 唯一 ResearchContext 实例            │
│                                                                        │
│  Phase 1 Research ──► Phase 2 Outline ──► Phase 3 Sections ──► Phase 4  │
│      │                    │                    │                  │     │
│      ▼                    ▼                    ▼                  ▼     │
│  ResearchEngine      LLM 大纲+绑定源     RC.retrieve(节)→LLM流式   摘要   │
│  (重构自 agent.py)                       每节只注入 top_k 片段      +组装 │
└───┬──────────────────────┬────────────────────┬─────────────────────────┘
    │                      │                    │
    ▼                      ▼                    ▼
┌─────────────┐   ┌──────────────────┐  ┌──────────────────────────────┐
│ 搜索/抓取层   │   │ ResearchContext  │  │ CitationManager (贯穿全程)     │
│ web_search   │──►│ (ChromaDB 临时)   │  │ 编号一致性的唯一真相源          │
│ site_registry│   │ add/retrieve/    │  │ Phase1 注册 → Phase4 format   │
│ content_     │   │ cleanup          │  └──────────────────────────────┘
│  fetcher     │   └──────────────────┘
└──────┬───────┘            │
       ▼                    ▼
  Tavily / Jina /     Embedding API (GLM/OpenAI 兼容)
  PubMed / arXiv
```

### 4.2 数据流（真实数据如何流到研报里 —— 消灭头号技术债）
```
搜索结果(全文) ──┬──► ResearchContext.add(分块+向量化)   [供 LLM 检索用]
                └──► CitationManager.add(URL/标题/来源)   [供引用编号用]

写第 k 节时:
  片段 = ResearchContext.retrieve(section_title, top_k=8)   # 只拿最相关 8 块
  prompt = SECTION_PROMPT(片段 + 该节绑定的引用编号 + 已写节关键句)
  LLM 基于「真实片段」流式生成 → 天然带 [n] 标注
```
> 对比 Week 6：Week 6 的 `references_text=""`，LLM 凭空写；Week 7 每节 prompt 携带真实检索片段，且片段里带引用编号，LLM 落笔即引。

### 4.3 模块划分与目录结构
```
week7task/
├── main.py                          # FastAPI 入口（复用+挂载新路由）
├── requirements.txt                 # + chromadb, tiktoken
├── render.yaml                      # 部署配置（+ embedding 环境变量）
├── .env.example
├── app/
│   ├── services/
│   │   ├── llm.py                   # [复用] LLM 客户端
│   │   ├── embeddings.py            # [新增] embedding API 封装 + 批量 + 缓存
│   │   ├── research_context.py      # [新增★] ResearchContext (任务1)
│   │   ├── research_engine.py       # [新增★] ResearchEngine (重构自 agent.py)
│   │   ├── research_pipeline.py     # [新增★] 四阶段编排 (任务2)
│   │   ├── web_search.py            # [复用] Tavily + Jina
│   │   ├── content_fetcher.py       # [复用] Jina + bs4
│   │   ├── site_registry.py         # [复用+封装超时/降级]
│   │   ├── citation_manager.py      # [复用] 贯穿全程单实例
│   │   ├── report_generator.py      # [改造] 保留 outline/section/abstract 子函数，供 pipeline 调用
│   │   └── agent_tools.py           # [复用] 工具执行器
│   ├── routers/
│   │   ├── report.py                # [改造] generate 改调 research_pipeline
│   │   ├── search.py                # [复用]
│   │   └── agent.py                 # [复用] 保留独立 Agent 端点(演示/调试)
│   ├── schemas/
│   │   ├── report.py                # [扩展] ReportMeta + 站点参数
│   │   └── agent.py                 # [复用]
│   └── utils/
│       └── config.py                # [扩展] embedding + RC + 超时配置
├── static/
│   └── index.html                   # [重写★] Manus 双栏 UI (任务3/4)
├── tests/
│   ├── test_research_context.py     # [新增] add→retrieve→cleanup
│   ├── test_pipeline.py             # [新增] 管道集成（mock 搜索）
│   └── test_citation_flow.py        # [新增] 编号一致性
├── scripts/
│   ├── run_pipeline_demo.py         # [新增] 真实课题跑一遍，输出运行日志
│   └── verify_week7.py              # [新增] 端到端冒烟
└── docs/
    ├── pipeline-run-log.md          # [产出] 真实运行日志（耗时/来源数/token）
    └── sse-protocol.md              # [产出] SSE 事件协议规范
```

### 4.4 关键组件设计要点
- **research_pipeline.py 是唯一状态持有者**：在函数开头 `cm = CitationManager()`、`rc = ResearchContext(report_id)`，Phase 1-4 全程传递同一对象；`finally` 块中 `rc.cleanup()`，保证异常也清理。
- **ResearchEngine 与 Phase 1 解耦**：引擎负责「跑 ReAct 循环 + 把每条来源的**全文**喂给 RC、把元数据喂给 CM」，并向外 yield `research_progress` 事件；不负责写报告。
- **report_generator.py 降级为函数库**：`generate_outline()`、`generate_abstract()` 保留；新增 `write_section_stream(section, retrieved_chunks, cm, prior_summaries)`。pipeline 调用它们，而不是它自己跑全流程。
- **前端事件驱动**：一个 `handleSSEEvent(event, data)` 分发器，左栏消费 `research_*`，右栏消费 `outline/section_*/abstract/references/report_complete`。

## 5. 关键组件详细设计（实现者照此编码）

### 5.1 ResearchContext（`app/services/research_context.py`）— 任务 1
职责：以 `report_id` 为命名空间的临时向量索引，解决 Token 超限。

```python
class ResearchContext:
    def __init__(self, report_id: str):
        self.report_id = report_id
        self.collection_name = f"research_{report_id}"
        self._client = chromadb.EphemeralClient()          # 纯内存
        self._collection = self._client.create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._degraded = False        # ChromaDB 不可用时置 True
        self._fallback: list[dict] = []  # 降级用内存列表

    async def add(self, content: str, url: str, site: str, title: str) -> int:
        """把一条来源正文分块(≈300 token/块, overlap 15%)存入向量索引。
        返回写入的块数。每块 metadata 带 {url, site, title, chunk_idx}。"""
        chunks = chunk_text(content, target_tokens=300, overlap=0.15)  # tiktoken 计数
        embeds = await embed_batch(chunks)                             # embeddings.py
        ids = [f"{hash(url)}_{i}" for i in range(len(chunks))]
        self._collection.add(
            ids=ids, documents=chunks, embeddings=embeds,
            metadatas=[{"url": url, "site": site, "title": title, "chunk_idx": i}
                       for i in range(len(chunks))],
        )
        return len(chunks)

    async def retrieve(self, section_title: str, top_k: int = 8) -> list[dict]:
        """写某节前检索最相关片段。返回 [{content, url, site, title}, ...]"""
        q_embed = (await embed_batch([section_title]))[0]
        res = self._collection.query(query_embeddings=[q_embed], n_results=top_k)
        return [
            {"content": doc, "url": m["url"], "site": m["site"], "title": m["title"]}
            for doc, m in zip(res["documents"][0], res["metadatas"][0])
        ]

    def cleanup(self):
        """删除临时 collection，释放空间。幂等，异常吞掉。"""
        try:
            self._client.delete_collection(self.collection_name)
        except Exception:
            pass
```

**分块函数 `chunk_text`**：用 tiktoken 编码计数，按 300 token 切，块间 overlap 45 token（15%）。纯文本无 tiktoken 时退化为按 ~1200 字符切（中文近似）。

**降级设计**：`__init__` 中 try/except 包裹 ChromaDB 初始化；失败则 `_degraded=True`，`add` 存入 `_fallback`，`retrieve` 用「关键词重叠打分（section_title 分词 vs 块内容）」返回 top_k。保证管道永不因向量库崩溃。

**并发与清理**：pipeline 的 `finally` 必调 `cleanup()`；另设兜底——`_reports` FIFO 淘汰时若关联 RC 未清理则清理（防泄漏）。

### 5.2 ResearchEngine（`app/services/research_engine.py`）— 重构自 agent.py
在 `agent.py` 的并行循环基础上抽出「研究」职责：

```python
class ResearchEngine:
    def __init__(self, citation_manager: CitationManager,
                 research_context: ResearchContext,
                 max_steps: int = 10):
        self.cm = citation_manager
        self.rc = research_context
        self.max_steps = max_steps

    async def research(self, topic: str, enabled_sites: list[str]) -> AsyncIterator[dict]:
        """跑 ReAct 循环并行搜索。每条来源:
           1. 全文 → self.rc.add(...)    (供检索)
           2. 元数据 → self.cm.add(...)  (供引用编号，工具执行器已做)
           yield research_progress / research_source_found 事件。
           步数超限 → 停止并 yield research_done(reason='max_steps')。
        """
```
要点：
- 复用 `agent_tools.execute_tool(..., citation_manager=self.cm)`——CM 一致性由此保证。
- **关键改动**：工具返回的 observation 现在只截断用于「喂回 LLM 的对话」；**完整全文**需另存进 RC。方案：`execute_fetch_url` / `execute_search_*` 增加可选回调或返回结构化 `(display, sources_with_fulltext)`，pipeline 拿到全文写 RC。（见 §5.3 实现顺序）
- `enabled_sites` 为空 → 只用 `search_web`；非空 → 引导 LLM 优先 `search_site`。
- 每次工具执行后 yield 人类可读进度：`🔍 正在搜索 PubMed: "..."` / `✅ 已收集 N 篇来源`。

### 5.3 ResearchPipeline（`app/services/research_pipeline.py`）— 任务 2（核心）
```python
async def run_research_pipeline(
    report_id: str, topic: str, num_sections: int,
    language: str, enabled_sites: list[str],
) -> AsyncIterator[dict]:
    cm = CitationManager()
    rc = ResearchContext(report_id)
    try:
        # ---- Phase 1: Research ----
        yield sse("research_start", {"topic": topic, "sites": enabled_sites})
        engine = ResearchEngine(cm, rc, max_steps=AGENT_MAX_STEPS)
        async for ev in engine.research(topic, enabled_sites):
            yield ev                                   # research_progress...
        yield sse("research_done", {"sources": cm.count})

        if cm.count < MIN_SOURCES:                     # 来源不足提示(前端顶部黄条)
            yield sse("warning", {"code": "few_sources", "count": cm.count})

        # ---- Phase 2: Outline (每节绑定引用来源) ----
        outline = await generate_outline_with_sources(topic, num_sections,
                                                       language, cm)
        yield sse("outline", {"topic": topic, "sections": outline,
                              "count": len(outline)})

        # ---- Phase 3: Section Writing ----
        prior_summaries = []
        for i, sec in enumerate(outline):
            yield sse("section_start", {"index": i, "title": sec["title"],
                                        "total": len(outline)})
            chunks = await rc.retrieve(sec["title"], top_k=RETRIEVE_TOP_K)   # ★只注入8块
            collected = ""
            async for chunk in write_section_stream(topic, sec, chunks, cm,
                                                     prior_summaries, language):
                collected += chunk
                yield sse("section_chunk", {"index": i, "chunk": chunk})
            used = extract_citation_indices(collected)   # 从正文解析 [n]
            yield sse("section_end", {"index": i, "title": sec["title"],
                                      "content": collected, "citations": used})
            prior_summaries.append({"title": sec["title"],
                                    "key": first_sentences(collected, 2)})

        # ---- Phase 4: Post-processing ----
        abstract = await generate_abstract(topic, outline, language)
        yield sse("abstract", {"abstract": abstract})
        yield sse("references", {"references": cm.format_references(),
                                 "citations_json": cm.to_dict()})
        report = assemble_report(topic, abstract, outline, cm, meta=...)
        yield sse("report_complete", {"report": report.model_dump(),
                                      "markdown": report.to_markdown(),
                                      "report_id": report_id})
        yield sse("done", {"report_id": report_id, "sources": cm.count})
    except Exception as e:
        logger.error("pipeline failed: %s", e, exc_info=True)
        yield sse("error", {"message": "研报生成失败，请重试", "phase": "..."})
    finally:
        rc.cleanup()                                    # ★保证清理
```
> `sse(event, obj)` = `{"event": event, "data": json.dumps(obj, ensure_ascii=False)}`。

**运行日志产出要求**（`docs/pipeline-run-log.md`）：真实课题跑一遍，记录：搜索总耗时、并行加速比、来源数量、每节检索块数与 token 用量、总生成时长。

### 5.4 关键常量（`config.py`）
```
RETRIEVE_TOP_K = 8         # 每节注入片段数
CHUNK_TARGET_TOKENS = 300  # 分块大小
CHUNK_OVERLAP = 0.15
MIN_SOURCES = 3            # 低于此触发 few_sources 警告
AGENT_MAX_STEPS = 10       # 研究阶段步数上限
PER_SEARCH_TIMEOUT = 15    # 单次搜索/抓取超时(秒)，超时跳过
```

## 6. 数据模型设计

### 6.1 核心领域模型（Pydantic，扩展 `schemas/report.py`）
```
ResearchReport
├── title: str
├── abstract: str
├── sections: list[ReportSection]
│   ├── title: str
│   ├── content: str            # Markdown，含 [n] 标注
│   └── citations: list[int]    # 本节实际用到的引用编号
├── references: list[Citation]  # 全篇引用，编号来自贯穿全程的 CM
├── meta: ReportMeta            # 【新增】
└── generated_at: str (ISO8601)

ReportMeta (新增，用于 Markdown 元数据头)
├── topic: str
├── num_sources: int
├── sites: list[str]            # 实际用到的站点名
├── language: str
├── generated_at: str
└── model: str                  # 生成用的 LLM 名

Citation (复用 CitationManager.Citation)
├── index: int
├── url, title, snippet: str
├── source_type: "web"|"academic"|"official"|"code"
└── site_name: str
```

### 6.2 运行期（非持久）数据结构
| 结构 | 归属 | 生命周期 |
|---|---|---|
| `CitationManager._sources: dict[url→Citation]` | pipeline 实例 | 单次研报 |
| `ChromaDB collection research_{id}` | ResearchContext | 单次研报，`cleanup()` 删除 |
| `_reports: dict[report_id→报告JSON]` | `routers/report.py` | 进程内，FIFO 上限 50（复用 Week 6） |
| `embedding cache: dict[text_hash→vector]` | embeddings.py | 进程内 LRU（可选，降本） |

### 6.3 请求/响应模型
```
ReportGenerateRequest (扩展)
├── topic: str (1..500)
├── num_sections: int (2..8)     # 作业要求 2-8
├── language: "zh-CN"|"en"
├── enabled_sites: list[str]     # 【新增】["pubmed","arxiv","who"]，空=仅Tavily
└── include_references: bool = True

ReportRefineRequest (复用 Week 6)
├── selected_text, context_before, context_after, instruction

ReportRefineResponse (复用)
├── refined_text, original_text, changes_summary
```

### 6.4 持久化说明
Week 7 **不引入数据库**——报告存进程内 `_reports`（重启丢失，可接受，演示级）。Week 8 再上 SQLite/PG。这条是有意的范围控制，避免过度设计。

---

## 7. 接口规范（API + SSE 协议）

### 7.1 HTTP 端点
| 方法 | 路径 | 说明 | 变化 |
|---|---|---|---|
| POST | `/api/v1/report/generate` | 四阶段管道，返回 SSE 流 | **改造**：内部改调 `run_research_pipeline` |
| POST | `/api/v1/report/refine` | 划词优化 | 复用 |
| GET | `/api/v1/report/{id}/export?format=md\|pdf` | 导出 | **改造**：MD 加元数据头 |
| POST | `/api/v1/agent/chat` | 独立 Agent（调试/演示保留） | 复用 |
| POST | `/api/v1/search/{web,fetch,site}` | 搜索原子端点 | 复用 |
| GET | `/api/v1/search/sites` | 列出可选定向站点（前端复选框数据源） | 复用 |
| GET | `/health`, `/health/deep` | 健康检查 | 复用 |

### 7.2 SSE 事件协议（`docs/sse-protocol.md` 需正式化）
事件严格顺序：
```
research_start
  → research_progress (×N)          # 左栏进度日志
  → research_source_found (×N)      # 可选，累计来源计数
research_done
  → [warning]                       # 可选，few_sources
outline
  → (每节循环)
     section_start
       → section_chunk (×M)         # 右栏增量渲染
     section_end                    # 含该节 citations
abstract
references
report_complete                     # 完整 report + markdown + report_id
done
  ── 或任意阶段 ──► error            # 终止，携带 message + phase
```

**各事件 data 字段契约**（前后端必须对齐）：
| event | data 字段 |
|---|---|
| `research_start` | `{topic, sites: []}` |
| `research_progress` | `{ts, icon, message}`（如 `{icon:"🔍", message:"正在搜索 PubMed: ..."}`） |
| `research_source_found` | `{count, title, site, url}` |
| `research_done` | `{sources: int, elapsed_s: float}` |
| `warning` | `{code:"few_sources", count}` |
| `outline` | `{topic, sections:[{title, description, source_indices:[]}], count}` |
| `section_start` | `{index, title, total}` |
| `section_chunk` | `{index, chunk}` |
| `section_end` | `{index, title, content, citations:[int]}` |
| `abstract` | `{abstract}` |
| `references` | `{references:markdown, citations_json:{count,citations:[]}}` |
| `report_complete` | `{report:{...}, markdown, report_id}` |
| `done` | `{report_id, sources}` |
| `error` | `{message, phase}` |

> 兼容性：Week 6 前端已处理 `outline/section_*/abstract/references/report_complete/done`；Week 7 前端**新增**处理 `research_*` 与 `warning/error`。旧的 `status` 事件可保留为兼容别名或移除。

### 7.3 导出的 Markdown 元数据头规范
```markdown
---
topic: "Long COVID 的神经系统影响"
generated_at: 2026-07-13T10:24:00Z
num_sources: 7
sites: [PubMed, WHO, Tavily]
language: zh-CN
model: glm-4.6v
---

# <标题>
## 摘要
## 目录
## <各章节>
## 参考文献
```

## 8. 环境配置与开发流程

### 8.1 环境变量（`.env` / `.env.example` / Render）
```
# LLM（复用 Week 6）
OPENAI_API_KEY=            # 必填
OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
OPENAI_MODEL=glm-4.6v

# Embedding（新增，可与 LLM 同源或独立）
OPENAI_EMBEDDING_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
OPENAI_EMBEDDING_API_KEY=  # 缺省回退 OPENAI_API_KEY
OPENAI_EMBEDDING_MODEL=embedding-2

# 搜索
TAVILY_API_KEY=           # 必填

# 管道参数（新增，有默认值）
AGENT_MAX_STEPS=10
RETRIEVE_TOP_K=8
CHUNK_TARGET_TOKENS=300
MIN_SOURCES=3
PER_SEARCH_TIMEOUT=15

PYTHONUNBUFFERED=1
PYTHONIOENCODING=utf-8    # Windows 本地 Emoji 防 GBK 报错
```

### 8.2 本地启动
```bash
cd week7task
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
cp ../week6task/.env .env    # 复用密钥，补 embedding 配置
uvicorn main:app --reload --timeout-keep-alive 120
# 打开 http://127.0.0.1:8000
```

### 8.3 开发流程与协作规范
- **迁移方式**：以 `week6task` 为基线，**复制**到 `week7task` 后增量改造（不在 week6 原地改，保留可回退基线）。第一步任务：`cp -r ../week6task/* .`（排除 `.venv`、`__pycache__`、chroma 缓存）。
- **分支/提交**：功能分支开发，提交信息用 `feat/fix/refactor/test/docs:` 前缀。每完成一个「任务 N」提交一次并附验证结论。
- **代码风格**：延续 Week 6——模块 docstring + 类型标注 + 中文注释解释「为什么」；async 全程；日志用 `logging.getLogger(__name__)`。
- **禁止**：`time.sleep` 阻塞事件循环（用 `asyncio.sleep`）；全局可变状态跨请求共享（CM/RC 必须实例级）；把密钥写进代码。
- **每日收尾**：更新 `docs/` 下对应产出物；跑 `scripts/verify_week7.py` 冒烟。

---

## 9. 项目里程碑与时间节点（7 天）

> DoD = Definition of Done（完成标准）。每个任务的 DoD 同时是测试计划的验收项。

### Day 1（周一）— 基线迁移 + ResearchContext
- [ ] **M1.1** 复制 week6 → week7，跑通旧功能（`uvicorn` 起，`/health` 通，旧 generate 能出报告）。
- [ ] **M1.2** 新增 `embeddings.py`：`embed_batch(texts)->list[vector]`，批量 + 重试 + 可选 LRU 缓存。
- [ ] **M1.3** 实现 `research_context.py`（任务 1）：`add/retrieve/cleanup` + `chunk_text` + 降级路径。
- **DoD**：`tests/test_research_context.py` 通过（add 3 条来源 → retrieve 返回相关块 → cleanup 后 collection 不存在）；ChromaDB 不可用时降级路径也通过。

### Day 2（周二）— 四阶段管道打通
- [ ] **M2.1** `research_engine.py`：重构 agent 循环，来源全文写 RC、元数据写 CM，yield `research_progress`。
- [ ] **M2.2** `research_pipeline.py`（任务 2）：串起 Phase 1-4，单一 CM+RC，`finally` 清理。
- [ ] **M2.3** `report.py` 的 `/generate` 改调 pipeline；`report_generator.py` 拆出可复用子函数。
- [ ] **M2.4** `scripts/run_pipeline_demo.py` 跑一次真实课题，产出 `docs/pipeline-run-log.md`。
- **DoD**：真实课题一次跑通 Phase 1→4，SSE 顺序正确；研报正文含真实 `[n]`；运行日志含耗时/来源数/token。**头号技术债消灭**。

### Day 3（周三）— Manus 双栏 UI
- [ ] **M3.1** 重写 `index.html`：CSS Grid 双栏；左栏输入+参数+定向站点复选+进度流+发送/停止；右栏标题+目录+章节+参考文献+工具栏。
- [ ] **M3.2** SSE 分发器处理全部事件；右栏 marked.js 增量渲染；顶部进度条（X/N 节）。
- **DoD**：浏览器输入课题→左栏实时滚动进度日志→右栏逐节出现→参考文献渲染；进度条正确。

### Day 4（周四）— 划词优化 UX
- [ ] **M4.1** Selection API 划词→浮层（5 风格）→POST `/refine`→替换选区。
- [ ] **M4.2** 撤销一次（保留 `original_text`）；选区消失浮层自动隐藏；视口边界处理。
- **DoD**：选中→优化→替换→撤销全流程可演示，截图存档。

### Day 5（周五）— 端到端集成 + 错误处理（任务 5）
- [ ] **M5.1** 定向站点超时/空结果 → 降级 Tavily，进度日志提示。
- [ ] **M5.2** 步数超限安全进入 Outline；单来源 >15s 跳过；来源 <3 顶部黄条。
- [ ] **M5.3** SSE 断开 → 前端「重新生成」兜底（续传列 best-effort）；后端 `error` 事件优雅收尾。
- **DoD**：§13.3 边界用例逐条通过并记录。

### Day 6（周六）— 质量提升 + 导出完善（任务 6）
- [ ] **M6.1** 强化 `SECTION_PROMPT`：每节 ≥1 数据/案例、用表格对比、`[n]` 格式、注入已写节关键句防重复。
- [ ] **M6.2** MD 导出加元数据头 + 目录；前端「复制到剪贴板」按钮。
- [ ] **M6.3** PDF：测 weasyprint 于 Render；中文字体缺失则 `window.print()` 降级。
- **DoD**：导出 MD 结构完整；PDF 至少一种方案可用；研报质量抽查达标。

### Day 7（周日）— 部署 + 全流程演示彩排
- [ ] **M7.1** 部署 Render（含 embedding 环境变量、`--timeout-keep-alive`）；冷启动与内存验证。
- [ ] **M7.2** 端到端彩排：输入课题→研究→流式生成→划词优化→下载，录屏。
- [ ] **M7.3** 回归 `verify_week7.py`；整理交付物与 README。
- **DoD**：线上/本地完整跑通一次全流程，OKR 1-6 全绿。

> **缓冲策略**：Day 5-7 有交叉缓冲。若 Day 2 管道延期，优先保 Phase 1+3 打通（真实数据落地），Outline 源绑定可简化；UI 的进度条、复制按钮属可降级项。

## 10. 资源需求与分配

### 10.1 外部服务与配额
| 资源 | 用途 | 免费额度 | Week 7 预估 | 风险 |
|---|---|---|---|---|
| GLM API | LLM 生成 + embedding | 按量 | 每份研报 ~15-25 次 LLM + ~30-60 次 embedding | embedding 若无免费额度→用 OpenAI 兼容替代或本地模型 |
| Tavily | 通用搜索 | 1000/月 | 每份 5-10 次 | 够用 |
| PubMed/arXiv/S2 | 定向站点 | 免费（限速） | 每份 ~5 次 | 遵守 rate_limit |
| Jina Reader | 正文/无 API 站点 | 免费 | 每份 ~5-10 次 | 偶发超时→兜底 |
| Render | 部署 | 免费 512MB | 单实例 | ChromaDB 内存——已用 embedding API 规避 |

### 10.2 人力/角色分配（单人或多模型协作皆适用）
| 角色 | 职责 | 对应任务 |
|---|---|---|
| 后端-引擎 | RC / Engine / Pipeline / embeddings | 任务 1、2、5 |
| 前端-交互 | 双栏 UI / SSE 集成 / 划词 / 导出 | 任务 3、4、6 |
| 集成-QA | 测试、边界、部署、运行日志 | 任务 5、6、Day7 |

若多模型并行：**接口先行**——先冻结 §7 的 SSE 协议与 §6 数据模型，前后端各自照契约开发，用 mock SSE 流联调。

---

## 11. 风险管理策略

| # | 风险 | 概率 | 影响 | 缓解 | 应急（Plan B） |
|---|---|---|---|---|---|
| R1 | ChromaDB 拖垮 Render（内存/冷启动） | 中 | 高 | embedding 走 API，用 EphemeralClient | 降级为内存列表+关键词检索（§5.1） |
| R2 | GLM 无 embedding 接口或收费 | 中 | 高 | 配置化 embedding endpoint | 换 OpenAI 兼容 embedding，或 ChromaDB 默认本地模型（Week8 离线包） |
| R3 | LLM 惰性/不并行 tool_calls | 高 | 中 | system prompt 强引导「一次多搜」+ URL 去重 | 引擎侧主动拆子问题并行发起 |
| R4 | 搜索/抓取超时拖慢全程 | 高 | 中 | 单次 15s 超时跳过 + asyncio.gather | 降级 Tavily；进度日志提示 |
| R5 | SSE 长连接被代理/超时切断 | 中 | 中 | `--timeout-keep-alive 120`；心跳事件 | 前端「重新生成」；续传列 best-effort |
| R6 | 研报质量空洞/章节重复 | 中 | 中 | 强化 prompt + 注入已写节关键句 + 真实片段 | 人工微调 prompt；提高 top_k |
| R7 | 引用编号错乱（两套 CM 回潮） | 低 | 高 | 架构强约束：pipeline 唯一 CM | 集成测试 `test_citation_flow` 守门 |
| R8 | 中文 PDF 字体缺失 | 高 | 低 | `window.print()` 降级 | Week8 weasyprint+字体 Docker |
| R9 | 并发多研报内存泄漏（RC 未清） | 低 | 中 | `finally cleanup` + FIFO 兜底清理 | 限制并发；定时清扫 |
| R10 | 单人 7 天工期紧 | 中 | 中 | Day5-7 缓冲 + 可降级项清单 | 砍 best-effort 续传、复制按钮等非核心 |

> **止损原则**：同一路线连续失败 2 次即停下诊断根因，换方案（如 embedding 从 GLM 切 OpenAI 兼容），不做无效增量修补。

---

## 12. 质量保障措施

### 12.1 代码质量
- 类型标注全覆盖；关键函数 docstring 说明契约。
- 每个新增 service 有对应单测；PR 合并前必跑测试。
- 复用 Week 6 已修复的安全项：SSRF 校验、异常不泄露 `str(e)` 给客户端（仅日志）、HTML title 转义、日志 `X-Request-ID`。
- 新增安全审查点：embedding API key 不入日志；ChromaDB collection 名以 report_id 派生（防注入）；refine 输入长度上限已由 Pydantic 约束。

### 12.2 研报内容质量门槛（抽查标准）
1. 每节 ≥1 个具体数据/案例；
2. 至少 1 处 Markdown 表格对比；
3. `[n]` 标注格式正确且编号存在于参考文献；
4. 章节间无明显观点重复；
5. 全篇来源可追溯（点开 URL 内容相关）。

### 12.3 可观测性
- pipeline 各 Phase 打点日志（耗时、来源数、token）。
- `research_progress` 事件即用户可见的可观测面。
- `/health/deep` 保留，Render 健康检查用。

---

## 13. 测试计划

### 13.1 单元测试（无需服务器，pytest）
| 文件 | 覆盖 | 关键断言 |
|---|---|---|
| `test_research_context.py` | add→retrieve→cleanup | 分块数合理；retrieve 返回结构含 content/url/site/title 且相关；cleanup 后 collection 消失；降级路径可用 |
| `test_citation_flow.py` | CM 贯穿一致性 | 同 URL 去重返回同号；Phase1 注册的号在 Phase4 references 中一致；`extract_citation_indices` 正确解析 `[n]` |
| `test_chunking.py` | `chunk_text` | ≈300 token/块；overlap 生效；中文/英文/超长/空串边界 |
| `test_embeddings.py` | embed_batch | 批量维度一致；重试；缓存命中 |

### 13.2 集成测试（mock 搜索层，避免真实 API 依赖）
| 文件 | 覆盖 |
|---|---|
| `test_pipeline.py` | mock `search_web/search_site/fetch_url` 返回固定文档 → 跑完整 pipeline → 断言 SSE 事件顺序、report 结构、references 非空、每节有内容 |
| `test_report_export.py` | 生成后 `/export?format=md` 含元数据头/目录/参考文献；404 路径 |
| `test_refine.py` | `/refine` 返回 refined_text 且非空（mock LLM） |

### 13.3 系统/边界测试（真实或半真实，人工+脚本记录）
| 用例 | 期望 |
|---|---|
| 正常课题（医学，启用 PubMed+WHO） | 全流程通，来源≥3，右栏逐节渲染 |
| 冷门课题（来源<3） | 顶部 `few_sources` 黄条，仍产出报告 |
| 定向站点全超时 | 自动降级 Tavily，进度日志有提示 |
| Agent 达 10 步未收敛 | 安全进入 Outline，不崩 |
| 单来源抓取 >15s | 跳过该来源，流程继续 |
| 生成中刷新/断网 | 前端可「重新生成」；无残留错误态 |
| 划词优化 + 撤销 | 替换成功，撤销还原 |
| MD/PDF 导出 | 文件正确，元数据完整 |
| 并发 2 份研报 | 引用/RC 互不串扰，各自 cleanup |

### 13.4 端到端冒烟（`scripts/verify_week7.py`）
一条命令跑：起服务→POST generate→消费 SSE→断言关键事件到齐→export→校验产物。作为每日收尾与部署后验证。

## 14. 部署策略与环境配置

### 14.1 Render 部署（复用 Week 6 `render.yaml`，增量）
```yaml
services:
  - type: web
    name: week7task
    runtime: python
    region: singapore
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1 --timeout-keep-alive 120
    healthCheckPath: /health
    envVars:
      - key: OPENAI_API_KEY {sync: false}
      - key: OPENAI_BASE_URL {value: https://open.bigmodel.cn/api/paas/v4/}
      - key: OPENAI_MODEL {value: glm-4.6v}
      - key: OPENAI_EMBEDDING_MODEL {value: embedding-3}
      - key: TAVILY_API_KEY {sync: false}
      - key: AGENT_MAX_STEPS {value: "10"}
      - key: RETRIEVE_TOP_K {value: "8"}
      - key: MIN_SOURCES {value: "3"}
      - key: PYTHONUNBUFFERED {value: "1"}
```
要点：
- `--timeout-keep-alive 120` 支撑长 SSE（Week 6 坑点 #4）。
- `--workers 1`：`_reports` 是进程内字典，多 worker 会不共享；Week 7 单 worker 即可。Week 8 上共享存储后再扩。
- **ChromaDB 内存**：EphemeralClient + embedding API，实测内存应可在 512MB 内；若超，Plan B 降级检索。
- weasyprint 系统依赖（pango/cairo）在 Render 可能缺失——**默认走前端 `window.print()`**，weasyprint 作为可选，失败即回退。

### 14.2 环境矩阵
| 环境 | 用途 | LLM/Embedding | 部署 |
|---|---|---|---|
| 本地 dev | 开发调试 | GLM | uvicorn --reload |
| 本地 test | pytest | mock | — |
| Render prod | 演示 | GLM | render.yaml |

### 14.3 部署后验证清单
1. `/health/deep` 返回 ok；2. 提交一个课题跑通全流程；3. 导出 MD 正常；4. 观察 Render 内存曲线无 OOM；5. 冷启动首请求 <30s。

---

## 15. 后续拓展与优化路径（Week 8 及以后）

### 15.1 Week 8 明确项（作业已列）
- 本地知识库接入：文档上传→复用 ResearchContext 的分块/embedding，与联网来源混合检索。
- UI 精修：动效、主题、目录高亮联动滚动、移动端。
- PDF 导出优化：weasyprint + 中文字体 Docker 化。
- 部署：稳定化、监控、可能的持久化存储。

### 15.2 架构演进方向
| 方向 | 现状 | 演进 |
|---|---|---|
| 报告持久化 | 进程内 `_reports` | SQLite/Postgres + 报告历史列表 |
| 多 worker | 单 worker | Redis 共享 job 状态 + SSE 续传（两段式 job/subscribe） |
| SearchProvider | 分散函数 | 统一 `SearchProvider` 基类（Tavily/PubMed/Jina 子类），加结果相似度去重 |
| 引用 | LLM 自插 `[n]` | 句子级 NER 自动插入 + 引用校验（悬空引用检测） |
| 检索 | 定长分块+cosine | 语义分块 + rerank 模型 + 混合检索（BM25+向量） |
| 报告类型 | 单一 prompt | 模板系统（综述/对比/尽调等不同 prompt） |
| 缓存 | 可选 embedding LRU | 搜索结果 TTL 缓存（同 query 1h 复用）降本 |

### 15.3 商业化考量（远期）
多用户与鉴权、用量计费与配额、团队协作与分享、导出格式扩展（docx/pptx，可接 PaperJSX 类工具）、私有部署与数据合规。

---

## 16. 实现者快速启动清单（Bootstrap Checklist）

按顺序执行，每步可独立验证：

1. `cp -r ../week6task/* .`（排除 `.venv/__pycache__/chroma*`），补 `.env` 的 embedding 配置，`uvicorn` 起验旧功能。
2. `requirements.txt` 加 `chromadb`、`tiktoken`；`pip install`。
3. 写 `app/services/embeddings.py` → 跑 `test_embeddings.py`。
4. 写 `app/services/research_context.py`（任务1）→ 跑 `test_research_context.py`。【DoD: Day1】
5. 写 `app/services/research_engine.py`（重构 agent，来源全文写 RC）。
6. 写 `app/services/research_pipeline.py`（任务2），改 `routers/report.py` 调它，拆 `report_generator.py` 子函数。
7. `scripts/run_pipeline_demo.py` 真跑一次 → `docs/pipeline-run-log.md`。【DoD: Day2，头号债务消灭】
8. 重写 `static/index.html` 双栏 + SSE 分发（任务3）。【DoD: Day3】
9. 加划词优化 + 撤销（任务4）。【DoD: Day4】
10. 错误处理与降级（任务5）→ 跑 §13.3 边界用例。【DoD: Day5】
11. 强化 prompt + MD 元数据 + 复制/PDF（任务6）。【DoD: Day6】
12. 部署 Render + 端到端彩排 + `verify_week7.py`。【DoD: Day7】

---

## 附录 A：SSE 事件顺序状态机（前端实现参考）
```
idle ──generate()──► RESEARCHING (听 research_*)
RESEARCHING ──outline──► OUTLINING (渲染目录, 进度条 0/N)
OUTLINING ──section_start──► WRITING (进度条 k/N, 右栏追加节)
WRITING ──section_chunk──► WRITING (marked 增量渲染)
WRITING ──section_end──► WRITING or (末节后) POSTPROCESS
POSTPROCESS ──abstract/references/report_complete──► DONE (启用导出)
任意态 ──error──► ERROR (提示 + 允许重新生成)
```

## 附录 B：关键文件改造对照速查
| 文件 | 动作 | 一句话 |
|---|---|---|
| `research_context.py` | 新增 | ChromaDB 临时索引 + 降级 |
| `embeddings.py` | 新增 | embedding API 封装 |
| `research_engine.py` | 新增(重构 agent) | 研究阶段，全文入 RC |
| `research_pipeline.py` | 新增 | 四阶段编排，单 CM+RC |
| `report_generator.py` | 拆分 | 变函数库供 pipeline 调用 |
| `routers/report.py` | 改 generate | 改调 pipeline；MD 加元数据 |
| `schemas/report.py` | 扩展 | ReportMeta + enabled_sites |
| `static/index.html` | 重写 | Manus 双栏 + 全事件 + 划词撤销 |
| `config.py` | 扩展 | embedding/RC/超时常量 |
| `requirements.txt` | 增量 | chromadb, tiktoken |

---
*本计划为可执行工程蓝图；接口契约（§6/§7）一经冻结即为前后端协作基准，变更需同步双方。*
