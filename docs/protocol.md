# Voice Bridge Upload Protocol v1

Contract for milestone 1: one authenticated upload endpoint, one receiver
implementation, one client contract used both on the local network and (later)
on a hosted server.

- **Revision:** 1 (first milestone). This file is the single source of truth for
  the wire contract and the Python-facing receiver interfaces.
- **Receiver:** `server/voice_bridge_server/` (FastAPI + uvicorn, pure Python).
- **Client:** `kindle/voice_bridge/` uploader (implementation owned by another
  workstream; obligations in [§13](#13-client-kindle-uploader-obligations)).
- **Scope:** receive, validate, store, acknowledge. No transcription, no
  playback API, no deletion API, no streaming, no retries, no cloud provider.

### Provisional and unverified

The following are **design assumptions, not hardware findings**. They must be
re-checked on the PW4 before any claim of end-to-end success, and this document
must be revised if they are wrong.

| Marker | Assumption | Status |
|---|---|---|
| P1 | The captured audio can be represented as **mono PCM WAV** | **Unverified.** HFP/SCO may deliver encoded frames (e.g. CVSD/mSBC) or a representation that cannot be labelled PCM. If capture is encoded, either decode to PCM before upload or add a new content type in a later revision. Never label encoded bytes as PCM. |
| P2 | Sample rate is between 8 kHz and 48 kHz, 8 or 16 bit | **Unverified.** HFP narrowband is commonly 8 kHz; wideband 16 kHz. The receiver is deliberately permissive inside mono PCM. |
| P3 | 15-second maximum is a useful bound | Provisional product limit, not a device limit. |
| P4 | The Kindle has a usable TLS runtime, trust store and correct clock | **Unverified.** Certificate verification must never be disabled to work around this. |
| P5 | Wi-Fi works while a custom service owns the Bluetooth controller | **Unverified.** |
| P6 | A 2 MiB body cap is comfortably above the largest legal clip | Holds for every format P2 allows: 15 s x 48 kHz x 16 bit mono is 1,440,044 bytes. The duration check therefore binds before the size check. |

An acknowledgement proves receipt and structural validity only. It does **not**
prove intelligibility, correct routing or headset compatibility.

---

## 1. Contract summary

| Item | Value |
|---|---|
| Base URL (local) | `http://127.0.0.1:8000` (loopback default) |
| Base URL (LAN) | `http://<laptop-lan-ip>:8000` — only with explicit user approval |
| Base URL (remote) | `https://<host>` — verified TLS required |
| Upload | `POST /v1/recordings` |
| Health | `GET /healthz` (no auth) |
| Auth | `Authorization: Bearer <token>` |
| Content type | `audio/wav` (exactly; `multipart/form-data` is rejected) |
| Max body | 2 MiB = 2,097,152 bytes (configurable) |
| Max duration | 15.0 s (configurable) |
| Success | `201 Created` + JSON, only after the clip is stored |
| Retries | None. One attempt per recording. No idempotency key in v1. |
| Read/delete API | None in v1 (filesystem only) |
| Retention | **Proposed, pending user approval — see [§9](#9-storage-and-retention--proposed-pending-user-approval)** |

---

## 2. Roles, invariants, non-goals

**Invariants**

1. `201` is returned **only after** the complete body has been validated and
   written to its final storage location. A `201` never describes a partial
   file, a queued write or an in-flight copy.
2. Every accepted upload gets a **server-generated opaque ID**. The client never
   supplies an ID, filename or timestamp that affects storage.
3. The receiver never logs the request body, the token, the `Authorization`
   header or the audio content.
4. The receiver never retries, never re-uploads and never contacts the client.
5. The same path, headers, body format, status codes and JSON schemas are used
   locally and remotely. Only the base URL, the transport security and the token
   differ ([§11](#11-transport-security-and-deployment-modes)).

**Non-goals for v1:** streaming or chunked *semantic* uploads, resumable uploads,
transcription, playback/download endpoints, listing endpoints, deletion
endpoints, multi-user accounts, per-user quotas, idempotency keys, webhooks,
browser/CORS clients, compression.

---

## 3. Transport and binding

- HTTP/1.1 or later over TCP. Plain HTTP is permitted **only** on loopback, or on
  a trusted LAN with explicit user approval ([§11](#11-transport-security-and-deployment-modes)).
- Default bind: `127.0.0.1:8000`. The bind address and port are **not** part of
  `Settings`; they are supplied to uvicorn (`--host`, `--port`).
- For LAN access, bind the **specific** LAN address (e.g. `--host 192.168.1.50`),
  not `0.0.0.0`, so the service is not accidentally reachable on other
  interfaces. Document Windows firewall rules in `README.md` (owned elsewhere).
- The application must never be exposed to the public internet directly. Remote
  deployment terminates TLS in front of the app ([§11](#11-transport-security-and-deployment-modes)).
- No cookies, no sessions, no `Set-Cookie`, no CORS headers, no `Vary`, no
  response compression.
- The server does not implement a per-request read timeout in v1. Bounded client
  timeouts ([§13](#13-client-kindle-uploader-obligations)) and any reverse-proxy
  timeout are the protection. A stalled client may hold one worker; acceptable
  for the local milestone, documented as a limitation.
- Multiple uvicorn workers are allowed: the receiver keeps no cross-request
  state, IDs are random, and writes are atomic per file.

---

## 4. Endpoints

### 4.1 `POST /v1/recordings`

Registers one completed recording.

**Request headers**

| Header | Required | Rule |
|---|---|---|
| `Authorization` | yes | Exactly `Bearer <token>`. Scheme is case-insensitive; exactly one space; token compared in constant time. |
| `Content-Type` | yes | Media type must be `audio/wav` (case-insensitive). Parameters are ignored. Anything else, including `multipart/form-data`, `audio/wave`, `audio/x-wav`, `application/octet-stream`, `audio/wav; codecs=...` is accepted only if the parsed media type is exactly `audio/wav`. |
| `Content-Length` | recommended | If present and greater than `max_body_bytes`, the request is rejected with `413` **without reading the body**. |
| `Transfer-Encoding: chunked` | allowed | Used when `Content-Length` is absent. The cap is enforced while streaming ([§6](#6-request-body-handling)). |
| `Content-Encoding` | must be absent or `identity` | Any other value (`gzip`, `deflate`, `br`, ...) is rejected with `415`. The body is never decompressed. |
| `Accept` | ignored | Response is always `application/json`. |
| Any other header | ignored | Including `X-*` headers, `User-Agent`, client-supplied filenames. No header other than the four above changes behaviour. |

**Request body:** the complete WAV file, byte-for-byte, with no wrapper,
encoding, base64 or multipart envelope. Zero-length bodies are rejected.

**Query string:** not part of v1; the receiver ignores any query parameters and
must not change behaviour because of them.

**Trailing slash:** both `/v1/recordings` and `/v1/recordings/` must be routed to
the same handler **without an HTTP redirect**, so clients never have to replay a
POST body.

**Success:** `201 Created`, JSON body in [§8.1](#81-success-201).

### 4.2 `GET /healthz`

Unauthenticated liveness probe. Returns `200` with:

```json
{"status": "ok", "api_version": "v1"}
```

Rules: no authentication, no storage access, no disk write, no configuration
echo (no paths, no token, no limits, no hostnames). `HEAD /healthz` may return
`200` with no body. This endpoint exists so a test can prove the process is up
before exercising the authenticated path.

### 4.3 Every other path and method

| Case | Status | `error.code` |
|---|---|---|
| Unknown path | `404` | `not_found` |
| Known path, unsupported method (e.g. `GET /v1/recordings`, `PUT`, `DELETE`, `OPTIONS`) | `405` + `Allow` header | `method_not_allowed` |

Both return the JSON error envelope. `GET /v1/recordings/{id}` is **not**
implemented in v1 and therefore returns `404 not_found`; there is no download or
delete API.

---

## 5. Authentication

- Header form: `Authorization: Bearer <token>`.
- The scheme is matched case-insensitively (`Bearer`, `bearer`). The separator is
  exactly one ASCII space. Leading/trailing whitespace, extra spaces, tabs, a
  missing token, an empty token, a different scheme (`Basic`, `Token`) or a
  duplicate `Authorization` header all produce `401`.
- Comparison uses `secrets.compare_digest` over UTF-8 bytes. No prefix matching,
  no trimming, no case folding of the token itself.
- Failure response:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer realm="voice-bridge"
Content-Type: application/json

{"error": {"code": "unauthorized", "message": "Missing or invalid bearer token."}}
```

- The message is fixed and identical for missing, malformed and wrong tokens, so
  the response does not distinguish them.
- The token is never echoed, never logged (not even truncated or hashed), never
  included in errors and never written to storage.
- The health endpoint is the only unauthenticated route.

---

## 6. Request body handling

The receiver must handle both fixed-length and streamed bodies with the same
limit. Implementation is byte-counting over ASGI `receive` messages; do not rely
on `Content-Length` alone.

1. If `Content-Length` is present and `> max_body_bytes`, respond `413
   payload_too_large` immediately, without reading the body, and ensure the
   connection is closed rather than reused (the unread body would desynchronise
   the stream).
2. Otherwise read the body in chunks (suggested 64 KiB), accumulating into a
   single `bytes` object. After each chunk, if the running total exceeds
   `max_body_bytes`, stop reading, discard the buffer, respond `413
   payload_too_large` with `Connection: close`.
3. If the client disconnects or the stream ends before the declared
   `Content-Length`, respond `400 incomplete_body` if a response is still
   possible; otherwise drop the connection. Nothing is stored in either case.
4. If the body is empty (zero bytes, whether declared or streamed), respond `400
   empty_body`.
5. Only after the full body is buffered is it validated ([§7](#7-wav-validation-provisional))
   and stored ([§9](#9-storage-and-retention--proposed-pending-user-approval)).
   The buffer is never written to disk before validation passes.

Notes:

- Buffering the whole body in memory is intentional: the cap is 2 MiB, and it
  guarantees that a rejected upload leaves no artifacts.
- A body exactly equal to `max_body_bytes` is allowed; `max_body_bytes + 1` is
  rejected. The cap applies to the **received bytes**, i.e. the WAV file
  including its header and any trailing bytes.
- No decompression, no transcoding, no normalisation. Stored bytes are identical
  to received bytes.

---

## 7. WAV validation (provisional)

All checks run on the buffered body before storage. Any failure produces `400`
with a specific `error.code` and stores nothing.

### 7.1 Accepted container

- `RIFF` little-endian only. `RIFX` (big-endian) and `RF64` are rejected.
- `WAVE` form.
- Chunks walked from offset 12. Each chunk header is 8 bytes: 4-byte ASCII ID,
  4-byte little-endian `uint32` size. Payloads are word-aligned: an odd-sized
  payload is followed by one pad byte that is **not** counted in the size.
- Unknown chunks (`LIST`, `fact`, `cue `, `bext`, ...) are skipped.
- `fmt ` must appear before `data`. Duplicate `fmt ` or duplicate `data` chunks
  are rejected.
- Trailing bytes after the `data` payload are ignored (they still count toward
  the body cap and are preserved in the stored file).

### 7.2 Accepted format

| Field | Requirement |
|---|---|
| `audio_format` | `1` (PCM). `0xFFFE` (WAVE_FORMAT_EXTENSIBLE) is rejected in v1, even when its subformat is PCM. |
| `num_channels` | `1` (mono) |
| `bits_per_sample` | `8` or `16` |
| `sample_rate` | `8000 <= rate <= 48000` |
| `block_align` | must equal `num_channels * bits_per_sample // 8` |
| `byte_rate` | must equal `sample_rate * block_align` |
| `fmt ` chunk size | `>= 16`; bytes beyond 16 are ignored |
| `data` size | `> 0` and a multiple of `block_align` |
| `data` size vs body | declared size must not exceed the bytes actually remaining after the data chunk header |

Sample values are **not** inspected. A silent or clipping clip is structurally
valid and is accepted; intelligibility is out of scope for the receiver.

### 7.3 Duration

```
bytes_per_second = sample_rate * block_align
duration_seconds = data_bytes / bytes_per_second
```

Rejection test uses integer arithmetic to avoid float drift:

```
reject if data_bytes > max_duration_seconds * bytes_per_second
```

So a clip of exactly `max_duration_seconds` is accepted and one byte more is
rejected. `duration_seconds` is reported rounded to 3 decimal places.

### 7.4 Error codes for this section

| Condition | `error.code` |
|---|---|
| Body is zero bytes | `empty_body` |
| Body ends early / client disconnect | `incomplete_body` |
| Not RIFF/WAVE, chunk overruns body, missing `fmt ` or `data`, `data` before `fmt `, duplicate `fmt `/`data`, `data` size not a multiple of `block_align`, inconsistent `block_align`/`byte_rate`, body shorter than 44 bytes | `invalid_wav` |
| Format tag not PCM, extensible, not mono, bits not 8/16, sample rate outside 8 kHz-48 kHz | `unsupported_wav_format` |
| Duration exceeds the limit | `duration_exceeds_limit` |

### 7.5 Reference parser interface (contractual)

`server/voice_bridge_server/audio.py` exposes a pure function so the WAV rules
can be unit-tested without HTTP (`tests/server/test_audio.py`):

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class WavInfo:
    sample_rate_hz: int
    channels: int
    bits_per_sample: int
    data_bytes: int
    duration_seconds: float

class WavError(ValueError):
    code: str      # one of the codes in §7.4
    message: str   # human readable, no secrets, no paths

def parse_wav(body: bytes, *, max_duration_seconds: float) -> WavInfo: ...
```

`parse_wav` raises `WavError` on any failure and never writes, logs or mutates
state. It must not raise bare `struct.error`/`IndexError` for any input; the
receiver maps unexpected exceptions to `invalid_wav` (or `internal_error` if the
fault is not input-related).

---

## 8. Responses

All responses are `application/json` with `Cache-Control: no-store`. Clients
must branch on `error.code`, never on `error.message`, and must ignore unknown
JSON fields for forward compatibility.

### 8.1 Success `201`

```json
{
  "id": "rec_20261110T130700Z_9f2c1a4b8e6d0f3c5a7b9d1e2f4a6c8b",
  "status": "stored",
  "received_bytes": 16044,
  "duration_seconds": 1.0,
  "sample_rate_hz": 8000,
  "channels": 1,
  "bits_per_sample": 16,
  "sha256": "0d5f2f0b2f5f4f1c9b0f1b6a0f7c3d2e1a4b5c6d7e8f9012345678abcdef0123",
  "stored_at": "2026-11-10T13:07:00.123456+00:00"
}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Server-generated opaque recording ID, regex `^rec_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{32}$`. Safe as a filename component; contains no client data. |
| `status` | string | Always `"stored"` in v1. Reserved for future states. |
| `received_bytes` | int | Exact body length in bytes. |
| `duration_seconds` | number | `data_bytes / bytes_per_second`, rounded to 3 decimals. |
| `sample_rate_hz` | int | From `fmt `. |
| `channels` | int | Always `1` in v1. |
| `bits_per_sample` | int | `8` or `16`. |
| `sha256` | string | Lowercase hex SHA-256 of the exact received bytes. Lets a client and test prove byte-identical delivery. |
| `stored_at` | string | RFC 3339 / ISO 8601 UTC timestamp with offset and microseconds, generated after the write completes. |

The response deliberately contains **no filesystem path, hostname, storage
directory or client IP**. The file is at `<storage_dir>/<id>.wav`; that mapping is
documented here and in `README.md`, not returned over the wire.

No `Location` header: v1 has no GET endpoint, so a `Location` would point at a
`404`.

### 8.2 Error envelope

```json
{
  "error": {
    "code": "invalid_wav",
    "message": "WAV data chunk size is not a multiple of block_align."
  }
}
```

`code` is stable and machine-readable. `message` is a short, non-sensitive
explanation: never contains the token, the body, the storage path, a stack trace
or a client-supplied string.

### 8.3 Status code and code table

| HTTP | `error.code` | When |
|---|---|---|
| `201` | — | Stored successfully |
| `400` | `empty_body` | Zero-byte body |
| `400` | `incomplete_body` | Stream ended before the declared length |
| `400` | `invalid_wav` | Structural WAV failure (§7.4) |
| `400` | `unsupported_wav_format` | Valid container, unsupported audio format (§7.4) |
| `400` | `duration_exceeds_limit` | Clip longer than `max_duration_seconds` |
| `401` | `unauthorized` | Missing, malformed or wrong bearer token |
| `404` | `not_found` | Unknown path, including any `/v1/recordings/{id}` |
| `405` | `method_not_allowed` | Known path, wrong method (`Allow` header set) |
| `413` | `payload_too_large` | Body over `max_body_bytes` (declared or streamed) |
| `415` | `unsupported_media_type` | `Content-Type` not `audio/wav`, or non-identity `Content-Encoding` |
| `500` | `storage_error` | Storage write/rename/fsync failed; nothing stored |
| `500` | `internal_error` | Unexpected fault; generic message only |

### 8.4 Check precedence (normative)

Tests depend on this order; implementations must follow it.

1. Route match → `404` / `405`.
2. `Authorization` → `401`.
3. `Content-Encoding` and `Content-Type` → `415`.
4. `Content-Length > max_body_bytes` → `413` (body not read).
5. Body read, with streaming cap → `413` / `400 incomplete_body`.
6. Zero-length body → `400 empty_body`.
7. WAV structure, format, duration → `400` (§7.4).
8. Storage → `500 storage_error`, else `201`.

Authentication precedes every body-related check, so an unauthenticated caller
learns nothing about limits or formats. `405` precedes `401`: an unsupported
method on a known path is reported as such without reading credentials.

---

## 9. Storage and retention — PROPOSED, PENDING USER APPROVAL

> **PENDING USER APPROVAL.** The rules below are a *proposal* needed to make the
> receiver testable. They are **not** an approved retention policy. Until the user
> explicitly approves them:
>
> - implementers may build to them, keeping every value configurable;
> - they must not be described to the user, in the handoff or in `README.md`, as
>   agreed or final;
> - no test may store real speech from the headset under these rules without the
>   coordinator confirming approval;
> - if the user changes the policy, only this section and the storage boundary
>   change; the HTTP contract in §3-§8 stays as written.

Proposed rules:

| ID | Proposal |
|---|---|
| R1 | The storage directory is **explicitly configured**. There is no default and no fallback to the working directory. `create_app_from_env` fails at startup if `KVB_STORAGE_DIR` is unset, empty or not a usable directory. |
| R2 | **No automatic deletion or expiry** in milestone 1. Clips persist until a human deletes them. No TTL, no size-based pruning, no background cleanup task. |
| R3 | **No read or delete HTTP API.** Access is filesystem-only: play with any WAV player, delete with the file manager. |
| R4 | **Failed uploads leave no artifacts.** Nothing is written before validation passes. An in-progress temp file is removed on any error. A crash may leave `.incoming-*.part` files; these are **not** cleaned automatically and are deleted manually. |
| R5 | Filenames are `<id>.wav` only. No client-supplied names, no timestamps from the client, no original filename metadata. |
| R6 | Client side: after a failed upload the sample is discarded once the error is shown; no automatic retry and no silent retention ([§13](#13-client-kindle-uploader-obligations)). |
| R7 | The storage directory must be outside any cloud-synced folder (OneDrive, Dropbox, iCloud) and must be excluded from version control. `.gitignore` is owned by another workstream; this document only states the requirement. |
| R8 | A `201` is an acknowledgement of receipt and structural validity, not evidence of intelligibility. |

Rationale for R1-R3: the first milestone needs to prove delivery and manual
playback. An expiry policy, a download endpoint or a delete endpoint would each
add an authenticated surface that has not been reviewed. Manual deletion is the
smallest policy that satisfies "the user can remove samples".

### 9.1 Write procedure (proposed, part of the storage boundary)

1. Generate `id` ([§9.2](#92-recording-ids)).
2. Create a temp file in the **same directory** as the final file, named
   `.incoming-<uuid4hex>.part`, opened exclusively (`O_CREAT|O_EXCL`), mode
   `0o600` on POSIX (ignored on Windows).
3. Write all bytes, `flush()`, `os.fsync()`, close.
4. Verify the temp file size equals `len(body)`.
5. If `<storage_dir>/<id>.wav` already exists, unlink the temp file and raise
   `FileExistsError` (an `OSError`) — never overwrite an existing clip.
6. `os.replace(temp, <storage_dir>/<id>.wav)` — atomic on the same volume.
7. `201` only after step 6 returns. On POSIX also fsync the directory
   best-effort; on Windows this is a no-op.
8. On any failure: close and unlink the temp file (best effort), then `500
   storage_error`. Never leave a final file behind.

IDs are 128-bit random, so a collision is negligible; the existence check in
step 5 makes the impossible case fail loudly (`500 storage_error`) instead of
silently destroying a previously stored clip. The receiver does not retry with a
new ID in v1.

### 9.2 Recording IDs

```
rec_<UTC yyyymmdd>T<UTC HHMMSS>Z_<32 lowercase hex chars>
```

- 53 characters, `[a-z0-9_]` only, no separators, no `..`, no drive letters.
- The timestamp is **server** time at acceptance; it is not authenticated
  metadata and clients must not parse it for logic.
- The hex suffix comes from a cryptographic random source
  (`secrets.token_hex(16)`), not from the body, the token or the client.
- Contractual helper (location is an implementation detail, format is not):

```python
def new_recording_id(now: datetime | None = None) -> str: ...
```

### 9.3 Disk-full and permission failures

Any `OSError` during the write procedure maps to `500 storage_error` with a
message such as `"Could not store recording."` — no path, no errno text that
reveals the filesystem layout. The temp file is removed. The client keeps its
sample only long enough to display the failure (R6).

---

## 10. Logging and secret hygiene

Allowed per accepted upload, one line: timestamp, `id`, `received_bytes`,
`duration_seconds`, HTTP status. Allowed per rejected upload: timestamp, status,
`error.code`, `received_bytes` if known. Allowed at startup: bind address, port,
storage directory **existence** (not contents), limits.

Forbidden everywhere, including debug levels, exception tracebacks and access
logs:

- the token, any prefix or hash of it, the `Authorization` header;
- the request body, any audio bytes, any base64 of audio;
- WAV sample values, transcripts, book content, pairing keys, device serials;
- full filesystem paths in HTTP responses (log lines may include the storage
  directory, responses may not).

The default uvicorn access log (method, path, status) is acceptable; body- or
header-logging middleware is not. Do not add middleware that buffers or echoes
request headers. Errors are logged by code, not by dumping the request.

---

## 11. Transport security and deployment modes

One contract, three modes. The path, headers, body, status codes and JSON are
identical in all of them.

| Mode | Transport | Token | Notes |
|---|---|---|---|
| **Loopback test** (default) | `http://127.0.0.1:8000` | Any non-empty dev token | Only reachable from the laptop. Safe for the known-sample test. |
| **Trusted-LAN test** | `http://<laptop-lan-ip>:8000` | Freshly generated, ≥32 chars | **Requires explicit user approval.** Audio and token are visible to anyone on that network. Never expose beyond the LAN. Rotate the token before any remote use. |
| **Remote / hosted** | `https://<host>` with certificate verification | Newly generated for the deployment, never one used over plain HTTP | Verified TLS is mandatory from the first test. No self-signed exceptions, no `verify=False`, no "temporarily disable validation". |

Rules:

- The Kindle client must validate certificates against its trust store. If the
  Kindle lacks a usable trust store or clock (P4), fix the client environment or
  use the loopback/LAN mode; **never** disable verification.
- Remote deployment terminates TLS at a reverse proxy or platform edge (Caddy,
  nginx, a VM with a real certificate, or the provider's TLS). The app itself
  speaks plain HTTP on a private interface behind that proxy.
- The proxy must enforce a body limit **slightly above** `max_body_bytes` (e.g.
  3 MiB) so the app — not the proxy — produces the JSON `413`. A proxy limit
  exactly equal to the app limit would replace the contract's error body with the
  proxy's HTML page.
- The proxy should also set a read timeout (≥30 s) and must not log request
  bodies or `Authorization` headers.
- No `X-Forwarded-*` trust is required in v1: the receiver does not use client
  IPs for decisions or responses, and does not log them.
- Rotate the token whenever it may have crossed plain HTTP, and whenever a
  deployment changes.
- Container/deployment mechanics (image, volume mount for `KVB_STORAGE_DIR`,
  `0.0.0.0` binding *inside* the container, persistent volume requirement) are
  documented by the deployment workstream. Contractually: the container reads
  `KVB_TOKEN` and `KVB_STORAGE_DIR` from the environment, listens on port 8000,
  and requires a persistent volume — ephemeral container storage does not satisfy
  R2.

---

## 12. Python-facing receiver interfaces

These are the stable interfaces the receiver and its tests are written against.
Only `config.py` and `app.py` names are contractual module paths; anything else
is an implementation detail.

### 12.1 `voice_bridge_server.config.Settings`

```python
from __future__ import annotations

import pathlib
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    storage_dir: pathlib.Path
    max_body_bytes: int = 2 * 1024 * 1024
    max_duration_seconds: float = 15.0
```

Validation, performed in `__post_init__` (frozen dataclass: use
`object.__setattr__` when normalising):

| Field | Rule | On violation |
|---|---|---|
| `token` | `str`, non-empty, and contains no ASCII whitespace (space, tab, CR, LF) because it travels in a header. Used verbatim, never trimmed. | `ValueError` |
| `storage_dir` | Accepts `str` or `os.PathLike`; normalised to `pathlib.Path(value).expanduser().resolve()`. Must not be empty. | `ValueError` |
| `max_body_bytes` | `int` (not `bool`), `> 0`. | `ValueError` |
| `max_duration_seconds` | `float`/`int` (not `bool`), finite, `> 0`. | `ValueError` |

`Settings` is pure: it performs **no filesystem access, no environment reads and
no logging**. Filesystem checks belong to `create_app`. Recommended token
generation is `secrets.token_urlsafe(32)`; v1 enforces only "non-empty, no
whitespace" so tests can use short tokens, and `README.md` documents the
≥32-character requirement for LAN and remote use.

### 12.2 `voice_bridge_server.app.create_app`

```python
def create_app(
    settings: Settings,
    *,
    store: RecordingStore | None = None,
) -> FastAPI: ...
```

- Required call form is `create_app(settings)`. The keyword-only `store` is a
  test seam; implementations **must** accept it.
- Startup validation, in order:
  1. `settings.storage_dir` is created if missing (`mkdir(parents=True,
     exist_ok=True)`);
  2. if it exists but is not a directory → `RuntimeError`;
  3. write a probe file `.kvb-write-probe` in it, then delete it; failure →
     `RuntimeError` naming the directory and "not writable".
  The probe runs even when a fake `store` is injected, so configuration errors
  surface identically in tests and in production.
- `store=None` means `LocalFileStore(settings.storage_dir)`.
- The returned app exposes `app.state.settings` and `app.state.store` for tests,
  and must work with `httpx.ASGITransport` and `fastapi.testclient.TestClient`
  without a network socket.
- `create_app` never reads environment variables and never logs the token.

### 12.3 `voice_bridge_server.app.create_app_from_env`

```python
from collections.abc import Mapping

def create_app_from_env(environ: Mapping[str, str] | None = None) -> FastAPI: ...
```

Used by uvicorn `--factory`. `environ=None` means `os.environ`.

| Variable | Required | Parse |
|---|---|---|
| `KVB_TOKEN` | yes | Verbatim string; validated by `Settings` |
| `KVB_STORAGE_DIR` | yes | `expanduser()` + `resolve()`; must be absolute after resolution |
| `KVB_MAX_BODY_BYTES` | no | Base-10 `int`; blank/whitespace means unset |
| `KVB_MAX_DURATION_SECONDS` | no | `float`; blank/whitespace means unset |

- Missing or blank required variable → `RuntimeError` naming the variable only,
  never its value: `"KVB_TOKEN is not set"`, `"KVB_STORAGE_DIR is not set"`.
- Unparseable number → `RuntimeError` naming the variable and the expected type.
- Invalid combination (e.g. `max_body_bytes=0`) surfaces as `ValueError` from
  `Settings`; the factory does not swallow it.
- No `.env` loading, no config file, no CLI flags in v1 (no `python-dotenv`
  dependency).
- Example start command, run from `server/` so the package is importable without
  installation:

```powershell
$env:KVB_TOKEN = "dev-only-token-replace-me"
$env:KVB_STORAGE_DIR = "C:\Users\james\Desktop\Claude Research\kindle-voice-bridge\.data\recordings"
python -m uvicorn voice_bridge_server.app:create_app_from_env --factory --host 127.0.0.1 --port 8000
```

### 12.4 Storage boundary

```python
from typing import Protocol
import pathlib

class RecordingStore(Protocol):
    def save(self, recording_id: str, body: bytes) -> pathlib.Path:
        """Persist body under recording_id and return the final path.

        Either persists the complete body and returns the final path, or raises
        OSError and leaves no final file. Must be atomic with respect to
        concurrent readers: a partially written file is never visible under the
        final name.
        """
```

- `LocalFileStore(storage_dir: pathlib.Path)` implements [§9.1](#91-write-procedure-proposed-part-of-the-storage-boundary)
  and is the v1 default.
- The protocol is the only seam needed for tests: a fake that raises `OSError`
  proves `500 storage_error`; a fake that records arguments proves the app
  passes the exact received bytes and the generated ID.
- A future object-storage backend implements the same protocol without touching
  the HTTP layer. That is the portability boundary; no provider SDK may be
  imported by `app.py`, `config.py` or `audio.py`.
- The store does not validate WAV, does not generate IDs and does not log.

---

## 13. Client (Kindle uploader) obligations

The uploader may be implemented independently against this section alone.

1. **URL:** `{base_url}/v1/recordings`, where `base_url` is configured, not
   hard-coded. No query string, no trailing slash required.
2. **Headers:** `Authorization: Bearer <token>`, `Content-Type: audio/wav`,
   `Accept: application/json`, and `Content-Length` when the runtime supports a
   fixed-length body. Chunked encoding is accepted by the server but not
   preferred.
3. **Body:** the complete WAV bytes, unmodified, with no wrapper or base64.
4. **One attempt.** No automatic retry, no backoff loop, no re-send after a
   timeout. Each user-initiated send is one POST and produces at most one stored
   clip. If the response is lost after the server stored the file, the client
   shows an error and the user decides whether to record again; a duplicate may
   then exist. That is accepted in v1 (no idempotency key).
5. **Timeouts:** bounded, e.g. 5 s connect and 20 s total on the LAN, 30 s total
   for a remote host (cold starts). On timeout: show an error, discard the
   sample (R6), do not retry.
6. **Success:** require `201` **and** a parseable JSON body with a non-empty `id`.
   Show the `id` (or a short suffix) as confirmation. Do not claim success on any
   other status, and do not treat a transport-level "connection closed" as
   success.
7. **Failure:** branch on `error.code` when the body parses; otherwise report the
   status code. Map at least: `unauthorized` → "check the token",
   `payload_too_large` / `duration_exceeds_limit` → "recording too long",
   `unsupported_media_type` / `invalid_wav` / `unsupported_wav_format` → "the
   recording format was rejected" (a client bug or a P1/P2 mismatch),
   `storage_error` / `internal_error` → "server could not store the recording",
   `not_found` / `method_not_allowed` → "server URL is wrong".
8. **Secrets:** never log the token, never include it in UI text, never send it
   to any host other than the configured `base_url`, and never send it over plain
   HTTP to a non-loopback address unless the user approved the LAN test.
9. **TLS:** verify certificates against the system trust store. Never disable
   verification, never accept a self-signed certificate, never fall back to HTTP
   automatically.
10. **Audio:** do not log audio bytes, do not write the sample to persistent
    storage beyond the agreed retention policy, and do not include book content
    in the request.
11. **Format:** send only bytes that are genuinely PCM WAV per §7. If capture
    yields encoded frames (P1), stop and raise the protocol question instead of
    labelling them PCM.

---

## 14. Testing contract

### 14.1 Known sample (no hardware, no network)

Generate deterministically with the standard library; do not commit audio files.

```python
import io
import math
import struct
import wave


def make_wav(seconds: float = 1.0, rate: int = 8000,
             freq: float = 440.0, amplitude: float = 0.2) -> bytes:
    frames = bytearray()
    for i in range(int(rate * seconds)):
        value = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", value)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()
```

`make_wav()` is 16,044 bytes and reports `duration_seconds == 1.0`. A 15 s clip
at 8 kHz/16-bit mono is 240,044 bytes; at 48 kHz/16-bit mono 1,440,044 bytes —
both under the 2 MiB cap (P6).

### 14.2 Health and manual smoke test

```powershell
curl.exe -sS http://127.0.0.1:8000/healthz
# {"status":"ok","api_version":"v1"}

curl.exe -sS -X POST http://127.0.0.1:8000/v1/recordings `
  -H "Authorization: Bearer $env:KVB_TOKEN" `
  -H "Content-Type: audio/wav" `
  --data-binary "@sample.wav" `
  -w "`n%{http_code}`n"
```

Expected: `201` and the §8.1 body; then
`<storage_dir>/<id>.wav` exists, is byte-identical to `sample.wav`, and plays.

### 14.3 Required test matrix

Tests exercise the HTTP surface through `create_app` (ASGI transport) or a real
loopback uvicorn instance. `parse_wav` may be unit-tested directly.

| # | Case | Expectation |
|---|---|---|
| 1 | Valid 1 s sample, valid token | `201`, schema valid, file byte-identical, `sha256` matches |
| 2 | Valid 15.0 s sample | `201`, `duration_seconds == 15.0` |
| 3 | 15.0 s + one `block_align` frame | `400 duration_exceeds_limit`, nothing stored |
| 4 | Missing `Authorization` | `401 unauthorized`, `WWW-Authenticate` present, nothing stored |
| 5 | Wrong token, `Basic`, empty bearer, two spaces | `401 unauthorized` each |
| 6 | `Content-Type: audio/mpeg` / `multipart/form-data` / missing | `415 unsupported_media_type` |
| 7 | `Content-Encoding: gzip` | `415 unsupported_media_type` |
| 8 | Body of `max_body_bytes + 1` with `Content-Length` | `413 payload_too_large`, body not read |
| 9 | Chunked body exceeding the cap | `413 payload_too_large`, connection closed, nothing stored |
| 10 | Truncated WAV (cut mid-`data`) | `400 invalid_wav` |
| 11 | `RIFF` magic replaced / `RIFX` / `RF64` | `400 invalid_wav` |
| 12 | Stereo, 24-bit, `0xFFFE`, 96 kHz, 4 kHz | `400 unsupported_wav_format` |
| 13 | `data` size not a multiple of `block_align` | `400 invalid_wav` |
| 14 | Empty body | `400 empty_body` |
| 15 | Fake store raising `OSError` | `500 storage_error`, no final file, temp removed |
| 16 | `GET /v1/recordings`, `DELETE`, `OPTIONS` | `405 method_not_allowed` + `Allow` |
| 17 | `GET /v1/recordings/{id}`, unknown path | `404 not_found` |
| 18 | `GET /healthz` with no auth | `200`, no config leakage |
| 19 | `/v1/recordings/` trailing slash | `201`, no redirect |
| 20 | Two sequential uploads | Distinct IDs, two files |
| 21 | Captured log output for cases 1, 4, 5 | No token, no audio, no `Authorization` value |
| 22 | `Settings` validation, env factory | `ValueError` / `RuntimeError` per §12.1, §12.3 |

### 14.4 What these tests do not prove

No test in this section proves PW4 capture, headset compatibility, HFP/SCO
routing, the real audio format, Wi-Fi/Bluetooth coexistence, TLS on the Kindle,
LAN reachability or hosted deployment. Those require hardware and user approval
([§15](#15-hardware-assumptions-and-open-questions)).

---

## 15. Hardware assumptions and open questions

Flagged for the hardware workstream; each answer may require a protocol revision.

1. **Actual capture format (P1, P2).** If the PW4/HFP path yields encoded frames,
   v1's `audio/wav` contract is wrong. Options: decode to PCM in the uploader
   (preferred, keeps the contract) or add `audio/sbc` in a v2 with its own
   validation. Do not relabel bytes.
2. **Real sample rate and bit depth.** If the device produces 16 kHz mono 16-bit,
   nothing changes. If it produces 8-bit or a rate outside 8-48 kHz, revise §7.2.
3. **Duration limit (P3).** Confirm 15 s is usable end to end, including capture
   start latency and the time to close the voice link before upload.
4. **TLS on the Kindle (P4).** Confirm a trust store and a roughly correct clock
   before any HTTPS test. A wrong clock fails verification; the fix is the clock,
   not `verify=False`.
5. **Wi-Fi coexistence (P5).** If upload fails while the custom service owns the
   controller, restore native Bluetooth before sending and update
   `docs/recovery.md` (owned elsewhere).
6. **Upload timing.** `201` after `os.replace` on a laptop disk is fast; confirm
   the client timeout is generous enough for a slow SD card or a cold remote
   start.
7. **Client HTTP capability.** Confirm the Kindle runtime can send a fixed
   `Content-Length` and set headers; if only chunked is available, §6 already
   supports it.
8. **Retention approval.** R1-R8 are pending user approval; the hardware
   workstream must not assume clips may be retained indefinitely on the device or
   the laptop.

---

## 16. Change control

- This document is the contract. Changes require coordinator approval and must be
  reflected in the receiver, the uploader and both test suites in the same batch.
- Additive changes that do not break v1: new optional response fields, new
  `error.code` values for new conditions, new optional environment variables.
- Breaking changes (path, auth scheme, content type, required fields, status
  semantics) require a new path version (`/v2/...`), not a silent edit.
- Provisional items P1-P6 and the pending retention policy (§9) are expected to
  change after hardware work; the HTTP envelope around them is intended to stay
  stable.
