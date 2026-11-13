import time
import unittest

from fnm_support import load_server

srv = load_server()


class PricePeriodsTest(unittest.TestCase):
    def test_sanitize_keeps_valid_and_drops_invalid(self):
        out = srv.sanitize_price_periods([
            {"start": "8:00", "end": "11:00", "rate": "1.05"},
            {"start": "", "end": "x", "rate": 1},
            {"start": "22:00", "end": "22:00", "rate": 0.3},
            {"start": "23:00", "end": "24:00", "rate": -1},
            {"start": "01:00", "end": "02:00", "rate": "abc"},
            "bad",
        ])
        self.assertEqual(out, [
            {"start": "08:00", "end": "11:00", "rate": 1.05},
            {"start": "23:00", "end": "24:00", "rate": 0.0},
        ])

    def test_sanitize_limits_to_12_periods(self):
        out = srv.sanitize_price_periods([{"start": "01:00", "end": "02:00", "rate": 0.5}] * 20)
        self.assertEqual(len(out), 12)

    def test_period_rate_plain_and_overnight(self):
        plain = [{"start": "08:00", "end": "11:00", "rate": 1.0}]
        self.assertEqual(srv.period_rate(8 * 60, plain, 0.5), 1.0)
        self.assertEqual(srv.period_rate(11 * 60, plain, 0.5), 0.5)
        self.assertEqual(srv.period_rate(8 * 60 - 1, plain, 0.5), 0.5)

        night = [{"start": "23:00", "end": "07:00", "rate": 0.3}]
        self.assertEqual(srv.period_rate(23 * 60 + 30, night, 0.6), 0.3)
        self.assertEqual(srv.period_rate(6 * 60 + 59, night, 0.6), 0.3)
        self.assertEqual(srv.period_rate(7 * 60, night, 0.6), 0.6)
        self.assertEqual(srv.period_rate(22 * 60 + 59, night, 0.6), 0.6)

    def test_price_rate_at_uses_local_time(self):
        night = [{"start": "23:00", "end": "07:00", "rate": 0.3}]
        ts = time.mktime((2026, 1, 1, 2, 0, 0, 0, 0, -1))
        self.assertEqual(srv.price_rate_at(ts, night, 0.6), 0.3)
        ts = time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1))
        self.assertEqual(srv.price_rate_at(ts, night, 0.6), 0.6)

    def test_day_cost_with_hourly_buckets(self):
        night = [{"start": "22:00", "end": "08:00", "rate": 0.3}]
        hourly = {h: 1.0 for h in range(24)}
        # 22 点至次日 7 点共 10 小时按 0.3，其余 14 小时按 0.6
        self.assertAlmostEqual(srv.day_cost(24.0, hourly, night, 0.6), 11.4, places=4)

    def test_day_cost_falls_back_to_base_rate(self):
        night = [{"start": "22:00", "end": "08:00", "rate": 0.3}]
        self.assertAlmostEqual(srv.day_cost(10.0, {}, night, 0.6), 6.0, places=4)
        self.assertIsNone(srv.day_cost(None, {}, night, 0.6))


if __name__ == "__main__":
    unittest.main()
