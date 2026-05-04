#!/usr/bin/env python3
"""Three-channel ground-truth bisection harness (M3 verifier).

Spawns deviceset 0 = USRP B210 with THREE channels:
  - MeshcoreDemod   (test article)         UDP 9999 JSON
  - ChirpChatDemod  (positive-control)     UDP 9998 raw bytes
  - FileSink        (IQ tap, full baseband) ./tmp/captures/onair_capture*

Stage A:  lora hwtest transmit --matrix basic   (Heltec ADVERTs)
Stage B:  meshcore_py TXT_MSG flurry            (longer payloads)

ChirpChat decode rate = ground-truth signal-availability oracle.
Mesh decode rate = test article. .sdriq capture = M1 replay material.
"""
from __future__ import annotations

import asyncio
import json
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_SDRANGEL = Path("/Users/tom/src/uhd/sdrangel")
REPO_GR4 = Path("/Users/tom/src/uhd/gr4-lora")
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
CAPTURE_DIR = PROJECT_ROOT / "tmp" / "captures"
RESULTS_DIR = HERE / "results"

BASE = "http://127.0.0.1:8091"
UDP_MESH = 9999
UDP_CHIRP = 9998
HELTEC_TCP = "10.0.23.152:5000"

# FileSink (sdriq) capture target.  Plugin appends timestamp suffix.
CAPTURE_BASENAME = CAPTURE_DIR / "onair_capture"

# Match handoff state — controlled by --decim CLI flag, default 3 (failing case).
FREQ_HZ = 869_618_000
SAMPLE_RATE = 1_000_000
LO_OFFSET = 62_500
RX_GAIN = 60
PREAMBLE_CHIRPS = 16
BANDWIDTH_INDEX = 18
SPREAD_FACTOR = 8
PARITY_BITS = 4

TXT_MSGS = [
    "ping",
    "longer test message",
    "this is a longer payload to exercise more codeword positions",
    "x" * 60,
    "the quick brown fox jumps over the lazy dog 0123456789",
]


def req(method: str, url: str, body: Any = None, timeout: float = 5.0) -> tuple[int, Any]:
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


def get_devicesets() -> dict[str, Any] | None:
    code, info = req("GET", BASE + "/sdrangel/devicesets")
    if code // 100 != 2:
        return None
    return info


def find_channel_indices() -> list[tuple[int, str]]:
    info = get_devicesets()
    if info is None:
        return []
    chans = info["deviceSets"][0].get("channels", []) or []
    return [(c["index"], str(c.get("id", ""))) for c in chans]


def remove_all_channels() -> None:
    while True:
        chans = find_channel_indices()
        if not chans:
            return
        idx, _id = chans[0]
        req("DELETE", BASE + f"/sdrangel/deviceset/0/channel/{idx}")
        time.sleep(0.7)


def add_channel(channel_type: str, settings_key: str, settings_body: dict[str, Any]) -> int:
    code, _ = req("POST", BASE + "/sdrangel/deviceset/0/channel",
                  {"channelType": channel_type, "direction": 0})
    assert code // 100 == 2, f"POST channel {channel_type} {code}"
    time.sleep(0.5)
    chans = find_channel_indices()
    matching = [i for i, cid in chans if cid == channel_type]
    assert matching, f"channel {channel_type} not found in {chans}"
    idx = matching[-1]
    code, body = req("PATCH", BASE + f"/sdrangel/deviceset/0/channel/{idx}/settings", {
        "channelType": channel_type, "direction": 0, settings_key: settings_body,
    })
    assert code // 100 == 2, f"PATCH channel {channel_type} {code} {body}"
    return idx


def configure_sdrangel(log2_soft_decim: int) -> dict[str, int]:
    req("DELETE", BASE + "/sdrangel/deviceset/0/device/run")
    time.sleep(0.7)
    remove_all_channels()

    code, _ = req("PUT", BASE + "/sdrangel/deviceset/0/device",
                  {"hwType": "USRP", "direction": 0})
    assert code // 100 == 2

    code, body = req("PATCH", BASE + "/sdrangel/deviceset/0/device/settings", {
        "deviceHwType": "USRP",
        "direction": 0,
        "usrpInputSettings": {
            "centerFrequency": FREQ_HZ,
            "devSampleRate": SAMPLE_RATE,
            "log2SoftDecim": log2_soft_decim,
            "antennaPath": "RX2",
            "loOffset": LO_OFFSET,
            "gain": RX_GAIN,
            "masterClockRate": 24_000_000,
        },
    })
    assert code // 100 == 2, f"PATCH device {code} {body}"

    indices: dict[str, int] = {}

    indices["mesh"] = add_channel("MeshcoreDemod", "MeshtasticDemodSettings", {
        "inputFrequencyOffset": LO_OFFSET,
        "bandwidthIndex": BANDWIDTH_INDEX,
        "spreadFactor": SPREAD_FACTOR,
        "deBits": 0,
        "decodeActive": 1,
        "nbParityBits": PARITY_BITS,
        "preambleChirps": PREAMBLE_CHIRPS,
        "sendViaUDP": 0,
        "sendJsonViaUDP": 1,
        "udpAddress": "127.0.0.1",
        "udpPort": UDP_MESH,
    })

    indices["chirp"] = add_channel("ChirpChatDemod", "ChirpChatDemodSettings", {
        "inputFrequencyOffset": LO_OFFSET,
        "bandwidthIndex": BANDWIDTH_INDEX,
        "spreadFactor": SPREAD_FACTOR,
        "deBits": 0,
        "decodeActive": 1,
        "nbParityBits": PARITY_BITS,
        "preambleChirps": PREAMBLE_CHIRPS,
        "codingScheme": 0,
        "sendViaUDP": 1,
        "udpAddress": "127.0.0.1",
        "udpPort": UDP_CHIRP,
    })

    # FileSink (sdriq IQ recorder, log2Decim=0 -> full 1 MS/s baseband).
    # NOTE: SigMFFileSink would need libsigmf (ENABLE_EXTERNAL_LIBRARIES=ON) which
    # we deliberately disable. Plain FileSink writes .sdriq, replayed via FileInput.
    indices["sigmf"] = add_channel("FileSink", "FileSinkSettings", {
        "inputFrequencyOffset": 0,
        "fileRecordName": str(CAPTURE_BASENAME) + ".sdriq",
        "log2Decim": 0,
        "spectrumSquelchMode": 0,
        "preRecordTime": 0,
        "squelchPostRecordTime": 0,
        "squelchRecordingEnable": 0,
    })

    code, _ = req("POST", BASE + "/sdrangel/deviceset/0/device/run", {})
    assert code // 100 == 2
    return indices


def start_recording(idx: int) -> None:
    code, body = req("POST", BASE + f"/sdrangel/deviceset/0/channel/{idx}/actions", {
        "channelType": "FileSink", "direction": 0,
        "FileSinkActions": {"record": 1},
    })
    print(f"  start_recording: code={code} body={body}", flush=True)


def stop_recording(idx: int) -> None:
    code, body = req("POST", BASE + f"/sdrangel/deviceset/0/channel/{idx}/actions", {
        "channelType": "FileSink", "direction": 0,
        "FileSinkActions": {"record": 0},
    })
    print(f"  stop_recording: code={code} body={body}", flush=True)


def teardown() -> None:
    req("DELETE", BASE + "/sdrangel/deviceset/0/device/run")
    time.sleep(0.7)
    remove_all_channels()


# ---- listeners --------------------------------------------------------------

class JsonListener:
    def __init__(self, port: int, label: str) -> None:
        self.port, self.label = port, label
        self.frames: list[dict[str, Any]] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        sock.settimeout(0.3)
        while not self.stop.is_set():
            rd, _, _ = select.select([sock], [], [], 0.3)
            if not rd:
                continue
            try:
                data, _ = sock.recvfrom(65536)
            except socket.timeout:
                continue
            try:
                obj = json.loads(data.decode("utf-8", "replace"))
                self.frames.append(obj)
                lora = obj.get("lora", {})
                rf = obj.get("rf", {})
                sys.stdout.write(
                    f"[{self.label} {len(self.frames):3d}] hdr={lora.get('header_crc','?'):<3} "
                    f"pay={lora.get('payload_crc','?'):<3} "
                    f"len={lora.get('packet_length','?')!s:>3} "
                    f"sync={lora.get('sync_word','?')!s:<6} "
                    f"sig={rf.get('signal_db','?')!s:<7} "
                    f"hex={lora.get('payload_hex','')[:40]}\n"
                )
            except json.JSONDecodeError:
                # raw bytes (ChirpChat sendViaUDP). Count it.
                self.frames.append({"raw_len": len(data), "raw": data.hex()[:80]})
                sys.stdout.write(f"[{self.label} {len(self.frames):3d}] raw_bytes={len(data)} hex={data.hex()[:40]}\n")
            sys.stdout.flush()
        sock.close()

    def __enter__(self) -> "JsonListener":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop.set()
        self.thread.join(timeout=2.0)


# ---- stage drivers (same as onair_burst.py) --------------------------------

def stage_a_transmit() -> int:
    print(f"\n[Stage A] lora hwtest transmit --matrix basic --tcp {HELTEC_TCP}", flush=True)
    cmd = ["uv", "run", "--active", "lora", "hwtest", "transmit",
           "--matrix", "basic", "--tcp", HELTEC_TCP,
           "--label", "onair-three-channel"]
    res = subprocess.run(cmd, cwd=REPO_GR4, capture_output=True, text=True, timeout=120)
    sys.stdout.write(res.stdout)
    sys.stderr.write(res.stderr)
    return res.returncode


async def stage_b_txt_msg_flurry() -> int:
    print("\n[Stage B] meshcore TXT_MSG flurry at SF=8/BW=62.5k", flush=True)
    sys.path.insert(0, str(REPO_GR4 / ".venv/lib/python3.13/site-packages"))
    try:
        from meshcore import MeshCore
    except ImportError as e:
        print(f"  meshcore lib import failed: {e}", flush=True)
        return 1

    host, port_s = HELTEC_TCP.split(":")
    port = int(port_s)
    mc = await MeshCore.create_tcp(host=host, port=port)
    await mc.ensure_contacts()

    print("  set_radio: 869.618 MHz, BW=62.5 kHz, SF=8, CR=8", flush=True)
    res = await mc.commands.set_radio(FREQ_HZ / 1e6, 62.5, SPREAD_FACTOR, 8)
    print(f"  set_radio: {res}", flush=True)
    await asyncio.sleep(2.0)

    sent = 0
    for i, msg in enumerate(TXT_MSGS):
        contacts = list(mc.contacts.values()) if hasattr(mc, "contacts") else []
        if not contacts:
            print(f"  [{i+1}] no contacts; broadcast advert", flush=True)
            await mc.commands.send_advert()
        else:
            dest = contacts[0]
            dest_name = (dest.get("adv_name") if isinstance(dest, dict)
                         else getattr(dest, "adv_name", "?"))
            print(f"  [{i+1}] TXT_MSG ({len(msg)} chars) -> {dest_name}", flush=True)
            try:
                await mc.commands.send_msg(dest, msg)
            except Exception as e:
                print(f"     send_msg failed: {e}; fallback advert", flush=True)
                await mc.commands.send_advert()
        sent += 1
        await asyncio.sleep(2.5)

    for k in range(10):
        await mc.commands.send_advert()
        sent += 1
        await asyncio.sleep(1.5)
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--decim", type=int, default=3, choices=[2, 3],
                    help="LOG2_SOFT_DECIM (2=>250kS/s distance=1.0; 3=>125kS/s distance=0.5)")
    ap.add_argument("--no-capture", action="store_true",
                    help="skip SigMFFileSink record action (channel still added)")
    args = ap.parse_args()

    if not REPO_GR4.exists():
        print("ERROR: gr4-lora not found", file=sys.stderr)
        return 2

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[config] decim={args.decim} mesh:9999 chirp:9998 capture={CAPTURE_BASENAME}", flush=True)
    indices = configure_sdrangel(log2_soft_decim=args.decim)
    print(f"[config] channels: {indices}", flush=True)
    time.sleep(2.0)

    if not args.no_capture:
        start_recording(indices["sigmf"])

    mesh = JsonListener(UDP_MESH, "mesh")
    chirp = JsonListener(UDP_CHIRP, "chirp")
    with mesh, chirp:
        time.sleep(2.0)
        rc_a = stage_a_transmit()
        time.sleep(2.0)
        try:
            rc_b = asyncio.run(stage_b_txt_msg_flurry())
        except Exception as e:
            print(f"  stage B exception: {e}", flush=True)
            rc_b = 1
        time.sleep(3.0)

    if not args.no_capture:
        stop_recording(indices["sigmf"])
        time.sleep(0.5)

    teardown()

    def hdr_ok(fs):
        return sum(1 for f in fs if f.get("lora", {}).get("header_crc") == "ok")
    def pay_ok(fs):
        return sum(1 for f in fs if f.get("lora", {}).get("payload_crc") == "ok")

    n_m, n_c = len(mesh.frames), len(chirp.frames)
    m_hdr, m_pay = hdr_ok(mesh.frames), pay_ok(mesh.frames)
    print()
    print(f"=== three-channel summary (decim={args.decim}) ===")
    print(f"  mesh frames_total:   {n_m}")
    print(f"  mesh header_crc_ok:  {m_hdr}")
    print(f"  mesh payload_crc_ok: {m_pay}")
    print(f"  chirp frames_total:  {n_c}  (positive control)")

    out = RESULTS_DIR / f"results_three_channel_decim{args.decim}.json"
    out.write_text(json.dumps({
        "decim": args.decim,
        "mesh_total": n_m, "mesh_hdr_ok": m_hdr, "mesh_pay_ok": m_pay,
        "chirp_total": n_c,
        "mesh_frames": mesh.frames,
        "chirp_frames": chirp.frames,
        "rc_a": rc_a, "rc_b": rc_b,
    }, indent=2))
    print(f"  out: {out}")

    # FileSink writes timestamped files: <basename>.<yyyy-MM-ddTHH_mm_ss_zzz>.sdriq
    sdriqs = sorted(CAPTURE_DIR.glob(f"{CAPTURE_BASENAME.name}*.sdriq"))
    if sdriqs:
        for f in sdriqs[-3:]:
            print(f"  sdriq: {f.name}  {f.stat().st_size}B")
    else:
        print("  sdriq: NOT WRITTEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
