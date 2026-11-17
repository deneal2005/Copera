from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from .accounts import log_action, notify
from .db import now_iso
from .security import digest, random_token


def create_pairing_code(conn: sqlite3.Connection, shop_id: str, actor_id: str) -> tuple[str, str]:
    code = random_token(24)
    created, expiry = now_iso(), (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO pairing_codes(code_hash, shop_id, expires_at, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                 (digest(code), shop_id, expiry, actor_id, created))
    log_action(conn, shop_id, actor_id, "print_agent.pairing_code_created", "print_agent")
    return code, expiry


def pair_agent(conn: sqlite3.Connection, code: str, name: str) -> dict:
    if not name.strip() or len(name.strip()) > 100:
        raise ValueError("Enter a device name of 1 to 100 characters.")
    row = conn.execute("SELECT * FROM pairing_codes WHERE code_hash = ? AND consumed_at IS NULL AND expires_at > ?",
                       (digest(code), now_iso())).fetchone()
    if not row:
        raise ValueError("This pairing code is expired or has already been used.")
    token, agent_id = random_token(48), str(uuid.uuid4())
    conn.execute("UPDATE pairing_codes SET consumed_at = ? WHERE code_hash = ? AND consumed_at IS NULL",
                 (now_iso(), digest(code)))
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        raise ValueError("This pairing code has already been used.")
    conn.execute(
        "INSERT INTO print_agents(id, shop_id, name, token_hash, created_at) VALUES (?, ?, ?, ?, ?)",
        (agent_id, row["shop_id"], name.strip(), digest(token), now_iso()),
    )
    log_action(conn, row["shop_id"], row["created_by"], "print_agent.paired", "print_agent", agent_id)
    return {"agent_id": agent_id, "shop_id": row["shop_id"], "name": name.strip(), "token": token}


def authenticate_agent(conn: sqlite3.Connection, token: str | None) -> dict | None:
    if not token:
        return None
    row = conn.execute("SELECT id, shop_id, name, status, revoked_at, printers_json, version, "
                       "last_heartbeat_at, active_job_id FROM print_agents WHERE token_hash = ?",
                       (digest(token),)).fetchone()
    return dict(row) if row and not row["revoked_at"] else None


def update_heartbeat(conn: sqlite3.Connection, agent: dict, version: str,
                     printers: list, active_job_id: str | None = None, last_error: str | None = None) -> None:
    if not isinstance(printers, list) or len(printers) > 50:
        raise ValueError("Printer inventory must be a list of at most 50 printers.")
    cleaned = []
    for printer in printers:
        if not isinstance(printer, dict) or not isinstance(printer.get("name"), str):
            raise ValueError("Printer inventory is invalid.")
        cleaned.append({
            "name": printer["name"][:160], "status": str(printer.get("status", "unknown"))[:60],
            "is_default": bool(printer.get("is_default", False)),
            "color": printer.get("color") if isinstance(printer.get("color"), bool) else None,
            "duplex": printer.get("duplex") if isinstance(printer.get("duplex"), bool) else None,
            "paper_sizes": [str(size)[:20] for size in printer.get("paper_sizes", [])[:30]]
            if isinstance(printer.get("paper_sizes", []), list) else [],
        })
    conn.execute(
        "UPDATE print_agents SET status = 'online', version = ?, printers_json = ?, active_job_id = ?, "
        "last_error = ?, last_heartbeat_at = ? WHERE id = ? AND shop_id = ? AND revoked_at IS NULL",
        (version[:40], json.dumps(cleaned, separators=(",", ":")), active_job_id,
         last_error[:500] if last_error else None, now_iso(), agent["id"], agent["shop_id"]),
    )


def list_agents(conn: sqlite3.Connection, shop_id: str) -> list[dict]:
    result = []
    for row in conn.execute("SELECT * FROM print_agents WHERE shop_id = ? ORDER BY created_at DESC", (shop_id,)):
        result.append({**dict(row), "printers": json.loads(row["printers_json"] or "[]"),
                       "token_hash": None})
        result[-1].pop("token_hash", None)
    return result


def create_print_jobs(conn: sqlite3.Connection, shop_id: str, order_id: str, method: str,
                      actor_id: str, shop_config: dict, risk_confirmed: bool = False,
                      reprint_confirmed: bool = False) -> list[dict]:
    if method not in ("manual", "automatic"):
        raise ValueError("Choose manual or automatic printing.")
    order = conn.execute("SELECT * FROM orders WHERE shop_id = ? AND id = ?", (shop_id, order_id)).fetchone()
    if not order:
        raise ValueError("Order not found.")
    if order["payment_status"] != "paid":
        raise ValueError("Verify payment before creating a print job.")
    if order["status"] == "cancelled":
        raise ValueError("A cancelled order cannot be printed. Record a refund if payment was collected.")
    if method == "automatic" and not shop_config["printing"].get("automatic_enabled", False):
        raise ValueError("Automatic printing is disabled in shop settings.")
    documents = conn.execute("SELECT * FROM documents WHERE shop_id = ? AND order_id = ? ORDER BY created_at, id",
                             (shop_id, order_id)).fetchall()
    if not documents:
        raise ValueError("This order has no available documents.")
    jobs = []
    needs_risk_confirmation = False
    if any(document["storage_name"].startswith("expired-") for document in documents):
        raise ValueError("This order's private documents expired under the shop retention policy and cannot be reprinted.")
    if method == "automatic":
        for document in documents:
            document_settings = json.loads(document["settings_json"])
            if (document_settings.get("color") == "color" and shop_config["printing"].get("approve_color", True)):
                needs_risk_confirmation = True
            if (document["page_count"] or 0) >= shop_config["printing"].get("large_job_pages", 100) \
                    and shop_config["printing"].get("approve_large_jobs", True):
                needs_risk_confirmation = True
        if needs_risk_confirmation and not risk_confirmed:
            raise ValueError("Confirm the color or high-page-count print warning before approving this automatic job.")
    for document in documents:
        existing = conn.execute(
            "SELECT * FROM print_jobs WHERE shop_id = ? AND order_id = ? AND document_id = ? AND method = ? "
            "AND status IN ('queued','paused','claimed','downloading','submitting','submitted','spooler_reported','printing') "
            "ORDER BY created_at DESC LIMIT 1",
            (shop_id, order_id, document["id"], method),
        ).fetchone()
        if existing:
            jobs.append(dict(existing))
            continue
        previous_attempts = conn.execute(
            "SELECT count(*) FROM print_jobs WHERE shop_id = ? AND order_id = ? AND document_id = ? AND method = ?",
            (shop_id, order_id, document["id"], method),
        ).fetchone()[0]
        if previous_attempts and not reprint_confirmed:
            raise ValueError("This document has an earlier print attempt. Confirm that the earlier copy is accounted for before retrying.")
        if previous_attempts and previous_attempts >= shop_config["printing"].get("retry_limit", 2) + 1:
            raise ValueError("This document has reached the configured print retry limit.")
        if document["page_count"] is None or document["needs_review"]:
            raise ValueError("Verify this document's page count before approving printing.")
        settings = json.loads(document["settings_json"])
        pages = document["page_count"]
        copies = settings.get("copies", 1)
        if pages > shop_config["printing"].get("max_pages_per_job", 200) or copies > shop_config["printing"].get("max_copies", 20):
            raise ValueError("This job exceeds the shop's automatic print limit.")
        if method == "automatic" and settings.get("color") == "color" and shop_config["printing"].get("approve_color", True):
            # This call is the explicit owner approval required for the color job.
            pass
        job_id = str(uuid.uuid4())
        dedupe = f"{order_id}:{document['id']}:{method}:{previous_attempts + 1}"
        conn.execute(
            "INSERT INTO print_jobs(id, shop_id, order_id, document_id, method, status, dedupe_key, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (job_id, shop_id, order_id, document["id"], method, dedupe, previous_attempts, now_iso(), now_iso()),
        )
        conn.execute("INSERT INTO print_job_attempts(id, shop_id, print_job_id, state, detail, created_at) VALUES (?, ?, ?, 'queued', ?, ?)",
                     (str(uuid.uuid4()), shop_id, job_id, f"Approved by {actor_id}", now_iso()))
        jobs.append(dict(conn.execute("SELECT * FROM print_jobs WHERE id = ?", (job_id,)).fetchone()))
    conn.execute("UPDATE orders SET status = 'queued', updated_at = ? WHERE shop_id = ? AND id = ? AND status != 'cancelled'",
                 (now_iso(), shop_id, order_id))
    log_action(conn, shop_id, actor_id, f"print.{method}.approved", "order", order_id, {"job_count": len(jobs)})
    notify(conn, shop_id, "owner", f"{method.title()} printing queued", f"{order['ticket']} has {len(jobs)} document job(s) in the queue.", order_id)
    return jobs


def claim_next_job(conn: sqlite3.Connection, agent: dict, shop_config: dict) -> dict | None:
    if not shop_config["printing"].get("automatic_enabled", False):
        return None
    stale_before = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    stale = conn.execute(
        "SELECT * FROM print_jobs WHERE shop_id = ? AND agent_id = ? AND status IN ('claimed', 'downloading') AND updated_at < ?",
        (agent["shop_id"], agent["id"], stale_before),
    ).fetchall()
    for job in stale:
        conn.execute("UPDATE print_jobs SET status = 'failed', error_message = 'Agent stopped responding during the job.', updated_at = ? WHERE id = ?",
                     (now_iso(), job["id"]))
        conn.execute("UPDATE print_agents SET active_job_id = NULL WHERE id = ?", (agent["id"],))
    active = conn.execute("SELECT id FROM print_jobs WHERE shop_id = ? AND agent_id = ? AND status IN ('claimed','downloading','submitting')",
                          (agent["shop_id"], agent["id"])).fetchone()
    if active:
        return None
    candidate = conn.execute(
        "SELECT j.*, o.ticket, o.payment_status, d.original_name, d.storage_name, d.mime_type, d.size_bytes, "
        "d.page_count, d.settings_json FROM print_jobs j JOIN orders o ON o.id = j.order_id AND o.shop_id = j.shop_id "
        "JOIN documents d ON d.id = j.document_id AND d.shop_id = j.shop_id "
        "WHERE j.shop_id = ? AND j.method = 'automatic' AND j.status = 'queued' AND o.payment_status = 'paid' AND o.status != 'cancelled' "
        "ORDER BY j.priority DESC, j.created_at ASC LIMIT 1",
        (agent["shop_id"],),
    ).fetchone()
    if not candidate:
        return None
    printers = json.loads(agent.get("printers_json") or "[]")
    if not printers:
        return None
    settings = json.loads(candidate["settings_json"])
    configured = shop_config["printing"].get("color_printer" if settings.get("color") == "color" else "default_printer", "")
    printer = next((p for p in printers if p.get("name") == configured and p.get("status") == "ready"), None) if configured else None
    if printer is None and not configured:
        printer = next((p for p in printers if p.get("is_default") and p.get("status") == "ready"), None)
    if printer is None and not configured:
        printer = next((p for p in printers if p.get("status") == "ready"), None)
    if not printer:
        return None
    if settings.get("color") == "color" and printer.get("color") is False:
        conn.execute("UPDATE print_jobs SET status = 'failed', error_message = 'Selected printer does not support color.', updated_at = ? WHERE id = ?",
                     (now_iso(), candidate["id"]))
        return None
    if settings.get("sides") == "double" and printer.get("duplex") is False:
        conn.execute("UPDATE print_jobs SET status = 'failed', error_message = 'Selected printer does not support duplex printing.', updated_at = ? WHERE id = ?",
                     (now_iso(), candidate["id"]))
        return None
    requested_paper = settings.get("paper_size", "A4").upper()
    if printer.get("paper_sizes") and requested_paper not in printer["paper_sizes"]:
        conn.execute("UPDATE print_jobs SET status = 'failed', error_message = 'Selected printer does not report support for this paper size.', updated_at = ? WHERE id = ?",
                     (now_iso(), candidate["id"]))
        return None
    changed = conn.execute(
        "UPDATE print_jobs SET status = 'claimed', agent_id = ?, attempts = attempts + 1, updated_at = ? "
        "WHERE id = ? AND status = 'queued'",
        (agent["id"], now_iso(), candidate["id"]),
    ).rowcount
    if changed != 1:
        return None
    conn.execute("UPDATE print_agents SET active_job_id = ? WHERE id = ?", (candidate["id"], agent["id"]))
    conn.execute("INSERT INTO print_job_attempts(id, shop_id, print_job_id, agent_id, state, created_at) VALUES (?, ?, ?, ?, 'claimed', ?)",
                 (str(uuid.uuid4()), agent["shop_id"], candidate["id"], agent["id"], now_iso()))
    return {"id": candidate["id"], "ticket": candidate["ticket"], "name": candidate["original_name"],
            "mime_type": candidate["mime_type"], "page_count": candidate["page_count"],
            "settings": settings, "printer": printer["name"], "download_path": f"/api/agent/jobs/{candidate['id']}/document"}


def agent_document(conn: sqlite3.Connection, agent: dict, job_id: str) -> dict | None:
    row = conn.execute(
        "SELECT d.shop_id, d.storage_name, d.original_name, d.mime_type FROM print_jobs j "
        "JOIN documents d ON d.id = j.document_id AND d.shop_id = j.shop_id "
        "WHERE j.id = ? AND j.shop_id = ? AND j.agent_id = ? AND j.status IN ('claimed','downloading')",
        (job_id, agent["shop_id"], agent["id"]),
    ).fetchone()
    if not row:
        return None
    conn.execute("UPDATE print_jobs SET status = 'downloading', updated_at = ? WHERE id = ?", (now_iso(), job_id))
    conn.execute("INSERT INTO print_job_attempts(id, shop_id, print_job_id, agent_id, state, created_at) VALUES (?, ?, ?, ?, 'downloaded', ?)",
                 (str(uuid.uuid4()), agent["shop_id"], job_id, agent["id"], now_iso()))
    return dict(row)


def report_job(conn: sqlite3.Connection, agent: dict, job_id: str, state: str,
               detail: str = "", spooler_job_id: str | None = None) -> dict:
    if state not in ("submission_started", "submitted", "failed", "spooler_reported"):
        raise ValueError("Unsupported print agent job state.")
    job = conn.execute("SELECT * FROM print_jobs WHERE id = ? AND shop_id = ? AND agent_id = ?",
                       (job_id, agent["shop_id"], agent["id"])).fetchone()
    if not job:
        raise ValueError("Print job not found for this agent.")
    if job["status"] in ("submitted", "spooler_reported") and state in ("submitted", "spooler_reported"):
        return dict(job)
    if job["status"] == "failed" and state == "failed":
        return dict(job)
    if state == "submission_started" and job["status"] not in ("claimed", "downloading"):
        raise ValueError("This job is not ready to start spooler submission.")
    if state == "submitted" and job["status"] != "submitting":
        raise ValueError("This job has not recorded a spooler submission start.")
    if state == "spooler_reported" and job["status"] != "submitted":
        raise ValueError("This job has not been submitted to the printer spooler.")
    if state == "failed" and job["status"] not in ("claimed", "downloading", "submitting"):
        raise ValueError("This job cannot accept that update.")
    timestamp = now_iso()
    if state == "submission_started":
        conn.execute("UPDATE print_jobs SET status = 'submitting', updated_at = ? WHERE id = ?", (timestamp, job_id))
        conn.execute("UPDATE orders SET status = 'printing', updated_at = ? WHERE shop_id = ? AND id = ?",
                     (timestamp, agent["shop_id"], job["order_id"]))
    elif state == "failed":
        conn.execute("UPDATE print_jobs SET status = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
                     (detail[:500] or "The printer agent reported a failure.", timestamp, job_id))
        conn.execute("UPDATE print_agents SET active_job_id = NULL, last_error = ? WHERE id = ?",
                     (detail[:500] or "Print job failed", agent["id"]))
        conn.execute("UPDATE orders SET status = 'failed', updated_at = ? WHERE shop_id = ? AND id = ?",
                     (timestamp, agent["shop_id"], job["order_id"]))
        notify(conn, agent["shop_id"], "owner", "Automatic print failed", detail[:400] or "The Print Agent could not submit the job.", job["order_id"])
    else:
        status = "submitted" if state == "submitted" else "spooler_reported"
        conn.execute("UPDATE print_jobs SET status = ?, spooler_job_id = coalesce(?, spooler_job_id), error_message = NULL, updated_at = ? WHERE id = ?",
                     (status, spooler_job_id, timestamp, job_id))
        if state == "submitted":
            conn.execute("UPDATE print_agents SET active_job_id = NULL WHERE id = ?", (agent["id"],))
            conn.execute("UPDATE orders SET status = 'printing', updated_at = ? WHERE shop_id = ? AND id = ?",
                         (timestamp, agent["shop_id"], job["order_id"]))
    conn.execute("INSERT INTO print_job_attempts(id, shop_id, print_job_id, agent_id, state, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (str(uuid.uuid4()), agent["shop_id"], job_id, agent["id"], state, detail[:500] or None, timestamp))
    log_action(conn, agent["shop_id"], None, f"print_agent.{state}", "print_job", job_id)
    return dict(conn.execute("SELECT * FROM print_jobs WHERE id = ?", (job_id,)).fetchone())


def owner_confirm_printed(conn: sqlite3.Connection, shop_id: str, job_id: str, actor_id: str) -> dict:
    job = conn.execute("SELECT * FROM print_jobs WHERE shop_id = ? AND id = ?", (shop_id, job_id)).fetchone()
    if not job:
        raise ValueError("Print job not found.")
    if job["status"] == "completed":
        return dict(job)
    if job["status"] not in ("submitted", "spooler_reported", "submitting") and not (
            job["status"] in ("queued", "printing") and job["method"] == "manual"):
        raise ValueError("This print job cannot be marked physically printed from its current state.")
    conn.execute("UPDATE print_jobs SET status = 'completed', updated_at = ? WHERE id = ?", (now_iso(), job_id))
    conn.execute("INSERT INTO print_job_attempts(id, shop_id, print_job_id, state, detail, created_at) VALUES (?, ?, ?, 'physically_confirmed', ?, ?)",
                 (str(uuid.uuid4()), shop_id, job_id, f"Confirmed by {actor_id}", now_iso()))
    unfinished = conn.execute("SELECT count(*) FROM print_jobs WHERE shop_id = ? AND order_id = ? AND status != 'completed'",
                              (shop_id, job["order_id"])).fetchone()[0]
    if unfinished == 0:
        conn.execute("UPDATE orders SET status = 'completed', updated_at = ? WHERE shop_id = ? AND id = ?",
                     (now_iso(), shop_id, job["order_id"]))
        notify(conn, shop_id, "owner", "Order completed", "All print jobs were confirmed by shop staff.", job["order_id"])
    log_action(conn, shop_id, actor_id, "print.physically_confirmed", "print_job", job_id)
    return dict(conn.execute("SELECT * FROM print_jobs WHERE id = ?", (job_id,)).fetchone())


def resolve_unknown_submission(conn: sqlite3.Connection, shop_id: str, job_id: str,
                               result: str, actor_id: str) -> dict:
    job = conn.execute("SELECT * FROM print_jobs WHERE shop_id = ? AND id = ? AND method = 'automatic'",
                       (shop_id, job_id)).fetchone()
    if not job or job["status"] != "submitting":
        raise ValueError("This job does not have an unknown spooler result to review.")
    if result == "printed":
        return owner_confirm_printed(conn, shop_id, job_id, actor_id)
    if result != "not_printed":
        raise ValueError("Check the printer and choose whether it printed or did not print.")
    detail = "Spooler output was checked by staff; no print was found. Safe to retry."
    conn.execute("UPDATE print_jobs SET status = 'failed', error_message = ?, updated_at = ? WHERE shop_id = ? AND id = ?",
                 (detail, now_iso(), shop_id, job_id))
    conn.execute("UPDATE print_agents SET active_job_id = NULL WHERE id = ?", (job["agent_id"],))
    conn.execute("UPDATE orders SET status = 'failed', updated_at = ? WHERE shop_id = ? AND id = ?",
                 (now_iso(), shop_id, job["order_id"]))
    conn.execute("INSERT INTO print_job_attempts(id, shop_id, print_job_id, state, detail, created_at) VALUES (?, ?, ?, 'not_printed_confirmed', ?, ?)",
                 (str(uuid.uuid4()), shop_id, job_id, f"Confirmed by {actor_id}", now_iso()))
    log_action(conn, shop_id, actor_id, "print.unknown_submission_resolved", "print_job", job_id,
               {"result": "not_printed"})
    notify(conn, shop_id, "owner", "Print job ready for retry", detail, job["order_id"])
    return dict(conn.execute("SELECT * FROM print_jobs WHERE id = ?", (job_id,)).fetchone())


def update_queue_job(conn: sqlite3.Connection, shop_id: str, job_id: str, action: str,
                     actor_id: str, priority: int | None = None) -> dict:
    job = conn.execute("SELECT * FROM print_jobs WHERE shop_id = ? AND id = ?", (shop_id, job_id)).fetchone()
    if not job:
        raise ValueError("Print job not found.")
    if action == "priority":
        if isinstance(priority, bool) or not isinstance(priority, int) or not -100 <= priority <= 100:
            raise ValueError("Queue priority must be a whole number between -100 and 100.")
        conn.execute("UPDATE print_jobs SET priority = ?, updated_at = ? WHERE shop_id = ? AND id = ?",
                     (priority, now_iso(), shop_id, job_id))
    elif action == "pause" and job["status"] == "queued":
        conn.execute("UPDATE print_jobs SET status = 'paused', updated_at = ? WHERE shop_id = ? AND id = ?",
                     (now_iso(), shop_id, job_id))
    elif action == "resume" and job["status"] == "paused":
        conn.execute("UPDATE print_jobs SET status = 'queued', updated_at = ? WHERE shop_id = ? AND id = ?",
                     (now_iso(), shop_id, job_id))
    elif action == "cancel" and job["status"] in ("queued", "paused"):
        conn.execute("UPDATE print_jobs SET status = 'cancelled', updated_at = ? WHERE shop_id = ? AND id = ?",
                     (now_iso(), shop_id, job_id))
    else:
        raise ValueError("This job cannot be paused or resumed from its current state.")
    log_action(conn, shop_id, actor_id, f"print.queue.{action}", "print_job", job_id,
               {"priority": priority} if action == "priority" else {})
    return dict(conn.execute("SELECT * FROM print_jobs WHERE id = ?", (job_id,)).fetchone())


def manual_job_state(conn: sqlite3.Connection, shop_id: str, job_id: str,
                     status: str, actor_id: str, detail: str = "") -> dict:
    job = conn.execute("SELECT * FROM print_jobs WHERE shop_id = ? AND id = ? AND method = 'manual'",
                       (shop_id, job_id)).fetchone()
    if not job:
        raise ValueError("Manual print job not found.")
    if status == "printing" and job["status"] in ("queued", "paused"):
        conn.execute("UPDATE print_jobs SET status = 'printing', updated_at = ? WHERE id = ?",
                     (now_iso(), job_id))
        conn.execute("UPDATE orders SET status = 'printing', updated_at = ? WHERE shop_id = ? AND id = ?",
                     (now_iso(), shop_id, job["order_id"]))
        conn.execute("INSERT INTO print_job_attempts(id, shop_id, print_job_id, state, detail, created_at) "
                     "VALUES (?, ?, ?, 'manual_printing', ?, ?)",
                     (str(uuid.uuid4()), shop_id, job_id, f"Started by {actor_id}", now_iso()))
        log_action(conn, shop_id, actor_id, "print.manual_started", "print_job", job_id)
        return dict(conn.execute("SELECT * FROM print_jobs WHERE id = ?", (job_id,)).fetchone())
    if status == "failed":
        conn.execute("UPDATE print_jobs SET status = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
                     (detail[:500] or "Manual printing failed.", now_iso(), job_id))
        conn.execute("UPDATE orders SET status = 'failed', updated_at = ? WHERE shop_id = ? AND id = ?",
                     (now_iso(), shop_id, job["order_id"]))
        notify(conn, shop_id, "owner", "Manual print failed", detail[:400] or "Switch this job to automatic printing or retry manually.", job["order_id"])
        log_action(conn, shop_id, actor_id, "print.manual_failed", "print_job", job_id)
        return dict(conn.execute("SELECT * FROM print_jobs WHERE id = ?", (job_id,)).fetchone())
    return owner_confirm_printed(conn, shop_id, job_id, actor_id)
