import shutil
import tempfile
import unittest
from pathlib import Path

from conftest_helpers import import_all, fresh_db

from retail_return_quality_action import analysis, db, workflow
from retail_return_quality_action.config import load_settings
from retail_return_quality_action.errors import NotFoundError, PermissionDenied, WorkflowError


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conn = fresh_db(self.tmp)
        import_all(self.conn)
        self.settings = load_settings()
        self.result = analysis.analyze(self.conn, as_of="2026-11-25", settings=self.settings)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def open(self, level="lot", entity="LOT-006"):
        return workflow.open_case(
            self.conn, level, entity, actor="QI-01", rationale="质检重复确认缺陷，退货分层后偏高",
        )

    def test_open_case_is_idempotent(self):
        first = self.open()
        second = self.open()
        self.assertEqual(first["case_id"], second["case_id"])
        self.assertEqual(first["proposed_level"], "escalate")

    def test_grade_requires_quality_role(self):
        case = self.open()
        with self.assertRaises(PermissionDenied):
            workflow.record_grade(
                self.conn, case["case_id"], grade="escalate", actor="PD-01",
                rationale="越权", role="product",
            )
        graded = workflow.record_grade(
            self.conn, case["case_id"], grade="escalate", actor="QI-01", rationale="确认升级",
        )
        self.assertEqual(graded["grade"], "escalate")
        self.assertEqual(graded["status"], "investigating")

    def test_grade_requires_rationale(self):
        case = self.open()
        with self.assertRaises(WorkflowError):
            workflow.record_grade(self.conn, case["case_id"], grade="watch", actor="QI-01", rationale=" ")

    def test_cross_role_decision_denied(self):
        case = self.open()
        workflow.record_grade(
            self.conn, case["case_id"], grade="escalate", actor="QI-01", rationale="确认"
        )
        with self.assertRaises(PermissionDenied):
            workflow.record_decision(
                self.conn, case["case_id"], decision="hold-shipment", actor="PD-01",
                role="product", rationale="越权决定供应链",
            )
        with self.assertRaises(PermissionDenied):
            workflow.record_decision(
                self.conn, case["case_id"], decision="revise-copy", actor="SC-01",
                role="supply", rationale="越权决定商品说明",
            )

    def test_supply_hold_requires_receipt(self):
        case = self.open()
        workflow.record_grade(
            self.conn, case["case_id"], grade="escalate", actor="QI-01", rationale="确认"
        )
        action_id = workflow.record_decision(
            self.conn, case["case_id"], decision="hold-shipment", actor="SC-01",
            role="supply", rationale="暂停未发货批次", lot_id="LOT-006",
        )["action_id"]
        workflow.transition_action(self.conn, action_id, to_state="confirmed", actor="SC-01", role="supply")
        workflow.transition_action(self.conn, action_id, to_state="in_progress", actor="SC-01", role="supply")
        with self.assertRaises(WorkflowError):
            workflow.transition_action(self.conn, action_id, to_state="complete", actor="SC-01", role="supply")
        workflow.register_receipt(
            self.conn, receipt_ref="RCP-1", receipt_type="hold-ack", outcome="partial",
            received_at="2026-11-27", lot_id="LOT-006", actor="SC-01",
        )
        with self.assertRaises(WorkflowError):
            workflow.transition_action(
                self.conn, action_id, to_state="complete", actor="SC-01", role="supply", receipt_ref="RCP-1"
            )
        workflow.register_receipt(
            self.conn, receipt_ref="RCP-2", receipt_type="hold-ack", outcome="accepted",
            received_at="2026-11-28", lot_id="LOT-006", actor="SC-01",
        )
        completed = workflow.transition_action(
            self.conn, action_id, to_state="complete", actor="SC-01", role="supply", receipt_ref="RCP-2"
        )
        self.assertEqual(completed["decision_state"], "complete")

    def test_receipt_lot_must_match_action_lot(self):
        case = self.open()
        workflow.record_grade(
            self.conn, case["case_id"], grade="escalate", actor="QI-01", rationale="确认"
        )
        action_id = workflow.record_decision(
            self.conn, case["case_id"], decision="supplier-fix", actor="SC-01",
            role="supply", rationale="要求整改", lot_id="LOT-006",
        )["action_id"]
        workflow.transition_action(self.conn, action_id, to_state="confirmed", actor="SC-01", role="supply")
        workflow.register_receipt(
            self.conn, receipt_ref="RCP-3", receipt_type="supplier-response", outcome="accepted",
            received_at="2026-11-29", lot_id="LOT-011", actor="SC-01",
        )
        with self.assertRaises(WorkflowError):
            workflow.transition_action(
                self.conn, action_id, to_state="complete", actor="SC-01", role="supply", receipt_ref="RCP-3"
            )

    def test_cannot_close_with_open_actions(self):
        case = self.open()
        workflow.record_grade(
            self.conn, case["case_id"], grade="escalate", actor="QI-01", rationale="确认"
        )
        workflow.record_decision(
            self.conn, case["case_id"], decision="supplier-fix", actor="SC-01",
            role="supply", rationale="要求整改", lot_id="LOT-006",
        )
        with self.assertRaises(WorkflowError):
            workflow.close_case(
                self.conn, case["case_id"], actor="QI-01", role="quality",
                outcome="已解决", rationale="关闭",
            )

    def test_close_and_reopen_keep_history(self):
        case = self.open()
        workflow.record_grade(
            self.conn, case["case_id"], grade="investigate", actor="QI-01", rationale="确认"
        )
        closed = workflow.close_case(
            self.conn, case["case_id"], actor="QI-01", role="quality",
            outcome="未复现", rationale="后续两批复检未再发现",
        )
        self.assertEqual(closed["status"], "closed")
        reopened = workflow.reopen_case(
            self.conn, case["case_id"], actor="QI-01", role="quality", rationale="新批次再次出现同类问题"
        )
        self.assertEqual(reopened["status"], "reopened")
        timeline = workflow.case_timeline(self.conn, case["case_id"])
        types = [row["decision_type"] for row in timeline]
        self.assertIn("open", types)
        self.assertIn("grade", types)
        self.assertIn("close", types)
        self.assertIn("reopen", types)

    def test_imported_actions_are_read_only(self):
        with self.assertRaises(WorkflowError):
            workflow.transition_action(
                self.conn, "AC-001", to_state="confirmed", actor="QI-01", role="quality"
            )

    def test_unknown_action_raises(self):
        with self.assertRaises(NotFoundError):
            workflow.transition_action(
                self.conn, "ACT-9999", to_state="confirmed", actor="QI-01", role="quality"
            )

    def test_improvement_check_detects_improvement(self):
        case = self.open()
        workflow.record_grade(
            self.conn, case["case_id"], grade="escalate", actor="QI-01", rationale="确认升级"
        )
        hold = workflow.record_decision(
            self.conn, case["case_id"], decision="hold-shipment", actor="SC-01",
            role="supply", rationale="暂停未发货批次", lot_id="LOT-006",
        )["action_id"]
        workflow.transition_action(self.conn, hold, to_state="confirmed", actor="SC-01", role="supply")
        workflow.transition_action(self.conn, hold, to_state="in_progress", actor="SC-01", role="supply")
        workflow.register_receipt(
            self.conn, receipt_ref="RCP-9", receipt_type="hold-ack", outcome="accepted",
            received_at="2026-11-27", lot_id="LOT-006", actor="SC-01",
        )
        workflow.transition_action(
            self.conn, hold, to_state="complete", actor="SC-01", role="supply", receipt_ref="RCP-9"
        )
        check = workflow.record_improvement_check(
            self.conn, case["case_id"], actor="QI-01", cutoff="2027-01-31", anchor="2026-11-27",
        )
        self.assertEqual(check["verdict"], "improved")
        self.assertLess(check["rate_ratio_hi"], 1.0)
        self.assertGreater(check["post_exposure"], 0)

    def test_improvement_needs_action_anchor_or_explicit_date(self):
        case = self.open()
        with self.assertRaises(WorkflowError):
            workflow.record_improvement_check(self.conn, case["case_id"], actor="QI-01", cutoff="2027-01-31")

    def test_immature_post_window_is_inconclusive(self):
        case = self.open()
        workflow.record_grade(
            self.conn, case["case_id"], grade="escalate", actor="QI-01", rationale="确认"
        )
        check = workflow.record_improvement_check(
            self.conn, case["case_id"], actor="QI-01", cutoff="2026-12-05", anchor="2026-11-27",
        )
        self.assertEqual(check["verdict"], "immature_exposure")
        self.assertIsNone(check["rate_ratio"])

    def test_decision_log_records_actor_and_role(self):
        case = self.open()
        workflow.record_grade(
            self.conn, case["case_id"], grade="escalate", actor="QI-01", rationale="确认"
        )
        workflow.record_decision(
            self.conn, case["case_id"], decision="revise-copy", actor="PD-02",
            role="product", rationale="补充材质说明",
        )
        rows = workflow.case_timeline(self.conn, case["case_id"])
        product_rows = [row for row in rows if row["role"] == "product"]
        self.assertEqual(len(product_rows), 1)
        self.assertEqual(product_rows[0]["actor"], "PD-02")
        self.assertEqual(product_rows[0]["value"], "revise-copy")

    def test_refund_state_events_are_appended(self):
        event = workflow.record_refund_state(
            self.conn, "RET-0007", state="paid", actor="CS-01", role="quality",
            amount_units=1, note="补退款",
        )
        self.assertEqual(event["old_state"], "pending")
        self.assertEqual(event["new_state"], "paid")
        with self.assertRaises(WorkflowError):
            workflow.record_refund_state(self.conn, "RET-0007", state="paid", actor="CS-01")
        events = workflow.refund_events(self.conn, "RET-0007")
        self.assertEqual(len(events), 1)
        # Re-analysis does not touch the recorded execution fact.
        analysis.analyze(self.conn, as_of="2026-12-31", settings=self.settings)
        events = workflow.refund_events(self.conn, "RET-0007")
        self.assertEqual(len(events), 1)

    def test_sku_case_scope_contains_lots(self):
        case = self.open("sku", "SKU-006")
        self.assertEqual(set(__import__("json").loads(case["scope_json"])), {"LOT-006", "LOT-011"})
        with self.assertRaises(WorkflowError):
            workflow.record_decision(
                self.conn, case["case_id"], decision="hold-shipment", actor="SC-01",
                role="supply", rationale="批次超范围", lot_id="LOT-001",
            )


if __name__ == "__main__":
    unittest.main()
