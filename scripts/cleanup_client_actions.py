"""Phase 3 TTL cleanup script for client_actions.

Deletes client_actions older than 7 days.

Usage:
    python -m scripts.cleanup_client_actions

Or from the backend root:
    cd English-analyzer-backend
    python scripts/cleanup_client_actions.py
"""
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from app.database import SessionLocal


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def cleanup_client_actions(dry_run: bool = False) -> int:
    """
    Delete client_actions created more than 7 days ago.
    Returns the number of rows deleted (or would-be-deleted in dry_run mode).
    """
    cutoff = datetime.now(timezone.utc)

    count_sql = text(
        "SELECT COUNT(*) FROM client_actions WHERE created_at < :cutoff"
    )
    delete_sql = text(
        "DELETE FROM client_actions WHERE created_at < :cutoff"
    )

    db = SessionLocal()
    try:
        total = db.scalar(count_sql, {"cutoff": cutoff}) or 0
        logger.info("Found %d client_actions older than %s", total, cutoff.isoformat())

        if dry_run:
            logger.info("[DRY RUN] Would delete %d rows", total)
            return total

        result = db.execute(delete_sql, {"cutoff": cutoff})
        db.commit()
        deleted = result.rowcount
        logger.info("Deleted %d client_actions older than 7 days", deleted)
        return deleted
    except Exception:
        logger.exception("Failed to cleanup client_actions")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    cleanup_client_actions(dry_run=dry_run)
