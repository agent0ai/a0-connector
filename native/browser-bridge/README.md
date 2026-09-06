# Agent Zero Browser Bridge

This subtree is the foundation for the standalone
`a0-browser-bridge` host companion. It owns three deliberately separate surfaces:

- Chromium native-host invocation and native-endian framed stdin/stdout;
- redacted CLI `status`, `doctor`, and `self-test` output;
- a Unix install/update transaction engine requiring an opaque verified
  candidate, with concrete macOS acquisition and CLI installation composition.

**macOS release status:** the corrected companion is published under protected
immutable tag `native-v2.12.0-macos-r2`, with Developer ID signing, notarization
and independent CLI final-byte pins. Actual native Chrome installation and
independent CLI installed-state verification passed on the signing host.
The failed r1 distribution remains immutable and is not a fallback. Live Chrome
pairing/control, Chrome Web Store approval and other platform acceptance remain
separate gates.

The reviewed release policy contains genuine publisher/builder public roots,
immutable catalog locations and the reviewed production extension origin.
Detached Ed25519 catalog verification, pinned-catalog
acquisition, and exact bounded artifact download/hashing are implemented, but
they do not prove platform signing, provenance, or offline self-test. The
production verified-candidate composer now includes a concrete Darwin signature,
notarization, embedded identity and signed local-build provenance path. Genuine
publisher/builder roots and published macOS release assets are provisioned.
Native macOS install/update acquires the compiled immutable release,
verifies it and invokes the transaction; metadata alone never enables installation.
The separate Python CLI fresh-host bootstrap independently pins final bytes;
it now selects the corrected r2 artifact.
Key identifiers alone cannot enable readiness; no fixture identity or signing
root is included in production policy.

Signed release-catalog v1 requires all nine delivery artifacts; v2 permits
complete declared platform groups. The current signed delivery is macOS-only.
Linux delivery and platform acceptance remain pending. Windows still needs PE/
Authenticode candidate verification, the private HKCU installation transaction,
delivery and platform acceptance; its private staging primitives alone do not
make Windows installation available.

The [payload archive profile](RELEASE-PAYLOAD-v1.md) preserves the signed
archive digest while deriving a separate executable digest through bounded
single-file extraction into private staging. That proof retains exact file
handles and feeds the platform/provenance/self-test install-candidate composition. GitHub Actions is
not required: separately pinned local-builder signatures are supported, without
treating an unsigned local build receipt as provenance.

Native trust contains signer roots and immutable version URLs, never its own
final executable or catalog digest. A signed detached derivation receipt binds
those final bytes after build. Installation retains `release-catalog.json`,
`release-catalog.sig`, `build-provenance.json`, and `build-provenance.sig` beside
the immutable executable; read-only status verifies both signatures and the
measured installed bytes, not an unsigned state flag. A separately built CLI
can additionally pin final companion/catalog hashes without circularity.

The browser registry is compiled into Rust in `src/registry.rs` and mirrored in
`browser-registry-v1.json` for review and release tooling. Its paths are per-user
canonical locations only; arbitrary browser profile paths are never accepted.
Automatic discovery on macOS/Linux checks only conventional compiled stable
executable locations without executing or scanning profiles; custom installs
can use explicit browser targeting. Updates reuse previously owned targets only
after full binary, manifest, and install-state readback. Windows installation
is still unavailable; private Windows staging is implemented but unverified on
Windows.

## Commands

```text
a0-browser-bridge status [--json]
a0-browser-bridge doctor [--json]
a0-browser-bridge self-test [--json]
a0-browser-bridge metadata --json
a0-browser-bridge install [--browser NAME ...] [--json]
a0-browser-bridge update [--json]
```

`install` and `update` reject missing or invalid release evidence before mutation.
The corrected r2 macOS install returned `INSTALL_VERIFIED` with one registered
Chrome target, followed by independent CLI verification. Do not generalize that
host result to other platforms. Native-host caller origins are validated before
stdin is read; unapproved origins receive a bounded stderr reason and no stdout
bytes. A
clean native port close exits `0`; malformed framing (empty, truncated, or over
the 768 KiB bound) exits `5`.

`src/json.rs`, `src/rpc.rs`, and `src/session.rs` provide the bounded,
fixture-testable relay foundation. It accepts one JSON-RPC 2.0 object per native
frame, enforces directional request/notification allowlists and method parameter
bounds, correlates full-duplex opaque string IDs, and uses injected monotonic
timestamps for the 10-second hello and maximum 120-second request deadlines.
Live correlations are capped at 512 and completed-ID tombstones at 2,048.
Correlation IDs use the extension's bounded ASCII symbolic grammar. Pairing
exchange parameters are exact-key objects, and their server base accepts HTTPS
or loopback HTTP only with canonical safe path segments and no URL credentials,
query, fragment, percent encoding, backslash, double slash, or dot segment.

The host-owned pairing backend generates an Ed25519 key from operating-system
entropy, sends only its public key and the one-time code to the exact Core
exchange route, validates the complete success response, and commits the
private seed only after success. Credentials live in macOS Keychain, Windows
Credential Manager, or Linux Secret Service. There is no silent plaintext-file
fallback; an unavailable native credential store fails closed. The blocking
rustls client uses platform certificate verification, disables redirects and
ambient proxy routing, caps the response at 16 KiB, and allows plaintext HTTP
only for the already-validated loopback origins accepted by the frozen RPC
contract.

A valid hello can negotiate an `unpaired` pairing-only session:
`bridge.ping`, `pairing.status`, `pairing.exchange`, and
`pairing.disconnect` remain available while privileged relay calls return
`NOT_PAIRED`. Pairing success is deliberately not browser-control readiness.
For an already paired profile, the native hello is deferred until the exact
Core admission is verified or its bounded deadline expires. A separate worker
loads the extension-and-install-bound paired credential, requests a fresh Core
challenge, signs the fixed trust-v1 proof,
and establishes a WebSocket-only Engine.IO/Socket.IO session on `/ws`. A
successful pairing exchange also starts this worker. The socket uses platform
TLS verification, no redirects, no proxy discovery, no cookies or API keys,
and preserves reverse-proxy base paths. It sends only the restricted browser
hello and maintains heartbeats. Browser-only events pass a bounded codec and
the relay activation gate; unrelated and unsupported traffic is rejected.
The seed is zeroized before socket establishment; proof material is transient.

Production admission remains closed. The implemented handshake accepts only
the full typed activation, exact namespace SID/generation/profile binding,
secure companion floor, and independently negotiated capability set. A partial
or runtime-pending Core response never enables browser control or late
promotion. `pairing.status` reports a bounded
connecting, authenticated/runtime-pending, or failed diagnostic. Connection
failure does not delete the paired key or retry automatically. Reopening the
native port creates a fresh attempt. Disconnect/EOF cancels the worker without
blocking the native reader; local disconnect still reports that Core revocation
is required. OS credential/DNS calls may finish asynchronously after cancellation,
but subsequent network phases check cancellation before sending.

The browser operation/control transport is implemented behind that activation
gate: eight-packet Core queues, a one-frame native input queue, independent
deadline polling, exact caller-credential bridge/load-generation binding, and
separate Core/native correlations. `connector_browser_control` carries an
explicit `method` naming the native browser control. Both operation and control
results echo the original request identity; unverified artifacts and remote
error details are not forwarded. This operation-only codec requires scoped
IDs even for `status`/`ensure`; connection-level status remains a separate seam.
Forwarded mutations that time out report `OUTCOME_UNKNOWN`. Late replies do not
terminate unrelated work. The local WebSocket smoke covers a correlated control
round-trip as well as signing, hello, heartbeat, and cancellation; it is not
evidence of production Chrome control.

Core transport identity is an immutable compile-selected profile, never a
protocol, environment, CLI, or user-supplied string. Operation/control,
critical-event acknowledgements, artifact acknowledgements, and queued worker
commands retain that exact profile and reject the other profile's handler. This
profile seam now admits only the separately signed, selected limited-development
route: its command queue drains operation, control, and critical-event traffic
for the frozen eight-action/four-feature subset. It does not create production
activation. Context and artifact transport remain explicitly unavailable to the
development profile.

The bounded context transport codec maps list/subscribe/unsubscribe/text-message
requests to separately correlated Socket.IO acknowledgements and projected
snapshot/event/completion notifications. Only contexts advertised on that
connection may be targeted; unknown fields, raw attachments/candidates, and
tool metadata are rejected. Pagination/completion are preserved and timed-out
acknowledgements are discarded. This codec is behind the same inactive production
relay gate. Core's existing-context message delivery and stream composition are
implemented; context creation and production side-panel activation are not.

Semantic click and empty-field TYPE are negotiated only through their exact
Core/extension approval codecs. Consequential requests bind the operation,
current document/ref, origin, target fingerprint, and data classification to
one explicit decision. TYPE recomputes the exact UTF-8 digest and never retains
raw text in events, receipts, or result correlations. Private artifact output
is independently spooled/verified in native and Core and is usable only after
matching acknowledgements and operation settlement; Windows storage still
requires its private-DACL adapter. Focused synthetic and disposable Chrome
checks are not a live paired installation acceptance result.

Production pairing and browser readiness retain their distinct release,
credential and authenticated Core admission gates. No test flag bypasses them.

## Local-development profile

For source development only, build a distinct binary with:

```bash
cargo build --locked --release --features local-development
```

That binary exposes `development install|update|status|uninstall --json` (with
`--yes` required for mutations) and installs only its own retained executable
bytes under a separate per-user Unix root. It registers native host
`io.agentzero.browser_bridge.dev` only for extension
`paoagmddepkmonpeboobaijlenlcokpc`; it never adopts a production manifest,
credential, or install state. Fresh manifests are create-only, status verifies
the complete state/binary/manifest binding, and update transactionally switches
exact-owned manifests/state to a new immutable source binary while preserving
installation identity, browser targets, and profile credentials. Windows is
unavailable.

The development build may perform the separately namespaced pairing exchange
against a canonical explicit-port HTTP loopback Core URL. Its inactive native
hello, status, exchange, and disconnect responses carry the development trust
marker, both readiness booleans false, and
`development_runtime_not_available`. For an already paired profile, it starts
only the separately domain-bound development challenge and signed Socket.IO
worker. An unselected Core returns the old exact pairing-only acknowledgement;
the port stays non-operational and cannot promote later. An explicitly selected
Core may instead return the exact identity/SID-bound
`a0.browser-bridge.development-runtime.v1` admission. That route drains only
`open/list/state/navigate/content/scroll/status/ensure`, cancellation,
finalization, reconciliation, navigation-site resolution, and critical events
with acknowledgements. One exact provisional reconciliation control may be
buffered until the hello ACK is verified. That early control uses Core's exact
five-field restricted sender envelope, and the hello result must echo the
outer correlation; unknown envelope or result fields remain invalid. Context, artifact/screenshot,
click/type/action-approval, and production activation remain unavailable; Core
loss closes the native port. Read-only pairing status stays local without
refreshing admission, while disconnect withdraws the route before deleting the
profile credential, returns its result, and closes the port. Either handshake
falls back to the inactive native response by eight seconds from the original
port start. This is not
signed-release evidence, fixture trust, or production runtime admission.

Protocol constants and fixture envelopes are reviewable in
`schemas/native-rpc-v1.schema.json` and `tests/fixtures/native-rpc-v1/`.

## Validation

```bash
PYTHONDONTWRITEBYTECODE=1 python3 tests/validate_structure.py
```

When the pinned toolchain is already available:

```bash
cargo fmt --check
cargo test --locked
```

For connector-only changes, the focused smoke check is:

```bash
cargo test --locked core_connector::tests
```

It covers real local WebSocket framing/authentication/heartbeat/cancellation,
proof signature/binding/expiry, and rejection of unsupported browser traffic.
It does not contact a running Agent Zero or the operating-system credential store.
