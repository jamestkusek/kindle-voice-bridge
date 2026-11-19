# Development dashboards

Two self-contained pages for iterating on Kindle capture:

- `/` lists recordings with their transcripts and answers, and plays each clip.
- `/debug` follows each ask through its pipeline: the trace feed, the exact
  model messages, the resolved reading context, the book library and the log
  tail.

Both are local development aids, **not** part of the upload contract in
[`protocol.md`](protocol.md). The upload endpoint, its bearer authentication and
the documented wire format are unchanged. The page body is inline CSS and JS
with no build step and no CDN.

## Start

Start the receiver from the project root:

```powershell
.\tools\kvb.ps1 receiver
```

Open `http://127.0.0.1:18767/` for the recordings page and
`http://127.0.0.1:18767/debug` for the debug page.

To run the app directly instead, use the same environment variables as the
receiver:

```powershell
$env:KVB_TOKEN = "dev-only-token-replace-me"
$env:KVB_STORAGE_DIR = "$env:LOCALAPPDATA\KindleVoiceBridge\recordings"
cd server
python -m uvicorn voice_bridge_server.app:create_app_from_env --factory --host 127.0.0.1 --port 18767
```

Open `http://127.0.0.1:18767/`.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/` | none | The recordings page. |
| `GET` | `/api/recordings` | none | JSON list of stored recordings, newest first. |
| `GET` | `/recordings/{id}.wav` | none | Play a recording inline. `?download=1` sends it as an attachment. |
| `DELETE` | `/api/recordings/{id}` | none | Delete one recording and return the new list. |
| `GET` | `/debug` | none | The debug page (feed, dev ask, library, logs). |
| `GET` | `/api/traces` | none | Trace summaries, newest first. `?limit=1..100`, default 50. |
| `GET` | `/api/traces/{id}` | none | One whole trace, or `404 not_found`. |
| `GET` | `/api/library` | none | Synced books and the newest sync report. |
| `GET` | `/api/logs` | none | The last log lines. `?limit=1..500` and `?q=` text filter. |
| `POST` | `/v1/dev/ask` | bearer | Run the pipeline from a typed question. |
| `POST` | `/api/library/sync` | bearer | Sync the library. `?dry_run=1` writes nothing. |

The two authenticated routes, the request bodies and the payload schema are in
[protocol.md](protocol.md). Sync semantics are in
[context-and-library.md](context-and-library.md).

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
        "model": "deepseek/deepseek-v4.1-flash-1017",
        "error": null,
        "retryable": false,
        "cost_usd": 0.00011,
        "updated_at": "2026-11-11T12:00:01+00:00"
      },
      "trace": {"origin": "kindle", "outcome": "ok"}
    }
  ]
}
```

`transcript` and `answer` are `null` when their sidecar does not exist. The
sidecars are written by the receiver (see [transcription.md](transcription.md));
this page only reads them, and re-types and caps every field so a corrupt
sidecar cannot bloat the listing. `trace` carries the origin and the outcome
from the trace file, or `null` when there is no trace; it lets the card link to
`/debug#<id>` and mark a dev ask with a `dev` chip.

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
- `<audio controls>` plays the clip inline; a Download link saves it, and a
  Trace link opens `/debug#<id>`.
- An ask that the debug page started carries a `dev` chip, so it cannot be
  mistaken for a spoken question.
- Delete asks for confirmation, then removes the file, its transcript sidecars,
  its answer sidecar, its context sidecar, its trace and refreshes the list.
- A Refresh button and the `R` key reload the list.
- The page polls every 2 seconds and updates cards in place, so new recordings
  and new transcripts or answers appear without a manual refresh. Audio playback
  and open **Details** disclosures survive an update. Polling backs off to 5
  seconds while the receiver is unreachable. There is still no WebSocket.

## Debug page

`/debug` is the observability view of one trace file per ask. The file is the
live feed, the detail page and the prompt record; there is no event bus and no
database.

The default view is high level. Only the header, the summary cards, the
question rows and the book cards are visible; the config strip, the dev ask,
the sync and the log tail sit behind one collapsed **Tools** disclosure.

- **Header.** The page title, a one-line receiver status ("Receiver reachable -
  N questions in the feed"), the token field behind its own disclosure, and an
  "updated N s ago" marker that turns red when the feed is stale.
- **Token field.** The operator pastes the receiver's bearer token. The page
  keeps it in `sessionStorage` for the tab. Only the dev ask and the sync send
  it; no other route needs it, and the server never puts the token in the page.
- **Summary cards.** Books (from `/api/library`), Questions, Answers, Errors and
  Last activity (from `/api/traces?limit=50`). Answers counts traces whose
  outcome is `ok`; Errors counts `failed` and `rejected`.
- **Questions.** Polled every 2 s. One compact row per trace: time, book title,
  page, a `page` or `highlight` kind badge, an answer preview and an outcome
  badge. A row whose outcome is still `pending` after 120 s turns red (stuck).
  The preview and the kind badge come from the trace detail, which the page
  loads six at a time, newest first; the summary alone carries the page anchor.
- **Book filter.** Clicking a book title in a row, or a book card, filters the
  question rows to that book and opens the book's details. Clicking the same
  title again, or the **Show all books** button in the Library heading, clears
  the filter. Matching uses `book_title` from the trace summary; a trace with no
  title is grouped under "Unknown book".
- **Expandable trace.** Clicking a row opens a panel with the decoded context
  and its rejection reason, the book key and alias match, the stage table with
  durations and detail, the resolved passage with its rung and character count,
  the exact system and user messages, the model response, the usage, the cost
  and the declared and sent answer lengths.
- **Deep link.** `/debug#<recording_id>` opens that trace. A trace outside the
  last 50 is loaded on its own into a pinned panel above the questions.
- **Library cards.** Title, authors, character count, question count, last asked
  time and last synced time for each synced book. Title, authors, characters
  and last synced time come from `/api/library`; question count and last asked
  time are counted from the loaded trace summaries. The last sync report is
  summarised under the cards.
- **Tools.** One disclosure holding the dev ask form, the sync button with its
  dry-run checkbox (on by default) and the last sync report, the log tail, and
  the config strip: storage directory, library store, library source, the
  default transcribe and answer models, whether an OpenRouter key is configured,
  the log level, and whether trace text is shown. A re-run button in a trace
  panel opens the disclosure and prefills the form.
- **Log tail.** The last 200 log lines, filtered by recording id or stage text.
  Log lines carry the stage, the id, the duration and the outcome, never prompt
  text or book text.

The question list, the detail routes, the library and the logs are open. The dev ask and
the sync return `401` without the bearer token.

## Sync

The sync scans `KVB_LIBRARY_SOURCE` (default
`<KVB_STORAGE_DIR>/library-source`) and extracts EPUB text. It takes no path
argument. Extracted books are written under `KVB_LIBRARY_DIR` (default
`<KVB_STORAGE_DIR>/library`). The dry run returns the same report and writes
nothing; a real run also writes `<library>/.sync/<stamp>.json`. Row statuses,
reason codes and the storage layout are in
[context-and-library.md](context-and-library.md).

## Privacy boundary

A trace contains the prompts, the resolved passage, the context payload and the
model response, so book text and highlighted text reach the storage directory.
That directory is private: keep it off any synced folder and out of version
control. Both pages are for the trusted LAN only, and `/debug` is open apart
from the dev ask and the sync. `KVB_DEBUG_TEXT=0` makes the receiver hide prompt
text, the resolved passage and the context text fields while keeping the stages,
the timings and the outcome.

## Security model

These routes are **unauthenticated**, apart from the dev ask and the sync, and
are intended only for the trusted LAN during development. Do not expose them to
the public internet.

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
- A trace holds the prompts, the resolved passage, the context payload and the
  model response, so the book text and any highlighted text reach the storage
  directory. That directory is private and must stay off any synced folder.
- The page and API are unauthenticated by design; see the warning above.

## Tests

`tests/server/test_review.py` covers the recordings page: the listing,
transcript and answer sidecars, exact playback bytes, malformed and unknown IDs,
path traversal, delete plus listing refresh, token absence and the self-contained
page. `tests/server/test_debug.py` covers the debug page, the trace feed and
detail, the log tail, the library routes, the dev ask, the sync, the token
checks and the debug environment variables. Run the suite with:

```powershell
.venv\Scripts\python.exe -m pytest
```
