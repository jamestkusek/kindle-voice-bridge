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

## KOReader plugin

These plugin actions work on the device:

- Run diagnostics
- Inspect Bluetooth
- Start HCI probe
- Stop HCI probe
- Export Bluetooth link keys

The upload path was verified separately: `kindle/voice_bridge/test_upload.py`
sent a generated WAV to the Windows receiver over Wi-Fi. The plugin no longer
exposes that test as a menu action; the upload path is verified end to end but
is not part of the HCI flow.

The **Record and send** action and `kindle/voice_bridge/capture_local.py` are
new and have not run on the device. They reuse the tested handoff and restore,
Bumble on the Kindle controller, the settle detector and the upload path. They
add three unproven things: Bumble's `serial:` transport on `/dev/ttymxc2`,
Bumble and the Audio Gateway running on ARMv7, and the upload immediately after
the Bluetooth restore.

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

## Remaining known issues

- The first ~2.4 s of a capture is a non-speech artifact, likely a stale or
  unfed SCO receive buffer. Speech starts only after it. `tools/hfp_capture.py`
  now discards it: a 0.40 s sliding window must show a small packet DC offset,
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
