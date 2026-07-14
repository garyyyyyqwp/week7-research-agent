"""Agent Schemas — Pydantic models for agent API."""

from pydantic import BaseModel, Field


class AgentChatRequest(BaseModel):
    """Request for agent chat endpoint."""

    question: str = Field(
        ..., min_length=1, max_length=5000,
        description="研究问题",
    )
    max_steps: int = Field(
        default=10, ge=1, le=20,
        description="最大推理步数",
    )
