"""Unit tests for collector scheduling helpers (no device required).

Run:  ./.venv/bin/python -m unittest -q test_collector
"""

import unittest

import collector


class TestShouldUpload(unittest.TestCase):
    def test_due_by_interval(self):
        # last upload 901s ago, 900s interval -> upload even without a change
        self.assertTrue(collector.should_upload(1000, 99, 900, False, True))

    def test_not_due_and_unchanged(self):
        self.assertFalse(collector.should_upload(1000, 900, 900, False, True))

    def test_change_triggers_upload_when_enabled(self):
        self.assertTrue(collector.should_upload(1000, 999, 900, True, True))

    def test_change_ignored_when_disabled(self):
        self.assertFalse(collector.should_upload(1000, 999, 900, True, False))

    def test_failed_upload_retries_after_interval(self):
        # callers advance last_upload_ts on failure, so the next try is one
        # interval later rather than every poll
        self.assertFalse(collector.should_upload(1001, 1000, 900, False, True))
        self.assertTrue(collector.should_upload(1900, 1000, 900, False, True))


class TestStatusKey(unittest.TestCase):
    def test_ignores_timestamp(self):
        a = {"ts": 1, "detection": None, "lat": 1.0, "lon": 2.0}
        b = {"ts": 2, "detection": None, "lat": 1.0, "lon": 2.0}
        self.assertEqual(collector.status_key(a), collector.status_key(b))

    def test_detection_change_is_a_change(self):
        a = {"detection": None, "lat": 1.0, "lon": 2.0}
        b = {"detection": "imsi_catcher", "lat": 1.0, "lon": 2.0}
        self.assertNotEqual(collector.status_key(a), collector.status_key(b))

    def test_none_record(self):
        self.assertIsNone(collector.status_key(None))


class TestDeviceIdHash(unittest.TestCase):
    def test_stable_and_short(self):
        h = collector._device_id_hash("some-uuid")
        self.assertEqual(h, collector._device_id_hash("some-uuid"))
        self.assertEqual(len(h), 16)
        self.assertNotIn("some-uuid", h)


if __name__ == "__main__":
    unittest.main()
