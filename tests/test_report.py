import json
import shutil
import tempfile
import unittest
from pathlib import Path

from conftest_helpers import import_all, fresh_db

from retail_return_quality_action import analysis, report, workflow
from retail_return_quality_action.config import load_settings


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conn = fresh_db(self.tmp)
        import_all(self.conn)
        self.settings = load_settings()
        self.result = analysis.analyze(self.conn, as_of="2026-11-25", settings=self.settings)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_markdown_lists_required_disclaimers(self):
        text = report.markdown_report(self.conn, settings=self.settings)
        for fragment in (
            "分析截止日",
            "成熟销售暴露",
            "原因未知",
            "渠道与政策分层",
            "数据质量与未知范围",
            "商品说明换版只解释",
            "已执行的退款与供应商处置保留当时决定",
        ):
            self.assertIn(fragment, text)

    def test_markdown_marks_investigate_and_escalate(self):
        text = report.markdown_report(self.conn, settings=self.settings)
        self.assertIn("LOT-006", text)
        self.assertIn("升级", text)
        self.assertIn("调查", text)

    def test_markdown_reports_unknown_and_gaps(self):
        text = report.markdown_report(self.conn, settings=self.settings)
        self.assertIn("原因编码不在有效映射", text)
        self.assertIn("批次号占位", text)

    def test_export_files_written(self):
        folder = self.tmp / "reports"
        path = report.write_markdown_report(
            self.conn, str(folder / "report.md"), settings=self.settings
        )
        self.assertTrue(Path(path).exists())
        exported = report.export_results(self.conn, out_dir=str(folder))
        for name, file in exported.items():
            self.assertTrue(Path(file).exists(), name)
        manifest = json.loads(Path(exported["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["snapshot_id"], self.result.snapshot_id)
        self.assertIn("overall", manifest)

    def test_decision_log_and_refund_history(self):
        case = workflow.open_case(
            self.conn, "lot", "LOT-006", actor="QI-01", rationale="质检重复确认缺陷",
        )
        workflow.record_grade(
            self.conn, case["case_id"], grade="escalate", actor="QI-01", rationale="确认升级"
        )
        log = report.decision_log(self.conn)
        self.assertTrue(any(row["decision_type"] == "grade" for row in log))
        refunds = report.refund_state_history(self.conn)
        self.assertEqual(len(refunds), 70)
        self.assertTrue(all("refund_state" in row for row in refunds))


if __name__ == "__main__":
    unittest.main()
