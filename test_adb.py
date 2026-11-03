"""Tests for the ADB reader's privacy helpers (no device or network needed).

Run:  ./.venv/bin/python -m unittest -q test_adb
"""

import hashlib
import os
import tempfile
import time
import unittest

import adb_reader


class TestDeviceId(unittest.TestCase):
    def test_stable_persisted_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RAYMAP_STATE_DIR"] = tmp
            try:
                first = adb_reader.device_hash_for_install()
                second = adb_reader.device_hash_for_install()
                self.assertEqual(first, second)
                self.assertEqual(len(first), 16)
                path = os.path.join(tmp, "device_id")
                self.assertTrue(os.path.exists(path))
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                # The stored value is a random uuid, not a hardware serial.
                self.assertNotIn(first, open(path).read())
            finally:
                os.environ.pop("RAYMAP_STATE_DIR", None)

    def test_hash_is_not_the_serial(self):
        serial = "764fa857"
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RAYMAP_STATE_DIR"] = tmp
            try:
                self.assertNotEqual(
                    adb_reader.device_hash_for_install(),
                    hashlib.sha256(serial.encode()).hexdigest()[:16],
                )
            finally:
                os.environ.pop("RAYMAP_STATE_DIR", None)


class TestClampTs(unittest.TestCase):
    def test_keeps_plausible_device_time(self):
        self.assertEqual(adb_reader.clamp_ts(1_000_000, 1_000_100), 1_000_000)

    def test_replaces_implausible_device_time(self):
        self.assertEqual(adb_reader.clamp_ts(1_000_000, 9_000_000), 9_000_000)
        self.assertEqual(adb_reader.clamp_ts("garbage", 5_000), 5_000)
        self.assertEqual(adb_reader.clamp_ts(None, 5_000), 5_000)


class TestRedact(unittest.TestCase):
    def test_coordinates_removed_for_logs(self):
        out = adb_reader.redact({"lat": 33.75, "lon": -84.39, "ts": 1})
        self.assertEqual(out["lat"], "<redacted>")
        self.assertEqual(out["lon"], "<redacted>")
        self.assertEqual(out["ts"], 1)

    def test_missing_coords_stay_none(self):
        self.assertIsNone(adb_reader.redact({"lat": None, "lon": None})["lat"])


class TestManifestVersion(unittest.TestCase):
    def test_takes_the_newest_entry(self):
        text = (
            '[[entries]]\nname = "1"\nrayhunter_version = "0.11.0"\n\n'
            '[[entries]]\nname = "2"\nrayhunter_version = "0.12.0"\n'
        )
        self.assertEqual(adb_reader.manifest_version(text), "0.12.0")

    def test_unreadable_manifest_is_none(self):
        self.assertIsNone(adb_reader.manifest_version(""))


class TestPow(unittest.TestCase):
    BODY = b'{"device":"x","records":[{"ts":1}]}'

    def test_solved_header_satisfies_the_server_rule(self):
        header = adb_reader.solve_pow(self.BODY, "devhash", 1)
        bits_s, nonce = header.split(":")
        self.assertEqual(int(bits_s), adb_reader.POW_BASE_BITS)
        digest = hashlib.sha256(self.BODY).hexdigest()
        bucket = int(time.time()) // adb_reader.POW_BUCKET_S
        ok = any(
            adb_reader._leading_zero_bits(
                hashlib.sha256(f"devhash|{b}|{digest}|{nonce}".encode()).digest()
            ) >= int(bits_s)
            for b in (bucket, bucket - 1)
        )
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
