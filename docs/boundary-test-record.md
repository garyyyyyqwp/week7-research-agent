# 边界情况测试记录（任务 5 产出）

> 记录日期：2026-07-18 · 对应代码状态：审计修复后（见 git log）
> 验证方式标注：`[自动化]` = tests/ 中有对应断言，离线可复跑；`[代码审查+模拟]` = 浏览器内模拟 SSE 事件序列验证前端行为；`[真实运行]` = 对真实课题跑通全管道。

## 作业要求的 5 个边界场景

### 1. 定向站点超时 / 空结果 → 自动降级 Tavily + 进度日志提示

| 项 | 结论 |
|---|---|
| 状态 | ✅ 通过 |
| 降级链路 | `site_registry._search_via_jina_extraction` 失败/解析不出结果时返回**空列表**（不再返回"搜索失败"占位条目污染参考文献），`agent_tools.execute_search_site` 检测到空结果后用真实域名（如 `site:pubmed.ncbi.nlm.nih.gov`）转 Tavily 搜索 |
| 用户可见性 | `research_engine` 检测 `[search_site 降级]` / `[超时]` 观测结果，在 `research_progress` 事件中输出「⚠️ 站点无结果/超时，已降级为 Tavily 通用搜索」— 左栏进度日志实时可见 `[代码审查+模拟]` |
| 回归保护 | 占位条目不再进入 CitationManager（引用列表不会出现"WHO 搜索失败"）`[代码审查]` |

### 2. Agent 最大步数保护（≤ AGENT_MAX_STEPS=10 步后直接进大纲，不崩溃）

| 项 | 结论 |
|---|---|
| 状态 | ✅ 通过 `[自动化]` |
| 证据 | `tests/test_pipeline.py` 管道事件序列测试通过；`research_engine.research()` 的 while/else 在达到上限时置 `hit_max_steps=True` 并输出「研究已达最大步数」提示（本轮修复：该提示此前重复发送两次，现仅一次），随后正常进入 Phase 2 |
| 附加 | `research_done` 事件携带 `hit_max_steps` 字段，运行日志记录 |

### 3. 单次搜索 > PER_SEARCH_TIMEOUT(15s) → 跳过该来源继续流程

| 项 | 结论 |
|---|---|
| 状态 | ✅ 通过 |
| 机制 | 工具执行与全文抓取均包 `asyncio.wait_for(..., timeout=PER_SEARCH_TIMEOUT)`；超时返回 `[超时]` 观测值，流程继续 `[代码审查]` |
| 用户可见性 | 本轮修复：超时现以「⏱️ … 超过 15s，已跳过该来源」出现在进度日志（此前只写进 LLM 消息历史，用户看不到） |
| 性能 | 本轮修复：全文抓取由逐个串行改为 `asyncio.gather` + `Semaphore(5)` 并行，多个慢来源不再线性叠加等待 |

### 4. 来源 < MIN_SOURCES(3) → 右栏顶部警告横幅

| 项 | 结论 |
|---|---|
| 状态 | ✅ 通过 `[代码审查+模拟]` |
| 机制 | `research_pipeline` 在 Phase 1 结束后发 `warning {code: few_sources}`；前端 `warningBanner` 置为 visible（浏览器内注入该事件实测横幅显示） |
| 防伪 | 本轮修复：站点失败占位条目不再虚增来源计数，来源数如实反映真实可用来源 |

### 5. SSE 连接中断 → 前端明确提示 + 重新生成兜底

| 项 | 结论 |
|---|---|
| 状态 | ✅ 通过（明确的"重新生成"兜底路线）`[代码审查+模拟]` |
| 静默断链（Wi-Fi 切换/NAT 假死） | 本轮修复：60s 无任何字节（含 sse ping）watchdog 中断连接，日志提示「连接超时」+ 重试链接（此前 UI 永久卡"生成中"） |
| 服务端中途断流（干净 EOF） | 本轮修复：流结束但未收到 `done/report_complete/error` 时提示「连接中断，报告不完整」+ 重试链接（此前静默截断） |
| 并发保护 | 本轮修复：生成进行中再按 Enter / 点旧重试链接会被 in-flight guard 拦截（此前两条 SSE 流交叉写 DOM 导致报告花掉） |
| 设计说明 | 按 PROJECT_PLAN 决策，断线续传（从未完成节继续）为 best-effort 范围外，采用「明确提示 + 一键重新生成」兜底 |

## 附加边界场景（审计发现并修复）

### 6. 嵌入 API 中途失效（Phase 1 途中 / Phase 3 检索时）

| 项 | 结论 |
|---|---|
| 状态 | ✅ 通过 `[自动化]` — `tests/test_audit_fixes.py::TestMidRunDegradationMigration` / `TestRetrieveFailureFallback` |
| 修复前 | 途中失败会丢弃之前所有已入库材料；检索失败时每节拿 0 条材料仍被要求标 [n]（"真实数据研报"红线被破坏且无提示） |
| 修复后 | 已入库 chunks 迁移进关键词检索兜底；管道发 `warning {code: degraded_retrieval}` 明确告知；降级后 ChromaDB collection 仍被 cleanup 删除（不泄漏） |

### 7. cleanup 真正删除临时 collection

| 项 | 结论 |
|---|---|
| 状态 | ✅ 通过 `[自动化]` — `TestCleanupDeletesCollection`（补齐 DoD 断言「cleanup 后 collection 不存在」，此前测试只验证了幂等未验证删除） |

### 8. 两份报告并发生成

| 项 | 结论 |
|---|---|
| 状态 | ✅ 隔离通过 `[自动化]` — `tests/test_research_context.py::TestConcurrentIsolation`（collection 按 report_id 命名空间隔离） |
| 资源保护 | 本轮新增全局并发上限 2（`app/utils/ratelimit.ConcurrencyGuard`），超限返回 429「当前生成任务较多」，防止 512MB 实例被多管道挤爆 |

### 9. 恶意/异常输入

| 项 | 结论 |
|---|---|
| 抓取内容含 HTML 注入 | ✅ `[自动化+模拟]` — CitationManager 注册入口剥离 HTML 标签（`TestCitationSanitization`）；前端 marked 输出经 DOMPurify 消毒（浏览器内注入 `<img onerror>` 载荷实测未执行） |
| SSRF（内网/file:// URL） | ✅ `[自动化]` — `TestValidatePublicUrl`：服务层 `fetch_url` 直接拦截 `169.254.169.254`/`localhost`/私网段/非 http(s) 协议；PDF 渲染资源白名单只放行内置字体（`TestPdfUrlFetcher`） |
| 超长 payload | ✅ schema 层长度上限（topic≤500 / selected_text≤5000 / instruction≤2000），`/generate` 3次/分/IP、`/refine` 10次/分/IP 限流 |

## 复跑方式

```bash
python -m pytest tests/ -q            # 全量 86 项（含上述自动化断言），离线可跑
python scripts/run_pipeline_demo.py   # 真实课题全管道（需 .env 中的 API key）
```
