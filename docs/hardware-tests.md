# Kindle hardware notes

These results come from a real Kindle PW4. They replace the earlier
"unverified" framing where a test has since passed. Each section states what was
observed and what remains unproven.

## Device

- Model: Kindle Paperwhite 4 (PW4)
- Firmware: 5.18.1.1.1
- Architecture: ARMv7
- C library: glibc 2.20
- Kernel: 4.1.15-lab126
- System Python: none. `python3` and `python` are not present.
- USB storage: `/mnt/us`, `fuse.fsp`, read-write
- Bluetooth device: `/dev/ttymxc2`, owned by root
- Bluetooth processes: `btd`, `bsa_server`, `btfd`

## Python runtime

A portable CPython 3.11.18 crashed at startup with SIGSEGV (exit status 139),
before it printed a version. A copy of the executable in `/tmp` also crashed
with status 139, so the failure is not caused by the `/mnt/us` mount.

A portable CPython 3.11.17 runs on the device. The `ssl`, `socket`, `ctypes`,
and `asyncio` modules import successfully. TLS connections remain untested.

Bumble 0.0.236 runs on that interpreter with the `bumble.crypto.builtin`
backend. All required Bumble modules import on the device, and the AES-CMAC
self-test passes there. The builtin backend is acceptable here because a stored
link key means the controller performs authentication.

## KOReader plugin

These plugin actions work on the device:

- Run diagnostics
- Inspect Bluetooth
- Start recording
- Stop & send
- Start HCI probe
- Stop HCI probe
- Record and send
- Export Bluetooth link keys

The resident Bluetooth session has been deployed. The operator reports one
Bluetooth handoff at session start and multiple recordings without Bluetooth
disconnects. The manual Start recording / Stop & send path has not been
hardware-tested. Offline tests use fakes and do not touch the device.

That path now opens a full-screen page driven by the daemon's state file. The
page logic is covered by `tests/koreader/voicebridge_spec.lua`, which stubs the
KOReader widgets and drives the page through preparing, recording, stopping,
sent, failed, capped and stale states. The spec does not test how the page looks
on e-ink: the partial refresh cost of a 1 Hz countdown, and whether the
inverted page ghosts, are unmeasured on the device.

The upload path was verified separately: `kindle/voice_bridge/test_upload.py`
sent a generated WAV to the Windows receiver over Wi-Fi. The plugin no longer
exposes that test as a menu action; the upload path is verified end to end but
is not part of the HCI flow.

The **Record and send** action and `kindle/voice_bridge/capture_local.py` have
now run the full chain on the device. Bumble's `serial:` transport on
`/dev/ttymxc2`, Bumble and the Audio Gateway on ARMv7, and the upload after the
Bluetooth restore all completed in one run.

## Network direction

The Kindle blocks inbound TCP connections. The working direction is Kindle to
laptop, outbound from the Kindle. The laptop listener accepts that connection.

A Windows firewall inbound rule was required for the listener. The rule is
scoped to the Kindle's IP address and the project's `python.exe` on port 18767.
Scope the rule this narrowly; do not open the port to all sources.

## Bluetooth handoff and restoration

The handoff works repeatedly. `bsa_server` was the sole holder of
`/dev/ttymxc2` in every run. The supervisor:

1. Stops `btd`.
2. Kills `bsa_server`, which frees the UART.
3. Configures the UART for raw H:4.
4. Relays raw H:4 to the connected laptop.
5. Resumes `btd`.
6. Restarts `btd` and requests Bluetooth on.

Logs show `bsa_server` reclaiming the UART each time, and the headset
reconnected afterwards. Bluetooth drops twice per run by design: once at the
handoff and once during restoration. Restoration is observed, not guaranteed. A
crash can still leave Bluetooth unusable.

## Controller access from the laptop

Bumble 0.0.236 on the laptop drove the Kindle's controller over the relay and
reported `HCI startup succeeded`. The controller is reachable through the
plaintext TCP relay.

## HFP Audio Gateway

The laptop acted as an HFP Audio Gateway and reached SCO with the headset. The
headset authenticated only when the link key bytes were reversed relative to the
Kindle pairing store. The original byte order failed with
`AUTHENTICATION_FAILURE_ERROR`. Use `tools/kindle_keys_to_bumble.py
--reverse-key-bytes` for this headset.

The export shape is now confirmed: a `<bt_devices>` root with `<device>`
elements holding `<bd_addr>`, `<link_key>` and `<key_type>`, with values written
as colon-separated hex and a trailing colon. The converter parses that shape
directly and writes both the plain and the `/P` peer key.

## Captures

Three captures were taken. The raw payload is the HCI SCO data, not a WAV.

| Run | Conditions | Result |
| --- | --- | --- |
| 1 | No call simulation | Digital silence |
| 2 | Call simulation plus downlink silence | 0.66 s of DC offset, then nothing |
| 3 | Call simulation, no downlink silence | 4.98 s at 16,000 B/s, 1,328 packets, all received correctly |

In run 3 the operator confirmed by ear that the final ~2.38 s is their voice.
All 1,328 packets were reported with status `correct`; no packet was reported as
possibly invalid, missing data, or partially lost.

## On-device capture

The first fully on-device run succeeded end to end. The Kindle read the headset
link key from `/var/local/zbluetooth/bt_devices.xml` in memory and never
exported it, performed the handoff, ran Bumble over the real UART as an HFP
Audio Gateway, captured the microphone, wrote a WAV, restored Bluetooth, and
uploaded the file to the receiver. The receiver stored a 5.003 s mono 8 kHz
16-bit WAV.

The settle detector worked live. The leading DC/frozen-spectrum artifact is
gone and the recording starts clean: mean -0.8, no DC offset, speech from
0.21 s. The resident manual path deliberately skips that detector: it records
from SCO readiness, for up to 15 seconds, and may include startup artifact or
silence at the beginning. That manual path remains unverified on hardware.

Its user interface is unverified too. The daemon publishes
`/tmp/voicebridge-bt-session.state` every 100 ms, and the plugin repaints the
page from that file. A hardware run should confirm three things: that **SPEAK
NOW** appears within about a second of the microphone going live, that the
countdown matches the real remaining time, and that the page does not ghost
badly after 15 one-second partial refreshes.

### Cue timing

SCO open is local readiness, not microphone readiness. On the first hardware
runs the operator reported the first one to two seconds of speech missing even
though the page already said **SPEAK NOW**, so the cue was firing too early.

The manual path now cues on the first packet whose AC RMS exceeds
`AUDIO_READY_RMS` (4.0 counts). Digital silence and the startup DC plateau both
read as zero AC energy, so they cannot trigger it. Everything before the cue is
trimmed from the WAV, and the 15-second cap counts only audio after the cue, so
the operator gets the full window from the moment they are told to speak. If no
signal appears within `AUDIO_READY_TIMEOUT` (4 s) the capture cues anyway, so a
quiet headset cannot stall the run.

Each manual capture now logs one line:

```text
Audio: first packet 0.31 s, first signal 2.14 s (threshold 4, peak 37.2),
first speech 2.61 s after SCO open.
```

That line is the measurement. If `first signal` is close to `first packet`, the
microphone was live all along and the earlier loss was something else. If
`first signal` is far behind and `peak` stays near zero, the headset is not
converting audio until it receives downlink audio, and `--send-silence` is the
lever to test.

## Upload configuration

`/mnt/us/kindle-voice-bridge/upload.json` holds `server_url` and `token`. Write
it once. `capture_local.py` and `bt_session.py` read it and leave it in place,
so later runs and sessions reuse it.

The bearer token therefore persists on the device in plain text on a FAT
filesystem. That is acceptable only for the trusted-LAN prototype. Delete the
file when testing is finished.

## Remaining known issues

- A 1.42 s stretch of literal digital silence runs from 2.97 s to 4.39 s. The
  gap is 94.9% exact zeros with no noise floor, which points to the headset
  gating its own microphone rather than packet loss.
- Bandwidth is narrow: 99% of the energy sits below 1,474 Hz, so sibilants are
  weak.
- The settle took 6.61 s on the device versus 2.4 s on the laptop. A run takes
  roughly 11 s of wall-clock for 5 s of audio. The device may start its
  microphone only after it receives downlink audio. `--send-silence` tests that
  hypothesis. The option is off by default and has not run on hardware.
- The one-shot path still hands Bluetooth off and restores it per run. The
  resident session is reported to hand off once and preserve the link across
  multiple recordings; the new manual audio path is not hardware-verified.
- The earlier captures began with a non-speech artifact, likely a stale or
  unfed SCO receive buffer. The on-device run did not show it.
  `tools/hfp_capture.py` discards it: a 0.40 s sliding window must show a small
  packet DC offset,
  a zero-crossing rate of at least 600/s, and an RMS of at least 60 counts
  before the capture starts recording. Replaying the three captures offline
  (`tools/replay_settle.py`) settles run 3 at 2.587 s after discarding 690
  packets and keeps the 2.393 s of speech; runs 1 and 2 never settle and would
  now fail instead of writing junk. The thresholds are derived from these three
  captures only, and the detector assumes PCM16 mono at 8 kHz.
- The laptop's own cleanup sometimes reports a timeout while the Kindle
  restoration still succeeds. The timeout is a report on the laptop side, not
  proof that restoration failed.
- The audio format is PCM16 mono at 8 kHz by strong inference (16,000 B/s and
  the requested HCI format), not by controller confirmation. The controller may
  return encoded data despite the PCM request.
- Only one headset model and one firmware version have been tested.
- The listener does not authenticate the Kindle client, and HCI is plaintext.
- The exported `bt-keys.txt` contains secret link keys. Delete it after use.
