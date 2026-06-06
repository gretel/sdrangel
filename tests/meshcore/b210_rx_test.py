#!/usr/bin/env python3
"""B210/USRP MeshCore RX test.

Launches sdrangel headless with USRP B210 input + MeshcoreDemod channel,
triggers ADVERTs from companion, monitors UDP for decoded frames.

Usage:
  ./b210_rx_test.py                              # full flow
  ./b210_rx_test.py --listen-only                # just listen (companion TX separately)
  ./b210_rx_test.py --duration 60                # longer listen
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
BUILD_DIR = PROJECT_ROOT / "build"
SDRANGEL_BIN = BUILD_DIR / "sdrangel"
CAPTURE_DIR = PROJECT_ROOT / "tmp" / "captures"

REST_BASE = "http://127.0.0.1:8091"
UDP_PORT = 9999

# --- MeshCore EU868 defaults (HarnessOptions B6.11.0 baseline) ---
FREQ_HZ = 869_618_000
LO_OFFSET = 62_500
DEV_SAMPLE_RATE = 1_000_000
LOG2_SOFT_DECIM = 2
RX_GAIN = 40
GAIN_MODE_MANUAL = 1
RX_ANTENNA = "RX2"
CLOCK_SOURCE = "internal"

BW_IDX = 18       # 62500 Hz
SF = 8
PARITY_BITS = 4
PREAMBLE_CHIRPS = 16
SYNC_WORD = 0x12
MCR_HZ = 24_000_000  # LibreSDR_B220mini MCR

# Companion
COMPANION_TCP = "10.0.23.152:5000"
LORA_HWTEST = PROJECT_ROOT / ".venv" / "bin" / "lora"


# ---------------------------------------------------------------------------
# REST helpers (from lib_sdrangel.py, local copy for self-containment)
# ---------------------------------------------------------------------------

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


def poll_ready(timeout: float = 40.0, settle: float = 5.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        code, _ = rest("GET", "/sdrangel", timeout=1.5)
        if code == 200:
            time.sleep(settle)
            return True
        time.sleep(0.5)
    return False


def get_devsets() -> list[dict[str, Any]]:
    d = get_json(REST_BASE + "/sdrangel/devicesets")
    return d.get("deviceSets", []) if d else []


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


def stop_devset(ds: int) -> None:
    rest("DELETE", f"/sdrangel/deviceset/{ds}/device/run")


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
                settings: dict[str, Any], direction: int) -> int:
    pre = {}
    devsets = get_devsets()
    if ds < len(devsets):
        pre = {int(c["index"]) for c in devsets[ds].get("channels", []) if "index" in c}
    else:
        pre = set()
    code, b = rest("POST", f"/sdrangel/deviceset/{ds}/channel",
                   {"channelType": channel_type, "direction": direction})
    if code // 100 != 2:
        raise RuntimeError(f"add_channel {channel_type}: {code} {b}")
    time.sleep(0.5)
    devsets = get_devsets()
    post = {int(c["index"]): c.get("id", "")
            for c in devsets[ds].get("channels", []) if "index" in c}
    new = [i for i in post if i not in pre]
    if not new:
        new = [i for i, t in post.items() if t == channel_type]
    if not new:
        raise RuntimeError(f"channel {channel_type} not found after POST")
    idx = new[-1]
    body = {"channelType": channel_type, "direction": direction,
            settings_key: settings}
    code, b = rest("PATCH",
                   f"/sdrangel/deviceset/{ds}/channel/{idx}/settings", body)
    if code // 100 != 2:
        raise RuntimeError(f"patch_channel {channel_type} idx[{idx}]: {code} {b}")
    return idx


def device_run(ds: int) -> None:
    code, b = rest("POST", f"/sdrangel/deviceset/{ds}/device/run", {})
    if code // 100 != 2:
        raise RuntimeError(f"device_run ds[{ds}]: {code} {b}")


# ---------------------------------------------------------------------------
# SDRangel lifecycle
# ---------------------------------------------------------------------------

sdr_proc: subprocess.Popen | None = None


def launch_sdrangel() -> subprocess.Popen:
    global sdr_proc
    # Kill stale instances first
    subprocess.run(["pkill", "-9", "sdrangel"], capture_output=True)
    time.sleep(1.5)
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    logfile = CAPTURE_DIR / "sdrangel_b210.log"
    env = os.environ.copy()
    env.setdefault("UHD_IMAGES_DIR",
        "/Users/tom/src/uhd/ettus-uhd-oc/install/share/uhd/images")
    sdr_proc = subprocess.Popen(
        [str(SDRANGEL_BIN)],
        stdout=open(logfile, "w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    if not poll_ready(60, settle=8.0):
        raise RuntimeError("sdrangel did not start in time")
    return sdr_proc


def kill_sdrangel() -> None:
    global sdr_proc
    subprocess.run(["pkill", "-9", "sdrangel"], capture_output=True)
    if sdr_proc:
        try:
            sdr_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sdr_proc.kill()
        sdr_proc = None


# ---------------------------------------------------------------------------
# UDP listener
# ---------------------------------------------------------------------------

def listen_udp(port: int, duration: float) -> list[dict[str, Any]]:
    """Listen on UDP port, return parsed JSON frames."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    sock.bind(("0.0.0.0", port))
    frames: list[dict[str, Any]] = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < duration:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        try:
            msg = json.loads(data.decode().strip())
            frames.append(msg)
            t = msg.get("type", msg.get("messageType", "?"))
            sys.stdout.write(f"\r  RX: type={t} data={json.dumps(msg)[:160]}   \n")
            sys.stdout.flush()
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            sys.stdout.write(f"\r  non-JSON: {data[:80]} ({e})   \n")
            sys.stdout.flush()
    sock.close()
    return frames


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

MESHCORE_DEMOD_SETTINGS_KEY = "MeshtasticDemodSettings"  # borrows donor SWG

def main() -> int:
    import argparse
    import threading
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen-only", action="store_true")
    ap.add_argument("--duration", type=int, default=45)
    args = ap.parse_args()

    print("=== B210 MeshCore RX Test ===")
    print(f"  Freq:      {FREQ_HZ} Hz")
    print(f"  LO offset: {LO_OFFSET} Hz")
    print(f"  SR:        {DEV_SAMPLE_RATE} -> {DEV_SAMPLE_RATE >> LOG2_SOFT_DECIM} ch")
    print(f"  BW idx:    {BW_IDX} ({62500} Hz)")
    print(f"  SF:        {SF}")
    print(f"  CR:        4/{4 + PARITY_BITS}")
    print(f"  Gain:      {RX_GAIN} dB ({RX_ANTENNA})")
    print(f"  Companion: {COMPANION_TCP}")
    print(f"  Duration:  {args.duration}s")
    print()

    usrp_settings = {
        "centerFrequency": FREQ_HZ,
        "devSampleRate": DEV_SAMPLE_RATE,
        "log2SoftDecim": LOG2_SOFT_DECIM,
        "antennaPath": RX_ANTENNA,
        "loOffset": LO_OFFSET,
        "gain": RX_GAIN,
        "gainMode": GAIN_MODE_MANUAL,
        "clockSource": CLOCK_SOURCE,
        "masterClockRate": MCR_HZ,
        "dcBlock": 1,
        "iqCorrection": 1,
    }
    demod_settings = {
        "inputFrequencyOffset": LO_OFFSET,
        "bandwidthIndex": BW_IDX,
        "spreadFactor": SF,
        "deBits": 0,
        "decodeActive": 1,
        "nbParityBits": PARITY_BITS,
        "preambleChirps": PREAMBLE_CHIRPS,
        "sendViaUDP": 0,
        "sendJsonViaUDP": 1,
        "udpAddress": "127.0.0.1",
        "udpPort": UDP_PORT,
    }

    try:
        print("[1/4] Launching sdrangel...")
        launch_sdrangel()
        remove_trailing(0)
        time.sleep(1)

        print("[2/4] Creating USRP B210 deviceset...")
        ds = add_devset(0)
        print(f"  Deviceset {ds}")
        put_device(ds, "USRP", 0)
        print(f"  USRP device mounted")
        patch_device(ds, "usrpInputSettings", usrp_settings, "USRP", 0)
        print(f"  USRP settings applied")

        print("[3/4] Adding MeshcoreDemod channel...")
        ch = add_channel(ds, "MeshcoreDemod",
                         MESHCORE_DEMOD_SETTINGS_KEY, demod_settings, 0)
        print(f"  Channel {ch}: MeshcoreDemod")

        print(f"  Starting stream...")
        device_run(ds)
        time.sleep(3)

        print(f"[4/4] Listening for ADVERTs ({args.duration}s)...")
        frames: list[dict[str, Any]] = []
        def _listen():
            nonlocal frames
            frames = listen_udp(UDP_PORT, args.duration)
        t_listener = threading.Thread(target=_listen, daemon=True)
        t_listener.start()
        time.sleep(2)

        if not args.listen_only:
            print(f"\n  Triggering companion ADVERTs...")
            companion_proc = subprocess.Popen(
                [str(LORA_HWTEST), "hwtest", "transmit",
                 "--matrix", "basic", "--tcp", COMPANION_TCP],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            companion_proc.wait(timeout=args.duration + 20)
            print(f"\n  Companion done (exit={companion_proc.returncode})")
        else:
            print(f"  Listen-only mode, waiting...")

        t_listener.join()

        print(f"\n=== RESULTS ===")
        print(f"  UDP frames: {len(frames)}")
        if frames:
            for i, f in enumerate(frames):
                print(f"  [{i}] {json.dumps(f, indent=1)[:300]}")
            # Check for ADVERT-like frames
            adverts = [f for f in frames if
                       f.get("type") == "advert" or
                       "advert" in json.dumps(f).lower()]
            if adverts:
                print(f"\n  ✓ {len(adverts)} ADVERT frames decoded!")
                for f in adverts[:3]:
                    print(f"    {json.dumps(f)[:200]}")
            else:
                print(f"\n  ✗ No ADVERT frames decoded")
        else:
            print(f"\n  ✗ No frames received. Check:")
            print(f"     - sdrangel log: tmp/captures/sdrangel_b210.log")
            print(f"     - B210 connected? uhd_find_devices")
            print(f"     - Companion transmitting?")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        kill_sdrangel()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
