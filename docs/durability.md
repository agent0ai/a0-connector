# Durable host operations

The builtin `_a0_connector` plugin lives in Agent Zero Core; this repository supplies the host companion. Both sides must support the durable protocol. Legacy requests keep their existing behavior.

Set `A0_CONNECTOR_DURABILITY_KEY` to a persistent Fernet key supplied through your secret manager before starting the companion. The key is never written into connector settings or passed to browser/helper child processes. Keep the same key and journal directory across companion restarts. Missing keys disable durable capability advertisement; a changed key, corrupt journal, or competing journal owner holds operations instead of discarding receipts.

`A0_CONNECTOR_DURABILITY_ROOT` optionally selects the private browser journal directory. Otherwise the companion uses its platform data directory under `a0/durability/browser`. Computer Use uses a separate `computer_use` journal. One companion process owns each journal through a kernel file lock. Receipt values, session bindings, browser runtime addresses, and exported profile archives are encrypted and authenticated; operation IDs and journal structure remain visible. Isolated running Chromium profiles use native browser storage under private directories, rather than encrypted receipt storage.

## Receipt protocol

Browser hello metadata advertises `durable_session_v1` and Computer Use advertises `computer_use_durable_v1`, each with its own `durable_instance_id`. An operation carries:

```json
{"durable":{"schema":1,"deployment":"64 lowercase hex characters","session_id":"64 lowercase hex characters","operation_id":"64 lowercase hex characters","instance_id":"the advertised instance","fingerprint":"SHA256 of canonical UTF-8 request JSON"}}
```

Canonical JSON sorts keys, uses compact separators, preserves Unicode, rejects nonfinite numbers, and excludes only outer `op_id` and `durable`. The session binds the Core context and deployment to the selected host target and implementation sources. Changing an operation's request under the same ID is rejected. Core must pin the advertised instance before its first effect; a replacement journal cannot prove the absence of an old operation.

The companion commits an intent before dispatch, then saves the full public response before replying. Requester cancellation does not cancel the receipt task. `durable_status` with `target_operation_id` returns `absent` with `found:false`, `running`, `outcome_unknown`, or `completed` with the original response. Only authoritative absence on the pinned instance permits an initial delivery. An existing incomplete intent is never automatically dispatched again, including after companion death. Explicit shutdown or scope revocation may interrupt an operation and leave that intent held. All actions recheck current gateway permissions; receipts never grant permission.

Receipts are bounded to 64 MiB each, a 256 MiB encrypted result budget, 10,000 operations and 1,000 sessions. A completed result that cannot fit becomes a fixed, known retention-limit failure without repeating its action; these small failure receipts may exceed the result budget. There is no automatic receipt eviction: reaching the operation/session limit requires operator maintenance, not silent loss of retry identities.

The direct manager envelope uses `data`; the Socket.IO client maps it to the existing outer `result` field. A completed status embeds the original manager response, including `response.data.durable`. Status metadata names the target operation and marks old browser references expired after reattachment.

## Browser sessions and portable profiles

Durable Browser supports isolated agent Chromium profiles and explicitly selected CDP targets. Personal browser profiles and Safari are not exported or copied. CDP targets pin the exact browser WebSocket identity resolved from `/json/version`; a replacement browser behind the same HTTP address requires review.

Agent Chromium runs as a detached broker using a private per-session profile. A companion restart reconnects to the recorded broker without navigation. A partial launch without a verifiable broker holds instead of launching another browser. `durable_attach` lists current tabs; it does not navigate. Existing content refs expire and require fresh content.

`durable_export` first closes the isolated browser and waits for its profile to become quiescent. Its snapshot contains a bounded archive, SHA256 and size, safe tab URLs, platform/family, executable digest and reported browser version, and adapter source digest. Export excludes native Chromium session/POST restoration records and rejects linked/special files. Export is limited to 20,000 entries, 128 MiB uncompressed and 32 MiB compressed.

`durable_import` accepts `snapshot` only into a fresh isolated profile with matching platform, browser binary and adapter sources. It validates the full archive, stages files privately, then publishes the directory atomically. `restore_documents:true` explicitly recreates bounded HTTP(S)/`about:blank` documents with fresh GETs and new tab IDs inside that import's own journal intent. It never restores a native POST or repeats an old browser operation. A partially completed import remains uncertain and is not repeated automatically. Browser or source upgrades can require a fresh compatible session rather than silently importing incompatible profile databases.

## Desktop sessions

Computer Use binds its journal to the companion instance, desktop identity, backend/helper sources and trust mode. It preserves existing platform approval, explicit rearm and native session-ID checks. `start_session` is a separate durable operation; a receipt does not arm a desktop implicitly. Personal desktop state is never transplanted to another host. Known captures/results can be recovered; an input whose helper reply was lost remains held.

## Verification

Run `pytest tests/test_host_browser_durable.py tests/test_computer_use_durable.py`. The browser process tests additionally require `A0_TEST_DURABLE_CHROMIUM` pointing to an explicitly selected executable; they create private temporary headless profiles and a synthetic local HTTP server. They cover real companion SIGKILL before/after result commit, preserved broker attachment, no duplicate POST, and managed profile export/import. Desktop tests use a real synthetic JSONL helper process, not a physical desktop acceptance claim.
