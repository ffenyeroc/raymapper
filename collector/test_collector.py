"""Unit tests for collector scheduling helpers (no device required).

Run:  ./.venv/bin/python -m unittest -q test_collector
"""

import unittest

import collector
import pow as power


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


class TestProofOfWork(unittest.TestCase):
    BODY = b'{"device":"abc","records":[{"ts":1}]}'

    def _verify(self, body, device, header, now, n):
        import hashlib
        bits, nonce = header.split(":")
        digest = hashlib.sha256(body).hexdigest()
        bucket = int(now) // power.POW_BUCKET_S
        return any(
            power.leading_zero_bits(
                hashlib.sha256(f"{device}|{b}|{digest}|{nonce}".encode()).digest()
            ) >= int(bits)
            for b in (bucket, bucket - 1)
        )

    def test_header_satisfies_the_rule(self):
        now = 1_700_000_000.0
        header = power.solve_header(self.BODY, "dev1", 1, now=now)
        self.assertTrue(self._verify(self.BODY, "dev1", header, now, 1))

    def test_harder_for_bigger_batches(self):
        self.assertGreater(
            power.target_bits(500), power.target_bits(1)
        )

    def test_bound_to_payload(self):
        now = 1_700_000_000.0
        header = power.solve_header(self.BODY, "dev1", 1, now=now)
        self.assertFalse(self._verify(self.BODY + b" ", "dev1", header, now, 1))
