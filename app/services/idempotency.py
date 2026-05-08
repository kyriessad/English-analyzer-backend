"""
Phase 3 idempotency service for client_actions.

Ensures the same client_action_id is only processed once.
"""
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.review import ClientAction


def get_existing_client_action(
    db: Session,
    user_id: UUID,
    client_action_id: str,
) -> ClientAction | None:
    """Look up an existing client_action by user_id + client_action_id."""
    return db.scalar(
        select(ClientAction).where(
            ClientAction.user_id == user_id,
            ClientAction.client_action_id == client_action_id,
        )
    )


def create_processing_action(
    db: Session,
    user_id: UUID,
    client_action_id: str,
    action_type: str,
    request_payload: dict | None = None,
) -> ClientAction:
    """Create a new client_action with status=processing."""
    action = ClientAction(
        user_id=user_id,
        client_action_id=client_action_id,
        action_type=action_type,
        request_payload=request_payload,
        status="processing",
    )
    db.add(action)
    db.flush()
    return action


def mark_action_succeeded(
    db: Session,
    action: ClientAction,
    response_payload: dict | None = None,
) -> None:
    """Mark a client_action as succeeded with optional response payload."""
    action.status = "succeeded"
    if response_payload is not None:
        action.response_payload = response_payload
    action.processed_at = datetime.now(timezone.utc)
    db.flush()


def mark_action_ignored(
    db: Session,
    action: ClientAction,
    response_payload: dict | None = None,
    reason: str = "",
) -> None:
    """Mark a client_action as ignored (business terminal state)."""
    action.status = "ignored"
    if response_payload is not None:
        action.response_payload = response_payload
    action.error_message = reason
    action.processed_at = datetime.now(timezone.utc)
    db.flush()


def mark_action_failed(
    db: Session,
    action: ClientAction,
    error_message: str = "",
) -> None:
    """Mark a client_action as failed."""
    action.status = "failed"
    action.error_message = error_message
    action.processed_at = datetime.now(timezone.utc)
    db.flush()
