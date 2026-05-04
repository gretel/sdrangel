"""sdrangel REST + lifecycle helpers.

Reusable module for driving sdrangel headlessly across the meshcore
M1..M4 milestone harness scripts.  Single source of truth — replaces
ad-hoc curl / inline python sprinkled across earlier harness scripts.

Why this module exists:

* On macOS this dev environment wraps `curl` in `rtk-tee` which TRUNCATES
  large response bodies at ~596 bytes and substitutes a footer pointing
  to a separate log file.  REST replies that contain a full device
  enumeration (~3 KB) come back unparseable when fetched via curl.  All
  REST in this module uses urllib.request which goes straight through.

* sdrangel must be launched with `--soapy` for the SoapySDR sample
  source/sink plugins to load (gated behind `m_enableSoapy` in
  `sdrbase/plugin/pluginmanager.cpp:230`).  Without the flag the plugin
  loader explicitly logs `Soapy SDR disabled skipping libinputsoapysdr.dylib`.

* The `SDRANGEL_USRP_MASTER_CLOCK_RATE_HZ` env var pins the B210 master
  clock at make-time.  Required when working with the LibreSDR_B220mini
  clone (matches gr4-lora 24 MHz MCR).

* Keep temp artefacts under `./tmp/captures/` (project-relative).
  Never `/tmp` (per AGENTS.md).
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REST_BASE = "http://127.0.0.1:8091"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SDRANGEL_BIN = PROJECT_ROOT / "build" / "sdrangel"


# ---------------------------------------------------------------------------
# REST primitives (urllib only — curl is rtk-tee'd and truncates)
# ---------------------------------------------------------------------------


def request(method: str, path: str, body: dict[str, Any] | None = None,
            timeout: float = 10.0) -> tuple[int, str]:
    """Issue a REST call. Returns (status_code, body_text).  Returns
    (0, error_msg) when the connection fails (sdrangel down/booting)."""
    url = REST_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except (urllib.error.URLError, ConnectionRefusedError, OSError) as e:
        return 0, str(e)


def get_json(path: str, timeout: float = 10.0) -> dict[str, Any] | None:
    code, body = request("GET", path, timeout=timeout)
    if code != 200:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# sdrangel lifecycle
# ---------------------------------------------------------------------------


@dataclass
class SDRangel:
    """Manage one sdrangel process + its log file."""

    log_path: Path
    soapy: bool = False
    mcr_hz: int | None = 24_000_000
    extra_env: dict[str, str] = field(default_factory=dict)
    proc: "subprocess.Popen[bytes] | None" = None

    def env(self) -> dict[str, str]:
        e = dict(os.environ)
        if self.mcr_hz is not None:
            e["SDRANGEL_USRP_MASTER_CLOCK_RATE_HZ"] = str(self.mcr_hz)
        elif "SDRANGEL_USRP_MASTER_CLOCK_RATE_HZ" in e:
            del e["SDRANGEL_USRP_MASTER_CLOCK_RATE_HZ"]
        e.setdefault(
            "UHD_IMAGES_DIR",
            "/Users/tom/src/uhd/ettus-uhd-oc/install/share/uhd/images",
        )
        e.update(self.extra_env)
        return e

    def spawn(self) -> int:
        if not SDRANGEL_BIN.exists():
            raise FileNotFoundError(f"missing sdrangel binary: {SDRANGEL_BIN}")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_bytes(b"")
        argv = [str(SDRANGEL_BIN)]
        if self.soapy:
            argv.append("--soapy")
        log_fp = open(self.log_path, "ab", buffering=0)
        self.proc = subprocess.Popen(
            argv, env=self.env(),
            stdout=log_fp, stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
        )
        return self.proc.pid

    def wait_ready(self, timeout: float = 40.0, settle: float = 3.0) -> bool:
        """Poll /sdrangel until 200, then sleep `settle` seconds.

        REST server binds and answers GETs before the main thread has
        finished processing all init-time MsgQueue work.  POST'd messages
        (e.g. MsgAddDeviceSet) submitted in that window are accepted (202)
        but never dispatched to the main thread's run loop.  A short
        post-200 settle gives the main thread a chance to drain its queue.
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc and self.proc.poll() is not None:
                return False
            code, _ = request("GET", "/sdrangel", timeout=1.5)
            if code == 200:
                if settle > 0:
                    time.sleep(settle)
                return True
            time.sleep(0.5)
        return False

    def alive(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None

    def kill(self, grace: float = 2.0) -> int | None:
        if not self.proc:
            return None
        if self.proc.poll() is not None:
            return self.proc.returncode
        try:
            self.proc.send_signal(signal.SIGTERM)
        except OSError:
            pass
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                self.proc.send_signal(signal.SIGKILL)
            except OSError:
                pass
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
        return self.proc.returncode

    def tail_log(self, n_bytes: int = 16_000) -> str:
        if not self.log_path.exists():
            return ""
        size = self.log_path.stat().st_size
        with open(self.log_path, "rb") as f:
            if size > n_bytes:
                f.seek(size - n_bytes)
            return f.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Plist + zombie cleanup
# ---------------------------------------------------------------------------


def wipe_plist() -> None:
    """Wipe sdrangel's per-user state. Safe to call when sdrangel is down."""
    subprocess.run(
        ["defaults", "delete", "com.f4exb.SDRangel"],
        check=False, capture_output=True,
    )


def kill_stale_sdrangel() -> int:
    """Kill any leftover sdrangel processes. Returns count killed."""
    out = subprocess.run(
        ["pgrep", "sdrangel"], capture_output=True, text=True,
    ).stdout.strip()
    pids = [int(p) for p in out.split() if p.strip().isdigit()]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if pids:
        time.sleep(2.0)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return len(pids)


# ---------------------------------------------------------------------------
# Deviceset / device / channel helpers
# ---------------------------------------------------------------------------


def list_devices(direction: int) -> list[dict[str, Any]]:
    d = get_json(f"/sdrangel/devices?direction={direction}")
    return d.get("devices", []) if d else []


def get_devset_count() -> int:
    d = get_json("/sdrangel/devicesets")
    return d.get("devicesetcount", 0) if d else 0


def add_deviceset(direction: int, timeout: float = 90.0) -> int:
    """POST a new deviceset of the given direction. Returns its index
    (which is current count - 1).  Polls until it appears.

    Robust to transient REST stalls — sdrangel's REST server can stall
    for 10–30 s during B210 FPGA bitstream load on cold boot.  We
    tolerate that by:

    * retrying the initial POST on `code=0` (connection refused / urllib
      socket error) up to a few times
    * issuing each polling GET with a short timeout (2 s) so a stalled
      GET doesn't burn the whole polling budget
    * applying the overall `timeout` budget across both POST retries
      and pre/post comparison
    """
    deadline = time.time() + timeout
    pre: int | None = None
    # Retry the initial GET-pre + POST until either pre is captured and
    # POST returns 202, or the budget runs out.
    while time.time() < deadline:
        if pre is None:
            d = get_json("/sdrangel/devicesets", timeout=2.0)
            if d is not None:
                pre = d.get("devicesetcount", 0)
                continue
            time.sleep(0.5)
            continue
        code, body = request("POST",
                             f"/sdrangel/deviceset?direction={direction}",
                             timeout=4.0)
        if code == 202:
            break
        if code == 0:
            # Transient stall (sdrangel busy / not yet up).  Retry shortly.
            time.sleep(0.5)
            continue
        raise RuntimeError(f"add_deviceset: {code} {body}")
    else:
        raise TimeoutError(f"add_deviceset: never got 202 within {timeout}s")
    if pre is None:
        raise TimeoutError("add_deviceset: pre-count never captured")
    # Poll for new count.
    while time.time() < deadline:
        d = get_json("/sdrangel/devicesets", timeout=2.0)
        if d is not None and d.get("devicesetcount", 0) > pre:
            return pre
        time.sleep(0.5)
    raise TimeoutError(f"new ds didn't appear within {timeout}s")


def remove_trailing_deviceset(target_count: int, timeout: float = 8.0) -> None:
    """Pop trailing devicesets until count <= target_count."""
    t0 = time.time()
    while get_devset_count() > target_count:
        request("DELETE", "/sdrangel/deviceset")
        time.sleep(0.4)
        if time.time() - t0 > timeout:
            return


def stop_deviceset(ds: int) -> None:
    request("DELETE", f"/sdrangel/deviceset/{ds}/device/run")


def put_device(ds: int, hw_type: str, direction: int,
               sequence: int = 0, stream_index: int = 0) -> None:
    """Mount a hardware device on the given deviceset.

    For B210:
      - USRP plugin: hw_type='USRP'
      - SoapySDR plugin: hw_type='SoapySDR'
    Stream index 0 = channel A:A, 1 = channel A:B (per [seq:idx] notation).
    """
    body = {
        "hwType": hw_type, "direction": direction,
        "deviceSequence": sequence, "deviceStreamIndex": stream_index,
    }
    # 30s timeout — UHD can block REST while loading B210 FPGA (~30s on
    # cold boot).  Shorter timeout would surface as a misleading 'code=0
    # connection refused' even though sdrangel is alive and busy.
    code, b = request("PUT", f"/sdrangel/deviceset/{ds}/device", body, timeout=30.0)
    if code // 100 != 2:
        raise RuntimeError(f"PUT device {hw_type}[{sequence}:{stream_index}] "
                           f"on ds[{ds}]: {code} {b}")


def patch_device_settings(ds: int, settings_key: str,
                          settings: dict[str, Any], hw_type: str,
                          direction: int) -> None:
    body = {"deviceHwType": hw_type, "direction": direction,
            settings_key: settings}
    # 30s — same rationale as put_device (FPGA load can block REST).
    code, b = request("PATCH", f"/sdrangel/deviceset/{ds}/device/settings",
                      body, timeout=30.0)
    if code // 100 != 2:
        raise RuntimeError(f"PATCH device settings ds[{ds}]: {code} {b}")


def add_channel(ds: int, channel_type: str, settings_key: str,
                settings: dict[str, Any], direction: int) -> int:
    """Add channel + apply settings.  Returns channel index."""
    pre_chs = _list_channel_ids(ds)
    code, b = request("POST", f"/sdrangel/deviceset/{ds}/channel",
                      {"channelType": channel_type, "direction": direction})
    if code // 100 != 2:
        raise RuntimeError(f"POST channel {channel_type}: {code} {b}")
    time.sleep(0.4)
    post_chs = _list_channel_ids(ds)
    new = [i for i in post_chs if i not in pre_chs]
    if not new:
        # fallback: pick last channel of matching type
        all_match = [i for i, t in _list_channel_ids_with_type(ds)
                     if t == channel_type]
        if not all_match:
            raise RuntimeError(f"channel {channel_type} not found post-POST")
        idx = all_match[-1]
    else:
        idx = new[-1]
    body = {"channelType": channel_type, "direction": direction,
            settings_key: settings}
    code, b = request("PATCH",
                      f"/sdrangel/deviceset/{ds}/channel/{idx}/settings",
                      body)
    if code // 100 != 2:
        raise RuntimeError(f"PATCH channel {channel_type} idx[{idx}]: {code} {b}")
    return idx


def channel_action(ds: int, ch_idx: int, channel_type: str,
                   action_key: str, action_body: dict[str, Any],
                   direction: int) -> tuple[int, str]:
    body = {"channelType": channel_type, "direction": direction,
            action_key: action_body}
    return request("POST",
                   f"/sdrangel/deviceset/{ds}/channel/{ch_idx}/actions", body)


def device_run(ds: int) -> None:
    code, b = request("POST", f"/sdrangel/deviceset/{ds}/device/run", {})
    if code // 100 != 2:
        raise RuntimeError(f"POST device/run ds[{ds}]: {code} {b}")


def _list_channel_ids(ds: int) -> list[int]:
    d = get_json(f"/sdrangel/deviceset/{ds}")
    if not d:
        return []
    return sorted(int(c.get("index"))
                  for c in d.get("channels", []) if c.get("index") is not None)


def _list_channel_ids_with_type(ds: int) -> list[tuple[int, str]]:
    d = get_json(f"/sdrangel/deviceset/{ds}")
    if not d:
        return []
    return [(int(c["index"]), c.get("id", ""))
            for c in d.get("channels", []) if "index" in c]
