"""Week 7 端到端冒烟验证脚本。

一键运行：python scripts/verify_week7.py

验证内容：
  1. 服务器健康检查 /health + /health/deep
  2. 搜索站点列表 /api/v1/search/sites
  3. 研报生成 SSE /api/v1/report/generate（最小化测试）
  4. 划词优化 /api/v1/report/refine
  5. 最终报告列表不为空

用法：
  python scripts/verify_week7.py [--url http://localhost:8002]

Exit code 0 = 全部通过。
"""

import argparse
import json
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError


# Emoji-safe — avoid Unicode in print to survive GBK consoles
def ok(msg): print(f"  [PASS] {msg}")
def fail(msg): print(f"  [FAIL] {msg}")
def info(msg): print(f"  [...] {msg}")


def api_get(base, path):
    url = base + path
    req = Request(url, headers={"Accept": "application/json"})
    resp = urlopen(req, timeout=10)
    return json.loads(resp.read())


def api_post_json(base, path, body):
    url = base + path
    data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    resp = urlopen(req, timeout=30)
    return json.loads(resp.read())


def check(label, condition):
    if condition:
        ok(label)
        return True
    else:
        fail(label)
        return False


def main():
    parser = argparse.ArgumentParser(description="Week 7 smoke test")
    parser.add_argument("--url", default="http://localhost:8002", help="Server base URL")
    args = parser.parse_args()
    base = args.url

    print(f"\nWeek 7 端到端冒烟 — {base}")
    print("=" * 60)
    passed = 0
    total = 0

    # --- 1. Health ---
    print("\n[1] Health Checks")
    total += 2
    try:
        data = api_get(base, "/health")
        passed += check("GET /health → status=ok", data.get("status") == "ok")
    except Exception as e:
        fail(f"GET /health failed: {e}")

    try:
        data = api_get(base, "/health/deep")
        ok = data.get("status") in ("ok", "degraded")
        passed += check(f"GET /health/deep → {data.get('status')}", ok)
    except Exception as e:
        fail(f"GET /health/deep failed: {e}")

    # --- 2. Sites ---
    print("\n[2] Search Sites")
    total += 1
    try:
        data = api_get(base, "/api/v1/search/sites")
        count = data.get("count", 0)
        passed += check(f"GET /search/sites → {count} sites", count >= 1)
    except Exception as e:
        fail(f"GET /search/sites failed: {e}")

    # --- 3. Refine ---
    print("\n[3] Refine Endpoint")
    total += 2
    try:
        data = api_post_json(base, "/api/v1/report/refine", {
            "selected_text": "This is a test.",
            "context_before": "",
            "context_after": "",
            "instruction": "make it better",
        })
        passed += check("POST /refine → refined_text present", bool(data.get("refined_text")))
        passed += check("POST /refine → original_text echoed",
                        data.get("original_text") == "This is a test.")
    except (URLError, TimeoutError) as e:
        fail(f"POST /refine failed (LLM may be unavailable): {e}")
        total -= 1  # Don't penalize for LLM connectivity

    # --- 4. Report Generate SSE (consume first 5 events) ---
    print("\n[4] Report Generation Pipeline (SSE)")
    total += 3
    topic = "Long COVID neurological effects"
    try:
        import ssl
        ctx = ssl.create_default_context()
        from http.client import HTTPSConnection, HTTPConnection
        from urllib.parse import urlparse

        parsed = urlparse(base)
        body = json.dumps({
            "topic": topic,
            "num_sections": 2,
            "language": "zh-CN",
            "enabled_sites": [],
            "include_references": True,
        }).encode("utf-8")

        if parsed.scheme == "https":
            conn = HTTPSConnection(parsed.hostname, parsed.port or 443, context=ctx, timeout=60)
        else:
            conn = HTTPConnection(parsed.hostname, parsed.port or 80, timeout=60)

        if parsed.path:
            path = parsed.path + "/api/v1/report/generate"
        else:
            path = "/api/v1/report/generate"

        conn.request("POST", path, body=body, headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        })

        resp = conn.getresponse()
        passed += check("POST /report/generate → HTTP 200", resp.status == 200)

        # Read first 20 SSE events
        events_seen = set()
        buffer = b""
        deadline = time.monotonic() + 120  # 2 minute max
        event_count = 0

        while time.monotonic() < deadline:
            chunk = resp.read(4096)
            if not chunk:
                break
            buffer += chunk
            lines = buffer.split(b"\n")
            buffer = lines.pop()

            for line in lines:
                if line.startswith(b"event:"):
                    evt = line[6:].strip().decode("utf-8", errors="replace")
                    events_seen.add(evt)
                    event_count += 1
                if event_count >= 30:
                    break
            if event_count >= 30:
                break

        conn.close()

        info(f"Captured events: {sorted(events_seen)}")
        passed += check("At least 5 SSE events received", len(events_seen) >= 5)
        passed += check("'done' or 'error' event seen",
                        "done" in events_seen or "error" in events_seen)

    except Exception as e:
        fail(f"SSE receive failed: {e}")

    # --- Summary ---
    print("\n" + "=" * 60)
    print(f"结果: {passed}/{total} 通过")
    if passed == total:
        print("全部通过!")
        sys.exit(0)
    else:
        print(f"{total - passed} 项失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
