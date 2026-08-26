from __future__ import annotations

import asyncio

import requests
from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError, TimeoutError as SQLAlchemyTimeoutError


def result_from_status(status_code: int) -> str:
    if status_code < 400:
        return "success"
    if status_code < 500:
        return "client_error"
    return "server_error"


def is_db_pool_timeout(exc: BaseException) -> bool:
    """Identify SQLAlchemy QueuePool checkout exhaustion without matching other DB errors."""
    if not isinstance(exc, SQLAlchemyTimeoutError):
        return False
    message = str(exc)
    return "QueuePool limit of size" in message and "connection timed out" in message


def classify_exception(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        return "cancelled"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if is_db_pool_timeout(exc):
        return "db_pool_timeout"
    if isinstance(exc, SQLAlchemyError):
        return "database_error"
    if isinstance(exc, ValidationError):
        return "validation_error"
    if isinstance(exc, HTTPException):
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return "auth_error"
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return "not_found"
        if exc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY:
            return "validation_error"
        if exc.status_code == status.HTTP_408_REQUEST_TIMEOUT:
            return "timeout"
        if exc.status_code == 499:
            return "cancelled"
        return "client_error" if exc.status_code < 500 else "server_error"
    return "internal_error"
