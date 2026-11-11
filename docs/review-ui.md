# Development dashboard

A small, self-contained dashboard for listening to recordings and reading their
transcripts and LLM answers while iterating on Kindle microphone capture. It is
a local development aid, **not** part of the upload contract in
[`protocol.md`](protocol.md). The upload endpoint, its bearer authentication and
the documented wire format are unchanged.

## Start

Start the receiver from the project root:

```powershell
.\tools\kvb.ps1 receiver
```

Open `http://127.0.0.1:18767/`.

To run the app directly instead, use the same environment variables as the
receiver:

```powershell
$env:KVB_TOKEN = "dev-only-token-replace-me"
$env:KVB_STORAGE_DIR = "$env:LOCALAPPDATA\KindleVoiceBridge\recordings"
cd server
python -m uvicorn voice_bridge_server.app:create_app_from_env --factory --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The dashboard page (inline CSS and JS, no build step, no CDN). |
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
      "parse_error": null,
      "transcript": {
        "status": "ok",
        "text": "Speak now. Fifteen, fourteen.",
        "model": "mistralai/voxtral-small-24b-2507",
        "attempts": 1,
        "error": null,
        "retryable": false,
        "cost_usd": 0.0015,
        "updated_at": "2026-11-11T12:00:00+00:00"
      },
      "answer": {
        "status": "ok",
        "text": "A thunderhead is the flat, anvil-shaped top of a cumulonimbus cloud.",
        "model": "mistralai/mistral-small-24b-instruct-2501",
        "error": null,
        "retryable": false,
        "cost_usd": 0.0000061,
        "updated_at": "2026-11-11T12:00:01+00:00"
      }
    }
  ]
}
```

`transcript` and `answer` are `null` when their sidecar does not exist. The
sidecars are written by the receiver (see [transcription.md](transcription.md));
this page only reads them, and re-types and caps every field so a corrupt
sidecar cannot bloat the listing.

Metadata is derived by parsing each file with
`voice_bridge_server.audio.parse_wav`, so there is no second WAV parser. A file
that cannot be parsed is still listed with `parse_error` set and its statistics
as `null`.

## Page features

- Recordings are listed newest first.
- Each card shows the ID and received time, then the answer, the transcript and
  the player. Each stage carries a muted line with its model and billed cost;
  the transcript line also shows the attempt count.
- A failed transcription or answer shows its error instead of the text. A
  recording with no transcript sidecar yet shows "Transcribing…", and a
  successful transcript with no answer sidecar yet shows "Waiting for answer…".
- All technical metadata is collapsed into a **Details** disclosure: duration,
  sample rate, channels, bit depth, file size, the first 12 hex characters of
  the SHA-256, and the status, model, attempts, cost, update time and error for
  both the transcription and the answer.
- `<audio controls>` plays the clip inline; a Download link saves it.
- Delete asks for confirmation, then removes the file, its transcript sidecars,
  its answer sidecar and refreshes the list.
- A Refresh button and the `R` key reload the list.
- The page polls every 2 seconds and updates cards in place, so new recordings
  and new transcripts or answers appear without a manual refresh. Audio playback
  and open **Details** disclosures survive an update. Polling backs off to 5
  seconds while the receiver is unreachable. There is still no WebSocket.

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
  statistics, plus the transcript and answer sidecars. It is fine for
  development-sized directories, not for thousands of files.
- There is no pagination, filtering, search or waveform view.
- Delete is permanent and has no undo beyond the browser confirmation.
- Statistics are parsed with a one-hour duration limit so an old clip longer
  than the current upload limit still appears. Structure and format rules are
  still enforced.
- The page and API are unauthenticated by design; see the warning above.

## Tests

`tests/server/test_review.py` covers the listing, transcript and answer
sidecars, exact playback bytes, malformed and unknown IDs, path traversal,
delete plus listing refresh, token absence and the self-contained page. Run the
suite with:

```powershell
.venv\Scripts\python.exe -m pytest
```
