#!/usr/bin/env python3
"""Donor-TX null-hypothesis oracle (Phase 2 step 1, M4 falsification).

Plan: tmp/plan_m4_falsification.md.

Hypothesis: stock upstream `usrpoutput` works for bursty LoRa-style
mods on the LibreSDR_B220mini hardware.  Donor `modmeshtastic` shares
`MeshtasticModEncoderLora` byte-identical with `modmeshcore`; if it
TXes cleanly through stock USRPOutput (post-revert of `a6f823eb`
`12e4e5c3`), the M4 PARTIAL "Heltec deafness" is downstream of the
streamer layer — bug is `modmeshcore`-specific (encoder bytes,
settings, identity, pipeline), NOT in `usrpoutput`.

Selectable donor TX:
  --mod chirpchat   -> ChirpChatMod   (parent donor of MeshcoreMod)
  --mod meshtastic  -> MeshtasticMod  (closest donor — shared encoder)
  --mod meshcore    -> MeshcoreMod    (test article, for A/B baseline)

Same RF params across mods (SF=8 BW=62.5k sync=0x12 preamble=16).
Continuous repeat (messageRepeat=0) gives ~30s of TX bursts for SA
observation.

Pre-conditions:
  - sdrangel running, ds[0] = USRP RX on B210 (buddy-share is the
    canonical use case; stock USRPOutput crash hypothesis was buddy-
    share-specific)
  - SDRANGEL_USRP_MASTER_CLOCK_RATE_HZ=24000000 in sdrangel's env
  - User at TinySA / equivalent for RF verification at 869.618 MHz

Result interpretation:
  - sdrangel survives 30s + RF on SA at 869.618 MHz + clean ASYNC
    -> usrpoutput stock OK for bursty mods on this hardware.  M4 bug
    is mod-specific.  Compare encoder bytes vs gr4-lora.
  - sdrangel crashes with libusb / SSSS storm
    -> real upstream usrpoutput+B210-clone interaction.  Time for a
    principled streamer-layer fix.
  - sdrangel survives but no RF on SA
    -> render or RF chain regression.  Re-verify gr4-lora baseline.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, cast

BASE = "http://127.0.0.1:8091"
CENTER_FREQ = 869_618_000
USRP_TX_RATE = 250_000
ANTENNA = "TX/RX"

# Mod variants: SWG schema key + channelType + body builder.
# All share the LoRa physical params (SF=8 BW=62.5k sync=0x12 preamble=16).
MOD_VARIANTS: dict[str, dict[str, Any]] = {
    "chirpchat": {
        "channel_type": "ChirpChatMod",
        "settings_key": "ChirpChatModSettings",
        "extra": {"textMessage": "PARITY-CHIRP"},
    },
    "meshtastic": {
        "channel_type": "MeshtasticMod",
        "settings_key": "MeshtasticModSettings",
        "extra": {"textMessage": "PARITY-MESHTASTIC"},
    },
    "meshcore": {
        "channel_type": "MeshcoreMod",
        "settings_key": "MeshtasticModSettings",   # donor-borrowed schema
        "extra": {"textMessage": "PARITY-MESHCORE"},
    },
}


def req(method, path, body=None, timeout=10):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except (urllib.error.URLError, ConnectionRefusedError, OSError) as e:
        return 0, str(e)


def get_devset_count():
    c, b = req("GET", "/sdrangel/devicesets")
    if c == 200:
        try:
            return json.loads(b).get("devicesetcount", 0)
        except json.JSONDecodeError:
            return -1
    return -1


def wait_devset_count(target, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get_devset_count() == target:
            return True
        time.sleep(0.2)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mod", choices=list(MOD_VARIANTS),
                    default="meshtastic",
                    help="donor mod plugin under test (default: meshtastic — "
                         "closest donor, shared encoder w/ modmeshcore)")
    ap.add_argument("--watch", type=float, default=30.0,
                    help="seconds to TX before survival check (default 30 — "
                         "long enough for SA observation)")
    ap.add_argument("--tx-stream-idx", type=int, default=0, choices=[0, 1],
                    help="USRP TX deviceStreamIndex (0=chain A, 1=chain B)")
    ap.add_argument("--tx-gain", type=int, default=20,
                    help="TX gain in dB (default 20 — gr4-lora cal: "
                         "P_source ≈ -33.85 dBm conducted)")
    args = ap.parse_args()

    variant = MOD_VARIANTS[args.mod]
    chan_type = cast(str, variant["channel_type"])
    settings_key = cast(str, variant["settings_key"])
    extra = cast(dict[str, Any], variant["extra"])

    # Verify ds[0] is USRP RX
    pre = get_devset_count()
    if pre < 1:
        sys.exit(f"[parity] need ds[0]=USRP RX already running, got count={pre}")

    print(f"[parity] mod={args.mod} channel_type={chan_type} "
          f"settings_key={settings_key} watch={args.watch}s "
          f"tx_stream_idx={args.tx_stream_idx} tx_gain={args.tx_gain}")

    ds_tx = -1
    try:
        # Add Tx deviceset
        c, b = req("POST", "/sdrangel/deviceset?direction=1")
        assert c == 202, f"add Tx ds: {c} {b}"
        assert wait_devset_count(pre + 1, timeout=10), "Tx ds never appeared"
        ds_tx = pre

        # USRP TX, buddy-share with ds[0]
        print(f"[parity] mounting USRP TX on ds[{ds_tx}] (buddy share)...")
        c, b = req("PUT", f"/sdrangel/deviceset/{ds_tx}/device",
                   {"hwType": "USRP", "direction": 1,
                    "deviceSequence": 0,
                    "deviceStreamIndex": args.tx_stream_idx})
        assert c // 100 == 2, f"PUT USRP: {c} {b}"

        c, b = req("PATCH", f"/sdrangel/deviceset/{ds_tx}/device/settings",
                   {"deviceHwType": "USRP", "direction": 1,
                    "usrpOutputSettings": {
                        "centerFrequency": CENTER_FREQ,
                        "devSampleRate": USRP_TX_RATE,
                        "log2SoftInterp": 0,
                        "antennaPath": ANTENNA,
                        "loOffset": 0,
                        "gain": args.tx_gain,
                        "masterClockRate": 0,
                    }})
        assert c // 100 == 2, f"PATCH USRP: {c} {b}"

        # Donor TX channel.  All variants share LoRa physical params.
        c, b = req("POST", f"/sdrangel/deviceset/{ds_tx}/channel",
                   {"channelType": chan_type, "direction": 1})
        assert c // 100 == 2, f"add {chan_type}: {c} {b}"
        time.sleep(0.7)

        body: dict[str, Any] = {
            "inputFrequencyOffset": 0,
            "bandwidthIndex": 18,    # 62.5 kHz
            "spreadFactor": 8,
            "deBits": 0,
            "nbParityBits": 4,
            "preambleChirps": 16,
            "syncWord": 0x12,
            "messageRepeat": 0,      # infinite repeat
            "channelMute": 0,
            "udpEnabled": 0,
        }
        body.update(extra)
        c, b = req("PATCH", f"/sdrangel/deviceset/{ds_tx}/channel/0/settings",
                   {"channelType": chan_type, "direction": 1,
                    settings_key: body})
        assert c // 100 == 2, f"PATCH {chan_type}: {c} {b}"

        # Run TX
        c, b = req("POST", f"/sdrangel/deviceset/{ds_tx}/device/run", {})
        assert c // 100 == 2, f"device run: {c} {b}"
        print(f"[parity] TX running. Watch SA at 869.618 MHz / 500 kHz span "
              f"/ RBW 3 kHz / single-sweep / marker.  TX for {args.watch}s...")
        time.sleep(args.watch)

        # Survival check
        post = get_devset_count()
        if post < 0:
            print(f"[parity] FAIL: sdrangel REST dead after {args.watch}s of "
                  f"{args.mod} TX -> architectural bug, donor crashes too.")
            return 2
        print(f"[parity] survival PASS: sdrangel alive after {args.watch}s of "
              f"{args.mod} TX (deviceset count={post}).")
        print(f"[parity] Was RF visible on SA marker at 869.618 MHz, "
              f"time-correlated with TX window?")
        print(f"  YES -> donor TX path OK for stock USRPOutput; M4 bug is "
              f"modmeshcore-specific.")
        print(f"  NO  -> render or RF chain regression; re-verify gr4-lora "
              f"baseline.")
        return 0

    finally:
        if ds_tx >= 0:
            req("POST", f"/sdrangel/deviceset/{ds_tx}/device/run/stop", {})
            time.sleep(0.7)
            req("DELETE", f"/sdrangel/deviceset/{ds_tx}")


if __name__ == "__main__":
    sys.exit(main())
