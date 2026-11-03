"""
Proof of work for raymap ingest.

Must stay byte-for-byte compatible with `pow_verify` in raymap-map/app_api.py:

    bits   = 12 + ceil(log2(max(1, n_records)))
    bucket = floor(now / 300)                 (server accepts bucket and bucket-1)
    digest = sha256(raw request body).hexdigest()
    header = "<bits>:<nonce>" where
             sha256("<device_hash>|<bucket>|<digest>|<nonce>") has >= bits
             leading zero bits

Cost for an honest device is a few milliseconds per batch; a batch claiming
thousands of records costs roughly linear CPU.
"""

from __future__ import annotations

import hashlib
import time

POW_BASE_BITS = 12
POW_BUCKET_S = 300


def leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        bits += 8 - byte.bit_length()
        break
    return bits


def target_bits(n_records: int) -> int:
    return POW_BASE_BITS + (max(1, int(n_records)) - 1).bit_length()


def solve_header(raw_body: bytes, device_hash: str, n_records: int,
                 now: float | None = None) -> str:
    """Return the X-Raymap-Pow header value for this exact payload."""
    bits = target_bits(n_records)
    digest = hashlib.sha256(raw_body).hexdigest()
    bucket = int((now if now is not None else time.time()) // POW_BUCKET_S)
    prefix = f"{device_hash}|{bucket}|{digest}|"
    nonce = 0
    while True:
        candidate = hashlib.sha256((prefix + str(nonce)).encode()).digest()
        if leading_zero_bits(candidate) >= bits:
            return f"{bits}:{nonce}"
        nonce += 1
