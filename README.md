# Kindle Voice Bridge

Ask an LLM a question out loud while reading on a Kindle.

## Why

The Kindle's on-screen keyboard is slow and clumsy. Typing a question breaks the
reading flow, so in practice you stop asking. This project turns the question
into a spoken sentence instead: a Bluetooth headset captures it, the receiver
transcribes it, an LLM answers it, and the answer appears back in KOReader.

The Kindle has no microphone or speaker, but it supports Bluetooth audio for
VoiceView, and a Bluetooth headset has a microphone. The project uses that path.

## What it does

1. The KOReader plugin starts a short capture from the headset.
2. The Kindle uploads the WAV over Wi-Fi to a small FastAPI receiver on your
   computer.
3. The receiver transcribes the audio with OpenRouter, asks a cheap LLM for a
   short answer, and writes both next to the recording.
4. The plugin polls for the answer and shows it in a popup.
5. A local dashboard lists recordings, transcripts and answers, and refreshes
   live as new ones arrive.

This is a development prototype. The receiver runs on your computer, the LAN
connection is plain HTTP, and the dashboard is unauthenticated. Read the security
boundary below before using it.

## Before you start

The Bluetooth handoff stops the stock Bluetooth daemon and can leave Bluetooth
unusable. Recovery is not guaranteed, so read
[docs/recovery.md](docs/recovery.md) before the first run.

## Security boundary

By default the receiver listens on all interfaces, and HTTP exposes the token
and the audio on the network. The dashboard is unauthenticated, so use a trusted
network only and read [docs/review-ui.md](docs/review-ui.md) for the detail.

## Requirements

- A computer with Windows and Python 3.11 or later.
- A Kindle Paperwhite 10th edition with firmware 5.18.1.1.1.
- A Bluetooth headset.
- A USB cable.
- A trusted local network.
- A portable CPython 3.11.17 and Bumble 0.0.236 at
  `/mnt/us/kindle-voice-bridge/` on the Kindle.

The Kindle has no system Python. The commands `python3` and `python` are not
present on the device.

## 1. Jailbreak the Kindle and install KOReader

The [jailbreak guide](https://kindlemodding.org/jailbreaking/index.html) covers
the jailbreak and the KOReader install. This project assumes a jailbroken
Kindle with KOReader already running.

## 2. Install the receiver

Run this command from the project directory in PowerShell:

```powershell
python -m pip install -e .
```

## 3. Start the receiver

```powershell
.\tools\kvb.ps1 receiver
```

The health check is at `http://127.0.0.1:18767/healthz`. The dashboard is at
`http://127.0.0.1:18767/`.

The command creates a token when none exists and never prints it. It stores
recordings in `%LOCALAPPDATA%\KindleVoiceBridge\recordings`. It turns
transcription and answering on when
`server/voice_bridge_server/voxtral-openrouter-key.txt` exists.

Add `-LocalOnly` to bind `127.0.0.1` only. Stop or restart the receiver with:

```powershell
.\tools\kvb.ps1 stop
.\tools\kvb.ps1 restart
```

If the execution policy blocks the script, run this command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\kvb.ps1 receiver
```

## 4. Connect the Kindle and copy the files

1. Connect the USB cable.
2. Run this command with the drive letter of the Kindle:

   ```powershell
   .\tools\kvb.ps1 deploy -KindleRoot D:\ -ServerUrl http://<laptop-ip>:18767
   ```

The command copies the device files and the KOReader plugin. It does not copy
the Python runtime or the Bumble bundle.

## 5. Write the upload configuration

The device reads `/mnt/us/kindle-voice-bridge/upload.json`, which holds
`server_url` and `token`. The `deploy` command writes the file when
`-ServerUrl` is present. Add `-Force` to replace an existing file.

To write the file by hand, create it with this content:

```json
{"server_url": "http://<laptop-ip>:18767", "token": "<token>"}
```

## 6. Make the first recording

1. Open the Voice Bridge plugin in KOReader.
2. Select **Start Bluetooth session**. Confirm the action.
3. Select **Ask question**. A strip opens along the bottom edge of the screen.
   You can also assign the **Voice Bridge: start/stop recording** Dispatcher
   action to a gesture.
4. Wait for **SPEAK NOW** and the countdown.
5. Ask the question, then tap the strip to stop and send.
6. The strip shows **SENT**, then **RESPONSE READY** when the answer arrives.
7. Tap the strip to read the answer. Closing the popup closes the strip.

The dashboard at `http://127.0.0.1:18767/` shows the recording, its transcript
and the answer, and updates live as new ones arrive.

## Where to go next

- [docs/architecture.md](docs/architecture.md): how the system works.
- [docs/protocol.md](docs/protocol.md): the upload contract.
- [docs/hardware-tests.md](docs/hardware-tests.md): hardware results, the laptop
  capture commands, and the resident session protocol.
- [docs/transcription.md](docs/transcription.md): the model comparison and the
  cost per clip.
- [docs/review-ui.md](docs/review-ui.md): the dashboard and its endpoints.
- [docs/recovery.md](docs/recovery.md): the Bluetooth recovery commands.
- [docs/one-pager.md](docs/one-pager.md): the project pitch and the next steps.
- [ai.md](ai.md): how AI was used to build the project.
- [PLAN.md](PLAN.md): the original project plan and the hardware precautions.
