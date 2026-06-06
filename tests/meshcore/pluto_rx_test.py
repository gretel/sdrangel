#!/usr/bin/env python3
"""Minimal PlutoSDR MeshCore RX test (M4/M5 verifier).

Launches sdrangel headless with PlutoSDR input + MeshcoreDemod channel,
then triggers ADVERTs from a companion transmitter and monitors UDP
for JSON-format decoded frames.

Usage:
  ./pluto_rx_test.py                    # full flow
  ./pluto_rx_test.py --listen-only      # just listen (companion TX separately)
  ./pluto_rx_test.py --pluto-ip 10.0.23.193
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

# --- MeshCore EU868 defaults ---
FREQ_HZ = 869_618_000
LO_OFFSET = 62_500          # LO offset for DC spur mitigation
DEV_SAMPLE_RATE = 1_000_000  # Pluto ADC rate
LOG2_DECIM = 2               # → 250 ksps channel rate
BW_HZ = 62_500               # SF8 BW
SF = 8
PARITY_BITS = 4
PREAMBLE_CHIRPS = 8

# Companion
COMPANION_TCP = "10.0.23.152:5000"
LORA_HWTEST = PROJECT_ROOT / ".venv" / "bin" / "lora"

# PlutoSDR IP (IIO-TCP)
PLUTO_IP = "10.0.23.193"


# ---------------------------------------------------------------------------
# REST helpers
# ---------------------------------------------------------------------------

def req(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        path,
        data=data,
        method=method,
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


def request(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 5.0) -> tuple[int, Any]:
    return req(method, REST_BASE + path, body, timeout)


def poll_ready(timeout: float = 30.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        code, _ = req("GET", REST_BASE + "/sdrangel")
        if code // 100 == 2:
            return True
        time.sleep(0.5)
    return False


def get_devsets() -> list[dict[str, Any]]:
    d = get_json(REST_BASE + "/sdrangel/devicesets")
    return d.get("deviceSets", []) if d else []


def add_devset(direction: int, timeout: float = 30.0) -> int:
    pre = len(get_devsets())
    request("POST", f"/sdrangel/deviceset?direction={direction}")
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if len(get_devsets()) > pre:
            return pre
        time.sleep(0.5)
    raise TimeoutError("add_devset never finished")


def remove_trailing(target: int) -> None:
    while len(get_devsets()) > target:
        request("DELETE", "/sdrangel/deviceset")


def stop_devset(ds: int) -> None:
    request("DELETE", f"/sdrangel/deviceset/{ds}/device/run")


def put_device(ds: int, hw_type: str, direction: int, props: dict[str, Any]) -> None:
    """PUT a device of hw_type on deviceset ds."""
    body = {
        "direction": direction,
        "hwType": hw_type,
        "sequence": 0,
        **props,
    }
    code, resp = request("PUT", f"/sdrangel/deviceset/{ds}/device", body, timeout=30.0)
    if code // 100 != 2:
        raise RuntimeError(f"put_device: {code} {resp}")


def patch_device_settings(ds: int, settings_key: str, settings: dict[str, Any]) -> None:
    body = {settings_key: settings}
    code, resp = request("PATCH", f"/sdrangel/deviceset/{ds}/device/settings", body, timeout=10.0)
    if code // 100 != 2:
        raise RuntimeError(f"patch_device_settings: {code} {resp}")


def add_channel(ds: int, channel_type: str, settings_key: str, settings: dict[str, Any]) -> int:
    body = {
        "channelType": channel_type,
        "direction": 0,
        "index": 0,  # appended
        **settings,
    }
    code, resp = request("POST", f"/sdrangel/deviceset/{ds}/channel", body, timeout=10.0)
    if code // 100 != 2:
        raise RuntimeError(f"add_channel: {code} {resp}")
    # Get channel index from the deviceset listing
    for c in get_devsets()[ds].get("channels", []):
        if c.get("id", "").startswith(channel_type):
            return int(c["index"])
    raise RuntimeError("channel created but not found in listing")


def patch_channel_settings(ds: int, ch: int, settings_key: str, settings: dict[str, Any]) -> None:
    body = {settings_key: settings}
    code, resp = request("PATCH", f"/sdrangel/deviceset/{ds}/channel/{ch}/settings", body, timeout=10.0)
    if code // 100 != 2:
        raise RuntimeError(f"patch_channel: {code} {resp}")


def devset_run(ds: int) -> None:
    code, resp = request("POST", f"/sdrangel/deviceset/{ds}/device/run")
    if code // 100 != 2:
        raise RuntimeError(f"devset_run: {code} {resp}")


# ---------------------------------------------------------------------------
# SDRangel lifecycle
# ---------------------------------------------------------------------------

sdrangel_proc: subprocess.Popen | None = None


def launch_sdrangel() -> subprocess.Popen:
    global sdrangel_proc
    # Kill stale instances
    subprocess.run(["pkill", "-9", "sdrangel"], capture_output=True)
    time.sleep(1)
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    logfile = CAPTURE_DIR / "sdrangel.log"
    env = os.environ.copy()
    # Force the Pluto library path if needed
    sdrangel_proc = subprocess.Popen(
        [str(SDRANGEL_BIN)],
        stdout=open(logfile, "w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    if not poll_ready(40):
        raise RuntimeError("sdrangel did not start in time")
    return sdrangel_proc


def kill_sdrangel() -> None:
    global sdrangel_proc
    subprocess.run(["pkill", "-9", "sdrangel"], capture_output=True)
    if sdrangel_proc:
        try:
            sdrangel_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            sdrangel_proc.kill()
        sdrangel_proc = None


# ---------------------------------------------------------------------------
# UDP listener
# ---------------------------------------------------------------------------

def listen_udp(port: int, duration: float, label: str = "") -> list[dict[str, Any]]:
    """Listen on UDP port for `duration` seconds, return parsed JSON frames."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(duration)
    sock.bind(("0.0.0.0", port))
    frames: list[dict[str, Any]] = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < duration:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            break
        try:
            msg = json.loads(data.decode().strip())
            frames.append(msg)
            sys.stdout.write(f"\r  [{label}] RX frame: {json.dumps(msg)[:120]}   \n")
            sys.stdout.flush()
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            sys.stdout.write(f"\r  [{label}] non-JSON: {data[:80]} ({e})   \n")
            sys.stdout.flush()
    sock.close()
    return frames


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen-only", action="store_true",
                    help="Skip lora hwtest transmit; just listen")
    ap.add_argument("--pluto-ip", default=PLUTO_IP)
    ap.add_argument("--duration", type=int, default=30,
                    help="Total listen duration (s)")
    args = ap.parse_args()

    pluto_uri = f"ip:{args.pluto_ip}"

    print("=== PlutoSDR MeshCore RX Test ===")
    print(f"  Pluto:      {pluto_uri}")
    print(f"  Freq:       {FREQ_HZ} Hz")
    print(f"  LO offset:  {LO_OFFSET} Hz")
    print(f"  BW:         {BW_HZ} Hz (idx {MESHCORE_BW_62500})")
    print(f"  SF:         {SF}")
    print(f"  Companion:  {COMPANION_TCP}")
    print(f"  Duration:   {args.duration}s")
    print()

    # --- PlutoSDR input settings ---
    pluto_settings = {
        "centerFrequency": FREQ_HZ + LO_OFFSET,  # true center = freq - LO offset
        "devSampleRate": DEV_SAMPLE_RATE,
        "log2Decim": LOG2_DECIM,
        "lpfBW": 500_000,  # lowpass filter BW
        "gain": 50,
        "gainMode": 0,  # GAIN_MANUAL
        "antennaPath": 0,  # A_BAL
        "dcBlock": 1,
        "iqCorrection": 1,
        "LOppmTenths": 0,
        "transverterMode": False,
        "transverterDeltaFrequency": 0,
    }

    # --- MeshcoreDemod settings ---
    BW_62500 = 18  # index into bandwidths table
    demod_settings = {
        "inputFrequencyOffset": LO_OFFSET,
        "bandwidthIndex": BW_62500,
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

        # Clean slate
        remove_trailing(0)
        time.sleep(0.5)

        print("[2/4] Creating PlutoSDR deviceset...")
        ds = add_devset(0)  # direction 0 = RX
        put_device(ds, "PlutoSDR", 0, {})
        patch_device_settings(ds, "plutoSdrInputSettings", pluto_settings)
        print(f"  Deviceset {ds}: PlutoSDR configured")

        print("[3/4] Adding MeshcoreDemod channel...")
        ch = add_channel(ds, "MeshcoreDemod", "MeshtasticDemodSettings", {})
        patch_channel_settings(ds, ch, "MeshtasticDemodSettings", demod_settings)
        print(f"  Channel {ch}: MeshcoreDemod configured")

        # Start streaming
        devset_run(ds)
        print("  Streaming started")
        time.sleep(2)  # settle

        # Start UDP listener in background
        from threading import Thread
        frames: list[dict[str, Any]] = []

        def _listen():
            nonlocal frames
            frames = listen_udp(UDP_PORT, args.duration, "demod")

        listener = Thread(target=_listen, daemon=True)
        listener.start()
        time.sleep(1)

        if not args.listen_only:
            print(f"\n[4/4] Triggering companion ADVERTs ({args.duration}s listen)...")
            companion_proc = subprocess.Popen(
                [str(LORA_HWTEST), "hwtest", "transmit",
                 "--matrix", "basic",
                 "--tcp", COMPANION_TCP],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            # Let it run while UDP listener collects
            companion_proc.wait(timeout=args.duration + 30)
            print(f"\n  Companion done (exit={companion_proc.returncode})")
        else:
            print(f"\n[4/4] Listening only for {args.duration}s...")
            listener.join()

        listener.join()
        print(f"\n=== Results ===")
        print(f"  UDP frames received: {len(frames)}")
        if frames:
            print(f"  Raw frames:")
            for i, f in enumerate(frames):
                print(f"    [{i}] {json.dumps(f, indent=2)[:300]}")
            # Check for decoded ADVERTs
            decoded = [f for f in frames if "advert" in json.dumps(f).lower() or "decoded" in json.dumps(f).lower() or f.get("type") == "advert"]
            if decoded:
                print(f"  -> {len(decoded)} ADVERT-like frames found!")
            else:
                print(f"  -> No ADVERT frames detected")
        else:
            print(f"  WARNING: No UDP frames received at all")
            print(f"  Check: Pluto reachable? Demod settings match companion?")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        kill_sdrangel()

    return 0


# Convenience constants
MESHCORE_BW_62500 = 18


if __name__ == "__main__":
    raise SystemExit(main())
