<!-- Historical: written before implementation. Some streaming and provider
     assumptions here were superseded; see docs/architecture.md for the
     shipped design. -->

# Kindle as a Bluetooth Mic → Remote Server: Feasibility Research

**Verdict: technically possible, publicly proven on one device, but it is a hard
project with one dominant unproven risk (Wi-Fi + Bluetooth coexistence).**

---

## 1. The constraint that defines the whole project

**Kindle e-readers have no built-in microphone and no built-in speaker.**

All audio on a Kindle e-reader is Bluetooth-only, and it exists for exactly one
Amazon feature: **VoiceView** (screen-reader TTS). Amazon's own docs describe the
flow as "pair your supported Bluetooth audio device" — there is no local capture
or playback path.

Consequences for your goal:

| You wanted | Reality |
|---|---|
| "extract audio from the microphone" | The mic must be the **headset's** mic, captured over HFP/SCO |
| "send it to a remote server" | Fine — but it's the *only* thing running on the radio budget |
| High-quality audio | Impossible via HFP: **8 kHz mono (CVSD)** or **16 kHz (mSBC wideband)** |

That is call-quality audio (~64 kbps CVSD). For speech-to-text / voice commands
to a server this is actually acceptable. For music or high-fidelity it is not.

---

## 2. Why you cannot just "use the Kindle's Bluetooth"

The stock stack is closed and **output-only**:

- **8th–10th gen** (Broadcom BCM4343 over UART): `com.lab126.btfd` + `btd` /
  `bsa_server` (Broadcom BSA).
- **11th gen+** (MediaTek): Bluedroid + Amazon's closed `ace_bt` + `btmanagerd`.
- **10th gen** reportedly used BlueZ.

The `com.lab126.btfd` LIPC interface (dumped on kindlemodding.org) exposes:

```
isAudibleAvailable, DiscoverA2DP, BTplaybackData, Connect, Bond,
ListPaired, ListConnected, BTmetadata, BTstate, BTenable ...
```

Notice: **A2DP playback and pairing only. No mic, no SCO, no capture property.**
VoiceView is a one-way A2DP sink into the headset.

There is also **no ALSA device for Bluetooth** on Kindle. The `gmplay` video
player documents this explicitly — it plays audio by piping PCM into
`gst-launch-0.10 fdsrc ! mixersink`, i.e. Amazon's `audiomgrd` mixer. That path
is output-only too.

**So: to capture mic audio you must take the Bluetooth controller away from
Amazon's daemon and drive the raw HCI transport yourself.**

---

## 3. The two viable architectures

### A. Bumble userspace stack over raw HCI  ← recommended, proven

Replace the stock stack entirely and speak HCI directly to the chip.

- Transport: `/dev/ttymxc2` (Broadcom UART, 8th–10th gen) or `/dev/stpbt`
  (MediaTek, 11th gen+).
- You stop/kill `btfd` / `btd` / `bsa_server`, take the UART, and run Bumble.

This is what both of the key reference projects do.

### B. Wrap Amazon's `ace_bt` (11th gen+ only)

`Sighery/kindlebt` wraps `ace_bt`/`btmanagerd` and keeps the stock stack alive.
Cleaner, but its **public API is BLE/HID only**. However, its reverse-engineered
`compat_ace_internals.h` shows `ace_bt` *does* implement HFP:

```
ACE_BT_HFP_API_INIT / CONNECT / DISCONNECT / REGISTER /
GET_PROFILE_STATE / GET_AUDIO_STATE / CONNECT_AUDIO / DISCONNECT_AUDIO /
CHANGE_CALL_STATE
```

So SCO control exists in the firmware and is reachable — but you would be
extending `kindlebt` with undocumented call offsets and there is **no public
proof of anyone doing HFP audio this way**. Higher elegance, higher risk.

---

## 4. Existing libraries / projects that help

### Core dependency

**`google/bumble`** — Python Bluetooth stack. This is the one that matters.
Confirmed present in the cloned source:

- `bumble/hfp.py` — `AgProtocol` (Audio Gateway) **and** `HfProtocol`
  (Handsfree), full AT command layer, `AgFeature`/`HfFeature`,
  `ESCO_PARAMETERS` with `SCO_CVSD_D0/D1`, `ESCO_CVSD_S1..S4`,
  `ESCO_MSBC_T1/T2` (wideband).
- `bumble/device.py` — `ScoLink`, `device.on('sco_connection')`,
  `send_sco_sdu()`.
- `bumble/transport/` — `serial:`, `hci-socket`, `pty`, `tcp-*`, `unix`.
  `transport.open_transport('serial:/dev/ttymxc2,3000000')`.
- `apps/hci_bridge.py` — bridges a UART HCI controller to Bumble over TCP/UDP.
- `bumble/audio/io.py` — PCM source/sink abstractions, WAV helpers.

### Reference implementations (read these)

| Project | Why it matters |
|---|---|
| **`yzgpk2002/kindle8-offline-home`** | ⭐ **The only public proof of Kindle HFP mic capture.** Kindle 8 / KT3, fw 5.16.2.1.1. Has `sco_capture.py`, `record.py`, `persistent_headset.py`, `audio_lifecycle.py`. Validated: "mic input confirmed, clear human voice" via WAV. |
| **`zampierilucas/kindle-hid-passthrough`** | Best reference for the **hard part**: taking over the controller. Documents `/dev/ttymxc2` vs `/dev/stpbt`, taking the UART from `bsa_server` after chip firmware load, upstart/udev, KPM packaging, recovery. Does HID, not audio — but the takeover plumbing is solved here. |
| **`Sighery/kindlebt`** | `ace_bt` wrapper, 11th gen+, HF 5.16.3+. Use if you go route B. |
| **`LamEnder/gmplay-next`** | Proves the *output* path (`gst-launch … mixersink`) and documents that BT has no ALSA device. |
| **`KindleModding/Hotfix`, KPM, KUAL/MRPI** | Jailbreak, packaging, entry points. |
| **`NiLuJe/FBInk`** | E-ink UI if you want one. |

### Concrete Bumble call sequence (from the KT3 project)

```python
# 1. Own the controller
device = Device.with_hci('KT3 handsfree', hci.Address('F0:F1:F2:F3:F4:F5'),
                         source, sink)          # StreamPacketSource/Sink over UART
device.classic_enabled = True
device.le_enabled = False

# 2. Reuse the EXISTING bond — link keys live in
#    /var/local/zbluetooth/bt_devices.xml
device.keystore = MemoryKeyStore()
await device.keystore.update(addr, PairingKeys(link_key=..., link_key_type=...))

await device.power_on()

# 3. HFP Audio Gateway bring-up
await connection.authenticate()
await connection.encrypt()
record = await hfp.find_hf_sdp_record(connection)     # headset = HF, we = AG
mux = await rfcomm.Client(connection).start()
on_dlc(await mux.open_dlc(record[0]))                 # AT handshake -> SLC

# 4. Open the voice (SCO) link
await device.send_command(
    hci.HCI_Enhanced_Setup_Synchronous_Connection_Command(
        connection_handle=connection.handle,
        **hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.SCO_CVSD_D1].asdict()))

# 5. Capture mic
def on_sco(link):
    def receive(packet):                 # <-- packet.data is the MIC audio
        capture(packet)
        # MUST keep sending downlink, or the link can drop:
        device.host.send_sco_sdu(connection_handle=packet.connection_handle,
                                 sdu=downlink_slice.ljust(len(packet.data), b'\0'))
    link.sink = receive

device.on('sco_connection', on_sco)
device.on('sco_connection_failure', on_fail)
```

Notes learned from that codebase:

- `find_hf_sdp_record` → **you are the Audio Gateway**, the headset is the
  Handsfree. Correct role for mic capture.
- The `link.sink` callback receives **uplink (mic)** packets; you must keep
  feeding **downlink** SDUs or the headset may drop the link.
- Format: CVSD → **mono PCM16 @ 8 kHz**. `mSBC_T1/T2` gives 16 kHz wideband if
  both ends negotiate it (better for ASR, less proven on Kindle).
- The downlink/`ESCO_PARAMETERS` preset may need retrying per headset.

---

## 5. The hard, dangerous parts

### 5.1 Taking the controller (brick risk)

You must kill `btfd`/`btd` early in boot and hold the UART. Both reference
projects warn explicitly:

> "Setting up a way back in first is highly encouraged. This project touches
> Bluetooth, udev and upstart, all of which run early enough in boot that a bad
> state can leave a Kindle stuck on a white screen. Modern Kindles have no
> serial pads, so USB SSH is the only way back in."

Install **Jobman** (fw 5.16+) or **usbnetlite** for USB SSH recovery *before*
touching Bluetooth. Also save/restore the UART line state (`stty -F /dev/ttymxc2`)
— the KT3 project does exactly that.

### 5.2 Stock UI hangs

Once `btfd` is gone, the stock library calls `isAudibleAvailable` and blocks
~**10 seconds** per query, producing visible stutter and Application Errors. The
KT3 project fixed this with a small **Lua LIPC shim** (`bt-query-service.lua`)
that answers read-only queries in ~30 ms. If you don't care about the stock
reader UI, you can skip it — but expect framework errors.

### 5.3 Wi-Fi + Bluetooth coexistence  ← **the big unknown for your goal**

This is the risk that matters most for *your* specific use case, and it is
**untested by everyone**.

- 8th–10th gen Kindles use a **combo Broadcom BCM4343** (Wi-Fi and BT in one
  chip). 11th gen+ uses MediaTek CONSYS, also combo.
- The KT3 duplex project validated everything **with Wi-Fi switched off** — its
  validation table literally records "Wi-Fi both switches = 0, USB down" as the
  offline test condition.
- Nobody has published continuous SCO **plus** sustained Wi-Fi upload.

SCO reserves periodic slots (every 30 ms) and is latency-sensitive; Wi-Fi upload
on a shared front-end can starve it. Expect dropouts. Mitigations: aggressive
buffering, low-bitrate Opus (16–24 kbps), batch uploads, accept gaps, or test
2.4 GHz vs 5 GHz channel placement.

**Plan to prototype this early — it can invalidate the whole design.**

### 5.4 Power

A Kindle idles for weeks. Continuous SCO + Wi-Fi + CPU will give **hours**, not
days. Also: don't write recordings to flash in a loop (`/tmp` is RAM-backed —
the KT3 project heartbeats there specifically to avoid flash wear). Stream, don't
store.

### 5.5 ABI: softfloat vs hardfloat

- fw **< 5.16.3** = ARM **softfloat** (SF). KT3 is 5.16.2.1.1 → SF.
- fw **≥ 5.16.3** = ARM **hardfloat** (HF).

You need a Python + Bumble build matching your ABI. The KT3 project shipped an
"ARM softfloat-compatible Python 3.11 + Bumble". `kindlebt` explicitly refuses SF.
**Choosing an HF device (5.16.3+) removes a whole class of pain.**

---

## 6. Device selection

From the KindleModding model table (30 models) + BT stack notes:

| Gen | Models | BT transport | Audio-mic viability |
|---|---|---|---|
| 8th | **KT3** (Kindle 2016), KOA | BCM4343 `/dev/ttymxc2` | ✅ **Proven** (KT3, fw 5.16.2.1.1, SF) |
| 9th | KOA2 | BCM4343 UART | Likely (same path, unproven) |
| 10th | PW4, KT4, KOA3 | BCM4343 UART | Likely (same path, unproven) |
| 11th+ | PW5, KT5, KS, CS… | MediaTek `/dev/stpbt` | Possible via `ace_bt`/`kindlebt`, **no public audio proof** |

**Recommendation: a 10th-gen or 11th-gen device on fw ≥ 5.16.3 (hardfloat).**
KT3 is the only *proven* device but is softfloat — more build friction.

---

## 7. What nobody has built yet (your actual contribution)

The KT3 project does **local** record → save → replay. It does **not** stream to a
server. The missing piece is small and easy:

```
SCO sink packets (PCM16 mono 8 kHz)
   → ring buffer
   → Opus encode (~16–24 kbps)
   → HTTPS chunked POST / WebSocket to your server
   → server-side decode + ASR
```

Raw PCM16 @ 8 kHz is only 128 kbps, so bandwidth is trivial. All the difficulty
is in §5, not in the upload.

---

## 8. Recommended build plan

1. **Recovery first.** Jailbreak (WinterBreak2 for KT3/PW3/KOA; Sanctuary for
   10th/11th gen), install Hotfix + KPM, then Jobman/usbnetlite for USB SSH.
   Verify you can get back in before touching Bluetooth.
2. **Prove the transport.** Get Bumble's `hci_bridge.py` talking to
   `/dev/ttymxc2` (or `/dev/stpbt`) and read the controller's BD_ADDR / version.
   This validates chip firmware, baud rate, and UART handover.
3. **Prove SCO capture headless.** Port the KT3 `record.py` +
   `persistent_headset.py` + `sco_capture.py` sequence, but dump to a ring
   buffer and print RMS levels instead of writing WAVs. Confirm you get non-silent
   mic audio.
4. **Prove coexistence immediately.** While SCO is live, run a sustained
   `curl`/WebSocket upload. Measure SCO packet loss. **This is the go/no-go gate.**
5. **Add streaming.** Opus encode → HTTPS. Keep the stock-UI Lua shim only if you
   want the library to stay usable.
6. **Package** as a KPM package with a KUAL/scriptlet entry point.

### Honest alternative

If the goal is "a mic that streams to my server" rather than "a Kindle that does
it", a **Pi Zero 2 W / ESP32 with a BT or I2S mic** costs ~£15, has a real ALSA
stack, no jailbreak, no brick risk, and no combo-radio problem. The Kindle is
genuinely one of the worst devices for this job — it has no mic, no speaker, no
ALSA BT device, a closed output-only BT stack, and a shared Wi-Fi/BT front-end.

The Kindle route is worth it only if *the Kindle being the device* is the point.

---

## 9. Sources

- `github.com/yzgpk2002/kindle8-offline-home` — `docs/bluetooth-duplex.md`,
  `docs/architecture.md`, `docs/validation.md`, `src/app/{record,persistent_headset,sco_capture,audio_lifecycle}.py`
- `github.com/zampierilucas/kindle-hid-passthrough` — README (transport, takeover, recovery)
- `github.com/Sighery/kindlebt` — `manual/limitations.md`, `include/kindlebt/compat_ace_internals.h`
- `github.com/google/bumble` — `bumble/hfp.py`, `bumble/device.py`, `bumble/transport/`, `apps/hci_bridge.py`
- `github.com/LamEnder/gmplay-next` — README ("no ALSA device for Bluetooth", `mixersink`)
- `kindlemodding.org` — models.json (30 models, firmwares, jailbreaks),
  `com.lab126.btfd` LIPC property dump
- Amazon VoiceView-over-Bluetooth support pages
