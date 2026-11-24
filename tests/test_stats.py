import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from retail_return_quality_action import stats


class StatsTests(unittest.TestCase):
    def test_poisson_sf_known_values(self):
        self.assertAlmostEqual(stats.poisson_sf(4, 2.267), 0.193978046856, places=10)
        self.assertAlmostEqual(stats.poisson_sf(0, 2.267), 1.0, places=12)
        self.assertAlmostEqual(stats.poisson_sf(3, 0.5), 0.014387677967, places=10)

    def test_chi2_quantile_roundtrip(self):
        for p in (0.025, 0.5, 0.975):
            x = stats.chi2_ppf(p, 9)
            self.assertAlmostEqual(stats.chi2_sf(x, 9), p, places=8)
        self.assertAlmostEqual(stats.chi2_ppf(0.025, 10), 20.48317735, places=4)
        self.assertAlmostEqual(stats.chi2_ppf(0.975, 10), 3.24697278, places=5)
        self.assertAlmostEqual(stats.chi2_ppf(0.025, 12), 23.336664, places=4)

    def test_poisson_exact_interval(self):
        rate, lo, hi = stats.poisson_rate_ci(0, 10)
        self.assertEqual(rate, 0.0)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.36889, places=4)
        rate, lo, hi = stats.poisson_rate_ci(5, 100)
        self.assertAlmostEqual(rate, 0.05, places=12)
        self.assertAlmostEqual(lo, 0.016235, places=5)
        self.assertAlmostEqual(hi, 0.116683, places=5)

    def test_bh(self):
        q = stats.bh_qvalues([0.01, 0.04, 0.03, 0.2])
        self.assertAlmostEqual(q[0], 0.04, places=10)
        self.assertAlmostEqual(q[1], 0.0533333333, places=8)
        self.assertAlmostEqual(q[2], 0.0533333333, places=8)
        self.assertAlmostEqual(q[3], 0.2, places=10)

    def test_decompose_rate_change_identity(self):
        early = {"web/P1": (100, 5), "store/P1": (100, 10)}
        late = {"web/P1": (200, 10), "store/P1": (50, 5)}
        d = stats.decompose_rate_change(early, late)
        self.assertAlmostEqual(d["delta"], d["mix_effect"] + d["rate_effect"] + d["residual"], places=12)

    def test_decompose_entity_mix_identity(self):
        entity = {"web/P1": (50, 10), "store/P1": (10, 0)}
        background = {"web/P1": (100, 10), "store/P1": (100, 10)}
        d = stats.decompose_entity_mix(entity, background)
        self.assertAlmostEqual(d["difference"], d["mix_effect"] + d["rate_effect"] + d["residual"], places=12)


if __name__ == "__main__":
    unittest.main()


class RateRatioTests(unittest.TestCase):
    def test_ratio_and_interval(self):
        ratio, lo, hi, p = stats.rate_ratio_ci(4, 100, 5, 200)
        self.assertAlmostEqual(ratio, 1.6, places=10)
        self.assertLess(lo, ratio)
        self.assertGreater(hi, ratio)
        self.assertTrue(0.0 <= p <= 1.0)

    def test_zero_events_after_intervention(self):
        ratio, lo, hi, p = stats.rate_ratio_ci(0, 28, 11, 40)
        self.assertEqual(ratio, 0.0)
        self.assertEqual(lo, 0.0)
        self.assertLess(hi, 1.0)
        self.assertLess(p, 0.01)

    def test_binomial_exact_interval(self):
        lo, hi = stats.binomial_exact_ci(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.3084971078, places=9)
        lo, hi = stats.binomial_exact_ci(5, 100)
        self.assertAlmostEqual(lo, 0.0164318792, places=9)
        self.assertAlmostEqual(hi, 0.1128349111, places=9)
        self.assertEqual(stats.binomial_exact_ci(10, 10), (stats.beta_ppf(0.025, 10, 1), 1.0))

    def test_binomial_test(self):
        self.assertAlmostEqual(stats.binomial_test(5, 10, 0.5), 1.0, places=10)
        self.assertLess(stats.binomial_test(0, 10, 0.5), 0.01)

    def test_z_from_alpha(self):
        self.assertAlmostEqual(stats._z_from_alpha(0.05), 1.959963984540054, places=9)
