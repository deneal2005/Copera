import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from smarth_xerox.accounts import DEFAULT_SETTINGS, save_setting, setup_shop, shop_settings
from smarth_xerox.db import connect, migrate
from smarth_xerox.documents import add_document
from smarth_xerox.orders import create_draft, create_order, find_draft
from smarth_xerox.printing import (agent_document, claim_next_job, create_pairing_code,
                                   create_print_jobs, owner_confirm_printed, pair_agent,
                                   report_job, resolve_unknown_submission, update_heartbeat,
                                   authenticate_agent)
from smarth_xerox.payments import mark_counter_paid
from print_agent import agent as print_agent
from smarth_xerox import config


class AgentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "agent-test.sqlite3"
        self.old_storage = config.STORAGE
        config.STORAGE = Path(self.temp.name) / "private"
        migrate(self.database)
        self.conn = connect(self.database)
        with self.conn:
            self.shop_id, self.owner_id = setup_shop(
                self.conn, "North Copy", "north-copy", "owner@example.test", "a long and unique passphrase",
            )
            save_setting(self.conn, self.shop_id, "pricing", {
                "rates": {"A4": {"bw_simplex_per_side": 200, "bw_duplex_per_sheet": 125,
                                  "color_simplex_per_side": 1000, "color_duplex_per_sheet": 750}},
                "minimum_order_paise": 0, "service_fee_paise": 0, "tax_rate_bps": 0,
                "binding_per_copy_paise": 0, "stapling_per_copy_paise": 0,
            }, self.owner_id, tuple(DEFAULT_SETTINGS))
            current = shop_settings(self.conn, self.shop_id)
            current["printing"].update({"automatic_enabled": True, "retry_limit": 2})
            save_setting(self.conn, self.shop_id, "printing", current["printing"], self.owner_id, tuple(DEFAULT_SETTINGS))
            shop = dict(self.conn.execute("SELECT * FROM shops WHERE id = ?", (self.shop_id,)).fetchone())
            draft, token = create_draft(self.conn, self.shop_id, "agent-draft-001")
            pdf = b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF"
            document = add_document(self.conn, self.shop_id, draft["id"], "sample.pdf", pdf, shop_settings(self.conn, self.shop_id))
            current_draft = find_draft(self.conn, self.shop_id, token)
            self.order, _, _ = create_order(self.conn, shop, current_draft, token, "Asha", "9876543210",
                                            "counter", "agent-order-001", shop_settings(self.conn, self.shop_id))
            mark_counter_paid(self.conn, self.shop_id, self.order["id"], self.owner_id)
            self.job = create_print_jobs(self.conn, self.shop_id, self.order["id"], "automatic", self.owner_id,
                                         shop_settings(self.conn, self.shop_id))[0]
            code, _ = create_pairing_code(self.conn, self.shop_id, self.owner_id)
            self.paired = pair_agent(self.conn, code, "Test printer computer")
            agent = authenticate_agent(self.conn, self.paired["token"])
            update_heartbeat(self.conn, agent, "1.0.0", [{"name": "Xerox A4", "status": "ready",
                                                           "is_default": True, "color": False,
                                                           "duplex": True, "paper_sizes": ["A4"]}])
        self.agent = authenticate_agent(self.conn, self.paired["token"])

    def tearDown(self):
        self.conn.close()
        config.STORAGE = self.old_storage
        self.temp.cleanup()

    def test_agent_claim_download_and_physical_confirmation_are_separate(self):
        self.assertIsNotNone(self.agent["last_heartbeat_at"])
        job = claim_next_job(self.conn, self.agent, shop_settings(self.conn, self.shop_id))
        self.assertEqual(job["id"], self.job["id"])
        document = agent_document(self.conn, self.agent, job["id"])
        self.assertEqual(document["original_name"], "sample.pdf")
        self.assertEqual(report_job(self.conn, self.agent, job["id"], "submission_started")["status"], "submitting")
        self.assertEqual(report_job(self.conn, self.agent, job["id"], "submitted", "spooler accepted", "CUPS-41")["status"], "submitted")
        self.assertEqual(self.conn.execute("SELECT status FROM orders WHERE id = ?", (self.order["id"],)).fetchone()[0], "printing")
        self.assertEqual(owner_confirm_printed(self.conn, self.shop_id, job["id"], self.owner_id)["status"], "completed")
        self.assertEqual(self.conn.execute("SELECT status FROM orders WHERE id = ?", (self.order["id"],)).fetchone()[0], "completed")

    def test_unknown_spooler_outcome_needs_owner_resolution_before_retry(self):
        job = claim_next_job(self.conn, self.agent, shop_settings(self.conn, self.shop_id))
        agent_document(self.conn, self.agent, job["id"])
        report_job(self.conn, self.agent, job["id"], "submission_started")
        self.assertEqual(resolve_unknown_submission(self.conn, self.shop_id, job["id"], "not_printed", self.owner_id)["status"], "failed")
        self.assertIsNone(claim_next_job(self.conn, self.agent, shop_settings(self.conn, self.shop_id)))
        manual = create_print_jobs(self.conn, self.shop_id, self.order["id"], "manual", self.owner_id,
                                   shop_settings(self.conn, self.shop_id))[0]
        self.assertEqual(manual["method"], "manual")


class LocalSpoolerTests(unittest.TestCase):
    def test_cups_arguments_are_separate_and_page_selection_is_validated(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="request id is Xerox-42", stderr="")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private.pdf"
            path.write_bytes(b"%PDF-1.4\n%%EOF")
            with patch.object(print_agent.shutil, "which", return_value="/usr/bin/lp"):
                spool_id = print_agent.print_document({}, "Xerox A4", {"copies": 2, "page_selection": "1-3, 5",
                    "sides": "double", "color": "bw", "paper_size": "A4"}, "XRX-2026-00001", path, runner)
                self.assertEqual(spool_id, "Xerox-42")
                self.assertIn("1-3,5", calls[0])
                self.assertIn("sides=two-sided-long-edge", calls[0])
                with self.assertRaisesRegex(ValueError, "page range"):
                    print_agent.print_document({}, "Xerox A4", {"copies": 1, "page_selection": "1; rm -rf /",
                        "sides": "single", "color": "bw", "paper_size": "A4"}, "ticket", path, runner)
        self.assertEqual(len(calls), 1)

    def test_downloaded_file_must_match_supported_signature(self):
        print_agent._page_count_hint(b"%PDF-1.4\n%%EOF")
        with self.assertRaisesRegex(ValueError, "signature check"):
            print_agent._page_count_hint(b"not a PDF")


if __name__ == "__main__":
    unittest.main()
