# Kindle Voice Bridge

Kindle Voice Bridge records the microphone of a Bluetooth headset on a Kindle
Paperwhite 10th edition. The computer stores each recording, plays it in a
review page, and can transcribe it through OpenRouter.

## Before you start

The Bluetooth handoff stops the stock Bluetooth daemon and can leave Bluetooth
unusable. Recovery is not guaranteed, so read
[docs/recovery.md](docs/recovery.md) before the first run.

## Security boundary

By default the receiver listens on all interfaces, and HTTP exposes the token
and the audio on the network. The review page is unauthenticated, so use a
trusted network only and read [docs/review-ui.md](docs/review-ui.md) for the
detail.

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

The receiver answers at `http://127.0.0.1:18767/healthz`. The review page is at
`http://127.0.0.1:18767/`.

The command creates a token when none exists and never prints it. It stores
recordings in `%LOCALAPPDATA%\KindleVoiceBridge\recordings`. It turns
transcription on when
`server/voice_bridge_server/voxtral-openrouter-key.txt` exists.

Add `-LocalOnly` to bind `127.0.0.1` only. The command prints the process ID.
Stop the receiver with `Stop-Process -Id <pid>`.

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
3. Select **Start recording**. A strip opens along the bottom edge of the
   screen.
4. Wait for **SPEAK NOW** and the countdown.
5. Tap the strip to stop and send. The strip shows **SENDING**, then **SENT**
   or **NOT SENT**.
6. Tap the strip again to close it.

**Record and send** records a fixed five-second clip and does not open the
strip.

## Where to go next

- [docs/architecture.md](docs/architecture.md): how the system works.
- [docs/protocol.md](docs/protocol.md): the upload contract.
- [docs/hardware-tests.md](docs/hardware-tests.md): hardware results, the laptop
  capture commands, and the resident session protocol.
- [docs/transcription.md](docs/transcription.md): the model comparison and the
  cost per clip.
- [docs/review-ui.md](docs/review-ui.md): the review page and its endpoints.
- [docs/recovery.md](docs/recovery.md): the Bluetooth recovery commands.
- [docs/one-pager.md](docs/one-pager.md): the project pitch and the next steps.
- [PLAN.md](PLAN.md): the project plan and the hardware precautions.
