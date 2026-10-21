from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from contextlib import suppress
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.hmac_util import sign

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("providers")

NAMES = ("alpha", "beta")


class Runtime(BaseSettings):
    webhook_secret: str = "dev-secret"
    app_webhook_base: str = "http://localhost:8000"

    model_config = SettingsConfigDict(extra="ignore")


runtime = Runtime()


class Behavior(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fail_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    error_type: Literal["technical", "moderation", "payload"] = "technical"
    sync_mode: Literal["ok", "503", "timeout"] = "ok"
    sync_timeout_seconds: float = Field(default=30.0, gt=0)
    webhook_count: int = Field(default=1, ge=0, le=5)
    webhook_delay_seconds: float | None = Field(default=None, ge=0)
    duplicate_mode: Literal["sequential", "concurrent"] = "sequential"


behaviors: dict[str, Behavior] = {name: Behavior() for name in NAMES}
lock = asyncio.Lock()
background: set[asyncio.Task] = set()

app = FastAPI(title="Fake providers")


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    background.add(task)
    task.add_done_callback(background.discard)


async def _deliver(name: str, raw: bytes) -> None:
    url = runtime.app_webhook_base.rstrip("/") + f"/webhooks/providers/{name}"
    headers = {
        "X-Signature": sign(raw, runtime.webhook_secret),
        "Content-Type": "application/json",
    }
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(url, content=raw, headers=headers)
            if response.status_code < 500:
                if response.status_code >= 400:
                    logger.warning("webhook %s status=%s body=%s", name, response.status_code, response.text[:300])
                return
            logger.warning("webhook %s status=%s", name, response.status_code)
        except httpx.HTTPError:
            logger.exception("webhook transport %s", name)
        await asyncio.sleep(0.2 * (attempt + 1))
    logger.error("webhook gave up provider=%s", name)


async def _fanout(name: str, raw: bytes, behavior: Behavior) -> None:
    delay = behavior.webhook_delay_seconds
    if delay is None:
        delay = random.uniform(2, 20)
    await asyncio.sleep(delay)
    if behavior.webhook_count <= 0:
        logger.info("silent provider=%s", name)
        return
    sends = [_deliver(name, raw) for _ in range(behavior.webhook_count)]
    if behavior.duplicate_mode == "concurrent" and behavior.webhook_count > 1:
        await asyncio.gather(*sends)
        return
    for send in sends:
        await send


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/{name}/settings", response_model=Behavior)
async def get_settings(name: str) -> Behavior:
    if name not in behaviors:
        raise HTTPException(status_code=404, detail="unknown provider")
    async with lock:
        return behaviors[name]


@app.put("/{name}/settings", response_model=Behavior)
async def put_settings(name: str, body: Behavior) -> Behavior:
    if name not in behaviors:
        raise HTTPException(status_code=404, detail="unknown provider")
    async with lock:
        behaviors[name] = body
        return body


@app.post("/{name}/tasks", status_code=202)
async def submit_task(name: str, payload: dict) -> dict:
    if name not in behaviors:
        raise HTTPException(status_code=404, detail="unknown provider")
    attempt_id = payload.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise HTTPException(status_code=400, detail="attempt_id is required")

    async with lock:
        behavior = behaviors[name].model_copy(deep=True)

    if behavior.sync_mode == "503":
        raise HTTPException(status_code=503, detail="unavailable")
    if behavior.sync_mode == "timeout":
        await asyncio.sleep(behavior.sync_timeout_seconds)
        raise HTTPException(status_code=503, detail="timeout")

    external_id = str(uuid.uuid4())
    failed = random.random() < behavior.fail_rate
    if failed:
        body = {
            "external_id": external_id,
            "attempt_id": attempt_id,
            "status": "failed",
            "result_url": None,
            "error_type": behavior.error_type,
        }
    else:
        body = {
            "external_id": external_id,
            "attempt_id": attempt_id,
            "status": "completed",
            "result_url": f"https://files.fake/{name}/{external_id}",
            "error_type": None,
        }
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    _spawn(_fanout(name, raw, behavior))
    logger.info(
        "accepted provider=%s attempt=%s external=%s failed=%s webhooks=%s delay=%s",
        name,
        attempt_id,
        external_id,
        failed,
        behavior.webhook_count,
        behavior.webhook_delay_seconds,
    )
    return {"external_id": external_id}


@app.on_event("shutdown")
async def _shutdown() -> None:
    for task in list(background):
        task.cancel()
    for task in list(background):
        with suppress(asyncio.CancelledError):
            await task
