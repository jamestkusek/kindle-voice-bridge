# Kindle Voice Bridge

This project records a Bluetooth headset microphone on a Kindle PW4 and sends
short clips to a computer. The receiver, the Kindle upload path, a laptop HFP
capture, and a fully on-device capture and upload have been verified on a real
PW4. The remaining gaps are listed in
[docs/hardware-tests.md](docs/hardware-tests.md).

This project was undertaken as part of the hiring process for the Hiya Software
Engineering Intern position.

## Start the receiver on Windows

In PowerShell, from the project directory:

```powershell
python -m pip install -e .
$env:KVB_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:KVB_STORAGE_DIR = "$env:LOCALAPPDATA\KindleVoiceBridge\recordings"
python -m uvicorn voice_bridge_server.app:create_app_from_env --factory --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/healthz` to check the receiver. Stop it with
Ctrl+C. The receiver stores accepted WAV files in `KVB_STORAGE_DIR`. Open the
files with an audio player and delete them when they are no longer needed.

## Send a WAV file

In another PowerShell window, set `KVB_TOKEN` to the token used by the receiver.
Then send a mono PCM WAV file:

```powershell
$env:KVB_SERVER_URL = "http://127.0.0.1:8000"
python kindle/voice_bridge/uploader.py sample.wav
```

The uploader leaves the source file unchanged. It does not retry failed uploads.
The receiver accepts recordings up to 15 seconds by default.

The receiver command binds to the computer only. The Kindle cannot connect to
this address. A LAN test requires a trusted network and explicit approval because
HTTP exposes the token and audio on the network. After approval, bind the
receiver to its LAN address. Set `KVB_SERVER_URL` to that address and set
`KVB_ALLOW_INSECURE_LAN=1` on the client. Do not expose this receiver directly to
the internet. Use verified HTTPS for a remote server.

## Laptop HCI dependency

The laptop tools need Bumble 0.0.236. Bumble also runs on the Kindle's bundled
Python 3.11.17 with the `bumble.crypto.builtin` backend. Install the optional
dependency from the project directory:

```powershell
python -m pip install -e '.[hci]'
```

Bumble `power_off()` does not restore the Kindle's stock Bluetooth. Review
[docs/recovery.md](docs/recovery.md) and explicitly authorize the handoff before
an HCI test.

## Working end-to-end flow

The laptop is the HFP Audio Gateway. The Kindle relays raw HCI from its Bluetooth
UART to the laptop over one outbound TCP connection. The Kindle blocks inbound
TCP, so the laptop must listen and the Kindle must connect out.

1. Convert the exported Kindle pairing store into a Bumble key store. This
   headset authenticated only with the key bytes reversed:

   ```powershell
   python tools/kindle_keys_to_bumble.py bt_devices.xml `
     --reverse-key-bytes -o .data/keys.json
   ```

   The converter reads the `<bt_devices>` export, writes each peer under both
   its plain address and Bumble's `AA:BB:CC:DD:EE:FF/P` form, and prints
   addresses only. The listener searches every namespace, so `--namespace` is
   not needed. Write the key store under `.data/`, which `.gitignore` excludes.

2. Start the laptop listener first. It waits up to 180 seconds for the Kindle:

   ```powershell
   python tools/hfp_capture.py `
     --host <laptop-wifi-ip> --port 18767 `
     --headset <headset-address> --output capture.raw `
     --keystore .data/keys.json --simulate-call `
     --acknowledge-controller-reset --acknowledge-unencrypted-hci `
     --authorize-handoff --authorize-microphone-recording
   ```

   The four `--acknowledge-*` and `--authorize-*` flags are required. Wait for
   `The HCI listener is ready.` before you touch the Kindle.

3. On the Kindle, open the Voice Bridge plugin and select **Start HCI probe**.
   The Kindle connects out to the laptop, hands off the Bluetooth UART, and
   relays raw H:4. Bluetooth drops twice per run by design and is restored
   afterwards. Use **Stop HCI probe** if the run does not end on its own.

   The plugin exposes these actions:

   - Run diagnostics
   - Inspect Bluetooth
   - Check Bumble runtime
   - Start Bluetooth session
   - Stop Bluetooth session
   - Start recording
   - Stop & send
   - Record and send
   - Start HCI probe
   - Stop HCI probe
   - Export Bluetooth link keys

   Start Bluetooth session, Record and send, Start HCI probe, and Export ask
   for confirmation first. The other actions only read device state or stop a
   running session.

4. The listener records five seconds of usable audio to `capture.raw` and
   writes the packet metadata to `capture.raw.json` next to it. Convert it:

   ```powershell
   python tools/raw_to_wav.py capture.raw 8000 capture.wav
   python tools/clean_raw_wav.py capture.raw 8000 capture-clean.wav
   ```

   `clean_raw_wav.py` removes the DC offset and normalises the peak. The
   capture itself now drops the leading non-speech artifact: `hfp_capture.py`
   holds packets until a 0.40 s window looks like live audio, then records the
   requested five seconds of usable audio. The JSON sidecar records the settle
   time, the discarded packet count, and the criterion values. If the stream
   never settles within 10 seconds the capture fails and writes no audio.
   `--no-settle` restores the old fixed five-second behaviour; on those files
   add `--start 2.4` to `clean_raw_wav.py` to trim the artifact by hand. The
   format is PCM16 mono 8 kHz by strong inference, not by controller
   confirmation, and the detector assumes that format.

   To check the detector against an existing capture without touching it:

   ```powershell
   python tools/replay_settle.py .data/capture3.raw .data/capture.raw
   ```

### On-device capture

`kindle/voice_bridge/capture_local.py` records and sends in one step, with no
laptop. It reads the headset link key from the Kindle pairing store, performs
the Bluetooth handoff, runs the HFP Audio Gateway with Bumble on the device,
writes a mono 8 kHz 16-bit PCM WAV in `/tmp`, restores Bluetooth, uploads the
WAV, and deletes it.

This path is verified end to end on hardware. The first fully on-device run read
the link key from `/var/local/zbluetooth/bt_devices.xml` in memory (never
exported), performed the handoff, ran Bumble over the real UART, captured the
microphone, wrote a 5.003 s mono 8 kHz 16-bit WAV, restored Bluetooth, and
uploaded it. The receiver stored the file.

The settle detector worked live: the recording starts clean with no leading DC
artifact (mean -0.8, speech from 0.21 s). Known limits are a 1.42 s stretch of
digital silence from 2.97 s to 4.39 s, narrow bandwidth (99% of energy below
1,474 Hz), a 6.61 s device settle that makes a run about 11 s of wall-clock for
5 s of audio, and the two Bluetooth drops per run. See
[docs/hardware-tests.md](docs/hardware-tests.md).

Deploy steps:

1. Copy `capture_local.py`, `pw4_supervisor.py` and `uploader.py` to
   `/mnt/us/kindle-voice-bridge/`.
2. Write `/mnt/us/kindle-voice-bridge/upload.json` once with
   `{"server_url": "...", "token": "..."}`. The script keeps this file, so
   later runs reuse it.
3. Start it from a device shell, or use **Record and send** in the plugin.

```sh
cd /mnt/us/kindle-voice-bridge
PYTHONHOME=/mnt/us/kindle-voice-bridge/python31117 \
PYTHONPATH=/mnt/us/kindle-voice-bridge/bumble \
LD_LIBRARY_PATH=/mnt/us/kindle-voice-bridge/python31117/lib \
/mnt/us/kindle-voice-bridge/python31117/bin/python3.11 -u -B \
  capture_local.py --config upload.json \
  --authorize-bluetooth-handoff --authorize-microphone-recording \
  --confirm-bluetooth-enabled-in-ui
```

Add `--headset 95:05:BB:2A:34:2E` when the pairing store holds more than one
device. The script prints one line that starts with `Result: `. The plugin
reads that line and shows it.

### Resident Bluetooth session

`kindle/voice_bridge/bt_session.py` is a manually started daemon. It performs
the Bluetooth handoff once at startup. It keeps Bumble powered on and owns the
Bluetooth UART for the whole session. Each recording then opens and closes only
the SCO audio link. The daemon does not run the handoff again, does not call
`power_on` again, and does not restore Bluetooth between recordings. Bluetooth
drops once at session start and once at session end, not twice per recording.

The resident session's single handoff and repeated recordings have been
reported working on the device. The new manual Start recording / Stop & send
path has not been hardware-tested yet; treat it as unverified until tested.
Offline tests use fakes and do not touch Bluetooth hardware.

Deploy steps:

1. Copy `bt_session.py`, `capture_local.py`, `pw4_supervisor.py` and
   `uploader.py` to `/mnt/us/kindle-voice-bridge/`.
2. Write `/mnt/us/kindle-voice-bridge/upload.json` once with
   `{"server_url": "...", "token": "..."}`. The daemon keeps this file, so
   later sessions and runs reuse it.
3. Start the session from the plugin, or from a device shell:

   ```sh
   cd /mnt/us/kindle-voice-bridge
   PYTHONHOME=/mnt/us/kindle-voice-bridge/python31117 \
   PYTHONPATH=/mnt/us/kindle-voice-bridge/bumble \
   LD_LIBRARY_PATH=/mnt/us/kindle-voice-bridge/python31117/lib \
   /mnt/us/kindle-voice-bridge/python31117/bin/python3.11 -u -B \
     bt_session.py --config upload.json \
     --authorize-bluetooth-handoff --authorize-microphone-recording \
     --confirm-bluetooth-enabled-in-ui
   ```

   Add `--headset 95:05:BB:2A:34:2E` when the pairing store holds more than one
   device. Add `--idle-timeout 600` to change the idle limit. Add
   `--keep-latency` to keep the CPU latency hold for the whole session. Add
   `--send-silence` to send paced silent downlink audio during each capture.
   The default is off.

   Start one session at a time. A second start fails before the Bluetooth
   handoff.

Manual recording:

- Select **Start recording** after starting the Bluetooth session. A full-screen
  page opens. It shows **PREPARING** while SCO opens, then inverts to a black
  page with **SPEAK NOW** and a countdown from 15. Tap anywhere to stop and
  send; the page then shows **SENDING**, then **SENT** or **NOT SENT** with the
  result. Tap again to close.
- The page is driven by the daemon, not by the plugin's own clock. **SPEAK NOW**
  appears only after the daemon reports that SCO is open and its audio callback
  is installed, and the countdown is the daemon's remaining time. If the daemon
  stops writing its state file, the page shows **NO SESSION** instead of a
  stale countdown.
- The capture begins as soon as SCO is ready and stores packets from the first
  callback, without the settle detector. It is capped at 15 seconds and
  auto-stops/sends at the cap. The beginning may contain startup artifact or
  silence; this is the trade-off for not waiting for the settle detector.
- **Record and send** remains the fixed five-second convenience action and does
  not open the page.

Control protocol:

- The daemon listens on the Unix domain socket
  `/tmp/voicebridge-bt-session.sock`. The socket mode is 0600. On Linux the
  daemon also checks `SO_PEERCRED` and accepts only the same user ID. The
  Kindle kernel 4.1 supports `AF_UNIX`. All Kindle processes may run as root,
  so the owner check is weak on this device. Do not expose the socket over the
  network.
- The protocol is one ASCII line in and one ASCII line out. Send
  `start_recording`, `stop_recording`, `record`, `status`, `phase`, `state`,
  `result`, or `stop`.
- `start_recording` replies `ok recording` only after SCO is open and the audio
  callback is installed. `stop_recording` stops capture, uploads once, and
  returns the result. `record` remains the fixed-duration convenience path.
- `status` and `phase` report `idle`, `preparing`, `recording`, `sending`, or
  `stopping`. For manual capture, `recording` means SCO is ready; it does not
  mean the settle detector accepted audio. `result` retrieves the latest result
  after automatic 15-second stop/send.
- `state` returns `ok <phase> <elapsed_ms> <remaining_ms> <link> <result>`. The
  two clocks are `-1` when the capture length is not known in advance, which is
  the case for the legacy `record` command. `result` is `-` until a recording
  finishes. The plugin's recording page reads the same fields from a file.
- While the session runs, the daemon rewrites
  `/tmp/voicebridge-bt-session.state` every 100 ms as
  `v1 <heartbeat> <phase> <elapsed_ms> <remaining_ms> <link> <result>`, mode
  0600. The heartbeat counts up on every write, so a reader can tell a live
  session from a stale file. The plugin polls this file instead of spawning a
  client process for every update, and the daemon removes it when it stops.
- `stop` replies with `ok stopping`. The daemon then restores Bluetooth and
  exits.
- The daemon handles each control command in its own task. A `stop` command
  waits for a running upload to finish. A `status` or `phase` command can time
  out during an upload.
- The client mode uses only the Python standard library:

  ```sh
  ... bt_session.py --client status
  ... bt_session.py --client phase
  ... bt_session.py --client state
  ... bt_session.py --client start_recording
  ... bt_session.py --client stop_recording
  ... bt_session.py --client record
  ... bt_session.py --client stop
  ```

The daemon reads `upload.json` and keeps it for later sessions. The plugin
shows the daemon reply.
The plugin uses the daemon for **Record and send** when a session is running.
It uses the one-shot `capture_local.py` path when no session runs.

### Capture latency experiment

Some handsets may start their microphone only after they receive downlink
audio. `--send-silence` sends paced silent PCM downlink packets during each
capture. The option is off by default, so the current behaviour is
reproducible.

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
replaces that message with "Recording — speak now" when the settle detector
accepts audio. Each recording writes one line to the daemon log. When the
plugin starts the session, the log is `bt-session.log`:

```text
Record: silence on, kept 5.00 s, settle 2.10 s, 2.55 s after the SCO link opened.
```

Compare that line with a run without `--send-silence`. The one-shot path has
the same flag:

```sh
... capture_local.py --config upload.json --send-silence ...
```

The one-shot path prints `Downlink silence used: yes`, the settle time, and the
settle time measured from the SCO link open.

Idle and power behaviour:

- The default idle limit is 600 seconds. The daemon restores Bluetooth after
  that limit without a recording.
- The daemon releases the CPU latency hold between recordings and re-acquires
  it for each capture. The tests do not cover this with the controller powered.
  If a recording fails after a long idle period, start the session with
  `--keep-latency`.

Restore and recovery:

- The daemon restores Bluetooth on `stop`, on the idle limit, on SIGTERM, on
  SIGINT, and on an unexpected Python error.
- The daemon cannot restore Bluetooth after SIGKILL, SIGSEGV, a kernel hang,
  or a power loss. Stock Bluetooth remains off. Reboot, or try the manual
  recovery commands:

  ```sh
  printf 0 > /proc/bluetooth/sleep/proto
  printf 0 > /proc/bluetooth/sleep/btwake
  printf 0 > /proc/bluetooth/btenable
  initctl restart btd
  lipc-set-prop com.lab126.btfd BTenable 1:1
  ```

  These commands are not a guarantee. Reboot is not guaranteed to restore
  Bluetooth.
- If the headset drops during the session, the next `record` reconnects with
  `device.connect`. It does not run the handoff and does not call `power_on`.
  If the controller itself stops answering, stop the session and start it
  again.

Remaining risks:

- Bumble stays resident with the controller powered. A Bumble crash or a
  controller hang can leave the session unusable. The restore path then runs
  only if the Python process can still execute it.
- A headset drop during a capture fails that recording. The session stays
  alive, and the next recording reconnects.
- A SIGKILL or a hard crash leaves Bluetooth off. The socket and lock file
  become stale. The next start removes the stale socket. You must restore the
  stock Bluetooth daemon first.
- The control socket is local only. Its owner check is weak when all processes
  run as root.
- The WAV file is private. The daemon writes it to `/tmp` with mode 0600 and
  deletes it after the upload. A crash during the upload can leave the file in
  `/tmp`.
- `bt-session.log` can contain the headset address and error type names. It
  does not contain the link key or the token. Delete it after use.

### Transcription

The receiver transcribes each recording as it arrives. Set `OPENROUTER_API_KEY`
before starting it and a background task sends the audio to OpenRouter after the
201 is sent. It writes `<id>.txt` and `<id>.transcript.json` next to the WAV and
never touches the audio. Without the key, transcription is off and the upload
path is unchanged.

```powershell
$env:OPENROUTER_API_KEY = "<key>"
python -m uvicorn voice_bridge_server.app:create_app_from_env --factory --host 0.0.0.0 --port 18767
```

`tools/transcribe_worker.py --once` backfills a storage directory the receiver
did not transcribe. Do not run it in a loop while the receiver hook is enabled.

The default model is `mistralai/voxtral-small-24b-2507`, a purpose-built ASR
model and the cheapest audio rate OpenRouter lists. OpenRouter has no
transcription endpoint, so the audio travels as an `input_audio` part of a chat
completion. [docs/transcription.md](docs/transcription.md) records the model
comparison, the cost per clip, and what to try if 8 kHz audio transcribes
poorly.

### Security notes

- HCI over the relay is plaintext, and the listener does not authenticate the
  Kindle client. Use a trusted network only.
- The exported `bt-keys.txt` holds secret link keys that can impersonate the
  Kindle's Bluetooth identity. Delete `/mnt/us/kindle-voice-bridge/bt-keys.txt`
  from the Kindle after use, and delete any local key store copies.
- The Windows firewall inbound rule for port 18767 is scoped to the Kindle IP
  and the project's `python.exe`. Remove the rule when you finish.
- Raw captures may contain private microphone audio. `.gitignore` excludes
  `*.wav` and `.data/`; keep captures out of version control.
- `upload.json` holds the bearer token in plain text on the device's FAT
  filesystem. The on-device scripts keep it so later runs can reuse it. This is
  acceptable only for the trusted-LAN prototype. Delete the file when testing
  is finished.
- Transcription sends the audio to OpenRouter, a third party. The receiver stays
  local, but the speech does not. Transcripts are written in plain text next to
  the recordings and are not redacted. Do not transcribe recordings you would
  not upload.

The upload contract is in [docs/protocol.md](docs/protocol.md). The project plan
and hardware precautions are in [PLAN.md](PLAN.md).
