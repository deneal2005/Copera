from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import config
from .accounts import log_action, shop_settings
from .db import atomic, connect, now_iso
from .documents import document_path

LOGGER = logging.getLogger("smarth_xerox.retention")


def cleanup_expired(conn=None) -> dict[str, int]:
    """Remove expired draft files and old files from finished orders, keeping business records."""
    own_connection = conn is None
    conn = conn or connect()
    removed_drafts = removed_order_files = 0
    try:
        shops = conn.execute("SELECT id FROM shops ORDER BY id").fetchall()
        for shop_row in shops:
            shop_id = shop_row["id"]
            settings = shop_settings(conn, shop_id)
            retention_days = max(1, min(90, int(settings["general"].get("retention_days", 7))))
            cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
            with atomic(conn):
                drafts = conn.execute(
                    "SELECT id FROM drafts WHERE shop_id = ? AND expires_at <= ?", (shop_id, now_iso())
                ).fetchall()
                for draft in drafts:
                    documents = conn.execute(
                        "SELECT storage_name FROM documents WHERE shop_id = ? AND draft_id = ?",
                        (shop_id, draft["id"]),
                    ).fetchall()
                    for document in documents:
                        try:
                            document_path(shop_id, document["storage_name"]).unlink(missing_ok=True)
                        except (FileNotFoundError, ValueError):
                            pass
                    conn.execute("DELETE FROM drafts WHERE shop_id = ? AND id = ?", (shop_id, draft["id"]))
                    removed_drafts += 1

                expired = conn.execute(
                    "SELECT d.id, d.storage_name FROM documents d JOIN orders o ON o.id = d.order_id AND o.shop_id = d.shop_id "
                    "WHERE d.shop_id = ? AND d.created_at < ? AND o.status IN ('completed','cancelled','failed') "
                    "AND d.storage_name NOT LIKE 'expired-%'",
                    (shop_id, cutoff),
                ).fetchall()
                for document in expired:
                    try:
                        document_path(shop_id, document["storage_name"]).unlink(missing_ok=True)
                    except (FileNotFoundError, ValueError):
                        pass
                    conn.execute("UPDATE documents SET storage_name = ? WHERE shop_id = ? AND id = ?",
                                 (f"expired-{document['id']}", shop_id, document["id"]))
                    removed_order_files += 1
                if drafts or expired:
                    log_action(conn, shop_id, None, "documents.retention_cleanup", "documents", None,
                               {"expired_drafts": len(drafts), "expired_order_files": len(expired),
                                "retention_days": retention_days})
        return {"expired_drafts": removed_drafts, "expired_order_files": removed_order_files}
    finally:
        if own_connection:
            conn.close()
