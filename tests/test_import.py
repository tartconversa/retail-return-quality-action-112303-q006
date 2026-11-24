import csv
import shutil
import tempfile
import unittest
from pathlib import Path

from conftest_helpers import DATA, ROOT, import_all, fresh_db

from retail_return_quality_action import db, importer
from retail_return_quality_action.errors import DataError


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.conn = fresh_db(self.tmp)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_import_and_row_counts(self):
        results = import_all(self.conn)
        by_dataset = {r.dataset: r for r in results}
        self.assertEqual(by_dataset["orders"].inserted, 120)
        self.assertEqual(by_dataset["returns"].inserted, 65)
        self.assertEqual(by_dataset["inspections"].inserted, 45)
        self.assertEqual(by_dataset["actions"].inserted, 39)
        self.assertEqual(by_dataset["returns_followup"].inserted, 5)
        self.assertEqual(db.scalar(self.conn, "SELECT COUNT(*) FROM orders"), 151)
        self.assertEqual(db.scalar(self.conn, "SELECT COUNT(*) FROM returns"), 70)

    def test_reimport_is_idempotent(self):
        import_all(self.conn)
        second = importer.import_directory(self.conn, DATA)
        for result in second:
            self.assertEqual(result.inserted, 0, result.dataset)
            self.assertEqual(result.updated, 0, result.dataset)
            self.assertEqual(result.rejected, 0, result.dataset)
            self.assertEqual(result.unchanged, result.total, result.dataset)
        self.assertEqual(db.scalar(self.conn, "SELECT COUNT(*) FROM orders"), 151)

    def test_convenience_issues_are_recorded_without_rejection(self):
        import_all(self.conn)
        codes = {
            row[0]
            for row in self.conn.execute("SELECT DISTINCT code FROM import_issue")
        }
        self.assertIn("returned_units_exceed_order", codes)
        over = db.scalar(
            self.conn,
            "SELECT COUNT(*) FROM returns r JOIN orders o USING(order_id)"
            " WHERE r.returned_units > o.units",
        )
        self.assertEqual(over, 2)

    def test_invalid_row_is_rejected_and_reported(self):
        folder = self.tmp / "data"
        folder.mkdir()
        for name in ("orders.csv", "returns.csv"):
            shutil.copy(DATA / name, folder / name)
        with (folder / "returns.csv").open("r", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        rows[0]["returned_units"] = "-3"
        with (folder / "returns.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        importer.import_dataset(self.conn, folder / "orders.csv")
        result = importer.import_dataset(self.conn, folder / "returns.csv")
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.rejected, 1)
        self.assertEqual(result.inserted, 64)
        issue = self.conn.execute("SELECT * FROM import_issue WHERE code='invalid_row'").fetchone()
        self.assertIsNotNone(issue)
        self.assertIn("returned_units", issue["message"])

    def test_missing_column_rejected(self):
        folder = self.tmp / "data"
        folder.mkdir()
        with (folder / "orders.csv").open("w", encoding="utf-8", newline="") as stream:
            stream.write("order_id,sku,channel,sold_at,units,policy_version\nORD-X,SKU-1,web,2026-10-01,1,POL-1\n")
        with self.assertRaises(DataError):
            importer.import_dataset(self.conn, folder / "orders.csv")

    def test_immutable_fact_change_is_rejected(self):
        importer.import_dataset(self.conn, DATA / "orders.csv")
        folder = self.tmp / "data"
        folder.mkdir()
        with (DATA / "orders.csv").open("r", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        rows[0]["units"] = str(int(rows[0]["units"]) + 5)
        with (folder / "orders.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        result = importer.import_dataset(self.conn, folder / "orders.csv")
        self.assertEqual(result.rejected, 1)
        self.assertIn("不可变更", result.issues[0]["message"])
        units = db.scalar(
            self.conn, "SELECT units FROM orders WHERE order_id=?", (rows[0]["order_id"],)
        )
        self.assertNotEqual(units, int(rows[0]["units"]))

    def test_mutable_field_update_is_counted(self):
        importer.import_dataset(self.conn, DATA / "lots.csv")
        folder = self.tmp / "data"
        folder.mkdir()
        with (DATA / "lots.csv").open("r", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        target = next(row for row in rows if row["lot_id"] == "LOT-001")
        target["unshipped_units"] = "99"
        with (folder / "lots.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        result = importer.import_dataset(self.conn, folder / "lots.csv")
        self.assertEqual(result.updated, 1)
        self.assertEqual(
            db.scalar(self.conn, "SELECT unshipped_units FROM lots WHERE lot_id='LOT-001'"), 99
        )

    def test_encoding_fallback(self):
        folder = self.tmp / "data"
        folder.mkdir()
        text = (DATA / "orders.csv").read_text(encoding="utf-8-sig")
        (folder / "orders.csv").write_bytes(text.encode("gb18030"))
        result = importer.import_dataset(self.conn, folder / "orders.csv")
        self.assertEqual(result.inserted, 120)


if __name__ == "__main__":
    unittest.main()
