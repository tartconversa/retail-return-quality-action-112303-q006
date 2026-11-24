import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from conftest_helpers import DATA, import_all, fresh_db

from retail_return_quality_action import analysis, db
from retail_return_quality_action.config import load_settings


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conn = fresh_db(self.tmp)
        import_all(self.conn)
        self.settings = load_settings()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def analyze(self, **kwargs):
        kwargs.setdefault("as_of", "2026-11-25")
        return analysis.analyze(self.conn, settings=self.settings, **kwargs)

    def test_no_reason_codes_are_left_unmapped(self):
        result = self.analyze()
        self.assertEqual(result.overall["data_quality"]["returns_reason_unmapped"], 0)

    def test_planted_escalation(self):
        result = self.analyze()
        risk = result.get("lot", "LOT-006")
        self.assertEqual(risk.proposed_level, "escalate")
        self.assertGreater(risk.smr, 2.0)
        self.assertLess(risk.q_value, 0.10)
        self.assertGreaterEqual(risk.evidence.confirmed_weight, 3)
        codes = {reason["code"] for reason in risk.reasons}
        self.assertIn("repeat_evidence", codes)

    def test_pending_evidence_blocks_escalation(self):
        result = self.analyze()
        risk = result.get("lot", "LOT-002")
        self.assertTrue(risk.evidence.blocking)
        self.assertNotIn(risk.proposed_level, ("escalate",))
        codes = {reason["code"] for reason in risk.reasons}
        self.assertIn("evidence_blocking", codes)

    def test_confirmed_repetition_without_significance_investigates(self):
        result = self.analyze()
        risk = result.get("lot", "LOT-011")
        self.assertEqual(risk.proposed_level, "investigate")
        self.assertGreater(risk.evidence.confirmed_weight, 0)
        codes = {reason["code"] for reason in risk.reasons}
        self.assertIn("repeat_evidence", codes)

    def test_unknown_reasons_drive_worst_case(self):
        result = self.analyze()
        risk = result.get("lot", "LOT-010")
        self.assertGreater(risk.unknown_units, 0)
        self.assertGreater(risk.worst_case_rate, risk.crude_rate)
        self.assertGreaterEqual(risk.unknown_share, 0.30)

    def test_traceability_gap_reported(self):
        result = self.analyze()
        risk = result.get("lot", "LOT-000")
        self.assertGreater(risk.traceability_gap_share, 0.9)
        codes = {reason["code"] for reason in risk.reasons}
        self.assertIn("traceability_gap", codes)

    def test_information_signal_is_not_quality_escalation(self):
        result = self.analyze()
        risk = result.get("lot", "LOT-003")
        self.assertEqual(risk.quality_units, 0)
        self.assertGreater(risk.information_units, 0)
        self.assertNotIn(risk.proposed_level, ("investigate", "escalate"))
        codes = {reason["code"] for reason in risk.reasons}
        self.assertIn("information_signal", codes)

    def test_immature_orders_excluded_from_denominator(self):
        result = self.analyze()
        self.assertGreater(result.overall["immature_units"], 0)
        # Sales after the cut-off are excluded entirely.
        self.assertGreater(result.overall["future_units_excluded"], 0)
        mature = result.overall["exposure_units"]
        self.assertLess(mature, db.scalar(self.conn, "SELECT SUM(units) FROM orders"))

    def test_policy_maturity_differs_by_policy(self):
        result = self.analyze()
        maturity = result.overall["maturity_days"]
        self.assertEqual(maturity["POL-0"], 14)
        self.assertEqual(maturity["POL-3"], 37)

    def test_stratification_is_explicit(self):
        result = self.analyze()
        self.assertGreaterEqual(len(result.strata), 8)
        for row in result.strata:
            self.assertEqual(row["stratum"], f"{row['channel']}/{row['policy_version']}")

    def test_sku_aggregates_lots(self):
        result = self.analyze()
        risk = result.get("sku", "SKU-006")
        self.assertEqual(set(risk.scope["lots"]), {"LOT-006", "LOT-011"})
        self.assertEqual(risk.proposed_level, "investigate")

    def test_revision_after_cutoff_does_not_change_earlier_snapshot(self):
        early = self.analyze(as_of="2026-11-25")
        before = early.get("lot", "LOT-002").quality_units
        later = self.analyze(as_of="2026-12-31")
        after = later.get("lot", "LOT-002").quality_units
        # RR-006/RR-007 are effective in December and add quality returns.
        self.assertGreater(after, before)

    def test_refunds_untouched_by_reanalysis(self):
        before = {
            row["return_id"]: row["refund_state"]
            for row in self.conn.execute("SELECT return_id, refund_state FROM returns")
        }
        self.analyze()
        self.analyze(as_of="2026-12-31")
        after = {
            row["return_id"]: row["refund_state"]
            for row in self.conn.execute("SELECT return_id, refund_state FROM returns")
        }
        self.assertEqual(before, after)

    def test_snapshot_persisted_with_digests(self):
        result = self.analyze(note="baseline")
        row = db.rows(self.conn, "SELECT * FROM risk_snapshot WHERE snapshot_id=?", (result.snapshot_id,))[0]
        self.assertEqual(row["as_of"], "2026-11-25")
        self.assertEqual(row["note"] if "note" in row.keys() else row["notes"], "baseline")
        self.assertEqual(len(row["config_digest"]), 64)
        self.assertEqual(len(row["data_digest"]), 64)
        count = db.scalar(
            self.conn, "SELECT COUNT(*) FROM risk_result WHERE snapshot_id=?", (result.snapshot_id,)
        )
        self.assertGreater(count, 0)

    def test_deterministic_scores(self):
        first = self.analyze()
        second = self.analyze()
        for level in ("lot", "sku"):
            a = {risk.entity_id: risk.as_row() for risk in first.entities[level]}
            b = {risk.entity_id: risk.as_row() for risk in second.entities[level]}
            self.assertEqual(a.keys(), b.keys())
            for key in a:
                self.assertAlmostEqual(a[key]["smr"] or 0.0, b[key]["smr"] or 0.0, places=9)
                self.assertEqual(a[key]["proposed_level"], b[key]["proposed_level"])

    def test_immature_only_entity_is_watch_not_clean(self):
        # Insert a brand-new lot sold after the cut-off with no mature exposure.
        self.conn.execute(
            "INSERT INTO orders(order_id, sku, lot_id, channel, sold_at, units, policy_version,"
            " record_hash, first_batch, last_batch) VALUES"
            " ('ORD-NEW', 'SKU-009', 'LOT-NEW', 'web', '2026-11-20', 5, 'POL-3', 'h', 0, 0)"
        )
        self.conn.commit()
        result = self.analyze()
        risk = result.get("lot", "LOT-NEW")
        self.assertEqual(risk.exposure_units, 0)
        self.assertGreater(risk.immature_units, 0)
        self.assertEqual(risk.proposed_level, "watch")
        self.assertIn("exposure_immature", {r["code"] for r in risk.reasons})

    def test_heterogeneity_present(self):
        result = self.analyze()
        self.assertIsNotNone(result.overall["stratum_heterogeneity_p"])
        self.assertGreaterEqual(result.overall["stratum_heterogeneity_p"], 0.0)
        self.assertLessEqual(result.overall["stratum_heterogeneity_p"], 1.0)


if __name__ == "__main__":
    unittest.main()
