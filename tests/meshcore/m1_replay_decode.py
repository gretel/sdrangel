#!/usr/bin/env python3
"""M1 oracle: replay a .sdriq through MeshcoreDemod offline.

Spawns sdrangel headless with FileInput on a known .sdriq capture
(or M2 fileoutput roundtrip dump), routes through MeshcoreDemod with
UDP JSON sink on port 9998, and reports decoded frames.

PASS = `lora.header_crc=ok` AND payload starts with our pubkey (head
byte 0x11 ADVERT).  Use against `tmp/captures/onair_capture.*.sdriq`
for ground-truth oracle, or against `tmp/m2_q24.sdriq` (output of
`convert_fileoutput_to_demod.py`) for M2 roundtrip oracle.

Usage:
    python3 tests/meshcore/m1_replay_decode.py \\
        --sdriq tmp/captures/onair_capture.2026-05-01T14_46_18_602.sdriq \\
        --no-convert --listen 12
"""
from __future__ import annotations

import argparse
import binascii
import importlib.util
import json
import random
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent

# Reuse the lifecycle helpers (spawn, REST primitives) from lib_sdrangel.
_spec = importlib.util.spec_from_file_location(
    "lib_sdrangel", str(HERE / "lib_sdrangel.py"))
assert _spec and _spec.loader
sdr = importlib.util.module_from_spec(_spec)
sys.modules["lib_sdrangel"] = sdr
_spec.loader.exec_module(sdr)


CENTER_FREQ = 869_618_000
SAMPLE_RATE = 250_000   # MeshCore wire rate (BW=62.5k * os=4)
LO_OFFSET = 62_500
SF = 8
BW_INDEX = 18      # 62500 Hz
PARITY = 4
PREAMBLE = 16
UDP_PORT = 9998    # avoid clash with on-air harness on 9999
PUBKEY_HEX = "7d1ca6dcd567946d"   # first 8 bytes of our identity


# ---------------------------------------------------------------------------
# Format conversion: raw qint32 IQ -> .sdriq with sampleSize=16
# ---------------------------------------------------------------------------


def convert_dump_to_sdriq(src: Path, dst: Path,
                          sample_rate: int = SAMPLE_RATE,
                          center_freq: int = CENTER_FREQ) -> int:
    """Read raw qint32 IQ pairs (8 bytes/sample), wrap as sdriq sampleSize=24.

    sdrangel build on this host has compile-time `Sample = { qint32; qint32 }`
    (24-bit RX sample size).  FileInput's reader DOES NOT auto-promote
    int16 input to int32 on this build (verified empirically: writing
    sampleSize=16 produced silent demod input — signal_db=-150).
    On-air capture written by sdrangel itself uses sampleSize=24 with raw
    qint32 IQ bytes.  Match that format exactly: copy dump bytes verbatim
    (already in qint32 IQ layout) + 32-byte header with sampleSize=24.

    Strips leading/trailing zero pads + replaces with low-amp white noise
    so MeshcoreDemod's noise-floor estimator sees realistic pre/post-burst
    conditions (pure zeros -> noise_db=-inf -> signal_db=-150 -> preamble
    correlator never locks).

    Returns number of samples wrapped.
    """
    raw = src.read_bytes()
    if len(raw) % 8:
        raise ValueError(f"{src} has {len(raw)} bytes - not multiple of 8 "
                         f"(qint32 IQ pair)")
    n_raw = len(raw) // 8
    print(f"  reading {n_raw} qint32 IQ samples ({len(raw)} bytes)")

    all_i32 = list(struct.unpack(f"<{2*n_raw}i", raw))
    ii = all_i32[0::2]
    qq = all_i32[1::2]

    # Strip leading/trailing zero pads + replace with low-amp white noise.
    # See docstring above for noise-floor rationale.
    first = next((k for k, (a, b) in enumerate(zip(ii, qq)) if a or b), 0)
    last = n_raw - next(
        (k for k, (a, b) in enumerate(zip(reversed(ii), reversed(qq))) if a or b),
        0,
    )
    n_active = max(0, last - first)
    print(f"  active signal: samples [{first}..{last}] = {n_active} "
          f"({n_active/sample_rate:.3f}s @ {sample_rate}Hz)")
    if n_active == 0:
        raise ValueError(f"{src}: no non-zero samples - render dumped silence")

    sig_i = ii[first:last]
    sig_q = qq[first:last]
    peak = max(max(abs(v) for v in sig_i), max(abs(v) for v in sig_q)) or 1
    print(f"  active peak abs: {peak}")

    # MeshcoreModSource produces samples scaled to q16 range
    # (~30k peak, full-scale q16=32767).  Predecessor on-air capture
    # written by USRPInput uses q24 range (peak ~500k, full-scale
    # q24 = 2^23 = 8388607).  FileInput @ sampleSize=24 normalises
    # by q24 full-scale internally -- q16-range values look 256x
    # quieter and fall below the preamble correlator threshold.
    target_peak = 4_000_000   # ~half of q24 full-scale, leaves headroom
    scale = max(1.0, float(target_peak) / float(peak))
    if scale > 1.0:
        print(f"  scaling by {scale:.1f}x to land peak ~{target_peak} (q24 range)")
        sig_i = [int(round(v * scale)) for v in sig_i]
        sig_q = [int(round(v * scale)) for v in sig_q]

    # Low-amplitude white noise pad (~ -46 dB rel to signal peak).
    # ~1/200 of signal peak: well below preamble correlator threshold
    # but non-zero so log() of average magnitude does not go to -inf.
    noise_amp = max(1, int(target_peak / 200))
    pad_pre_samples = int(0.10 * sample_rate)   # 100 ms lead-in
    # Trailing noise must give demod enough samples to FLUSH the last
    # symbols (FFT buffer + Hamming + header CRC + payload FEC pipeline).
    # 100 ms is too short -> early_eom=true, payload_hex empty.
    # 500 ms covers SF8 worst-case decoder latency (~270 symbols * 4 ms).
    pad_post_samples = int(0.50 * sample_rate)
    rng = random.Random(0xC0FFEE)
    pad_pre = [rng.randint(-noise_amp, noise_amp) for _ in range(2 * pad_pre_samples)]
    pad_post = [rng.randint(-noise_amp, noise_amp) for _ in range(2 * pad_post_samples)]
    print(f"  noise pad: pre={pad_pre_samples} post={pad_post_samples} "
          f"(noise_amp={noise_amp}, ~ -46 dB)")

    # Interleave I/Q for the active body.
    body_iq = []
    for a, b in zip(sig_i, sig_q):
        body_iq.append(a)
        body_iq.append(b)

    full = pad_pre + body_iq + pad_post
    body = struct.pack(f"<{len(full)}i", *full)

    # 32-byte sdriq header.  sampleSize=24 matches the build\'s Sample
    # type and the predecessor on-air capture format.
    prefix = struct.pack("<IQQII",
                         sample_rate,
                         center_freq,
                         0,          # startTimeStamp = unknown
                         24,         # sampleSize (matches qint32 fields)
                         0)          # filler
    assert len(prefix) == 28
    crc = binascii.crc32(prefix) & 0xFFFFFFFF
    header = prefix + struct.pack("<I", crc)

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as f:
        f.write(header)
        f.write(body)
    total = pad_pre_samples + n_active + pad_post_samples
    print(f"  wrote {dst} (header=32B + {total} samples * 8 = "
          f"{dst.stat().st_size} bytes)")
    return n_active


# ---------------------------------------------------------------------------
# REST setup: FileInput device + MeshcoreDemod channel
# ---------------------------------------------------------------------------


def configure_replay(ds: int, sdriq_path: Path) -> int:
    """Mount FileInput on deviceset[ds], add MeshcoreDemod, return ch idx."""
    sdr.put_device(ds, "FileInput", direction=0)

    sdr.patch_device_settings(
        ds, "fileInputSettings",
        {
            "fileName": str(sdriq_path.absolute()),
            "loop": 0,
            "fileRecordType": 0,
            "playLoop": 0,
            "accelerationFactor": 1,
            # Force log2SoftDecim=0 so channel rate == file sample rate.
            # MeshcoreDemod with os=4 + channel=BW*os=250ksps -> interpolator
            # distance=1.0 (safe decimate path).  Default may be > 0 which
            # would put us in distance<1 upsample regime (works post the
            # 2026-05-01 fix but extra failure surface).
            "log2SoftDecim": 0,
        },
        "FileInput", direction=0,
    )

    # OFFLINE REPLAY: file IQ is already at baseband (mod produced
    # samples centered at 0 Hz; m_inputFrequencyOffset on the mod side
    # was 0).  Demod inputFrequencyOffset must therefore be 0, NOT the
    # on-air harness's LO_OFFSET=62500.  Setting it nonzero shifts the
    # signal away from the dechirper's expected center -> wrong-bin
    # sync (got 0xc3 instead of 0x12 in first attempt).
    demod_settings = {
        "inputFrequencyOffset": 0,
        "bandwidthIndex": BW_INDEX,
        "spreadFactor": SF,
        "deBits": 0,
        "decodeActive": 1,
        "nbParityBits": PARITY,
        "preambleChirps": PREAMBLE,
        "sendViaUDP": 0,
        "sendJsonViaUDP": 1,
        "udpAddress": "127.0.0.1",
        "udpPort": UDP_PORT,
    }
    return sdr.add_channel(
        ds, "MeshcoreDemod", "MeshtasticDemodSettings",
        demod_settings, direction=0,
    )


# ---------------------------------------------------------------------------
# Listen + classify
# ---------------------------------------------------------------------------


def listen_for_decode(timeout_s: float) -> list[dict[str, Any]]:
    """Bind UDP, drain for `timeout_s` seconds, return parsed frames."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", UDP_PORT))
    s.settimeout(0.5)
    frames: list[dict[str, Any]] = []
    deadline = time.time() + timeout_s
    last_progress = time.time()
    while time.time() < deadline:
        try:
            data, _ = s.recvfrom(65536)
        except socket.timeout:
            continue
        try:
            frames.append(json.loads(data.decode()))
            elapsed = time.time() - last_progress
            print(f"  [{elapsed:.1f}s] frame {len(frames)}: "
                  f"hdr={frames[-1].get('lora', {}).get('header_crc', '?')} "
                  f"sig={frames[-1].get('rf', {}).get('signal_db', 0):.1f}")
            last_progress = time.time()
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    s.close()
    return frames


def classify(frames: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Return (verdict, summary lines)."""
    summary: list[str] = []
    if not frames:
        return "NO_FRAMES", summary
    our = []
    other = []
    for fr in frames:
        lora = fr.get("lora", {})
        rf = fr.get("rf", {})
        payload = lora.get("payload_hex", "")
        hdr = lora.get("header_crc", "?")
        hfec = lora.get("header_fec", "?")
        pay = lora.get("payload_crc", "?")
        pfec = lora.get("payload_fec", "?")
        head8 = payload[:16]
        head_long = payload[:128]
        line = (f"  hdr={hdr} hfec={hfec} pay={pay} pfec={pfec} "
                f"sf={rf.get('spreading_factor')} "
                f"bw={rf.get('bandwidth_hz')} sig={rf.get('signal_db', 0):.1f}dB "
                f"len={lora.get('packet_length')} "
                f"sync={lora.get('sync_word')}")
        summary.append(line)
        summary.append(f"      head128={head_long}")
        # Our ADVERT: head byte 0x11, then pubkey first 8 bytes.
        if head8.startswith("11") and PUBKEY_HEX in payload[:96]:
            our.append(fr)
        else:
            other.append(fr)
    summary.insert(0, f"  total frames: {len(frames)}; "
                      f"OUR_ADVERT={len(our)}; other={len(other)}")
    if our:
        return "PASS", summary
    if any(f.get("lora", {}).get("header_crc") == "ok" for f in frames):
        return "DECODE_NOT_OURS", summary
    return "FAIL_NO_HEADER_OK", summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path,
                    help="raw qint32 IQ dump to convert -> .sdriq "
                    "(only with default convert mode; rarely needed)")
    ap.add_argument("--sdriq", type=Path,
                    default=PROJECT_ROOT / "tmp" / "captures"
                    / "onair_capture.2026-05-01T14_46_18_602.sdriq")
    ap.add_argument("--listen", type=float, default=12.0,
                    help="seconds to wait for UDP decode frames")
    ap.add_argument("--no-convert", action="store_true",
                    help="skip raw->sdriq conversion; --sdriq points at "
                    "an existing pre-built .sdriq file (e.g. predecessor "
                    "on-air capture for replay-tool sanity test)")
    args = ap.parse_args()

    if args.no_convert:
        if not args.sdriq.exists():
            print(f"ERROR: --no-convert requires existing {args.sdriq}")
            return 2
        print(f"[1/4] using existing {args.sdriq} (no convert)")
    else:
        if not args.input or not args.input.exists():
            print("ERROR: convert mode requires --input <raw qint32 IQ dump>")
            return 2
        print("[1/4] convert dump -> sdriq")
        n = convert_dump_to_sdriq(args.input, args.sdriq)
        print(f"      {n} samples wrapped, sr={SAMPLE_RATE} cf={CENTER_FREQ}")

    print("[2/4] spawn sdrangel (no soapy, no buddy share, no MCR pin needed)")
    sdr.kill_stale_sdrangel()
    sdr.wipe_plist()
    log = HERE / "results" / "m1_replay.log"
    proc = sdr.SDRangel(log_path=log, soapy=False, mcr_hz=None)
    proc.spawn()
    if not proc.wait_ready(timeout=30):
        print("FAIL: sdrangel never reached REST-ready")
        proc.kill()
        return 3

    try:
        print("[3/4] mount FileInput + MeshcoreDemod, run device")
        ds = sdr.add_deviceset(direction=0)
        ch = configure_replay(ds, args.sdriq)
        sdr.device_run(ds)
        print(f"      ds={ds} ch={ch}; listening UDP {UDP_PORT} for "
              f"{args.listen}s")

        print("[4/4] decoding...")
        frames = listen_for_decode(args.listen)
        verdict, lines = classify(frames)

        print(f"\n=========================")
        print(f"  VERDICT: {verdict}")
        print(f"=========================")
        for ln in lines:
            print(ln)
        # Always dump full frame JSON for diagnostic — payload_hex is what
        # we need to compare against expected encoder output.
        if frames:
            print("\n--- raw frames ---")
            for i, fr in enumerate(frames):
                print(f"frame[{i}]:")
                print(json.dumps(fr, indent=2))
        return 0 if verdict == "PASS" else 1
    finally:
        proc.kill(grace=2.0)
        sdr.kill_stale_sdrangel()


if __name__ == "__main__":
    sys.exit(main())
