# Kindle Voice Bridge

This project records a Bluetooth headset microphone on a Kindle PW4 and sends
short clips to a computer. The receiver, the Kindle upload path, and a Bluetooth
HFP capture to the laptop have been verified on a real PW4. The remaining gaps
are listed in [docs/hardware-tests.md](docs/hardware-tests.md).

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

The laptop tools need Bumble 0.0.236. Install the optional dependency from the
project directory:

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

   The plugin exposes five actions: Run diagnostics, Inspect Bluetooth, Start
   HCI probe, Stop HCI probe, and Export Bluetooth link keys. Start and Export
   ask for confirmation first; the other three only read device state.

4. The listener records five seconds to `capture.raw` and writes the packet
   metadata to `capture.raw.json` next to it. Convert it:

   ```powershell
   python tools/raw_to_wav.py capture.raw 8000 capture.wav
   python tools/clean_raw_wav.py capture.raw 8000 capture-clean.wav --start 2.4
   ```

   `clean_raw_wav.py` removes the DC offset, normalises the peak, and with
   `--start` drops a leading section. The first ~2.4 s of a capture is a
   non-speech artifact; nothing detects that boundary automatically, so the
   start time is an operator choice. The format is PCM16 mono 8 kHz by strong
   inference, not by controller confirmation.

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

The upload contract is in [docs/protocol.md](docs/protocol.md). The project plan
and hardware precautions are in [PLAN.md](PLAN.md).
