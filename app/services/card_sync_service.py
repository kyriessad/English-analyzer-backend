from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.card import Card
from app.models.review import ClientAction
from app.schemas.card import (
    CardCreate,
    CardResponse,
    CardSyncRequest,
    CardSyncResponse,
    CardUpdate,
)
from app.services.card_service import (
    apply_card_update,
    create_card_in_transaction,
    get_card_or_404,
    soft_delete_card,
)
from app.services.idempotency import (
    create_processing_action,
    get_existing_client_action,
    mark_action_succeeded,
)
from app.services.lexical_metadata import upsert_card_lexical_metadata_best_effort


def _action_type(payload: CardSyncRequest) -> str:
    return f"card_sync_{payload.operation.lower()}"


def _request_json(payload: CardSyncRequest) -> dict:
    return payload.model_dump(mode="json")


def _return_existing_action(
    action: ClientAction,
    payload: CardSyncRequest,
) -> CardSyncResponse:
    if action.action_type != _action_type(payload) or action.request_payload != _request_json(payload):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "idempotency_key_reused",
                "message": "client_action_id was already used for a different card operation.",
            },
        )
    if action.status == "succeeded" and action.response_payload:
        saved = dict(action.response_payload)
        saved["replayed"] = True
        return CardSyncResponse.model_validate(saved)
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "card_action_in_progress",
            "message": "This card operation is already being processed; retry it later.",
        },
    )


def _start_action(
    db: Session,
    user_id: UUID,
    payload: CardSyncRequest,
) -> ClientAction | CardSyncResponse:
    existing = get_existing_client_action(db, user_id, payload.client_action_id)
    if existing is not None:
        return _return_existing_action(existing, payload)
    try:
        return create_processing_action(
            db,
            user_id,
            payload.client_action_id,
            _action_type(payload),
            request_payload=_request_json(payload),
        )
    except IntegrityError:
        db.rollback()
        existing = get_existing_client_action(db, user_id, payload.client_action_id)
        if existing is None:
            raise
        return _return_existing_action(existing, payload)


def _apply_create(db: Session, user_id: UUID, request: CardSyncRequest) -> Card:
    raw_payload = dict(request.payload or {})
    raw_payload["user_id"] = str(user_id)
    raw_payload["local_temp_id"] = request.local_id
    payload = CardCreate.model_validate(raw_payload)
    return create_card_in_transaction(db, payload)


def _apply_update(db: Session, user_id: UUID, request: CardSyncRequest) -> Card:
    card = get_card_or_404(db, request.card_id, user_id, for_update=True)
    before_content_normalized = card.content_normalized
    raw_payload = dict(request.payload or {})
    raw_payload["base_version"] = request.base_version
    payload = CardUpdate.model_validate(raw_payload)
    apply_card_update(card, payload)
    if card.content_normalized != before_content_normalized:
        upsert_card_lexical_metadata_best_effort(db, card)
    return card


def _apply_delete(db: Session, user_id: UUID, request: CardSyncRequest) -> Card:
    card = get_card_or_404(
        db,
        request.card_id,
        user_id,
        include_deleted=True,
        for_update=True,
    )
    # A tombstone is terminal. A second device deleting an already-deleted
    # card should converge to that tombstone rather than report a false
    # conflict or recreate the record.
    if card.deleted_at is not None:
        return card
    return soft_delete_card(card, request.base_version)


def sync_card_operation(
    db: Session,
    user_id: UUID,
    request: CardSyncRequest,
) -> CardSyncResponse:
    action_or_response = _start_action(db, user_id, request)
    if isinstance(action_or_response, CardSyncResponse):
        return action_or_response
    action = action_or_response

    try:
        if request.operation == "CREATE":
            card = _apply_create(db, user_id, request)
        elif request.operation == "UPDATE":
            card = _apply_update(db, user_id, request)
        else:
            card = _apply_delete(db, user_id, request)

        db.flush()
        response = CardSyncResponse(
            operation=request.operation,
            local_id=request.local_id,
            card=CardResponse.model_validate(card),
        )
        mark_action_succeeded(db, action, response.model_dump(mode="json"))
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "card_sync_integrity_conflict",
                "message": "The card operation conflicts with existing server data.",
            },
        ) from exc
