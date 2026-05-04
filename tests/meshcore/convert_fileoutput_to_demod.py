#!/usr/bin/env python3
"""Convert a FileOutput-recorded .sdriq into MeshcoreDemod-replayable form.

The FileOutput plugin writes sampleSize=16 (matches dspTxBits=16) but
FileInput/MeshcoreDemod expect sampleSize=24 (matches dspRxBits=24).
sampleSize=16 played back gives `signal_db=-150` because the demod's
amplitude scaling assumes q24 magnitudes; q16 input is 256x too quiet
to clear the preamble correlator threshold.

This helper:
  1. Reads the source .sdriq (ss=16, qint16 IQ)
  2. Strips leading + trailing zero runs (TX run captures silence
     around the actual burst)
  3. Scales active samples to ~half-scale of q24 (peak ~4M)
  4. Wraps with deterministic-seed white-noise pads (100 ms lead-in,
     500 ms tail) so the noise-floor estimator has a non-zero
     reference and the per-symbol decode pipeline gets enough samples
     to flush
  5. Writes a new .sdriq with sampleSize=24 (qint32 IQ)

The output is directly replayable through `m1_replay_decode.py
--no-convert --sdriq <output>` (or any FileInput-fed MeshcoreDemod
chain).

Usage:
    python3 tests/meshcore/convert_fileoutput_to_demod.py \\
        --input  tmp/m2_advert.sdriq \\
        --output tmp/m2_advert_q24.sdriq
"""
from __future__ import annotations

import argparse
import binascii
import random
import struct
from pathlib import Path

TARGET_PEAK = 4_000_000   # ~half-scale of q24 (full = 8388607)
NOISE_SEED = 0xC0FFEE
NOISE_FRACTION = 200      # noise_amp = TARGET_PEAK / NOISE_FRACTION
LEAD_IN_S = 0.10          # 100 ms pre-burst noise (noise floor reference)
TAIL_S = 0.50             # 500 ms post-burst noise (decoder flush room)


def convert(src: Path, dst: Path) -> None:
    data = src.read_bytes()
    sr, cf, ts, ss, fl, _ = struct.unpack("<IQQIII", data[:32])
    if ss != 16:
        raise ValueError(f"{src}: expected sampleSize=16 got {ss}")

    body = data[32:]
    n = len(body) // 4   # 2 int16 per sample
    ii = [0] * n
    qq = [0] * n
    for i in range(n):
        a, b = struct.unpack_from("<hh", body, i * 4)
        ii[i] = a
        qq[i] = b

    # Strip silence around the active burst.
    first = next((k for k, (a, b) in enumerate(zip(ii, qq)) if a or b), 0)
    last = n - next(
        (k for k, (a, b) in enumerate(zip(reversed(ii), reversed(qq))) if a or b),
        0,
    )
    n_active = max(0, last - first)
    if n_active == 0:
        raise ValueError(f"{src}: no non-zero samples (capture is all silence)")

    sig_i = ii[first:last]
    sig_q = qq[first:last]
    peak = max(max(abs(v) for v in sig_i), max(abs(v) for v in sig_q)) or 1
    print(f"in:  sr={sr} ss=16 samples={n} active=[{first}..{last}]={n_active} peak={peak}")

    # Scale q16 samples (peak ~30k) up to q24 range (peak ~4M).
    scale = max(1.0, float(TARGET_PEAK) / float(peak))
    print(f"     scale {scale:.1f}x  -> peak ~{TARGET_PEAK}")
    sig_i = [int(round(v * scale)) for v in sig_i]
    sig_q = [int(round(v * scale)) for v in sig_q]

    noise_amp = max(1, TARGET_PEAK // NOISE_FRACTION)
    pre_n = int(LEAD_IN_S * sr)
    post_n = int(TAIL_S * sr)
    rng = random.Random(NOISE_SEED)
    pad_pre = [rng.randint(-noise_amp, noise_amp) for _ in range(2 * pre_n)]
    pad_post = [rng.randint(-noise_amp, noise_amp) for _ in range(2 * post_n)]

    body_iq: list[int] = []
    for a, b in zip(sig_i, sig_q):
        body_iq.append(a)
        body_iq.append(b)

    full = pad_pre + body_iq + pad_post
    out = struct.pack(f"<{len(full)}i", *full)

    prefix = struct.pack("<IQQII", sr, cf, 0, 24, 0)
    crc = binascii.crc32(prefix) & 0xFFFFFFFF
    header = prefix + struct.pack("<I", crc)
    dst.write_bytes(header + out)
    dur_s = (len(full) // 2) / sr
    print(f"out: {dst} {dst.stat().st_size}B sampleSize=24 duration={dur_s:.3f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, required=True,
                    help="source .sdriq written by FileOutput (ss=16)")
    ap.add_argument("--output", type=Path, required=True,
                    help="destination .sdriq for FileInput playback (ss=24)")
    args = ap.parse_args()
    convert(args.input, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
