# Agent implementation plan

> Historical coordinator brief for the first milestone. See [README.md](README.md)
> for the current state.

## Coordinator brief

Implement the first milestone defined in [`PLAN.md`](PLAN.md): capture a short Bluetooth-headset microphone recording **on the Kindle PW4** and send it over Wi-Fi to a receiver running on the Windows laptop. The initial control interface is a KOReader plugin, with capture independent of the reader. The laptop must be awake and running the receiver for this phase. Design the receiver and upload contract so the application can later be deployed remotely without requiring the laptop during normal use.

**This is a handoff document, not an implementation.** The existing source, test, configuration and supplementary documentation files in the scaffold are intentionally empty placeholders; empty JSON and TOML files are not usable configurations. Remote provider selection and all hardware-dependent details remain open.

### Non-negotiable boundaries

- Target: **PW4**, last reported firmware **5.18.1.1.1**, a generic Bluetooth headset and a receiver on the Windows laptop. Verify the actual firmware and device state before testing. The user reports completing the **SpiderCat** jailbreak; do not repeat it or assume KPM, update blocking, SSH or KOReader configuration has been verified.
- First version: manually start/stop capture, approximately **15 seconds maximum**, and upload the completed recording after capture and closure of the Bluetooth voice link. Live streaming, speech recognition, LLMs, TTS and book context are later work.
- This is the user's only Kindle. No boot-time service, udev/upstart changes, persistent system modifications, factory reset or firmware changes during prototyping without explicit approval. Launch experiments manually.
- Do not take over the controller until the user confirms USB SSH has been tested and a device-specific rollback procedure has been reviewed. Recovery methods can fail; do not promise an unbrick path. Do not connect to, modify or test the physical device without explicit permission.
- Do not create provider accounts, submit payment details, deploy a public service, or use credentials without the user's agreement. The laptop receiver is the initial target; hosted deployment is later work.
- No always-on listening. Keep samples bounded and preferably in memory; do not log audio, pairing keys, authorization headers or credentials. Agree on failed-upload retention, playback access and deletion before implementing storage.
- Authenticate uploads and restrict payload sizes and timeouts. Prefer verified HTTPS on the LAN; authenticated HTTP is permitted only for an explicitly approved trusted-LAN test, where audio and tokens are visible on the network. Never expose it to the public internet; rotate any token used over plain HTTP before remote use. Require authenticated HTTPS with certificate validation for every remote test.

## Scaffold and ownership

The existing directories and files indicate intended responsibilities, not mandatory APIs or a completed implementation. The coordinator may adjust them after documenting interface changes.

```text
kindle-voice-bridge/
├── PLAN.md                        # approved goals and safety gates
├── agent-plan.md                  # this handoff
├── README.md                      # setup and operation; initially blank
├── .gitignore, pyproject.toml     # initially blank
├── docs/                          # protocol, recovery, hardware evidence
├── kindle/voice_bridge/           # capture, lifecycle, control and uploader
├── koreader/voicebridge.koplugin/ # initial reader controls
├── server/voice_bridge_server/    # portable receiver, initially on Windows
└── tests/                         # unit, UI and integration tests
```

Potential work streams are: coordinator/integration; portable local receiver; Kindle capture; KOReader UI; safety and verification. Assign one writer per shared file. Review third-party licences before copying code; the KT3 reference does not offer a blanket licence for its entire repository.

## Work order and dependencies

1. **Establish portable foundations.** Populate `.gitignore` before tests create recordings, caches, local configuration or secrets. Plan a provider-independent Python web app, with explicit dependencies, configurable bind address/port, secret and storage, and an optional container deployment. Do not tie core application logic to a provider-specific runtime or database. Document how the Kindle runtime will be validated; do not assume the KT3 runtime is compatible.
2. **Confirm the setup.** When the user is ready, confirm model, current firmware, jailbreak tools and update-blocking state. No account creation or remote deployment without permission.
3. **Agree interfaces.** Write `docs/protocol.md` before dependent components are built. Include the same authenticated upload contract for local and future remote use, content type and metadata, size limits, timeouts, acknowledgements, failure and retention behavior. Define the reader-to-service start, stop, cancel and status contract. Mark hardware-dependent formats provisional.
4. **Develop without hardware.** Build the portable receiver and tests, a mockable Kindle service, and a minimal UI against fakes. Verify a generated sample and security behavior without claiming this proves device or headset compatibility.
5. **Review recovery with the user.** With explicit agreement, verify USB SSH and existing Bluetooth behavior, record the actual controller/runtime details and review rollback. Do not treat simulated tests as satisfying this gate.
6. **Prove PW4 capture.** With explicit permission, manually capture a bounded sample, transfer it for listening and restore native Bluetooth. An HFP control connection or non-silent packets is not sufficient. Determine whether the board/controller routes usable SCO audio to the host and document the actual format.
7. **Test laptop delivery.** Upload after capture and voice-link closure. Check whether Wi-Fi functions while the custom service owns the controller; if not, investigate restoring native Bluetooth before upload. Update the protocol if hardware results differ from initial assumptions.
8. **Integrate UI and run acceptance tests.** Test the chosen reading workflow, repeatability, error handling, cleanup and restoration. Document results and limitations. Do not enable automatic startup as part of this milestone.
9. **Later: select and deploy remotely.** With permission, compare current provider terms and deploy the same application to a compatible service or VM. Configure verified HTTPS, persistent storage and a new secret; test the actual deployment. Provider-specific serverless runtimes may require an adapter.

Hardware failure or unsupported runtime blocks claims of completion; it is not permission to escalate system changes. The laptop receiver satisfies the first deployment target; hosted deployment is a separate future gate.

## Interface guidance

### Session and capture

- Represent a single session with explicit states such as idle, connecting, recording, stopping, uploading, complete, error and restoring. Define valid transitions and behavior for repeated commands, disconnects, timeout and cancellation.
- `start` is user initiated. `stop` ends capture and closes the voice link before upload. `cancel` stops capture and clears or handles the sample according to the approved retention policy. Exiting the service attempts restoration even after an error.
- Encapsulate controller ownership, headset connection and capture behind mockable boundaries. Record the original state needed for restoration; never claim restoration succeeded without checking it.
- Keep the capture callback fast: no network operations or blocking disk writes in the Bluetooth audio path. Enforce time and memory limits and report overflow or packet loss.
- Bumble is a candidate userspace stack, not evidence of PW4 compatibility. Confirm transport, controller routing, HFP role, codec and host-side representation on the actual device. Provide any downlink packets required for a stable voice session only after validating the chosen implementation.

### Audio and upload

- Prefer a simple bounded recording in a documented format. WAV with mono PCM is a candidate only if captured data can be correctly represented or decoded. Do not label encoded bytes as PCM. The common 8 kHz/16-bit mono case is about 240 kB for 15 seconds, excluding the header; actual format is unverified.
- Use a configurable endpoint and bounded authenticated request; bind the receiver to loopback by default, enabling the LAN interface only by explicit configuration. Specify authentication, headers, maximum size, timeouts, response schema and errors in `docs/protocol.md`. The same upload contract must support local and remote deployment. Prefer verified HTTPS locally; an approved trusted-LAN-only HTTP test exposes both audio and token on that network and must not be exposed publicly. Remote access requires verified HTTPS and a rotated credential. Ensure the Kindle has the needed TLS runtime, trust store and correct time; do not disable certificate verification to work around incompatibility.
- Validate audio length and structure on the receiver. Acknowledgement proves receipt, not intelligibility. Avoid indefinite retries and accidental duplication. Account for remote service cold starts, quotas and transient failures.
- Specify sample access, configurable storage location, retention, deletion and failed-upload behavior. Use a replaceable storage boundary; local files are suitable initially, but do not assume ephemeral hosted storage persists. Do not save test audio by default without an agreed policy. Avoid logging audio, tokens, pairing keys and personal book content.

### Kindle interface

- Initially target a small KOReader plugin with start, stop/send, cancel and status operations. Keep it independent of the capture implementation; a loopback-only local control channel is a candidate if supported and appropriately protected.
- Keep UI work responsive and ensure repeated taps cannot start overlapping recordings. Display recording, sending, completion and actionable error states.
- KOReader cannot open DRM-protected or KFX books. Verify the user's workflow before making it mandatory. A standalone or native Kindle interface is an alternative, but seamless stock-reader integration has not been established.

## Test strategy

**Testing preference (user-approved):** favor focused unit tests and simulated or in-process integration tests. Keep automated end-to-end tests to a minimum. The user prefers to perform end-to-end and hardware acceptance testing personally; any device access requires their specific permission. This preference is not a reason to delete existing tests.

**Unit tests:** use fakes for Bluetooth, timing, capture, upload and restoration. Cover state transitions, duration and memory limits, cancellation, headset disconnect, malformed audio, receiver size limits, timeouts, invalid credentials, TLS failures, upload failure and restoration after exceptions. Verify logs do not contain audio or secrets.

**Integration tests:** generate a sample in memory and exercise the upload/receiver boundary. Verify acknowledgement and content, unauthorized rejection, error handling and retention/deletion. Test the session flow using fake capture. A local integration test does not prove hosted deployment or hardware compatibility.

**Local acceptance:** run the receiver on Windows, verify known-sample upload, playback, deletion and unauthorized rejection, then confirm that the Kindle can reach it over the approved local network.

**Future hosted acceptance:** with permission, deploy to a compatible hosted service and securely send a known sample. Verify retrieval or playback and deletion, without a home computer serving requests. Record the provider, plan, expected charges, limits and any cold-start behavior.

**Hardware acceptance:** with user permission and recovery confirmed, document firmware, headset, software versions and actual format. Listen to a capture transferred by the safest available method, then to recordings received by the laptop. Test Wi-Fi coexistence, the duration limit, cancellation, missing headset, network loss, rejected credentials, service exit and native Bluetooth restoration. Finish with at least **10 consecutive successful short recordings**, reporting actual latency and failures. Never record pairing keys, serial numbers, private book content or audio in the report.

## Completion and handoff

Provide the user with local setup/start/stop instructions, desktop and LAN test results, a separate table of actual hardware tests, the rollback procedure and its limitations, retention settings, dependency/licence notes, known issues, and documented steps for a future hosted deployment. Report hosted tests and provider costs only if that separate deployment is undertaken. If hardware access was unavailable or a gate failed, state this and identify the next action; do not mark the milestone complete.

## Research references

- [`PLAN.md`](PLAN.md) and [`../kindle-bt-mic-streaming-research.md`](../kindle-bt-mic-streaming-research.md) — the latter includes older streaming and deployment assumptions that are not the current plan
- [SpiderCat](https://github.com/KindleModding/SpiderCat), [KindleModding: what's next](https://kindlemodding.org/jailbreaking/whats-next/) and [Jobman](https://kindlemodding.org/jailbreaking/whats-next/jailbreaking-manager/)
- [kindle-hid-passthrough](https://github.com/zampierilucas/kindle-hid-passthrough) — controller takeover reference, not audio capture
- [kindle8-offline-home](https://github.com/yzgpk2002/kindle8-offline-home) — model-specific HFP reference and its licence/dependency notes
- [Google Bumble](https://github.com/google/bumble) and [KOReader](https://github.com/koreader/koreader)
