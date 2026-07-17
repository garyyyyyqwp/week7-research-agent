"""轻量级内存限流 — 无外部依赖，单 worker 部署场景够用。

/generate、/refine、/agent/chat 都是无鉴权且触发付费 LLM/搜索调用的端点，
公网部署时任何人（或任意网页借访客浏览器）都能刷额度。这里提供：
  1. rate_limit(n, window) —— 每 IP 滑动窗口限流依赖项
  2. concurrency_guard(n) —— 全局并发上限（防止多条研报管道挤爆 512MB 实例）

单 worker 内存实现的边界：多 worker/多实例不共享计数（本项目 startCommand
固定 --workers 1，见 render.yaml）。
"""

import asyncio
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_BUCKETS: dict[str, deque] = defaultdict(deque)
_MAX_TRACKED_IPS = 10_000  # 防内存膨胀的粗粒度兜底


def _client_ip(request: Request) -> str:
    """取真实客户端 IP（Render 反代后在 X-Forwarded-For 首位）。"""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(max_calls: int, window_s: int = 60):
    """FastAPI dependency: 每 IP 在 window_s 秒内最多 max_calls 次。"""

    async def _dep(request: Request) -> None:
        if len(_BUCKETS) > _MAX_TRACKED_IPS:
            _BUCKETS.clear()

        ip = _client_ip(request)
        now = time.monotonic()
        q = _BUCKETS[ip]
        while q and now - q[0] > window_s:
            q.popleft()
        if len(q) >= max_calls:
            raise HTTPException(
                status_code=429,
                detail=f"请求过于频繁，请 {window_s} 秒后再试",
            )
        q.append(now)

    return _dep


class ConcurrencyGuard:
    """全局并发上限（非阻塞：超限直接 429，不排队）。"""

    def __init__(self, max_concurrent: int):
        self.max_concurrent = max_concurrent
        self._active = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            if self._active >= self.max_concurrent:
                raise HTTPException(
                    status_code=429,
                    detail="当前生成任务较多，请稍后再试",
                )
            self._active += 1

    async def release(self) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)


# 研报生成管道全局并发上限（每条管道占用大量内存 + LLM/搜索配额）
generation_guard = ConcurrencyGuard(max_concurrent=2)
