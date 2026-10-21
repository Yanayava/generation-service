from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import router
from app.config import settings
from app.db import engine, session_scope
from app.errors import AppError
from app.watchdog import watchdog_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop = asyncio.Event()
    loop = asyncio.create_task(watchdog_loop(stop))
    logger.info(
        "worker ready timeout=%ss http_timeout=%ss grace=%ss",
        settings.provider_timeout,
        settings.provider_http_timeout,
        settings.dispatch_grace_seconds,
    )
    try:
        yield
    finally:
        stop.set()
        loop.cancel()
        with suppress(asyncio.CancelledError):
            await loop
        await engine.dispose()


app = FastAPI(title="Generation service", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(AppError)
async def app_error(_request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


@app.get("/health")
async def health() -> dict:
    async with session_scope() as session:
        async with session.begin():
            await session.execute(text("SELECT 1"))
    return {"status": "ok"}
