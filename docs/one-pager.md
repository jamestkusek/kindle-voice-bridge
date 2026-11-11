# Kindle Voice Bridge

**Ask your book a question out loud, without leaving the page.**

## The problem

I read a lot on a Kindle — academic papers and fiction. Constantly I want to ask
an LLM something the built-in dictionary can't answer: what does this expression
mean *here*, what is the author trying to convey in this sentence, what is this
reference pointing at. KOReader already has an LLM plugin that can answer those
questions, but you have to type them out on an e-ink keyboard. That is slow,
clumsy, and it breaks the reading flow — enough friction that I usually don't
bother.

The Kindle has no microphone and no speaker. But it does support Bluetooth audio
for VoiceView, and a Bluetooth headset has a microphone. So the question became:
can the Kindle capture headset mic audio, get it to an LLM, and bring the answer
back — with a tap and a spoken sentence instead of a paragraph of typing?

## Who it's for

Primarily me: a heavy Kindle reader who wants low-friction, in-context
explanations while reading. More broadly, anyone using KOReader's LLM features
who would rather talk than type, and readers for whom a slow on-screen keyboard
is a real barrier rather than an annoyance.

## What I built

A working prototype of the full loop: speak a question into a Bluetooth headset
and read the LLM's answer on the Kindle.

- **KOReader plugin (Lua)** — starts and stops the Bluetooth session, opens the
  live recording strip, shows the answer in a popup, and exposes the same action
  to KOReader gestures through the Dispatcher.
- **Kindle-side capture supervisor (Python 3.11)** — stops the stock Bluetooth
  daemon, takes the Broadcom UART (`/dev/ttymxc2`) from `bsa_server`, switches it
  to raw H:4, and relays it to the laptop over an outbound TCP connection (the
  Kindle blocks inbound connections). It restores stock Bluetooth on exit
  (observed, not guaranteed).
- **Laptop-side HFP Audio Gateway (Bumble)** — pairs with the headset using the
  exported link keys, negotiates HFP, opens the SCO voice link, and captures the
  mic stream. It detects and discards about 2.4 s of stale SCO buffer at the
  start, then records the clip.
- **Receiver and upload path (FastAPI)** — an authenticated
  `POST /v1/recordings` endpoint with strict WAV validation, size and duration
  caps, atomic storage, and a Kindle-side uploader. The receiver transcribes the
  clip with OpenRouter, asks a cheap LLM for a short answer, and writes both
  sidecars next to the WAV.
- **Local dashboard** — lists recordings, transcripts and answers, plays each
  clip, collapses the file metadata into a details disclosure, and refreshes
  live as new recordings and LLM responses arrive.
- **Audio tooling** — raw HCI payload to WAV conversion, DC-offset removal and
  peak normalisation, plus an offline replay tool for tuning the "is this live
  audio?" detector.

## What's proven, and what isn't

Proven on real hardware: Bluetooth handoff and restoration, Bumble driving the
Kindle controller over the relay, an HFP Audio Gateway reaching SCO, intelligible
voice capture, and the full loop in one session — capture, Wi-Fi upload,
transcription, LLM answer, and the answer shown in KOReader.

Still open: the audio format is a strong inference (PCM16 mono 8 kHz), not
controller-confirmed; Wi-Fi/Bluetooth coexistence during capture is not measured;
the laptop is still in the loop; the relay is plaintext and unauthenticated; and
only one headset and one firmware version have been tested. The question is sent
without the current page or chapter as context.

## What I'd do next

1. **Add reading context.** Send the current page, chapter and book title with
   the question, so answers can use the passage the reader is looking at.
2. **Remove the laptop.** Move the receiver to a hosted HTTPS service, or push
   capture and ASR onto the device, so normal use doesn't need a PC awake.
3. **Make it a tap, not an experiment.** A single start/stop control in the
   reader, visible status, and no manual key export or relay setup.
4. **Harden the audio path.** Confirm the real codec and sample rate, try
   wideband mSBC for better transcription, and cut the startup artifact and
   latency.
5. **Test the risky assumptions.** Measure SCO packet loss while uploading,
   battery cost, and recovery when a run crashes mid-handoff.
6. **Broaden support.** More headsets, more Kindle models and firmware, and an
   authenticated, encrypted relay.

## Status

Working prototype. The full loop works on real hardware, but it still needs a
laptop, a trusted LAN, and a manual session start.
