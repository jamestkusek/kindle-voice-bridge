# Milestone 2: reading context, library sync, observability

## Goal

Send a small reading context with each question, keep books and question history
on the server, and make the ask pipeline visible while it runs.

In scope:

- Context per ask: book title and authors, page, fraction, chapter, and the
  highlighted text when the ask starts from a highlight.
- A highlight action in KOReader that starts the normal spoken ask.
- A host-side library sync that copies books to the server and extracts text.
- A `/debug` page with a live trace feed, the exact model messages, the resolved
  passage, sync reports, and a log tail.

Out of scope: past chats on the Kindle, embeddings, whole-book retrieval, PDF,
MOBI and KFX parsing, dashboard authentication, and automated end-to-end tests.

## Design bet

One trace file per ask is the observability system. It is the live feed, the
detail page, and the prompt and usage record. No event bus, no database.

The Kindle sends a pointer, not page text. Page text extraction costs about
0.5-1 s on the device, so it is not in the ask path. The server resolves the
pointer against a synced book when it has one.

## Context payload

The daemon sends one optional header on `POST /v1/recordings`:

```
X-KVB-Context: base64url(no padding) of UTF-8 JSON, at most 4096 bytes decoded
```

```json
{
  "v": 1,
  "book": {"title": "", "authors": [""], "size_bytes": 0, "partial_md5": "", "format": "epub"},
  "pos": {"page": 142, "total_pages": 310, "fraction": 0.43, "chapter": ""},
  "locator": {"xpointer": "", "href": ""},
  "highlight": "text or null"
}
```

A missing header keeps the current messages and model unchanged. A malformed or
oversized header never rejects the upload. The audio is stored, the context
stage records `rejected` with a reason, and the answer uses the transcript alone.

The plugin writes `/tmp/voicebridge-context.json` before it writes the
`start_recording` command. The daemon reads and unlinks the file when the
capture starts.

## Trace

One `<recording_id>.trace.json` beside the WAV, rewritten atomically after each
stage.

```json
{
  "v": 1, "id": "rec_...", "origin": "kindle|dev", "created_at": "",
  "outcome": "pending|ok|failed",
  "context": {"header_bytes": 0, "parsed": {}, "rejected_reason": null},
  "book": {"key": "bk_...", "match": "md5-alias|title+size|none"},
  "stages": [{"name": "receive", "started_at": "", "dur_ms": 3, "outcome": "ok", "detail": {}}],
  "resolve": {"rung": "highlight|xpointer|fraction|none", "reason": "", "passage": ""},
  "llm": {"model": "", "prompt": {"system": "", "user": ""}, "usage": {},
          "cost_usd": null, "cost_source": "response|none", "response_text": ""},
  "answer": {"chars_declared": 0, "chars_sent": 480}
}
```

Stages: `receive`, `validate`, `store`, `context`, `resolve`, `transcribe`,
`prompt`, `answer`, `sidecars`. Limits: 2 KB per detail field, 16 KB per trace,
4 KB each for prompt and response. A pending trace older than 120 s is shown as
`STUCK`.

Logs carry only the stage name, recording id, duration and outcome. Prompt text
and passages stay in the trace.

## Library

```
<storage>/library/<book_key>/meta.json
<storage>/library/<book_key>/text.json
<storage>/library/.sync/<timestamp>.json
```

`book_key` is `bk_` plus the first 16 hex characters of
`sha256(normalised_title|normalised_authors)`. The size and partial md5 are
aliases. `text.json` holds `paragraphs` and `cum_chars`. EPUB text is extracted
in spine order with `zipfile` and `html.parser`. The sync report records one row
per file with `new`, `duplicate`, `changed`, `removed`, `unsupported`, `drm`, or
`parse_failed`.

## API

| Method | Path | Auth |
| --- | --- | --- |
| POST | `/v1/recordings` with optional `X-KVB-Context` | bearer |
| GET | `/v1/recordings/{id}/answer` | bearer |
| POST | `/v1/dev/ask` | bearer |
| POST | `/api/library/sync?dry_run=1` | bearer |
| GET | `/debug`, `/api/traces`, `/api/traces/{id}`, `/api/library`, `/api/logs`, `/healthz` | open |

`/v1/dev/ask` accepts a question, optional context, prompt and model. It writes a
0.1 s silent WAV and runs the normal background pipeline with
`origin: "dev"`. A trace is available while it runs.

`/api/library/sync` scans only `KVB_LIBRARY_SOURCE` and stores extracted books
under `KVB_LIBRARY_DIR`. It does not accept a path. A dry run returns the report
without writing.

## Debug page

`/debug` contains:

- A token field kept in `sessionStorage`, and a config strip with the storage
  directory, library source and store, models, key presence and log level.
- A live feed polled every 2 s. Each row shows time, origin, short id, last
  stage and outcome, book, page, cost and total time. Stuck rows turn red.
- An expandable panel per row, also addressable as `#<id>`: decoded context,
  book key and alias match, stage table, resolved passage with its rung and
  offsets, exact system and user messages, response, usage and cost.
- A dev-ask form and a re-run button that prefills it from any trace.
- A library table and the last sync report.
- The last 200 log lines, filterable by id.

## Device

The plugin gains `collectReadingContext()` with each getter in `pcall`, a
"Show reading context" probe, and one highlight-dialog entry. Every field is
optional. A missing document API yields no context and never blocks the ask.

The daemon gains `--context-file` and `--cue-delay`. The cue delay defaults to
2.0 s. After the audio-ready event, the daemon waits for the delay, then starts
the visible capture and trims everything before that point. This removes the
startup artifact at the beginning of the uploaded WAV. A delay of 0 restores the
previous behaviour.

The answer state line keeps UTF-8 and is capped at 480 characters. Control
characters are stripped. `safe_text` remains the ASCII formatter for logs.

## Phases

1. Trace and context modules, with unit tests.
2. Server wiring: header, sidecar, prompt, stage callbacks. No-context requests
   stay byte-identical.
3. Library sync and EPUB extraction.
4. Device: context file, header, cue delay, UTF-8 answer.
5. Plugin: context collector, probe, highlight entry.
6. Debug routes, dev ask, sync endpoint, dashboard.
7. Docs.
8. Verification: full suite and a live smoke test on a spare port.

## Test policy

Unit tests cover pure functions and file stores: context decoding, book keys,
prompt construction, fraction slicing, trace caps, EPUB extraction from fixture
archives, sync reports, the Lua context serialiser, and the cue offset. The
existing suite must pass unchanged where it asserts current behaviour. The
microphone, Bluetooth, KOReader UI and OpenRouter calls are verified live, not
automated.

## Risks

- Title and authors merge editions. The trace shows the match so a wrong merge is
  visible.
- xpointer is recorded but not resolved. Fraction resolution is approximate.
- Traces contain prompts and book text. The storage directory is private and
  the dashboard is for the trusted LAN only. `KVB_DEBUG_TEXT=0` hides text while
  keeping stages.
- Capture timing depends on the headset. The cue delay is configurable.
- `usage.cost` may be absent. Show `none` rather than a guess.
