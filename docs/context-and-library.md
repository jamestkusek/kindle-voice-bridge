# Reading context and the book library

One page on what the Kindle sends with a question, how the server identifies the
book, what it can resolve, how books are synced, and how to inspect the result.
The wire contract is in [protocol.md](protocol.md); the pages are in
[review-ui.md](review-ui.md).

## Context payload

The Kindle sends a pointer, not page text. Extracting page text costs about
0.5-1 s on the device, so it does not run in the ask path. The server resolves
the pointer against a synced book when it has one.

Flow:

1. The plugin writes `/tmp/voicebridge-context.json` before it writes the
   `start_recording` command. Every getter runs in its own `pcall`, so a build
   without one API loses only that field. No open document means no file.
2. The daemon reads and unlinks the file when the capture starts, so a stale
   file never attaches to a later recording.
3. The uploader sends the file bytes, base64url without padding, in the optional
   `X-KVB-Context` header on `POST /v1/recordings`.
4. The receiver decodes the header, writes `<id>.context.json` and records the
   `context` stage in the trace.

Payload, schema version 1:

```json
{
  "v": 1,
  "book": {"title": "", "authors": [""], "size_bytes": 0, "partial_md5": "", "format": "epub"},
  "pos": {"page": 142, "total_pages": 310, "fraction": 0.43, "chapter": ""},
  "locator": {"xpointer": "", "href": ""},
  "highlight": "text or null"
}
```

| Field | Meaning |
|---|---|
| `v` | Schema version. Only `1` is accepted. |
| `book.title`, `book.authors` | Book identity. `authors` is a list of names. |
| `book.size_bytes`, `book.partial_md5` | Aliases for matching a stored copy. `size_bytes` is the file size, `partial_md5` the KOReader checksum. |
| `book.format` | File suffix, such as `epub`. Only EPUB is synced and parsed. |
| `pos.page`, `pos.total_pages` | Current page and page count. |
| `pos.fraction` | `page / total_pages`, the input to fraction resolution. |
| `pos.chapter` | Chapter title from the table of contents, when the build has it. |
| `locator.xpointer` | CFI-like locator. Recorded, not resolved. |
| `locator.href` | Reserved; the plugin sends an empty string. |
| `highlight` | The selected text when the ask starts from a highlight, else `null`. |

Size limits:

- The decoded payload is at most 4096 bytes.
- The plugin crops, in order, the highlight, then the chapter, then the title,
  until the encoded file fits in 2500 bytes or the text fields are empty.
- The uploader refuses to send a header whose encoded value is over 4096 bytes,
  so an oversized file fails that upload before the request is made.

A missing or empty header leaves the messages and the model unchanged. A
malformed or oversized header never rejects the upload: the audio is stored, the
`context` stage records `rejected` with a reason, and the answer uses the
transcript alone.

| Reason | Cause |
|---|---|
| `too_large` | More base64 text or more decoded bytes than the cap. |
| `bad_base64` | A character outside the base64 alphabets. |
| `bad_json` | Valid base64 that is not UTF-8 JSON. |
| `bad_shape` | JSON that is not an object, a version other than `1`, or a section of the wrong type. |

The sidecar is written beside the WAV. Schema version 1:

```json
{"schema": "v1", "status": "ok", "parsed": {}, "header_bytes": 0}
```

`status: "invalid"` and a `reason` replace `parsed` when the header was
rejected. No sidecar is written when the header is absent.

## Book identity

```
book_key = "bk_" + sha256(normalised_title + "|" + normalised_authors)[:16]
```

Normalisation is NFKC, then casefold, then every run of non-alphanumeric
characters becomes one space, then the ends are trimmed. Title and authors take
part; size and partial md5 never do.

Size and partial md5 are aliases. `meta.json` keeps them in `sizes` and
`aliases`, so a stored book can still be found when the title changed between
editions or the metadata is missing. The lookup order is the key, then the md5
alias, then the size alias.

Title and authors merge editions. The trace records the key and the match label
that the resolver returns, so a wrong merge is visible on `/debug`.

## Resolution ladder

The server never reads page text from the device. It resolves the pointer, in
this order:

| Rung | Input | Result |
|---|---|---|
| `highlight` | `highlight` | The selected text, frame lines removed, at most 700 characters. |
| `fraction` | `pos.fraction` against a synced book | About 2000 characters centred on that fraction, assembled from whole and partial paragraphs. |
| `none` | no usable input | No passage. |

`xpointer` is recorded but not resolved in this version.

When every rung fails, the trace records one reason:

| Reason | Meaning |
|---|---|
| `no-library` | No resolver is configured on the receiver. |
| `resolve-failed` | The resolver raised. |
| `bad-result` | The resolver returned an unexpected shape. |
| `no-book` | No synced book matched the context. |
| `no-position` | The book matched but the context carries no usable fraction. |
| `no-text` | The book matched and the fraction produced no text. |
| `xpointer-unresolved` | Nothing else matched and an xpointer was present. |

A resolved passage takes the place of the highlight in the prompt. The answer
call then sends one user message:

```text
READING CONTEXT
Book: Dune by Frank Herbert
Page: 42 of 600
Chapter: 2
Passage:
...

END READING CONTEXT

Question: Who wrote Dune?
```

The frame is plain text, not a delimiter the model parses. Lines that would
forge the frame (`READING CONTEXT`, `END READING CONTEXT`, `Question:`) are
dropped from quoted book text, and the system prompt tells the model that the
block is quoted reference material. Without a title or a passage there is no
block, so a context-free ask sends the messages it always did.

`create_app_from_env` wires a resolver backed by the synced library. It matches
the book with `Library.find` and resolves the passage with
`context.resolve_passage`. `create_app(..., resolve=...)` remains an injection
point for tests; without a resolver an ask records rung `none` and reason
`no-library`.

## Library sync

```
<library>/<book_key>/meta.json    title, authors, aliases, sizes, char count, source file
<library>/<book_key>/text.json    paragraphs, cum_chars
<library>/.sync/<stamp>.json      one report per sync
```

`POST /api/library/sync` with the bearer token scans the configured source
directory and returns the report. `?dry_run=1` computes the same report and
writes nothing. No path is accepted: the endpoint always scans
`KVB_LIBRARY_SOURCE`, or `<KVB_STORAGE_DIR>/library-source` when it is unset.
Extracted books are stored under `KVB_LIBRARY_DIR`, or
`<KVB_STORAGE_DIR>/library` when it is unset. `GET /api/library` returns the
books and the newest report.

Point `KVB_LIBRARY_SOURCE` at a folder of EPUB files, such as a mounted Kindle
`documents` folder or a Calibre export. Keep the source and the store separate:
the scan treats every non-EPUB file under the source as `unsupported`.

One row per file:

| Status | Meaning |
|---|---|
| `new` | No stored book for this key. The folder is written. |
| `duplicate` | The stored copy has the same size and nanosecond mtime. Nothing is written. |
| `changed` | The key is known but the source size or mtime differs. The folder is rewritten. |
| `removed` | A stored book was not found in the scan. Its folder is kept. |
| `unsupported` | Not an `.epub` file. |
| `drm` | The archive carries `META-INF/encryption.xml`. |
| `parse_failed` | The archive could not be read or written. |

A `parse_failed` row carries its own reason: `format` (not an EPUB), `bad_zip`,
`bad_epub`, `too_large`, `no_metadata`, `read_error`, `write_failed`. A missing
source directory sets the report reason `source_missing` and is not an error.

Extraction uses the standard library only: `zipfile` for the archive,
`xml.etree.ElementTree` for `container.xml` and the OPF, `html.parser` for the
XHTML. Documents are read in spine order, and block elements end a paragraph.
Limits: 2000 zip entries, 50 MiB decompressed, 2 Mi characters extracted. An
archive over a limit fails as `too_large` and creates no book folder.

`meta.json` keeps `key`, `title`, `authors`, `aliases`, `sizes`, `format`,
`chars`, `added_at`, `updated_at`, `source_path`, `source_size` and
`source_mtime_ns`. `text.json` holds the paragraphs and the cumulative start
offset of each one, which is what fraction slicing indexes.

## Dev loop

`/debug` shows the trace feed, the resolved passage, the exact model messages
and the library. The full page is in [review-ui.md](review-ui.md).

`POST /v1/dev/ask` runs the normal pipeline without a microphone. It writes a
0.1 s silent WAV through the same store, records `origin: "dev"`, and returns
`{"id": "...", "trace": "..."}` with `201`. Limits: question 2000 characters,
system prompt 4000, model 200. The typed question replaces the transcript, so
the `transcribe` stage is recorded as `skipped`. A trace is readable from
`/api/traces` while the answer is still running, and its outcome is `pending`
until the pipeline finishes. Without an OpenRouter key the `transcribe` stage is
recorded as `skipped`, no answer call runs, and the ask still succeeds.

```powershell
$trace = Invoke-RestMethod -Method Post http://127.0.0.1:18767/v1/dev/ask `
  -Headers @{ Authorization = "Bearer $env:KVB_TOKEN" } `
  -ContentType "application/json" `
  -Body '{"question": "What is a thunderhead?"}'
Invoke-RestMethod "http://127.0.0.1:18767/api/traces/$($trace.id)"
```

`KVB_DEBUG_TEXT=0` hides prompt text, the resolved passage and the context text
fields from the trace files while keeping the stages, the timings and the
outcome. The response text is kept either way.

Unit tests for this area: `tests/server/test_context.py` (decoding, book keys,
prompt block, fraction slicing, resolution), `tests/server/test_library.py`
(EPUB extraction and sync reports), `tests/server/test_trace.py` (caps and text
hiding), `tests/server/test_debug.py` (routes, dev ask, sync), and
`tests/server/test_answer.py` (context in the prompt).

## Privacy

A trace carries the prompts, the resolved passage, the context payload and the
model response. Book text and highlighted text therefore reach the storage
directory. That directory is private and must stay off any synced folder, and
`/debug` is for the trusted LAN only: it is unauthenticated apart from the dev
ask and the sync. Do not expose it to the public internet, and delete traces
that must not stay on disk.
