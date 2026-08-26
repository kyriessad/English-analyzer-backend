import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.pool import QueuePool

from app.main import db_pool_timeout_handler
from app.observability.errors import classify_exception, is_db_pool_timeout
from app.observability.metrics import (
    DB_POOL_TIMEOUT_TOTAL,
    db_pool_checked_out_value,
    db_pool_overflow_value,
)


def _real_queue_pool_timeout() -> SQLAlchemyTimeoutError:
    pool = QueuePool(
        lambda: sqlite3.connect(":memory:"),
        pool_size=1,
        max_overflow=0,
        timeout=0,
    )
    held = pool.connect()
    try:
        with pytest.raises(SQLAlchemyTimeoutError) as raised:
            pool.connect()
        return raised.value
    finally:
        held.close()
        pool.dispose()


def test_pool_timeout_returns_503_code_and_increments_metric():
    timeout_error = _real_queue_pool_timeout()
    test_app = FastAPI()
    test_app.add_exception_handler(SQLAlchemyTimeoutError, db_pool_timeout_handler)

    @test_app.get("/pool-timeout")
    def pool_timeout():
        raise timeout_error

    before = DB_POOL_TIMEOUT_TOTAL._value.get()
    response = TestClient(test_app).get("/pool-timeout")

    assert response.status_code == 503
    assert response.json() == {
        "code": "DB_POOL_TIMEOUT",
        "detail": "服务器当前请求较多，请稍后重试",
        "request_id": None,
    }
    assert DB_POOL_TIMEOUT_TOTAL._value.get() == before + 1


def test_other_sqlalchemy_timeout_is_not_misclassified():
    error = SQLAlchemyTimeoutError("unrelated SQLAlchemy timeout")
    assert not is_db_pool_timeout(error)
    assert classify_exception(error) == "database_error"


def test_pool_metric_values_follow_real_queue_pool_state():
    pool = QueuePool(
        lambda: sqlite3.connect(":memory:"),
        pool_size=1,
        max_overflow=1,
        timeout=0,
    )
    first = pool.connect()
    second = pool.connect()
    try:
        assert db_pool_checked_out_value(pool) == 2
        assert db_pool_overflow_value(pool) == 1
    finally:
        second.close()
        first.close()

    assert db_pool_checked_out_value(pool) == 0
    assert db_pool_overflow_value(pool) == 0
    pool.dispose()
