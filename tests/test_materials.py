import csv
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {'orders.csv': (['order_id', 'sku', 'lot_id', 'channel', 'sold_at', 'units', 'policy_version'], 120), 'returns.csv': (['return_id', 'order_id', 'returned_units', 'reason_code', 'received_at', 'refund_state', 'review_state'], 65), 'reason_map.csv': (['reason_code', 'category', 'valid_from', 'valid_to', 'mapping_version'], 16), 'inspections.csv': (['inspection_id', 'lot_id', 'sample_size', 'finding_code', 'severity', 'review_state', 'inspected_at'], 45), 'actions.csv': (['action_id', 'lot_id', 'owner_role', 'action_type', 'decision_state', 'effective_at', 'receipt_ref'], 39)}

class MaterialTests(unittest.TestCase):
    def test_declared_csv_contracts(self):
        for filename, (columns, count) in EXPECTED.items():
            with self.subTest(filename=filename):
                with (ROOT / "data" / filename).open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                self.assertEqual(reader.fieldnames, columns)
                self.assertEqual(len(rows), count)
                self.assertTrue(all(None not in row for row in rows))

if __name__ == "__main__":
    unittest.main()
