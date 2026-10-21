from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GenerateIn(BaseModel):
    user_id: UUID
    model_slug: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    ref_images: list[str] = Field(default_factory=list)


class GenerateOut(BaseModel):
    task_id: UUID


class LegOut(BaseModel):
    provider: str
    priority: int
    max_ref_images: int
    skip_on_moderation: bool


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: str
    status: str
    error_type: str | None
    cause: str | None
    external_id: str | None
    result_url: str | None
    late_result_url: str | None
    late_received_at: datetime | None
    started_at: datetime
    finished_at: datetime | None


class TaskOut(BaseModel):
    id: UUID
    status: str
    model_slug: str
    prompt: str
    price: int
    ref_images: list[str]
    result_url: str | None
    failure_reason: str | None
    current_leg: LegOut | None
    attempts: list[AttemptOut]


class LedgerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    amount: int
    reason: str
    idempotency_key: str
    task_id: UUID | None
    payment_id: str | None
    balance_after: int | None
    created_at: datetime


class BalanceOut(BaseModel):
    user_id: UUID
    balance: int
    ledger: list[LedgerOut]
