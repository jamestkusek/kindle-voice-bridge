# Development review UI

A small, self-contained page for listening to recordings while iterating on
Kindle microphone capture. It is a local development aid, **not** part of the
upload contract in [`protocol.md`](protocol.md). The upload endpoint, its bearer
authentication and the documented wire format are unchanged.

## Start

Use the same environment variables as the receiver:

```powershell
$env:KVB_TOKEN = "dev-only-token-replace-me"
$env:KVB_STORAGE_DIR = "C:\Users\james\Desktop\Claude Research\kindle-voice-bridge\.data\recordings"
cd server
python -m uvicorn voice_bridge_server.app:create_app_from_env --factory --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The review page (inline CSS and JS, no build step, no CDN). |
| `GET` | `/api/recordings` | JSON list of stored recordings, newest first. |
| `GET` | `/recordings/{id}.wav` | Play a recording inline. `?download=1` sends it as an attachment. |
| `DELETE` | `/api/recordings/{id}` | Delete one recording and return the new list. |

The list response has this shape:

```json
{
  "count": 1,
  "recordings": [
    {
      "id": "rec_20261110T130700Z_9f2c1a4b8e6d0f3c5a7b9d1e2f4a6c8b",
      "received_at": "2026-11-10T13:07:00+00:00",
      "size_bytes": 16044,
      "sha256_prefix": "0d5f2f0b2f5f",
      "duration_seconds": 1.0,
      "sample_rate_hz": 8000,
      "channels": 1,
      "bits_per_sample": 16,
      "parse_error": null
    }
  ]
}
```

Metadata is derived by parsing each file with
`voice_bridge_server.audio.parse_wav`, so there is no second WAV parser. A file
that cannot be parsed is still listed with `parse_error` set and its statistics
as `null`.

## Page features

- Recordings are listed newest first.
- Each row shows the ID, received time, duration, sample rate, channels, bit
  depth, file size and the first 12 hex characters of the SHA-256.
- `<audio controls>` plays the clip inline; a Download link saves it.
- Delete asks for confirmation, then removes the file and refreshes the list.
- A Refresh button and the `R` key reload the list. There is no polling,
  WebSocket or auto-refresh loop.

## Security model

These routes are **unauthenticated** and are intended only for the trusted LAN
during development. Do not expose them to the public internet.

- A recording ID must match `^rec_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{32}$` before it
  is used to build a path. The resolved path is also checked to stay inside the
  configured storage directory.
- The bearer token is never sent to the page, never included in the JSON and
  never echoed in an error.
- The page renders every value with DOM text nodes, never with `innerHTML`.
- Responses set `Cache-Control: no-store`; the page sets a restrictive
  `Content-Security-Policy`, `X-Content-Type-Options: nosniff` and
  `X-Frame-Options: DENY`.
- Playback always uses `Content-Type: audio/wav` and a `Content-Disposition`
  filename derived from the validated ID.

## Limitations

- `received_at` is the file's modification time, not a persisted acceptance
  timestamp. The upload response's `stored_at` is not stored on disk.
- The listing reads every matching WAV to compute its SHA-256 prefix and parse
  statistics. It is fine for development-sized directories, not for thousands of
  files.
- There is no pagination, filtering, search, waveform view or transcription.
- Delete is permanent and has no undo beyond the browser confirmation.
- Statistics are parsed with a one-hour duration limit so an old clip longer
  than the current upload limit still appears. Structure and format rules are
  still enforced.
- The page and API are unauthenticated by design; see the warning above.

## Tests

`tests/server/test_review.py` covers the listing, exact playback bytes,
malformed and unknown IDs, path traversal, delete plus listing refresh, token
absence and the self-contained page. Run the suite with:

```powershell
.venv\Scripts\python.exe -m pytest
```
