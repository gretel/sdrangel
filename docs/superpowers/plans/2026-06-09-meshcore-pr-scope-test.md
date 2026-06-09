# MeshCore PR Scope & MCR Pinning Test Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use /skill:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Determine whether devices/soapysdr/devicesoapysdr.cpp MCR pinning changes are required for SoapySDR TX, then produce a clean PR containing only working MeshCore channel plugins + SoapySDR TX fixes (no USRPOutput).

**Architecture:** The `pr/meshcore` branch has 8 MeshCore commits (on top of base `cdbb92e66`) that interleave:
- MeshCore protocol library + channel plugins (wanted)
- SoapySDR output TX fixes (wanted)
- USRPOutput MCR pinning + thread API changes (broken, exclude)
- devices/soapysdr MCR pinning (questionable — test required)

We create a clean branch from upstream HEAD, apply only wanted files, test MCR pinning dependency, then finalize PR.

**Tech Stack:** C++20, Qt 5.15, SoapySDR, UHD, CMake

**Roadmap:** None

**Phase:** Single-plan implementation

---

## File Structure

### Wanted files (will be in clean branch)

| File | Source | Responsibility |
|------|--------|----------------|
| `modemmeshcore/` (20 files) | New code | MeshCore protocol library: packet format, crypto (Monocypher), builders, command parser, identity store |
| `plugins/channelrx/demodmeshcore/` (20 files) | New code | MeshCore RX channel plugin: LoRa decode pipeline, GUI, settings, packet table, key dialog |
| `plugins/channeltx/modmeshcore/` (18 files) | New code | MeshCore TX channel plugin: LoRa encode pipeline, GUI, MESHCORE: command parser, REST actions |
| `plugins/channelrx/CMakeLists.txt` | Modified (+6 lines) | Add `demodmeshcore` subdirectory |
| `plugins/channeltx/CMakeLists.txt` | Modified (+4 lines) | Add `modmeshcore` subdirectory |
| `plugins/samplesink/soapysdroutput/soapysdroutput.cpp` | Modified | TX fixes: handleInputMessages, setGain post-activation, fullScale threshold, timed first write |
| `plugins/samplesink/soapysdroutput/soapysdroutput.h` | Modified | MsgGetStreamInfo/MsgReportStreamInfo message classes |
| `plugins/samplesink/soapysdroutput/soapysdroutputthread.cpp` | Modified | TX thread fixes, diagnostic counters |
| `plugins/samplesink/soapysdroutput/soapysdroutputthread.h` | Modified | getStreamStatus(), counter fields |
| `sdrbase/webapi/webapirequestmapper.cpp` | Modified (+5 lines) | WebAPI route mapping for MeshCore mod actions |
| `sdrbase/webapi/webapiutils.cpp` | Modified (+3 lines) | WebAPI utility support |
| `swagger/` (6 files) | Modified | SWG bindings: SWGMeshcoreModActions, ModelFactory registration |
| `exports/export.h` | Modified (+12 lines) | Export macro for modemmeshcore symbols |
| `devices/soapysdr/devicesoapysdr.cpp` | **Pending test** | MCR pinning + auto_tick_rate=0 for SoapyUHD |

### Excluded files (broken or upstream-only)

| File | Reason |
|------|--------|
| `plugins/samplesink/usrpoutput/usrpoutput.cpp` | MCR pinning + API changes broken per user |
| `plugins/samplesink/usrpoutput/usrpoutput.h` | Same |
| `devices/usrp/deviceusrpparam.cpp` | Not in our scope (native USRP path) |
| `.github/workflows/` | Upstream infra |
| `external/CMakeLists.txt` | Upstream change |
| `cmake/Modules/FindFLAC.cmake` | Upstream change |
| `flatpak/` | Upstream change |
| `debian/` | Upstream change |
| `snap/` | Upstream change |
| `pyproject.toml`, `uv.lock`, `.python-version`, `.envrc` | Local tooling only |
| `gitdiff.sh` | Local tooling |

---

### Task 1: Create clean branch with wanted files

**Files:**
- Modify: `.git/HEAD` (via git branch/checkout)
- Create: None (branch ref only)
- Working tree: All wanted files from current HEAD

- [ ] **Step 1: Save current HEAD reference for cherry-picking**

```bash
git rev-parse HEAD > ./tmp/meshcore-head.txt
cat ./tmp/meshcore-head.txt
```
Expected: `a9f0889667ae4d19904062785b116fc8901b3d19`

- [ ] **Step 2: Check upstream base commit**

```bash
git rev-parse upstream/main
```
Expected: some SHA (e.g. `cdbb92e662386d5d082329957a175fc3b1fc28bb`)

- [ ] **Step 3: Create and switch to clean branch**

```bash
git checkout -b pr/meshcore-clean upstream/main
```

- [ ] **Step 4: Check out only wanted files from pr/meshcore branch**

We copy the wanted files over from the original branch using `git checkout <branch> -- <path>` syntax.

```bash
# Protocol library (entirely new)
git checkout pr/meshcore -- modemmeshcore/

# Demod plugin (entirely new)
git checkout pr/meshcore -- plugins/channelrx/demodmeshcore/

# Mod plugin (entirely new)
git checkout pr/meshcore -- plugins/channeltx/modmeshcore/

# Modified existing files
git checkout pr/meshcore -- plugins/channelrx/CMakeLists.txt
git checkout pr/meshcore -- plugins/channeltx/CMakeLists.txt
git checkout pr/meshcore -- sdrbase/webapi/webapirequestmapper.cpp
git checkout pr/meshcore -- sdrbase/webapi/webapiutils.cpp
git checkout pr/meshcore -- exports/export.h

# SoapySDR output TX fixes (modified)
git checkout pr/meshcore -- plugins/samplesink/soapysdroutput/

# SWG bindings (modified)
git checkout pr/meshcore -- swagger/sdrangel/code/qt5/client/SWGChannelActions.cpp
git checkout pr/meshcore -- swagger/sdrangel/code/qt5/client/SWGChannelActions.h
git checkout pr/meshcore -- swagger/sdrangel/code/qt5/client/SWGMeshcoreModActions.cpp
git checkout pr/meshcore -- swagger/sdrangel/code/qt5/client/SWGMeshcoreModActions.h
git checkout pr/meshcore -- swagger/sdrangel/code/qt5/client/SWGModelFactory.h
```

Note: `devices/soapysdr/devicesoapysdr.cpp` is NOT checked out yet — it will be added in Task 2 after testing.

- [ ] **Step 5: Verify no USRPOutput files leaked in**

```bash
git diff --cached --stat plugins/samplesink/usrpoutput/
```
Expected: empty (no staged changes to usrpoutput)

- [ ] **Step 6: Verify the staged diff is clean and complete**

```bash
git diff --cached --stat | wc -l
git diff --cached --stat | head -30
```
Expected: ~80 files, all from the wanted list above. No usrpoutput, no .github, no flatpak.

- [ ] **Step 7: Reconfigure cmake for new branch source tree**

After checking out files from another branch, the cmake cache may reference stale paths. Reconfigure:

```bash
cd build
cmake .. -DCMAKE_BUILD_PARALLEL_LEVEL=$(sysctl -n hw.ncpu) 2>&1 | tail -10
```
Expected: CMake configuration succeeds. New subdirectories (modemmeshcore, demodmeshcore, modmeshcore) detected.

- [ ] **Step 7b: Build all targets**

Build everything including new plugin .so files (not just sdrangel binary):

```bash
cmake --build . -j$(sysctl -n hw.ncpu) 2>&1 | tail -30
```
Expected: Build succeeds. `modemmeshcore` library, `demodmeshcore` plugin, `modmeshcore` plugin, and `sdrangel` executable built without errors.

- [ ] **Step 8: Commit the initial clean state**

```bash
git add -A
git commit -m "meshcore: add MeshCore protocol RX/TX channel plugins with SoapySDR TX fixes"
git log --oneline -3
```

- [ ] **Step 9: Record the commit SHA for rollback**

```bash
git rev-parse HEAD > ./tmp/meshcore-clean-sha.txt
cat ./tmp/meshcore-clean-sha.txt
```

---

### Task 2: Test SoapyUHD TX without MCR pinning in devicesoapysdr.cpp

**Files:**
- Create: `tests/tx_soapy_meshcore.json` (modified if refactored — see existing)
- Modify: (none — we build WITHOUT the devicesoapysdr change)

- [ ] **Step 1: Check current SoapySDROutputThread state**

Read the thread to verify it has the fixes from `f815a3628`:
- `handleInputMessages()` scheduled for DSP engine thread
- `setGain()` moved to `start()` post-activation
- `fullScale` threshold check uses `>= 2049` (not `32767 vs 32768`)
- Timed first write pattern with `getHardwareTime()` per burst

```bash
rg -n "getHardwareTime\|handleInputMessages\|fullScale\|setGain" plugins/samplesink/soapysdroutput/soapysdroutputthread.cpp | head -20
```
Expected: All four fix patterns present.

- [ ] **Step 2: Verify devicesoapysdr.cpp was NOT staged**

```bash
git diff --cached -- devices/soapysdr/devicesoapysdr.cpp
```
Expected: No staged changes (we didn't check it out in Task 1).

- [ ] **Step 3: Build without devicesoapysdr changes**

Since devicesoapysdr.cpp has NOT been modified from upstream state, no special build step needed — just ensure the binary is current:

```bash
cmake --build . --target sdrangel -j$(sysctl -n hw.ncpu) 2>&1 | tail -10
```
Expected: "Built target sdrangel" (or "Nothing to be done")

- [ ] **Step 4: Prepare TX test config**

Write a minimal TX test JSON. The key: device args must include `auto_tick_rate=0` since we're testing whether the code workaround is needed vs. user-supplied args.

```json
{
  "soapySDROutputSettings": {
    "device": "uhd",
    "device_parameter": "master_clock_rate=32e6,auto_tick_rate=0",
    "centerFrequency": 869618000,
    "sampleRate": 1000000,
    "bandwidth": 1000000,
    "gain": 73
  }
}
```

This config tells SoapyUHD to pin MCR at 32 MHz and disable auto_tick_rate via the device args string, which exercises the existing SoapySDR kwargs path without the code change in devicesoapysdr.cpp.

- [ ] **Step 5: Run sdrangel with SoapyUHD output**

**Start in a separate terminal/tmux pane** so you can send REST commands from another terminal.

In terminal 1:
```bash
cd build
./sdrangel/sdrangel -p 8091 -d 2>&1
```
Wait ~5s for startup. Expected: sdrangel starts, SoapyUHD opens device.

- [ ] **Step 6: Configure SoapySDR output via REST**

```bash
# Add a SoapySDR output deviceset
curl -s -X POST "http://localhost:8091/sdrangel/deviceset?direction=1" | python3 -m json.tool

# Set device
curl -s -X PUT "http://localhost:8091/sdrangel/deviceset/0/device" \
  -H "Content-Type: application/json" \
  -d '{"hwType":"SoapySDR","direction":1}' | python3 -m json.tool

# Apply SoapySDROutput settings (with auto_tick_rate=0 in device args)
curl -s -X PUT "http://localhost:8091/sdrangel/deviceset/0/channel/0/settings" \
  -H "Content-Type: application/json" \
  -d '{
    "channelType": "SoapySDROutput",
    "soapySDROutputSettings": {
      "device_parameter": "master_clock_rate=32e6,auto_tick_rate=0",
      "centerFrequency": 869618000,
      "sampleRate": 1000000,
      "gain": 73
    }
  }' | python3 -m json.tool
```
Expected: HTTP 200, settings applied.

- [ ] **Step 7: Add MeshCore modulator channel**

```bash
curl -s -X POST "http://localhost:8091/sdrangel/deviceset/0/channel" \
  -H "Content-Type: application/json" \
  -d '{"channelType":"MeshcoreMod","direction":1}' | python3 -m json.tool
```
Expected: HTTP 200, channel added.

- [ ] **Step 8: Send an ADVERT packet via REST sendNow action**

```bash
curl -s -X POST "http://localhost:8091/sdrangel/deviceset/0/channel/0/actions" \
  -H "Content-Type: application/json" \
  -d '{
    "channelType": "MeshcoreMod",
    "MeshcoreModActions": {
      "sendNow": 1,
      "type": "advert",
      "name": "SDRangelTest"
    }
  }' | python3 -m json.tool
```
Expected: HTTP 200, device transmits.

- [ ] **Step 9: Verify RF on spectrum analyzer**

Use TinySA or other SA to check for a transmission at 869.618 MHz with LoRa characteristics (~10 mW with gain=73).

```bash
# If using rftools capture-run
rftools capture-run --arm --calc maxh --freq 869.618M --span 1M
# (trigger TX independently)
rftools capture-run --read
```
Expected: Peak at 869.618 MHz, amplitude above noise floor.

- [ ] **Step 10: Also test with lora_trx or companion**

If available, run a companion receiver on the same frequency and check for received ADVERT packets:

```bash
# Listen on companion serial/bridge
meshcore-cli listen
# Wait for ADVERT from SDRangelTest node
```
Expected: Companion receives ADVERT with name "SDRangelTest".

- [ ] **Step 11: Interpret results**

| Outcome | Conclusion |
|---------|-----------|
| **RF confirmed** (SA peak +/or companion decodes) | `devices/soapysdr` change is **not required**. User can pass `auto_tick_rate=0` via device args. Drop from PR. |
| **No RF** (SA shows nothing, companion sees nothing) | `devices/soapysdr` change **is required**. Without it, UHD re-derives MCR during `set_tx_rate()` and decimator chain breaks. Keep in PR. |

- [ ] **Step 12: Kill sdrangel**

```bash
pkill sdrangel
# Wait for USB reset (~3s for B210)
sleep 3
```
Expected: Process killed, device released.

---

### Task 3: Decide devicesoapysdr.cpp scope

**Files:**
- Modify: `devices/soapysdr/devicesoapysdr.cpp` (only if test showed it's needed)

- [ ] **Step 1: Apply or skip devicesoapysdr changes based on test result**

**If test passed (RF confirmed without change):**
```bash
echo "MCR pinning not needed via devicesoapysdr. Dropping from PR scope."
# No action needed — devicesoapysdr.cpp is already at upstream state
```

**If test failed (no RF without change):**
```bash
# Check out the MCR pinning changes from original branch
git checkout pr/meshcore -- devices/soapysdr/devicesoapysdr.cpp
git add devices/soapysdr/devicesoapysdr.cpp
```

- [ ] **Step 2: Update PR description**

Edit `./tmp/pr-description.md`:

If devicesoapysdr change included:
- Add "MCR pinning" bullet under SoapySDR TX fixes section
- Mark devices/soapysdr/ in the commit list

If devicesoapysdr change excluded:
- Keep current description as-is (already omits it)

- [ ] **Step 3: Delete testing artifacts**

```bash
rm -f ./tmp/test_rf_confirm.json ./tmp/tx_test_output.txt
```

---

### Task 4: Finalize and commit

**Files:**
- All staged changes from Task 1 + optional Task 3 additions

- [ ] **Step 1: Verify final file list**

```bash
git diff --cached --stat | grep -v "^ " | wc -l
git diff --cached --stat | head -40
```
Expected: No `usrpoutput/`, no `.github/`, no `flatpak/`, no `debian/`, no `external/`, no `cmake/`

- [ ] **Step 2: Verify build with final file set**

```bash
cmake --build . --target sdrangel -j$(sysctl -n hw.ncpu) 2>&1 | tail -10
```
Expected: Build succeeds.

- [ ] **Step 3: Verify the SoapySDR output thread changes compile**

```bash
cmake --build . --target sdrangel -j$(sysctl -n hw.ncpu) 2>&1 | rg -i "error|warning" | head -10
```
Expected: No errors or warnings in SoapySDROutput files.

- [ ] **Step 4: Final git status check**

```bash
git status --short
```
Expected: All wanted files staged. No unknown modified files.

- [ ] **Step 5: Commit**

```bash
git diff --cached --stat
git commit
```

Write commit message (amend if needed):
```
meshcore: add MeshCore protocol RX/TX channel plugins with SoapySDR TX fixes

Add a vendor-neutral protocol library (modemmeshcore/) with full wire
packet decode/encode, Ed25519+X25519+AES-128-HMAC crypto via vendored
Monocypher and tiny-AES-c, and two SDRangel channel plugins:

  - demodmeshcore: LoRa PHY decode pipeline, multi-pipeline support
    (up to 4 parallel decode chains), preset selector with 14 MeshCore
    regional defaults, key management dialog, UDP JSON sink
  - modmeshcore: LoRa PHY encode pipeline, MESHCORE: command syntax
    for type-driven TX (advert/txt_msg/anon_req/grp_txt/ack), REST
    sendNow action via SWGMeshcoreModActions

SoapySDR TX fixes: handleInputMessages DSP thread scheduling, setGain
post-activation, CS16 fullScale threshold correction, timed first write
pattern, and TX diagnostic counters (packets/underflows/errors).
```

- [ ] **Step 6: Push (user authenticates)**

```bash
git push origin pr/meshcore-clean
```
⚠️ Requires user authentication. Not automated.

- [ ] **Step 7: Open PR via GitHub UI**

The PR is against `f4exb/sdrangel` (upstream). PR description is at `./tmp/pr-description.md`.

---

## Rollback Points

| Checkpoint | How to roll back |
|-----------|------------------|
| After Task 1 clean branch creation | `git checkout pr/meshcore && git branch -D pr/meshcore-clean` |
| After Task 1 commit | `git reset --hard HEAD~1` |
| After Task 2 test (no changes made) | No rollback needed |
| After Task 3 devicesoapysdr apply | `git reset HEAD devices/soapysdr/devicesoapysdr.cpp && git checkout -- devices/soapysdr/devicesoapysdr.cpp` |
| After Task 4 commit | `git reset --soft HEAD~1 && git restore --staged .` |
