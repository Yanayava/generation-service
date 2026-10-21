from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.errors import AppError
from app.hmac_util import verify
from app.models import Attempt, LedgerEntry, Leg, Model, Task, User
from app.orchestrator import apply_payment, create_generation, dispatch_loop, settle
from app.schemas import AttemptOut, BalanceOut, GenerateIn, GenerateOut, LedgerOut, LegOut, TaskOut

router = APIRouter()


async def _signed_json(request: Request) -> dict:
    raw = await request.body()
    if not verify(raw, request.headers.get("x-signature", ""), settings.webhook_secret):
        raise AppError(401, "invalid signature")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError(400, "invalid json") from exc
    if not isinstance(data, dict):
        raise AppError(400, "invalid json")
    return data


@router.post("/generate", response_model=GenerateOut, status_code=201)
async def generate(body: GenerateIn) -> GenerateOut:
    task_id = await create_generation(
        user_id=body.user_id,
        model_slug=body.model_slug,
        prompt=body.prompt,
        ref_images=body.ref_images,
    )
    return GenerateOut(task_id=task_id)


@router.post("/webhooks/providers/{name}")
async def provider_webhook(name: str, request: Request) -> dict:
    if name not in {"alpha", "beta"}:
        raise AppError(404, "unknown provider")
    data = await _signed_json(request)
    try:
        attempt_id = UUID(str(data["attempt_id"]))
    except (KeyError, ValueError) as exc:
        raise AppError(400, "attempt_id is required") from exc

    status = data.get("status")
    if status == "completed":
        result_url = data.get("result_url")
        if not isinstance(result_url, str) or not result_url:
            raise AppError(400, "result_url is required")
        result = await settle(
            attempt_id,
            outcome="completed",
            source="webhook",
            result_url=result_url,
            expected_provider=name,
        )
    elif status == "failed":
        error_type = data.get("error_type")
        if error_type not in {"technical", "moderation", "payload"}:
            raise AppError(400, "unsupported error_type")
        result = await settle(
            attempt_id,
            outcome="failed",
            source="webhook",
            error_type=error_type,
            expected_provider=name,
        )
    else:
        raise AppError(400, "unsupported status")

    if not result.found:
        raise AppError(404, "attempt not found")
    if result.dispatch_task_id is not None:
        await dispatch_loop(result.dispatch_task_id)
    return {"status": "ok"}


@router.post("/webhooks/payments")
async def payment_webhook(request: Request) -> dict:
    data = await _signed_json(request)
    try:
        payment_id = str(data["payment_id"]).strip()
        user_id = UUID(str(data["user_id"]))
        status = str(data["status"])
    except (KeyError, ValueError) as exc:
        raise AppError(400, "payment_id, user_id and status are required") from exc
    if not payment_id:
        raise AppError(400, "payment_id is required")
    tokens = data.get("tokens")
    if isinstance(tokens, bool) or not isinstance(tokens, int | float) or int(tokens) != tokens:
        raise AppError(422, "tokens must be a positive integer")
    outcome = await apply_payment(
        payment_id=payment_id,
        user_id=user_id,
        tokens=int(tokens),
        status=status,
    )
    return {"status": outcome}


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: UUID) -> TaskOut:
    async with session_scope() as session:
        async with session.begin():
            task = await session.get(Task, task_id)
            if task is None:
                raise AppError(404, "task not found")
            model = await session.get(Model, task.model_id)
            leg = await session.get(Leg, task.current_leg_id) if task.current_leg_id else None
            attempts = (
                (
                    await session.execute(
                        select(Attempt).where(Attempt.task_id == task.id).order_by(Attempt.started_at, Attempt.id)
                    )
                )
                .scalars()
                .all()
            )
            return TaskOut(
                id=task.id,
                status=task.status,
                model_slug=model.slug if model else "",
                prompt=task.prompt,
                price=task.price,
                ref_images=list(task.ref_images or []),
                result_url=task.result_url,
                failure_reason=task.failure_reason,
                current_leg=(
                    LegOut(
                        provider=leg.provider,
                        priority=leg.priority,
                        max_ref_images=leg.max_ref_images,
                        skip_on_moderation=leg.skip_on_moderation,
                    )
                    if leg
                    else None
                ),
                attempts=[AttemptOut.model_validate(item) for item in attempts],
            )


@router.get("/users/{user_id}/balance", response_model=BalanceOut)
async def get_balance(user_id: UUID) -> BalanceOut:
    async with session_scope() as session:
        async with session.begin():
            user = await session.get(User, user_id)
            if user is None:
                raise AppError(404, "user not found")
            rows = (
                (
                    await session.execute(
                        select(LedgerEntry)
                        .where(LedgerEntry.user_id == user_id)
                        .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
            return BalanceOut(
                user_id=user.id,
                balance=user.balance,
                ledger=[LedgerOut.model_validate(row) for row in rows],
            )
