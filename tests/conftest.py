from __future__ import annotations

import os
import time

import httpx
import pytest

APP = os.environ.get("APP_URL", "http://localhost:8000")
PROVIDERS = os.environ.get("PROVIDER_URL", "http://localhost:8001")


def _wait(url: str) -> None:
    last: object = None
    for _ in range(60):
        try:
            response = httpx.get(url, timeout=2)
            if response.status_code == 200:
                return
            last = response.status_code
        except httpx.HTTPError as exc:
            last = exc
        time.sleep(1)
    pytest.fail(f"{url} не поднялся: {last}. Сначала: docker compose up --build -d")


@pytest.fixture(scope="session")
def stack() -> None:
    _wait(f"{APP}/health")
    _wait(f"{PROVIDERS}/health")


def behavior(**overrides: object) -> dict:
    base: dict[str, object] = {
        "fail_rate": 0,
        "error_type": "technical",
        "sync_mode": "ok",
        "sync_timeout_seconds": 30,
        "webhook_count": 1,
        "webhook_delay_seconds": 0.05,
        "duplicate_mode": "sequential",
    }
    base.update(overrides)
    return base


@pytest.fixture
def providers(stack: None):
    for name in ("alpha", "beta"):
        response = httpx.put(f"{PROVIDERS}/{name}/settings", json=behavior(), timeout=5)
        response.raise_for_status()
    yield
    for name in ("alpha", "beta"):
        httpx.put(f"{PROVIDERS}/{name}/settings", json=behavior(), timeout=5)
