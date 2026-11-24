import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from conftest_helpers import DATA

from retail_return_quality_action import cli


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "app.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--db", self.db, *args])
        return code, out.getvalue(), err.getvalue()

    def test_end_to_end_flow(self):
        code, out, _ = self.run_cli("import", "--dir", str(DATA))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload), 15)
        code, out, _ = self.run_cli("review", "apply")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("analyze", "--as-of", "2026-11-25")
        self.assertEqual(code, 0)
        analysis_payload = json.loads(out)
        self.assertEqual(analysis_payload["snapshot_id"], 1)
        top_lot = analysis_payload["lot"][0]
        self.assertEqual(top_lot["entity_id"], "LOT-006")
        self.assertEqual(top_lot["proposed_level"], "escalate")
        code, out, _ = self.run_cli(
            "open-case", "--level", "lot", "--id", "LOT-006", "--actor", "QI-01",
            "--reason", "质检重复确认缺陷",
        )
        case = json.loads(out)
        self.assertEqual(case["case_id"], "CASE-0001")
        code, out, _ = self.run_cli(
            "grade", case["case_id"], "--grade", "escalate", "--actor", "QI-01", "--reason", "确认升级",
        )
        self.assertEqual(json.loads(out)["grade"], "escalate")
        code, out, _ = self.run_cli(
            "decide", case["case_id"], "--decision", "hold-shipment", "--role", "supply",
            "--actor", "SC-01", "--reason", "暂停未发货批次", "--lot", "LOT-006",
        )
        action_id = json.loads(out)["action_id"]
        code, out, _ = self.run_cli(
            "action", action_id, "--to", "confirmed", "--role", "supply", "--actor", "SC-01"
        )
        self.assertEqual(json.loads(out)["decision_state"], "confirmed")
        code, out, _ = self.run_cli(
            "receipt", "--receipt-ref", "RCP-CLI", "--type", "hold-ack", "--outcome", "accepted",
            "--received-at", "2026-11-27", "--lot", "LOT-006", "--actor", "SC-01",
        )
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli(
            "action", action_id, "--to", "in_progress", "--role", "supply", "--actor", "SC-01"
        )
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli(
            "action", action_id, "--to", "complete", "--role", "supply", "--actor", "SC-01",
            "--receipt", "RCP-CLI",
        )
        self.assertEqual(json.loads(out)["decision_state"], "complete")
        code, out, _ = self.run_cli(
            "check", case["case_id"], "--cutoff", "2027-01-31", "--anchor", "2026-11-27"
        )
        self.assertEqual(json.loads(out)["verdict"], "improved")
        code, out, _ = self.run_cli(
            "report", "--out", str(self.tmp / "reports" / "report.md")
        )
        self.assertTrue(Path(json.loads(out)["markdown"]).exists())
        code, out, _ = self.run_cli("cases")
        cases = json.loads(out)
        self.assertEqual(cases[0]["status"], "investigating")

    def test_error_exit_code(self):
        code, out, err = self.run_cli("grade", "CASE-9999", "--grade", "watch", "--actor", "X", "--reason", "Y")
        self.assertEqual(code, 2)
        self.assertIn("案件不存在", err)

    def test_show_before_analyze_fails_cleanly(self):
        self.run_cli("import", "--dir", str(DATA))
        code, out, err = self.run_cli("show", "--level", "lot")
        self.assertEqual(code, 2)
        self.assertIn("风险快照", err)


if __name__ == "__main__":
    unittest.main()
