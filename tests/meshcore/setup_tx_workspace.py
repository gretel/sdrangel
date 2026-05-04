#!/usr/bin/env python3
"""Bring up an SDRangel TX workspace for MeshCore Phase 4b prep.

Mirrors `setup_workspace.py` but for the TX side: switches
deviceset 0 to a FileOutput device, adds a MeshcoreMod channel,
PATCHes a MESHCORE: command into `textMessage` (which triggers
encoder via `Packet::buildFrameFromCommand`), runs the device for
a short window to flush the modulated frame to disk, then stops.

The resulting .sdriq can be replayed via setup_workspace.py
(--source file --file ...) to verify TX -> RX round-trip without
any RF hardware. For Phase 4b on-air, point an actual sink at
the same channel settings instead.

Usage:
    python3 setup_tx_workspace.py --output tmp/tx_advert.sdriq \\
        --command 'MESHCORE:type=advert; name=sdr-test'

Reset state with `--clean` (removes any prior meshcore channels
on deviceset 0 before re-adding).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8091"
FREQ_HZ_DEFAULT = 869_618_000
HW_TYPE = "FileOutput"
DE_BITS = 0
MESSAGE_REPEAT_DEFAULT = 1
TX_RUN_SECONDS = 2.0

# Per-channel-type profile. `sampleRate` is the mod source / channel rate
# (= oversampling * bandwidth = 4 * BW for both donor and clone). Setting
# anything else corrupts the TX pipeline.
PROFILES = {
    "meshcore": {
        "channel_type": "MeshcoreMod",
        "settings_key": "MeshtasticModSettings",  # donor SWG schema (alias)
        "sample_rate": 250_000,                   # 4 * 62500
        "bandwidth_index": 18,                    # 62.5 kHz (MeshCore EU)
        "spread_factor": 8,
        "parity_bits": 4,                         # CR=4/8
        "preamble_chirps": 16,                    # MeshCore EU standard (B6.18)
        "sync_word": 0x12,
        "default_command": (
            "MESHCORE:type=advert; "
            "name=sdr-test; "
            "seed=01010101010101010101010101010101"
            "01010101010101010101010101010101"
        ),
    },
    "meshtastic": {
        "channel_type": "MeshtasticMod",
        "settings_key": "MeshtasticModSettings",
        "sample_rate": 12_500,                    # 4 * 3125 (BW idx 5)
        "bandwidth_index": 5,                     # 3.125 kHz (donor default)
        "spread_factor": 7,                       # donor default
        "parity_bits": 1,                         # CR=4/5 default
        "preamble_chirps": 16,                    # Meshtastic default
        "sync_word": 0x34,
        # No MESHCORE: prefix => encoder takes UTF-8 bytes raw.
        "default_command": "donor-bisect",
    },
    # Pure-PHY underlying plugin. ChirpChat is the original LoRa
    # mod/demod sdrangel ships; meshtastic + meshcore both clone it.
    # Use this to round-trip-bisect: if ChirpChatMod -> demodmeshcore
    # decodes, our meshcore-specific code (settings defaults, frame
    # format, sync-word emission, etc.) introduced the gap.
    "chirpchat": {
        "channel_type": "ChirpChatMod",
        "settings_key": "ChirpChatModSettings",
        "sample_rate": 250_000,                   # match meshcore params for direct compare
        "bandwidth_index": 18,                    # 62.5 kHz
        "spread_factor": 8,
        "parity_bits": 4,                         # CR=4/8
        "preamble_chirps": 8,
        "sync_word": 0x12,
        "default_command": "ChirpChat-bisect",
    },
}
LOG2_INTERP_DEFAULT = 0


def req(method: str, url: str, body=None, timeout: float = 5.0):
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


def step(label: str, code: int, body=None):
    ok = code // 100 == 2
    marker = "ok " if ok else "FAIL"
    print(f"  [{marker} {code}] {label}")
    if not ok and body:
        print(f"        {body}")
    if not ok:
        sys.exit(1)


def find_channel_index_by_prefix(base: str, ds_idx: int, prefix: str) -> int | None:
    code, info = req("GET", base + "/sdrangel/devicesets")
    if code // 100 != 2 or info is None:
        return None
    devsets = info.get("deviceSets", [])
    if ds_idx >= len(devsets):
        return None
    chans = devsets[ds_idx].get("channels", []) or []
    for ch in chans:
        if str(ch.get("id", "")).lower().startswith(prefix.lower()):
            return ch["index"]
    return None


def main():
    p = argparse.ArgumentParser(description="Bring up an SDRangel TX workspace.")
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--output", required=True,
                   help="output .sdriq path (FileOutput device fileName)")
    p.add_argument("--channel-type", choices=tuple(PROFILES.keys()),
                   default="meshcore",
                   help="channel plugin: meshcore (default) or meshtastic donor")
    p.add_argument("--command", default=None,
                   help="textMessage value (default: profile-specific)")
    p.add_argument("--freq", type=int, default=FREQ_HZ_DEFAULT)
    p.add_argument("--sample-rate", type=int, default=None,
                   help="override mod source rate (default: profile)")
    p.add_argument("--log2-interp", type=int, default=LOG2_INTERP_DEFAULT)
    p.add_argument("--message-repeat", type=int, default=MESSAGE_REPEAT_DEFAULT)
    p.add_argument("--run-seconds", type=float, default=TX_RUN_SECONDS,
                   help="how long device/run stays active before stop")
    p.add_argument("--keep", action="store_true",
                   help="leave the new TX deviceset in place after run "
                        "(default: DELETE it via /sdrangel/deviceset)")
    args = p.parse_args()

    profile = PROFILES[args.channel_type]
    sample_rate = int(args.sample_rate if args.sample_rate is not None else profile["sample_rate"])
    command = str(args.command if args.command is not None else profile["default_command"])
    chan_type = str(profile["channel_type"])
    settings_key = str(profile["settings_key"])
    bandwidth_index = int(profile["bandwidth_index"])
    spread_factor = int(profile["spread_factor"])
    parity_bits = int(profile["parity_bits"])
    preamble_chirps = int(profile["preamble_chirps"])
    sync_word = int(profile["sync_word"])

    print(f"== sdrangel @ {args.base} "
          f"(TX, {chan_type} -> FileOutput -> {args.output}) ==")
    code, info = req("GET", args.base + "/sdrangel")
    if code // 100 != 2 or not isinstance(info, dict):
        print(f"ERROR: cannot reach SDRangel at {args.base} (HTTP {code})")
        sys.exit(2)
    pre_count = info["devicesetlist"]["devicesetcount"]
    print(f"  devicesets pre-add: {pre_count}")

    print("\n== add TX deviceset ==")
    code, body = req("POST", args.base + "/sdrangel/deviceset?direction=1")
    step("POST /sdrangel/deviceset?direction=1", code, body)
    ds_idx = pre_count
    time.sleep(0.3)
    print(f"  TX deviceset index: {ds_idx}")

    print("\n== device ==")
    code, _ = req("PUT", args.base + f"/sdrangel/deviceset/{ds_idx}/device",
                  {"hwType": HW_TYPE, "direction": 1})
    step(f"PUT device hwType={HW_TYPE} direction=1 (TX)", code)

    dev_settings = {
        "deviceHwType": HW_TYPE,
        "direction": 1,
        "fileOutputSettings": {
            "fileName": args.output,
            "centerFrequency": args.freq,
            "sampleRate": sample_rate,
            "log2Interp": args.log2_interp,
        },
    }
    code, body = req(
        "PATCH",
        args.base + f"/sdrangel/deviceset/{ds_idx}/device/settings",
        dev_settings,
    )
    step(f"PATCH device settings (file={args.output}, "
         f"sr={sample_rate} Hz, log2Interp={args.log2_interp})",
         code, body)

    print("\n== channel ==")
    code, _ = req("POST", args.base + f"/sdrangel/deviceset/{ds_idx}/channel",
                  {"channelType": chan_type, "direction": 1})
    step(f"POST {chan_type} channel", code)
    time.sleep(0.5)

    # Channel id prefixes are derived from chan_type lowercase
    # (e.g. "MeshcoreMod" -> "meshcore", "MeshtasticMod" -> "meshtastic").
    prefix = chan_type.replace("Mod", "").lower()
    idx = find_channel_index_by_prefix(args.base, ds_idx, prefix)
    if idx is None:
        print(f"ERROR: {chan_type} channel not found after POST in deviceset {ds_idx}")
        sys.exit(3)
    print(f"  channel index: {idx}")

    chan_settings = {
        "channelType": chan_type,
        "direction": 1,
        settings_key: {
            "inputFrequencyOffset": 0,
            "bandwidthIndex": bandwidth_index,
            "spreadFactor": spread_factor,
            "deBits": DE_BITS,
            "nbParityBits": parity_bits,
            "preambleChirps": preamble_chirps,
            "syncWord": sync_word,
            "channelMute": 0,
            "messageRepeat": args.message_repeat,
            "textMessage": command,
            "udpEnabled": 0,
        },
    }
    code, body = req(
        "PATCH",
        args.base + f"/sdrangel/deviceset/{ds_idx}/channel/{idx}/settings",
        chan_settings,
    )
    step(f"PATCH channel settings (BW idx {bandwidth_index}, SF{spread_factor}, "
         f"CR=4/{4+parity_bits}, sync=0x{sync_word:02x}, "
         f"preamble={preamble_chirps}, repeat={args.message_repeat})",
         code, body)
    print(f"  textMessage={command!r}")

    print("\n== run ==")
    code, _ = req("POST", args.base + f"/sdrangel/deviceset/{ds_idx}/device/run", {})
    step("POST device/run", code)

    print(f"\n  TX active for {args.run_seconds:.1f} s ...")
    time.sleep(args.run_seconds)

    code, _ = req("DELETE", args.base + f"/sdrangel/deviceset/{ds_idx}/device/run")
    step("DELETE device/run", code)

    import os
    if os.path.exists(args.output):
        size = os.path.getsize(args.output)
        print(f"\nwrote {args.output} ({size} bytes)")
    else:
        print(f"\nWARNING: {args.output} not present after TX run")

    if not args.keep:
        # DELETE /sdrangel/deviceset removes the LAST deviceset only.
        # Our new TX one is the last, so this works as long as nobody
        # added another after us.
        time.sleep(0.3)
        code, _ = req("DELETE", args.base + "/sdrangel/deviceset")
        step("DELETE last deviceset (cleanup our TX one)", code)


if __name__ == "__main__":
    main()
