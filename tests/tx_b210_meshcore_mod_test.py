#!/usr/bin/env python3
"""B210 TX test: USRP output + MeshcoreMod — verify sample rate fix.

Launches sdrangel, configures B210 TX output + MeshcoreMod (ADVERT),
transmits for N seconds, then checks the log for correct sample rate.

Usage:
  SDRANGEL_USRP_MASTER_CLOCK_RATE_HZ=24000000 ./tx_b210_meshcore_mod_test.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
BUILD_DIR = PROJECT_ROOT / "build"
SDRANGEL_BIN = BUILD_DIR / "sdrangel"
CAPTURE_DIR = PROJECT_ROOT / "tmp" / "captures"

REST_BASE = "http://127.0.0.1:8091"

FREQ_HZ = 869_618_000
LO_OFFSET = 0
DEV_SAMPLE_RATE = 250_000
LOG2_SOFT_INTERP = 0
TX_GAIN = 73
TX_ANTENNA = "TX/RX"
CLOCK_SOURCE = "internal"
MCR_HZ = 24_000_000

BW_IDX = 18       # 62500 Hz
SF = 8
PARITY_BITS = 4
PREAMBLE_CHIRPS = 8
SYNC_WORD = 0x12


def req(method: str, url: str, body: Any = None,
        timeout: float = 10.0) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            txt = resp.read().decode()
            return resp.status, json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = None
        return e.code, body
    except (urllib.error.URLError, ConnectionRefusedError, OSError):
        return 0, None


def get_json(path: str, timeout: float = 10.0) -> dict[str, Any] | None:
    code, data = req("GET", path, timeout=timeout)
    return data if code // 100 == 2 else None


def rest(method: str, path: str, body: Any = None,
         timeout: float = 10.0) -> tuple[int, Any]:
    return req(method, REST_BASE + path, body, timeout)


def poll_ready(timeout: float = 60.0, settle: float = 10.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        code, _ = rest("GET", "/sdrangel", timeout=1.5)
        if code == 200:
            time.sleep(settle)
            return True
        time.sleep(0.5)
    return False


def get_devset_count() -> int:
    d = get_json(REST_BASE + "/sdrangel/devicesets")
    return d.get("devicesetcount", 0) if d else 0


def add_devset(direction: int, timeout: float = 90.0) -> int:
    pre = get_devset_count()
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, _ = rest("POST", f"/sdrangel/deviceset?direction={direction}",
                       timeout=4.0)
        if code == 202:
            break
        if code == 0:
            time.sleep(0.5)
            continue
        raise RuntimeError(f"add_devset: {code}")
    while time.time() < deadline:
        if get_devset_count() > pre:
            return pre
        time.sleep(0.5)
    raise TimeoutError(f"add_devset timeout ({timeout}s)")


def remove_trailing(target_count: int) -> None:
    while get_devset_count() > target_count:
        rest("DELETE", "/sdrangel/deviceset")
        time.sleep(0.3)


def put_device(ds: int, hw_type: str, direction: int,
               sequence: int = 0, stream_index: int = 0) -> None:
    body = {
        "hwType": hw_type, "direction": direction,
        "deviceSequence": sequence, "deviceStreamIndex": stream_index,
    }
    code, b = rest("PUT", f"/sdrangel/deviceset/{ds}/device", body, timeout=60.0)
    if code // 100 != 2:
        raise RuntimeError(f"put_device {hw_type}: {code} {b}")


def patch_device(ds: int, settings_key: str, settings: dict[str, Any],
                 hw_type: str, direction: int) -> None:
    body = {"deviceHwType": hw_type, "direction": direction,
            settings_key: settings}
    code, b = rest("PATCH", f"/sdrangel/deviceset/{ds}/device/settings",
                   body, timeout=30.0)
    if code // 100 != 2:
        raise RuntimeError(f"patch_device ds[{ds}]: {code} {b}")


def add_channel(ds: int, channel_type: str, settings_key: str,
                settings: dict[str, Any], direction: int) -> None:
    code, b = rest("POST", f"/sdrangel/deviceset/{ds}/channel",
                   {"channelType": channel_type, "direction": direction})
    if code // 100 != 2:
        raise RuntimeError(f"add_channel {channel_type}: {code} {b}")
    time.sleep(0.3)
    body = {"channelType": channel_type, "direction": direction,
            settings_key: settings}
    code, b = rest("PATCH", f"/sdrangel/deviceset/{ds}/channel/0/settings",
                   body)
    if code // 100 != 2:
        raise RuntimeError(f"patch_channel {channel_type}: {code} {b}")


def device_run(ds: int) -> None:
    code, b = rest("POST", f"/sdrangel/deviceset/{ds}/device/run", {})
    if code // 100 != 2:
        raise RuntimeError(f"device_run ds[{ds}]: {code} {b}")


def device_stop(ds: int) -> None:
    rest("DELETE", f"/sdrangel/deviceset/{ds}/device/run")


# ---------------------------------------------------------------------------

MESHCORE_MOD_SETTINGS_KEY = "MeshtasticModSettings"  # borrows donor SWG

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=10,
                    help="TX duration (seconds)")
    ap.add_argument("--soapy", action="store_true",
                    help="Use SoapySDR output instead of native USRP")
    ap.add_argument("--mcr", type=int, default=MCR_HZ,
                    help="Master clock rate (Hz)")
    args = ap.parse_args()

    # Ensure MCR env var is set (needed by device open)
    mcr_str = str(args.mcr)
    if "SDRANGEL_USRP_MASTER_CLOCK_RATE_HZ" not in os.environ:
        os.environ["SDRANGEL_USRP_MASTER_CLOCK_RATE_HZ"] = mcr_str

    logfile = CAPTURE_DIR / "sdrangel_tx.log"
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

    hw_type = "SoapySDR" if args.soapy else "USRP"
    dev_settings_key = "soapySdrOutputSettings" if args.soapy else "usrpOutputSettings"

    dev_settings = {
        "centerFrequency": FREQ_HZ,
        "devSampleRate": DEV_SAMPLE_RATE,
        "log2SoftInterp": LOG2_SOFT_INTERP,
        "antennaPath": TX_ANTENNA,
        "loOffset": LO_OFFSET,
        "gain": TX_GAIN,
        "gainMode": 1,  # manual
        "clockSource": CLOCK_SOURCE,
        "masterClockRate": MCR_HZ,
    }
    if args.soapy:
        dev_settings["bandwidth"] = 250000

    demod_settings = {
        "inputFrequencyOffset": 0,
        "bandwidthIndex": BW_IDX,
        "spreadFactor": SF,
        "deBits": 0,
        "nbParityBits": PARITY_BITS,
        "preambleChirps": PREAMBLE_CHIRPS,
        "syncWord": SYNC_WORD,
        "messageType": 1,  # ADVERT
        "messageRepeat": 3,
        "channelMute": 0,
    }

    sdr_proc: subprocess.Popen | None = None

    try:
        # Kill stale
        subprocess.run(["pkill", "-9", "sdrangel"], capture_output=True)
        time.sleep(1.5)

        print("[1/4] Launching sdrangel...")
        env = os.environ.copy()
        env.setdefault("UHD_IMAGES_DIR",
            "/Users/tom/src/uhd/ettus-uhd-oc/install/share/uhd/images")
        sdr_proc = subprocess.Popen(
            [str(SDRANGEL_BIN)],
            stdout=open(logfile, "w"), stderr=subprocess.STDOUT,
            env=env,
        )
        if not poll_ready(60, settle=10.0):
            raise RuntimeError("sdrangel did not start in time")
        print(f"  PID {sdr_proc.pid}")

        remove_trailing(0)
        time.sleep(0.5)

        print(f"[2/4] Creating {hw_type} TX deviceset...")
        ds = add_devset(1)  # direction=1 = TX
        print(f"  Deviceset {ds}")
        put_device(ds, hw_type, 1)
        print(f"  Device mounted")
        patch_device(ds, dev_settings_key, dev_settings, hw_type, 1)
        print(f"  Settings applied")

        print(f"[3/4] Adding MeshcoreMod channel...")
        add_channel(ds, "MeshcoreMod",
                    MESHCORE_MOD_SETTINGS_KEY, demod_settings, 1)
        print(f"  MeshcoreMod configured")

        print(f"[4/4] Starting TX for {args.duration}s...")
        device_run(ds)
        print(f"  Streaming started")
        time.sleep(args.duration)
        device_stop(ds)
        print(f"  Streaming stopped")

        # Check the log for key rate info
        print(f"\n=== Log check ===")
        with open(logfile) as f:
            text = f.read()

        for line in text.split("\n"):
            if "actual sample rate" in line.lower():
                print(f"  RATE: {line.strip()}")
            if "master_clock_rate" in line.lower() and "applySettings" in line:
                print(f"  MCR:  {line.strip()}")
            if "sample rate set" in line.lower():
                print(f"  SET:  {line.strip()}")
            if "auto_tick_rate" in line.lower():
                print(f"  ATR:  {line.strip()}")
            if "SampleSourceFifo" in line and "overrun" in line.lower():
                print(f"  OVR:  {line.strip()}")
            if "SampleSourceFifo" in line and "underrun" in line.lower():
                print(f"  UNR:  {line.strip()}")

        # Check for errors
        errors = [l for l in text.split("\n")
                  if "error" in l.lower() or "LIBUSB" in l or "CRITICAL" in l]
        if errors:
            print(f"\n  Errors/Warnings ({len(errors)} lines):")
            for e in errors[:5]:
                print(f"    {e.strip()}")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if sdr_proc:
            subprocess.run(["pkill", "-9", "sdrangel"], capture_output=True)
            try:
                sdr_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sdr_proc.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
