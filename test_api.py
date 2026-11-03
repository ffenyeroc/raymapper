"""Unit tests for the pure helpers in app_api (no database required).

Run:  ./.venv/bin/python -m unittest -q test_api
"""

import unittest

import app_api


class TestClampDays(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(app_api.clamp_days(0), 1)
        self.assertEqual(app_api.clamp_days(-5), 1)
        self.assertEqual(app_api.clamp_days(7), 7)
        self.assertEqual(app_api.clamp_days(90), 90)
        self.assertEqual(app_api.clamp_days(10_000), app_api.MAX_DAYS)

    def test_garbage_falls_back_to_default(self):
        self.assertEqual(app_api.clamp_days("nonsense"), 30)
        self.assertEqual(app_api.clamp_days(None), 30)


class TestClassify(unittest.TestCase):
    def test_corroborated_detection(self):
        self.assertEqual(app_api.classify(5, 2), ("detected_corroborated", "multi"))

    def test_single_device_detection_is_marked(self):
        # The whole point: 1 device is reported, but never as corroborated.
        self.assertEqual(app_api.classify(1, 1), ("detected_single", "single"))
        self.assertEqual(app_api.classify(3, 1), ("detected_single", "single"))

    def test_clear_cells(self):
        self.assertEqual(app_api.classify(2, 0), ("clear_multi", "multi"))
        self.assertEqual(app_api.classify(1, 0), ("clear_single", "single"))


class TestValidateRecord(unittest.TestCase):
    def ok(self, r):
        return app_api.validate_record(r)[0]

    def test_accepts_a_minimal_record(self):
        self.assertTrue(self.ok({"lat": 33.75, "lon": -84.39, "ts": 1700000000}))

    def test_rejects_missing_or_bad_coordinates(self):
        self.assertFalse(self.ok({"ts": 1}))
        self.assertFalse(self.ok({"lat": "x", "lon": 1, "ts": 1}))
        self.assertFalse(self.ok({"lat": 91, "lon": 0, "ts": 1}))
        self.assertFalse(self.ok({"lat": 0, "lon": 181, "ts": 1}))
        self.assertFalse(self.ok({"lat": None, "lon": None, "ts": 1}))
        self.assertFalse(self.ok({"lat": 1, "lon": 1}))  # missing ts

    def test_rejects_bad_optional_fields(self):
        self.assertFalse(self.ok({"lat": 1, "lon": 1, "ts": "soon"}))
        self.assertFalse(self.ok({"lat": 1, "lon": 1, "rssi_dbm": "strong"}))
        self.assertFalse(self.ok({"lat": 1, "lon": 1, "detection": 42}))
        self.assertFalse(self.ok(["not", "a", "dict"]))

    def test_cell_precision_is_bounded(self):
        self.assertGreaterEqual(app_api.CELL_PRECISION, 4)
        self.assertLessEqual(app_api.CELL_PRECISION, 8)


if __name__ == "__main__":
    unittest.main()
