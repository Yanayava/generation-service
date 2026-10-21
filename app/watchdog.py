from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from app.config import settings
from app.db import session_scope
from app.orchestrator import dispatch_loop, settle_attempt

logger = logging.getLogger("app.watchdog")

# FOR UPDATE OF t SKIP LOCKED: из четырёх воркеров строку забирает один.
# Упавшая транзакция отпускает блокировку сама, следующий цикл её подберёт.
DUE_SQL = text(
    """
    SELECT t.current_attempt_id AS attempt_id
    FROM tasks AS t
    JOIN attempts AS a ON a.id = t.current_attempt_id
    WHERE t.status IN ('dispatching', 'waiting')
      AND (
            (a.status = 'waiting' AND a.deadline_at < now())
         OR (a.status = 'sending' AND a.started_at < now() - make_interval(secs => :grace))
      )
    ORDER BY t.updated_at
    FOR UPDATE OF t SKIP LOCKED
    LIMIT 1
    """
)


async def reap_once() -> bool:
    dispatch_id = None
    async with session_scope() as session:
        async with session.begin():
            row = (await session.execute(DUE_SQL, {"grace": settings.dispatch_grace_seconds})).first()
            if row is None or row[0] is None:
                return False
            result = await settle_attempt(
                session,
                row[0],
                outcome="failed",
                error_type="technical",
                source="watchdog",
            )
            dispatch_id = result.dispatch_task_id
    if dispatch_id is not None:
        await dispatch_loop(dispatch_id)
    return True


async def watchdog_loop(stop: asyncio.Event) -> None:
    logger.info(
        "watchdog started timeout=%ss grace=%ss",
        settings.provider_timeout,
        settings.dispatch_grace_seconds,
    )
    while not stop.is_set():
        try:
            progressed = True
            while progressed and not stop.is_set():
                progressed = await reap_once()
        except Exception:
            logger.exception("watchdog iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.watchdog_interval_seconds)
        except TimeoutError:
            pass
