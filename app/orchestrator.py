from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import session_scope
from app.errors import AppError
from app.models import Attempt, LedgerEntry, Leg, Model, Payment, Task, User
from app.provider_client import SyncProviderError, submit
from app.routing import LegChoice, select_leg

logger = logging.getLogger("app.orchestrator")

OPEN_TASK = ("dispatching", "waiting")
OPEN_ATTEMPT = ("sending", "waiting")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SettleResult:
    found: bool = False
    dispatch_task_id: uuid.UUID | None = None


def _is_unique_violation(exc: IntegrityError) -> bool:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) == "23505"


async def _ensure_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(pg_insert(User).values(id=user_id).on_conflict_do_nothing(index_elements=[User.id]))


async def _lock_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    result = await session.execute(select(User).where(User.id == user_id).with_for_update())
    return result.scalar_one()


async def _legs(session: AsyncSession, model_id: uuid.UUID) -> list[LegChoice]:
    rows = (await session.execute(select(Leg).where(Leg.model_id == model_id))).scalars().all()
    return [
        LegChoice(
            id=row.id,
            provider=row.provider,
            priority=row.priority,
            max_ref_images=row.max_ref_images,
            skip_on_moderation=row.skip_on_moderation,
        )
        for row in rows
    ]


async def _tried(session: AsyncSession, task_id: uuid.UUID) -> set[uuid.UUID]:
    rows = (await session.execute(select(Attempt.leg_id).where(Attempt.task_id == task_id))).scalars().all()
    return set(rows)


async def _insert_ledger(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    amount: int,
    reason: str,
    key: str,
    task_id: uuid.UUID | None = None,
    payment_id: str | None = None,
) -> None:
    # Баланс меняет триггер на эту вставку. Объект User в сессии после неё устаревает.
    session.add(
        LedgerEntry(
            id=uuid.uuid4(),
            user_id=user_id,
            amount=amount,
            reason=reason,
            idempotency_key=key,
            task_id=task_id,
            payment_id=payment_id,
        )
    )
    await session.flush()


async def _open_attempt(session: AsyncSession, task: Task, leg: LegChoice) -> Attempt:
    now = utcnow()
    attempt = Attempt(
        id=uuid.uuid4(),
        task_id=task.id,
        leg_id=leg.id,
        provider=leg.provider,
        status="sending",
        started_at=now,
    )
    session.add(attempt)
    await session.flush()
    task.current_leg_id = leg.id
    task.current_attempt_id = attempt.id
    task.status = "dispatching"
    task.updated_at = now
    return attempt


async def _fail_and_refund(session: AsyncSession, task: Task, *, reason: str, now: datetime) -> None:
    task.status = "failed"
    task.failure_reason = reason
    task.result_url = None
    task.updated_at = now
    try:
        async with session.begin_nested():
            await _insert_ledger(
                session,
                user_id=task.user_id,
                amount=task.price,
                reason="refund",
                key=f"refund:{task.id}",
                task_id=task.id,
            )
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise
        logger.info("refund already recorded task=%s", task.id)


async def create_generation(
    *,
    user_id: uuid.UUID,
    model_slug: str,
    prompt: str,
    ref_images: list[str],
) -> uuid.UUID:
    async with session_scope() as session:
        async with session.begin():
            model = (
                await session.execute(select(Model).where(Model.slug == model_slug))
            ).scalar_one_or_none()
            if model is None:
                raise AppError(404, "model not found")

            leg = select_leg(
                await _legs(session, model.id),
                ref_count=len(ref_images),
                tried=set(),
                previous_error=None,
            )
            if leg is None:
                raise AppError(400, "no suitable leg")

            await _ensure_user(session, user_id)
            user = await _lock_user(session, user_id)
            active = (
                await session.execute(
                    select(func.count())
                    .select_from(Task)
                    .where(Task.user_id == user_id, Task.status.in_(OPEN_TASK))
                )
            ).scalar_one()
            if active >= 3:
                raise AppError(429, "too many active tasks")
            if user.balance < model.price:
                raise AppError(402, "insufficient tokens")

            task = Task(
                id=uuid.uuid4(),
                user_id=user_id,
                model_id=model.id,
                prompt=prompt,
                ref_images=list(ref_images),
                price=model.price,
                status="dispatching",
                updated_at=utcnow(),
            )
            session.add(task)
            await session.flush()
            await _insert_ledger(
                session,
                user_id=user_id,
                amount=-model.price,
                reason="charge",
                key=f"charge:{task.id}",
                task_id=task.id,
            )
            await _open_attempt(session, task, leg)
            task_id = task.id

    try:
        await dispatch_loop(task_id)
    except Exception:
        logger.exception("dispatch failed after charge task=%s", task_id)
    return task_id


async def claim_for_http(session: AsyncSession, task_id: uuid.UUID) -> tuple[uuid.UUID, str, dict] | None:
    task = (
        await session.execute(select(Task).where(Task.id == task_id).with_for_update())
    ).scalar_one_or_none()
    if task is None or task.status != "dispatching" or task.current_attempt_id is None:
        return None
    attempt = (
        await session.execute(select(Attempt).where(Attempt.id == task.current_attempt_id).with_for_update())
    ).scalar_one()
    if attempt.status != "sending" or attempt.dispatch_token is not None:
        return None
    attempt.dispatch_token = uuid.uuid4()
    await session.flush()
    return (
        attempt.id,
        attempt.provider,
        {
            "attempt_id": str(attempt.id),
            "task_id": str(task.id),
            "prompt": task.prompt,
            "ref_images": list(task.ref_images or []),
        },
    )


async def dispatch_loop(task_id: uuid.UUID) -> None:
    while True:
        async with session_scope() as session:
            async with session.begin():
                claimed = await claim_for_http(session, task_id)
                if claimed is None:
                    return
                attempt_id, provider, payload = claimed

        try:
            external_id = await submit(provider, payload)
        except SyncProviderError as exc:
            result = await settle(
                attempt_id,
                outcome="failed",
                error_type=exc.kind,
                source="sync",
            )
            if result.dispatch_task_id is None:
                return
            continue

        await mark_submitted(attempt_id, external_id)


async def mark_submitted(attempt_id: uuid.UUID, external_id: str) -> None:
    async with session_scope() as session:
        async with session.begin():
            task_id = (
                await session.execute(select(Attempt.task_id).where(Attempt.id == attempt_id))
            ).scalar_one_or_none()
            if task_id is None:
                return
            task = (
                await session.execute(select(Task).where(Task.id == task_id).with_for_update())
            ).scalar_one()
            attempt = (
                await session.execute(select(Attempt).where(Attempt.id == attempt_id).with_for_update())
            ).scalar_one()
            if attempt.external_id is None:
                attempt.external_id = external_id
            if attempt.status != "sending" or task.current_attempt_id != attempt.id or task.status != "dispatching":
                return
            now = utcnow()
            attempt.status = "waiting"
            attempt.deadline_at = now + timedelta(seconds=settings.provider_timeout)
            task.status = "waiting"
            task.updated_at = now


async def settle(
    attempt_id: uuid.UUID,
    *,
    outcome: str,
    source: str,
    error_type: str | None = None,
    result_url: str | None = None,
    expected_provider: str | None = None,
) -> SettleResult:
    async with session_scope() as session:
        async with session.begin():
            return await settle_attempt(
                session,
                attempt_id,
                outcome=outcome,
                source=source,
                error_type=error_type,
                result_url=result_url,
                expected_provider=expected_provider,
            )


async def settle_attempt(
    session: AsyncSession,
    attempt_id: uuid.UUID,
    *,
    outcome: str,
    source: str,
    error_type: str | None = None,
    result_url: str | None = None,
    expected_provider: str | None = None,
) -> SettleResult:
    """Единственное решение по попытке.

    Вебхук, синхронный обрыв и сторож обязаны сойтись сюда.
    Деньги двигаются только если эта транзакция первой закрывает задачу в failed.
    """
    task_id = (
        await session.execute(select(Attempt.task_id).where(Attempt.id == attempt_id))
    ).scalar_one_or_none()
    if task_id is None:
        return SettleResult(found=False)

    task = (await session.execute(select(Task).where(Task.id == task_id).with_for_update())).scalar_one()
    attempt = (
        await session.execute(select(Attempt).where(Attempt.id == attempt_id).with_for_update())
    ).scalar_one()
    if expected_provider is not None and attempt.provider != expected_provider:
        raise AppError(409, "provider mismatch")

    now = utcnow()
    is_current = (
        task.current_attempt_id == attempt.id
        and task.status in OPEN_TASK
        and attempt.status in OPEN_ATTEMPT
    )
    if not is_current:
        _note_late_success(task, attempt, outcome, result_url, now)
        logger.info(
            "ignore stale attempt=%s task=%s status=%s outcome=%s source=%s",
            attempt.id,
            task.id,
            task.status,
            outcome,
            source,
        )
        return SettleResult(found=True)

    if outcome == "completed":
        attempt.status = "succeeded"
        attempt.result_url = result_url
        attempt.cause = source
        attempt.error_type = None
        attempt.finished_at = now
        task.status = "completed"
        task.result_url = result_url
        task.failure_reason = None
        task.updated_at = now
        logger.info("completed task=%s attempt=%s provider=%s", task.id, attempt.id, attempt.provider)
        return SettleResult(found=True)

    attempt.status = "timed_out" if source == "watchdog" else "failed"
    attempt.error_type = error_type
    attempt.cause = source
    attempt.finished_at = now
    # Закрыть попытку до новой строки: частичный уникальный индекс пускает только одну открытую.
    await session.flush()

    if error_type == "payload":
        await _fail_and_refund(session, task, reason="payload", now=now)
        logger.info("payload failure task=%s", task.id)
        return SettleResult(found=True)

    tried = await _tried(session, task.id)
    tried.add(attempt.leg_id)
    route_error = "moderation" if error_type == "moderation" else "technical"
    nxt = select_leg(
        await _legs(session, task.model_id),
        ref_count=len(task.ref_images or []),
        tried=tried,
        previous_error=route_error,
    )
    if nxt is None:
        await _fail_and_refund(session, task, reason="exhausted", now=now)
        logger.info("exhausted task=%s", task.id)
        return SettleResult(found=True)

    await _open_attempt(session, task, nxt)
    logger.info("advance task=%s to provider=%s", task.id, nxt.provider)
    return SettleResult(found=True, dispatch_task_id=task.id)


def _note_late_success(
    task: Task,
    attempt: Attempt,
    outcome: str,
    result_url: str | None,
    now: datetime,
) -> None:
    """Правило 6: опоздавший успех не становится результатом и не трогает журнал.

    Повторный вебхук по уже выигравшей попытке тоже сюда попадает и ничего не пишет.
    """
    if outcome != "completed" or not result_url or attempt.late_received_at is not None:
        return
    if task.status == "completed" and task.current_attempt_id == attempt.id and attempt.status == "succeeded":
        return
    attempt.late_result_url = result_url
    attempt.late_received_at = now


async def apply_payment(*, payment_id: str, user_id: uuid.UUID, tokens: int, status: str) -> str:
    if tokens <= 0:
        raise AppError(422, "tokens must be positive")
    async with session_scope() as session:
        async with session.begin():
            await _ensure_user(session, user_id)
            await _lock_user(session, user_id)
            inserted = (
                await session.execute(
                    pg_insert(Payment)
                    .values(payment_id=payment_id, user_id=user_id, tokens=tokens, status=status)
                    .on_conflict_do_nothing(index_elements=[Payment.payment_id])
                    .returning(Payment.payment_id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                return "duplicate"
            if status != "succeeded":
                return "ignored"
            await _insert_ledger(
                session,
                user_id=user_id,
                amount=tokens,
                reason="topup",
                key=f"payment:{payment_id}",
                payment_id=payment_id,
            )
            return "applied"
