#!/usr/bin/env python3
"""B210 sendNow TX matrix — find a configuration where sdrangel can
emit a MeshCore ADVERT through the LibreSDR_B220mini (B210 clone) and
have it land on a Heltec companion while an RX deviceset is live (or
stopped, in serial topologies).

Both candidate sample-sink plugins are tested:

  * USRPOutput     — UHD direct
  * SoapySDROutput — UHD via SoapySDR (requires sdrangel `--soapy` flag)

Each REST device list shows B210 enumerated twice per plugin:
  `[0:0]` = channel A:A,  `[0:1]` = channel A:B
  (notation = `[deviceSequence:deviceStreamIndex]`).

Airtime cap: at most one ADVERT per combo (messageRepeat=1, single
sendNow REST action).

Combo phases:

  a   SoapySDR plugin (parallel-plugin reference)
  c   serial RX/TX (TX-only, no concurrent RX)
  d   USRP plugin baseline (RX+TX buddy-share)
  m   mixed-plugin sanity (USRP RX + Soapy TX, etc.) — expect failure
      because cross-plugin buddy share is not architecturally supported.

Run individual phase or single combo:
  python3 tests/meshcore/run_tx_matrix.py --phase a
  python3 tests/meshcore/run_tx_matrix.py --combo a1
  python3 tests/meshcore/run_tx_matrix.py --smoke    # spawn+enumerate only
  python3 tests/meshcore/run_tx_matrix.py            # all combos

Outputs land in tests/meshcore/results/  (non-temporary, persisted):
  smoke.log, <combo>.log    — full sdrangel stdout per run
  results_<phase>.json      — aggregated combo results per phase
  matrix_full_run.json      — default aggregate when --phase / --combo unset
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

_LIB_PATH = Path(__file__).resolve().parent / "lib_sdrangel.py"
_OPT_PATH = Path(__file__).resolve().parent / "lib_sdrangel_options.py"
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("lib_sdrangel", _LIB_PATH)
assert _spec and _spec.loader, f"could not load {_LIB_PATH}"
sdr = importlib.util.module_from_spec(_spec)
sys.modules["lib_sdrangel"] = sdr  # required so @dataclass can resolve __module__
_spec.loader.exec_module(sdr)

_optspec = importlib.util.spec_from_file_location(
    "lib_sdrangel_options", _OPT_PATH)
assert _optspec and _optspec.loader, f"could not load {_OPT_PATH}"
opt = importlib.util.module_from_spec(_optspec)
sys.modules["lib_sdrangel_options"] = opt
_optspec.loader.exec_module(opt)


# ---------------------------------------------------------------------------
# Config — single source of truth is lib_sdrangel_options.HarnessOptions.
# Edit values there to test variants.  Anything not exposed by HarnessOptions
# yet (Soapy-side fields, companion endpoint) lives below as named constants.
# ---------------------------------------------------------------------------

# Default option set — B6.11.0 user-constraint baseline (RX 40, TX 20,
# external clock, TX 250ksps + log2SoftInterp=0, BW 62500, RX 1 MS/s).
# Per-run env overrides (B6.17 self-decode oracle): user reports 40 dB
# TX proven for next-room, but default stays at safer baseline so a
# stray run doesn't blast the front-end.
def _opts_from_env():
    o = opt.HarnessOptions()
    if (v := os.environ.get("SDRANGEL_TX_GAIN")):
        o.tx_gain = int(v)
    if (v := os.environ.get("SDRANGEL_RX_GAIN")):
        o.rx_gain = int(v)
    return o


OPTS = _opts_from_env()

# Soapy-side fields not covered by HarnessOptions (kept here for visibility;
# fold into options module if Soapy-plugin work resumes in B6.12).
SOAPY_RX_ANTENNA = "RX2"
SOAPY_TX_ANTENNA = "TX/RX"

# Companion + tooling endpoints.
COMPANION = ("10.0.23.152", 5000)
MESHCORE_CLI = "/Users/tom/src/uhd/meshcore-cli/.venv/bin/meshcore-cli"
IDENTITY_PATH = ("/Users/tom/Library/Application Support/"
                 "f4exb/SDRangel/meshcore/identity.bin")
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Persistent results — non-temporary. Per-combo .log + per-phase JSON.
RESULTS_DIR = Path(__file__).resolve().parent / "results"
LOG_PATTERNS = (
    "USRPOutput|USRPInput|SoapySDROutput|SoapySDRInput|UHD|"
    "libusb|libc|terminate|exception|underrun|overrun|"
    "locked|actual TX|master_clock|first.{0,5}S|"
    "LIBUSB_ERROR|FIFO|sentSamples|writtenSamples|"
    "MeshcoreMod|sendNow|encoder"
)


# ---------------------------------------------------------------------------
# Combo definition
# ---------------------------------------------------------------------------


@dataclass
class Combo:
    name: str
    rx_plugin: str | None      # None for serial / TX-only
    rx_idx: int | None
    tx_plugin: str
    tx_idx: int
    phase: str = "a"
    note: str = ""


def all_combos() -> list[Combo]:
    """Build the matrix.  Execution order a -> c -> d plus mixed sanity."""
    out: list[Combo] = []

    # Phase a: SoapySDR plugin reference.
    out += [
        Combo("a1", "SoapySDR", 0, "SoapySDR", 1, phase="a",
              note="diff chain (canonical buddy-share like USRP)"),
        Combo("a2", "SoapySDR", 0, "SoapySDR", 0, phase="a",
              note="same chain — likely conflict but cheap to test"),
        Combo("a3", "SoapySDR", 1, "SoapySDR", 0, phase="a",
              note="diff chain swap"),
        Combo("a4", "SoapySDR", 1, "SoapySDR", 1, phase="a",
              note="same chain swap — likely conflict"),
    ]

    # Phase c: serial RX/TX (TX-only, no concurrent RX).
    out += [
        Combo("c1", None, None, "USRP", 0, phase="c",
              note="serial USRP TX-only chain 0"),
        Combo("c2", None, None, "USRP", 1, phase="c",
              note="serial USRP TX-only chain 1"),
        Combo("c3", None, None, "SoapySDR", 0, phase="c",
              note="serial Soapy TX-only chain 0"),
        Combo("c4", None, None, "SoapySDR", 1, phase="c",
              note="serial Soapy TX-only chain 1"),
    ]

    # Phase d: USRP buddy-share (RX+TX both USRP plugin).
    out += [
        Combo("d1", "USRP", 0, "USRP", 1, phase="d",
              note="USRP RX[0] + TX[1] buddy-share"),
        Combo("d2", "USRP", 1, "USRP", 0, phase="d"),
        Combo("d3", "USRP", 0, "USRP", 0, phase="d"),
        Combo("d4", "USRP", 1, "USRP", 1, phase="d"),
    ]

    # Mixed-plugin sanity (cross-plugin buddy share — expect failure).
    out += [
        Combo("m1", "USRP", 0, "SoapySDR", 1, phase="m",
              note="USRP RX + Soapy TX cross-plugin (sanity)"),
        Combo("m2", "SoapySDR", 0, "USRP", 1, phase="m",
              note="Soapy RX + USRP TX cross-plugin (sanity)"),
    ]

    return out


# ---------------------------------------------------------------------------
# Settings body builders
# ---------------------------------------------------------------------------


# All USRP / MeshCore body builders delegate to HarnessOptions.  The
# named knobs there are the single source of truth — to test variants,
# edit lib_sdrangel_options.HarnessOptions defaults or pass overrides
# at construction time.
def usrp_input_settings() -> dict[str, Any]:
    return OPTS.usrp_input_body()


def usrp_output_settings() -> dict[str, Any]:
    return OPTS.usrp_output_body()


def meshcore_demod_settings() -> dict[str, Any]:
    return OPTS.meshcore_demod_body()


def meshcore_mod_settings(text: str) -> dict[str, Any]:
    return OPTS.meshcore_mod_body(text)


# Soapy bodies: not yet covered by HarnessOptions (B6.12 work).  Pull
# numeric values via OPTS so RX/TX sample rates, gains, BW track the
# central baseline; the SoapySDR-specific knobs (auto*, LOppmTenths)
# stay inline until SoapySDROutput injectBurst lands.
def soapy_input_settings() -> dict[str, Any]:
    return {
        "centerFrequency": OPTS.center_freq_hz,
        "devSampleRate": OPTS.rx_dev_sample_rate,
        "log2Decim": OPTS.rx_log2_soft_decim,
        "antenna": SOAPY_RX_ANTENNA,
        "globalGain": OPTS.rx_gain,
        "autoGain": 0,
        "autoDCCorrection": 1,
        "autoIQCorrection": 1,
        "LOppmTenths": 0,
        "bandwidth": int(OPTS.rx_dev_sample_rate * 0.8),
    }


def soapy_output_settings() -> dict[str, Any]:
    return {
        "centerFrequency": OPTS.center_freq_hz,
        "devSampleRate": OPTS.tx_dev_sample_rate,
        "log2Interp": OPTS.tx_log2_soft_interp,
        "antenna": SOAPY_TX_ANTENNA,
        "globalGain": OPTS.tx_gain,
        "autoGain": 0,
        "autoDCCorrection": 0,
        "autoIQCorrection": 0,
        "LOppmTenths": 0,
        "bandwidth": int(OPTS.tx_dev_sample_rate * 0.8),
    }


def settings_for(plugin: str, direction: int) -> tuple[str, dict[str, Any]]:
    """Map (plugin, direction) -> (SWG settings key, body)."""
    if plugin == "USRP":
        if direction == 0:
            return "usrpInputSettings", usrp_input_settings()
        return "usrpOutputSettings", usrp_output_settings()
    if plugin == "SoapySDR":
        # JSON key is 'soapySDRInputSettings' / 'soapySDROutputSettings'
        # — uppercase D-R per SWGDeviceSettings.cpp:478 setValue lookup
        # of pJson["soapySDRInputSettings"].  The C++ accessor uses
        # camelCase (getSoapySdrInputSettings) but the WIRE JSON key
        # does not.  Don't trust the getter casing here.
        if direction == 0:
            return "soapySDRInputSettings", soapy_input_settings()
        return "soapySDROutputSettings", soapy_output_settings()
    raise ValueError(f"unknown plugin: {plugin}")


# ---------------------------------------------------------------------------
# Identity + companion
# ---------------------------------------------------------------------------


def read_identity_pubkey() -> str:
    p = Path(IDENTITY_PATH)
    if not p.exists():
        raise FileNotFoundError(f"identity bin missing: {p}")
    raw = p.read_bytes()
    if len(raw) < 64:
        raise ValueError(f"identity too short: {len(raw)} bytes")
    return raw[32:64].hex()


def companion_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


def companion_contacts(host: str, port: int,
                       timeout: float = 15.0) -> dict[str, Any]:
    cmd = [MESHCORE_CLI, "-t", host, "-p", str(port), "-j", "-q", "contacts"]
    out = subprocess.check_output(cmd, timeout=timeout).decode()
    return json.loads(out)


# ---------------------------------------------------------------------------
# Per-combo execution
# ---------------------------------------------------------------------------


def setup_rx_deviceset(combo: Combo) -> int:
    """Mount the RX side per combo.  Returns deviceset index (0)."""
    assert combo.rx_plugin is not None and combo.rx_idx is not None
    ds = sdr.add_deviceset(direction=0)
    sdr.put_device(ds, combo.rx_plugin, direction=0,
                   stream_index=combo.rx_idx)
    skey, sbody = settings_for(combo.rx_plugin, direction=0)
    sdr.patch_device_settings(ds, skey, sbody, combo.rx_plugin, direction=0)
    sdr.add_channel(ds, "MeshcoreDemod", "MeshtasticDemodSettings",
                    meshcore_demod_settings(), direction=0)
    sdr.device_run(ds)
    return ds


def setup_tx_deviceset(combo: Combo, ds_index: int) -> int:
    """Mount the TX side and load the MeshcoreMod with the advert text."""
    ds = sdr.add_deviceset(direction=1)
    sdr.put_device(ds, combo.tx_plugin, direction=1,
                   stream_index=combo.tx_idx)
    skey, sbody = settings_for(combo.tx_plugin, direction=1)
    sdr.patch_device_settings(ds, skey, sbody, combo.tx_plugin, direction=1)
    text_advert = build_advert_text()
    sdr.add_channel(ds, "MeshcoreMod", "MeshtasticModSettings",
                    meshcore_mod_settings(text_advert), direction=1)
    return ds


def build_advert_text() -> str:
    pubkey = read_identity_pubkey()
    short = pubkey[:8]
    name = f"SDRangel-{short}"
    return f"MESHCORE:type=advert; name={name}; seed={pubkey}"


# ---------------------------------------------------------------------------
# Step-by-step run with per-step crash/error classification
# ---------------------------------------------------------------------------
#
# Outcome taxonomy (mutually exclusive):
#
#   PASS                        — companion advert advanced for our pubkey
#   PARTIAL_PUBKEY_OLD          — pubkey known to companion but not advanced
#   PARTIAL_NO_REACH            — encoder fired, companion didn't register
#   FAIL_NO_ENCODE              — encoder never fired but harness ran clean
#   CRASH_DURING_<STEP>         — sdrangel terminated during that step
#   CRASH_BEFORE_<STEP>         — sdrangel was already dead when step started
#   ERR_<STEP>                  — REST/4xx/timeout but sdrangel still alive
#   COMPANION_UNREACHABLE       — couldn't reach Heltec to query baseline
#   HARNESS_ERROR_SPAWN         — couldn't even start sdrangel
#
# Each combo result also carries:
#
#   last_step_completed         — name of last step that finished cleanly
#   encoder_fired_count         — # of MeshcoreModEncoder::encode lines in log
#   pubkey_in_companion         — pubkey present in companion's contact list
#   pubkey_advanced             — pubkey's last_advert > pre_advert (the test)
#   steps                       — list of {name, duration_s, alive_after, err}


EVIDENCE_KEYS = (
    "usrpoutput", "soapysdr", "uhd", "libusb", "terminate", "underrun",
    "overrun", "actual tx", "master_clock", "lockedauto", "libusb_error",
    "exception", "meshcoremod", "sendnow", "ssss", "could not set",
    "device mismatch", "stream", "asking for clock", "antenna",
)


@dataclass
class StepResult:
    name: str
    duration_s: float
    alive_after: bool
    error: str | None = None


def run_step(res: dict[str, Any], sdrproc: Any,
             name: str, fn: Any, *,
             expect_alive_after: bool = True) -> tuple[bool, Any]:
    """Run a step with crash/error classification.

    Returns (ok, value).  ok is True on success.
    On failure, populates res['outcome'] + res['error'] and returns (False, None).
    """
    if res["outcome"] != "_RUNNING":
        return False, None
    if not sdrproc.alive():
        res["outcome"] = f"CRASH_BEFORE_{name.upper()}"
        res["error"] = "sdrangel was already dead at step entry"
        res["steps"].append(asdict(StepResult(
            name, 0.0, False, "alive=False before step")))
        return False, None
    t0 = time.time()
    try:
        ret = fn()
        dur = round(time.time() - t0, 2)
    except Exception as e:
        dur = round(time.time() - t0, 2)
        # Drain process briefly to settle exit status, then check alive.
        time.sleep(0.4)
        alive = sdrproc.alive()
        if not alive:
            res["outcome"] = f"CRASH_DURING_{name.upper()}"
        else:
            res["outcome"] = f"ERR_{name.upper()}"
        res["error"] = f"{name}: {e}"
        res["steps"].append(asdict(StepResult(name, dur, alive, str(e))))
        return False, None
    alive = sdrproc.alive()
    res["steps"].append(asdict(StepResult(name, dur, alive, None)))
    if expect_alive_after and not alive:
        res["outcome"] = f"CRASH_DURING_{name.upper()}"
        res["error"] = f"sdrangel terminated immediately after {name}"
        return False, ret
    res["last_step_completed"] = name
    return True, ret


def finalize_combo(res: dict[str, Any], sdrproc: Any,
                   started: float, pubkey: str, pre_advert: int) -> dict[str, Any]:
    """Run after combo body regardless of outcome.  Captures evidence,
    queries companion, kills sdrangel, picks final outcome."""
    tail = sdrproc.tail_log(n_bytes=64_000)
    encode_lines = [ln for ln in tail.splitlines()
                    if "MeshcoreModEncoder::encode" in ln]
    res["encoder_fired_count"] = len(encode_lines)
    res["evidence"] = [ln for ln in tail.splitlines()
                       if any(k in ln.lower() for k in EVIDENCE_KEYS)][-80:]

    # Always check companion (even on crash) — encoder may have emitted
    # successfully BEFORE the crash and the advert reached the air.
    try:
        post = companion_contacts(*COMPANION, timeout=20)
        if pubkey in post:
            res["pubkey_in_companion"] = True
            post_advert = post[pubkey].get("last_advert", 0)
            res["post_advert"] = post_advert
            res["pre_advert"] = pre_advert
            res["adv_name"] = post[pubkey].get("adv_name", "")
            if post_advert > pre_advert:
                res["pubkey_advanced"] = True
        else:
            res["pubkey_in_companion"] = False
    except Exception as e:
        res["companion_post_query_error"] = str(e)

    # If outcome was still running (i.e. all steps completed cleanly), pick
    # final based on companion + encoder evidence.
    if res["outcome"] == "_RUNNING":
        if res["pubkey_advanced"]:
            res["outcome"] = "PASS"
        elif res["pubkey_in_companion"]:
            res["outcome"] = "PARTIAL_PUBKEY_OLD"
        elif res["encoder_fired_count"] > 0:
            res["outcome"] = "PARTIAL_NO_REACH"
        else:
            res["outcome"] = "FAIL_NO_ENCODE"
    else:
        # If a CRASH/ERR was recorded but the advert STILL got through,
        # promote to a PASS-with-crash variant for visibility.
        if res["pubkey_advanced"]:
            res["outcome_alt"] = res["outcome"]
            res["outcome"] = "PASS_THEN_CRASH"

    try:
        sdrproc.kill(grace=2.0)
    except Exception:
        pass
    time.sleep(0.4)
    sdr.kill_stale_sdrangel()
    res["duration_s"] = round(time.time() - started, 1)
    print(f"  outcome={res['outcome']} dur={res['duration_s']}s "
          f"steps={len(res['steps'])} encoder_fired={res['encoder_fired_count']} "
          f"in_companion={res['pubkey_in_companion']} "
          f"advanced={res['pubkey_advanced']} "
          f"err={(res['error'] or '')[:80]}")
    return res


def run_combo(combo: Combo, args: argparse.Namespace) -> dict[str, Any]:
    """Execute one combo end-to-end with per-step classification."""
    print(f"\n========== {combo.name} ({combo.phase}) ==========")
    print(f"  rx={combo.rx_plugin}[{combo.rx_idx}] tx={combo.tx_plugin}"
          f"[{combo.tx_idx}]")
    if combo.note:
        print(f"  note: {combo.note}")

    res: dict[str, Any] = {
        "combo": asdict(combo),
        "phase": combo.phase,
        "outcome": "_RUNNING",
        "error": "",
        "evidence": [],
        "steps": [],
        "last_step_completed": "(none)",
        "encoder_fired_count": 0,
        "pubkey_in_companion": False,
        "pubkey_advanced": False,
        "pre_advert": 0,
        "started_at": time.time(),
        "duration_s": 0.0,
    }
    started = time.time()
    pubkey = read_identity_pubkey()

    sdr.kill_stale_sdrangel()
    sdr.wipe_plist()

    needs_soapy = (combo.rx_plugin == "SoapySDR"
                   or combo.tx_plugin == "SoapySDR")
    log_path = RESULTS_DIR / f"{combo.name}.log"
    sdrproc = sdr.SDRangel(
        log_path=log_path, soapy=needs_soapy, mcr_hz=24_000_000,
    )

    # Companion baseline (BEFORE spawning sdrangel — fast fail if Heltec down).
    # SDRANGEL_NO_COMPANION=1 bypasses for offline/dump-capture tests.
    pre_advert = 0
    if os.getenv("SDRANGEL_NO_COMPANION") == "1":
        print(f"  companion baseline skipped (SDRANGEL_NO_COMPANION=1)")
    else:
        try:
            baseline = companion_contacts(*COMPANION, timeout=15)
            pre_advert = baseline.get(pubkey, {}).get("last_advert", 0)
        except Exception as e:
            res["outcome"] = "COMPANION_UNREACHABLE"
            res["error"] = f"companion baseline: {e}"
            res["duration_s"] = round(time.time() - started, 1)
            print(f"  outcome={res['outcome']} err={res['error']}")
            return res

    # Spawn sdrangel
    try:
        pid = sdrproc.spawn()
        print(f"  spawned pid={pid} log={log_path}")
    except Exception as e:
        res["outcome"] = "HARNESS_ERROR_SPAWN"
        res["error"] = str(e)
        res["duration_s"] = round(time.time() - started, 1)
        return res

    try:
        # boot
        def _boot() -> bool:
            if not sdrproc.wait_ready(timeout=45):
                raise RuntimeError("REST not ready within 45s")
            return True
        ok, _ = run_step(res, sdrproc, "boot", _boot)
        if not ok:
            return finalize_combo(res, sdrproc, started, pubkey, pre_advert)

        # RX setup (only if combo has RX side)
        ds_rx_holder: dict[str, int] = {"v": -1}
        if combo.rx_plugin is not None:
            ok, ds_rx = run_step(res, sdrproc, "rx_setup",
                                 lambda: setup_rx_deviceset(combo))
            if not ok:
                return finalize_combo(res, sdrproc, started, pubkey, pre_advert)
            ds_rx_holder["v"] = ds_rx
            print(f"  RX up on ds[{ds_rx}]")
            ok, _ = run_step(res, sdrproc, "rx_warmup",
                             lambda: time.sleep(args.rx_warmup) or True)
            if not ok:
                return finalize_combo(res, sdrproc, started, pubkey, pre_advert)

        # TX setup
        ok, ds_tx = run_step(res, sdrproc, "tx_setup",
                             lambda: setup_tx_deviceset(combo, ds_rx_holder["v"]))
        if not ok:
            return finalize_combo(res, sdrproc, started, pubkey, pre_advert)
        print(f"  TX up on ds[{ds_tx}]")

        # device/run for TX
        ok, _ = run_step(res, sdrproc, "tx_run",
                         lambda: sdr.device_run(ds_tx) or True)
        if not ok:
            return finalize_combo(res, sdrproc, started, pubkey, pre_advert)
        # Allow ATR/DAC settle
        time.sleep(2.0)
        if not sdrproc.alive():
            res["outcome"] = "CRASH_DURING_TX_SETTLE"
            res["error"] = "sdrangel died during 2s post-run settle"
            return finalize_combo(res, sdrproc, started, pubkey, pre_advert)

        # sendNow
        def _send_now() -> Any:
            code, body = sdr.channel_action(
                ds_tx, 0, "MeshcoreMod", "MeshcoreModActions",
                {"sendNow": 1}, direction=1,
            )
            if code != 202:
                raise RuntimeError(f"sendNow: {code} {body}")
            return body
        ok, _ = run_step(res, sdrproc, "tx_sendnow", _send_now)
        if not ok:
            return finalize_combo(res, sdrproc, started, pubkey, pre_advert)

        print(f"  sendNow fired; waiting {args.wait}s for propagation...")
        time.sleep(args.wait)
        if not sdrproc.alive():
            res["outcome"] = "CRASH_DURING_TX_WAIT"
            res["error"] = "sdrangel terminated during propagation wait"
            return finalize_combo(res, sdrproc, started, pubkey, pre_advert)

        # All steps completed cleanly; outcome will be picked in finalize.
        return finalize_combo(res, sdrproc, started, pubkey, pre_advert)

    finally:
        # finalize_combo handles its own kill + summary print on the
        # normal path; this finally only catches paths where the body
        # raised before reaching finalize_combo.
        try:
            if sdrproc.alive():
                sdrproc.kill(grace=2.0)
                sdr.kill_stale_sdrangel()
                if "duration_s" not in res or res["duration_s"] == 0.0:
                    res["duration_s"] = round(time.time() - started, 1)
                print(f"  outcome={res['outcome']} (unexpected exit path) "
                      f"err={(res['error'] or '')[:80]}")
        except Exception:
            pass

    return res


# ---------------------------------------------------------------------------
# Smoke test (spawn + enumerate + kill)
# ---------------------------------------------------------------------------


def smoke_test() -> int:
    print("[smoke] killing stale + wiping plist")
    sdr.kill_stale_sdrangel()
    sdr.wipe_plist()

    log = RESULTS_DIR / "smoke.log"
    s = sdr.SDRangel(log_path=log, soapy=True, mcr_hz=24_000_000)
    try:
        pid = s.spawn()
        print(f"[smoke] spawned pid={pid}")
        if not s.wait_ready(timeout=45):
            print("[smoke] FAIL: not ready in 45s")
            print(s.tail_log(n_bytes=4000))
            return 2
        rx = sdr.list_devices(0)
        tx = sdr.list_devices(1)
        b210_rx = [x for x in rx if "BADC10E" in x.get("displayedName", "")]
        b210_tx = [x for x in tx if "BADC10E" in x.get("displayedName", "")]
        print(f"[smoke] rx total={len(rx)}  B210 entries={len(b210_rx)}")
        for x in b210_rx:
            print(f"  RX  hw={x['hwType']:<10} {x['displayedName']}")
        print(f"[smoke] tx total={len(tx)}  B210 entries={len(b210_tx)}")
        for x in b210_tx:
            print(f"  TX  hw={x['hwType']:<10} {x['displayedName']}")
        # Expect 4 RX (USRP[0:0,0:1] + SoapySDR[0:0,0:1]) and 4 TX.
        ok = (len(b210_rx) == 4 and len(b210_tx) == 4)
        return 0 if ok else 3
    finally:
        s.kill(grace=2.0)
        sdr.kill_stale_sdrangel()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true",
                    help="spawn + enumerate + kill (no TX, no companion)")
    ap.add_argument("--phase", choices=["a", "c", "d", "m"],
                    help="run only one phase")
    ap.add_argument("--combo",
                    help="run a single combo by name (e.g. c2)")
    ap.add_argument("--wait", type=float, default=20.0,
                    help="propagation wait after sendNow (sec)")
    ap.add_argument("--rx-warmup", type=float, default=4.0,
                    help="seconds to keep RX live before TX setup")
    ap.add_argument("--results",
                    default=str(RESULTS_DIR / "matrix_results.json"))
    args = ap.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.smoke:
        return smoke_test()

    combos = all_combos()
    if args.combo:
        combos = [c for c in combos if c.name == args.combo]
        if not combos:
            print(f"unknown combo: {args.combo}")
            return 2
    elif args.phase:
        combos = [c for c in combos if c.phase == args.phase]

    print(f"running {len(combos)} combo(s)")
    if os.getenv("SDRANGEL_NO_COMPANION") == "1":
        print(f"companion check bypassed (SDRANGEL_NO_COMPANION=1)")
    elif not companion_reachable(*COMPANION):
        print(f"companion {COMPANION} unreachable — abort")
        return 2

    results: list[dict[str, Any]] = []
    for c in combos:
        try:
            results.append(run_combo(c, args))
        except KeyboardInterrupt:
            print("INTERRUPTED")
            break
        except Exception as e:
            results.append({
                "combo": asdict(c), "outcome": "HARNESS_ERROR",
                "error": repr(e), "duration_s": 0,
            })

    out = Path(args.results)
    out.write_text(json.dumps(results, indent=2))
    print(f"\nresults: {out}")
    counts: dict[str, int] = {}
    for r in results:
        k = r.get("outcome", "?")
        counts[k] = counts.get(k, 0) + 1
    print(f"summary: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
