"""Agent Router — ReAct agent with parallel tool calls + citation tracking."""

import json
import uuid

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from app.schemas.agent import AgentChatRequest
from app.services.agent import run_agent_stream
from app.utils.ratelimit import rate_limit

router = APIRouter(tags=["agent"])


@router.post("/chat", dependencies=[Depends(rate_limit(10, 60))])
async def agent_chat(request: AgentChatRequest):
    """Agent chat with SSE streaming.

    Events:
        thought: Agent reasoning
        action: Tool call
        observation: Tool result
        answer_chunk: Streaming answer token
        answer: Final answer
        citations: Reference list
        perf: Performance stats (serial vs parallel)
        done: Session summary
    """
    session_id = uuid.uuid4().hex[:12]

    async def event_generator():
        async for event in run_agent_stream(
            question=request.question,
            session_id=session_id,
            max_steps=request.max_steps,
        ):
            yield event

    return EventSourceResponse(event_generator())
