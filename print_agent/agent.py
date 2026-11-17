#!/usr/bin/env python3
"""Paired Smart Xerox printer worker. Run on the shop computer beside its printer."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

AGENT_VERSION = "1.0.0"
PAGE_LIST = re.compile(r"^(?:\d+(?:-\d+)?)(?:,\d+(?:-\d+)?)*$")
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024


def config_directory() -> Path:
    explicit = os.getenv("SMX_AGENT_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.name == "nt":
        return Path(os.getenv("APPDATA", str(Path.home()))) / "SmartXerox"
    return Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "smarth-xerox"


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        temp.unlink(missing_ok=True)


def save_config(value: dict) -> Path:
    path = config_directory() / "agent.json"
    _write_json(path, value)
    return path


def load_config(path: Path | None = None) -> dict:
    target = path or config_directory() / "agent.json"
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError("Print Agent is not paired. Run `python print_agent/agent.py pair` first.") from exc
    if not all(isinstance(value.get(key), str) and value[key] for key in ("server_url", "agent_id", "shop_id", "token")):
        raise RuntimeError("Print Agent configuration is incomplete. Pair it again with the shop.")
    return value


def _require_safe_server(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Print Agent requires HTTPS outside localhost.")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Enter a full server URL beginning with https://.")
    return url.rstrip("/")


def api_request(config_value: dict, method: str, path: str, payload: dict | None = None,
                max_bytes: int = 1024 * 1024) -> tuple[dict | None, bytes]:
    url = config_value["server_url"].rstrip("/") + path
    headers = {"Authorization": f"Bearer {config_value['token']}", "Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise RuntimeError("Server response exceeded the configured size limit.")
            content_type = response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        raw = exc.read(65536)
        try:
            detail = json.loads(raw).get("error", {}).get("message", "Request rejected by the server.")
        except Exception:
            detail = "Request rejected by the server."
        raise RuntimeError(detail[:300]) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ConnectionError("Could not reach the Smart Xerox server.") from exc
    if content_type == "application/json":
        return json.loads(body or b"{}"), b""
    return None, body


def pair(server_url: str, device_name: str) -> dict:
    server_url = _require_safe_server(server_url)
    pairing_code = getpass.getpass("One-time pairing code from the owner dashboard: ").strip()
    if len(pairing_code) < 20:
        raise ValueError("The pairing code is invalid or incomplete.")
    request = urllib.request.Request(
        server_url + "/api/agent/pair",
        data=json.dumps({"pairing_code": pairing_code, "device_name": device_name}).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read(65536))
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Pairing code was rejected. Generate a fresh code and try again.") from exc
    credentials = result.get("agent", {})
    credentials.update({"server_url": server_url, "token": credentials.pop("token", ""),
                        "windows_pdf_printer": os.getenv("SMX_SUMATRA_PATH", "")})
    if not credentials.get("token"):
        raise RuntimeError("The server did not return agent credentials.")
    path = save_config(credentials)
    print(f"Paired with shop {credentials.get('shop_id', '')[:8]}. Credentials saved to {path}.")
    print("Keep this user account private. Revoke this agent from the owner dashboard if the computer is replaced.")
    return credentials


def discover_printers() -> list[dict]:
    if os.name == "nt":
        script = ("Get-Printer | ForEach-Object { $c = Get-PrintConfiguration -PrinterName $_.Name -ErrorAction SilentlyContinue; "
                  "[PSCustomObject]@{name=$_.Name; status=$_.PrinterStatus.ToString(); is_default=$_.Default; "
                  "color=$c.Color; duplex=($c.DuplexingMode -and $c.DuplexingMode -ne 'OneSided'); paper_sizes=@($c.PaperSize.ToString())} } "
                  "| ConvertTo-Json -Compress")
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=20, check=False)
        if result.returncode != 0:
            return []
        try:
            parsed = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return []
        entries = parsed if isinstance(parsed, list) else [parsed]
        printers = []
        for item in entries:
            state = str(item.get("status", "")).lower()
            printers.append({"name": str(item.get("name", "")),
                             "status": "offline" if "offline" in state or "paused" in state else "ready",
                             "is_default": bool(item.get("is_default")), "color": item.get("color"),
                             "duplex": item.get("duplex"),
                             "paper_sizes": [str(item.get("paper_sizes", ""))] if item.get("paper_sizes") else []})
        return [printer for printer in printers if printer["name"]]
    if not shutil.which("lpstat"):
        return []
    status = subprocess.run(["lpstat", "-p", "-d"], capture_output=True, text=True, timeout=10, check=False)
    default_match = re.search(r"system default destination:\s*(.+)", status.stdout)
    default_name = default_match.group(1).strip() if default_match else ""
    discovered = []
    for line in status.stdout.splitlines():
        match = re.match(r"printer (.+?) is (idle|printing|disabled)(?:\s|$)", line)
        if not match:
            continue
        name, state = match.groups()
        options = subprocess.run(["lpoptions", "-p", name, "-l"], capture_output=True, text=True, timeout=10, check=False) if shutil.which("lpoptions") else None
        text = options.stdout if options else ""
        paper_line = next((entry for entry in text.splitlines() if entry.lower().startswith("pagesize/")), "")
        paper_sizes = re.findall(r"\b(A4|A3|Legal|Letter)\b", paper_line, flags=re.IGNORECASE)
        color_line = next((entry for entry in text.splitlines() if "color" in entry.lower() and "/" in entry), "")
        color_supported = bool(re.search(r"\b(RGB|CMYK|Color|True)\b", color_line, flags=re.IGNORECASE))
        duplex_line = next((entry for entry in text.splitlines() if "duplex" in entry.lower() and "/" in entry), "")
        duplex_supported = bool(re.search(r"\b(duplexno|duplextumble|duplexnotumble|true)\b", duplex_line, flags=re.IGNORECASE))
        discovered.append({"name": name, "status": "offline" if state == "disabled" else "ready",
                           "is_default": name == default_name,
                           "color": color_supported if color_line else None,
                           "duplex": duplex_supported if duplex_line else None,
                           "paper_sizes": sorted(set(size.upper() for size in paper_sizes))})
    return discovered


def _page_count_hint(data: bytes) -> None:
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("Downloaded document exceeded the Print Agent size limit.")
    if data.startswith(b"%PDF-") and b"%%EOF" in data[-2048:]:
        return
    if data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return
    if data.startswith(b"\x89PNG\r\n\x1a\n") and b"IEND\xaeB`\x82" in data[-20:]:
        return
    raise ValueError("Downloaded document failed its file signature check.")


def print_document(config_value: dict, printer_name: str, settings: dict, ticket: str,
                   path: Path, runner=subprocess.run) -> str | None:
    copies = settings.get("copies", 1)
    if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= 99:
        raise ValueError("Invalid print copy count.")
    page_selection = settings.get("page_selection", "all")
    if page_selection != "all" and not PAGE_LIST.fullmatch(str(page_selection).replace(" ", "")):
        raise ValueError("Invalid page range supplied for printing.")
    if os.name == "nt":
        executable = config_value.get("windows_pdf_printer") or os.getenv("SMX_SUMATRA_PATH", "")
        if not executable or not Path(executable).is_file():
            raise RuntimeError("Install SumatraPDF and set SMX_SUMATRA_PATH before enabling Windows automatic printing.")
        settings_list = []
        if page_selection != "all":
            settings_list.append(str(page_selection).replace(" ", ""))
        settings_list.append(f"{copies}x")
        settings_list.append("duplexlong" if settings.get("sides") == "double" else "simplex")
        if settings.get("color") != "color":
            settings_list.append("monochrome")
        paper = {"A4": "A4", "A3": "A3", "LEGAL": "legal", "LETTER": "letter"}.get(settings.get("paper_size", "A4"))
        if paper:
            settings_list.append(f"paper={paper}")
        if settings.get("orientation") in ("portrait", "landscape"):
            settings_list.append(settings["orientation"])
        result = runner([executable, "-print-to", printer_name, "-print-settings", ",".join(settings_list),
                         "-silent", str(path)], capture_output=True, text=True, timeout=120, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"Windows print bridge exited with code {result.returncode}.")
        return None
    if not shutil.which("lp"):
        raise RuntimeError("CUPS `lp` is not installed on this computer.")
    args = ["lp", "-d", printer_name, "-n", str(copies), "-t", ticket]
    if page_selection != "all":
        args.extend(["-P", str(page_selection).replace(" ", "")])
    if settings.get("sides") == "double":
        sides = "two-sided-short-edge" if settings.get("orientation") == "landscape" else "two-sided-long-edge"
    else:
        sides = "one-sided"
    args.extend(["-o", f"sides={sides}"])
    args.extend(["-o", "ColorModel=RGB" if settings.get("color") == "color" else "ColorModel=Gray"])
    args.extend(["-o", "media=" + str(settings.get("paper_size", "A4")), str(path)])
    result = runner(args, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "CUPS rejected the print job.").strip()[:300]
        raise RuntimeError(detail)
    match = re.search(r"request id is\s+([^\s]+)", result.stdout or "")
    return match.group(1) if match else None


def _ledger_path() -> Path:
    return config_directory() / "pending-reports.json"


def _read_ledger() -> dict:
    try:
        return json.loads(_ledger_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        raise RuntimeError("Agent report ledger is corrupted. Preserve the file and contact support before clearing it.")


def _save_ledger(ledger: dict) -> None:
    _write_json(_ledger_path(), ledger)


def _post_report(config_value: dict, job_id: str, state: str, detail: str = "",
                 spooler_job_id: str | None = None) -> None:
    api_request(config_value, "POST", f"/api/agent/jobs/{job_id}/report", {
        "state": state, "detail": detail[:500], "spooler_job_id": spooler_job_id,
    })


def flush_reports(config_value: dict, logger: logging.Logger) -> str | None:
    ledger = _read_ledger()
    active_id = None
    for job_id, report in list(ledger.items()):
        if report.get("state") == "submitting":
            active_id = job_id
            continue  # outcome is uncertain; never resubmit without owner review
        if report.get("state") not in ("submitted", "failed"):
            continue
        try:
            _post_report(config_value, job_id, report["state"], report.get("detail", ""), report.get("spooler_job_id"))
        except (ConnectionError, RuntimeError) as exc:
            logger.warning("Could not report job %s yet: %s", job_id[:8], exc)
            continue
        ledger.pop(job_id, None)
        _save_ledger(ledger)
    return active_id


def _store_report(job_id: str, state: str, detail: str = "", spooler_job_id: str | None = None) -> None:
    ledger = _read_ledger()
    ledger[job_id] = {"state": state, "detail": detail[:500], "spooler_job_id": spooler_job_id}
    _save_ledger(ledger)


def _remove_report(job_id: str) -> None:
    ledger = _read_ledger()
    if job_id in ledger:
        ledger.pop(job_id)
        _save_ledger(ledger)


def process_job(config_value: dict, job: dict, logger: logging.Logger) -> str | None:
    job_id = job.get("id", "")
    if not re.fullmatch(r"[a-f0-9-]{36}", job_id):
        raise ValueError("Server sent an invalid print job identifier.")
    ledger = _read_ledger()
    saved = ledger.get(job_id)
    if saved and saved.get("state") in ("submitted", "failed", "submitting"):
        flush_reports(config_value, logger)
        return job_id if saved.get("state") == "submitting" else None
    content_type, data = None, b""
    try:
        # The job ID is a server-generated UUID; the download path is constrained to this agent API.
        _, data = api_request(config_value, "GET", f"/api/agent/jobs/{job_id}/document", max_bytes=MAX_DOCUMENT_BYTES)
        _page_count_hint(data)
        suffix = ".pdf" if data.startswith(b"%PDF-") else ".jpg" if data.startswith(b"\xff\xd8\xff") else ".png"
        with tempfile.TemporaryDirectory(prefix="smx-print-") as directory:
            document = Path(directory) / f"{job_id}{suffix}"
            document.write_bytes(data)
            _store_report(job_id, "submitting")
            _post_report(config_value, job_id, "submission_started")
            try:
                spooler_id = print_document(config_value, job["printer"], job["settings"], job["ticket"], document)
            except subprocess.TimeoutExpired:
                logger.error("Print command timed out for job %s; submission state requires owner review.", job_id[:8])
                return job_id
            except Exception as exc:
                detail = str(exc)[:500]
                _store_report(job_id, "failed", detail)
                try:
                    flush_reports(config_value, logger)
                except Exception:
                    logger.exception("Could not report failed print job %s", job_id[:8])
                return None
            _store_report(job_id, "submitted", spooler_job_id=spooler_id)
            try:
                flush_reports(config_value, logger)
            except Exception:
                logger.exception("Job %s was submitted but its status report is queued locally.", job_id[:8])
            logger.info("Submitted job %s for ticket %s to printer %s.", job_id[:8], job["ticket"], job["printer"])
            return None
    except (ConnectionError, RuntimeError, ValueError) as exc:
        # If no OS submission started, the retry is safe and bounded by owner policy.
        current = _read_ledger().get(job_id)
        if not current or current.get("state") != "submitting":
            _store_report(job_id, "failed", str(exc)[:500])
            try:
                flush_reports(config_value, logger)
            except Exception:
                logger.exception("Could not report job download failure %s", job_id[:8])
        else:
            logger.error("Job %s has uncertain submission state; operator review is required: %s", job_id[:8], exc)
        return job_id if current and current.get("state") == "submitting" else None


def heartbeat(config_value: dict, printers: list[dict], active_job_id: str | None, last_error: str | None = None) -> None:
    api_request(config_value, "POST", "/api/agent/heartbeat", {
        "version": AGENT_VERSION, "printers": printers, "active_job_id": active_job_id, "last_error": last_error,
    })


def run_agent(config_value: dict) -> None:
    directory = config_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger = logging.getLogger("smarth_xerox.print_agent")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(directory / "agent.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    logger.info("Smart Xerox Print Agent %s starting for shop %s.", AGENT_VERSION, config_value["shop_id"][:8])
    pending_active = flush_reports(config_value, logger)
    backoff = 2
    while True:
        try:
            printers = discover_printers()
            active_job = pending_active
            heartbeat(config_value, printers, active_job)
            pending_active = flush_reports(config_value, logger)
            active_job = pending_active
            if active_job:
                time.sleep(5)
                continue
            result, _ = api_request(config_value, "GET", "/api/agent/jobs/next")
            job = result.get("job") if result else None
            if job:
                pending_active = process_job(config_value, job, logger)
                continue
            backoff = 2
            time.sleep(5)
        except KeyboardInterrupt:
            logger.info("Print Agent stopping.")
            return
        except (ConnectionError, TimeoutError) as exc:
            logger.warning("Server connection failed: %s; retrying in %s seconds.", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception:
            logger.exception("Print Agent cycle failed; reconnecting in %s seconds.", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def make_test_pdf() -> bytes:
    stream = b"BT /F1 20 Tf 72 720 Td (Smart Xerox Print Agent test page) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, content in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode() + content + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(pdf)


def local_test_print(config_value: dict) -> None:
    printers = discover_printers()
    if not printers:
        raise RuntimeError("No printers were discovered. Check CUPS or Windows printer settings first.")
    ready = [printer for printer in printers if printer["status"] == "ready"]
    if not ready:
        raise RuntimeError("No ready printers were found.")
    printer = next((item for item in ready if item["is_default"]), ready[0])
    print("Discovered printers:")
    for index, item in enumerate(ready, start=1):
        print(f"{index}. {item['name']}" + (" (default)" if item["is_default"] else ""))
    answer = input(f"Send a one-page test print to {printer['name']}? Type YES to continue: ")
    if answer != "YES":
        print("Test print cancelled.")
        return
    with tempfile.TemporaryDirectory(prefix="smx-test-print-") as directory:
        path = Path(directory) / "smarth-xerox-test.pdf"
        path.write_bytes(make_test_pdf())
        spooler_id = print_document(config_value, printer["name"], {
            "copies": 1, "page_selection": "all", "sides": "single", "color": "bw", "paper_size": "A4",
        }, "TEST", path)
    print(f"Submitted to the local print service. Spooler reference: {spooler_id or 'not returned'}.")
    print("Confirm that paper came out correctly at the shop printer.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Smart Xerox local printer agent")
    sub = parser.add_subparsers(dest="command", required=True)
    pair_parser = sub.add_parser("pair", help="pair this computer with a shop")
    pair_parser.add_argument("--url", required=True, help="shop system URL")
    pair_parser.add_argument("--name", default=platform.node() or "Shop printer computer")
    sub.add_parser("run", help="run the automatic print worker")
    sub.add_parser("test", help="discover a printer and send a confirmed test page")
    args = parser.parse_args()
    if args.command == "pair":
        pair(args.url, args.name)
    elif args.command == "test":
        local_test_print(load_config())
    else:
        run_agent(load_config())


if __name__ == "__main__":
    main()
