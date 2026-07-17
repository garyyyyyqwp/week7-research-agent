"""Pipeline Demo — 跑一次真实课题，输出运行日志。

Usage:
    python scripts/run_pipeline_demo.py

Reads .env for API keys. Runs the full 4-phase pipeline on a real topic
and writes a structured run log (docs/pipeline-run-log.md) with:
  - 搜索耗时、来源数量
  - 每节检索块数与 token 用量
  - 总时长

This is the verification script for Task B DoD ("真实课题一次跑通 Phase 1→4").
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.research_pipeline import run_research_pipeline


# Default demo topic — from PROJECT_PLAN.md examples
DEMO_TOPIC = "Long COVID 的神经系统影响"

DEMO_PARAMS = {
    "num_sections": 4,
    "language": "zh-CN",
    "enabled_sites": ["pubmed", "who"],
}


async def main():
    """Run the pipeline demo and produce the log."""
    report_id = f"demo_{int(time.time())}"
    start = time.monotonic()

    print(f"🚀 启动管道演示...")
    print(f"   课题: {DEMO_TOPIC}")
    print(f"   章节数: {DEMO_PARAMS['num_sections']}, 定向站点: {DEMO_PARAMS['enabled_sites']}")
    print(f"   Report ID: {report_id}")
    print()

    # Collect events
    events: list[dict] = []
    research_progress_count = 0
    section_chunk_count = 0
    total_sources = 0
    research_elapsed = 0.0      # ★ 搜索耗时（研究阶段）
    chunks_stored = 0           # ★ 入库 chunk 总数
    hit_max_steps = False
    used_sites: list[str] = []
    sections_data: list[dict] = []
    section_start_ts: dict[int, float] = {}
    warnings: list[str] = []
    errors: list[str] = []

    async for event in run_research_pipeline(
        report_id=report_id,
        topic=DEMO_TOPIC,
        **DEMO_PARAMS,
    ):
        events.append(event)

        ev_type = event.get("event", "")
        ev_data = event.get("data", "{}")

        try:
            data = json.loads(ev_data)
        except (json.JSONDecodeError, TypeError):
            data = {}

        if ev_type == "research_progress":
            research_progress_count += 1
            msg = data.get("message", "")[:100]
            icon = data.get("icon", "")
            print(f"  {icon} {msg}")

        elif ev_type == "research_done":
            total_sources = data.get("sources", 0)
            research_elapsed = data.get("elapsed_s", 0)
            chunks_stored = data.get("chunks_stored", 0)
            hit_max_steps = data.get("hit_max_steps", False)
            print(f"\n📊 研究阶段完成: {total_sources} 个来源, "
                  f"{chunks_stored} 个入库块, {research_elapsed:.1f}s")

        elif ev_type == "warning":
            warnings.append(data.get("message", data.get("code", "")))
            print(f"\n⚠️  警告: {data.get('message', data.get('code', ''))}")

        elif ev_type == "outline":
            outline_count = data.get("count", 0)
            sections_list = data.get("sections", [])
            section_titles = [s.get("title", "?") for s in sections_list]
            print(f"\n📋 大纲 ({outline_count} 节):")
            for i, t in enumerate(section_titles, 1):
                print(f"    {i}. {t}")

        elif ev_type == "section_start":
            idx = data.get("index", 0) + 1
            total = data.get("total", 0)
            title = data.get("title", "")
            section_start_ts[data.get("index", 0)] = time.monotonic()
            print(f"\n✍️  撰写第 {idx}/{total} 节: {title}")

        elif ev_type == "section_chunk":
            section_chunk_count += 1

        elif ev_type == "section_end":
            raw_idx = data.get("index", 0)
            idx = raw_idx + 1
            title = data.get("title", "")
            citations = data.get("citations", [])
            content_len = len(data.get("content", ""))
            sec_elapsed = time.monotonic() - section_start_ts.get(raw_idx, start)
            sections_data.append({
                "index": idx,
                "title": title,
                "content_length": content_len,
                "citations": citations,
                "retrieved_chunks": data.get("retrieved_chunks", 0),
                "elapsed_s": round(sec_elapsed, 1),
            })
            print(f"    ✅ 完成 ({sec_elapsed:.1f}s, 长度: {content_len} 字, "
                  f"检索块: {data.get('retrieved_chunks', 0)}, 引用: {citations})")

        elif ev_type == "abstract":
            abstract_len = len(data.get("abstract", ""))
            print(f"\n📝 摘要: {abstract_len} 字")

        elif ev_type == "references":
            refs = data.get("references", "")
            ref_count = data.get("citations_json", {}).get("count", 0)
            print(f"\n📚 参考文献: {ref_count} 条")

        elif ev_type == "report_complete":
            print(f"\n✅ 报告完成! report_id={data.get('report_id', '?')}")

        elif ev_type == "done":
            total_elapsed = data.get("elapsed_s", 0)
            print(f"\n🎉 管道完成! 总耗时: {total_elapsed:.1f}s")

        elif ev_type == "error":
            errors.append(data.get("message", str(data)))
            print(f"\n❌ 错误: {data.get('message', '')} ({data.get('phase', '')})")

    total_elapsed = time.monotonic() - start

    # --- Write structured run log ---
    log_path = Path(__file__).resolve().parent.parent / "docs" / "pipeline-run-log.md"
    log_path.parent.mkdir(exist_ok=True)

    # Estimate token usage (rough: 4 chars per token for Chinese)
    total_tokens_est = sum(
        s.get("content_length", 0) // 4 for s in sections_data
    )

    log_content = f"""# Pipeline Run Log

> 自动生成 — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}

## 运行参数
| 参数 | 值 |
|---|---|
| 课题 | {DEMO_TOPIC} |
| 章节数 | {DEMO_PARAMS['num_sections']} |
| 语言 | {DEMO_PARAMS['language']} |
| 定向站点 | {', '.join(DEMO_PARAMS['enabled_sites']) or '无 (仅 Tavily)'} |
| Report ID | {report_id} |

## 耗时统计
| 指标 | 值 |
|---|---|
| 总耗时 | {total_elapsed:.1f}s |
| 搜索耗时 (Phase 1 研究阶段) | {research_elapsed:.1f}s |
| 写作+后处理耗时 (Phase 2-4) | {total_elapsed - research_elapsed:.1f}s |
| 研究阶段事件数 | {research_progress_count} |
| Section chunk 事件数 | {section_chunk_count} |
| 达到最大步数保护 | {'是' if hit_max_steps else '否'} |

## 来源统计
| 指标 | 值 |
|---|---|
| 收集来源数 | {total_sources} |
| 入库 chunk 总数 | {chunks_stored} |
| 警告 | {('; '.join(warnings)) if warnings else '无'} |

## 章节详情
| # | 标题 | 耗时 (s) | 长度 (字) | 检索块数 | Token 估算 | 引用 |
|---|---|---|---|---|---|---|
"""

    for s in sections_data:
        tokens_est = s["content_length"] // 4
        cites_str = ", ".join(f"[{c}]" for c in s.get("citations", []))
        log_content += (
            f"| {s['index']} | {s['title']} | {s.get('elapsed_s', '?')} | "
            f"{s['content_length']} | {s.get('retrieved_chunks', 0)} | "
            f"~{tokens_est} | {cites_str} |\n"
        )

    log_content += f"""
## Token 用量估算
| 指标 | 值 |
|---|---|
| 正文总计 (字) | {sum(s['content_length'] for s in sections_data)} |
| 正文总计 (token 估算) | ~{total_tokens_est} |

## 错误
| 数量 | 详情 |
|---|---|
| {len(errors)} | {'无' if not errors else chr(10).join(f'- {e}' for e in errors)} |

---
*本日志由 scripts/run_pipeline_demo.py 自动生成 (Project Plan §5.3)*
"""

    log_path.write_text(log_content, encoding="utf-8")
    print(f"\n📄 运行日志已保存: {log_path}")

    # Final summary
    print(f"\n{'='*60}")
    print(f"总结:")
    print(f"  - 来源数: {total_sources}")
    print(f"  - 章节数: {len(sections_data)}")
    print(f"  - 总耗时: {total_elapsed:.1f}s")
    print(f"  - 错误: {len(errors)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
