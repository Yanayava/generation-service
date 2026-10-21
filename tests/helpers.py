from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx

APP = os.environ.get("APP_URL", "http://localhost:8000")
SECRET = b"dev-secret"


def sign(body: bytes) -> str:
    return hmac.new(SECRET, body, hashlib.sha256).hexdigest()


def signed_post(path: str, payload: dict) -> httpx.Response:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return httpx.post(
        f"{APP}{path}",
        content=raw,
        headers={"X-Signature": sign(raw), "Content-Type": "application/json"},
        timeout=30,
    )


def credit(user_id: str, tokens: int, payment_id: str | None = None) -> httpx.Response:
    return signed_post(
        "/webhooks/payments",
        {
            "payment_id": payment_id or str(uuid4()),
            "user_id": user_id,
            "tokens": tokens,
            "status": "succeeded",
        },
    )


def generate(user_id: str, model: str, refs: list[str] | None = None, prompt: str = "a red bicycle") -> httpx.Response:
    return httpx.post(
        f"{APP}/generate",
        json={"user_id": user_id, "model_slug": model, "prompt": prompt, "ref_images": refs or []},
        timeout=30,
    )


def task(task_id: str) -> dict:
    response = httpx.get(f"{APP}/tasks/{task_id}", timeout=10)
    response.raise_for_status()
    return response.json()


def balance(user_id: str) -> dict:
    response = httpx.get(f"{APP}/users/{user_id}/balance", timeout=10)
    response.raise_for_status()
    body = response.json()
    assert body["balance"] == sum(row["amount"] for row in body["ledger"])
    return body


def wait_task(task_id: str, predicate, timeout: float) -> dict:
    last = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        last = task(task_id)
        if predicate(last):
            return last
        time.sleep(0.25)
    raise AssertionError(json.dumps(last, ensure_ascii=False, indent=2))


def burst(count: int, call):
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(call, range(count)))
