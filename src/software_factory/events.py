from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EventStatus = Literal["started", "completed", "failed", "cancelled", "info"]


class FactoryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    run_id: str
    sequence: int = 0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    node: str
    status: EventStatus
    attempt: int = 1
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)


EventSink = Callable[[FactoryEvent], Awaitable[None]]


async def null_event_sink(event: FactoryEvent) -> None:
    del event
