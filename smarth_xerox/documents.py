from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path

from . import config
from .db import now_iso

PDF_PAGE = re.compile(rb"/Type\s*/Page\b")
PDF_COUNT = re.compile(rb"/Type\s*/Pages\b.{0,300}?/Count\s+(\d+)\b", re.DOTALL)
SAFE_NAME = re.compile(r"[^A-Za-z0-9._() -]+")
SIGNATURES = {
    "pdf": lambda data: data[:1024].lstrip().startswith(b"%PDF-"),
    "jpg": lambda data: data.startswith(b"\xff\xd8\xff"),
    "jpeg": lambda data: data.startswith(b"\xff\xd8\xff"),
    "png": lambda data: data.startswith(b"\x89PNG\r\n\x1a\n"),
}


def safe_original_name(name: str) -> str:
    leaf = name.replace("\\", "/").split("/")[-1]
    cleaned = SAFE_NAME.sub("_", leaf).strip(" .")[:120]
    return cleaned or "document"


def _page_count(data: bytes, extension: str) -> int | None:
    if extension in ("jpg", "jpeg", "png"):
        return 1
    page_nodes = len(PDF_PAGE.findall(data))
    if page_nodes:
        return page_nodes
    counts = [int(value) for value in PDF_COUNT.findall(data)]
    return max(counts) if counts else None


def _convert_office(data: bytes, original_name: str) -> tuple[bytes, str]:
    converter = shutil.which("libreoffice") or shutil.which("soffice")
    if not converter:
        raise ValueError("DOC/DOCX conversion is unavailable on this server. Upload a PDF instead.")
    suffix = Path(original_name).suffix.lower()
    if suffix not in (".doc", ".docx"):
        raise ValueError("This document format is not supported.")
    with tempfile.TemporaryDirectory(prefix="smx-convert-") as tmp:
        source = Path(tmp) / f"input{suffix}"
        source.write_bytes(data)
        result = subprocess.run(
            [converter, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(source)],
            capture_output=True, timeout=45, check=False,
        )
        output = Path(tmp) / "input.pdf"
        if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            raise ValueError("The office document could not be converted. Check the file and upload a PDF.")
        return output.read_bytes(), "pdf"


def validate_document(data: bytes, original_name: str, settings: dict) -> tuple[bytes, str, str, int | None, bool]:
    name = safe_original_name(original_name)
    extension = Path(name).suffix.lower().lstrip(".")
    allowed = {str(item).lower().lstrip(".") for item in settings["general"].get("formats", [])}
    if extension not in allowed:
        raise ValueError(f"{extension.upper() or 'This'} format is not enabled by the shop.")
    if not data:
        raise ValueError("The selected file is empty.")
    if len(data) > settings["general"]["max_file_bytes"]:
        raise ValueError("This file is larger than the shop's upload limit.")
    if extension in ("doc", "docx"):
        data, extension = _convert_office(data, name)
    signature = SIGNATURES.get(extension)
    if not signature or not signature(data):
        raise ValueError("The file contents do not match a supported document format.")
    if extension == "pdf" and b"%%EOF" not in data[-2048:]:
        raise ValueError("This PDF appears incomplete or corrupted. Export it again and retry.")
    if extension in ("jpg", "jpeg") and not data.endswith(b"\xff\xd9"):
        raise ValueError("This JPEG appears incomplete or corrupted. Export it again and retry.")
    if extension == "png" and b"IEND\xaeB`\x82" not in data[-20:]:
        raise ValueError("This PNG appears incomplete or corrupted. Export it again and retry.")
    mime = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}[extension]
    pages = _page_count(data, extension)
    max_pages = settings["general"].get("max_pages", 1000)
    if pages is not None and pages > max_pages:
        raise ValueError(f"This document exceeds the shop's {max_pages}-page limit.")
    needs_review = pages is None
    if pages is None and extension != "pdf":
        raise ValueError("The file page count could not be verified.")
    return data, mime, extension, pages, needs_review


def store_document(shop_id: str, original_name: str, data: bytes) -> tuple[str, int]:
    opaque_name = f"{uuid.uuid4().hex}.pdf" if original_name.lower().endswith((".doc", ".docx")) else f"{uuid.uuid4().hex}{Path(original_name).suffix.lower()}"
    directory = (config.STORAGE / shop_id).resolve()
    root = config.STORAGE.resolve()
    if root not in directory.parents and directory != root:
        raise ValueError("Invalid storage destination.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / opaque_name
    temp = directory / f".{opaque_name}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return opaque_name, len(data)


def document_path(shop_id: str, storage_name: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:pdf|jpg|jpeg|png)", storage_name):
        raise ValueError("Invalid private document reference.")
    root = config.STORAGE.resolve()
    path = (root / shop_id / storage_name).resolve()
    if root not in path.parents or not path.is_file():
        raise FileNotFoundError("Document not found.")
    return path


def add_document(conn, shop_id: str, draft_id: str, original_name: str,
                 data: bytes, settings: dict) -> dict:
    converted, mime, extension, pages, needs_review = validate_document(data, original_name, settings)
    final_name = safe_original_name(original_name)
    if extension == "pdf" and Path(final_name).suffix.lower() in (".doc", ".docx"):
        final_name = f"{Path(final_name).stem}.pdf"
    storage_name, size = store_document(shop_id, final_name, converted)
    document_id = str(uuid.uuid4())
    defaults = {
        "color": "bw", "sides": "single", "paper_size": "A4", "copies": 1,
        "page_selection": "all", "add_ons": [], "orientation": "auto", "instructions": "",
    }
    try:
        conn.execute(
            "INSERT INTO documents(id, shop_id, draft_id, original_name, storage_name, mime_type, "
            "size_bytes, page_count, needs_review, settings_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (document_id, shop_id, draft_id, final_name, storage_name, mime, size, pages,
             int(needs_review), json.dumps(defaults), now_iso()),
        )
    except Exception:
        document_path(shop_id, storage_name).unlink(missing_ok=True)
        raise
    return {
        "id": document_id, "original_name": final_name, "mime_type": mime,
        "size_bytes": size, "page_count": pages, "needs_review": needs_review, "settings": defaults,
        "_storage_name": storage_name,
    }
