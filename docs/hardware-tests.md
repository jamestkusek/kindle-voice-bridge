# Kindle hardware notes

These results come from a real Kindle Paperwhite 10th edition. They replace the
earlier "unverified" framing where a test has since passed. Each section states
what was observed and what remains unproven.

## Device

- Model: Kindle Paperwhite 10th edition
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

A Windows firewall rule for inbound port 18767 was required for the listener.
The rule is scoped to the Kindle's IP address and the project's `python.exe` on
port 18767. Scope the rule this narrowly; do not open the port to all sources.

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

## Laptop capture commands

Convert the exported pairing store into a Bumble key store. This headset
authenticated only with the key bytes reversed:

```powershell
python tools/kindle_keys_to_bumble.py bt_devices.xml `
  --reverse-key-bytes -o .data/keys.json
```

The converter reads the `<bt_devices>` export. It writes each peer under both
its plain address and the Bumble `AA:BB:CC:DD:EE:FF/P` form. It prints addresses
only. The listener searches every namespace, so `--namespace` is not needed.
Write the key store under `.data/`, which `.gitignore` excludes.

Start the laptop listener first. It waits up to 180 seconds for the Kindle:

```powershell
python tools/hfp_capture.py `
  --host <laptop-wifi-ip> --port 18767 `
  --headset <headset-address> --output capture.raw `
  --keystore .data/keys.json --simulate-call `
  --acknowledge-controller-reset --acknowledge-unencrypted-hci `
  --authorize-handoff --authorize-microphone-recording
```

The four `--acknowledge-*` and `--authorize-*` flags are required. Wait for
`The HCI listener is ready.` before the next step on the Kindle.

The listener records five seconds of usable audio to `capture.raw`. It writes
the packet metadata to `capture.raw.json` next to the file. Convert the capture:

```powershell
python tools/raw_to_wav.py capture.raw 8000 capture.wav
python tools/clean_raw_wav.py capture.raw 8000 capture-clean.wav
```

`clean_raw_wav.py` removes the DC offset and normalises the peak.
`hfp_capture.py` holds packets until a 0.40 s window looks like live audio. It
then records the requested five seconds of usable audio. The JSON sidecar
records the settle time, the discarded packet count, and the criterion values.
If the stream never settles within 10 seconds, the capture fails and writes no
audio. `--no-settle` restores the old fixed five-second behaviour. On those
files, add `--start 2.4` to `clean_raw_wav.py` to trim the artifact by hand. The
format is PCM16 mono 8 kHz by strong inference. The controller has not confirmed
the format. The detector assumes that format.

To check the detector against an existing capture without touching it:

```powershell
python tools/replay_settle.py .data/capture3.raw .data/capture.raw
```

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

### Send-silence experiment

Some handsets start the microphone only after they receive downlink audio.
`--send-silence` sends paced silent PCM downlink packets during each capture.
The option is off by default, so the current behaviour is reproducible.

Start one session with the option:

```sh
cd /mnt/us/kindle-voice-bridge
PYTHONHOME=/mnt/us/kindle-voice-bridge/python31117 \
PYTHONPATH=/mnt/us/kindle-voice-bridge/bumble \
LD_LIBRARY_PATH=/mnt/us/kindle-voice-bridge/python31117/lib \
/mnt/us/kindle-voice-bridge/python31117/bin/python3.11 -u -B \
  bt_session.py --config upload.json --send-silence \
  --authorize-bluetooth-handoff --authorize-microphone-recording \
  --confirm-bluetooth-enabled-in-ui
```

Then select **Record and send** in the plugin. The plugin shows
"Preparing the microphone…" when the run starts. In a Bluetooth session, it
replaces that message with a recording prompt when the settle detector accepts
audio. Each recording writes one line to the daemon log. When the plugin starts
the session, the log is `bt-session.log`:

```text
Record: silence on, kept 5.00 s, settle 2.10 s, 2.55 s after the SCO link opened.
```

Compare that line with a run without `--send-silence`. The one-shot path has the
same flag:

```sh
cd /mnt/us/kindle-voice-bridge
PYTHONHOME=/mnt/us/kindle-voice-bridge/python31117 \
PYTHONPATH=/mnt/us/kindle-voice-bridge/bumble \
LD_LIBRARY_PATH=/mnt/us/kindle-voice-bridge/python31117/lib \
/mnt/us/kindle-voice-bridge/python31117/bin/python3.11 -u -B \
  capture_local.py --config upload.json --send-silence \
  --authorize-bluetooth-handoff --authorize-microphone-recording \
  --confirm-bluetooth-enabled-in-ui
```

The one-shot path prints `Downlink silence used: yes`, the settle time, and the
settle time measured from the SCO link open.

## Resident session protocol

The daemon listens on the Unix domain socket
`/tmp/voicebridge-bt-session.sock`. The socket mode is 0600. On Linux the daemon
also checks `SO_PEERCRED` and accepts only the same user ID. The Kindle kernel
4.1 supports `AF_UNIX`. All Kindle processes may run as root, so the owner check
is weak on this device. Do not expose the socket over the network.

The protocol is one ASCII line in and one ASCII line out. The client sends
`start_recording`, `stop_recording`, `record`, `status`, `phase`, `state`,
`result`, or `stop`.

- `start_recording` replies `ok recording` only after SCO is open and the audio
  callback is installed.
- `stop_recording` stops the capture, uploads once, and returns the result.
- `record` is the fixed-duration convenience path.
- `status` and `phase` report `idle`, `preparing`, `recording`, `sending`, or
  `stopping`. For a manual capture, `recording` means that SCO is ready. It does
  not mean that the settle detector accepted audio.
- `result` retrieves the latest result after the automatic 15 s stop and send.
- `state` returns `ok <phase> <elapsed_ms> <remaining_ms> <link> <result>`. The
  two clocks are `-1` when the capture length is not known in advance. This is
  the case for the legacy `record` command. `result` is `-` until a recording
  finishes. The recording page of the plugin reads the same fields from a file.
- `stop` replies `ok stopping`. The daemon restores Bluetooth and exits.
- The daemon handles each control command in its own task. A `stop` command
  waits for a running upload. A `status` or `phase` command can time out during
  an upload.

While the session runs, the daemon rewrites
`/tmp/voicebridge-bt-session.state` every 100 ms. The format is
`v1 <heartbeat> <phase> <elapsed_ms> <remaining_ms> <link> <result>`. The mode
is 0600. The heartbeat counts up on every write, so a reader can tell a live
session from a stale file. The plugin polls this file instead of spawning a
client process for every update. The daemon removes the file when it stops.

The client mode uses only the Python standard library:

```sh
... bt_session.py --client status
... bt_session.py --client phase
... bt_session.py --client state
... bt_session.py --client start_recording
... bt_session.py --client stop_recording
... bt_session.py --client record
... bt_session.py --client stop
```

The daemon reads `upload.json` and keeps the file for later sessions. The plugin
shows the daemon reply. The plugin uses the daemon for **Record and send** when
a session runs. The plugin uses the one-shot `capture_local.py` path when no
session runs.

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
