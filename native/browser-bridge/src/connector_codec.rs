//! Bounded browser-only Socket.IO/native mapping. This is transport, not authority:
//! every decoded command still passes the RelaySession activation gate.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use crate::json::{self, Value};
use crate::rpc::{self, Peer, RpcMessage, RpcRequest, RpcResponse};
use crate::transport_profile::{BrowserTransportProfile, PRODUCTION_HANDLER_ID};

// ContextCodec remains production-only and intentionally imports this fixed
// production identity rather than the compiled browser transport profile.
pub(crate) const HANDLER_ID: &str = PRODUCTION_HANDLER_ID;
const MAX_PENDING: usize = 128;
const MAX_COMPLETED: usize = 2048;
const BINDINGS: &[&str] = &[
    "contract_version",
    "bridge_id",
    "load_generation_id",
    "context_id",
    "browser_session_id",
    "turn_id",
    "op_id",
    "action_id",
    "control_id",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum CodecError {
    InvalidPacket,
    UnsupportedEvent,
    InvalidResponse,
    Capacity,
}

struct Pending {
    correlation: String,
    identity: String,
    method: String,
    bindings: BTreeMap<String, Value>,
    response_bindings: BTreeMap<String, Value>,
    operation_action: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct PendingOperationBinding {
    pub(crate) bridge_id: String,
    pub(crate) load_generation_id: String,
    pub(crate) context_id: String,
    pub(crate) browser_session_id: String,
    pub(crate) turn_id: String,
    pub(crate) op_id: String,
    pub(crate) action_id: String,
    pub(crate) action: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct VerifiedArtifactClaim {
    pub(crate) artifact_id: String,
    pub(crate) mime_type: String,
    pub(crate) byte_count: u64,
    pub(crate) sha256: String,
    pub(crate) purpose: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct OperationSettlement {
    pub(crate) binding: PendingOperationBinding,
    pub(crate) succeeded: bool,
    pub(crate) artifacts: Vec<VerifiedArtifactClaim>,
}

pub(crate) struct PreparedResponse {
    response_id: String,
    identity: String,
    packet: String,
    operation: Option<OperationSettlement>,
}

impl PreparedResponse {
    pub(crate) fn operation(&self) -> Option<&OperationSettlement> {
        self.operation.as_ref()
    }
}

pub(crate) struct ConnectorCodec {
    transport_profile: BrowserTransportProfile,
    load_generation_id: String,
    sequence: u64,
    pending: BTreeMap<String, Pending>,
    identities: BTreeSet<String>,
    completed: VecDeque<String>,
}

impl ConnectorCodec {
    pub(crate) fn new(load_generation_id: String) -> Self {
        Self::with_profile(load_generation_id, BrowserTransportProfile::compiled())
    }

    pub(crate) fn with_profile(
        load_generation_id: String,
        transport_profile: BrowserTransportProfile,
    ) -> Self {
        Self {
            transport_profile,
            load_generation_id,
            sequence: 0,
            pending: BTreeMap::new(),
            identities: BTreeSet::new(),
            completed: VecDeque::new(),
        }
    }

    pub(crate) const fn transport_profile(&self) -> BrowserTransportProfile {
        self.transport_profile
    }

    pub(crate) fn command(&mut self, packet: &str, bridge_id: &str) -> Result<Vec<u8>, CodecError> {
        if packet.len() > rpc::MAX_NON_ARTIFACT_BYTES {
            return Err(CodecError::InvalidPacket);
        }
        // Core sends named events, not Socket.IO requests. Reject ACK IDs here
        // rather than dropping one and leaving Core's request unresolved.
        let array = json::parse(
            packet
                .strip_prefix("42/ws,")
                .ok_or(CodecError::InvalidPacket)?
                .as_bytes(),
        )
        .map_err(|_| CodecError::InvalidPacket)?;
        let array = array
            .as_array()
            .filter(|v| v.len() == 2)
            .ok_or(CodecError::InvalidPacket)?;
        let event = array[0].as_str().ok_or(CodecError::InvalidPacket)?;
        let envelope = array[1].as_object().ok_or(CodecError::InvalidPacket)?;
        #[cfg(feature = "local-development")]
        if self.transport_profile.is_limited_development() {
            crate::development_session::validate_development_core_event_envelope(envelope)
                .map_err(|_| CodecError::InvalidPacket)?;
        }
        if envelope.get("handlerId").and_then(Value::as_str)
            != Some(self.transport_profile.handler_id())
        {
            return Err(CodecError::InvalidPacket);
        }
        let correlation = identifier(envelope, "correlationId")?.to_owned();
        let mut params = envelope
            .get("data")
            .and_then(Value::as_object)
            .ok_or(CodecError::InvalidPacket)?
            .clone();
        if identifier(&params, "load_generation_id")? != self.load_generation_id
            || identifier(&params, "bridge_id")? != bridge_id
        {
            return Err(CodecError::InvalidPacket);
        }
        let method = match event {
            "connector_browser_op" => "browser.perform".to_owned(),
            "connector_browser_control" => {
                let method = params
                    .remove("method")
                    .and_then(|v| v.as_str().map(str::to_owned))
                    .ok_or(CodecError::InvalidPacket)?;
                if !matches!(
                    method.as_str(),
                    "browser.cancel"
                        | "browser.finalize_turn"
                        | "browser.resolve_challenge"
                        | "browser.reconcile"
                ) {
                    return Err(CodecError::UnsupportedEvent);
                }
                method
            }
            _ => return Err(CodecError::UnsupportedEvent),
        };
        if method == "browser.perform" {
            let action = params
                .get("action")
                .and_then(Value::as_str)
                .ok_or(CodecError::InvalidPacket)?;
            if !self.transport_profile.permits_browser_action(action) {
                return Err(CodecError::UnsupportedEvent);
            }
            if self.transport_profile.is_limited_development() {
                let required = params
                    .get("required_capabilities")
                    .and_then(Value::as_array)
                    .ok_or(CodecError::InvalidPacket)?;
                if !required.iter().any(|value| value.as_str() == Some(action))
                    || required.iter().any(|value| {
                        value.as_str().is_none_or(|capability| {
                            !self
                                .transport_profile
                                .permits_browser_capability(capability)
                        })
                    })
                {
                    return Err(CodecError::UnsupportedEvent);
                }
            }
        }
        if method == "browser.resolve_challenge"
            && !self.transport_profile.permits_action_challenge()
            && params.get("action_class").and_then(Value::as_str) != Some("navigate")
        {
            return Err(CodecError::UnsupportedEvent);
        }
        let identity_key = if method == "browser.perform" {
            "op_id"
        } else {
            "control_id"
        };
        if method != "browser.reconcile" {
            for key in ["context_id", "browser_session_id", "turn_id"] {
                identifier(&params, key)?;
            }
        }
        let identity = format!("{identity_key}:{}", identifier(&params, identity_key)?);
        if self.identities.contains(&identity) {
            return Err(CodecError::InvalidPacket);
        }
        if self.pending.len() >= MAX_PENDING {
            return Err(CodecError::Capacity);
        }
        self.sequence = self.sequence.checked_add(1).ok_or(CodecError::Capacity)?;
        let id = format!("core-{}", self.sequence);
        let bindings = params
            .iter()
            .filter(|(key, _)| BINDINGS.contains(&key.as_str()))
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect();
        // Core routing bindings are validated above and retained privately for
        // result correlation, not forwarded as extra native method arguments.
        params.remove("bridge_id");
        params.remove("load_generation_id");
        let message = RpcMessage::Request(RpcRequest {
            id: Some(id.clone()),
            method: method.clone(),
            params: Value::Object(params.clone()),
        })
        .encode();
        rpc::parse_message(&message, Peer::Server).map_err(|_| CodecError::InvalidPacket)?;
        let response_bindings = if method == "browser.resolve_challenge" {
            ["challenge_id", "decision"]
                .into_iter()
                .map(|key| {
                    params
                        .get(key)
                        .cloned()
                        .map(|value| (key.to_owned(), value))
                        .ok_or(CodecError::InvalidPacket)
                })
                .collect::<Result<BTreeMap<_, _>, _>>()?
        } else if method == "browser.perform"
            && params
                .get("action")
                .and_then(Value::as_str)
                .is_some_and(|action| matches!(action, "click" | "type" | "upload_file"))
        {
            let target = params
                .get("target")
                .and_then(Value::as_object)
                .ok_or(CodecError::InvalidPacket)?;
            let args = params
                .get("args")
                .and_then(Value::as_object)
                .ok_or(CodecError::InvalidPacket)?;
            BTreeMap::from([
                (
                    "tab_handle".to_owned(),
                    target
                        .get("tab_handle")
                        .cloned()
                        .ok_or(CodecError::InvalidPacket)?,
                ),
                (
                    "ref".to_owned(),
                    args.get("ref").cloned().ok_or(CodecError::InvalidPacket)?,
                ),
                (
                    "expected_action_class".to_owned(),
                    args.get("expected_action_class")
                        .cloned()
                        .ok_or(CodecError::InvalidPacket)?,
                ),
            ])
        } else {
            BTreeMap::new()
        };
        let operation_action = (method == "browser.perform")
            .then(|| {
                params
                    .get("action")
                    .and_then(Value::as_str)
                    .map(str::to_owned)
            })
            .flatten();
        self.identities.insert(identity.clone());
        self.pending.insert(
            id,
            Pending {
                correlation,
                identity,
                method,
                bindings,
                response_bindings,
                operation_action,
            },
        );
        Ok(message)
    }

    pub(crate) fn pending_operation(&self, op_id: &str) -> Option<PendingOperationBinding> {
        if !rpc::valid_opaque_id(op_id) {
            return None;
        }
        let pending = self.pending.values().find(|pending| {
            pending.method == "browser.perform"
                && pending.bindings.get("op_id").and_then(Value::as_str) == Some(op_id)
        })?;
        operation_binding(pending)
    }

    #[cfg(test)]
    pub(crate) fn response(&mut self, bytes: &[u8]) -> Result<String, CodecError> {
        let prepared = self.prepare_response(bytes)?;
        if prepared
            .operation()
            .is_some_and(|operation| !operation.artifacts.is_empty())
        {
            return Err(CodecError::InvalidResponse);
        }
        self.commit_response(prepared)
    }

    pub(crate) fn prepare_response(&self, bytes: &[u8]) -> Result<PreparedResponse, CodecError> {
        let RpcMessage::Response(response) =
            rpc::parse_message(bytes, Peer::Extension).map_err(|_| CodecError::InvalidResponse)?
        else {
            return Err(CodecError::InvalidResponse);
        };
        let pending = self
            .pending
            .get(&response.id)
            .ok_or(CodecError::InvalidResponse)?;
        let projection = response_data(pending, &response)?;
        let event = if pending.method == "browser.perform" {
            "connector_browser_op_result"
        } else {
            "connector_browser_control_result"
        };
        let packet = format!(
            "42/ws,{}",
            Value::Array(vec![
                Value::String(event.into()),
                Value::Object(BTreeMap::from([
                    (
                        "correlationId".into(),
                        Value::String(pending.correlation.clone())
                    ),
                    ("data".into(), Value::Object(projection.data)),
                ]))
            ])
            .encode()
        );
        if packet.len() > rpc::MAX_NON_ARTIFACT_BYTES {
            return Err(CodecError::InvalidResponse);
        }
        Ok(PreparedResponse {
            response_id: response.id,
            identity: pending.identity.clone(),
            packet,
            operation: projection.operation,
        })
    }

    pub(crate) fn commit_response(
        &mut self,
        prepared: PreparedResponse,
    ) -> Result<String, CodecError> {
        if self
            .pending
            .get(&prepared.response_id)
            .is_none_or(|pending| pending.identity != prepared.identity)
        {
            return Err(CodecError::InvalidResponse);
        }
        let pending = self
            .pending
            .remove(&prepared.response_id)
            .expect("validated correlation");
        self.completed.push_back(pending.identity);
        if self.completed.len() > MAX_COMPLETED {
            if let Some(oldest) = self.completed.pop_front() {
                self.identities.remove(&oldest);
            }
        }
        Ok(prepared.packet)
    }
}

struct ResponseProjection {
    data: BTreeMap<String, Value>,
    operation: Option<OperationSettlement>,
}

fn identifier<'a>(fields: &'a BTreeMap<String, Value>, key: &str) -> Result<&'a str, CodecError> {
    fields
        .get(key)
        .and_then(Value::as_str)
        .filter(|v| rpc::valid_opaque_id(v))
        .ok_or(CodecError::InvalidPacket)
}

fn response_data(
    pending: &Pending,
    response: &RpcResponse,
) -> Result<ResponseProjection, CodecError> {
    let mut output = pending.bindings.clone();
    let mut operation = None;
    if pending.method != "browser.perform" {
        output.insert("method".into(), Value::String(pending.method.clone()));
    }
    match &response.result {
        Ok(value) => {
            let fields = value.as_object().ok_or(CodecError::InvalidResponse)?;
            if fields.get("contract_version").and_then(Value::as_u64) != Some(1) {
                return Err(CodecError::InvalidResponse);
            }
            // Never replace authoritative request identity with response claims.
            for key in BINDINGS {
                if let Some(value) = fields.get(*key) {
                    if pending.bindings.get(*key) != Some(value) {
                        return Err(CodecError::InvalidResponse);
                    }
                }
            }
            let identity_keys: &[&str] = if pending.method == "browser.perform" {
                &["op_id", "action_id"]
            } else {
                &["control_id"]
            };
            for key in identity_keys {
                if fields.get(*key).is_none() || fields.get(*key) != pending.bindings.get(*key) {
                    return Err(CodecError::InvalidResponse);
                }
            }
            if pending.method == "browser.resolve_challenge" {
                validate_resolve_response(fields, pending)?;
            }
            output.insert("ok".into(), Value::Bool(true));
            if pending.method == "browser.perform" {
                const KEYS: &[&str] = &[
                    "action_id",
                    "artifacts",
                    "completed_at_ms",
                    "contract_version",
                    "op_id",
                    "receipts",
                    "result",
                    "status",
                ];
                if fields.len() != KEYS.len()
                    || KEYS.iter().any(|key| !fields.contains_key(*key))
                    || fields.get("status").and_then(Value::as_str) != Some("succeeded")
                    || !fields
                        .get("completed_at_ms")
                        .and_then(Value::as_u64)
                        .is_some_and(|value| (1..=9_007_199_254_740_991).contains(&value))
                {
                    return Err(CodecError::InvalidResponse);
                }
                // Protocol §7.3 requires this extension-local completion timestamp.
                // It is diagnostic data, not authority/freshness, and is deliberately
                // absent from the compatible Core operation-result projection.
                let result = fields
                    .get("result")
                    .filter(|v| v.as_object().is_some())
                    .ok_or(CodecError::InvalidResponse)?;
                output.insert("result".into(), result.clone());
                let receipts = fields
                    .get("receipts")
                    .and_then(Value::as_array)
                    .filter(|values| values.is_empty())
                    .ok_or(CodecError::InvalidResponse)?;
                let artifacts = parse_artifact_claims(
                    fields
                        .get("artifacts")
                        .and_then(Value::as_array)
                        .ok_or(CodecError::InvalidResponse)?,
                )?;
                if pending.operation_action.as_deref() == Some("screenshot") {
                    validate_screenshot_result(result, &artifacts)?;
                } else if matches!(
                    pending.operation_action.as_deref(),
                    Some("click" | "upload_file")
                ) {
                    if !artifacts.is_empty() {
                        return Err(CodecError::InvalidResponse);
                    }
                    validate_click_result(result, pending)?;
                    if pending.operation_action.as_deref() == Some("upload_file")
                        && result
                            .as_object()
                            .and_then(|fields| fields.get("action_class"))
                            .and_then(Value::as_str)
                            != Some("external_side_effect")
                    {
                        return Err(CodecError::InvalidResponse);
                    }
                } else if pending.operation_action.as_deref() == Some("type") {
                    if !artifacts.is_empty() {
                        return Err(CodecError::InvalidResponse);
                    }
                    validate_type_result(result, pending)?;
                } else if !artifacts.is_empty() {
                    return Err(CodecError::InvalidResponse);
                }
                output.insert("receipts".into(), Value::Array(receipts.to_vec()));
                output.insert("artifacts".into(), fields["artifacts"].clone());
                operation = Some(OperationSettlement {
                    binding: operation_binding(pending).ok_or(CodecError::InvalidResponse)?,
                    succeeded: true,
                    artifacts,
                });
            } else {
                output.insert("result".into(), value.clone());
            }
        }
        Err(error) => {
            let fields = error
                .data
                .as_ref()
                .and_then(Value::as_object)
                .ok_or(CodecError::InvalidResponse)?;
            let code = fields
                .get("a0_code")
                .and_then(Value::as_str)
                .filter(|v| {
                    v.as_bytes().first().is_some_and(u8::is_ascii_uppercase)
                        && v.len() <= 64
                        && v.bytes()
                            .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_')
                })
                .ok_or(CodecError::InvalidResponse)?;
            let outcome = fields
                .get("outcome")
                .and_then(Value::as_str)
                .filter(|v| matches!(*v, "not_applied" | "applied" | "unknown"))
                .ok_or(CodecError::InvalidResponse)?;
            let retryable = fields
                .get("retryable")
                .filter(|v| matches!(v, Value::Bool(_)))
                .ok_or(CodecError::InvalidResponse)?;
            output.insert("ok".into(), Value::Bool(false));
            output.insert("code".into(), Value::String(code.into()));
            // Remote errors may carry page or secret values. Preserve only the
            // bounded typed outcome; don't forward message/details verbatim.
            output.insert(
                "error".into(),
                Value::String("The browser operation did not complete successfully.".into()),
            );
            output.insert(
                "error_data".into(),
                Value::Object(BTreeMap::from([
                    ("outcome".into(), Value::String(outcome.into())),
                    ("retryable".into(), retryable.clone()),
                    ("details".into(), Value::Object(BTreeMap::new())),
                ])),
            );
            if pending.method == "browser.perform" {
                operation = Some(OperationSettlement {
                    binding: operation_binding(pending).ok_or(CodecError::InvalidResponse)?,
                    succeeded: false,
                    artifacts: Vec::new(),
                });
            }
        }
    }
    Ok(ResponseProjection {
        data: output,
        operation,
    })
}

fn operation_binding(pending: &Pending) -> Option<PendingOperationBinding> {
    let value = |key: &str| {
        pending
            .bindings
            .get(key)
            .and_then(Value::as_str)
            .map(str::to_owned)
    };
    Some(PendingOperationBinding {
        bridge_id: value("bridge_id")?,
        load_generation_id: value("load_generation_id")?,
        context_id: value("context_id")?,
        browser_session_id: value("browser_session_id")?,
        turn_id: value("turn_id")?,
        op_id: value("op_id")?,
        action_id: value("action_id")?,
        action: pending.operation_action.clone()?,
    })
}

fn parse_artifact_claims(values: &[Value]) -> Result<Vec<VerifiedArtifactClaim>, CodecError> {
    if values.len() > 16 {
        return Err(CodecError::InvalidResponse);
    }
    values
        .iter()
        .map(|value| {
            let fields = value.as_object().ok_or(CodecError::InvalidResponse)?;
            const KEYS: &[&str] = &[
                "artifact_id",
                "byte_count",
                "mime_type",
                "purpose",
                "sha256",
            ];
            if fields.len() != KEYS.len() || KEYS.iter().any(|key| !fields.contains_key(*key)) {
                return Err(CodecError::InvalidResponse);
            }
            let artifact_id = identifier(fields, "artifact_id")?.to_owned();
            let mime_type = fields
                .get("mime_type")
                .and_then(Value::as_str)
                .filter(|value| {
                    !value.is_empty()
                        && value.len() <= 256
                        && value.is_ascii()
                        && value.contains('/')
                })
                .ok_or(CodecError::InvalidResponse)?
                .to_owned();
            let byte_count = fields
                .get("byte_count")
                .and_then(Value::as_u64)
                .filter(|value| *value <= rpc::MAX_ARTIFACT_BYTES)
                .ok_or(CodecError::InvalidResponse)?;
            let sha256 = fields
                .get("sha256")
                .and_then(Value::as_str)
                .filter(|value| valid_sha256(value))
                .ok_or(CodecError::InvalidResponse)?
                .to_owned();
            let purpose = fields
                .get("purpose")
                .and_then(Value::as_str)
                .filter(|value| matches!(*value, "screenshot" | "download"))
                .ok_or(CodecError::InvalidResponse)?
                .to_owned();
            Ok(VerifiedArtifactClaim {
                artifact_id,
                mime_type,
                byte_count,
                sha256,
                purpose,
            })
        })
        .collect()
}

fn validate_screenshot_result(
    result: &Value,
    artifacts: &[VerifiedArtifactClaim],
) -> Result<(), CodecError> {
    let fields = result.as_object().ok_or(CodecError::InvalidResponse)?;
    const KEYS: &[&str] = &["artifact_id", "browser_id", "lease_id", "tab_handle"];
    if fields.len() != KEYS.len()
        || KEYS.iter().any(|key| !fields.contains_key(*key))
        || artifacts.len() != 1
        || artifacts[0].purpose != "screenshot"
        || identifier(fields, "artifact_id")? != artifacts[0].artifact_id
        || identifier(fields, "browser_id").is_err()
        || identifier(fields, "lease_id").is_err()
        || identifier(fields, "tab_handle").is_err()
    {
        return Err(CodecError::InvalidResponse);
    }
    Ok(())
}

fn validate_click_result(result: &Value, pending: &Pending) -> Result<(), CodecError> {
    validate_semantic_action_result(result, pending, false)
}

fn validate_type_result(result: &Value, pending: &Pending) -> Result<(), CodecError> {
    validate_semantic_action_result(result, pending, true)
}

fn validate_semantic_action_result(
    result: &Value,
    pending: &Pending,
    exact_sensitive_input: bool,
) -> Result<(), CodecError> {
    let fields = result.as_object().ok_or(CodecError::InvalidResponse)?;
    const KEYS: &[&str] = &[
        "lease_id",
        "browser_id",
        "tab_handle",
        "document_epoch",
        "ref",
        "action_class",
    ];
    if fields.len() != KEYS.len() || KEYS.iter().any(|key| !fields.contains_key(*key)) {
        return Err(CodecError::InvalidResponse);
    }
    let expected_tab = pending
        .response_bindings
        .get("tab_handle")
        .and_then(Value::as_str)
        .ok_or(CodecError::InvalidResponse)?;
    let tab_handle = identifier(fields, "tab_handle")?;
    if identifier(fields, "lease_id").is_err()
        || tab_handle != expected_tab
        || identifier(fields, "browser_id")? != tab_handle
        || fields.get("ref") != pending.response_bindings.get("ref")
        || !fields
            .get("document_epoch")
            .and_then(Value::as_str)
            .is_some_and(valid_document_epoch)
    {
        return Err(CodecError::InvalidResponse);
    }
    let actual = fields
        .get("action_class")
        .and_then(Value::as_str)
        .ok_or(CodecError::InvalidResponse)?;
    let expected = pending
        .response_bindings
        .get("expected_action_class")
        .and_then(Value::as_str)
        .ok_or(CodecError::InvalidResponse)?;
    if (exact_sensitive_input && actual != "sensitive_input")
        || (!exact_sensitive_input && !action_class_at_least_expected(expected, actual))
    {
        return Err(CodecError::InvalidResponse);
    }
    Ok(())
}

fn valid_document_epoch(value: &str) -> bool {
    if value != "0" && value.starts_with('0') {
        return false;
    }
    value
        .parse::<u64>()
        .ok()
        .is_some_and(|epoch| epoch <= 9_007_199_254_740_991)
}

fn action_class_at_least_expected(expected: &str, actual: &str) -> bool {
    match expected {
        "reversible_input" => matches!(
            actual,
            "reversible_input" | "sensitive_input" | "external_side_effect" | "unknown"
        ),
        "sensitive_input" => {
            matches!(
                actual,
                "sensitive_input" | "external_side_effect" | "unknown"
            )
        }
        "external_side_effect" => matches!(actual, "external_side_effect" | "unknown"),
        "unknown" => actual == "unknown",
        _ => false,
    }
}

fn valid_sha256(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|digest| {
        digest.len() == 64
            && digest
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    })
}

fn validate_resolve_response(
    fields: &BTreeMap<String, Value>,
    pending: &Pending,
) -> Result<(), CodecError> {
    const KEYS: &[&str] = &[
        "contract_version",
        "control_id",
        "challenge_id",
        "status",
        "decision",
    ];
    if fields.len() != KEYS.len()
        || KEYS.iter().any(|key| !fields.contains_key(*key))
        || fields.get("status").and_then(Value::as_str) != Some("resolved")
    {
        return Err(CodecError::InvalidResponse);
    }
    for key in ["challenge_id", "decision"] {
        if fields.get(key) != pending.response_bindings.get(key) {
            return Err(CodecError::InvalidResponse);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};

    const DIGEST: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    #[test]
    fn core_extension_reconciliation_parity_fixture_preserves_exact_binding() {
        let fixture = json::parse(include_bytes!(
            "../tests/fixtures/browser-reconcile-v1.json"
        ))
        .expect("synthetic shared fixture");
        let fixture = fixture.as_object().unwrap();
        let profile = BrowserTransportProfile::fixture_development();
        let mut codec = ConnectorCodec::with_profile("load-dev-1".into(), profile);
        let command = format!(
            "42/ws,{}",
            Value::Array(vec![
                Value::String("connector_browser_control".into()),
                Value::Object(BTreeMap::from([
                    (
                        "handlerId".into(),
                        Value::String(profile.handler_id().into())
                    ),
                    ("eventId".into(), Value::String("event-reconcile-1".into())),
                    ("correlationId".into(), Value::String("reconcile-1".into())),
                    (
                        "ts".into(),
                        Value::String("2026-09-05T12:34:56.789Z".into())
                    ),
                    ("data".into(), fixture["core_control"].clone()),
                ])),
            ])
            .encode()
        );
        let native = codec.command(&command, "bridge-dev-1").unwrap();
        let RpcMessage::Request(request) = rpc::parse_message(&native, Peer::Server).unwrap()
        else {
            panic!("expected reconcile request")
        };
        assert_eq!(request.method, "browser.reconcile");
        assert_eq!(request.params, fixture["extension_request"]);
        let response = RpcMessage::Response(RpcResponse {
            id: request.id.unwrap(),
            result: Ok(fixture["extension_result"].clone()),
        })
        .encode();
        let packet = codec.response(&response).unwrap();
        let decoded = json::parse(packet.strip_prefix("42/ws,").unwrap().as_bytes()).unwrap();
        let decoded = decoded.as_array().unwrap();
        assert_eq!(
            decoded[0].as_str(),
            Some("connector_browser_control_result")
        );
        assert_eq!(
            decoded[1].as_object().unwrap()["data"],
            fixture["core_control_result"]
        );
    }

    fn command(op: &str) -> String {
        let RpcMessage::Request(mut req) = rpc::parse_message(
            include_bytes!("../tests/fixtures/native-rpc-v1/browser-perform.valid.json"),
            Peer::Server,
        )
        .unwrap() else {
            panic!()
        };
        let Value::Object(ref mut params) = req.params else {
            panic!()
        };
        params.insert("op_id".into(), Value::String(op.into()));
        params.insert("bridge_id".into(), Value::String("bridge-1".into()));
        params.insert(
            "load_generation_id".into(),
            Value::String("generation-1".into()),
        );
        format!(
            "42/ws,{}",
            Value::Array(vec![
                Value::String("connector_browser_op".into()),
                Value::Object(BTreeMap::from([
                    ("handlerId".into(), Value::String(HANDLER_ID.into())),
                    (
                        "correlationId".into(),
                        Value::String("correlation-1".into())
                    ),
                    ("data".into(), req.params),
                ]))
            ])
            .encode()
        )
    }

    fn type_command(op: &str, text: &str) -> String {
        let RpcMessage::Request(mut req) = rpc::parse_message(
            include_bytes!("../tests/fixtures/native-rpc-v1/browser-perform.valid.json"),
            Peer::Server,
        )
        .unwrap() else {
            panic!()
        };
        let Value::Object(ref mut params) = req.params else {
            panic!()
        };
        params.insert("op_id".into(), Value::String(op.into()));
        params.insert("action".into(), Value::String("type".into()));
        params.insert(
            "args".into(),
            Value::Object(BTreeMap::from([
                ("ref".into(), Value::String("frame0:node24".into())),
                ("text".into(), Value::String(text.into())),
                (
                    "text_sha256".into(),
                    Value::String(
                        Sha256::digest(text.as_bytes())
                            .iter()
                            .map(|byte| format!("{byte:02x}"))
                            .collect(),
                    ),
                ),
                (
                    "expected_action_class".into(),
                    Value::String("sensitive_input".into()),
                ),
            ])),
        );
        params.insert(
            "required_capabilities".into(),
            Value::Array(vec![
                Value::String("type".into()),
                Value::String("semantic_dom_v1".into()),
                Value::String("cursor_v1".into()),
                Value::String("trusted_input_v1".into()),
            ]),
        );
        params.insert("bridge_id".into(), Value::String("bridge-1".into()));
        params.insert(
            "load_generation_id".into(),
            Value::String("generation-1".into()),
        );
        format!(
            "42/ws,{}",
            Value::Array(vec![
                Value::String("connector_browser_op".into()),
                Value::Object(BTreeMap::from([
                    ("handlerId".into(), Value::String(HANDLER_ID.into())),
                    (
                        "correlationId".into(),
                        Value::String("correlation-type-1".into())
                    ),
                    ("data".into(), req.params),
                ]))
            ])
            .encode()
        )
    }

    fn resolve_command(control_id: &str) -> String {
        format!(
            concat!(
                "42/ws,[\"connector_browser_control\",{{",
                "\"handlerId\":\"{HANDLER_ID}\",\"correlationId\":\"correlation_2\",",
                "\"data\":{{\"method\":\"browser.resolve_challenge\",",
                "\"contract_version\":1,\"bridge_id\":\"bridge-1\",",
                "\"load_generation_id\":\"generation-1\",",
                "\"control_id\":\"{control_id}\",\"challenge_id\":\"challenge-1\",",
                "\"context_id\":\"context-1\",\"browser_session_id\":\"session-1\",",
                "\"turn_id\":\"turn-1\",\"op_id\":\"op-1\",\"action_id\":\"action-1\",",
                "\"tab_handle\":\"tab-1\",\"document_id\":null,\"document_epoch\":0,",
                "\"canonical_parameter_hash\":\"{DIGEST}\",",
                "\"target_fingerprint\":\"{DIGEST}\",\"origin\":\"https://example.test\",",
                "\"action_class\":\"navigate\",\"decision\":\"allow_once\",",
                "\"grant\":{{\"origin_grant_id\":\"grant-1\",\"scope\":\"operation\",",
                "\"origin\":\"https://example.test\",\"expires_at_ms\":2000}}",
                "}}}}]"
            ),
            HANDLER_ID = HANDLER_ID,
            control_id = control_id,
            DIGEST = DIGEST,
        )
    }

    fn action_resolve_command(control_id: &str) -> String {
        format!(
            concat!(
                "42/ws,[\"connector_browser_control\",{{",
                "\"handlerId\":\"{HANDLER_ID}\",\"correlationId\":\"correlation_3\",",
                "\"data\":{{\"method\":\"browser.resolve_challenge\",",
                "\"contract_version\":1,\"bridge_id\":\"bridge-1\",",
                "\"load_generation_id\":\"generation-1\",",
                "\"control_id\":\"{control_id}\",\"challenge_id\":\"challenge-2\",",
                "\"context_id\":\"context-1\",\"browser_session_id\":\"session-1\",",
                "\"turn_id\":\"turn-1\",\"op_id\":\"op-2\",\"action_id\":\"action-2\",",
                "\"tab_handle\":\"tab-1\",\"document_id\":\"document-1\",\"document_epoch\":1,",
                "\"canonical_parameter_hash\":\"{DIGEST}\",",
                "\"target_fingerprint\":\"{DIGEST}\",\"origin\":\"https://example.test\",",
                "\"action_class\":\"unknown\",\"data_classification\":\"none\",",
                "\"decision\":\"approve_once\",\"grant\":{{",
                "\"action_grant_id\":\"grant-2\",\"scope\":\"operation\",",
                "\"origin\":\"https://example.test\",\"action_class\":\"unknown\",",
                "\"canonical_parameter_hash\":\"{DIGEST}\",",
                "\"target_fingerprint\":\"{DIGEST}\",\"data_classification\":\"none\",",
                "\"expires_at_ms\":2000}}",
                "}}}}]"
            ),
            HANDLER_ID = HANDLER_ID,
            control_id = control_id,
            DIGEST = DIGEST,
        )
    }

    fn type_resolve_command(control_id: &str) -> String {
        format!(
            concat!(
                "42/ws,[\"connector_browser_control\",{{",
                "\"handlerId\":\"{HANDLER_ID}\",\"correlationId\":\"correlation_type_2\",",
                "\"data\":{{\"method\":\"browser.resolve_challenge\",",
                "\"contract_version\":1,\"bridge_id\":\"bridge-1\",",
                "\"load_generation_id\":\"generation-1\",",
                "\"control_id\":\"{control_id}\",\"challenge_id\":\"challenge-type-1\",",
                "\"context_id\":\"context-1\",\"browser_session_id\":\"session-1\",",
                "\"turn_id\":\"turn-1\",\"op_id\":\"op-type-1\",\"action_id\":\"action-type-1\",",
                "\"tab_handle\":\"tab-1\",\"document_id\":\"document-1\",\"document_epoch\":1,",
                "\"canonical_parameter_hash\":\"{DIGEST}\",",
                "\"target_fingerprint\":\"{DIGEST}\",\"origin\":\"https://example.test\",",
                "\"action_class\":\"sensitive_input\",\"data_classification\":{{",
                "\"kind\":\"text\",\"sensitivity\":\"sensitive\",\"text_sha256\":\"{DIGEST}\"}},",
                "\"decision\":\"approve_once\",\"grant\":{{",
                "\"action_grant_id\":\"grant-type-1\",\"scope\":\"operation\",",
                "\"origin\":\"https://example.test\",\"action_class\":\"sensitive_input\",",
                "\"canonical_parameter_hash\":\"{DIGEST}\",",
                "\"target_fingerprint\":\"{DIGEST}\",\"data_classification\":{{",
                "\"kind\":\"text\",\"sensitivity\":\"sensitive\",\"text_sha256\":\"{DIGEST}\"}},",
                "\"expires_at_ms\":2000}}",
                "}}}}]"
            ),
            HANDLER_ID = HANDLER_ID,
            control_id = control_id,
            DIGEST = DIGEST,
        )
    }

    #[test]
    fn emitted_operation_success_requires_exact_completion_timestamp_envelope() {
        let mut packet = json::parse(command("op-open").strip_prefix("42/ws,").unwrap().as_bytes()).unwrap();
        let Value::Array(ref mut envelope) = packet else { panic!() };
        let Value::Object(ref mut wrapper) = envelope[1] else { panic!() };
        let Value::Object(ref mut params) = wrapper.get_mut("data").unwrap() else { panic!() };
        params.insert("action".into(), Value::String("open".into()));
        params.insert("target".into(), Value::Null);
        params.insert("args".into(), json::parse(br#"{"url":"https://example.test/"}"#).unwrap());
        params.insert("required_capabilities".into(), json::parse(br#"["open","tab_leases_v1","tab_groups_v1"]"#).unwrap());
        let command = format!("42/ws,{}", packet.encode());
        let mut codec = ConnectorCodec::new("generation-1".into());
        let native = codec.command(&command, "bridge-1").unwrap();
        let RpcMessage::Request(request) = rpc::parse_message(&native, Peer::Server).unwrap() else { panic!() };
        // Exact operationSuccess envelope emitted by the extension after open.
        let valid = format!(
            r#"{{"jsonrpc":"2.0","id":"{}","result":{{"contract_version":1,"op_id":"op-open","action_id":"action-1","status":"succeeded","result":{{"lease_id":"lease-1","browser_id":"a0t1.generation-1.lease-1","tab_handle":"a0t1.generation-1.lease-1"}},"receipts":[],"artifacts":[],"completed_at_ms":1788492445123}}}}"#,
            request.id.unwrap()
        );
        for invalid in ["0", "-1", "1.5", "1e3", "9007199254740992", "null", "true", "\"1788492445123\"", "{}"] {
            assert_eq!(codec.response(valid.replace("1788492445123", invalid).as_bytes()), Err(CodecError::InvalidResponse), "accepted timestamp {invalid}");
        }
        for invalid in [
            valid.replace(",\"completed_at_ms\":1788492445123", ""),
            valid.replace("\"completed_at_ms\":1788492445123", "\"completed_at_ms\":1788492445123,\"unexpected\":true"),
            valid.replace("\"op_id\":\"op-open\"", "\"op_id\":\"other-op\""),
            valid.replace("\"action_id\":\"action-1\"", "\"action_id\":\"other-action\""),
        ] {
            assert_eq!(codec.response(invalid.as_bytes()), Err(CodecError::InvalidResponse));
        }
        let forwarded = codec.response(valid.as_bytes()).unwrap();
        assert!(forwarded.contains("connector_browser_op_result"));
        assert!(forwarded.contains("\"ok\":true"));
        assert!(forwarded.contains("\"op_id\":\"op-open\""));
        assert!(forwarded.contains("\"action_id\":\"action-1\""));
        assert!(forwarded.contains("\"bridge_id\":\"bridge-1\""));
        assert!(!forwarded.contains("completed_at_ms"));
        assert!(!forwarded.contains("1788492445123"));
    }

    #[test]
    fn operation_roundtrip_preserves_request_identity_and_redacts_errors() {
        let mut codec = ConnectorCodec::new("generation-1".into());
        let native = codec.command(&command("op-1"), "bridge-1").unwrap();
        let RpcMessage::Request(req) = rpc::parse_message(&native, Peer::Server).unwrap() else {
            panic!()
        };
        let fields = req.params.as_object().unwrap();
        assert!(!fields.contains_key("bridge_id"));
        assert!(!fields.contains_key("load_generation_id"));
        let response = format!(
            r#"{{"jsonrpc":"2.0","id":"{}","error":{{"code":-32010,"message":"secret page value","data":{{"a0_code":"OUTCOME_UNKNOWN","outcome":"unknown","retryable":false,"details":{{"password":"secret"}}}}}}}}"#,
            req.id.unwrap()
        );
        let packet = codec.response(response.as_bytes()).unwrap();
        assert!(packet.contains("connector_browser_op_result"));
        assert!(packet.contains("\"op_id\":\"op-1\""));
        assert!(packet.contains("\"bridge_id\":\"bridge-1\""));
        assert!(packet.contains("\"load_generation_id\":\"generation-1\""));
        assert!(!packet.contains("secret"));
        assert!(codec.response(response.as_bytes()).is_err());
        assert!(codec.command(&command("op-1"), "bridge-1").is_err());
    }

    #[test]
    fn transport_profile_rejects_cross_profile_operation_and_control_handlers() {
        let production = BrowserTransportProfile::fixture_production();
        let development = BrowserTransportProfile::fixture_development();
        let production_packet = command("op-profile-production");
        // Direction isolation needs an otherwise admitted development action;
        // semantic click/trusted input are intentionally production-only.
        let development_packet = production_packet
            .replace(production.handler_id(), development.handler_id())
            .replace("\"action\":\"click\"", "\"action\":\"open\"")
            .replace(
                "\"required_capabilities\":[\"click\",\"semantic_dom_v1\",\"cursor_v1\",\"trusted_input_v1\"]",
                "\"required_capabilities\":[\"open\"]",
            );

        let mut production_codec = ConnectorCodec::with_profile("generation-1".into(), production);
        assert_eq!(
            production_codec.command(&development_packet, "bridge-1"),
            Err(CodecError::InvalidPacket)
        );
        assert!(production_codec
            .command(&production_packet, "bridge-1")
            .is_ok());

        let mut development_codec =
            ConnectorCodec::with_profile("generation-1".into(), development);
        assert_eq!(
            development_codec.command(&production_packet, "bridge-1"),
            Err(CodecError::InvalidPacket)
        );
        assert!(development_codec
            .command(&development_packet, "bridge-1")
            .is_ok());

        let production_control = resolve_command("control-profile-production");
        let development_control =
            production_control.replace(production.handler_id(), development.handler_id());
        let mut production_codec = ConnectorCodec::with_profile("generation-1".into(), production);
        assert_eq!(
            production_codec.command(&development_control, "bridge-1"),
            Err(CodecError::InvalidPacket)
        );
        assert!(production_codec
            .command(&production_control, "bridge-1")
            .is_ok());

        let mut development_codec =
            ConnectorCodec::with_profile("generation-1".into(), development);
        assert_eq!(
            development_codec.command(&production_control, "bridge-1"),
            Err(CodecError::InvalidPacket)
        );
        assert!(development_codec
            .command(&development_control, "bridge-1")
            .is_ok());
    }

    #[test]
    fn development_profile_allows_fixed_operations_and_site_controls_only() {
        let production = BrowserTransportProfile::fixture_production();
        let development = BrowserTransportProfile::fixture_development();
        let dev_packet =
            |packet: String| packet.replace(production.handler_id(), development.handler_id());

        let with_action = |packet: String, action: &str| {
            packet
                .replace("\"action\":\"click\"", &format!("\"action\":\"{action}\""))
                .replace(
                    "\"required_capabilities\":[\"click\",\"semantic_dom_v1\",\"cursor_v1\",\"trusted_input_v1\"]",
                    &format!("\"required_capabilities\":[\"{action}\"]"),
                )
        };
        for action in [
            "content", "ensure", "list", "navigate", "open", "scroll", "state", "status",
        ] {
            let mut operation = ConnectorCodec::with_profile("generation-1".into(), development);
            assert!(operation
                .command(
                    &with_action(dev_packet(command(&format!("op-dev-{action}"))), action,),
                    "bridge-1",
                )
                .is_ok());
        }

        let mut operation = ConnectorCodec::with_profile("generation-1".into(), development);
        for (index, action) in ["click", "type", "screenshot", "hover"]
            .into_iter()
            .enumerate()
        {
            let packet = with_action(
                dev_packet(command(&format!("op-dev-denied-{index}"))),
                action,
            );
            assert_eq!(
                operation.command(&packet, "bridge-1"),
                Err(CodecError::UnsupportedEvent),
                "development forwarded unadmitted action {action}"
            );
        }

        let widened = with_action(dev_packet(command("op-dev-widened")), "open").replace(
            "\"required_capabilities\":[\"open\"]",
            "\"required_capabilities\":[\"open\",\"artifacts_v1\"]",
        );
        assert_eq!(
            operation.command(&widened, "bridge-1"),
            Err(CodecError::UnsupportedEvent)
        );

        let mut controls = ConnectorCodec::with_profile("generation-1".into(), development);
        assert!(controls
            .command(&dev_packet(resolve_command("control-dev-site")), "bridge-1")
            .is_ok());
        assert_eq!(
            controls.command(
                &dev_packet(action_resolve_command("control-dev-action")),
                "bridge-1",
            ),
            Err(CodecError::UnsupportedEvent)
        );
    }

    #[test]
    fn rejects_wrong_handler_unknown_events_and_mismatched_success() {
        let mut codec = ConnectorCodec::new("generation-1".into());
        assert!(codec
            .command(
                &command("op-1").replace("generation-1", "stale-generation"),
                "bridge-1"
            )
            .is_err());
        assert!(codec
            .command(
                &command("op-1").replace(HANDLER_ID, "other.Handler"),
                "bridge-1"
            )
            .is_err());
        assert!(codec
            .command(
                &command("op-1").replace("connector_browser_op", "connector_file_op"),
                "bridge-1"
            )
            .is_err());
        assert!(codec.command(&command("op-1"), "another-bridge").is_err());
        let native = codec.command(&command("op-1"), "bridge-1").unwrap();
        let RpcMessage::Request(req) = rpc::parse_message(&native, Peer::Server).unwrap() else {
            panic!()
        };
        let response = format!(
            r#"{{"jsonrpc":"2.0","id":"{}","result":{{"contract_version":1,"op_id":"another-op","action_id":"action-1","status":"succeeded","result":{{}},"receipts":[],"artifacts":[]}}}}"#,
            req.id.unwrap()
        );
        assert_eq!(
            codec.response(response.as_bytes()),
            Err(CodecError::InvalidResponse)
        );
        assert_eq!(codec.pending.len(), 1);
    }

    #[test]
    fn challenge_resolution_roundtrip_is_exactly_request_and_result_bound() {
        let mut codec = ConnectorCodec::new("generation-1".into());
        let native = codec
            .command(&resolve_command("control-1"), "bridge-1")
            .unwrap();
        let RpcMessage::Request(request) = rpc::parse_message(&native, Peer::Server).unwrap()
        else {
            panic!()
        };
        assert_eq!(request.method, "browser.resolve_challenge");
        let params = request.params.as_object().unwrap();
        assert!(!params.contains_key("bridge_id"));
        assert!(!params.contains_key("load_generation_id"));
        assert_eq!(
            params.get("challenge_id").and_then(Value::as_str),
            Some("challenge-1")
        );
        let native_id = request.id.unwrap();

        let mismatched = format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"id\":\"{native_id}\",\"result\":{{",
                "\"contract_version\":1,\"control_id\":\"control-1\",",
                "\"challenge_id\":\"challenge-1\",\"status\":\"resolved\",",
                "\"decision\":\"allow_turn\"}}}}"
            ),
            native_id = native_id,
        );
        assert_eq!(
            codec.response(mismatched.as_bytes()),
            Err(CodecError::InvalidResponse)
        );

        let response = format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"id\":\"{native_id}\",\"result\":{{",
                "\"contract_version\":1,\"control_id\":\"control-1\",",
                "\"challenge_id\":\"challenge-1\",\"status\":\"resolved\",",
                "\"decision\":\"allow_once\"}}}}"
            ),
            native_id = native_id,
        );
        let packet = codec.response(response.as_bytes()).unwrap();
        assert!(packet.contains("connector_browser_control_result"));
        assert!(packet.contains("\"method\":\"browser.resolve_challenge\""));
        assert!(packet.contains("\"bridge_id\":\"bridge-1\""));
        assert!(packet.contains("\"op_id\":\"op-1\""));
        assert!(packet.contains("\"challenge_id\":\"challenge-1\""));
        assert!(packet.contains("\"decision\":\"allow_once\""));
    }

    #[test]
    fn challenge_resolution_rejects_unbound_or_noncanonical_core_fields() {
        let mut codec = ConnectorCodec::new("generation-1".into());
        assert_eq!(
            codec.command(
                &resolve_command("control-1").replace(
                    "https://example.test\",\"action_class",
                    "https://EXAMPLE.test\",\"action_class",
                ),
                "bridge-1",
            ),
            Err(CodecError::InvalidPacket)
        );
        assert_eq!(
            codec.command(
                &resolve_command("control-2")
                    .replace("\"scope\":\"operation\"", "\"scope\":\"turn\"",),
                "bridge-1",
            ),
            Err(CodecError::InvalidPacket)
        );
        assert_eq!(
            codec.command(
                &resolve_command("control-3").replace(
                    "\"document_epoch\":0",
                    "\"document_epoch\":0,\"unexpected\":true",
                ),
                "bridge-1",
            ),
            Err(CodecError::InvalidPacket)
        );
    }

    #[test]
    fn action_resolution_roundtrip_preserves_exact_decision_binding() {
        let mut codec = ConnectorCodec::new("generation-1".into());
        let native = codec
            .command(&action_resolve_command("control-2"), "bridge-1")
            .unwrap();
        let RpcMessage::Request(request) = rpc::parse_message(&native, Peer::Server).unwrap()
        else {
            panic!()
        };
        let native_id = request.id.unwrap();
        let response = format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"id\":\"{native_id}\",\"result\":{{",
                "\"contract_version\":1,\"control_id\":\"control-2\",",
                "\"challenge_id\":\"challenge-2\",\"status\":\"resolved\",",
                "\"decision\":\"approve_once\"}}}}"
            ),
            native_id = native_id,
        );
        let packet = codec.response(response.as_bytes()).unwrap();
        assert!(packet.contains("\"challenge_id\":\"challenge-2\""));
        assert!(packet.contains("\"decision\":\"approve_once\""));
        assert!(packet.contains("\"op_id\":\"op-2\""));
        assert!(packet.contains("\"action_id\":\"action-2\""));
    }

    #[test]
    fn type_resolution_requires_full_tagged_classification_binding() {
        let mut codec = ConnectorCodec::new("generation-1".into());
        let native = codec
            .command(&type_resolve_command("control-type-1"), "bridge-1")
            .unwrap();
        let RpcMessage::Request(request) = rpc::parse_message(&native, Peer::Server).unwrap()
        else {
            panic!()
        };
        let params = request.params.as_object().unwrap();
        assert_eq!(
            params
                .get("data_classification")
                .and_then(Value::as_object)
                .and_then(|value| value.get("text_sha256"))
                .and_then(Value::as_str),
            Some(DIGEST)
        );

        let valid = type_resolve_command("control-type-2");
        let grant_marker = "\"grant\":{";
        let grant_start = valid.find(grant_marker).unwrap();
        let (prefix, grant) = valid.split_at(grant_start);
        let mismatched = format!(
            "{prefix}{}",
            grant.replacen(
                &format!("\"text_sha256\":\"{DIGEST}\""),
                &format!("\"text_sha256\":\"{}\"", "f".repeat(64)),
                1,
            )
        );
        assert_eq!(
            codec.command(&mismatched, "bridge-1"),
            Err(CodecError::InvalidPacket)
        );
    }

    #[test]
    fn type_success_is_exactly_bound_and_does_not_retain_or_return_text() {
        const PRIVATE_TEXT: &str = "private proposed text 🚀";
        let mut codec = ConnectorCodec::new("generation-1".into());
        let native = codec
            .command(&type_command("op-type", PRIVATE_TEXT), "bridge-1")
            .unwrap();
        let RpcMessage::Request(request) = rpc::parse_message(&native, Peer::Server).unwrap()
        else {
            panic!()
        };
        assert_eq!(
            request
                .params
                .as_object()
                .and_then(|params| params.get("args"))
                .and_then(Value::as_object)
                .and_then(|args| args.get("text"))
                .and_then(Value::as_str),
            Some(PRIVATE_TEXT)
        );
        let pending = codec.pending.values().next().unwrap();
        assert!(!pending.response_bindings.contains_key("text"));
        assert!(!pending.response_bindings.contains_key("text_sha256"));

        let native_id = request.id.unwrap();
        let valid = format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"id\":\"{native_id}\",\"result\":{{",
                "\"contract_version\":1,\"op_id\":\"op-type\",\"action_id\":\"action-1\",",
                "\"status\":\"succeeded\",\"result\":{{",
                "\"lease_id\":\"lease-1\",",
                "\"browser_id\":\"a0t1.fixture-generation.fixture-lease\",",
                "\"tab_handle\":\"a0t1.fixture-generation.fixture-lease\",",
                "\"document_epoch\":\"1\",\"ref\":\"frame0:node24\",",
                "\"action_class\":\"sensitive_input\"}},",
                "\"receipts\":[],\"artifacts\":[],\"completed_at_ms\":1788492445123}}}}"
            ),
            native_id = native_id,
        );
        assert_eq!(
            codec.response(
                valid
                    .replace("\"sensitive_input\"", "\"unknown\"")
                    .as_bytes()
            ),
            Err(CodecError::InvalidResponse)
        );
        assert_eq!(
            codec.response(
                valid
                    .replace("\"ref\":\"frame0:node24\"", "\"ref\":\"other\"")
                    .as_bytes()
            ),
            Err(CodecError::InvalidResponse)
        );
        assert_eq!(
            codec.response(
                valid
                    .replace(
                        "\"artifacts\":[]",
                        &format!(
                            concat!(
                                "\"artifacts\":[{{\"artifact_id\":\"artifact-1\",",
                                "\"mime_type\":\"image/png\",\"byte_count\":1,",
                                "\"sha256\":\"sha256:{DIGEST}\",\"purpose\":\"screenshot\"}}]"
                            ),
                            DIGEST = DIGEST,
                        ),
                    )
                    .as_bytes()
            ),
            Err(CodecError::InvalidResponse)
        );

        let packet = codec.response(valid.as_bytes()).unwrap();
        assert!(packet.contains("\"action_class\":\"sensitive_input\""));
        assert!(!packet.contains(PRIVATE_TEXT));
        assert!(!packet.contains("text_sha256"));
    }

    #[test]
    fn click_success_is_bound_to_request_target_ref_and_conservative_class() {
        let mut codec = ConnectorCodec::new("generation-1".into());
        let native = codec.command(&command("op-click"), "bridge-1").unwrap();
        let RpcMessage::Request(request) = rpc::parse_message(&native, Peer::Server).unwrap()
        else {
            panic!()
        };
        let native_id = request.id.unwrap();
        let valid = format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"id\":\"{native_id}\",\"result\":{{",
                "\"contract_version\":1,\"op_id\":\"op-click\",\"action_id\":\"action-1\",",
                "\"status\":\"succeeded\",\"result\":{{",
                "\"lease_id\":\"lease-1\",",
                "\"browser_id\":\"a0t1.fixture-generation.fixture-lease\",",
                "\"tab_handle\":\"a0t1.fixture-generation.fixture-lease\",",
                "\"document_epoch\":\"1\",\"ref\":\"frame0:node24\",",
                "\"action_class\":\"unknown\"}},\"receipts\":[],\"artifacts\":[],\"completed_at_ms\":1788492445123}}}}"
            ),
            native_id = native_id,
        );
        let mismatched = valid.replace("\"ref\":\"frame0:node24\"", "\"ref\":\"other\"");
        assert_eq!(
            codec.response(mismatched.as_bytes()),
            Err(CodecError::InvalidResponse)
        );
        let packet = codec.response(valid.as_bytes()).unwrap();
        assert!(packet.contains("\"op_id\":\"op-click\""));
        assert!(packet.contains("\"action_class\":\"unknown\""));

        let mut codec = ConnectorCodec::new("generation-1".into());
        let native = codec.command(&command("op-click-2"), "bridge-1").unwrap();
        let RpcMessage::Request(request) = rpc::parse_message(&native, Peer::Server).unwrap()
        else {
            panic!()
        };
        let invalid = valid
            .replace(&native_id, request.id.as_deref().unwrap())
            .replace("\"op_id\":\"op-click\"", "\"op_id\":\"op-click-2\"")
            .replace("\"document_epoch\":\"1\"", "\"document_epoch\":\"01\"");
        assert_eq!(
            codec.response(invalid.as_bytes()),
            Err(CodecError::InvalidResponse)
        );
    }
}
