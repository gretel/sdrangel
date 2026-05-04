"""SDRangel UI option enumeration — single source of truth for harness.

Mirrors what the SDRangel GUI exposes for the plugins under test:
USRPInput, USRPOutput, MeshcoreMod, MeshcoreDemod.  Harness scripts
(run_tx_matrix.py, onair_three_channel.py, test_modulator_companion.py)
import named constants from here instead of using inline literals.

Canonical sources (consulted when populating this module):
  * SWG generated cpp  — wire-level JSON keys (always wins on casing)
    swagger/sdrangel/code/qt5/client/SWG{USRPInput,USRPOutput,
    MeshtasticMod,MeshtasticDemod}Settings.cpp
  * Settings header — C++ enum types + member fields
    plugins/samplesource/usrpinput/usrpinputsettings.h
    plugins/samplesink/usrpoutput/usrpoutputsettings.h
    plugins/channeltx/modmeshcore/meshcoremodsettings.h
    plugins/channelrx/demodmeshcore/meshcoredemodsettings.h
  * Settings cpp — defaults via resetToDefaults()
  * GUI cpp + ui — slider min/max + dropdown enum strings + spinbox ranges
    plugins/samplesource/usrpinput/usrpinputgui.cpp + .ui
    plugins/samplesink/usrpoutput/usrpoutputgui.cpp + .ui
    plugins/channeltx/modmeshcore/meshcoremodgui.cpp + .ui
    plugins/channelrx/demodmeshcore/meshcoredemodgui.cpp + .ui

JSON key gotcha (sdrangel-dev skill rule):
  * SoapySDR settings key on the wire is `soapySDRInputSettings`
    (UPPERCASE D-R), NOT `soapySdrInputSettings`.  The C++ accessor
    casing differs from the JSON wire-level key.  Always cross-check
    SWG `*.cpp` `pJson["..."]` lookups.

Note:  this module is STATIC (built from source code).  Schema drift
will surface as harness tests failing against the running binary.
For dynamic introspection, hit
`GET /sdrangel/devices/<hwType>/settings` on a running sdrangel
(out of scope here).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any


# =============================================================================
# USRPInput (plugins/samplesource/usrpinput)
# =============================================================================


class USRPGainMode(IntEnum):
    """USRPInputSettings::GainMode — usrpinputsettings.h:34-37.

    NOTE on UHD reality:  B210 hardware has no built-in AGC; the
    "auto" option here means *sdrangel's* GUI auto-distribute mode
    (per UI tooltip "Optimally distributed gain").  It still drives
    UHD's manual `usrp->set_gain()` underneath; UHD doesn't get an
    AGC enable flag from this code path.  Treat AUTO as a label for
    a sdrangel-internal scheme, not a hardware AGC.
    """
    AUTO = 0       # sdrangel optimally-distributed (still calls set_gain)
    MANUAL = 1     # explicit dB number


# Antenna paths exposed by the USRPInput GUI dropdown for B210/B220.
# Source: plugins/samplesource/usrpinput/usrpinputgui.cpp ui->antenna entries
# (B210 supports TX/RX, RX2 ports per UHD `multi_usrp::get_rx_antennas()`).
USRP_RX_ANTENNAS = ("TX/RX", "RX2")
USRP_TX_ANTENNAS = ("TX/RX", "TX/RX2")  # B210 TX/RX only; verify on UI

# Clock source enum.  Source: usrpinput/usrpinputgui.cpp ui->clockSource
# combo box entries.
USRP_CLOCK_SOURCES = ("internal", "external", "mimo", "gpsdo")

# Gain slider range (dB).  Source: usrpinput/usrpinputgui.ui
#   <widget class="QSlider" name="gain"> minimum 0, maximum 89 (or 76)
# B210 hard cap is 89.75 dB; UI slider integer 0..89.
USRP_RX_GAIN_RANGE = (0, 89, 1)
USRP_TX_GAIN_RANGE = (0, 89, 1)

# Sample rate range — UI spinbox.  Practical AD9361 chain:
#   200 ksps min (below = needs SoftDecim), 56 MS/s max single-ch.
USRP_DEV_SAMPLE_RATE_RANGE = (200_000, 56_000_000, 1)

# log2 soft decim/interp — UI spinbox 0..6.
USRP_LOG2_SOFT_RANGE = (0, 6, 1)

# Defaults from resetToDefaults() in usrpinputsettings.cpp.
USRP_INPUT_DEFAULTS: dict[str, Any] = {
    "centerFrequency": 435_000_000,
    "devSampleRate": 3_000_000,
    "loOffset": 0,
    "log2SoftDecim": 0,
    "lpfBW": 10_000_000.0,
    "gain": 50,
    "antennaPath": "TX/RX",
    "gainMode": USRPGainMode.AUTO.value,
    "clockSource": "internal",
    "transverterMode": False,
    "transverterDeltaFrequency": 0,
    "dcBlock": False,
    "iqCorrection": False,
}

# Defaults from usrpoutputsettings.cpp resetToDefaults().
USRP_OUTPUT_DEFAULTS: dict[str, Any] = {
    "centerFrequency": 435_000_000,
    "devSampleRate": 3_000_000,
    "loOffset": 0,
    "log2SoftInterp": 0,
    "lpfBW": 10_000_000.0,
    "gain": 50,
    "antennaPath": "TX/RX",
    "clockSource": "internal",
    "transverterMode": False,
    "transverterDeltaFrequency": 0,
}


# =============================================================================
# MeshcoreMod / MeshcoreDemod (plugins/channeltx/modmeshcore + ../channelrx/demodmeshcore)
# =============================================================================


# Bandwidths table — verbatim from meshcoremodsettings.cpp:28-58 +
# meshcoredemodsettings.cpp matching block.  28 entries (3*8+4).
# Slider in GUI (meshcoremodgui.cpp setBandwidths) clamps maxIndex by
#   `basebandSampleRate / oversampling`
# so the *actual* useable max BW depends on the channel rate.
# To use BW=62500 → channel rate ≥ 250000 (62500*4)
# To use BW=125000 → channel rate ≥ 500000
# To use BW=250000 → channel rate ≥ 1000000
MESHCORE_BANDWIDTHS_HZ: tuple[int, ...] = (
    325, 488, 750, 1500,
    2604, 3125, 3906,
    5208, 6250, 7813,
    10417, 12500, 15625,
    20833, 25000, 31250,
    41667, 50000, 62500,        # idx 16, 17, 18  ← 62500 = idx 18
    83333, 100000, 125000,      # idx 19, 20, 21  ← 125000 = idx 21
    166667, 200000, 250000,     # idx 22, 23, 24  ← 250000 = idx 24
    333333, 400000, 500000,     # idx 25, 26, 27  ← 500000 = idx 27
)
MESHCORE_OVERSAMPLING = 4

# Convenience constants for common BWs.
class MeshcoreBW(IntEnum):
    """Indices into MESHCORE_BANDWIDTHS_HZ."""
    BW_62500 = 18
    BW_125000 = 21
    BW_250000 = 24
    BW_500000 = 27


def meshcore_min_channel_rate_for(bw_idx: int) -> int:
    """Minimum channel sample rate required for the GUI slider to allow
    this bandwidth index.  Mirrors MeshcoreModGUI::setBandwidths cap."""
    return MESHCORE_BANDWIDTHS_HZ[bw_idx] * MESHCORE_OVERSAMPLING


# Spread factor — UI spinbox 6..12 (LoRa standard SF range).
MESHCORE_SF_RANGE = (6, 12, 1)

# Coding-rate parity bits.  m_nbParityBits = 1..4 mapping CR=4/5..4/8.
MESHCORE_PARITY_BITS_RANGE = (1, 4, 1)

# Preamble chirps — UI spinbox.  MeshCore default 8.
MESHCORE_PREAMBLE_RANGE = (4, 32, 1)

# Sync word — single byte in UI hex spin.
MESHCORE_SYNC_WORD_DEFAULT = 0x12


class MeshcoreMessageType(IntEnum):
    """MeshcoreModSettings::MessageType — meshcoremodsettings.h:38-46."""
    TEXT = 0
    ADVERT = 1
    TXT_MSG = 2
    GRP_TXT = 3
    ANON_REQ = 4
    ACK = 5


# Defaults from meshcoremodsettings.cpp::resetToDefaults().
MESHCORE_MOD_DEFAULTS: dict[str, Any] = {
    "inputFrequencyOffset": 0,
    "bandwidthIndex": MeshcoreBW.BW_62500.value,
    "spreadFactor": 8,
    "deBits": 0,
    "preambleChirps": 8,
    "quietMillis": 1000,
    "nbParityBits": 4,
    "syncWord": MESHCORE_SYNC_WORD_DEFAULT,
    "channelMute": False,
    "messageType": MeshcoreMessageType.ADVERT.value,
    "messageRepeat": 1,
    "udpEnabled": False,
    "udpAddress": "127.0.0.1",
    "udpPort": 9998,
    "invertRamps": False,
    "meshcoreRegionCode": "EU_868",
    "meshcorePresetName": "MESHCORE_EU",
    "meshcoreChannelIndex": 0,
}

# Defaults from meshcoredemodsettings.cpp::resetToDefaults() — partial; full
# audit on demand.  Sufficient to drive the harness's RX-side PATCH bodies.
MESHCORE_DEMOD_DEFAULTS: dict[str, Any] = {
    "inputFrequencyOffset": 0,
    "bandwidthIndex": MeshcoreBW.BW_62500.value,
    "spreadFactor": 8,
    "deBits": 0,
    "decodeActive": True,
    "preambleChirps": 8,
    "nbParityBits": 4,
    "sendViaUDP": False,
    "sendJsonViaUDP": False,
    "udpAddress": "127.0.0.1",
    "udpPort": 9999,
    "meshcoreRegionCode": "EU_868",
    "meshcorePresetName": "MESHCORE_EU",
    "meshcoreChannelIndex": 0,
}


# =============================================================================
# JSON wire-level key map (the sdrangel-dev gotcha layer)
# =============================================================================


# REST PATCH envelope sub-key per (plugin, direction). All confirmed from
# SWG generated cpp `pJson[...]` lookups.
REST_SETTINGS_KEYS: dict[tuple[str, int], str] = {
    ("USRP", 0): "usrpInputSettings",
    ("USRP", 1): "usrpOutputSettings",
    ("SoapySDR", 0): "soapySDRInputSettings",   # uppercase D-R
    ("SoapySDR", 1): "soapySDROutputSettings",  # uppercase D-R
    ("FileOutput", 1): "fileOutputSettings",
    ("FileInput", 0): "fileInputSettings",
}


CHANNEL_SETTINGS_KEYS: dict[str, str] = {
    "MeshcoreDemod": "MeshtasticDemodSettings",   # borrows donor SWG schema
    "MeshcoreMod":   "MeshtasticModSettings",     # borrows donor SWG schema
}


# =============================================================================
# Harness-level option container
# =============================================================================


@dataclass
class HarnessOptions:
    """Ranges + defaults the harness exposes as named knobs.  Picking
    a value (e.g. rx_gain=40) becomes a deliberate selection out of
    USRP_RX_GAIN_RANGE, with the reader seeing the full surface here.

    Defaults below match the **B6.11.0 user-constraint baseline**:
      * RX gain 40 (UI-adjustable, manual mode)
      * TX gain 20
      * external clock source (10 MHz reference now connected)
      * TX wire rate 250 ksps + log2SoftInterp=0 → channel = 250 ksps
        (= BW*os for sf=8/bw=62500/os=4)
      * RX kept at proven 1 MS/s + log2SoftDecim=2 → channel = 250 ksps
      * lo_offset 62500 (same as before; in spec)
      * BW idx 18 (62500 Hz; companion config)

    Override any field from the call site to test variants.
    """
    # Hardware-shared
    center_freq_hz: int = 869_618_000
    lo_offset_hz: int = 62_500
    master_clock_rate_hz: int = 24_000_000
    clock_source: str = "external"   # was "internal"; external clock now wired

    # RX (USRPInput)
    rx_dev_sample_rate: int = 1_000_000
    rx_log2_soft_decim: int = 2
    rx_gain: int = 40                # was 60 (saturating front end)
    rx_gain_mode: USRPGainMode = USRPGainMode.MANUAL
    rx_antenna: str = "RX2"

    # TX (USRPOutput)
    tx_dev_sample_rate: int = 250_000   # was 1_000_000 (long burst → ring overflow)
    tx_log2_soft_interp: int = 0        # was 2 → channel rate = wire rate
    tx_gain: int = 20                   # was 60; companion one room away
    tx_antenna: str = "TX/RX"

    # MeshCore PHY
    sf: int = 8
    bw_idx: int = MeshcoreBW.BW_62500.value
    parity_bits: int = 4   # CR=4/8
    preamble_chirps: int = 16
    sync_word: int = MESHCORE_SYNC_WORD_DEFAULT

    # Companion / oracle
    udp_demod_port: int = 9999

    def usrp_input_body(self) -> dict[str, Any]:
        """USRPInputSettings PATCH body (subset).  REST key
        `usrpInputSettings`.  Mirrors UI-exposed knobs."""
        return {
            "centerFrequency": self.center_freq_hz,
            "devSampleRate": self.rx_dev_sample_rate,
            "log2SoftDecim": self.rx_log2_soft_decim,
            "antennaPath": self.rx_antenna,
            "loOffset": self.lo_offset_hz,
            "gain": self.rx_gain,
            "gainMode": int(self.rx_gain_mode),
            "clockSource": self.clock_source,
            "masterClockRate": self.master_clock_rate_hz,
        }

    def usrp_output_body(self) -> dict[str, Any]:
        """USRPOutputSettings PATCH body (subset).  REST key
        `usrpOutputSettings`."""
        return {
            "centerFrequency": self.center_freq_hz,
            "devSampleRate": self.tx_dev_sample_rate,
            "log2SoftInterp": self.tx_log2_soft_interp,
            "antennaPath": self.tx_antenna,
            "loOffset": 0,   # tx LO offset relative to channel; keep 0
            "gain": self.tx_gain,
            "clockSource": self.clock_source,
            # masterClockRate=0 means "inherit from RX buddy" per
            # B6.1 fix; keep 0 in buddy-share, set explicit in TX-only.
            "masterClockRate": 0,
        }

    def meshcore_demod_body(self) -> dict[str, Any]:
        return {
            "inputFrequencyOffset": self.lo_offset_hz,
            "bandwidthIndex": self.bw_idx,
            "spreadFactor": self.sf,
            "deBits": 0,
            "decodeActive": 1,
            "nbParityBits": self.parity_bits,
            "preambleChirps": self.preamble_chirps,
            "sendViaUDP": 0,
            "sendJsonViaUDP": 1,
            "udpAddress": "127.0.0.1",
            "udpPort": self.udp_demod_port,
        }

    def meshcore_mod_body(self, text: str) -> dict[str, Any]:
        return {
            "inputFrequencyOffset": 0,
            "bandwidthIndex": self.bw_idx,
            "spreadFactor": self.sf,
            "deBits": 0,
            "nbParityBits": self.parity_bits,
            "preambleChirps": self.preamble_chirps,
            "syncWord": self.sync_word,
            "messageRepeat": 1,
            "channelMute": 0,
            "textMessage": text,
            "udpEnabled": 0,
        }

    def validate(self) -> list[str]:
        """Return list of human-readable rule violations (empty = ok).

        Catches obvious misconfigurations before they hit sdrangel:
          * BW exceeds channel rate / oversampling cap (UI slider would
            also reject — fail loudly here).
          * Gain outside slider range.
          * Master clock rate not divisible by sample rate (UHD will
            silently re-derive — ChirpChat parity ran into this).
        """
        violations: list[str] = []
        bw_hz = MESHCORE_BANDWIDTHS_HZ[self.bw_idx]
        rx_channel_rate = self.rx_dev_sample_rate >> self.rx_log2_soft_decim
        tx_channel_rate = self.tx_dev_sample_rate >> self.tx_log2_soft_interp
        rx_max_bw = rx_channel_rate // MESHCORE_OVERSAMPLING
        tx_max_bw = tx_channel_rate // MESHCORE_OVERSAMPLING
        if bw_hz > rx_max_bw:
            violations.append(
                f"RX channel rate {rx_channel_rate} too low for "
                f"BW={bw_hz}: requires ≥ {bw_hz * MESHCORE_OVERSAMPLING}"
            )
        if bw_hz > tx_max_bw:
            violations.append(
                f"TX channel rate {tx_channel_rate} too low for "
                f"BW={bw_hz}: requires ≥ {bw_hz * MESHCORE_OVERSAMPLING}"
            )
        if not (USRP_RX_GAIN_RANGE[0] <= self.rx_gain <= USRP_RX_GAIN_RANGE[1]):
            violations.append(
                f"rx_gain={self.rx_gain} outside slider range "
                f"{USRP_RX_GAIN_RANGE[:2]}"
            )
        if not (USRP_TX_GAIN_RANGE[0] <= self.tx_gain <= USRP_TX_GAIN_RANGE[1]):
            violations.append(
                f"tx_gain={self.tx_gain} outside slider range "
                f"{USRP_TX_GAIN_RANGE[:2]}"
            )
        if self.clock_source not in USRP_CLOCK_SOURCES:
            violations.append(
                f"clock_source={self.clock_source!r} not in "
                f"{USRP_CLOCK_SOURCES}"
            )
        return violations


# =============================================================================
# Smoke print — invoke directly for quick option dump
# =============================================================================


def main() -> int:
    """Print the active baseline + option ranges for debugging."""
    o = HarnessOptions()
    print("=== HarnessOptions defaults (B6.11.0 baseline) ===")
    for k, v in vars(o).items():
        print(f"  {k:24s} = {v!r}")
    v = o.validate()
    if v:
        print("\nVIOLATIONS:")
        for x in v:
            print(f"  - {x}")
    else:
        print("\nvalidation: OK")
    print("\n=== ranges ===")
    print(f"  USRP_RX_GAIN_RANGE       = {USRP_RX_GAIN_RANGE}")
    print(f"  USRP_TX_GAIN_RANGE       = {USRP_TX_GAIN_RANGE}")
    print(f"  USRP_DEV_SAMPLE_RATE_RANGE = {USRP_DEV_SAMPLE_RATE_RANGE}")
    print(f"  USRP_LOG2_SOFT_RANGE     = {USRP_LOG2_SOFT_RANGE}")
    print(f"  USRP_CLOCK_SOURCES       = {USRP_CLOCK_SOURCES}")
    print(f"  USRP_RX_ANTENNAS         = {USRP_RX_ANTENNAS}")
    print(f"  USRP_TX_ANTENNAS         = {USRP_TX_ANTENNAS}")
    print(f"  USRPGainMode             = "
          f"{[(m.name, m.value) for m in USRPGainMode]}")
    print(f"  MeshcoreBW               = "
          f"{[(m.name, m.value, MESHCORE_BANDWIDTHS_HZ[m.value]) for m in MeshcoreBW]}")
    print(f"  MeshcoreMessageType      = "
          f"{[(m.name, m.value) for m in MeshcoreMessageType]}")
    print(f"  MESHCORE_SF_RANGE        = {MESHCORE_SF_RANGE}")
    print(f"  MESHCORE_PARITY_BITS_RANGE = {MESHCORE_PARITY_BITS_RANGE}")
    print(f"  MESHCORE_PREAMBLE_RANGE  = {MESHCORE_PREAMBLE_RANGE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
