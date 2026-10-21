"""Четыре обязательных сценария из условия.

Тесты ходят в сервис из compose и в настоящую базу через его API.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from tests.conftest import PROVIDERS, behavior
from tests.helpers import balance, burst, credit, generate, signed_post, wait_task

pytestmark = pytest.mark.usefixtures("providers")


def test_01_twenty_identical_payments_credit_once():
    user_id = str(uuid4())
    payment_id = str(uuid4())
    payload = {"payment_id": payment_id, "user_id": user_id, "tokens": 100, "status": "succeeded"}

    responses = burst(20, lambda _: signed_post("/webhooks/payments", payload))

    assert [response.status_code for response in responses] == [200] * 20
    outcomes = sorted(response.json()["status"] for response in responses)
    assert outcomes.count("applied") == 1
    assert outcomes.count("duplicate") == 19

    body = balance(user_id)
    assert body["balance"] == 100
    assert [row["reason"] for row in body["ledger"]] == ["topup"]
    assert body["ledger"][0]["idempotency_key"] == f"payment:{payment_id}"

    again = signed_post("/webhooks/payments", payload)
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"
    assert balance(user_id)["balance"] == 100


def test_02_ten_failures_on_the_last_leg_refund_once(providers):
    import httpx

    httpx.put(f"{PROVIDERS}/alpha/settings", json=behavior(sync_mode="503"), timeout=5).raise_for_status()
    httpx.put(f"{PROVIDERS}/beta/settings", json=behavior(webhook_count=0), timeout=5).raise_for_status()

    user_id = str(uuid4())
    assert credit(user_id, 10).status_code == 200

    created = generate(user_id, "image-fast")
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]

    waiting = wait_task(
        task_id,
        lambda body: (
            body["status"] == "waiting"
            and body["current_leg"]["provider"] == "beta"
            and len(body["attempts"]) == 2
            and body["attempts"][0]["status"] == "failed"
            and body["attempts"][0]["cause"] == "sync"
        ),
        timeout=15,
    )
    attempt_id = waiting["attempts"][1]["id"]
    payload = {
        "external_id": waiting["attempts"][1]["external_id"],
        "attempt_id": attempt_id,
        "status": "failed",
        "result_url": None,
        "error_type": "technical",
    }

    responses = burst(10, lambda _: signed_post("/webhooks/providers/beta", payload))

    assert [response.status_code for response in responses] == [200] * 10
    done = wait_task(task_id, lambda body: body["status"] == "failed", timeout=10)
    assert done["result_url"] is None
    assert done["failure_reason"] == "exhausted"
    assert [item["provider"] for item in done["attempts"]] == ["alpha", "beta"]
    assert done["attempts"][1]["status"] == "failed"
    assert done["attempts"][1]["error_type"] == "technical"

    body = balance(user_id)
    assert body["balance"] == 10
    refunds = [row for row in body["ledger"] if row["reason"] == "refund"]
    assert len(refunds) == 1
    assert refunds[0]["amount"] == 2
    assert refunds[0]["idempotency_key"] == f"refund:{task_id}"


def test_03_first_leg_fails_second_completes(providers):
    import httpx

    httpx.put(
        f"{PROVIDERS}/alpha/settings",
        json=behavior(fail_rate=1, error_type="technical"),
        timeout=5,
    ).raise_for_status()
    httpx.put(
        f"{PROVIDERS}/beta/settings",
        json=behavior(fail_rate=0, webhook_count=2, duplicate_mode="concurrent"),
        timeout=5,
    ).raise_for_status()

    user_id = str(uuid4())
    assert credit(user_id, 20).status_code == 200
    created = generate(user_id, "image-fast", refs=["ref-1"])
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]

    done = wait_task(task_id, lambda body: body["status"] == "completed", timeout=15)
    assert done["result_url"].startswith("https://files.fake/beta/")
    assert done["current_leg"]["provider"] == "beta"
    assert [(item["provider"], item["status"], item["error_type"]) for item in done["attempts"]] == [
        ("alpha", "failed", "technical"),
        ("beta", "succeeded", None),
    ]
    assert done["attempts"][1]["late_result_url"] is None

    body = balance(user_id)
    assert body["balance"] == 18
    assert [row["reason"] for row in body["ledger"]] == ["charge", "topup"]


def test_04_late_completed_after_watchdog_keeps_the_refund(providers):
    import httpx

    # Пять референсов принимает только beta. Сторож сдаётся через 60 секунд,
    # файл приходит на десятой секунде после этого.
    httpx.put(
        f"{PROVIDERS}/beta/settings",
        json=behavior(fail_rate=0, webhook_count=1, webhook_delay_seconds=70),
        timeout=5,
    ).raise_for_status()

    user_id = str(uuid4())
    assert credit(user_id, 10).status_code == 200
    refs = [f"ref-{index}" for index in range(5)]
    created = generate(user_id, "image-fast", refs=refs)
    assert created.status_code == 201, created.text
    task_id = created.json()["task_id"]

    failed = wait_task(task_id, lambda body: body["status"] == "failed", timeout=90)
    assert failed["result_url"] is None
    assert failed["failure_reason"] == "exhausted"
    assert len(failed["attempts"]) == 1
    assert failed["attempts"][0]["provider"] == "beta"
    assert failed["attempts"][0]["status"] == "timed_out"
    assert failed["attempts"][0]["cause"] == "watchdog"
    assert balance(user_id)["balance"] == 10

    late = wait_task(
        task_id,
        lambda body: body["attempts"][0]["late_result_url"] is not None,
        timeout=30,
    )
    assert late["status"] == "failed"
    assert late["result_url"] is None
    assert late["attempts"][0]["late_result_url"].startswith("https://files.fake/beta/")
    body = balance(user_id)
    assert body["balance"] == 10
    assert len([row for row in body["ledger"] if row["reason"] == "refund"]) == 1
    assert len([row for row in body["ledger"] if row["reason"] == "charge"]) == 1
