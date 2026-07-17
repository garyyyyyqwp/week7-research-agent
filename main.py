"""Week 7: 研究报告智能体 — FastAPI 入口

核心路由：
  POST /api/v1/search/web       — 真实联网搜索 (Tavily)
  POST /api/v1/search/fetch     — 网页正文提取 (Jina Reader)
  POST /api/v1/search/site      — 定向站点抓取 (SiteRegistry)
  POST /api/v1/agent/chat       — ReAct Agent (并行 tool_calls + 引用追踪)
  POST /api/v1/report/generate  — 四阶段管道 SSE 流式生成 (Week 7 核心)
  POST /api/v1/report/refine    — 划词优化
  GET  /api/v1/report/{id}/export — 文档导出
"""

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.routers import search, agent, report
from app.utils.config import OPENAI_API_KEY

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="研究报告智能体",
    description="Week 7 — 四阶段研究管道 + Manus 双栏 UI",
    version="0.2.0",
)

# CORS：前端与 API 同源部署，默认不需要跨域；如需允许其他前端来源，
# 用逗号分隔写入 CORS_ALLOW_ORIGINS 环境变量。不再使用 allow_origins=["*"]
# （无鉴权 + 通配 CORS = 任何网页都能借访客浏览器消耗付费 API 配额）
_cors_origins = [
    o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Add X-Request-ID header and log each request."""
    import uuid
    request_id = uuid.uuid4().hex[:12]
    logger.info("[%s] %s %s", request_id, request.method, request.url.path)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response

app.include_router(search.router, prefix="/api/v1/search")
app.include_router(agent.router, prefix="/api/v1/agent")
app.include_router(report.router, prefix="/api/v1/report")

STATIC_DIR = Path(__file__).parent / "static"

# 提供 /static/vendor/*（本地化的 marked/DOMPurify）与内置字体等静态资源
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    """Serve the demo frontend."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    """Basic health check."""
    return {"status": "ok"}


@app.get("/health/deep")
async def health_deep():
    """Deep health check — verifies connectivity to external services."""
    import time as _time
    checks = {}

    # Check configs loaded
    checks["config"] = bool(OPENAI_API_KEY)

    # Quick connectivity check to the configured LLM base URL
    import socket
    from urllib.parse import urlparse
    from app.utils.config import OPENAI_BASE_URL

    llm_host = urlparse(OPENAI_BASE_URL).hostname or "open.bigmodel.cn"
    try:
        t0 = _time.monotonic()
        socket.getaddrinfo(llm_host, 443, proto=socket.IPPROTO_TCP)
        checks["llm_connectivity"] = {
            "status": "ok",
            "host": llm_host,
            "latency_ms": round((_time.monotonic() - t0) * 1000),
        }
    except Exception as e:
        checks["llm_connectivity"] = {"status": "error", "host": llm_host, "detail": str(e)}

    healthy = all(
        v if isinstance(v, bool) else v.get("status") != "error"
        for v in checks.values()
    )

    return {"status": "ok" if healthy else "degraded", "checks": checks}
