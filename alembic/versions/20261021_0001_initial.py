"""Схема журнала, задач и стартовый каталог.

Баланс меняет только триггер на вставку в журнал. Повтор операции
невозможен: у строки уникальный ключ, и AFTER INSERT не стреляет,
если строка не легла.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-21
"""
from __future__ import annotations

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


UPGRADE_SQL = """
CREATE TABLE users (
    id UUID PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT balance_non_negative CHECK (balance >= 0)
);

CREATE TABLE ledger_entries (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users (id),
    amount INTEGER NOT NULL,
    balance_after INTEGER,
    reason TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    task_id UUID,
    payment_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ledger_amount_nonzero CHECK (amount <> 0),
    CONSTRAINT ledger_reason CHECK (reason IN ('charge', 'refund', 'topup')),
    CONSTRAINT ledger_idempotency UNIQUE (idempotency_key)
);

CREATE INDEX ledger_entries_user_created
    ON ledger_entries (user_id, created_at DESC, id DESC);

-- AFTER INSERT, не BEFORE: ON CONFLICT DO NOTHING всё равно вызывает
-- BEFORE-триггер для отброшенной строки. AFTER срабатывает только
-- по строке, которая реально появилась.
CREATE FUNCTION ledger_apply_balance() RETURNS trigger AS $$
DECLARE
    new_balance INTEGER;
BEGIN
    UPDATE users
    SET balance = balance + NEW.amount
    WHERE id = NEW.user_id
    RETURNING balance INTO new_balance;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'user % does not exist', NEW.user_id;
    END IF;

    UPDATE ledger_entries
    SET balance_after = new_balance
    WHERE id = NEW.id;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_apply_balance
AFTER INSERT ON ledger_entries
FOR EACH ROW
EXECUTE FUNCTION ledger_apply_balance();

CREATE TABLE payments (
    payment_id TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users (id),
    tokens INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT payments_tokens_positive CHECK (tokens > 0)
);

CREATE TABLE models (
    id UUID PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    price INTEGER NOT NULL,
    CONSTRAINT models_price_positive CHECK (price > 0)
);

CREATE TABLE legs (
    id UUID PRIMARY KEY,
    model_id UUID NOT NULL REFERENCES models (id),
    provider TEXT NOT NULL,
    priority INTEGER NOT NULL,
    max_ref_images INTEGER NOT NULL,
    skip_on_moderation BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT legs_provider CHECK (provider IN ('alpha', 'beta')),
    CONSTRAINT legs_refs_non_negative CHECK (max_ref_images >= 0),
    CONSTRAINT legs_model_priority UNIQUE (model_id, priority),
    CONSTRAINT legs_model_provider UNIQUE (model_id, provider)
);

CREATE TABLE tasks (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users (id),
    model_id UUID NOT NULL REFERENCES models (id),
    prompt TEXT NOT NULL,
    ref_images JSONB NOT NULL,
    price INTEGER NOT NULL,
    status TEXT NOT NULL,
    current_leg_id UUID REFERENCES legs (id),
    current_attempt_id UUID,
    result_url TEXT,
    failure_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tasks_price_positive CHECK (price > 0),
    CONSTRAINT tasks_status CHECK (status IN ('dispatching', 'waiting', 'completed', 'failed'))
);

CREATE INDEX tasks_user_active
    ON tasks (user_id)
    WHERE status IN ('dispatching', 'waiting');

CREATE TABLE attempts (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES tasks (id),
    leg_id UUID NOT NULL REFERENCES legs (id),
    provider TEXT NOT NULL,
    external_id TEXT,
    dispatch_token UUID,
    status TEXT NOT NULL,
    error_type TEXT,
    cause TEXT,
    result_url TEXT,
    late_result_url TEXT,
    late_received_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    deadline_at TIMESTAMPTZ,
    CONSTRAINT attempts_status CHECK (
        status IN ('sending', 'waiting', 'succeeded', 'failed', 'timed_out')
    ),
    CONSTRAINT attempts_error_type CHECK (
        error_type IS NULL OR error_type IN ('technical', 'moderation', 'payload')
    ),
    CONSTRAINT attempts_cause CHECK (
        cause IS NULL OR cause IN ('webhook', 'sync', 'watchdog')
    )
);

-- Одна открытая попытка на задачу. Второй отправитель упирается в индекс,
-- а не в надежду, что воркеры не пересекутся.
CREATE UNIQUE INDEX attempts_one_open
    ON attempts (task_id)
    WHERE status IN ('sending', 'waiting');

CREATE INDEX attempts_task_started
    ON attempts (task_id, started_at);

ALTER TABLE tasks
    ADD CONSTRAINT tasks_current_attempt_fk
    FOREIGN KEY (current_attempt_id) REFERENCES attempts (id);

ALTER TABLE ledger_entries
    ADD CONSTRAINT ledger_task_fk
    FOREIGN KEY (task_id) REFERENCES tasks (id);

INSERT INTO models (id, slug, price) VALUES
    ('10000000-0000-0000-0000-000000000001', 'image-fast', 2),
    ('10000000-0000-0000-0000-000000000002', 'video-long', 25);

INSERT INTO legs (id, model_id, provider, priority, max_ref_images, skip_on_moderation) VALUES
    ('20000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 'alpha', 0, 4, FALSE),
    ('20000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', 'beta', 1, 10, FALSE),
    ('20000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000002', 'beta', 0, 2, FALSE),
    ('20000000-0000-0000-0000-000000000004', '10000000-0000-0000-0000-000000000002', 'alpha', 1, 1, TRUE);
"""


DOWNGRADE_SQL = """
DROP TABLE IF EXISTS ledger_entries;
DROP TABLE IF EXISTS payments;
ALTER TABLE IF EXISTS tasks DROP CONSTRAINT IF EXISTS tasks_current_attempt_fk;
DROP TABLE IF EXISTS attempts;
DROP TABLE IF EXISTS tasks;
DROP TABLE IF EXISTS legs;
DROP TABLE IF EXISTS models;
DROP TABLE IF EXISTS users;
DROP FUNCTION IF EXISTS ledger_apply_balance();
"""


def _run(sql: str) -> None:
    # psycopg3 в обычном курсоре не принимает несколько команд за раз.
    # ClientCursor говорит с Postgres простым протоколом и выполняет весь скрипт.
    import psycopg

    fairy = op.get_bind().connection
    raw = getattr(fairy, "driver_connection", fairy)
    with psycopg.ClientCursor(raw) as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _run(UPGRADE_SQL)


def downgrade() -> None:
    _run(DOWNGRADE_SQL)
