# Transcribing recordings

The receiver transcribes each recording **as it arrives**. A successful upload
returns 201, and a background task then sends the audio to OpenRouter. There is
no polling and no second process. It writes two sidecars next to the WAV and
never touches the audio:

```text
rec_20261111T114801Z_5bba38da….wav
rec_20261111T114801Z_5bba38da….txt               the transcript
rec_20261111T114801Z_5bba38da….transcript.json   status, model, usage, attempts
```

## Why not a transcription endpoint

OpenRouter has no `/audio/transcriptions` route. It exposes 48 models that
accept audio **input** on a chat completion, so a transcript is a chat request
with an `input_audio` content part:

```json
{
  "model": "mistralai/voxtral-small-24b-2507",
  "temperature": 0,
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "Transcribe the speech in this recording verbatim. …"},
    {"type": "input_audio", "input_audio": {"data": "<base64 wav>", "format": "wav"}}
  ]}]
}
```

That matters for quality: these are chat models, not ASR services, so the prompt
has to forbid a preamble or the transcript arrives wrapped in commentary.

## Choosing a model

Prices are per token, from `GET https://openrouter.ai/api/v1/models`, filtered
to models whose `architecture.input_modalities` include `audio`.

| Model | Prompt | Audio | Note |
| --- | --- | --- | --- |
| `mistralai/voxtral-small-24b-2507` | $0.10/M | **$0.10/M** | Purpose-built ASR (Voxtral Small 24B) |
| `google/gemini-2.5-flash-lite` | $0.10/M | $0.30/M | General model, strong audio understanding |
| `openai/gpt-audio-mini` | $0.60/M | $0.60/M | Chat-oriented audio model |
| `google/gemini-2.5-flash` | $0.30/M | $1.00/M | Overkill for 8 kHz narrowband |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | free | free | Reasoning model; rate-limited, not a transcriber |

**The default is `mistralai/voxtral-small-24b-2507`.** It is the only model on
the list that is an actual ASR model rather than a generalist that also hears
audio, and it is also the cheapest audio rate. For 8 kHz narrowband speech with
startup artifact, a model trained on speech should beat a generalist.

### Measured cost

The listing price is not the whole story, so these are the numbers from three
real 15-second clips:

| Clip | Audio tokens | Billed |
| --- | --- | --- |
| 15 s | 15 | $0.0015181 |
| 10 s | 14 | $0.0014190 |
| 15 s | 13 | $0.0013097 |

Voxtral bills **one audio token per second of audio**, at $0.0001 each, so:

```text
cost ≈ $0.0000043 (the text prompt) + $0.0001 × seconds of audio
     ≈ $0.0015 per 15-second clip
     ≈ $0.006 per minute of audio
```

That is about **thirty times** what comparing the `prompt` field alone suggests.
The `audio` price in the model listing is per audio token, and audio tokens are
roughly one per second — not the ~25-32 tokens per second that Gemini-style
audio tokenisation uses. Reading the listing's `prompt` price as the audio price
underestimates the bill by an order of magnitude.

For comparison, `google/gemini-2.5-flash-lite` at $0.0000003 per audio token with
~25 tokens per second lands near **$0.00013 per 15-second clip**, roughly eleven
times cheaper. So if volume matters, Gemini is the cheap option and Voxtral is
the accuracy option. At prototype volume the difference is cents.

## The answer model

After the transcript, the receiver asks a second model for a short answer to the
question. The default is `mistralai/mistral-small-24b-instruct-2501`. Override it
with `KVB_ANSWER_MODEL`.

The answer is two sentences, so a large model buys nothing. Measured on the
question "what is a thunderhead?":

| Model | Prompt / completion per million | Answer | Cost |
| --- | --- | --- | --- |
| `mistralai/mistral-nemo` | $0.029 / $0.030 | one sentence | $0.0000015 |
| `mistralai/mistral-small-24b-instruct-2501` | $0.050 / $0.080 | two sentences | $0.0000061 |
| `mistralai/ministral-3b-2512` | $0.100 / $0.100 | two sentences | $0.0000107 |
| `google/gemini-2.5-flash-lite` | $0.100 / $0.400 | two sentences | $0.0000198 |

Mistral Small 24B is the default because it gave the clearer two-sentence
answer at a cost that is not worth tuning further. Use `mistral-nemo` when one
sentence is enough and the lowest cost matters.

### If the transcripts come back poor

The recordings are 8 kHz mono PCM16. Voxtral is trained around 16 kHz, and the
provider may or may not resample. If output looks like mangled phonetics rather
than wrong words, try, in order:

1. `--model google/gemini-2.5-flash-lite` — different audio front end.
2. Upsample to 16 kHz before sending.
3. `--model google/gemini-2.5-flash` — more capacity, still cheap.

The worker records the model in each sidecar, so a comparison is just a matter
of running two workers against copies of the same files.

## Running it

The key is read from `OPENROUTER_API_KEY` and is never written to a sidecar, a
log line or an error message.

```powershell
$env:OPENROUTER_API_KEY = "<key>"
$env:KVB_STORAGE_DIR = "$env:LOCALAPPDATA\KindleVoiceBridge\recordings"
python tools/transcribe_worker.py --interval 5
```

Drain the backlog once and exit:

```powershell
python tools/transcribe_worker.py --once
```

Options: `--model`, `--max-attempts` (default 3), `--timeout` (default 90 s),
`--api-key-env` (default `OPENROUTER_API_KEY`).

## Behaviour

- Only `rec_<UTC stamp>_<32 hex>.wav` files are considered. The pattern is the
  one in `voice_bridge_server.storage`, shared with the review routes.
- A file with a successful sidecar is never sent again.
- A failed file is retried on the next poll up to `--max-attempts`, then left
  alone. `retryable` in the sidecar says whether the failure was transient
  (network, 429, 5xx) or permanent (bad key, unknown model, empty audio).
- The sidecars are written atomically. The WAV is opened read-only.
- The worker keeps running after an unexpected error and reports the exception
  type only.

## Limits

- The transcript is whatever the model returns. There is no confidence score,
  no word timings and no speaker separation.
- Nothing is redacted. A transcript of private speech is written in plain text
  next to the recording.
- The audio is sent to a third party. That is a deliberate choice for this
  prototype; the receiver itself stays local.
- The review page does not show transcripts yet. They are files on disk.
