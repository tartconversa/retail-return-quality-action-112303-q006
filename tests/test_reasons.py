import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from conftest_helpers import import_all, fresh_db

from retail_return_quality_action import reasons
from retail_return_quality_action.errors import DataError


class ReasonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conn = fresh_db(self.tmp)
        import_all(self.conn)
        self.resolver = reasons.ReasonResolver(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_legacy_alias_maps_to_current_category(self):
        result = self.resolver.classify("damaged", date(2026, 10, 20))
        self.assertEqual(result.category, "quality")
        self.assertTrue(result.via_alias)

    def test_new_catalogue_code(self):
        result = self.resolver.classify("R-21", date(2026, 11, 20))
        self.assertEqual(result.category, "quality")
        self.assertFalse(result.via_alias)

    def test_code_switch_is_bridged(self):
        # The same category is reached before and after the catalogue switch.
        before = self.resolver.classify("damaged", date(2026, 11, 10))
        after = self.resolver.classify("R-21", date(2026, 11, 20))
        self.assertEqual(before.category, after.category)

    def test_revision_code_classified_by_revision_date(self):
        # RET-0006 is revised to R-21 on 2026-12-02.  R-21 only exists from
        # 2026-11-15, and the return itself arrived earlier; the revision date
        # must be used so the revised code is not misread as unknown.
        code, _, revision_at = self.resolver.effective_code("RET-0006", "damaged", date(2026, 12, 10))
        result = self.resolver.classify(code, revision_at)
        self.assertEqual(result.category, "quality")

    def test_unknown_code_is_unknown_not_zero(self):
        result = self.resolver.classify("NOT-A-CODE", date(2026, 11, 20))
        self.assertEqual(result.category, "unknown")
        self.assertIn("未知", result.note)

    def test_effective_code_respects_as_of(self):
        code, review, _ = self.resolver.effective_code("RET-0006", "damaged", date(2026, 11, 25))
        self.assertEqual(code, "damaged")
        self.assertIsNone(review)
        code, review, effective_at = self.resolver.effective_code("RET-0006", "damaged", date(2026, 12, 10))
        self.assertEqual(code, "R-21")
        self.assertEqual(review, "RR-006")
        self.assertEqual(effective_at, date(2026, 12, 2))

    def test_revisions_are_append_only_and_ordered(self):
        rows = self.resolver.revisions("RET-0006")
        self.assertEqual([row["seq"] for row in rows], [1])
        self.assertEqual(rows[0]["old_reason_code"], "damaged")
        self.assertEqual(rows[0]["new_reason_code"], "R-21")

    def test_apply_reviews_is_idempotent(self):
        before = self.conn.execute("SELECT COUNT(*) FROM return_revision").fetchone()[0]
        stats = reasons.apply_reviews(self.conn)
        after = self.conn.execute("SELECT COUNT(*) FROM return_revision").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(stats["applied"], 0)

    def test_inspection_reviews_are_applied(self):
        applied = reasons.apply_inspection_reviews(self.conn)
        self.assertEqual(applied["applied"], 0)
        row = self.conn.execute(
            "SELECT review_state FROM inspection_review WHERE inspection_id='IN-004'"
            " ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["review_state"], "confirmed")

    def test_validate_reason_code(self):
        reasons.validate_reason_code(self.conn, "R-21")
        reasons.validate_reason_code(self.conn, "damaged")
        with self.assertRaises(DataError):
            reasons.validate_reason_code(self.conn, "NOPE")


if __name__ == "__main__":
    unittest.main()
