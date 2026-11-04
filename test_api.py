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

    def test_resolution_band_is_enforced(self):
        # r6 is the smallest cell served (~7.4 km across); r3 the coarsest.
        self.assertEqual(app_api.clamp_res(2), 3)
        self.assertEqual(app_api.clamp_res(7), 6)
        self.assertEqual(app_api.clamp_res(9), 6)
        self.assertEqual(app_api.clamp_res(15), 6)
        self.assertEqual(app_api.clamp_res(6), 6)
        self.assertEqual(app_api.clamp_res("junk"), app_api.DEFAULT_RES)


class TestH3Columns(unittest.TestCase):
    def test_parent_chain_is_consistent(self):
        import h3 as h3lib
        cells = app_api.h3_columns(33.749, -84.388)
        self.assertEqual(set(cells), {f"h3_r{r}" for r in app_api.HEX_RESOLUTIONS})
        self.assertEqual({len(v) for v in cells.values()}, {15})
        for r in app_api.HEX_RESOLUTIONS[:-1]:
            self.assertEqual(
                h3lib.cell_to_parent(cells[f"h3_r{r + 1}"], r), cells[f"h3_r{r}"]
            )


if __name__ == "__main__":
    unittest.main()


# --- Proof of work / ingest limits (added with local-hosting hardening) ------
import gzip  # noqa: E402
import hashlib  # noqa: E402
import time  # noqa: E402
import zlib  # noqa: E402


def _solve(body: bytes, device: str, n: int, now: float) -> str:
    """Client-side solver, mirrored here so the server rule is exercised."""
    bits = app_api.target_bits(n)
    digest = hashlib.sha256(body).hexdigest()
    bucket = int(now) // app_api.POW_BUCKET_S
    prefix = f"{device}|{bucket}|{digest}|"
    nonce = 0
    while True:
        if app_api.leading_zero_bits(hashlib.sha256((prefix + str(nonce)).encode()).digest()) >= bits:
            return f"{bits}:{nonce}"
        nonce += 1


class TestPowDifficulty(unittest.TestCase):
    def test_scales_with_record_count(self):
        self.assertEqual(app_api.target_bits(1), 12)
        self.assertEqual(app_api.target_bits(2), 13)
        self.assertEqual(app_api.target_bits(12), 16)
        self.assertEqual(app_api.target_bits(500), 21)

    def test_monotonic(self):
        bits = [app_api.target_bits(n) for n in range(1, 600)]
        self.assertEqual(bits, sorted(bits))

    def test_leading_zero_bits(self):
        self.assertEqual(app_api.leading_zero_bits(b"\x00\x00"), 16)
        self.assertEqual(app_api.leading_zero_bits(b"\x00\x01"), 15)
        self.assertEqual(app_api.leading_zero_bits(b"\x80"), 0)


class TestPowVerify(unittest.TestCase):
    BODY = b'{"records":[{"ts":1},{"ts":2}]}'

    def setUp(self):
        self._saved = app_api.POW_REQUIRED
        app_api.POW_REQUIRED = True

    def tearDown(self):
        app_api.POW_REQUIRED = self._saved

    def test_accepts_valid_header(self):
        now = 1_700_000_000.0
        header = _solve(self.BODY, "dev1", 2, now)
        ok, reason, required = app_api.pow_verify(self.BODY, "dev1", header, 2, now)
        self.assertTrue(ok, reason)
        self.assertEqual(required, 13)

    def test_tolerates_previous_bucket(self):
        # Solve exactly on a bucket boundary, then verify one bucket later:
        # the server accepts the current and previous bucket (±5 min skew).
        t0 = 1_700_000_400.0  # divisible by POW_BUCKET_S
        header = _solve(self.BODY, "dev1", 2, t0)
        ok, reason, _ = app_api.pow_verify(self.BODY, "dev1", header, 2, t0 + 300)
        self.assertTrue(ok, reason)

    def test_rejects_missing_header(self):
        ok, reason, required = app_api.pow_verify(self.BODY, "dev1", None, 2, 1_700_000_000.0)
        self.assertFalse(ok)
        self.assertIn("missing", reason)
        self.assertEqual(required, 13)

    def test_rejects_insufficient_bits(self):
        ok, reason, required = app_api.pow_verify(self.BODY, "dev1", "12:0", 2, 1_700_000_000.0)
        self.assertFalse(ok)
        self.assertIn("insufficient", reason)

    def test_rejects_wrong_device(self):
        now = 1_700_000_000.0
        header = _solve(self.BODY, "dev1", 2, now)
        ok, _, _ = app_api.pow_verify(self.BODY, "other-device", header, 2, now)
        self.assertFalse(ok)

    def test_rejects_tampered_body(self):
        now = 1_700_000_000.0
        header = _solve(self.BODY, "dev1", 2, now)
        ok, _, _ = app_api.pow_verify(self.BODY + b" ", "dev1", header, 2, now)
        self.assertFalse(ok)

    def test_disabled_flag_skips_check(self):
        app_api.POW_REQUIRED = False
        ok, _, _ = app_api.pow_verify(self.BODY, "dev1", None, 2, 1_700_000_000.0)
        self.assertTrue(ok)


class TestDecompressCap(unittest.TestCase):
    def test_inflates(self):
        self.assertEqual(app_api.decompress_capped(gzip.compress(b"hello")), b"hello")

    def test_rejects_gzip_bomb(self):
        bomb = gzip.compress(b"\x00" * 1_000_000)
        with self.assertRaises(ValueError):
            app_api.decompress_capped(bomb, limit=1024)

    def test_rejects_non_gzip(self):
        with self.assertRaises((zlib.error, ValueError, OSError)):
            app_api.decompress_capped(b"not gzip")


class TestLimits(unittest.TestCase):
    def test_limits_are_sane(self):
        self.assertLessEqual(app_api.MAX_DECOMPRESSED_BYTES, 16 * 1024 * 1024)
        self.assertLessEqual(app_api.MAX_BODY_BYTES, app_api.MAX_DECOMPRESSED_BYTES)
        self.assertGreaterEqual(app_api.MAX_RECORDS, 10)
        self.assertGreater(app_api.QUOTA_RECORDS_HOUR, 60)


class TestPowBindsRawBody(unittest.TestCase):
    """The client hashes the bytes it sends (possibly gzipped), so the server
    must verify against the raw request body, not the decompressed one."""

    def setUp(self):
        self._saved = app_api.POW_REQUIRED
        app_api.POW_REQUIRED = True

    def tearDown(self):
        app_api.POW_REQUIRED = self._saved

    def test_raw_body_verifies_and_decompressed_does_not(self):
        plain = b'{"records":[{"ts":1}]}'
        packed = gzip.compress(plain)
        now = 1_700_000_000.0
        header = _solve(packed, "dev1", 1, now)
        ok_raw, reason, _ = app_api.pow_verify(packed, "dev1", header, 1, now)
        ok_plain, _, _ = app_api.pow_verify(plain, "dev1", header, 1, now)
        self.assertTrue(ok_raw, reason)
        self.assertFalse(ok_plain)


class TestBbox(unittest.TestCase):
    def test_parses_and_normalizes(self):
        self.assertEqual(app_api.parse_bbox("-85,30,-80,35"), (-85.0, 30.0, -80.0, 35.0))
        self.assertEqual(app_api.parse_bbox("-80,35,-85,30"), (-85.0, 30.0, -80.0, 35.0))

    def test_rejects_garbage(self):
        self.assertIsNone(app_api.parse_bbox(None))
        self.assertIsNone(app_api.parse_bbox(""))
        self.assertIsNone(app_api.parse_bbox("a,b,c"))


# --- history, moderation guard (added with the /detections slice) -----------
import h3 as h3lib  # noqa: E402
from datetime import date  # noqa: E402


class TestParseMonth(unittest.TestCase):
    def test_accepts_month_and_day(self):
        self.assertEqual(app_api.parse_month("2026-11"), date(2026, 11, 1))
        self.assertEqual(app_api.parse_month("2026-11-23"), date(2026, 11, 1))

    def test_rejects_junk(self):
        for value in (None, "", "2026", "November", "2026-13"):
            self.assertIsNone(app_api.parse_month(value), value)


class TestAdminGuard(unittest.TestCase):
    def test_loopback_only_without_token(self):
        saved = app_api.ADMIN_TOKEN
        app_api.ADMIN_TOKEN = ""
        try:
            self.assertTrue(app_api.admin_allowed("127.0.0.1", None))
            self.assertTrue(app_api.admin_allowed("::1", None))
            self.assertFalse(app_api.admin_allowed("10.0.0.5", None))
            # Behind a reverse proxy the host is the proxy's loopback address and
            # proves nothing, so the exemption must not apply.
            self.assertFalse(app_api.admin_allowed("127.0.0.1", None, proxied=True))
        finally:
            app_api.ADMIN_TOKEN = saved

    def test_token_required_when_configured(self):
        saved = app_api.ADMIN_TOKEN
        app_api.ADMIN_TOKEN = "s3cret"
        try:
            self.assertTrue(app_api.admin_allowed("10.0.0.5", "s3cret"))
            self.assertFalse(app_api.admin_allowed("10.0.0.5", "nope"))
            self.assertFalse(app_api.admin_allowed("127.0.0.1", None))
            self.assertTrue(app_api.admin_allowed("127.0.0.1", "s3cret", proxied=True))
        finally:
            app_api.ADMIN_TOKEN = saved


class TestFoldHistory(unittest.TestCase):
    def test_folds_r7_children_into_parent(self):
        parent = h3lib.latlng_to_cell(33.749, -84.388, 6)
        kids = h3lib.cell_to_children(parent, 7)
        month = date(2026, 11, 1)
        rows = [(month, kids[0], 3, 1, None), (month, kids[1], 2, 2, None)]
        agg = app_api.fold_history(rows, 6)
        det, det_dev, _ = agg[(month, parent)]
        self.assertEqual(det, 5)
        self.assertEqual(det_dev, 2)  # max, never a sum across cells

    def test_same_res_passes_through(self):
        cell = h3lib.latlng_to_cell(33.749, -84.388, 7)
        month = date(2026, 11, 1)
        agg = app_api.fold_history([(month, cell, 4, 1, None)], 7)
        self.assertIn((month, cell), agg)
        self.assertEqual(agg[(month, cell)][0], 4)
