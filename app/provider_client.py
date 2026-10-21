from __future__ import annotations

import httpx

from app.config import settings


class SyncProviderError(Exception):
    """Провайдер не принял задачу. kind — technical или payload."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


async def submit(provider: str, payload: dict) -> str:
    url = settings.provider_url(provider).rstrip("/") + "/tasks"
    try:
        async with httpx.AsyncClient(timeout=settings.provider_http_timeout) as client:
            response = await client.post(url, json=payload)
    except httpx.TimeoutException as exc:
        raise SyncProviderError("technical") from exc
    except httpx.HTTPError as exc:
        raise SyncProviderError("technical") from exc

    if response.status_code == 202:
        body = response.json()
        external_id = body.get("external_id")
        if not isinstance(external_id, str) or not external_id:
            raise SyncProviderError("payload")
        return external_id
    if 400 <= response.status_code < 500:
        raise SyncProviderError("payload")
    raise SyncProviderError("technical")
