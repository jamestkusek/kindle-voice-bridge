# Kindle Voice Bridge: Project Plan

## Goal

Record a short spoken prompt from a Bluetooth headset on the Kindle and send it over Wi-Fi to a receiver running on the Windows laptop. Design the receiver so it can later be moved to a remote server:

```text
Bluetooth headset microphone
        │ HFP voice connection
        ▼
Kindle Paperwhite 10th generation (PW4)
        │ capture a short recording
        │ send the completed recording over Wi-Fi
        ▼
Windows laptop running the receiver on the local network
        └─ receive and verify the recording for testing
```

The first milestone is **reliable, intelligible microphone audio captured on the Kindle and delivered to the laptop receiver**. For this local phase, the laptop must be awake and running the service, and reachable from the Kindle. Later, deploy the receiver remotely so normal use no longer depends on the laptop.

The server must be reachable and the headset connected; Wi-Fi alone cannot guarantee success. A later milestone can add speech recognition, an LLM, answers and reading context.

## Known setup and decisions

- **Kindle:** Paperwhite 10th generation / PW4, last reported firmware **5.18.1.1.1**. Confirm the current version before hardware work.
- **Jailbreak:** the user reports the Kindle has been jailbroken using [SpiderCat](https://github.com/KindleModding/SpiderCat). Verify the installed tools and update-blocking state rather than assuming their current configuration.
- **Headset:** generic Bluetooth headset with a microphone that works for calls on other devices. Its compatibility with the Kindle implementation remains untested.
- **Server:** initially run on the Windows laptop on a trusted local network. A later hosted deployment should not require the laptop to stay awake; no provider is committed.
- **Interface:** a KOReader plugin is the initial candidate for controls while reading, but the capture service must be independent of KOReader. KOReader does not support DRM-protected books or KFX; if those are essential, evaluate a separate native Kindle interface. Installation and compatibility have not been confirmed.
- **Interaction:** explicit tap to start and stop; send after recording finishes. Start with an approximately 15-second limit. Live streaming is deferred.
- **Device constraints:** this is the user's only Kindle. Prefer manually started experiments and a tested recovery path over automatic startup or persistent changes. Physical-device work requires the user's explicit agreement.

## Architecture

### Kindle capture and interface

A manually launched service will attempt to use the headset's Bluetooth **HFP** voice connection. A userspace Bluetooth stack such as [Bumble](https://github.com/google/bumble) is the leading candidate, not a proven PW4 solution. The [Kindle 8 / KT3 project](https://github.com/yzgpk2002/kindle8-offline-home) demonstrates headset capture on a different model; it is a device-specific research snapshot, not a ready-to-install PW4 package. Check dependencies and licences before reusing code.

First inspect the PW4's controller, transport, runtime, Bluetooth services and audio routing. It is not established that this specific device can expose the headset voice data over the required HCI path. Controller ownership, HFP negotiation, actual audio representation and intelligibility all need hardware verification. Do not label encoded Bluetooth data as PCM without validation.

After a successful capture, close the voice link and send the completed recording. Stock Bluetooth may be unavailable while a custom service owns the controller. Test whether Wi-Fi works in that state; if necessary, investigate restoring stock Bluetooth before upload. The service must attempt orderly restoration on exit or error, but recovery cannot be guaranteed.

The initial UI should provide start, stop/send, cancel and clear recording/upload/error status without blocking the reading interface. Capture must start only through an explicit action. Keep recordings time- and size-bounded, preferably in RAM; decide and document the handling and deletion of a failed upload before implementation.

### Portable receiver: local first, remotely deployable later

Build a provider-independent HTTP receiver, initially running on Windows, using a standard Python web framework such as FastAPI with explicit dependencies. Keep the endpoint, bind address, port, authentication secret and storage location configurable. Define one upload contract used in both environments; keep Kindle addresses and provider-specific services out of core application logic. Provide a container deployment option and document the start command and configuration so a conventional hosted service or VM can run the same application. Platform-specific serverless environments may still require an adapter; portability is a design goal, not an untested guarantee.

Bind to loopback by default; explicitly configure a private LAN interface when the Kindle needs access, and document Windows firewall settings. Authenticate uploads and limit request sizes and timeouts. Prefer HTTPS with certificate validation on the LAN when practical; for an initial trusted-LAN-only test, authenticated HTTP may be used with user approval, understanding that audio and the token are visible on the network. Never expose this HTTP endpoint to the public internet. Remote deployment requires HTTPS with certificate verification and authentication from its first test; rotate any credential used over plain HTTP before remote use.

Store credentials outside public code and logs. An acknowledgement proves receipt, not intelligibility. Define where samples are retained, how they can be played for inspection, and when they are deleted. Local file storage is acceptable for the first phase; define a storage boundary and document persistent storage for hosted deployments. Do not assume that serverless temporary storage is durable.

**Initial provider shortlist (researched 2026-11-10; selection pending):**

- [Fly.io](https://docs.fly.io/about/free-waiver) can run a conventional containerized app, making it a candidate for migrating the local receiver. Its under-$5 bill waiver is conditional, requires a card and is not a spending cap; bills of $5 or more are charged in full.
- [Render](https://render.com/docs/free) can host a conventional app and has a free web service, but the free service sleeps after inactivity and may take time to wake; its local files are not durable. Its paid tier avoids the free-service sleep limitation.
- [Cloudflare Workers](https://developers.cloudflare.com/workers/platform/pricing/) with [Workers KV](https://developers.cloudflare.com/kv/platform/pricing/) is a separate free-tier serverless option, but the Python receiver and storage integration may need an adapter or port. KV is eventually consistent; verify that this is acceptable for retrieval. [R2](https://developers.cloudflare.com/r2/pricing/) is an alternative object store with a separate free allowance and subscription checkout. The free Worker has a limited per-request CPU budget. [Workers AI](https://developers.cloudflare.com/workers-ai/platform/pricing/) provides a separate, metered free allowance for possible future transcription.

Confirm current pricing, account/payment requirements, audio retention, data location, TLS compatibility and costs before selecting or deploying a provider. AI and storage allowances are separate from the endpoint allowance.

## Phases and acceptance gates

### 1. Verify device and recovery

- Confirm the model, firmware, jailbreak state, installed package tools, update-blocking state and existing Bluetooth operation. SpiderCat's modern setup normally includes KPM; verify the actual device state.
- With user agreement, set up and test a suitable independent USB SSH method, such as Jobman where applicable, and review a device-specific rollback procedure. Jobman's optional recovery mode is experimental and no recovery method guarantees protection against every failure.
- Inspect the Bluetooth transport, controller services, available runtime and dependencies before attempting controller handover. Avoid boot-time or persistent system changes during prototyping.

**Gate:** the user has reviewed the risks and rollback, USB access has been tested, and physical-device experiments have explicit permission.

### 2. Build and test the portable local receiver

- Define the upload contract, configurable endpoint, authentication, request limits, acknowledgements, errors and retention policy. Use the same contract for local and future hosted deployments.
- Run the receiver on the Windows laptop and send a known sample locally. Verify receipt, playback, deletion and rejection of invalid credentials.
- With user agreement, expose it only to the trusted local network and verify the Kindle can reach it over Wi-Fi. Test TLS certificate verification when HTTPS is enabled; do not disable verification to make it work.
- Document dependencies, configuration and a container deployment option. Do not create a cloud account, add payment details or deploy remotely without permission.

**Gate:** a known recording reaches the laptop receiver, can be played and is handled according to the retention policy; the Kindle can reach the receiver over the local network.

### 3. Prove microphone capture on the PW4

- Study the PW4 controller-handover approach in `kindle-hid-passthrough` and the HFP capture approach in `kindle8-offline-home`; neither proves the complete PW4 audio path.
- With recovery available and explicit permission, manually run a minimal capture experiment. Connect the headset and capture a bounded sample. Copy the sample for listening by the safest available method.
- Stop the experiment and verify normal Bluetooth and reading. Record the actual format and controller behavior, not pairing keys or private audio.

**Gate:** the sample contains intelligible speech, and normal Bluetooth can be restored. An HFP control connection or non-silent packets alone is not proof of usable capture.

### 4. Send a recording from the Kindle

- Connect capture to the laptop receiver. Stop capture and the voice link before sending the bounded sample.
- Test Wi-Fi while the custom service owns the controller; if necessary, investigate restoring native Bluetooth before upload. Closing the voice link alone does not guarantee radio coexistence.
- Handle network loss, timeout and authentication errors visibly. Do not silently discard a recording or retry indefinitely; apply the agreed bounded retention policy.

**Gate:** several complete, intelligible headset recordings captured on the Kindle reach the receiver on the awake laptop.

### 5. Add reading controls

- Add a minimal KOReader plugin for **start**, **stop/send** and **cancel**, with clear status and error feedback. Keep Bluetooth and network work off the UI thread.
- If the user's books or reading workflow are incompatible with KOReader, evaluate a separate native Kindle interface; seamless stock-reader integration is not assumed.

**Gate:** the complete flow can be operated from the chosen Kindle interface without SSH for each prompt.

### 6. Reliability and cleanup

- Test at least 10 consecutive short recordings, including one at the duration limit; listen to the received files and record actual latency and failures.
- Test a missing headset, lost network, rejected credentials, cancelled recording, interrupted service and restoration of stock Bluetooth.
- Verify that recording is not continuous, temporary audio follows the retention policy, secrets and samples do not enter logs, and normal reading returns after exit.
- Document startup, shutdown, service configuration, costs, known limitations and rollback. Do not enable automatic startup without explicit approval.

**Gate:** repeatable end-to-end capture and local delivery, followed by a clean return to normal operation; report any unverified conditions rather than assuming success.

### Later: move the receiver to a hosted server

Select a suitable provider with the user and deploy the same application where supported. Configure a public HTTPS endpoint with certificate validation, rotate credentials, provision persistent storage and update the Kindle's server address. Test authentication, TLS, upload, playback, deletion and failure handling against the actual deployment. Serverless platforms with different runtimes may need an adapter; test the chosen deployment rather than assuming compatibility.

## Main risks

| Risk | Approach |
|---|---|
| PW4 HFP/SCO capture is not supported or differs from KT3 | Test the actual transport, audio routing, runtime and headset before committing to the design. |
| Controller handover disrupts native Bluetooth or boot | Test USB access and review rollback first; launch manually; avoid early-boot changes. Recovery is not guaranteed. |
| Wi-Fi and Bluetooth do not coexist | Test capture and completed-recording upload early; evaluate restoring native Bluetooth before upload if needed. |
| Laptop sleeps or is unreachable | Keep it awake and the receiver running for the local phase; display connection errors. A hosted deployment is deferred. |
| Future remote service sleeps, changes limits or incurs charges | Compare current provider terms, set spending safeguards where available and monitor usage. |
| Audio or credentials leak | Authenticate, use verified TLS for remote access, keep secrets out of logs and public storage, and set explicit sample retention and deletion rules. Plain HTTP is limited to an approved trusted-LAN test and exposes the audio and token on that network. |
| The chosen UI cannot open the user's books | Verify the reading workflow; keep capture independent and evaluate a native interface if KOReader is unsuitable. |

## References

- [SpiderCat](https://github.com/KindleModding/SpiderCat) and [KindleModding: what's next](https://kindlemodding.org/jailbreaking/whats-next/)
- [KindleModding: Jobman](https://kindlemodding.org/jailbreaking/whats-next/jailbreaking-manager/)
- [KOReader](https://github.com/koreader/koreader)
- [kindle-hid-passthrough](https://github.com/zampierilucas/kindle-hid-passthrough) — controller handover reference; HID, not microphone capture
- [kindle8-offline-home](https://github.com/yzgpk2002/kindle8-offline-home) — HFP capture reference tested on a different model
- [Google Bumble](https://github.com/google/bumble) — userspace Bluetooth stack
- Existing research: `../kindle-bt-mic-streaming-research.md`; its older streaming and provider assumptions are not the current plan.
