# 交接提示词库（给其他模型下任务用）

> 配套文件：`PROJECT_PLAN.md`（唯一事实源）。
> 用法：每次给模型下任务 = 【通用头部】+ 【某一个任务块】。一个会话只做一个任务，做完对着 DoD 验收通过再开下一个。
> 不要一次性把"整个项目做完"丢给模型——必然偏离契约、前后端对不上。

---

## 0. 使用顺序速查

| 顺序 | 任务块 | 对应计划 | 一句话 |
|---|---|---|---|
| 1 | 任务 A | §16 步1-4 | 基线跑通 + ResearchContext + embeddings |
| 2 | 任务 B | §5.2/5.3 | 四阶段管道（核心，消灭 LLM 编造） |
| 3 | 任务 C | §4.1/§7 | Manus 双栏 UI + SSE 集成 |
| 4 | 任务 D | 任务4/附录A | 划词优化 + 撤销 |
| 5 | 任务 E | §11/§13.3 | 容错降级 |
| 6 | 任务 F | 任务6 | 质量提升 + 导出完善 |
| 7 | 任务 G | §14 | 部署 + 彩排 |

> 若要前后端**并行**：先冻结 §6/§7 契约，任务 A/B（后端）与任务 C（前端）可同时开，前端先用假 SSE 流联调。

---

## 1. 通用头部（每个任务前都粘这段，只换「本次任务」和「DoD」）

```
你是资深全栈工程师，正在实现「研究报告智能体」项目。

## 铁律（每次都遵守）
1. 唯一事实源是 PROJECT_PLAN.md。动手前先读它，重点读本次任务标注的章节。
2. 基线代码已从 week6task 复制到当前目录。优先复用已有模块，不要重写能用的东西。
3. 严格遵守 §7 的 SSE 事件协议 和 §6 的数据模型：字段名、事件名一律不得改动。
4. 禁止：用 time.sleep（一律 asyncio.sleep）；把 CitationManager/ResearchContext
   做成全局或跨请求共享（必须实例级）；把密钥写进代码或打进日志。
5. 全程 async；补类型标注；注释解释「为什么」而不是「做了什么」。
6. 若某处与计划冲突或计划有坑，先停下来告诉我理由，不要擅自改契约。

## 交付要求
1. 明确列出新建/修改了哪些文件
2. 给出验证方法（跑了什么测试/命令，结果如何）
3. 不确定的地方先问，不要猜

## 本次任务
<粘贴对应任务块的「本次任务」>

## 完成标准 DoD
<粘贴对应任务块的「DoD」>
```

> 若目标模型**不能读文件**（纯聊天窗口）：把 PROJECT_PLAN.md 对应章节的原文贴进「本次任务」上方。
> 能读文件的编码 agent（Cursor / Claude Code / Trae 等）直接让它读即可。

---

## 2. 任务 A — Day1：基线 + ResearchContext（先跑通这个再往下）

**本次任务**（对应计划 §16 步骤 1-4、§5.1、§3.2）
```
1. 确认基线已跑通：uvicorn 起服务，/health 通，旧的 /report/generate 能出报告。
2. requirements.txt 增加 chromadb、tiktoken 并安装。
3. 新建 app/services/embeddings.py：实现 embed_batch(texts)->list[vector]，
   批量请求 + 失败重试；embedding 走外部 API（配置见 §8.1），
   不要用 ChromaDB 默认本地 onnxruntime 模型（会拖垮 Render 内存）。
4. 新建 app/services/research_context.py，严格按 §5.1 的类签名实现
   add / retrieve / cleanup，以及 chunk_text（300 token/块，15% overlap），
   并实现 ChromaDB 不可用时的降级路径（内存列表 + 关键词检索）。
```
**DoD**
```
- tests/test_research_context.py 通过：add 3 条来源 → retrieve 返回相关块
  （结构含 content/url/site/title）→ cleanup 后 collection 不存在。
- 模拟 ChromaDB 初始化异常时，降级路径也能正常 add/retrieve。
```

---

## 3. 任务 B — Day2：四阶段管道【核心，消灭头号技术债】

**本次任务**（对应计划 §5.2、§5.3、§2.2 第 1 条、§4.2）
```
把「真实搜索数据」接进研报生成，消灭 report_generator.py 靠 LLM 知识编造的问题。
1. 新建 research_engine.py：在 agent.py 并行循环基础上重构为 ResearchEngine，
   每条来源的【完整正文】写入 ResearchContext，元数据写入同一个 CitationManager，
   并 yield research_progress 进度事件。
2. 新建 research_pipeline.py：按 §5.3 串起 Phase 1-4，
   全程只用一个 CitationManager + 一个 ResearchContext，finally 里必须 cleanup。
3. 改 routers/report.py 的 /generate 改调 run_research_pipeline；
   把 report_generator.py 拆成可被 pipeline 调用的子函数（outline / section / abstract）。
4. 写 scripts/run_pipeline_demo.py 跑一次真实课题，输出 docs/pipeline-run-log.md
   （含：搜索耗时、来源数量、每节检索块数与 token 用量、总时长）。
```
**DoD**
```
- 真实课题一次跑通 Phase 1→4，SSE 事件顺序符合 §7.2。
- 研报正文里的 [n] 能对应到真实来源 URL（抽查可打开且内容相关）。
- 同一引用编号在正文与文末参考文献里一致（tests/test_citation_flow.py 通过）。
- 每节 prompt 只注入 retrieve 出的 top_k 片段，不是全量原文。
```

---

## 4. 任务 C — Day3：Manus 双栏 UI + SSE 集成

**本次任务**（对应计划 §4.1、§7.2、附录 A）
```
重写 static/index.html，实现 Manus 风格双栏（保持零构建：单 HTML + CDN marked.js）。
1. CSS Grid 双栏：左栏=课题输入 + 参数(章节数2-8/语言/定向站点复选) + 研究进度流
   + 发送/停止按钮；右栏=标题 + 目录(可点击跳转) + 章节 + 参考文献 + 工具栏。右栏 max-width 800px。
2. 用 fetch + ReadableStream 解析 SSE（不用 EventSource，因为是 POST）。
3. 一个 handleSSEEvent 分发器处理【全部】事件：左栏消费 research_*，
   右栏消费 outline/section_*/abstract/references/report_complete，另处理 warning/error。
4. 右栏 section_chunk 事件触发 marked.js 增量渲染；顶部进度条显示 已完成X/共N节。
```
**DoD**
```
- 浏览器输入课题 → 左栏实时滚动进度日志 → 右栏逐节流式出现 → 参考文献渲染。
- 进度条随 section_start/section_end 正确更新。
- 目录项可点击跳转到对应章节。
```

---

## 5. 任务 D — Day4：划词优化 + 撤销

**本次任务**（对应计划 任务4、附录 A）
```
在右栏接入划词优化交互（后端 /report/refine 已存在，直接调用）。
1. 用 Selection API 监听右栏 mouseup，选中文字(>5字)时在选区附近弹出浮动面板，
   含 5 种风格：更学术严谨 / 更通俗易懂 / 补充数据支撑 / 精简表达 / 英文润色。
2. 点风格 → POST /report/refine（带 selected_text + 前后各约200字上下文 + instruction）
   → 显示加载中 → 收到 refined_text 后替换选区原文。
3. 支持撤销一次：保留 original_text，提供「撤销」入口可还原。
4. 选区消失时浮层自动隐藏；浮层位置做视口边界处理。
```
**DoD**
```
- 选中 → 弹面板 → 选风格 → 替换成功 → 撤销能还原，全流程可演示（截图存档）。
```

---

## 6. 任务 E — Day5：端到端集成 + 错误处理

**本次任务**（对应计划 §11 风险表、§13.3 边界用例、任务5）
```
处理真实使用的边界情况，保证任何异常都不崩、有用户可见提示。
1. 搜索失败降级：定向站点超时或空结果 → 自动降级 Tavily 通用搜索，进度日志明确提示。
2. 最大步数保护：Agent 研究阶段最多 10 步，超限直接进入 Outline，不崩。
3. 单来源超时：单次搜索/抓取 >15s → 记日志、跳过该来源、流程继续。
4. 来源不足：来源 <3 时右栏顶部显示黄条「数据来源较少，内容可能不够全面」。
5. SSE 断开：前端提供「重新生成」兜底；后端任意阶段异常发 error 事件优雅收尾（续传属 best-effort，本次不做）。
```
**DoD**
```
- §13.3 的每一条边界用例逐条实测通过并记录结果。
```

---

## 7. 任务 F — Day6：研报质量提升 + 导出完善

**本次任务**（对应计划 任务6、§7.3、§12.2）
```
1. 强化 SECTION_PROMPT：① 每节至少 1 个具体数据或案例；② 用 Markdown 表格对比多方案/数据；
   ③ 引用用 [n] 格式；④ 注入已完成节的关键句，避免不同节重复同一观点。
2. Markdown 导出：加完整元数据头（课题/生成时间/来源数量/站点列表，格式见 §7.3）
   + 目录 + 正文 + 参考文献；前端加「复制到剪贴板」按钮（零依赖）。
3. PDF 导出：测 weasyprint 在 Render 的可用性；中文字体缺失则用 window.print() 作为降级方案。
```
**DoD**
```
- 导出的 .md 结构完整（元数据头 + 目录 + 正文 + 参考文献）。
- PDF 至少一种方案可用。
- 研报质量按 §12.2 五条抽查标准达标。
```

---

## 8. 任务 G — Day7：部署 + 端到端彩排

**本次任务**（对应计划 §14）
```
1. 部署到 Render：按 §14.1 更新 render.yaml（含 embedding 环境变量、--timeout-keep-alive 120、单 worker）。
2. 端到端彩排：输入课题 → AI 研究 → 流式生成 → 划词优化 → 下载，完整跑一次并录屏。
3. 跑 scripts/verify_week7.py 回归；整理交付物与 README。
```
**DoD**
```
- 按 §14.3 清单验证：/health/deep ok、全流程跑通、导出正常、Render 内存无 OOM、冷启动首请求 <30s。
- OKR 1-6 全绿。
```

---

## 9. 验收与纠偏话术（复制即用）

- **验收**：对着该任务 DoD 逐项打勾。没跑测试就说"完成"的，回一句：
  > "请按 DoD 逐项跑验证并把命令和结果贴出来，未验证不算完成。"

- **契约偏差**（前后端字段/事件名对不上、擅改数据模型）：
  > "这里和 PROJECT_PLAN.md §{X} 的契约不一致（{具体哪个字段/事件}）。请按计划改回，不要改契约。"

- **反复失败**（同一问题改了 2 次还没好）：
  > "这个问题已经改了两次仍未解决。请停下来分析根因：前两次为什么失败？然后换一个根本不同的方案，不要继续打补丁。"（对应计划 §11 止损原则）

- **范围蔓延**（模型自作主张加功能/上数据库）：
  > "本次只做 {任务范围}。计划已明确 Week 7 不引入数据库/不做 {X}（见 §1.3/§6.4），请勿超范围。"
