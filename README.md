# Kindle Voice Bridge

Kindle Voice Bridge records the microphone of a Bluetooth headset on a Kindle
Paperwhite 10th edition. It sends each short recording to a computer. The
computer stores the recording, plays it in a review page, and can transcribe it
through OpenRouter.

The receiver, the Kindle upload path, the laptop HFP capture, and the on-device
capture have passed tests on a real Paperwhite 10th edition. The remaining gaps
are in [docs/hardware-tests.md](docs/hardware-tests.md).

The project was part of the hiring process for the Hiya Software Engineering
Intern position.

The Kindle has no microphone and no speaker. It supports Bluetooth audio for
VoiceView, and a Bluetooth headset has a microphone. This project captures that
microphone and sends the audio to a computer.

## Requirements

- A computer with Windows and Python 3.11 or later.
- A Kindle Paperwhite 10th edition with firmware 5.18.1.1.1.
- A Bluetooth headset.
- A USB cable.
- A trusted local network for a LAN test.
- A portable CPython 3.11.17 and Bumble 0.0.236 at
  `/mnt/us/kindle-voice-bridge/` on the Kindle.

The Kindle has no system Python. The commands `python3` and `python` are not
present on the device.

## Quick start

Run the commands from the project directory in PowerShell.

1. Install the receiver:

   ```powershell
   python -m pip install -e .
   ```

2. Start the receiver:

   ```powershell
   .\tools\kvb.ps1 receiver
   ```

3. Open `http://127.0.0.1:18767/healthz` in a browser. The receiver answers when
   it runs.

4. Connect the USB cable.

5. Copy the device files to the mounted Kindle:

   ```powershell
   .\tools\kvb.ps1 deploy -KindleRoot D:\ -ServerUrl http://<laptop-ip>:18767
   ```

6. Run the tests:

   ```powershell
   .\tools\kvb.ps1 test
   ```

The `receiver` command reads the token from `.data\receiver-config.json`. When
that file is absent, the command reads the token from the Kindle `upload.json`.
When both files are absent, the command writes a new token to
`.data\receiver-config.json`. The script never prints the token.

The `receiver` command sets `KVB_TOKEN`, `KVB_STORAGE_DIR`, and
`KVB_ALLOW_INSECURE_LAN`. It sets `OPENROUTER_API_KEY` from
`server/voice_bridge_server/voxtral-openrouter-key.txt` when that file exists.
Without the key file, transcription is off. The receiver stores accepted WAV
files in `%LOCALAPPDATA%\KindleVoiceBridge\recordings`.

The `receiver` command listens on all interfaces by default. HTTP exposes the
token and the audio on the network. Use a trusted network only. Add `-LocalOnly`
to bind `127.0.0.1`.

The `deploy` command copies `capture_local.py`, `bt_session.py`,
`pw4_supervisor.py`, `uploader.py`, `hci_relay.py`, and `launch_hci.py` to
`/mnt/us/kindle-voice-bridge/`. It copies the KOReader plugin to
`/mnt/us/koreader/plugins/voicebridge.koplugin/`. It writes `upload.json` when
`-ServerUrl` is present. It does not copy the Python runtime or the Bumble
bundle.

The `transcriber` command starts `tools/transcribe_worker.py --interval 5` in
the background. The command needs the key file. The `test` command runs
`python -m pytest -q`. Run `.\tools\kvb.ps1 help` for the full option list.

If the PowerShell execution policy blocks the script, run it with
`powershell -NoProfile -ExecutionPolicy Bypass -File tools\kvb.ps1 receiver`.
Stop the receiver with `Stop-Process -Id <pid>`. The `receiver` command prints
the process ID.

### Start without the script

In PowerShell, from the project directory:

```powershell
python -m pip install -e .
$env:KVB_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:KVB_STORAGE_DIR = "$env:LOCALAPPDATA\KindleVoiceBridge\recordings"
python -m uvicorn voice_bridge_server.app:create_app_from_env --factory --host 127.0.0.1 --port 18767
```

Open `http://127.0.0.1:18767/healthz` to check the receiver. Stop the receiver
with Ctrl+C. Open the stored files with an audio player. Delete the files when
they are no longer needed.

The manual command binds to the computer only. The Kindle cannot connect to this
address. A LAN test requires a trusted network and explicit approval because
HTTP exposes the token and the audio on the network.

After approval, bind the receiver to its LAN address. Set `KVB_SERVER_URL` to
that address. Set `KVB_ALLOW_INSECURE_LAN=1` on the client. Do not expose this
receiver directly to the internet. Use verified HTTPS for a remote server.

## Send a WAV file

In another PowerShell window, set `KVB_TOKEN` to the token of the receiver. Then
send a mono PCM WAV file:

```powershell
$env:KVB_SERVER_URL = "http://127.0.0.1:18767"
python kindle/voice_bridge/uploader.py sample.wav
```

The uploader leaves the source file unchanged. It does not retry a failed
upload.

## How a recording flows

The computer is the HFP Audio Gateway. The Kindle relays raw HCI from its
Bluetooth UART to the computer over one outbound TCP connection. The Kindle
blocks inbound TCP. The computer listens and the Kindle connects out.

The laptop tools need Bumble 0.0.236. Install the optional dependency from the
project directory:

```powershell
python -m pip install -e '.[hci]'
```

Bumble also runs on the bundled Python 3.11.17 of the Kindle with the
`bumble.crypto.builtin` backend.

Bumble `power_off()` does not restore the stock Bluetooth of the Kindle. Read
[docs/recovery.md](docs/recovery.md). Authorize the handoff before an HCI test.

### Laptop HCI capture

The commands for the key conversion, the listener, and the WAV conversion are in
[docs/hardware-tests.md](docs/hardware-tests.md). Start the listener first. Wait
for `The HCI listener is ready.` before the step on the Kindle.

On the Kindle, open the Voice Bridge plugin. Select **Start HCI probe**. The
Kindle connects out to the laptop, hands off the Bluetooth UART, and relays raw
H:4. Bluetooth drops twice per run by design and is restored afterwards. Use
**Stop HCI probe** if the run does not end on its own.

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

Start Bluetooth session, Record and send, Start HCI probe, and Export ask for
confirmation first. The other actions only read device state or stop a running
session.

## Device side

The Bluetooth handoff stops the stock Bluetooth daemon. It can leave Bluetooth
unusable. Recovery is not guaranteed.

### One-shot capture

`kindle/voice_bridge/capture_local.py` records and sends one clip. The script
needs no computer. It performs these steps:

1. It reads the headset link key from the Kindle pairing store.
2. It performs the Bluetooth handoff.
3. It runs the HFP Audio Gateway with Bumble on the device.
4. It writes a mono 8 kHz 16-bit PCM WAV in `/tmp`.
5. It restores Bluetooth.
6. It uploads the WAV.
7. It deletes the file.

This path has passed an end-to-end test on hardware. The first run read the link
key from `/var/local/zbluetooth/bt_devices.xml` in memory. It did not export the
key. It wrote a 5.003 s WAV, and the receiver stored the file.

The settle detector worked in that run. The recording started clean with no
leading DC artifact. The mean was -0.8, and speech started at 0.21 s.

Known limits are a 1.42 s stretch of digital silence from 2.97 s to 4.39 s. The
bandwidth is narrow, with 99% of energy below 1,474 Hz. A run also has a 6.61 s
device settle and two Bluetooth drops. A run takes about 11 s of wall-clock for
5 s of audio. See [docs/hardware-tests.md](docs/hardware-tests.md).

Deploy steps:

1. Connect the USB cable.
2. Run `.\tools\kvb.ps1 deploy -KindleRoot D:\ -ServerUrl http://<laptop-ip>:18767`.
   The command copies `capture_local.py`, `pw4_supervisor.py`, `uploader.py`,
   `hci_relay.py`, and `launch_hci.py` to `/mnt/us/kindle-voice-bridge/`.
3. Write `/mnt/us/kindle-voice-bridge/upload.json` once with
   `{"server_url": "...", "token": "..."}`. The `deploy` command writes the file
   when `-ServerUrl` is present. The script keeps the file, so later runs reuse
   it.
4. Start the capture from a device shell, or select **Record and send** in the
   plugin.

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
device. The script prints one line that starts with `Result: `. The plugin reads
that line and shows the result.

### Resident session

`kindle/voice_bridge/bt_session.py` is a daemon that the operator starts by
hand. It performs the Bluetooth handoff once at startup. It keeps Bumble powered
and owns the Bluetooth UART for the whole session. Each recording opens and
closes only the SCO audio link. The daemon does not run the handoff again, does
not call `power_on` again, and does not restore Bluetooth between recordings.
Bluetooth drops once at session start and once at session end.

The operator reported that the single handoff and the repeated recordings work
on the device. The manual Start recording and Stop & send path has produced
recordings and uploads on the device. The first runs cued the operator too
early, so the start of the speech was lost. The current detector has not been
tested on hardware. The offline tests use fakes and do not touch Bluetooth
hardware.

Deploy steps:

1. Use the deploy steps for the one-shot capture. Add `bt_session.py` to the
   copy list.
2. Start the session from the plugin, or from a device shell:

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
device. Add `--idle-timeout 600` to change the idle limit. Add `--keep-latency`
to keep the CPU latency hold for the whole session. Add `--send-silence` to send
paced silent downlink audio during each capture. The default is off.

Start one session at a time. A second start fails before the Bluetooth handoff.

### Manual recording page

- Select **Start recording** after the session starts. A full-screen page opens.
- The page shows **PREPARING** while SCO opens. It then changes to a black page
  with **SPEAK NOW** and a countdown from 15.
- Tap anywhere to stop and send. The page shows **SENDING**, then **SENT** or
  **NOT SENT** with the result. Tap again to close.
- The daemon drives the page. The plugin does not use its own clock.
- **SPEAK NOW** appears only after the daemon detects that the microphone is
  converting audio. SCO open is not sufficient, because the receive path
  delivers about 2.5 s of startup artifact before usable audio. The countdown
  is the remaining time from the daemon.
- The page shows **NO SESSION** when the daemon stops writing the state file. It
  does not show a stale countdown.
- The capture stores packets from the first callback and does not use the
  settle detector. The daemon trims every packet before the cue, so the file
  starts where the microphone became live. The 15 s cap counts only audio after
  the cue.
- The daemon waits up to 4 s for a signal. If no signal appears, it cues
  anyway and records from that point.
- **Record and send** stays as the fixed five-second action. It does not open
  the page.

### Control protocol

The daemon listens on the Unix domain socket
`/tmp/voicebridge-bt-session.sock`. The socket mode is 0600. On Linux the daemon
also checks `SO_PEERCRED` and accepts only the same user ID. The Kindle kernel
4.1 supports `AF_UNIX`. All Kindle processes may run as root, so the owner check
is weak on this device. Do not expose the socket over the network.

The protocol is one ASCII line in and one ASCII line out. The client sends
`start_recording`, `stop_recording`, `record`, `status`, `phase`, `state`,
`result`, or `stop`. The replies and the client mode are in
[docs/hardware-tests.md](docs/hardware-tests.md).

While the session runs, the daemon rewrites
`/tmp/voicebridge-bt-session.state` every 100 ms. The format is
`v1 <heartbeat> <phase> <elapsed_ms> <remaining_ms> <link> <result>`. The mode
is 0600. The heartbeat counts up on every write, so a reader can tell a live
session from a stale file. The plugin polls this file instead of spawning a
client process for every update. The daemon removes the file when it stops.

The daemon reads `upload.json` and keeps the file for later sessions. The plugin
shows the daemon reply. The plugin uses the daemon for **Record and send** when
a session runs. The plugin uses the one-shot `capture_local.py` path when no
session runs.

### Capture latency experiment

Some handsets start the microphone only after they receive downlink audio.
`--send-silence` sends paced silent PCM downlink packets during each capture.
The option is off by default, so the current behaviour is reproducible. The
one-shot path has the same flag. The log line and the comparison are in
[docs/hardware-tests.md](docs/hardware-tests.md).

### Idle and power

- The default idle limit is 600 seconds. The daemon restores Bluetooth after
  that limit without a recording.
- The daemon releases the CPU latency hold between recordings and re-acquires
  the hold for each capture. The tests do not cover this with the controller
  powered. If a recording fails after a long idle period, start the session with
  `--keep-latency`.

### Restore and recovery

The daemon restores Bluetooth on `stop`, on the idle limit, on SIGTERM, on
SIGINT, and on an unexpected Python error.

The daemon cannot restore Bluetooth after SIGKILL, SIGSEGV, a kernel hang, or a
power loss. Stock Bluetooth remains off. Reboot the Kindle, or try the manual
recovery commands in [docs/recovery.md](docs/recovery.md). The commands are not
a guarantee. A reboot is not guaranteed to restore Bluetooth.

If the headset drops during the session, the next `record` reconnects with
`device.connect`. The daemon does not run the handoff and does not call
`power_on`. If the controller stops answering, stop the session and start it
again.

## Receiver side

The receiver is a FastAPI application in `server/voice_bridge_server/`. It reads
`KVB_TOKEN` and `KVB_STORAGE_DIR` from the environment. It accepts recordings up
to 15 seconds by default. It validates each WAV with
`voice_bridge_server.audio.parse_wav` and stores the file in `KVB_STORAGE_DIR`.

The upload contract is in [docs/protocol.md](docs/protocol.md).

The review page is at `http://127.0.0.1:18767/`. The page lists recordings,
plays them, shows the transcript, and deletes a recording. The page is
unauthenticated and is for the trusted LAN during development. Do not expose the
page to the public internet. The detail is in
[docs/review-ui.md](docs/review-ui.md).

## Transcription

The receiver transcribes each recording as it arrives. Set `OPENROUTER_API_KEY`
before the receiver starts. A background task sends the audio to OpenRouter
after the 201 response. The receiver writes `<id>.txt` and
`<id>.transcript.json` next to the WAV. It never changes the audio. Without the
key, transcription is off and the upload path is unchanged.

```powershell
$env:OPENROUTER_API_KEY = "<key>"
python -m uvicorn voice_bridge_server.app:create_app_from_env --factory --host 0.0.0.0 --port 18767
```

`tools/transcribe_worker.py --once` backfills a storage directory that the
receiver did not transcribe. Do not run the worker in a loop while the receiver
hook is enabled.

The default model is `mistralai/voxtral-small-24b-2507`. It is a purpose-built
ASR model and the cheapest audio rate that OpenRouter lists. A 15 s clip costs
about $0.0015. OpenRouter has no transcription endpoint. The audio travels as an
`input_audio` part of a chat completion. The model comparison, the cost per
clip, and what to try if 8 kHz audio transcribes poorly are in
[docs/transcription.md](docs/transcription.md).

## Limits and risks

### The Bluetooth handoff

The handoff stops the stock Bluetooth daemon (`btd`) and takes the UART from
`bsa_server`. The handoff can leave Bluetooth unusable. Recovery is not
guaranteed. Do not run the handoff without explicit approval.

- Bumble stays resident with the controller powered. A Bumble crash or a
  controller hang can leave the session unusable. The restore path runs only if
  the Python process can still execute it.
- A headset drop during a capture fails that recording. The session stays alive,
  and the next recording reconnects.
- A SIGKILL or a hard crash leaves Bluetooth off. The socket and the lock file
  become stale. The next start removes the stale socket. Restore the stock
  Bluetooth daemon first.
- The control socket is local only. Its owner check is weak when all processes
  run as root.
- The WAV file is private. The daemon writes the file to `/tmp` with mode 0600
  and deletes the file after the upload. A crash during the upload can leave the
  file in `/tmp`.
- `bt-session.log` can contain the headset address and error type names. The log
  does not contain the link key or the token. Delete the log after use.

### Security

- HCI over the relay is plaintext. The listener does not authenticate the Kindle
  client. Use a trusted network only.
- The exported `bt-keys.txt` holds secret link keys that can impersonate the
  Bluetooth identity of the Kindle. Delete
  `/mnt/us/kindle-voice-bridge/bt-keys.txt` from the Kindle after use. Delete
  the local copies of the key store.
- The Windows firewall rule for inbound port 18767 is scoped to the Kindle IP
  address and the `python.exe` of the project. Remove the rule when the test
  finishes.
- Raw captures may contain private microphone audio. `.gitignore` excludes
  `*.wav` and `.data/`. Keep captures out of version control.
- `upload.json` holds the bearer token in plain text on the FAT filesystem of the
  device. The on-device scripts keep the file so later runs can reuse it. This is
  acceptable only for the trusted-LAN prototype. Delete the file when the test
  finishes.
- Transcription sends the audio to OpenRouter, a third party. The receiver stays
  local, but the speech does not. The receiver writes transcripts in plain text
  next to the recordings and does not redact them. Do not transcribe a recording
  that should not be uploaded.

### Measured limits

- The audio format is PCM16 mono 8 kHz by strong inference. The controller has
  not confirmed the format. The controller may return encoded data despite the
  PCM request.
- One headset model and one firmware version have passed tests.

## Where the detail lives

- [docs/protocol.md](docs/protocol.md): the upload contract.
- [docs/hardware-tests.md](docs/hardware-tests.md): hardware results, the laptop
  capture commands, and open questions.
- [docs/transcription.md](docs/transcription.md): the model comparison and the
  cost per clip.
- [docs/review-ui.md](docs/review-ui.md): the review page and its endpoints.
- [docs/recovery.md](docs/recovery.md): the Bluetooth recovery commands.
- [PLAN.md](PLAN.md): the project plan and the hardware precautions.
