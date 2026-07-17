# 研究报告智能体（Week 7 毕业项目）

输入一个研究课题，系统通过 Tavily 与定向权威站点（PubMed / arXiv / WHO / CDC / GitHub / Semantic Scholar）进行真实联网搜索，将来源全文写入临时向量库做 RAG 检索，再由 LLM（Qwen3-235B，阿里云 MaaS，OpenAI 兼容接口）逐节流式撰写带 `[n]` 行内引用的研究报告。前端为 Manus 风格双栏界面：左栏实时滚动研究进度，右栏逐节渲染报告正文，支持划词优化与文档导出。

核心特性：

- **真实数据研报**：所有引用来自实际抓取的网页/文献，非模型编造
- **四阶段管道**：Research → Outline → Section Writing (RAG) → Post-processing，全程 SSE 流式
- **双栏 UI**：左栏进度流 + 参数配置，右栏目录/章节/参考文献实时增量渲染
- **划词优化**：选中报告任意片段，输入指令即可局部重写（`/report/refine`）
- **引用追溯**：CitationManager 全程追踪来源，`[n]` 标记可映射回参考文献条目
- **MD + PDF 导出**：PDF 由 WeasyPrint 生成，内置 Noto Sans SC 中文字体

## 架构总览

四阶段管道（`app/services/research_pipeline.py`），单个 async generator 串起全流程，以 SSE 事件流输出：

```
用户课题
   │
   ▼
Phase 1  Research（ResearchEngine：ReAct 循环 + 并行 tool_calls）
   │        Tavily 搜索 / Jina Reader 正文提取 / 定向站点抓取
   │        来源全文 → ResearchContext；来源元数据 → CitationManager
   │        SSE: research_start → research_progress* → research_done
   ▼
Phase 2  Outline（基于已收集来源生成大纲）
   │        SSE: outline
   ▼
Phase 3  Section Writing（逐节 RAG 写作）
   │        每节以章节标题检索 RC top_k=8 相关块（300-token 分块），
   │        prompt 只注入检索块、绝不塞全文；LLM 流式输出带 [n] 引用正文
   │        SSE: section_start → section_chunk* → section_end（每节循环）
   ▼
Phase 4  Post-processing（摘要 + 参考文献 + 全文组装）
            SSE: abstract → references → report_complete → done
```

两条架构不变量：**一份报告全程只有一对 CitationManager + ResearchContext 实例**（消除 Week 6「双 CM 导致引用编号错乱、LLM 凭空捏造引用」的技术债）；ResearchContext 使用 ChromaDB 临时 collection，`finally` 中必然执行 `rc.cleanup()`，用完即删、异常也不泄漏。ChromaDB 或 embedding API 不可用时自动降级为无向量检索路径。

## 本地运行

要求 Python 3.11+。

```bash
cd week7task
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash；Linux/macOS 用 .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env    # 填入 OPENAI_API_KEY 与 TAVILY_API_KEY

uvicorn main:app --port 8002 --timeout-keep-alive 120
# 打开 http://127.0.0.1:8002
```

健康检查：`GET /health`（基础）、`GET /health/deep`（含 LLM 域名连通性）。

主要 API（详见 `main.py` docstring）：

| 路由 | 说明 |
|---|---|
| `POST /api/v1/report/generate` | 四阶段管道 SSE 流式生成（核心） |
| `POST /api/v1/report/refine` | 划词优化 |
| `GET /api/v1/report/{id}/export` | 导出 MD / PDF |
| `POST /api/v1/agent/chat` | ReAct Agent（并行 tool_calls + 引用追踪） |
| `POST /api/v1/search/web` | Tavily 联网搜索 |
| `POST /api/v1/search/fetch` | 网页正文提取（Jina Reader） |
| `POST /api/v1/search/site` | 定向站点抓取（SiteRegistry） |

## 环境变量

来源：`.env.example` 与 `render.yaml`。

| 变量 | 必填 | 默认/示例 | 说明 |
|---|---|---|---|
| `OPENAI_API_KEY` | **是** | — | LLM API Key（阿里云 MaaS） |
| `OPENAI_BASE_URL` | 否 | `https://ws-…maas.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容接口地址 |
| `OPENAI_MODEL` | 否 | `qwen3-235b-a22b-instruct-2507` | 生成模型 |
| `OPENAI_EMBEDDING_API_KEY` | 否 | 回退 `OPENAI_API_KEY` | Embedding Key，可与 LLM 同源 |
| `OPENAI_EMBEDDING_BASE_URL` | 否 | 回退 `OPENAI_BASE_URL` | Embedding 接口地址 |
| `OPENAI_EMBEDDING_MODEL` | 否 | `text-embedding-v3` | Embedding 模型 |
| `TAVILY_API_KEY` | **是** | — | Tavily 联网搜索 Key |
| `AGENT_MAX_STEPS` | 否 | `10` | ReAct 循环最大步数 |
| `RETRIEVE_TOP_K` | 否 | `8` | 每节 RAG 检索块数 |
| `CHUNK_TARGET_TOKENS` | 否 | `300` | 分块目标 token 数 |
| `CHUNK_OVERLAP` | 否 | `0.15` | 分块重叠比例 |
| `MIN_SOURCES` | 否 | `3` | 研究阶段最少来源数 |
| `PER_SEARCH_TIMEOUT` | 否 | `15` | 单次搜索超时（秒） |
| `EMBEDDING_BATCH_SIZE` | 否 | `10` | Embedding 批量大小（阿里云 text-embedding-v3 上限 10） |
| `EMBEDDING_MAX_RETRIES` | 否 | `3` | Embedding 重试次数 |
| `CORS_ALLOW_ORIGINS` | 否 | 空（同源） | 逗号分隔的跨域来源白名单 |

Jina Reader 无需 Key（`https://r.jina.ai/{url}` 零配置）。

## Render 部署

仓库根目录含 `render.yaml`，在 Render 用 **Blueprint** 方式一键创建服务：

1. Render 控制台 → New → Blueprint，选择本仓库；
2. 按提示填入三个 secret：`OPENAI_API_KEY`、`OPENAI_EMBEDDING_API_KEY`、`TAVILY_API_KEY`（`render.yaml` 中标记为 `sync: false`），其余变量已在文件中给定；
3. 部署完成后访问 `/health` 验证。

注意事项：

- Python 版本通过 `PYTHON_VERSION` 环境变量指定（当前 `3.11.11`）。Render native runtime 不读取 `runtime.txt`。
- PDF 中文字体已内置在 `static/fonts/`（Noto Sans SC），导出时经 `@font-face` + `file://` 直接加载，**不依赖系统字体、无需 apt-get**（native runtime 构建期无 root 权限，装系统包必然失败）。
- 免费实例空闲约 15 分钟后休眠，下次访问冷启动约 1 分钟，属正常现象。

## 部署地址

线上演示: https://<your-service>.onrender.com  <!-- TODO: 填入实际 Render URL -->

## 测试

```bash
python -m pytest tests/ -q
```

测试全部离线可跑：LLM / 搜索 / embedding 均以 mock 注入，不消耗 API 配额、无需网络。覆盖内容：

- `tests/test_research_context.py` — 向量库 add/retrieve/cleanup、分块、降级路径、并发隔离
- `tests/test_pipeline.py` — 四阶段 SSE 事件顺序与数据契约
- `tests/test_citation_flow.py` — 引用编号端到端一致性
- `tests/test_pdf_export.py` — PDF 导出（中文字体、格式）
- `tests/test_audit_fixes.py` — 历次审计修复的回归测试

另有 `scripts/run_pipeline_demo.py` 可用真实课题跑通完整管道（消耗 API 配额），并输出结构化运行日志（耗时/来源数/token 用量）。

## 项目结构

```
week7task/
├── main.py                        # FastAPI 入口：路由挂载、CORS、请求日志、健康检查
├── render.yaml                    # Render Blueprint 部署配置
├── requirements.txt
├── .env.example
├── app/
│   ├── routers/
│   │   ├── report.py              # /generate(SSE 管道) /refine(划词优化) /export(MD/PDF)
│   │   ├── agent.py               # /chat — ReAct agent 流式对话
│   │   └── search.py              # /web /fetch /site — 搜索三件套
│   ├── services/
│   │   ├── research_pipeline.py   # 四阶段编排核心：单 CM+RC 贯穿，finally 清理
│   │   ├── research_engine.py     # Phase 1 研究引擎：ReAct 循环 + 并行工具调用
│   │   ├── research_context.py    # 临时向量库：ChromaDB collection + 分块 + 降级
│   │   ├── report_generator.py    # 大纲/逐节流式写作/摘要/全文组装
│   │   ├── citation_manager.py    # 引用追踪与参考文献格式化（实例级，非全局）
│   │   ├── embeddings.py          # 外部 embedding API 封装（批量 + 重试）
│   │   ├── web_search.py          # Tavily 搜索
│   │   ├── content_fetcher.py     # 网页正文提取（Jina Reader 优先，BS4 兜底）
│   │   ├── site_registry.py       # 定向站点注册表（PubMed/arXiv/WHO/CDC/GitHub/S2）
│   │   ├── agent.py               # ReAct Agent 引擎（/agent/chat 用）
│   │   ├── agent_tools.py         # 工具定义（Function Calling 格式）+ 引用注册
│   │   └── llm.py                 # OpenAI 兼容异步客户端
│   ├── schemas/                   # Pydantic 请求/响应模型
│   └── utils/
│       ├── config.py              # 环境变量集中加载
│       └── ratelimit.py           # 内存级每-IP 限流 + 并发上限
├── static/
│   ├── index.html                 # Manus 双栏前端（单文件）
│   ├── fonts/                     # 内置 Noto Sans SC（PDF 中文）
│   └── vendor/                    # 本地化 marked.js / DOMPurify
├── tests/                         # 离线单元/集成测试
├── scripts/                       # 演示与验证脚本（run_pipeline_demo.py 等）
└── docs/                          # 技术选型、验证报告等文档
```

## 已知限制

- **报告存储为单 worker 内存态**：服务重启后历史报告的服务端导出（`/export`）失效。前端已做兜底——报告 Markdown 保存在浏览器侧，可客户端直接导出 MD，并对失效情况给出提示。
- **无用户鉴权**：公网部署时接口对任何人开放。已通过每-IP 限流 + 全局并发上限（`app/utils/ratelimit.py`）与关闭通配 CORS 缓解刷量风险，但这不是鉴权替代品。
- **Render 免费实例冷启动**：休眠唤醒约需 1 分钟，期间请求会等待或超时。
- **长报告生成耗时较长**：取决于章节数、来源抓取与 LLM 速度，SSE 连接需保持（`--timeout-keep-alive 120`）。

## 文档索引

| 文档 | 内容 |
|---|---|
| [PROJECT_PLAN.md](PROJECT_PLAN.md) | 完整项目计划：需求、架构决策（§4）、管道规格（§5.3）、SSE 契约（§7.2）、运行手册（§8） |
| [docs/sse-protocol.md](docs/sse-protocol.md) | SSE 事件协议参考（13 个事件的字段与时序） |
| [docs/pipeline-run-log.md](docs/pipeline-run-log.md) | 真实课题管道运行日志（搜索耗时/来源数/每节检索块与 token 用量） |
| [docs/boundary-test-record.md](docs/boundary-test-record.md) | 边界情况测试记录（5 个作业场景 + 审计附加场景） |
| [docs/tech-selection.md](docs/tech-selection.md) | 技术选型对比与决策记录 |
| [docs/module1-verification-report.md](docs/module1-verification-report.md) | 模块 1（ResearchContext）验证报告 |
| [HANDOFF_PROMPTS.md](HANDOFF_PROMPTS.md) | 开发交接提示词 |
