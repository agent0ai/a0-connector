# Browser Bridge Companion DOX Rail

## Purpose

- Own the standalone `a0-browser-bridge` Rust companion and its per-user install planning contract.
- Keep Chromium native-messaging framing isolated from ordinary CLI output.
- Preserve fail-closed release activation until reviewed production trust roots and extension origins are compiled in.

## Scope and ownership

- This document owns `native/browser-bridge/**`, including Rust sources, browser registry data, JSON schemas, and native-subtree tests.
- The binary is host software. It must never be copied into, installed by, or treated as part of an Agent Zero Docker filesystem.
- A0 CLI integration and release automation live outside this subtree and require their own DOX review.

## Security contracts

- Companion 2.12.3 pins the new immutable `native-v2.12.3-macos` release
  locations for bounded outbound result/event backpressure and clean-EOF
  cancellation in addition to the earlier inbound queue correction. The secure
  compatibility floor remains 2.12.0. New final-byte signing, notarization,
  publication and installation evidence are required; never overwrite r2 bytes.

- The corrected macOS companion is signed/notarized and published under the
  protected r2 tag with independently pinned CLI bootstrap bytes. Actual native
  Chrome installation and independent CLI installed-state readback passed on
  the signing host. Preserve the earlier failing r1 distribution unchanged;
  it is not a fallback. Live Chrome pairing/control, CWS approval and other
  platform delivery/acceptance remain separate gates.
- Native-host stdout is reserved exclusively for native-endian 32-bit length-prefixed frames. Diagnostics go to stderr or redacted CLI JSON.
- Reject untrusted native caller origins before reading stdin. Origins must be exact, non-wildcard `chrome-extension://<32 lowercase a-p characters>/` values compiled from reviewed release policy.
- Native inbound and outbound frames are capped at 768 KiB. Non-artifact JSON
  payloads are capped at 512 KiB, raw artifact chunks at 192 KiB, complete
  artifacts at 25 MiB, event metadata at 64 KiB, and diagnostics at 2,048
  bytes. Apply the frame bound before allocation.
- Native traffic is single-object JSON-RPC 2.0. Reject batches, duplicate JSON
  keys, numeric/null IDs, wrong-direction methods, request/notification kind
  mismatches, unknown methods/actions/enums, and malformed method parameters.
- Correlate full-duplex requests by expected response source plus opaque string
  ID. IDs use `[A-Za-z0-9][A-Za-z0-9._:-]*` and are at most 256 bytes. Bound
  live requests and completed-ID tombstones, cap request deadlines at 120
  seconds, and require the first `bridge.hello` within 10 seconds.
- `pairing.exchange` accepts exactly `contract_version`, `pairing_code`, and
  `server_base_origin`. Its server base is HTTPS or loopback HTTP only; reject
  URL credentials, query/fragment data, percent/backslash encodings, malformed
  hosts/ports, double slashes, dot segments, and unsafe path characters.
- Pairing key generation and credential persistence are host-owned. Generate
  Ed25519 keys from operating-system entropy, post only the public key, require
  an exact bounded Core success response, and commit the private seed only
  after that success. Store credentials only in macOS Keychain, Windows
  Credential Manager, or Linux Secret Service. A plaintext file fallback needs
  an explicit user acknowledgement contract and must never be silent.
- Credential lookup is scoped to the exact compiled caller extension ID and
  the validated `bridge.hello` install-instance ID. Store one versioned record
  per hashed profile slot and bind both identities inside the record; the
  per-user companion installation ID remains shared. A legacy extension-only
  singleton is a repair/re-pair signal only: never decode it into a profile
  credential, migrate it automatically, or let one profile delete another
  profile's key.
- Production `credential.rotate/status/revoke`
  takes only contract version from Chrome: rotation IDs and Ed25519 seeds are
  native-owned. Persist pending seeds in the profile/domain OS credential
  store before network effects, retain the active key until the fresh admitted
  candidate handshake confirms its generation, and clear expired candidates
  only on exact authoritative Core status. OS-released profile mutation locks
  serialize stages, commit, expiry and local deletion across native processes.
  A durable Core revoke event can settle only this port's explicit pending
  revoke before socket shutdown; unknown outcomes never imply revocation.
- Pairing HTTP disables redirects and ambient proxies, uses rustls with the
  platform certificate verifier, caps responses, and preserves the frozen
  HTTPS-or-loopback-HTTP boundary. An ambiguous transport or post-success local
  commit failure is an unknown outcome requiring server-side revocation before
  retry. Local disconnect must not claim that the server record was revoked.
- Pairing success does not authorize browser control. For a paired profile,
  retain the initial native hello request while the signed challenge/proof
  WebSocket handshake completes, bounded by the same ten-second hello
  deadline. Unpaired and repair-required profiles answer immediately so
  onboarding remains reachable. Emit runtime readiness only after exact full
  Core activation and connector-binding attestation; denial, malformed or
  partial readiness, disconnect, and expiry produce a paired-but-inactive
  response and never promote later on the same native connection.
- `core_connector.rs` owns the signed Core transport worker. Start
  it only for an exact paired hello; load only the caller-extension and
  install-instance-bound credential, validate the exact bounded challenge,
  sign fixed JCS bytes, and zeroize the seed before socket establishment.
  Use the pinned WebSocket client and platform TLS verifier with no redirects,
  ambient cookies/API keys, or proxy discovery. Preserve the configured base
  path and send normalized Origin/Referer headers.
- The worker accepts Engine.IO open/heartbeat and the `/ws` restricted
  namespace/hello acknowledgements. Build the runtime hello from the validated
  extension generation plus immutable credential/profile/native provenance;
  require the full exact Core-owned host projection, activation evidence, and
  connector binding, including the authenticated namespace SID. Core commands
  and results remain unavailable until that evidence atomically promotes the
  worker to Ready. After admission, bounded eight-packet queues
  carry browser commands/results to the owning relay thread. Cap packets at
  512 KiB, enforce handshake and heartbeat deadlines, and accept only bounded
  Engine.IO intervals up to 60 seconds and timeouts up to Core's 120-second
  ceiling. Reject binary/unsupported traffic and do not retry automatically.
  A full eight-packet Core-command queue applies bounded backpressure rather
  than treating a legitimate burst as malformed protocol. Retain only the one
  already-read packet, preserve FIFO order, and wait at most one second with
  five-millisecond cancellation checks, additionally capped by the current
  Engine.IO and pending renewal deadlines. No queue growth, operation replay,
  admission extension or blocking native EOF is allowed. Exhaustion still
  closes the worker. Worker failures emit only a fixed enum-derived stderr
  category; never upstream error text or packet/identity/credential data.
  The opposite-direction eight-packet result/event queue also retains just one
  pending packet with at most one second of FIFO backpressure. Every five
  milliseconds recheck worker status, cancellation and the worker-published
  current Engine.IO/pending-renewal deadline; readiness never comes from queue
  acceptance. Native input clean EOF independently cancels this owner-thread
  wait, and remains a successful port close. Exhaustion emits only fixed
  `CORE_RESULT_BACKPRESSURE_EXPIRED` and closes the session; it never resends an
  operation, grows a queue, or claims effects were not applied. Reconciliation
  results followed by recovered critical events share this same bounded FIFO.
  DNS/credential calls are worker-local; cancellation
  is checked between network phases and normal socket reads use a short timeout.
  Native EOF/disconnect must not wait for OS DNS or credential operations.
- Production Core admission is renewed every 15 seconds by an authenticated
  same-generation hello on reserved ACK ID 1 with a unique refresh correlation.
  The exact unchanged route must be acknowledged within eight seconds; stale,
  unsolicited, denied, changed-authority or timed-out ACKs terminate the worker.
  Context ACKs begin at 2. Engine.IO liveness does not renew runtime authority.
  Both inner and outer WsResult correlations must match; only the framework's
  bounded optional duration metadata is accepted. Development traffic is
  unchanged by this production-only renewal.
- A production worker may attempt the exact securely retained pending rotation
  signer on its next native hello. It commits that key only after full exact
  admitted Core hello confirms the expected new generation and profile; no
  retained active seed crosses socket establishment. A definite pre-Ready
  authentication rejection permits one fresh attempt with the still-stored
  active key for interrupted/expired rotation recovery. Network, malformed,
  timeout and post-Ready failures never trigger that fallback, and neither key
  is deleted based on a guessed outcome or local expiry.
- `transport_profile.rs` owns the two private compile-selected Core transport
  identities. Operation/control, critical-event acknowledgement, artifact and
  queued-command codecs must retain one exact profile and reject the other
  profile's handler. Protocol, environment, CLI and user strings never select
  or construct a profile; recognizing the development identity grants no
  admission, route, spool, readiness or queue-draining authority.
- `connector_codec.rs` maps only explicit browser operation/control events to
  native RPC. Require the codec's exact fixed handler, a
  caller-credential-bound bridge ID, current extension load generation, and all
  scoped request IDs.
  Controls carry an explicit native `method`; never infer it from field shape.
  Keep Core correlations separate from allocated native IDs, bound pending
  mappings/tombstones, echo request bindings, reject mismatched results, and
  redact error message/details. Strip Core-only bridge/load routing fields after
  validation but retain them privately for result correlation. Artifacts remain
  unsupported by this codec.
  Successful `browser.perform` replies require the canonical exact eight-field
  protocol §7.3 envelope, including `completed_at_ms` as a positive integral
  JavaScript-safe timestamp (at most 9007199254740991). Reject missing, malformed
  or extra fields; preserve exact operation/action correlation. The timestamp
  is diagnostic only, never freshness or authorization, and is not forwarded
  into the compatible Core operation-result projection. Seven-field synthetic
  success fixtures are not a supported alternate protocol.
- Navigation approval is an exact bound protocol seam. Accept
  `challenge.required` only as a critical notification with non-null
  operation/action IDs, canonical credential-free HTTP(S) origin, lowercase
  parameter/fingerprint/lease/browser SHA-256 values, nullable exact document
  ID plus safe epoch, printable-ASCII summary, the ordered deny/once/turn
  options, and a positive safe expiry. `browser.resolve_challenge` accepts only
  the matching navigation control fields and a null denial grant or an exact
  operation/turn origin grant. Retain the request challenge and decision
  privately and accept only an exact resolved result echo; arbitrary model text
  or local UI choice is never grant authority.
- Consequential click approval is a separate exact branch and must not widen
  the navigation shape. A click request has only a live tab handle and exact
  `{ref, expected_action_class}` arguments, carries no action preauthorization,
  and its result is bound back to the requested tab/ref with a canonical
  document epoch and conservative action class. An action challenge requires a
  non-null document ID, one of the three consequential classes, no classified
  data, and ordered decline/approve-once choices. Approval accepts only the
  exact operation-scoped action grant with matching origin, class, parameter
  hash, target fingerprint, classification, and expiry; decline carries no
  grant. The negotiated `click` subset now has extension, native, Core authority,
  and protected UI composition; it never implies typing, form submission,
  production release trust, or complete runtime activation.
- Semantic type authority is a negotiated branch after exact Core/extension
  and protected approval UI composition. A type request
  has only a live tab handle and exact `{ref, text, text_sha256,
  expected_action_class: sensitive_input}` arguments. Recompute the bare
  lowercase SHA-256 over the exact 1 through 32,768 UTF-8 bytes, reject NUL and
  carriage return without normalization, and retain raw text only in the
  transient native request forwarded to the extension. Type challenges and
  grants carry the exact tagged sensitive-text classification with that digest,
  a generic fixed summary, and full equality binding; no text/ref/length enters
  the event. Success echoes only the bound tab/ref/document epoch and exact
  `sensitive_input` class with empty receipts/artifacts. Never copy raw text or
  its digest into pending result correlation, results, diagnostics, or logs.
  These codecs do not advertise `type`, authorize submit/replacement/key input,
  or relax production release/runtime activation.
- `context_codec.rs` owns a separate bounded, connection-local production-only
  context codec. RelaySession must reject context requests, acknowledgements
  and notifications under the development transport profile even if a
  synthetic or future state accidentally marks that session Ready.
  List/subscribe/unsubscribe/text-message calls get distinct Socket.IO ACK IDs;
  require exact handler/correlation readback and preserve native IDs. Only
  advertised contexts may be requested, and only subscribed/pending-subscribe
  contexts receive allowlisted projected notifications. Preserve pagination and
  completion, reject tool metadata and unverified artifact/candidate input, and
  discard ACKs for expired requests. No context history is persisted. This
  codec does not activate the production relay or implement Core message execution.
- Context codec additionally maps production-only queue add/remove/send and
  explicit `browser.approval_decision`, retaining exact context/item/challenge
  request identities through their ACKs. Queue notifications are a separate
  bounded `context.queue_updated` projection with no synthetic log cursor:
  at most 32 owned IDs, 100-character previews, empty attachments and zero
  attachment counts. Queue mutations and approval failures are unknown outcomes,
  never retryable. Local decisions return only matching Core accepted receipts;
  they never constitute a browser grant. Cross-language shapes are frozen in
  `tests/fixtures/context-queue-approval-v1.json`.
- `artifact.rs` owns the transport-independent native artifact spool. Construct
  routes only from a validated `NativeInvocation` plus the current caller-bound
  server credential/connection projection, and require an injected live-route
  authorizer for every begin, append, completion, and consume. Bind direction
  and standard purpose to the exact origin/install/load/server/bridge/key/SID/
  context/session/turn/action/operation/artifact identity. Reserve declared
  count and bytes through consumption, cap artifacts at 25 MiB and ordered raw
  chunks at 192 KiB, and expose only a pathless descriptor after exact file
  length and SHA-256 verification. Spools live under a generated private child
  of a verified per-user directory and are deleted on consume, abort, expiry,
  disconnect, revocation, or shutdown. Windows uses artifact_windows.rs to
  atomically create protected current-owner/SYSTEM-only full-access DACLs and
  verify actual owner, protected DACL and both explicit ACEs through retained
  handles. Reject UNC/device/stream/normalization aliases and every reparse
  component; keep ancestor/root handles without write/delete sharing while
  private files live. Files use CREATE_NEW, non-reparse handles and read-only
  sharing; input handoff rechecks handle identity, link count, ACL, size and
  digest. Any unsupported filesystem/ACL/share behavior fails closed. Windows
  runtime tests are platform-only; macOS checks do not prove Windows acceptance.
  The primitive itself does not
  advertise readiness; the codec/operation consumer and independently admitted
  release route own its production composition.
- `artifact_codec.rs` owns output screenshot/download mapping and the separate
  production-only Core input-spool/ACK lane. Require every extension artifact phase to match an exact still-live
  `browser.perform` context/session/turn/action/operation binding retained by
  `connector_codec.rs`, with operation action equal to purpose. Add bridge/load
  only from that Core-issued pending operation, translate canonical padded
  base64 `data` to the Core `data_base64` field, and accept an ACK only when its
  credential bridge, generation, full binding, phase, progress or pathless
  descriptor exactly match. After full admission, construct the private spool
  from the exact authenticated server/key/SID route and current Ready worker.
  Verify each native phase locally before forwarding the same bytes to Core's
  independent verifier; an exact end ACK makes the local descriptor ready, but
  retain its reservation until the exact bound operation result consumes it or
  an abort, mismatch, expiry, disconnect, revocation, or shutdown cleans it.
  Never expose a host path to Core/UI/history or substitute descriptor-only transport. Bound live
  artifacts, declared bytes, frame requests, deadlines, and late-response
  tombstones. Input chunks require a Core-issued pending upload operation, exact
  bridge/load/context/session/turn/action/op/artifact binding, the same private
  Ready route, ordered canonical base64 and full native digest/length checking.
  The native relay returns only the exact pathless input ACK to Core. An upload
  browser.perform remains deferred until its exact input artifact is complete.
  Only extension artifact.input_path may consume one exact pending operation's
  verified private ephemeral path; that path is sealed inside an extension
  WeakMap token, used only by DOM.setFileInputFiles after one-use site consent,
  and never appears in JSON results, storage, logs or UI. Rehash and verify the
  retained file inode/length before that handoff; retain until operation cleanup.
  Spooling is not browser upload consent. Windows private spooling requires
  the explicit verified DACL adapter, and this composition does not relax the
  global production activation block.
- The native input reader is detached with a one-frame queue; the owner runs
  deadlines/Core polling even without extension input. A forwarded mutation or
  control expiring locally is `OUTCOME_UNKNOWN`, never `not_applied`. Late
  completed/expired replies are discarded without terminating unrelated work.
- Authenticated/runtime-pending diagnostics are not browser readiness. Keep
  relay state non-operational until the complete scoped runtime attestation
  exists. Companion runtime admission requires its own version to be at least
  the compiled `2.12.0` secure floor and exactly reflected by Core; this does
  not impose a same-version requirement on the independently compatible CLI.
  Never send browser commands or expose raw proofs, server errors, or keys in
  connection diagnostics. Test fixture credentials remain `cfg(test)` only.
- Stable manifest generation accepts only absolute executable paths and verified release origins; it never accepts an extension ID or executable path from a page, server, or native message.
- Registration paths come only from the compiled browser registry. Do not accept arbitrary browser profile paths.
- Stable install/update must not mutate when signed catalog, artifact digest, platform signature, or compiled trust evidence is unavailable or unverified.
- The provisioned production origin is the owner-supplied CWS draft identity
  `nhliclifilepdkoolioacpjpijomfplj`, whose supplied RSA SPKI SHA-256 is
  `d7b82b858b4f3aeeb8e02f9f89ec5fb97e3209a3d742f2d5d62248071df57459`.
  The verified Developer ID Application metadata is team `R2KNNFH5FC`, signing
  identifier `io.agentzero.browser_bridge`. `release.rs` and the public policy
  record these identities only: they do not assert CWS approval, notarization of
  a future rebuilt binary, signed catalog/provenance verification or runtime
  activation. Genuine independent `publisher-2026` and `builder-2026` public
  roots are provisioned, with exact recipe/toolchain and version URLs under
  the published `native-v2.12.0-macos-r2` public download tag under existing
  update/delete protection. The failed r1
  distribution remains immutable and is not an installation fallback.
  `RELEASE_SIGNATURE_VERIFIER_READY` denotes the invoked strict Ed25519
  verifier only. Missing/unverified downloads, platform proof, installed
  evidence or Core admission still fail closed. Never use fixture signing keys.
  The separate development origin, install and credential namespaces are unchanged.
- Stable `status`/`doctor` use the transaction's read-only installed-state
  verifier instead of reporting every existing state as unknown. Require a
  configured publisher/builder roots and detached signed catalog plus local
  derivation receipt retained in the immutable release directory. Never embed
  final executable/catalog hashes in that same executable: this circular pin
  cannot be built. Compile signer identities and immutable version URLs before
  build, then authenticate final archive/executable/catalog digests from signed
  evidence. Independently distributed CLI/bootstrap packages may pin final
  companion bytes. Validate the non-lowerable floor, current target,
  private owned nonsymlink root/state/executable, existing read-only lock, no
  pending transaction, retained executable size/hash, every exact generated
  browser manifest and its recorded path/hash binding, and state readback.
  Report only `installed`/`INSTALL_VERIFIED` with a browser count on success.
  Missing state stays not installed; unavailable roots or failed verification
  stay blocked. This path creates nothing, executes no candidate, fetches no
  metadata and performs no recovery. Retained evidence files are private,
  nonsymlink, bounded and identity-checked. Unsigned state is never authority.
  Empty compiled roots remain intentional; no test signer enters production.
- `install_transaction.rs` owns the real stable Unix install/update transaction.
  Its child `install_lifecycle.rs` owns stable repair and explicit local retirement
  using that same retained signed installed-evidence verifier and lock. Repair
  accepts auto or the exact already-owned browser set; it never adds unrelated
  registrations. Recreate only absent exact generated manifests (including one
  missing native-messaging directory under a verified vendor parent), using
  private complete staging and create-only publication. Repair permissions via
  retained owned nonsymlink single-link descriptors only after signed executable
  digest verification; never rewrite executable bytes. Foreign manifests block
  all effects. Uninstall without `--yes` returns action-required 4 and changes
  nothing. `uninstall --yes` prepares a private retryable cleanup inventory and
  returns pending 6, retaining all registrations and credentials. Its child
  `credential_cleanup.rs` records only advisory account-slot metadata and the
  verified install-state digest. `credential_cleanup_macos.rs` searches only the
  same default user keychain and compiled production service as the credential
  backend, requests attributes but never secret data, disables authentication
  UI, and bounds results before copying strings. Reject duplicate/foreign slots,
  truncation and malformed attributes; unavailable providers (including Linux)
  are explicitly unavailable, never an empty verified enumeration. Development
  never reaches this provider. Even an empty inventory cannot prove server-side
  revocation or authorize deletion; only the existing exact per-profile admitted
  Core receipt path can delete that profile's credential under its mutation lock.
  No new receipt ingestion, CLI impersonation or unscoped key deletion exists.
  `--yes --force-local` separately retires only exact owned
  registrations and install state to a private recoverable backup, retain
  executable releases, logs and every credential, and return cleanup-pending 6.
  No server revocation or profile enumeration is implied. Interrupted retirement
  leaves an explicit private marker that blocks ordinary install/update recovery
  rather than automatically restoring browser access. Partial repair/retirement
  returns explicit recovery-required disposition, never unchanged/success.
  CLI machine output is the bounded pathless lifecycle.v1 contract shared by
  repair/uninstall. Fixture effect tests are not signed production acceptance.
  Its opaque `FullyVerifiedInstallCandidate` retains the exact open payload file
  handle and binds catalog/artifact, platform-signature, provenance, and offline
  self-test evidence to that handle identity and digest. Its crate-private
  `release_candidate.rs` composer consumes the exact verified catalog and
  derived payload, requires configured production roots and the current target,
  and invokes platform signature, provenance, embedded identity, then offline
  self-test in that order. Rehash/rewind both retained handles between phases;
  reject any changed bytes, metadata, version/platform/contract mismatch, or
  non-ready/oversized self-test result. The transaction retains the derived
  proof and staging cleanup owner throughout install. The historical candidate
  artifact SHA/size fields describe the installed executable, not its compressed
  archive; the retained derivation proof binds both independently. Verifier
  implementations are trusted crate-private code, never caller-supplied
  callbacks or public evidence flags. The concrete Linux self-test runner uses
  an inherited executable descriptor, a cleared environment, no shell or PATH
  lookup, null stdin/stderr, an eight-second deadline and 2,048-byte stdout cap.
  Only the reviewed network-free self-test may be executed, after prior gates.
  `release_linux.rs` implements the concrete Linux release policy: reverify
  the pinned publisher's detached catalog signature and exact archive binding,
  require independent signed builder provenance binding the retained executable,
  validate static ELF metadata, then invoke that exact-FD test. This is not an
  unconditional Linux platform-signature pass. Acquisition uses only one
  compiled version/platform/architecture provenance source and the verified
  catalog payload; normal stable CLI install/update enters this composer on
  Linux with a private XDG_RUNTIME_DIR staging parent. Missing roots, sources,
  desktop runtime directory or any failed proof remains unavailable.
  `release_elf.rs` parses bounded ELF64 little-endian x86-64/AArch64 headers and
  exactly one allocated non-writable `.a0_release` PROGBITS section mapped by
  one read-only PT_LOAD segment. The nine-field stable metadata must match
  version/platform/host/contracts. Pure fixtures run on macOS; actual Linux
  loader execution and installation remain platform acceptance checks.
  macOS has no public fd-exec equivalent. `release_macos.rs` implements the
  documented Darwin-only private staged-path exception: one create-only locked
  verification lease, current-user private parent chain, no symlink/hardlink,
  retained descriptor/size/hash rechecks before and after every exact bounded
  codesign, metadata and self-test command. Require the compiled
  Developer ID Application team/identifier, both architectures, hardened
  runtime and secure timestamps. Parse flags from exactly one `CodeDirectory`
  line per architecture; separate `Executable Segment flags` are not signature
  policy fields and cannot supply or negate hardened-runtime/ad-hoc evidence.
  Reject missing/duplicate CodeDirectory lines and preserve the secure-timestamp
  check, plus an explicit `notarized` codesign
  requirement with `--check-notarization` for the exact retained bare Mach-O.
  Pass inline requirements with the leading `=` as a separate argument.
  App-only `spctl --type execute` is not a valid bare command-line tool check;
  never accept its failure or localized output as a substitute for the explicit
  successful Apple ticket requirement. DMG/app wrappers have separate assessment.
  `release_metadata.rs` embeds a bounded `__TEXT,__a0_release` JSON section and
  exposes the same identity through read-only `metadata --json`;
  `release_macho.rs` validates both Intel/Apple Silicon executable slices and
  their exact matching version/host/platform/contracts without execution.
  `release_provenance.rs` verifies ASCII/JCS detached Ed25519 local-build
  statements against independently compiled builder roots, binding catalog,
  archive, executable, source repository/commit/tree, recipe and toolchain.
  GitHub Actions is optional, not a trust anchor replacement. These adapters
  are concrete and genuine publisher/builder public roots are provisioned;
  exact final Developer ID/notary evidence is still required. Native macOS install/update
  now calls compiled immutable catalog/provenance acquisition, bounded payload
  download/extraction, concrete verification, then the real transaction. No
  arbitrary URL or unsigned latest lookup is accepted. The installer publishes
  four authenticated sidecars atomically with the payload and requires exact
  sidecar readback on same-version reuse. This per-user design does not contain an
  actively compromised same-UID host. The engine derives user
  paths and manifest targets internally, uses a bounded OS advisory lock,
  private journals/backups, immutable version directories, exact ownership
  collision checks, atomic manifest/state replacement, readback, and
  conservative rollback/recovery. Test candidates and injected roots/failures
  remain `cfg(test)` only. Windows returns
  `WINDOWS_INSTALL_ADAPTER_UNAVAILABLE` until an explicit HKCU transaction with
  private Windows ACL handling lands. On macOS/Linux, `auto` checks only the
  compiled stable application bundles and conventional vendor launchers; it
  never executes a browser, searches `PATH` or profiles, or selects
  prerelease/Snap/Flatpak locations. An update may union its previously
  registered browser set only after exact state, active-binary, path binding,
  and every owned-manifest digest readback succeeds. Shared manifest targets
  are deduplicated, no discovered browser fails closed, and explicit browser
  selection remains available without executable discovery.
- `release::catalog` verifies bounded canonical ASCII/JCS stable catalogs with
  detached Ed25519 signatures against compiled public roots only, exact compiled
  extension origins, exact declared delivery coverage, compatibility and
  a non-lowerable security floor. Catalog v1 retains its exact ten root fields,
  no `platforms` key and all nine artifact entries. Backward-compatible v2 adds
  one required signed `platforms` list: nonempty known platform names, unique
  and strictly ASCII-lexically ordered (`linux`, `macos`, `windows`). Its matrix
  must equal the union of complete declared groups: macOS universal2 installer
  plus payload; Windows x86_64 and arm64 installer/payload pairs; Linux any
  bootstrap plus x86_64/aarch64 payloads. Reject incomplete groups, undeclared
  targets, duplicates, unknown values or fields and noncanonical lists. A v2
  Mac-only catalog cannot supply a Windows/Linux artifact or authorize those
  targets; their later publication and platform acceptance remain separate.
  `schemas/release-catalog-v2.schema.json` shares unchanged v1 artifact/coverage
  definitions and encodes the conditional complete-group requirements. This
  separates packaging availability only: no signature, origin, secure-floor,
  provenance, platform-verification or runtime-authority gate is relaxed.
  Artifact verification hashes an exact bounded
  stream and returns private-construction evidence without executing or
  extracting bytes. This evidence does not certify platform code signatures,
  offline self-test, provenance, or installation; those independent gates remain
  required. Fixture signing roots exist only inside Rust test code.
- Catalog acquisition selects only compiled release/version URLs and the
  compiled catalog SHA-256, then verifies its detached signature and exact
  release. It has no caller URL, environment, unsigned latest-release, or
  redirect fallback. Metadata uses platform TLS, no proxy/cookies/caller
  headers, bounded time and bytes, and exact optional content-length readback.
  Artifact downloading likewise accepts only the signed catalog URL and hashes
  the exact bounded stream into private caller-owned staging. Empty production
  catalog locations remain unavailable; this does not mint install authority.
- `release_payload.rs` and `RELEASE-PAYLOAD-v1.md` define the one-binary stable
  payload archive profile. Consume the exact catalog-proved `.tar.gz` handle,
  hash the same compressed bytes supplied to one-member gzip, accept exactly
  one root USTAR regular executable, and retain both archive and derived-file
  identities in generated private staging. Reject links, devices, traversal,
  extensions, extra entries, trailing members/data, and bounded-expansion
  violations. This pathless derived proof is not install authority: platform
  signature, provenance, embedded-version/contract, offline self-test, and
  production release policy still must bind the same executable handle through
  the crate-private candidate composer. Its handle accessor duplicates only the
  retained read-only file object, never reopens an executable pathname.
  `release_payload_windows.rs` owns Windows private payload staging and reuses
  `artifact_windows.rs` for protected current-user/SYSTEM-only DACL creation and
  readback. Reject reparse points, UNC/device/alternate-stream paths and hardlinks;
  retain ancestor guards and sealed read handles denying write/delete sharing.
  Rehash the sealed executable before minting derivation proof, retain downloaded
  archive cleanup ownership with that proof, and close owned handles before
  identity-checked cleanup. This staging adapter does not implement Authenticode,
  PE candidate verification or HKCU installation and cannot grant install
  authority. Shared parser/identity fixtures run on macOS; the Windows-only
  ACL/sealing test remains uncompiled and unexecuted without a Windows target.
- Signed catalog v1 covers all nine delivery artifacts; v2 may declare complete platform groups independently. Each included entry still requires its immutable HTTPS URL, exact size and digest; placeholder cross-platform records are not release evidence.
- Do not invent production extension IDs, catalog signing keys, signatures, or artifact digests. Empty production trust arrays and a disabled signature verifier are an intentional activation block. Activation requires reviewed pinned public-key bytes and a functioning signature verifier; key identifiers alone are never readiness evidence.
- A clean native-messaging EOF is a normal port close and exits successfully. Empty, truncated, oversized, or I/O-failed frames are integrity failures and exit with code `5`.
- Default diagnostics must not expose home paths, usernames, URLs, credentials, pairing material, raw manifests, or arbitrary environment values.
- No shell evaluation, dynamic plugin loading, background daemon/service installation, browser profile editing, or Docker-host mutation.
- Test authority that bypasses unpublished production trust may exist only
  behind Rust `cfg(test)`. Never add a runtime flag, environment variable, CLI
  option, or configuration file that activates fixture trust.
- `local-development` is a compile-time-only, non-production profile. It fixes
  the native host to `io.agentzero.browser_bridge.dev`, the extension origin to
  the reviewed development ID, and uses distinct install/state/keychain names.
  Its `development install|update|status|uninstall` CLI accepts only the executing
  binary as explicit source authority, performs no download or `PATH` search,
  and is Unix-only. Fresh registration is create-only; foreign regular files,
  dangling symlinks, unsafe locks, changed rollback targets, and unverifiable
  owned state fail closed without replacement. Development update requires an
  exact healthy owned install, preserves its install ID and registered browser
  set, stages the executing source at a new immutable content-addressed path,
  and retains the old binary. Its private canonical journal derives every
  target from exact old/new states; complete same-directory stages are
  published create-only after exact target removal, and recovery accepts only
  missing, exact old/new, partial journal-stage, or exact two-link publication
  states. A pending journal blocks install/uninstall and status reports that
  update recovery is required. Update never reads, migrates, deletes, or
  rewrites the separate profile credential namespace. This profile may exchange
  the separate Core development pairing record over canonical explicit-port
  loopback HTTP. A paired profile may start only the separate signed
  development challenge and hello worker: it binds the exact profile,
  generation and authenticated namespace SID, omits production activation, and
  falls back by eight seconds from the original native-session start. The old
  signed pairing-only ACK stays non-draining and may never promote later on the
  same port. An exact, selected `a0.browser-bridge.development-runtime.v1`
  admission may instead construct only the separate development route and
  drain operation, control, and critical-event traffic for the fixed
  `content/ensure/list/navigate/open/scroll/state/status` action subset and
  `cursor_v1/semantic_dom_v1/tab_groups_v1/tab_leases_v1` features. While the
  hello ACK is pending, retain at most one exact development-handler,
  identity-bound `browser.reconcile` control and release it only after that
  limited admission. Core's real restricted sender envelope is exactly
  `{handlerId,eventId,correlationId,ts,data}`; validate the bounded event ID,
  timestamp and restricted correlation as well as the handler and reconcile
  identity. The hello ACK's single result always echoes the outer correlation
  and may contain only the server's bounded nonnegative diagnostic duration as
  an additional field. All other early application packets fail closed. Context,
  artifacts/screenshots, click/type/action challenges, production routes, and
  production activation remain unavailable. A Ready development worker losing
  Core is fatal to the native port rather than a stale-ready projection.
  Read-only `pairing.status` remains local while Ready and cannot refresh
  admission or mask Core loss. `pairing.disconnect` withdraws the exact route
  and worker before local credential deletion, flushes its result, and then
  closes that native port; exchange remains unavailable while admitted.
  Uninstall must report credential cleanup pending until exact per-profile
  enumeration exists.

## Platform contract

- Installation is per-user and elevation-free.
- Supported registry entries are Chrome, Edge, Brave, Vivaldi, Opera, and Chromium stable on macOS, Windows, and conventional Linux desktop packages.
- Windows registration is HKCU-only. Brave, Vivaldi, and Opera share the reviewed Chrome compatibility key.
- macOS/Linux manifests use reviewed per-user native-messaging directories and absolute binary paths.
- Snap and Flatpak browser paths are not registration targets in v1.

## Verification

- `scripts/macos-installer.swift` owns the host-side AppKit setup launcher.
  Only an explicit Install click runs the exact bundled
  `Contents/Resources/a0-browser-bridge install --browser auto --json` with
  bounded output, a worker deadline, cleared environment, no shell, PATH,
  elevation, credentials or automatic retries. Verified completion requires
  the exact successful native receipt, not just exit status. Timeout or partial
  output is an unknown outcome; do not claim rollback. Pairing is a separate
  subsequent browser step. Its receipt-only self-test starts no process.
- `scripts/package-macos-release.mjs` packages a previously signed universal
  candidate into a signed macOS 13+ AppKit setup app and DMG, and a separate
  one-entry USTAR gzip payload. Use create-only output, exact Developer ID
  identity/team, source/binary digest readback and explicit icon input. The
  companion is copied unchanged, never re-signed by packaging. No installation,
  notarization, public upload or release activation occurs in this script.
  Final DMG hashes must be measured after notarization/stapling; the packaging
  receipt explicitly records only the pre-notary hash.
- `scripts/release-signer.swift` owns standalone local publisher/builder
  Ed25519 signing, separate from runtime and pairing/notarization credentials.
  Commands are `initialize|public publisher|builder`,
  `sign publisher|builder /absolute/input`, and Keychain-free `--self-test`.
  Use a stable compiled executable with default user-controlled macOS application
  access; never the Swift interpreter or all-application access. Only
  nonsynchronized 32-byte seeds use service
  `io.agentzero.browser_bridge.release-signing.v1`, accounts `publisher-2026`
  and `builder-2026`. Initialization is create-only, duplicate-safe and checks
  independent keys; no overwrite, rotation, deletion or secret export exists.
  Exact-account enumeration rejects ambiguity and malformed persistent records.
  Signing reads at most 128 KiB from retained nonsymlink regular single-link
  descriptors with identity/time readback, and emits only public metadata,
  input digest/size and detached signature. It performs no network activity or
  activation; the publisher independently validates statement semantics first.
  In-memory verification does not establish real Keychain acceptance.

- `scripts/build-macos-candidate.mjs` is an explicit local publisher build step,
  not native install authority. It requires an exact available Developer ID
  Application identity/team, explicit absolute Cargo/output paths and selected
  arm64/universal coverage. It builds stable source with locked offline
  dependencies, fingerprints inputs, signs only a new copied/combined binary,
  checks every signature slice/runtime/timestamp and embeds no new trust roots.
  Its ZIP and public signing receipt remain unsubmitted/not production-ready;
  an arm64-only candidate cannot satisfy universal release admission. It never
  installs toolchains, exports keys, invokes notarization or touches pairing.

- Structural validation without Rust: `PYTHONDONTWRITEBYTECODE=1 python3 tests/validate_structure.py` from this directory.
- When a pinned Rust toolchain is available: `cargo fmt --check` and `cargo test --locked`.
- Do not install Rust or fetch dependencies merely to validate this subtree.
