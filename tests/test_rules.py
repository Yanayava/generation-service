from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from tests.conftest import APP, PROVIDERS, behavior
from tests.helpers import balance, burst, credit, generate, signed_post, wait_task

pytestmark = pytest.mark.usefixtures("providers")


def test_no_suitable_leg_does_not_charge():
    user_id = str(uuid4())
    assert credit(user_id, 25).status_code == 200
    response = generate(user_id, "video-long", refs=["a", "b", "c"])
    assert response.status_code == 400
    body = balance(user_id)
    assert body["balance"] == 25
    assert [row["reason"] for row in body["ledger"]] == ["topup"]


def test_insufficient_tokens():
    user_id = str(uuid4())
    assert credit(user_id, 1).status_code == 200
    response = generate(user_id, "image-fast")
    assert response.status_code == 402
    assert balance(user_id)["balance"] == 1


def test_fourth_concurrent_task_is_rejected():
    httpx.put(f"{PROVIDERS}/alpha/settings", json=behavior(webhook_count=0), timeout=5).raise_for_status()
    user_id = str(uuid4())
    assert credit(user_id, 100).status_code == 200

    responses = burst(4, lambda _: generate(user_id, "image-fast"))
    codes = sorted(response.status_code for response in responses)
    assert codes == [201, 201, 201, 429]
    assert balance(user_id)["balance"] == 94


def test_moderation_skips_flagged_leg():
    httpx.put(
        f"{PROVIDERS}/beta/settings",
        json=behavior(fail_rate=1, error_type="moderation"),
        timeout=5,
    ).raise_for_status()
    user_id = str(uuid4())
    assert credit(user_id, 25).status_code == 200
    created = generate(user_id, "video-long", refs=["frame"])
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]

    done = wait_task(task_id, lambda body: body["status"] == "failed", timeout=15)
    assert [item["provider"] for item in done["attempts"]] == ["beta"]
    assert done["attempts"][0]["error_type"] == "moderation"
    assert done["failure_reason"] == "exhausted"
    assert balance(user_id)["balance"] == 25


def test_invalid_signature_is_rejected():
    response = httpx.post(
        f"{APP}/webhooks/payments",
        content=b'{"payment_id":"x","user_id":"00000000-0000-0000-0000-000000000001","tokens":1,"status":"succeeded"}',
        headers={"X-Signature": "deadbeef", "Content-Type": "application/json"},
        timeout=10,
    )
    assert response.status_code == 401


def test_unknown_user_balance_is_missing():
    response = httpx.get(f"{APP}/users/{uuid4()}/balance", timeout=10)
    assert response.status_code == 404


def test_payload_failure_does_not_try_the_next_leg():
    httpx.put(
        f"{PROVIDERS}/alpha/settings",
        json=behavior(fail_rate=1, error_type="payload"),
        timeout=5,
    ).raise_for_status()
    user_id = str(uuid4())
    assert credit(user_id, 10).status_code == 200
    created = generate(user_id, "image-fast")
    assert created.status_code == 201, created.text
    done = wait_task(created.json()["task_id"], lambda body: body["status"] == "failed", timeout=15)
    assert [item["provider"] for item in done["attempts"]] == ["alpha"]
    assert done["failure_reason"] == "payload"
    assert balance(user_id)["balance"] == 10
    signed_post(
        "/webhooks/providers/alpha",
        {
            "attempt_id": done["attempts"][0]["id"],
            "external_id": done["attempts"][0]["external_id"],
            "status": "completed",
            "result_url": "https://files.fake/alpha/late",
            "error_type": None,
        },
    )
    still = wait_task(created.json()["task_id"], lambda body: body["attempts"][0]["late_result_url"], timeout=10)
    assert still["status"] == "failed"
    assert still["result_url"] is None
    assert balance(user_id)["balance"] == 10
