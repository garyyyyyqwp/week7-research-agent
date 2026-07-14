"""打包下一个 DeepSeek 会话所需的上下文。

用法:
  python scripts/pack_context.py C  # 为任务 C 打包

输出:
  context_for_task_C.md  — 可直接粘贴给 DeepSeek 的完整 prompt

原理:
  任务依赖的文件(关键接口、SSE协议、数据结构)读成文字放进 prompt，
  不依赖的(深层的内部实现)不包含，平衡完整性与长度。
"""

import sys
from pathlib import Path

# 每个任务需要读哪些文件
TASK_CONTEXT = {
    "C": {  # 前端双栏 + SSE
        "why": "前端需要知道后端的 SSE 事件名和字段结构",
        "files": [
            ("app/services/research_pipeline.py", None),
            ("app/routers/report.py", None),
        ],
        "plan_sections": ["4.1", "7.2", "9 Day3"],
        "handoff_block": "任务C — Day3",
    },
    "D": {  # 划词优化
        "why": "前端需要知道 /refine 接口的请求/响应格式",
        "files": [
            ("app/routers/report.py", "refine"),
            ("app/schemas/report.py", "Refine"),
        ],
        "plan_sections": ["9 Day4"],
        "handoff_block": "任务D — Day4",
    },
    "E": {  # 容错
        "why": "需要理解管道结构和已知的异常路径",
        "files": [
            ("app/services/research_pipeline.py", "pipeline"),
            ("app/services/research_engine.py", "max_steps"),
        ],
        "plan_sections": ["9 Day5", "11"],
        "handoff_block": "任务E — Day5",
    },
    "F": {  # 质量+导出
        "why": "需要理解 prompt 模板和导出链路",
        "files": [
            ("app/services/report_generator.py", "SECTION_PROMPT"),
            ("app/routers/report.py", "export"),
        ],
        "plan_sections": ["7.3", "9 Day6", "12.2"],
        "handoff_block": "任务F — Day6",
    },
    "G": {  # 部署
        "why": "需要知道依赖和环境配置",
        "files": [
            ("requirements.txt", None),
            ("render.yaml", None),
        ],
        "plan_sections": ["9 Day7", "14"],
        "handoff_block": "任务G — Day7",
    },
}

HEADER = """# 任务 {task} 的完整上下文 — 直接粘贴给 DeepSeek（放到对话第一条）

**先粘贴本文件全部内容，然后可以说："开始执行，遵守上述铁律和 DoD。"**

---

## 重要说明

为什么要打包这个："
  - 你在不同会话分别做了任务 A(ResearchContext)、B(管道核心)
  - 现在开新会话做任务 {task}，新模型没有上会话的记忆
  - 所以我把本项目计划的关联章节、PLAN.md 抽取精要、后端关键接口、
    以及 HANDOFF_PROMPTS.md 的任务块，统一打包成一个 prompt
  - 确保新模型获得与上会话一致的事实，不凭记忆猜测
"""


def extract_section(text: str, keyword: str) -> str:
    """Try to extract a focused section around keyword from a file."""
    lines = text.splitlines()
    results = []
    capture = False
    cap_count = 0
    for i, line in enumerate(lines):
        if keyword.lower() in line.lower():
            capture = True
            cap_count = 30  # capture 30 lines around each match
        if capture:
            results.append(line)
            cap_count -= 1
            if cap_count <= 0:
                capture = False
    return "\n".join(results[:100]) if results else "(全文见上文)"


def main():
    args = sys.argv[1:]
    if not args or args[0] not in TASK_CONTEXT:
        print("用法: python scripts/pack_context.py [任务字母]")
        print(f"可用: {', '.join(sorted(TASK_CONTEXT.keys()))}")
        sys.exit(1)

    task = args[0]
    ctx = TASK_CONTEXT[task]
    out = Path("context_for_task_{task}.md")

    lines = [HEADER.format(task=task)]

    # 1. HANDOFF_PROMPTS 的任务块
    hf = Path("HANDOFF_PROMPTS.md")
    if hf.exists():
        content = hf.read_text(encoding="utf-8")
        # find the task block
        marker = f"## {ctx['handoff_block']}"
        if marker in content:
            lines.append("## 任务描述（来自 HANDOFF_PROMPTS.md）")
            start = content.index(marker)
            end = content.find("\n## ", start + 5)
            end = end if end > start else len(content)
            lines.append(content[start:end])
            lines.append("")

    # 2. PLAN 关键章节摘录
    plan = Path("PROJECT_PLAN.md")
    if plan.exists():
        ptext = plan.read_text(encoding="utf-8")
        lines.append(f"## 关联的 PLAN 章节: {', '.join(ctx['plan_sections'])}")
        for section_ref in ctx["plan_sections"]:
            # Try to extract
            marker = f"## {section_ref}"
            if marker in ptext:
                start = ptext.index(marker)
                # Get next ## or end
                rest = ptext[start + len(marker):]
                end = rest.find("\n## ")
                end = end + len(marker) + len(rest[:end]) if end > 0 else len(ptext)
                lines.append(f"### §{section_ref}")
                lines.append(ptext[start:start + end].strip())
                lines.append("")
        lines.append("")

    # 3. 关键接口文件
    lines.append("## 本次任务依赖的关键接口（从源代码读取）")
    for fpath_str, keyword in ctx["files"]:
        fpath = Path(fpath_str)
        if fpath.exists():
            text = fpath.read_text(encoding="utf-8")
            lines.append(f"### 文件: {fpath}")
            if keyword:
                extracted = extract_section(text, keyword)
                lines.append(extracted if extracted else "(keyword not found)")
            else:
                lines.append(text)
            lines.append("")

    output = "\n".join(lines)
    outpath = Path(f"context_for_task_{task}.md")
    outpath.write_text(output, encoding="utf-8")
    print(f"[ok] 已生成 {outpath} ({len(output)} 字符)")
    print(f"    粘贴到 DeepSeek 新会话的第一条消息即可。")


if __name__ == "__main__":
    main()
