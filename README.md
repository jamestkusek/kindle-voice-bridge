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

The `receiver` command reads the token from `.data\receiver-config.json`. When
that file is absent, the command reads the token from the Kindle `upload.json`.
When both files are absent, the command writes a new token to
`.data\receiver-config.json`. The script never prints the token.

The command sets `KVB_TOKEN`, `KVB_STORAGE_DIR`, and `KVB_ALLOW_INSECURE_LAN`.
It sets `OPENROUTER_API_KEY` from
`server/voice_bridge_server/voxtral-openrouter-key.txt` when that file exists.
Without the key file, transcription is off. The receiver stores accepted WAV
files in `%LOCALAPPDATA%\KindleVoiceBridge\recordings`.

Add `-LocalOnly` to bind `127.0.0.1` only. The command prints the process ID.
Stop the receiver with `Stop-Process -Id <pid>`. Run `.\tools\kvb.ps1 help`
for the full option list.

The PowerShell execution policy may block the script. In that case, run this
command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\kvb.ps1 receiver
```

### Start without the script

Run these commands in PowerShell from the project directory:

```powershell
python -m pip install -e .
$env:KVB_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:KVB_STORAGE_DIR = "$env:LOCALAPPDATA\KindleVoiceBridge\recordings"
python -m uvicorn voice_bridge_server.app:create_app_from_env --factory --host 127.0.0.1 --port 18767
```

Open `http://127.0.0.1:18767/healthz` to check the receiver. Stop the receiver
with Ctrl+C.

The manual command binds to the computer only. The Kindle cannot connect to this
address. A LAN test requires a trusted network. Bind the receiver to its LAN
address. Set `KVB_SERVER_URL` to that address, and set
`KVB_ALLOW_INSECURE_LAN=1` on the client. Do not expose this receiver directly
to the internet. Use verified HTTPS for a remote server.

Open the stored files with an audio player. Delete the files when they are no
longer needed.

## 4. Connect the Kindle and copy the files

1. Connect the USB cable.
2. Run this command with the drive letter of the Kindle:

   ```powershell
   .\tools\kvb.ps1 deploy -KindleRoot D:\ -ServerUrl http://<laptop-ip>:18767
   ```

The `deploy` command copies the device files to
`/mnt/us/kindle-voice-bridge/`. It copies the KOReader plugin to
`/mnt/us/koreader/plugins/voicebridge.koplugin/`. It does not copy the Python
runtime or the Bumble bundle.

## 5. Write the upload configuration

The device reads `/mnt/us/kindle-voice-bridge/upload.json`. The file holds
`server_url` and `token`.

The `deploy` command writes the file when `-ServerUrl` is present. The command
keeps an existing file. Add `-Force` to replace it.

To write the file by hand, create it with this content:

```json
{"server_url": "http://<laptop-ip>:18767", "token": "<token>"}
```

The device keeps the file, so later runs reuse it.

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
